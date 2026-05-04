"""Autopilot v2 daemon — the main loop.

This is a Python scheduler (not a Claude session). It runs two concurrent loops:

  _main_tick_loop — scheduled work (cron, pipeline sweep, task dispatch,
                    ideation, watchdog). Tick interval AP2_TICK_S (30s).
  _mm_loop        — Mattermost polling (TB-122). Runs on AP2_MM_TICK_S (10s)
                    so operator messages are handled promptly even while a
                    task agent is running. Each new mention spawns an
                    asyncio.create_task(handle_message(...)) so concurrent
                    mentions don't serialize.

Each unit of work is a fresh SDK `query()` call, so contexts never accumulate.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import diagnose, events, ideation, prompts, retry, rollback, tools, verify, web
from .board import Board, board_file_lock
from .config import Config
from .cron import (
    CronJob,
    bootstrap as bootstrap_cron,
    due_jobs,
    load_jobs,
    load_state,
    mark_run,
)
from .mattermost import check_new_messages
from .result import TaskResult
from .tools import (
    CONTROL_AGENT_TOOLS,
    MM_HANDLER_TOOLS,
    TASK_AGENT_FENCED_PATHS,
    TASK_AGENT_TOOLS,
    build_mcp_server,
    do_board_edit,
)


def _task_disallowed_tools() -> list[str]:
    """Edit/Write blocks for fenced paths plus the always-on Bash blocks.

    Built once at module load via this helper for testability — a unit test
    can assert each fenced path produces both an `Edit(<path>)` and a
    `Write(<path>)` entry without spinning up the SDK.
    """
    fenced = []
    for path in TASK_AGENT_FENCED_PATHS:
        fenced.append(f"Edit({path})")
        fenced.append(f"Write({path})")
    return ["Bash(git push*)", "Bash(rm -rf *)", *fenced]


_TASK_DISALLOWED_TOOLS = _task_disallowed_tools()


RUNNING = True

# Module-level dedup so we don't re-emit board_malformed_line every tick for
# the same offending line. Cleared on daemon restart.
_SEEN_MALFORMED: set[str] = set()


def _handle_signal(signum, frame):  # noqa: ARG001
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def run_task(cfg: Config, sdk, mcp_server, task) -> None:
    """Execute a single Ready task in an isolated SDK query()."""
    prompt = prompts.build_task_prompt(cfg, task)
    events.append(cfg.events_file, "task_start", task=task.id, title=task.title)
    # TB-110 rollback boundary: capture HEAD BEFORE the daemon writes
    # `move_to_active` to TASKS.md. If the agent later violates the
    # state-file fence, `git reset --hard <pre_run_head>` restores the
    # entire system (TASKS.md + everything else in `_STATE_FILE_NAMES`)
    # to its pre-task-dispatch shape.
    pre_run_head = rollback.git_head(cfg)
    do_board_edit(cfg, {"action": "move_to_active", "task_id": task.id})
    # Snapshot fenced files AFTER move_to_active so the daemon's own
    # legitimate write doesn't show up as a violation. Anything the
    # agent then mutates (committed or working-tree only) shows up as
    # a hash mismatch on the post-run snapshot.
    pre_run_fenced = rollback.snapshot_fenced_files(cfg)

    # Pre-flight debug dump: if the SDK subprocess crashes with an empty
    # stderr (observed on stoch's TB-58/TB-59), these files are the only way
    # to reproduce the failure. Dumped BEFORE the query starts so even a
    # SIGKILL-before-write leaves us the prompt.
    #
    # Two-layer message log (TB-85): `.stream.jsonl` holds compact per-envelope
    # summaries (first 200 chars of any text block, tool name + truncated args,
    # tool-result preview) for at-a-glance reading. `.messages.jsonl` mirrors
    # the same `seq` ordering with FULL content. Diagnose by scanning the
    # stream, then `jq 'select(.seq==N)'` on messages.jsonl for the full body.
    prompt_dump, stream_dump, messages_dump = _prep_debug_dumps(cfg, task.id)
    prompt_dump.write_text(prompt)

    stderr_lines, _stderr_sink = _make_stderr_sink()

    # Captures the structured payload from a `report_result` MCP tool call
    # (TB-101 / TB-104). This is the only completion signal task agents
    # emit; legacy `RESULT:` text-block parsing was removed in TB-104.
    # When no tool call lands, the daemon treats `parsed.status` as
    # "unknown" and routes through HEAD-recovery (`_infer_result_from_head`)
    # — if the agent committed with the mandated `<TB-N>: ...` subject
    # prefix the work is salvaged; otherwise the task shelves to Backlog
    # for retry.
    task_complete_args: dict = {}

    # TB-114: capture every `pipeline_task_start` tool call the agent makes
    # during its run — used after the agent returns to decide whether to
    # park the task in Pipeline Pending (instead of Complete) and to record
    # which pids are blocking it. We capture both the tool args (name,
    # command) and the daemon's tool-result payload (pid, started_at, log).
    pipeline_starts: list[dict] = []
    pipeline_args_by_id: dict[str, dict] = {}

    # Ring buffer of stream-message summaries so we can attach the last few
    # messages to the error event; dumps to disk for full history.
    stream_log: list[dict] = []
    seq = [0]  # mutable closure counter

    def _log_message(msg) -> None:
        idx = seq[0]
        seq[0] += 1
        summary = {"seq": idx, **_summarize_message(msg)}
        stream_log.append(summary)
        if len(stream_log) > 200:
            del stream_log[: len(stream_log) - 200]
        import json as _json
        with stream_dump.open("a") as f:
            f.write(_json.dumps(summary, default=str) + "\n")
        with messages_dump.open("a") as f:
            full = {"seq": idx, **_serialize_message_full(msg)}
            f.write(_json.dumps(full, default=str) + "\n")
        # TB-101: capture the structured payload from `report_result` tool
        # calls. The real SDK delivers the tool name with the MCP server
        # prefix (`mcp__autopilot__report_result`); FakeSDK in unit tests
        # uses the bare name (`report_result`). Match both. Last-write-wins
        # if the agent calls the tool more than once.
        for part in (getattr(msg, "content", None) or []):
            pname = getattr(part, "name", None)
            if pname in ("report_result", "mcp__autopilot__report_result"):
                inp = getattr(part, "input", None)
                if isinstance(inp, dict):
                    task_complete_args.clear()
                    task_complete_args.update(inp)
            elif pname in (
                "pipeline_task_start", "mcp__autopilot__pipeline_task_start",
            ):
                inp = getattr(part, "input", None)
                if isinstance(inp, dict):
                    use_id = getattr(part, "id", None) or ""
                    pipeline_args_by_id[use_id] = dict(inp)
            else:
                # Tool result block: pair it with its tool_use_id so we can
                # learn the daemon-side pid/started_at for each
                # pipeline_task_start call.
                tu_id = getattr(part, "tool_use_id", None)
                if not tu_id or tu_id not in pipeline_args_by_id:
                    continue
                content = getattr(part, "content", None)
                payload = _extract_tool_result_payload(content)
                if not isinstance(payload, dict):
                    continue
                started_args = pipeline_args_by_id.pop(tu_id)
                pipeline_starts.append({
                    "name": str(started_args.get("name") or "").strip(),
                    "command": str(started_args.get("command") or "").strip(),
                    "pid": payload.get("pid"),
                    "started_at": payload.get("started_at"),
                    "log": payload.get("log") or "",
                })

    async def _consume() -> str:
        text = ""
        async for msg in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=TASK_AGENT_TOOLS,
                disallowed_tools=_TASK_DISALLOWED_TOOLS,
                permission_mode="bypassPermissions",
                max_turns=int(os.environ.get("AP2_TASK_MAX_TURNS", 50)),
                setting_sources=["project"],
                stderr=_stderr_sink,
                model=os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7"),
                extra_args={"effort": os.environ.get("AP2_AGENT_EFFORT", "xhigh")},
            ),
        ):
            _log_message(msg)
            t = _extract_text(msg)
            if t:
                text = t
        return text

    # If consume hits a timeout / opaque SDK crash without HEAD salvage we
    # defer the corresponding `_handle_failure` until AFTER the TB-110
    # violation check below — so a fenced-file mutation made before the
    # crash still gets the rollback + state_violation routing instead of
    # whatever generic failure status the consume branch would otherwise
    # have stamped.
    early_failure_status: str | None = None
    early_failure_extras: dict[str, str] = {}
    result_text = ""
    # TB-123: plumb the current task id into a contextvar so MCP tool handlers
    # (specifically `do_cron_propose`) can stamp `proposed_by_task` on the
    # `cron_proposed` event without forcing the agent to pass its own id.
    # Reset right after `_consume()` returns so a subsequent run / unrelated
    # tool dispatch sees a clean slate.
    _ctx_token = tools._task_id_ctx.set(task.id)
    try:
        result_text = await asyncio.wait_for(_consume(), timeout=cfg.task_timeout_s)
    except asyncio.TimeoutError:
        # Even on timeout, the agent may have committed before stalling — check
        # HEAD before declaring failure. Same recovery path as the post-run
        # status=unknown fallback below.
        inferred = _infer_result_from_head(cfg, task)
        if inferred is not None:
            events.append(
                cfg.events_file, "task_implicit_commit",
                task=task.id, commit=inferred.commit,
                subject=inferred.summary, reason="timeout_recovered",
            )
            parsed_override = inferred  # type: ignore[assignment]
        else:
            events.append(
                cfg.events_file,
                "task_timeout",
                task=task.id,
                timeout_s=cfg.task_timeout_s,
                stderr_tail="\n".join(stderr_lines[-30:]),
                last_messages=stream_log[-10:],
                prompt_dump=str(prompt_dump),
                stream_dump=str(stream_dump),
                messages_dump=str(messages_dump),
            )
            early_failure_status = "timeout"
            early_failure_extras = {
                "timeout_s": str(cfg.task_timeout_s),
                "stderr_tail": "\n".join(stderr_lines[-30:]),
            }
    except Exception as e:  # noqa: BLE001
        # Same logic for an opaque SDK subprocess crash (the "exit code 1 /
        # empty stderr_tail" pattern observed on stoch's TB-58/59). If the
        # agent reached the commit turn before the crash, HEAD will name the
        # task and we can salvage the run.
        inferred = _infer_result_from_head(cfg, task)
        if inferred is not None:
            events.append(
                cfg.events_file, "task_implicit_commit",
                task=task.id, commit=inferred.commit,
                subject=inferred.summary, reason="error_recovered",
            )
            parsed_override = inferred  # type: ignore[assignment]
        else:
            events.append(
                cfg.events_file,
                "task_error",
                task=task.id,
                error=f"{type(e).__name__}: {e}",
                stderr_tail="\n".join(stderr_lines[-30:]),
                last_messages=stream_log[-10:],
                prompt_dump=str(prompt_dump),
                stream_dump=str(stream_dump),
                messages_dump=str(messages_dump),
            )
            early_failure_status = "error"
            early_failure_extras = {
                "error": f"{type(e).__name__}: {e}",
                "stderr_tail": "\n".join(stderr_lines[-30:]),
            }
    finally:
        # TB-123: clear the task-id contextvar regardless of how the SDK
        # query exited (success, timeout, or arbitrary exception).
        tools._task_id_ctx.reset(_ctx_token)

    # TB-110: post-hoc state-file violation check. Hash-compare fenced
    # files against the pre_run_fenced snapshot taken right after
    # move_to_active. Any difference (committed by the agent or just
    # dirtied in the working tree) is a violation. We preempt any other
    # status decision: the rollback wipes the run wholesale via
    # `git reset --hard <pre_run_head>`, restoring every fenced file +
    # the rest of `_STATE_FILE_NAMES` (TB-112) coherently. Routes through
    # `_handle_failure(status="state_violation")` so retries count and
    # repeated violations exhaust to Frozen.
    violations = rollback.detect_fenced_violations(cfg, pre_run_fenced)
    if violations:
        events.append(
            cfg.events_file,
            "task_state_violation",
            task=task.id,
            fenced_files=violations,
            pre_run_head=pre_run_head,
        )
        if pre_run_head:
            with board_file_lock(cfg.tasks_file):
                try:
                    rollback.linear_rollback_to(cfg, pre_run_head)
                except Exception as exc:  # noqa: BLE001
                    events.append(
                        cfg.events_file,
                        "rollback_error",
                        task=task.id,
                        boundary=pre_run_head,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        _handle_failure(
            cfg, task,
            status="state_violation",
            debug_paths={
                "prompt": str(prompt_dump),
                "stream": str(stream_dump),
                "messages": str(messages_dump),
            },
            extras={
                "fenced_files": ", ".join(violations),
                "pre_run_head": (pre_run_head or "")[:8] or "(no git repo)",
            },
        )
        events.append(
            cfg.events_file,
            "task_complete",
            task=task.id,
            status="state_violation",
            commit="",
            summary="(state-file violation; agent run reverted)",
        )
        board_after = Board.load(cfg.tasks_file)
        loc = board_after.find(task.id)
        dest = loc[0] if loc else "?"
        _commit_state_files(
            cfg, f"state: {task.id} → {dest}",
            paths=_task_state_paths(task),
        )
        return

    # No violation. If consume failed without HEAD salvage we now do the
    # deferred failure handling and return — preserving the original
    # behavior of the timeout / error path (no `task_complete` event,
    # no state-file commit).
    if early_failure_status is not None:
        _handle_failure(
            cfg, task,
            status=early_failure_status,
            debug_paths={
                "prompt": str(prompt_dump),
                "stream": str(stream_dump),
                "messages": str(messages_dump),
            },
            extras=early_failure_extras,
        )
        return

    # Result-payload precedence (TB-101 / TB-104):
    #   1. HEAD-recovery override (crash / timeout salvage path).
    #   2. `report_result` MCP tool call — structured, no regex parsing.
    #   3. Neither fired → status="unknown", routes through HEAD-recovery
    #      below (`_infer_result_from_head`). If the agent committed with
    #      the mandated `<TB-N>: ...` subject prefix the work is salvaged;
    #      otherwise the task shelves to Backlog for retry.
    if 'parsed_override' in locals():
        parsed = parsed_override
    elif task_complete_args:
        parsed = _task_result_from_tool_args(task_complete_args)
    else:
        parsed = TaskResult(status="unknown", raw=(result_text or "")[-500:])
    if parsed.status not in _VALID_RESULT_STATUSES:
        inferred = _infer_result_from_head(cfg, task)
        if inferred is not None:
            events.append(
                cfg.events_file,
                "task_implicit_commit",
                task=task.id,
                commit=inferred.commit,
                subject=inferred.summary,
                reason="status_unknown",
            )
            parsed = inferred
    commit_hash = parsed.commit
    final_status = parsed.status
    # TB-114: if the agent dispatched any pipelines via `pipeline_task_start`,
    # the work isn't really done — pipeline subprocesses are still running.
    # Park the task in `Pipeline Pending`, emit a `task_pipeline_pending`
    # event with the captured pids, and skip both verifier paths (they'd
    # check for output artifacts the pipelines haven't produced yet). The
    # daemon's per-tick Pipeline Pending sweep re-runs verification once
    # every pid for this task has died.
    pipelines_for_task = [
        p for p in pipeline_starts
        if isinstance(p.get("pid"), int)
    ]
    if parsed.status == "complete" and pipelines_for_task:
        do_board_edit(cfg, {
            "action": "move_to_pipeline_pending", "task_id": task.id,
        })
        events.append(
            cfg.events_file,
            "task_pipeline_pending",
            task=task.id,
            commit=parsed.commit,
            summary=parsed.summary[:300],
            pipelines=[
                {
                    "name": p.get("name", ""),
                    "pid": p["pid"],
                    "started_at": p.get("started_at"),
                    "log": p.get("log", ""),
                }
                for p in pipelines_for_task
            ],
        )
        events.append(
            cfg.events_file,
            "task_complete",
            task=task.id,
            status="pipeline_pending",
            commit=parsed.commit,
            summary=parsed.summary[:300],
        )
        board_after = Board.load(cfg.tasks_file)
        loc = board_after.find(task.id)
        dest = loc[0] if loc else "?"
        _commit_state_files(
            cfg, f"state: {task.id} → {dest}",
            paths=_task_state_paths(task),
        )
        return
    if parsed.status == "complete":
        # Project-wide regression gate: when AP2_VERIFY_CMD is set, run it
        # against HEAD (post-agent-commit) before declaring Complete. Failure
        # routes through `_handle_failure` like any other task failure
        # (Backlog → retry → Frozen on exhaustion). Skipped when the env var
        # is empty, when the task carries `#no-verify`, or when an explicit
        # crash-recovery / timeout-recovery path produced this result (the
        # agent's HEAD already represents the recovered state and we don't
        # want a verify failure to mask a successful recovery — see
        # _infer_result_from_head).
        verify_res = _run_verify(cfg, task)
        if verify_res is not None and not verify_res.passed:
            events.append(
                cfg.events_file,
                "verification_failed",
                task=task.id,
                command=verify_res.command,
                exit_code=verify_res.exit_code,
                stderr_tail=verify_res.stderr_tail,
                duration_s=round(verify_res.duration_s, 2),
            )
            _handle_failure(
                cfg, task,
                status="verification_failed",
                debug_paths={
                    "prompt": str(prompt_dump),
                    "stream": str(stream_dump),
                    "messages": str(messages_dump),
                },
                extras={
                    "kind": "project_wide",
                    "verify_command": verify_res.command,
                    "exit_code": str(verify_res.exit_code),
                    "stderr_tail": verify_res.stderr_tail[:300],
                },
            )
            final_status = "verification_failed"
        else:
            # Per-task verification (TB-69): run the briefing's `## Verification`
            # bullets after the project-wide gate (TB-66) but before
            # move_to_complete. Skip when no briefing or no section.
            per_verdict = await _maybe_per_task_verify(cfg, sdk, task)
            if per_verdict is not None and per_verdict.overall == "fail":
                events.append(
                    cfg.events_file,
                    "verification_failed",
                    task=task.id,
                    kind="per_task",
                    overall=per_verdict.overall,
                    criteria=[
                        {"kind": c.kind, "status": c.status,
                         "bullet": c.bullet[:200], "notes": c.notes[:200]}
                        for c in per_verdict.criteria
                    ],
                    duration_s=round(per_verdict.duration_s, 2),
                )
                _handle_failure(
                    cfg, task,
                    status="verification_failed",
                    debug_paths={
                        "prompt": str(prompt_dump),
                        "stream": str(stream_dump),
                        "messages": str(messages_dump),
                    },
                    extras={
                        "kind": "per_task",
                        "failed_criteria": "; ".join(
                            f"[{c.status}] {c.bullet[:120]}"
                            for c in per_verdict.criteria
                            if c.status == "fail"
                        )[:400] or "(no criteria captured)",
                    },
                )
                final_status = "verification_failed"
            else:
                if per_verdict is not None and per_verdict.overall == "partial":
                    events.append(
                        cfg.events_file,
                        "verification_partial",
                        task=task.id,
                        criteria=[
                            {"kind": c.kind, "status": c.status,
                             "bullet": c.bullet[:200], "notes": c.notes[:200]}
                            for c in per_verdict.criteria
                        ],
                    )
                do_board_edit(cfg, {"action": "move_to_complete", "task_id": task.id})
                retry.reset_attempt(cfg.retry_state_file, task.id)
                _append_progress(cfg, task, parsed)
                # TB-123: cron-proposal moved off result-parsing onto the
                # `cron_propose` MCP tool. `cron_proposed` events fire from
                # `do_cron_propose` during the SDK query (with `proposed_by_task`
                # set via the contextvar above). No post-run dispatch step.
                # Delete the per-run debug dumps on success — only keep evidence for
                # failures (parsed.status in {incomplete, blocked, failed}) or crashes.
                for p in (prompt_dump, stream_dump, messages_dump):
                    try:
                        p.unlink()
                    except FileNotFoundError:
                        pass
    else:
        _handle_failure(
            cfg, task,
            status=parsed.status,
            parsed=parsed,
            debug_paths={
                "prompt": str(prompt_dump),
                "stream": str(stream_dump),
                "messages": str(messages_dump),
            },
            extras={
                "commit": (parsed.commit or "")[:8] or "(no commit)",
            } if parsed.commit else None,
        )
    events.append(
        cfg.events_file,
        "task_complete",
        task=task.id,
        status=final_status,
        commit=commit_hash,
        summary=parsed.summary[:300],
    )
    # Commit state-file updates (TASKS.md, progress.md, retry_state.json,
    # the task's briefing) right after the task agent's own source-code
    # commit. Narrowed allowlist (TB-126) ensures unrelated dirty briefings
    # in `.cc-autopilot/tasks/` don't ride along — `git log -- <file>`
    # blames the right state commit on revert/bisect.
    board_after = Board.load(cfg.tasks_file)
    loc = board_after.find(task.id)
    dest = loc[0] if loc else "?"
    _commit_state_files(
        cfg, f"state: {task.id} → {dest}",
        paths=_task_state_paths(task),
    )


def _handle_failure(
    cfg: Config,
    task,
    *,
    status: str,
    parsed: TaskResult | None = None,
    debug_paths: dict[str, str] | None = None,
    extras: dict[str, str] | None = None,
) -> None:
    """Move a failed task to Backlog, or Frozen if it has exhausted retries.

    TB-114: ALWAYS appends a `## Attempts` entry to the briefing — for
    every failure mode (timeout, error, state_violation, verification_failed,
    incomplete/blocked/failed). The next attempt's agent can `Read` the
    briefing and see the full failure narrative + debug-dump paths to
    pick up where the prior attempt left off.
    """
    attempts = retry.bump_attempt(cfg.retry_state_file, task.id)
    if attempts >= cfg.max_retries:
        do_board_edit(cfg, {"action": "move_to_frozen", "task_id": task.id})
        events.append(
            cfg.events_file,
            "retry_exhausted",
            task=task.id,
            attempts=attempts,
            last_status=status,
        )
    else:
        do_board_edit(cfg, {"action": "move_to_backlog", "task_id": task.id})
    summary = (parsed.summary if parsed is not None else "") or ""
    _append_attempts(
        cfg, task,
        status=status,
        summary=summary,
        debug_paths=debug_paths,
        extras=extras,
    )


def _recover_orphans(cfg: Config) -> None:
    """Move any task left in Active back to Ready (crashed mid-run)."""
    if not cfg.tasks_file.exists():
        return
    board = Board.load(cfg.tasks_file)
    orphans = [t.id for t in board.iter_tasks("Active")]
    for tid in orphans:
        do_board_edit(cfg, {"action": "move_to_ready", "task_id": tid})
        events.append(cfg.events_file, "orphan_recovery", task=tid)
    if orphans:
        # Orphan recovery only mutates TASKS.md (move_to_ready writes the
        # board). retry_state / progress / briefings are not touched here.
        _commit_state_files(
            cfg,
            f"state: recovered {len(orphans)} orphan(s): {', '.join(orphans)}",
            paths=["TASKS.md"],
        )


async def handle_message(cfg: Config, sdk, mcp_server, msg: dict) -> None:
    """Run a Mattermost handler agent for one mention.

    TB-145: handler ALWAYS uses `MM_HANDLER_TOOLS` — no board-state
    snapshot, no FULL/RESTRICTED toggle. The previous TB-122 design
    consulted `Board.iter_tasks("Active")` to pick between two toolset
    variants, but the check was a TOCTOU race against the daemon's main
    tick loop (a Backlog task could be promoted mid-handler-turn,
    leaving the handler with `cron_edit` / `board_edit` it shouldn't
    have). The unconditional restricted toolset closes that race
    surface; the convenience cost (cron + ideation-state edits via chat)
    is recoverable through the CLI.
    """
    prompt = prompts.build_mattermost_prompt(cfg, msg)
    events.append(
        cfg.events_file,
        "mattermost",
        channel=msg.get("channel_name"),
        user=msg.get("user"),
        thread_id=msg.get("thread_id"),
        summary=(msg.get("text") or "")[:300],
        # TB-145: toolset is always the restricted set now; field kept
        # for events.jsonl audit-trail continuity (downstream consumers
        # may filter on it).
        toolset="restricted",
    )

    async def _consume() -> None:
        async for _ in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=MM_HANDLER_TOOLS,
                permission_mode="bypassPermissions",
                max_turns=int(os.environ.get("AP2_CONTROL_MAX_TURNS", 15)),
                setting_sources=["project"],
                model=os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7"),
                extra_args={"effort": os.environ.get("AP2_AGENT_EFFORT", "xhigh")},
            ),
        ):
            pass

    try:
        await asyncio.wait_for(_consume(), timeout=cfg.control_timeout_s)
    except asyncio.TimeoutError:
        events.append(
            cfg.events_file,
            "mattermost_timeout",
            timeout_s=cfg.control_timeout_s,
            thread_id=msg.get("thread_id"),
        )
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "mattermost_error",
            error=f"{type(e).__name__}: {e}",
        )


def _make_stderr_sink() -> tuple[list[str], "callable"]:
    """200-line ring buffer for SDK subprocess stderr, plus an `stderr=`
    callback the SDK will invoke per line. The caller pulls
    ``"\\n".join(buf[-30:])`` for failure-event payloads.

    Without this, the SDK raises ProcessError with the useless "Check
    stderr output for details" sentinel and no way to see the actual
    crash. Used by both `run_task` and `_run_control_agent`.
    """
    buf: list[str] = []

    def sink(line: str) -> None:
        buf.append(line)
        if len(buf) > 200:
            del buf[: len(buf) - 200]

    return buf, sink


async def _run_control_agent(
    cfg: Config,
    sdk,
    mcp_server,
    *,
    label: str,
    prompt: str,
    allowed_tools,
    max_turns: int,
    effort: str | None = None,
) -> tuple[bool, str | None, str, Path]:
    """SDK plumbing for control-agent runs (cron jobs, ideation).

    Returns ``(timed_out, error, stderr_tail, prompt_dump_path)``. On
    success: ``(False, None, "", path)``. On timeout: ``(True, None,
    tail, path)``. On any other exception: ``(False, "<Type>: <msg>",
    tail, path)``. The caller owns the surrounding event vocabulary,
    cooldown bookkeeping, and state commit.

    TB-156: ``effort`` lets a caller override the reasoning-effort budget
    for this specific invocation. When ``None`` (the default) we fall back
    to the global ``AP2_AGENT_EFFORT`` env (default ``xhigh``) so existing
    callers keep their pre-TB-156 behavior. Per-call-site lowering (e.g.
    status-report) is opt-in: the caller computes its own effort using
    its own per-site env knob and passes it explicitly.
    """
    prompt_dump, _, _ = _prep_debug_dumps(cfg, label)
    prompt_dump.write_text(prompt)
    stderr_lines, stderr_sink = _make_stderr_sink()

    resolved_effort = (
        effort if effort is not None
        else os.environ.get("AP2_AGENT_EFFORT", "xhigh")
    )

    async def _consume() -> None:
        async for _ in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=allowed_tools,
                permission_mode="bypassPermissions",
                max_turns=max_turns,
                setting_sources=["project"],
                stderr=stderr_sink,
                model=os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7"),
                extra_args={"effort": resolved_effort},
            ),
        ):
            pass

    try:
        await asyncio.wait_for(_consume(), timeout=cfg.control_timeout_s)
    except asyncio.TimeoutError:
        return True, None, "\n".join(stderr_lines[-30:]), prompt_dump
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", "\n".join(stderr_lines[-30:]), prompt_dump
    return False, None, "", prompt_dump


# TB-144: status-report internals (skip-gate + boring-types + prompt body
# + the shared `run_status_report` callable) live in `ap2.status_report`
# now so the chat-trigger MCP tool can share them with the cron path. Keep
# these re-exports so existing call sites (`from ap2.daemon import
# _status_report_should_skip` in `tests/test_status_report_skip.py`) still
# resolve — the symbol moved, the import contract didn't.
from . import status_report as _status_report_mod
_STATUS_REPORT_BORING_TYPES = _status_report_mod._STATUS_REPORT_BORING_TYPES
_status_report_should_skip = _status_report_mod._status_report_should_skip


async def run_cron(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    # TB-144: status-report jobs delegate to the shared routine in
    # `ap2.status_report` so the cron path and the chat-trigger MCP tool
    # (`mcp__autopilot__status_report_run`) share one prompt, one
    # skip-gate (TB-128), and one event vocabulary. The cron path passes
    # `trigger="cron"` so `cron_state[status-report].last_run` advances
    # (the chat path explicitly does NOT advance it — operator-triggered
    # reports must not silence the next scheduled cron). `job.prompt` is
    # ignored for this job: the routine uses `STATUS_REPORT_PROMPT`
    # verbatim so an operator's stale `cron.yaml` doesn't drift from the
    # canonical contract.
    if job.name == "status-report":
        await _status_report_mod.run_status_report(
            cfg, sdk, mcp_server,
            trigger="cron",
            max_turns=job.max_turns,
        )
        return

    prompt = prompts.build_control_prompt(cfg, job.name, job.prompt)
    events.append(cfg.events_file, "cron_start", job=job.name)
    # TB-126: snapshot the state surface before the cron runs so we can
    # commit ONLY paths the cron actually mutated. Without this, a leftover
    # dirty briefing from a prior op rides along with the next cron commit.
    pre_snapshot = _snapshot_state_paths(cfg)
    timed_out, error, stderr_tail, prompt_dump = await _run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label=f"cron-{job.name}",
        prompt=prompt,
        allowed_tools=job.allowed_tools or CONTROL_AGENT_TOOLS,
        max_turns=job.max_turns,
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "cron_timeout",
            job=job.name,
            timeout_s=cfg.control_timeout_s,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "cron_error",
            job=job.name,
            error=error,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    mark_run(cfg.cron_state_file, job.name)
    events.append(cfg.events_file, "cron_complete", job=job.name)
    # No-op for crons that didn't touch the board (e.g. status-report).
    touched = _changed_state_paths(pre_snapshot, _snapshot_state_paths(cfg))
    if touched:
        _commit_state_files(cfg, f"state: cron {job.name}", paths=touched)


def _extract_text(msg) -> str:
    """Best-effort extraction of an assistant message's final text block."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in reversed(content):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text
    result = getattr(msg, "result", None)
    if isinstance(result, str):
        return result
    return ""


# TB-85: per-envelope debug logging. Walks the SDK message's content blocks to
# surface text + tool_use + tool_result without dumping full payloads (that
# goes to .messages.jsonl). The previous instrumentation only captured
# `_extract_text(msg)[:500]` which returned None for most envelopes since the
# SDK emits many AssistantMessage envelopes that are pure tool_use with no
# text — leaving the stream dump useless for diagnosing the
# "exit code 1 / empty stderr" crash class.

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _extract_tool_result_payload(content) -> dict | None:
    """Parse a ToolResultBlock's content into the dict the daemon's MCP
    tools return via `_ok(...)` (the body of the inner `text` field is a
    JSON object with `message` + structured fields).

    Returns the dict on success, or None when the shape doesn't match (the
    block was an error, the content is a non-JSON string, etc.).
    """
    import json as _json

    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for blk in content:
            t = getattr(blk, "text", None)
            if isinstance(t, str):
                text = t
                break
            if isinstance(blk, dict):
                t = blk.get("text")
                if isinstance(t, str):
                    text = t
                    break
    if not text:
        return None
    try:
        payload = _json.loads(text)
    except _json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _stringify_block_content(c) -> str:
    """Best-effort one-line stringify of a ToolResultBlock.content payload.

    Real shapes seen: a bare string (e.g., a Bash tool's output), or a list of
    sub-blocks (e.g., text + image). We only need a preview, so flatten to a
    string and let callers truncate.
    """
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            t = getattr(b, "text", None)
            parts.append(t if isinstance(t, str) else str(b))
        return " ".join(parts)
    return str(c)


def _walk_blocks(msg, *, full: bool) -> dict:
    """Walk a Message's `.content` blocks. Returns extracted fields ready to
    merge into a dict by `_summarize_message` / `_serialize_message_full`.

    `full=False` truncates text to 200 chars and tool args to 200 chars (for
    .stream.jsonl); `full=True` returns untruncated content (.messages.jsonl).
    """
    import json as _json

    out: dict = {}
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return out

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    blocks_full: list[dict] = []

    for part in content:
        block_full: dict = {"block_type": type(part).__name__}

        text = getattr(part, "text", None)
        if isinstance(text, str):
            if text.strip():
                text_parts.append(text)
            block_full["text"] = text

        # ToolUseBlock: has `name` + `input` + `id`.
        name = getattr(part, "name", None)
        inp = getattr(part, "input", None)
        if name is not None and inp is not None:
            args_json = _json.dumps(inp, default=str)
            tool_calls.append({
                "name": name,
                "args_preview": _truncate(args_json, 200),
            })
            block_full["name"] = name
            block_full["input"] = inp
            tool_id = getattr(part, "id", None)
            if tool_id is not None:
                block_full["id"] = tool_id

        # ToolResultBlock: has `tool_use_id` + `content` (str or list of blocks).
        tu_id = getattr(part, "tool_use_id", None)
        if tu_id is not None:
            tr_content = getattr(part, "content", None)
            preview_str = _stringify_block_content(tr_content)
            is_err = bool(getattr(part, "is_error", False))
            tool_results.append({
                "tool_use_id": tu_id,
                "is_error": is_err,
                "preview": _truncate(preview_str, 200),
            })
            block_full["tool_use_id"] = tu_id
            block_full["is_error"] = is_err
            if tr_content is not None:
                block_full["content"] = preview_str if isinstance(tr_content, str) else _stringify_block_content(tr_content)

        blocks_full.append(block_full)

    if not full:
        if text_parts:
            out["text_preview"] = _truncate(text_parts[-1], 200)
        if tool_calls:
            out["tool_calls"] = tool_calls
        if tool_results:
            out["tool_results"] = tool_results
    else:
        out["content"] = blocks_full

    return out


def _summarize_message(msg) -> dict:
    """Compact per-envelope summary for `.stream.jsonl` (TB-85).

    Returns: `{type, text_preview?, tool_calls?, tool_results?, stop_reason?,
    num_turns?, total_cost_usd?, subtype?, usage?, model_usage?}`. Optional
    fields are omitted when absent so the dump stays scannable. `seq` is
    added by the caller.
    """
    out: dict = {"type": type(msg).__name__}
    out.update(_walk_blocks(msg, full=False))

    # AssistantMessage carries the model string; ResultMessage carries usage /
    # cost / stop_reason at the message level. Capture both so the stream is
    # debuggable end-to-end (which model produced this turn? what stop_reason
    # ended it?).
    for k in ("model", "stop_reason", "num_turns", "total_cost_usd"):
        v = getattr(msg, k, None)
        if v is not None:
            out[k] = v
    sub = getattr(msg, "subtype", None)
    if sub is not None:
        out["subtype"] = sub
    # TB-157: capture token / cache counters from ResultMessage. The `usage`
    # dict shape is well-known (Anthropic API response): input_tokens,
    # output_tokens, cache_creation_input_tokens, cache_read_input_tokens.
    # `model_usage` carries the same fields broken down by model when the
    # session spans multiple variants. Pass through verbatim — downstream
    # aggregators (adhoc/token_breakdown.py, the web detail page) parse the
    # nested dict directly.
    for k in ("usage", "model_usage"):
        v = getattr(msg, k, None)
        if isinstance(v, dict) and v:
            out[k] = v
    # Some ResultMessage variants carry text in `.result` rather than via
    # content blocks.
    if "text_preview" not in out:
        result = getattr(msg, "result", None)
        if isinstance(result, str) and result.strip():
            out["text_preview"] = _truncate(result, 200)
    return out


def _serialize_message_full(msg) -> dict:
    """Full-content per-envelope record for `.messages.jsonl` (TB-85).

    Same shape as `_summarize_message` but without truncation. Cross-reference
    with the stream summary by `seq`.
    """
    out: dict = {"type": type(msg).__name__}
    out.update(_walk_blocks(msg, full=True))
    for k in ("model", "stop_reason", "num_turns", "total_cost_usd", "subtype", "result"):
        v = getattr(msg, k, None)
        if v is not None:
            out[k] = v
    # TB-157: same usage / model_usage capture as the compact summary; the
    # full-record file is the durable archive for cost-tradeoff analysis.
    for k in ("usage", "model_usage"):
        v = getattr(msg, k, None)
        if isinstance(v, dict) and v:
            out[k] = v
    return out


def _append_progress(cfg: Config, task, r: TaskResult) -> None:
    """Append a complete task entry to progress.md as a self-contained section.

    Section header is `## [YYYY-MM-DD] TB-N: Title` so the log stays a coherent
    reverse-time-ordered log and tools can parse sections cleanly.
    """
    cfg.progress_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"\n## [{_today()}] {task.id}: {task.title}"]
    if r.commit:
        lines.append(f"- **Commit:** `{r.commit[:8]}`")
    if r.summary:
        lines.append(f"- **Summary:** {r.summary}")
    if r.files_changed:
        lines.append(f"- **Files:** {', '.join(r.files_changed)}")
    if r.tests_passed is not None:
        lines.append(f"- **Tests:** {'pass' if r.tests_passed else 'fail'}")
    with cfg.progress_file.open("a") as f:
        f.write("\n".join(lines) + "\n")


def _append_attempts(
    cfg: Config,
    task,
    *,
    status: str,
    summary: str = "",
    debug_paths: dict[str, str] | None = None,
    extras: dict[str, str] | None = None,
) -> None:
    """Append a `## Attempts` entry to the task's briefing (TB-114).

    Fires for EVERY failure mode — timeout, error, state_violation,
    verification_failed (project / per-task / pipeline_pending), and
    the agent's own incomplete/blocked/failed statuses. Without this
    trail, a task that hits Frozen via repeated timeouts has no
    narrative the next agent can read.

    `debug_paths` is `{"prompt": <path>, "stream": <path>, "messages":
    <path>}` for failure modes that have a per-run dump. Paths render
    as project-relative bullets so the next agent can `Read` them.

    `extras` is arbitrary key/value lines (e.g. `timeout_s`, `exit_code`,
    `stderr_tail`, `fenced_files`) — bullets under the entry.
    """
    if not task.briefing:
        return
    p = Path(task.briefing)
    full = p if p.is_absolute() else cfg.project_root / p
    if not full.exists():
        return

    lines = [f"\n### {_today()} — {status}"]
    lines.append(summary or "(no summary)")
    if extras:
        for k, v in extras.items():
            v_str = str(v)
            if len(v_str) > 400:
                v_str = v_str[:397] + "…"
            lines.append(f"- **{k}:** {v_str}")
    if debug_paths:
        bullets = []
        for k, dpath in debug_paths.items():
            if not dpath:
                continue
            try:
                rp = Path(dpath).resolve().relative_to(cfg.project_root.resolve())
                shown = str(rp)
            except (ValueError, OSError):
                shown = str(dpath)
            bullets.append(f"`{k}: {shown}`")
        if bullets:
            lines.append("- **Debug dumps:** " + ", ".join(bullets))
    entry = "\n".join(lines) + "\n"

    text = full.read_text()
    header = "\n## Attempts\n"
    if header in text:
        full.write_text(text.rstrip() + entry)
    else:
        full.write_text(text.rstrip() + header + entry)


def _today() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# parse_result returns one of these for a well-formed RESULT block. Anything
# else (most often "unknown") triggers the commit-inference fallback below.
_VALID_RESULT_STATUSES = {"complete", "incomplete", "blocked", "failed"}


def _task_result_from_tool_args(args: dict) -> TaskResult:
    """Build a TaskResult from a `report_result` MCP tool call's args dict.

    Tolerant of two shapes per field, because the @tool decorator's schema
    is a hint not a contract — different agents and SDK versions hand the
    same logical value as either a Python-native value or its string form.
      - `files_changed`: list[str] OR comma-separated string
      - `tests_passed`: bool OR "true"/"false" string

    TB-123: the `cron` field was dropped from `report_result` — cron
    proposals now flow through the `cron_propose` MCP tool, which emits
    its own `cron_proposed` events at call time. The `TaskResult.cron`
    dataclass field is retained as default-empty per the briefing's
    "out of scope" note (deferred deletion in a follow-up).
    """

    def _bool_like(v) -> bool | None:
        if isinstance(v, bool):
            return v
        if v is None:
            return None
        s = str(v).strip().lower()
        if s in ("true", "yes", "1", "pass", "passed"):
            return True
        if s in ("false", "no", "0", "fail", "failed"):
            return False
        return None

    def _list_str(v) -> list[str]:
        if isinstance(v, list):
            return [str(f).strip() for f in v if str(f).strip()]
        if isinstance(v, str):
            return [f.strip() for f in v.split(",") if f.strip()]
        return []

    return TaskResult(
        status=str(args.get("status", "unknown")).strip().lower(),
        commit=str(args.get("commit", "") or "").strip(),
        summary=str(args.get("summary", "") or "").strip(),
        files_changed=_list_str(args.get("files_changed")),
        tests_passed=_bool_like(args.get("tests_passed")),
        raw="(via mcp__autopilot__report_result tool call)",
    )


def _infer_result_from_head(cfg: Config, task) -> "TaskResult | None":
    """Synthesize a TaskResult from HEAD if its commit subject names `task.id`.

    Used as a fallback when the agent's RESULT block is malformed or absent.
    Returns None if not a git repo, log fails, or HEAD's subject doesn't
    mention the task ID — keeping the existing failure path untouched.
    """
    if not (cfg.project_root / ".git").exists():
        return None
    root = str(cfg.project_root)
    log = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%H%x00%s"],
        capture_output=True, text=True,
    )
    if log.returncode != 0:
        return None
    out = log.stdout.strip()
    if "\x00" not in out:
        return None
    sha, subject = out.split("\x00", 1)
    # Tightened (TB-74): the prompt-side convention (TB-65) requires the
    # commit subject to START with `<TB-N>: <description>`. We mirror that
    # here — any subject whose first colon-or-whitespace-delimited token
    # isn't exactly `task.id` is rejected. This avoids the false-positive
    # surfaced live on stoch when a sync commit subject mentioned the same
    # numeric task ID from a different project (`ap2 sync: ... (TB-70) ...`
    # collided with stoch's TB-70).
    import re as _re
    first_token = _re.split(r"[:\s]", subject, maxsplit=1)[0]
    if first_token != task.id:
        return None
    show = subprocess.run(
        ["git", "-C", root, "show", "--name-only", "--format=", sha],
        capture_output=True, text=True,
    )
    files = (
        [l for l in show.stdout.splitlines() if l.strip()]
        if show.returncode == 0 else None
    )
    return TaskResult(
        status="complete",
        commit=sha[:8],
        summary=subject,
        files_changed=files,
        tests_passed=None,
        cron=[],
        raw=f"<inferred from HEAD {sha[:8]}>",
    )


@dataclass
class VerifyResult:
    """Outcome of running the project-wide AP2_VERIFY_CMD against HEAD.

    Returned by `_run_verify` when the gate is configured. `exit_code=None`
    means the command exceeded `AP2_VERIFY_TIMEOUT_S`. stderr/stdout are
    tail-truncated to 2k chars to keep events.jsonl entries bounded.
    """

    passed: bool
    command: str
    exit_code: int | None
    stderr_tail: str
    stdout_tail: str
    duration_s: float


def _run_verify(cfg: Config, task) -> "VerifyResult | None":
    """Execute the project-wide regression gate, returning a result or None.

    Returns None (skip path) when:
      - AP2_VERIFY_CMD is unset or blank — the default; preserves pre-TB-66
        behavior so projects that haven't opted in see no change.
      - The task carries `#no-verify` — operator opt-out for tasks the gate
        can't meaningfully check (docs-only, infra changes the project's
        test command can't see, etc.).

    Otherwise runs `cfg.verify_cmd` via `shell=True` in `cfg.project_root`
    and returns a `VerifyResult`. Note `shell=True` is intentional: the
    command is operator-supplied configuration (not agent-supplied input),
    so shell parsing of forms like `uv run pytest -q` is the desired
    behavior, not an injection risk.
    """
    if not cfg.verify_cmd:
        return None
    if "#no-verify" in (task.tags or []):
        return None
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cfg.verify_cmd,
            shell=True,
            cwd=str(cfg.project_root),
            capture_output=True,
            text=True,
            timeout=cfg.verify_timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        # `e.stderr` and `e.stdout` may be bytes or str depending on Python
        # version + capture path. Normalize to str so callers don't care.
        def _to_str(x) -> str:
            if x is None:
                return ""
            if isinstance(x, bytes):
                return x.decode("utf-8", errors="replace")
            return x

        return VerifyResult(
            passed=False,
            command=cfg.verify_cmd,
            exit_code=None,
            stderr_tail=_to_str(e.stderr)[-2000:],
            stdout_tail=_to_str(e.stdout)[-2000:],
            duration_s=time.monotonic() - t0,
        )
    return VerifyResult(
        passed=proc.returncode == 0,
        command=cfg.verify_cmd,
        exit_code=proc.returncode,
        stderr_tail=proc.stderr[-2000:],
        stdout_tail=proc.stdout[-2000:],
        duration_s=time.monotonic() - t0,
    )


async def _maybe_per_task_verify(cfg: Config, sdk, task) -> "verify.VerifyVerdict | None":
    """Run the per-task verifier (TB-69) for `task` if its briefing has a
    `## Verification` section. Returns None to mean "skip" (legacy task or
    no concrete criteria) — the caller proceeds to move_to_complete unchanged.
    """
    if not task.briefing:
        return None
    p = Path(task.briefing)
    full = p if p.is_absolute() else cfg.project_root / p
    if not full.exists():
        return None
    text = full.read_text()
    return await verify.verify_task(
        briefing_text=text,
        project_root=cfg.project_root,
        timeout_s=cfg.verify_timeout_s,
        sdk=sdk,
        # TB-127: hand the verifier the task id so prose-bullet judging
        # can locate the task's actual implementation commit (subject
        # `<task.id>: ...`) instead of HEAD. On retries of an
        # already-committed task, HEAD is a daemon state-bookkeeping
        # commit; without `task_id` the prose judge sees only that and
        # hallucinates "no changes to file X".
        task_id=task.id,
        # TB-157: thread events_file through so per-judge `judge_call`
        # events land on the canonical aggregation surface. The judge
        # path bypasses the daemon's `_log_message` (its own SDK loop),
        # so this is the only capture point for prose-judge cost.
        events_file=cfg.events_file,
    )


def _prep_debug_dumps(cfg: Config, task_id: str) -> tuple[Path, Path, Path]:
    """Build paths for the per-run prompt + stream + messages dumps (TB-85).

    `.cc-autopilot/debug/` isn't tracked. Files named with UTC timestamp +
    task id so concurrent tasks (if ever allowed) don't clobber each other.
    Failures keep all three files; `run_task` deletes them on successful
    complete.

    Files:
      - <ts>-<task>.prompt.md     full prompt sent to the SDK
      - <ts>-<task>.stream.jsonl  per-envelope summary (preview text + tool
                                  calls + tool results); cheap to scan
      - <ts>-<task>.messages.jsonl  full content per envelope, mirrored by `seq`
    """
    import datetime as dt

    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prompt = debug_dir / f"{ts}-{task_id}.prompt.md"
    stream = debug_dir / f"{ts}-{task_id}.stream.jsonl"
    messages = debug_dir / f"{ts}-{task_id}.messages.jsonl"
    return prompt, stream, messages


# Files the daemon is authoritative for. Committed together per semantic unit
# (a completed task, a cron ideation run, an orphan recovery) so the git log
# tracks board evolution alongside the task agents' source-code commits.
# `ideation_state.md` is the per-cycle progress assessment ideation
# overwrites at the start of every cron run (TB-87) — committing it with
# the rest of the state files keeps the assessment recoverable from git
# history for retrospectives.
_STATE_FILE_NAMES = (
    "TASKS.md",
    ".cc-autopilot/progress.md",
    "CLAUDE.md",
    ".cc-autopilot/ideation_state.md",
    # TB-112: bring three more under daemon auto-commit so the linear
    # rollback design (TB-111) gets cohesion for free.
    #   - cron.yaml: schedule config; mutated via the cron_edit MCP
    #     tool. Previously deferred (TB-83) as YAGNI; relevant now.
    #   - retry_state.json: per-task retry counter. Un-gitignored at
    #     the same time so commits succeed.
    #   - operator_log.md: operator decisions (TB-106). Was committed
    #     ad-hoc; now part of the canonical state-file set.
    # Files that stay gitignored — cron_state.json, mm_state.json,
    # auto_diagnose_state.json, events.jsonl — are ephemeral runtime
    # state. Rollback should NOT re-fire crons / replay MM / re-fire
    # watchdog / replay events; leaving them uncommitted gives that
    # property for free.
    ".cc-autopilot/cron.yaml",
    ".cc-autopilot/retry_state.json",
    ".cc-autopilot/operator_log.md",
)
# Directories whose contents are also daemon-owned audit trail. Staged with
# `git add <dir>` so new briefings (from `add_backlog` auto-fill, ideation
# proposals, or `/tb prep`) and accumulated `## Attempts` edits ride along
# with the state-file commit (TB-73). Briefings get linked from TASKS.md, so
# bundling them keeps reverts/bisects semantically intact.
#
# `.cc-autopilot/insights/` (TB-89) is daemon-owned audit trail too — the
# index file is auto-regenerated by ap2, individual insight files are
# written by tasks/operators. Including the dir keeps git history lined up
# with what ideation actually saw on each cycle.
_STATE_DIRS = (
    ".cc-autopilot/tasks",
    ".cc-autopilot/insights",
)


async def _sweep_pipeline_pending(cfg: Config, sdk) -> None:
    """Walk Pipeline Pending and verify any task whose pipelines all died.

    For each task in `Pipeline Pending` we read events.jsonl backwards to
    find the most recent `task_pipeline_pending` event for that task — its
    `pipelines` field lists the (pid, started_at) tuples we need to check.
    A pipeline is "dead" if `os.kill(pid, 0)` raises ProcessLookupError
    (or if psutil reports the process create_time differs from the
    recorded `started_at`, defending against pid recycling). When every
    pipeline for the task is dead, re-run the verification harness:

      1. Project-wide gate (`_run_verify`) — same as the synchronous path.
      2. Per-task verification (`_maybe_per_task_verify`) — runs the
         briefing's `## Verification` bullets against the post-pipeline
         working tree.

    Pass → move to Complete, append progress, dispatch any cron directives
    captured at launch time. Fail → `_handle_failure(status="verification_failed")`
    routes through Backlog (with retry-counter bump) → Frozen at
    exhaustion. Tick continues; the next dispatch picks the (now-Backlog)
    task back up.
    """
    if not cfg.tasks_file.exists():
        return
    board = Board.load(cfg.tasks_file)
    pending = list(board.iter_tasks("Pipeline Pending"))
    if not pending:
        return

    # Index task_pipeline_pending events by task id (newest wins).
    task_pipelines: dict[str, list[dict]] = {}
    summaries: dict[str, dict] = {}
    for evt in events.tail(cfg.events_file, n=2000):
        if evt.get("type") != "task_pipeline_pending":
            continue
        tid = evt.get("task")
        if not isinstance(tid, str):
            continue
        pls = evt.get("pipelines") or []
        if isinstance(pls, list):
            task_pipelines[tid] = [p for p in pls if isinstance(p, dict)]
            summaries[tid] = {
                "commit": evt.get("commit", "") or "",
                "summary": evt.get("summary", "") or "",
            }

    for task in pending:
        pipelines = task_pipelines.get(task.id) or []
        if not pipelines:
            # Defensive: no record of which pids gate this task. Skip —
            # don't auto-resolve without evidence the dispatcher knew
            # about. Operator can manually move the task off Pipeline
            # Pending if needed.
            continue
        alive = [p for p in pipelines if _pipeline_alive(p)]
        if alive:
            continue
        # All pipelines dead — verify and resolve.
        result_summary = summaries.get(task.id, {})
        final_status = "complete"
        verify_res = _run_verify(cfg, task)
        if verify_res is not None and not verify_res.passed:
            events.append(
                cfg.events_file,
                "verification_failed",
                task=task.id,
                source="pipeline_pending",
                command=verify_res.command,
                exit_code=verify_res.exit_code,
                stderr_tail=verify_res.stderr_tail,
                duration_s=round(verify_res.duration_s, 2),
            )
            _handle_failure(
                cfg, task,
                status="verification_failed",
                extras={
                    "kind": "project_wide",
                    "source": "pipeline_pending",
                    "verify_command": verify_res.command,
                    "exit_code": str(verify_res.exit_code),
                    "stderr_tail": verify_res.stderr_tail[:300],
                },
            )
            final_status = "verification_failed"
        else:
            per_verdict = await _maybe_per_task_verify(cfg, sdk, task)
            if per_verdict is not None and per_verdict.overall == "fail":
                events.append(
                    cfg.events_file,
                    "verification_failed",
                    task=task.id,
                    kind="per_task",
                    source="pipeline_pending",
                    overall=per_verdict.overall,
                    criteria=[
                        {"kind": c.kind, "status": c.status,
                         "bullet": c.bullet[:200], "notes": c.notes[:200]}
                        for c in per_verdict.criteria
                    ],
                    duration_s=round(per_verdict.duration_s, 2),
                )
                _handle_failure(
                    cfg, task,
                    status="verification_failed",
                    extras={
                        "kind": "per_task",
                        "source": "pipeline_pending",
                        "failed_criteria": "; ".join(
                            f"[{c.status}] {c.bullet[:120]}"
                            for c in per_verdict.criteria
                            if c.status == "fail"
                        )[:400] or "(no criteria captured)",
                    },
                )
                final_status = "verification_failed"
            else:
                if per_verdict is not None and per_verdict.overall == "partial":
                    events.append(
                        cfg.events_file,
                        "verification_partial",
                        task=task.id,
                        source="pipeline_pending",
                        criteria=[
                            {"kind": c.kind, "status": c.status,
                             "bullet": c.bullet[:200], "notes": c.notes[:200]}
                            for c in per_verdict.criteria
                        ],
                    )
                do_board_edit(cfg, {
                    "action": "move_to_complete", "task_id": task.id,
                })
                retry.reset_attempt(cfg.retry_state_file, task.id)
                synth = TaskResult(
                    status="complete",
                    commit=result_summary.get("commit", ""),
                    summary=result_summary.get("summary", "") or
                            f"pipelines completed ({len(pipelines)}); verification passed",
                    files_changed=[],
                    tests_passed=None,
                    cron=[],
                    raw="(pipeline_pending → complete)",
                )
                _append_progress(cfg, task, synth)
        events.append(
            cfg.events_file,
            "task_complete",
            task=task.id,
            status=final_status,
            source="pipeline_pending",
            commit=result_summary.get("commit", ""),
            summary=(result_summary.get("summary") or "")[:300],
        )
        board_after = Board.load(cfg.tasks_file)
        loc = board_after.find(task.id)
        dest = loc[0] if loc else "?"
        _commit_state_files(
            cfg, f"state: {task.id} → {dest}",
            paths=_task_state_paths(task),
        )


def _pipeline_alive(pipeline: dict) -> bool:
    """True if the pipeline subprocess identified by (pid, started_at) is
    still running. Defends against pid recycling by comparing
    `psutil.Process(pid).create_time()` to the recorded `started_at` when
    psutil is available; falls back to a bare `os.kill(pid, 0)` check.
    """
    pid = pipeline.get("pid")
    if not isinstance(pid, int):
        return False
    started_at = pipeline.get("started_at")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but isn't ours — treat as alive (we can't kill, so
        # we don't reliably know it's dead either). Won't happen in
        # practice since the daemon spawned it.
        return True
    if isinstance(started_at, (int, float)):
        try:
            import psutil

            ct = int(psutil.Process(pid).create_time())
            if abs(ct - int(started_at)) > 2:
                # PID recycled — the pid we recorded is gone, replaced
                # by an unrelated process.
                return False
        except Exception:  # noqa: BLE001
            pass
    return True


def _commit_state_files(
    cfg: Config,
    message: str,
    *,
    paths: Iterable[str],
) -> None:
    """Stage + commit a narrow allowlist of daemon-owned state files.

    `paths` is a caller-supplied list of repo-relative paths the current
    operation actually touched (TB-126). Only paths inside the daemon-owned
    state set (`_STATE_FILE_NAMES` ∪ `_STATE_DIRS`) AND that exist on disk
    are staged — anything else is dropped defensively. This keeps state
    commits semantically narrow: a `state: TB-N → Backlog` commit no longer
    rides along with an unrelated briefing that happened to be dirty in the
    working tree from a prior operation.

    Silently no-ops when the working tree is clean for the supplied paths
    (e.g. a status-report cron that didn't touch any state file). Failures
    emit `state_commit_error` events but don't raise — a broken commit
    shouldn't wedge the daemon.
    """
    # Silent no-op when the project isn't a git repo — lets tests and non-git
    # experimentation use ap2 without every tick emitting a commit error.
    if not (cfg.project_root / ".git").exists():
        return
    rel_paths = _filter_state_paths(cfg, paths)
    if not rel_paths:
        return
    root = str(cfg.project_root)
    add = subprocess.run(
        ["git", "-C", root, "add", "--"] + rel_paths,
        capture_output=True, text=True,
    )
    if add.returncode != 0:
        events.append(cfg.events_file, "state_commit_error",
                      stage="add", message=message, error=add.stderr[:300])
        return
    diff = subprocess.run(
        ["git", "-C", root, "diff", "--cached", "--quiet", "--"] + rel_paths,
        capture_output=True,
    )
    if diff.returncode == 0:
        return  # nothing staged is actually different from HEAD
    commit = subprocess.run(
        ["git", "-C", root, "commit", "-m", message, "--"] + rel_paths,
        capture_output=True, text=True,
    )
    if commit.returncode != 0:
        events.append(cfg.events_file, "state_commit_error",
                      stage="commit", message=message, error=commit.stderr[:300])


def _filter_state_paths(cfg: Config, paths: Iterable[str]) -> list[str]:
    """Filter caller-supplied paths to existing files inside the state set.

    Defensive: a caller threading the wrong path through (e.g. a source file)
    silently no-ops rather than letting a state commit pull in unrelated
    code. Dedupes while preserving caller order so commit log reads
    naturally. Callers may pass POSIX-style or `Path`-style relative strings.
    """
    allowed_files = set(_STATE_FILE_NAMES)
    allowed_dirs = tuple(_STATE_DIRS)
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        # Normalize: posix slashes, drop a `./` prefix if present. NOT a
        # `lstrip("./")` — that's a charset strip and would eat the leading
        # dot in `.cc-autopilot/...`.
        rel = str(p).replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if rel in seen:
            continue
        # Path must be either an explicit state file or live inside a state dir.
        if rel not in allowed_files and not any(
            rel == d or rel.startswith(d.rstrip("/") + "/")
            for d in allowed_dirs
        ):
            continue
        full = cfg.project_root / rel
        if not full.exists():
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _snapshot_state_paths(cfg: Config) -> dict[str, str]:
    """Hash every state-relevant path's working-tree content.

    Used by control-agent paths (cron tick, ideation) where we don't know
    statically which subset of the state surface the agent will touch.
    Caller compares pre/post snapshots via `_changed_state_paths` and
    threads the delta into `_commit_state_files`.

    Missing files are absent from the dict (so `_changed_state_paths` sees
    "appeared" / "disappeared" as a hash mismatch). Unreadable files map to
    a sentinel so a transient I/O error doesn't crash the snapshot.
    """
    import hashlib

    out: dict[str, str] = {}

    def _hash(p: Path) -> str | None:
        if not p.is_file():
            return None
        try:
            return hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            return "unreadable"

    for name in _STATE_FILE_NAMES:
        h = _hash(cfg.project_root / name)
        if h is not None:
            out[name] = h
    for d in _STATE_DIRS:
        dpath = cfg.project_root / d
        if not dpath.is_dir():
            continue
        for f in dpath.rglob("*"):
            if not f.is_file():
                continue
            try:
                rel = str(f.relative_to(cfg.project_root))
            except ValueError:
                continue
            h = _hash(f)
            if h is not None:
                out[rel.replace("\\", "/")] = h
    return out


def _changed_state_paths(
    before: dict[str, str], after: dict[str, str]
) -> list[str]:
    """Return state-relevant paths whose hash differs between snapshots.

    Includes paths that appeared (new file) or disappeared (deletion). Sort
    keeps commit-log diffs deterministic.
    """
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def _task_state_paths(task) -> list[str]:
    """Repo-relative state paths a task-completion (or failure) operation
    can dirty. Used by `run_task` and the pipeline-pending sweep.

    - `TASKS.md`: every board move.
    - `progress.md`: `_append_progress` on Complete.
    - `retry_state.json`: `bump_attempt` / `reset_attempt`.
    - The task's briefing: `_append_attempts` on every failure mode.

    Files that exist but weren't actually modified are filtered downstream
    by `git diff --cached --quiet`, so passing a fixed superset is safe.
    """
    paths = [
        "TASKS.md",
        ".cc-autopilot/progress.md",
        ".cc-autopilot/retry_state.json",
    ]
    if task.briefing:
        paths.append(str(task.briefing).replace("\\", "/"))
    return paths


def _emit_daemon_start(cfg: Config) -> dict:
    """Append the `daemon_start` event with the current source version (TB-139).

    Stamps the running source revision so a post-mortem can correlate
    state-file mutations with the exact commit the daemon was loading.
    Editable installs (the common case here) get `<base>+<sha>.<ts>`;
    released wheels get just the base version.

    Extracted so the daemon's startup-event shape is unit-testable without
    spinning up the full `main_loop` (which is async + needs the SDK).
    """
    from . import get_version

    return events.append(
        cfg.events_file, "daemon_start",
        pid=os.getpid(), version=get_version(),
    )


async def main_loop(cfg: Config) -> None:
    """Start the daemon: bootstrap, then run the two concurrent loops until SIGTERM.

    TB-122: splits the original single `while RUNNING: _tick()` loop into two
    concurrent asyncio coroutines:
    - `_main_tick_loop` — cron, pipeline-pending sweep, task dispatch,
      ideation, watchdog. Tick interval `AP2_TICK_S` (30s default).
    - `_mm_loop` — Mattermost polling only, on a faster `AP2_MM_TICK_S`
      (10s default). Each new mention spawns an `asyncio.create_task` so
      back-to-back mentions don't serialize, and so handler agents don't
      block subsequent polls.

    Both loops share the same `Config`, SDK handle, and MCP server. Board
    mutations go through `locked_board()` (fcntl.flock), which already
    serializes concurrent access. The pause flag is respected in both loops.

    `asyncio.gather(...)` runs both loops concurrently; if either coroutine
    raises (which neither should — every per-tick error is caught inside),
    the surrounding `try/finally` still emits `daemon_stop` and cleans up
    the pid file.
    """
    cfg.ensure_dirs()
    if bootstrap_cron(cfg.cron_file):
        events.append(cfg.events_file, "cron_bootstrap", path=str(cfg.cron_file))
    _recover_orphans(cfg)
    _import_sdk_or_die()
    import claude_agent_sdk as sdk  # type: ignore

    mcp_server = build_mcp_server(cfg)
    # TB-144: hand the MCP tool surface a reference to the daemon's SDK
    # + MCP server so `mcp__autopilot__status_report_run` can dispatch a
    # status-report sub-agent. The reference is process-wide and lives
    # for the daemon's lifetime — module-level dict (not contextvar)
    # because the value is a long-lived singleton, not per-task plumbing.
    _status_report_mod.configure(sdk, mcp_server)
    _emit_daemon_start(cfg)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text(str(os.getpid()))

    # Track outstanding MM handler tasks so we can drain them on shutdown.
    # `_mm_loop` adds; the set's discard-on-done keeps it bounded.
    handler_tasks: set[asyncio.Task] = set()

    # TB-130: the read-only web UI is now part of the daemon lifecycle.
    # Spawned as an asyncio task on the same loop so its lifetime is
    # bounded by `main_loop` — no orphaned `ap2 web` process pointing at
    # a stale events.jsonl after the daemon dies. `AP2_WEB_DISABLED=1`
    # opts out (CI / headless). The bind itself happens inside
    # `_web_loop_for_daemon`, with bind errors logged as `web_error` and
    # otherwise swallowed so a port collision can't take the daemon down.
    web_task: asyncio.Task | None = None
    if not web.is_web_disabled():
        web_task = asyncio.create_task(_web_loop_for_daemon(cfg))

    try:
        await asyncio.gather(
            _main_tick_loop(cfg, sdk, mcp_server),
            _mm_loop(cfg, sdk, mcp_server, handler_tasks),
        )
    finally:
        # Best-effort drain of in-flight handlers. They have their own
        # `cfg.control_timeout_s` cap so this is bounded even if a handler
        # is mid-SDK-query.
        if handler_tasks:
            for t in handler_tasks:
                t.cancel()
            await asyncio.gather(*handler_tasks, return_exceptions=True)
        # Cancel and drain the web task. `serve_async` traps the cancel,
        # shuts down the HTTP server cleanly, and emits a `web_stop` from
        # `_web_loop_for_daemon` for symmetry with `web_start`.
        if web_task is not None:
            web_task.cancel()
            await asyncio.gather(web_task, return_exceptions=True)
        events.append(cfg.events_file, "daemon_stop")
        try:
            cfg.pid_file.unlink()
        except OSError:
            pass


async def _web_loop_for_daemon(cfg: Config) -> None:
    """Wrapper around `web.serve_async` for the daemon lifecycle (TB-130).

    Resolves host/port, emits `web_start` / `web_stop` lifecycle events
    around the run, and translates a port-bind `OSError` into a
    `web_error` event so a clash (e.g. an `ap2 web` already listening on
    the port) can't take the rest of the daemon down. The web UI is a
    convenience — its absence shouldn't impact task dispatch.

    TB-155: when the configured `start_port` is already bound (typically a
    stale daemon, an `ap2 web` standalone, or another project's daemon),
    `web.serve_async` walks forward up to `DEFAULT_WEB_PORT_MAX_ATTEMPTS`
    ports before giving up. The `web_start` event records the actually-
    bound port; `requested_port` is added when it differs from the bound
    one so post-mortem can spot the silent enumeration.
    """
    host = "127.0.0.1"
    start_port = web.daemon_web_port()
    # Captured by `_on_bind` so the `web_stop` event in the finally block
    # reflects the actual bound port even after auto-enumeration. If the
    # bind itself fails (range exhausted), these stay at the requested
    # values — symmetric with the pre-TB-155 behavior.
    bound_port = start_port
    bound_host = host
    try:
        def _on_bind(h: str, p: int) -> None:
            nonlocal bound_host, bound_port
            bound_host, bound_port = h, p
            payload = {
                "host": h,
                "port": p,
                "url": f"http://{h}:{p}/",
            }
            if p != start_port:
                # Surface the silent enumeration: operators grepping for
                # `requested_port` see exactly which conflicts the daemon
                # papered over.
                payload["requested_port"] = start_port
            events.append(cfg.events_file, "web_start", **payload)

        await web.serve_async(
            cfg,
            host=host,
            start_port=start_port,
            max_attempts=web.DEFAULT_WEB_PORT_MAX_ATTEMPTS,
            on_bind=_on_bind,
        )
    except asyncio.CancelledError:
        # Normal shutdown path — main_loop's finally cancelled us.
        raise
    except OSError as e:
        events.append(
            cfg.events_file, "web_error",
            host=host, port=start_port,
            max_attempts=web.DEFAULT_WEB_PORT_MAX_ATTEMPTS,
            error=f"{type(e).__name__}: {e}",
        )
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file, "web_error",
            host=host, port=start_port,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        events.append(
            cfg.events_file, "web_stop", host=bound_host, port=bound_port,
        )


async def _main_tick_loop(cfg: Config, sdk, mcp_server) -> None:
    """Main tick loop: cron, pipeline sweep, task dispatch, ideation, watchdog.

    TB-122: Mattermost polling was moved to `_mm_loop` so it doesn't block
    on long-running `run_task` calls. This loop now handles only scheduled
    work — every step inside `_tick` already had its own try/except, so a
    failure in any one step doesn't break the loop.
    """
    while RUNNING:
        if cfg.pause_flag.exists():
            await _interruptible_sleep(cfg.tick_interval_s)
            continue
        await _tick(cfg, sdk, mcp_server)
        # Short 1s-granularity sleep so SIGTERM is noticed promptly.
        await _interruptible_sleep(cfg.tick_interval_s)


async def _mm_loop(
    cfg: Config,
    sdk,
    mcp_server,
    handler_tasks: set[asyncio.Task] | None = None,
) -> None:
    """Mattermost polling loop — runs independently of the main tick (TB-122).

    Polls `check_new_messages` on `AP2_MM_TICK_S` (default 10s). For each
    new mention, spawns an `asyncio.create_task(handle_message(...))` so:
      - the next poll is not blocked on a slow handler agent
      - back-to-back mentions don't serialize (each gets its own task)
      - the main tick loop is never blocked by MM work

    Every handler runs with the same fixed `MM_HANDLER_TOOLS` toolset
    (TB-145, replacing TB-122's board-state-conditional toggle).

    The pause flag suppresses polling (same semantics as today's tick: the
    operator can still send messages while paused, but they won't be seen
    until the daemon resumes).

    `handler_tasks` is the daemon-level set used by `main_loop` to drain
    in-flight handlers on shutdown. Optional so unit tests can drive
    `_mm_loop` directly without owning the bookkeeping.
    """
    while RUNNING:
        if not cfg.pause_flag.exists():
            try:
                for msg in check_new_messages(cfg):
                    task = asyncio.create_task(
                        handle_message(cfg, sdk, mcp_server, msg)
                    )
                    if handler_tasks is not None:
                        handler_tasks.add(task)
                        task.add_done_callback(handler_tasks.discard)
            except Exception as e:  # noqa: BLE001
                events.append(
                    cfg.events_file, "mm_poll_error",
                    error=f"{type(e).__name__}: {e}",
                )
        await _interruptible_sleep(cfg.mm_tick_interval_s)


async def _interruptible_sleep(total_s: int) -> None:
    """Sleep up to `total_s` seconds, breaking promptly on SIGTERM/SIGINT.

    Used by both `_main_tick_loop` and `_mm_loop` so a shutdown signal
    doesn't stall behind the longer tick interval.
    """
    slept = 0
    while slept < total_s and RUNNING:
        await asyncio.sleep(1)
        slept += 1


async def _tick(cfg: Config, sdk, mcp_server) -> None:
    # 0. Drain the operator queue (TB-131). Runs BEFORE every other
    # stage so cron / pipeline-pending sweep / task dispatch / ideation
    # all read an up-to-date board snapshot. Operator-side `ap2 add`,
    # `ap2 backlog`, `ap2 unfreeze`, `ap2 delete` (and the MM-handler
    # `operator_queue_append` MCP tool) append jsonl records here
    # instead of mutating TASKS.md directly; the drain replays them
    # under board_file_lock and commits the resulting state files in a
    # single coherent commit. Idempotent via per-record uuid +
    # operator_queue_state.json so a crash mid-drain resumes without
    # double-applying.
    try:
        drain_res = tools.drain_operator_queue(cfg)
        if drain_res.get("applied"):
            _commit_state_files(
                cfg,
                f"state: drained {drain_res['applied']} operator op(s)",
                paths=drain_res.get("touched_paths") or [],
            )
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "operator_queue_drain_error",
            error=f"{type(e).__name__}: {e}",
        )

    # 1. Cron (MM polling moved to _mm_loop — TB-122)
    try:
        jobs = load_jobs(cfg.cron_file)
        state = load_state(cfg.cron_state_file)
        for job in due_jobs(jobs, state, cfg.project_root):
            await run_cron(cfg, sdk, mcp_server, job)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "cron_error", error=f"{type(e).__name__}: {e}")

    # 2. Pipeline Pending sweep (TB-114). Each task in `Pipeline Pending`
    # has one or more pids it dispatched via `pipeline_task_start`. When
    # ALL of those pids are dead, re-run verification against the now-
    # populated working tree and route to Complete or Backlog/Frozen.
    try:
        await _sweep_pipeline_pending(cfg, sdk)
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file, "pipeline_pending_sweep_error",
            error=f"{type(e).__name__}: {e}",
        )

    # 3. Next Ready task (auto-promote top-of-Backlog if Ready is empty)
    try:
        board = Board.load(cfg.tasks_file)
        # Surface task-shaped lines that fail the parser. A common cause is a
        # manual edit inserting `(<sha>)` between **TB-N** and **Title**, which
        # makes the task invisible to `completed_ids()` and silently blocks
        # every Backlog task that depends on it.
        for section, line in board.malformed_lines:
            sig = f"{section}:{line}"
            if sig in _SEEN_MALFORMED:
                continue
            _SEEN_MALFORMED.add(sig)
            events.append(
                cfg.events_file,
                "board_malformed_line",
                section=section,
                line=line[:240],
            )
        task = board.next_ready()
        if task is None:
            # next_dispatchable skips any Backlog task with unmet `blocked on:`
            # references — backward-compatible: tasks with no declared blockers
            # are always dispatchable.
            backlog = board.next_dispatchable("Backlog")
            if backlog is not None:
                do_board_edit(cfg, {"action": "move_to_ready", "task_id": backlog.id})
                events.append(
                    cfg.events_file,
                    "backlog_auto_promoted",
                    task=backlog.id,
                    title=backlog.title,
                )
                board = Board.load(cfg.tasks_file)
                task = board.next_ready()
        if task:
            await run_task(cfg, sdk, mcp_server, task)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "task_error", error=f"{type(e).__name__}: {e}")

    # 4. Ideation: fire when the working board is fully empty, throttled by
    # AP2_IDEATION_COOLDOWN_S (default 3600). Owned by `ap2.ideation`, not
    # cron.yaml — see that module for the prompt + override mechanism.
    try:
        await ideation._maybe_ideate(cfg, sdk, mcp_server)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "ideation_error", error=f"{type(e).__name__}: {e}")

    # 5. Idle watchdog (TB-71) — when nothing meaningful has happened for
    # AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S (default 3h), build a self-diagnose
    # report and post it to AP2_MM_CHANNELS[0]. Throttled by
    # AP2_AUTO_DIAGNOSE_COOLDOWN_S (default 6h) to avoid spamming when idle
    # persists. Skips silently when MM env is unset (after one warning).
    try:
        _maybe_auto_diagnose(cfg)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "auto_diagnose_error",
                      error=f"{type(e).__name__}: {e}")




def _maybe_auto_diagnose(cfg: Config, *, now: float | None = None) -> None:
    """Idle-watchdog hook (TB-71). See `_tick` step 5 for context.

    Inspects events.jsonl for the most recent meaningful event. If the gap
    exceeds `cfg.auto_diagnose_idle_threshold_s` AND we haven't fired within
    `cfg.auto_diagnose_cooldown_s`, post `diagnose.render_markdown` to
    `AP2_MM_CHANNELS[0]` via `tools._mm_post`. Updates persistent state in
    `cfg.auto_diagnose_state_file`.

    `now` parameter exists so tests can drive a fake clock; production uses
    `time.time()`.
    """
    if now is None:
        now = time.time()

    state = _load_diagnose_state(cfg)
    report = diagnose.build_report(cfg, now=now)

    # No meaningful events yet (fresh daemon) → can't be idle. Skip.
    if report.since_last_activity_s is None:
        return

    if report.since_last_activity_s < cfg.auto_diagnose_idle_threshold_s:
        return

    if now - state.get("last_fired", 0.0) < cfg.auto_diagnose_cooldown_s:
        return

    # TB-121: when every Backlog task is review-gated and nothing else is
    # in flight, the daemon is correctly idle — operator approval is the
    # only thing that can move work forward, so a "daemon idle, here's
    # the diagnose dump" alert misdescribes the state. Post a softer
    # one-liner reminder instead and reuse the diagnose cooldown so the
    # operator isn't spammed.
    if diagnose.is_wholly_pending_review(report):
        channel = _first_mm_channel()
        pending = report.board_health.get("pending_review") or []
        ids_str = ", ".join(pending[:10])
        reminder = (
            f"**ap2 pending review** — `{cfg.project_root.name}` has "
            f"{len(pending)} ideation proposal"
            f"{'s' if len(pending) != 1 else ''} awaiting operator "
            f"approval ({ids_str}). Run `ap2 approve TB-N` to dispatch, "
            f"or `ap2 delete TB-N --force` to discard."
        )
        post_id: str | None = None
        if channel:
            try:
                post_id = tools._mm_post(channel, reminder)
            except Exception as e:  # noqa: BLE001
                events.append(
                    cfg.events_file,
                    "auto_diagnose_post_error",
                    channel=channel,
                    error=f"{type(e).__name__}: {e}",
                )
                return
        events.append(
            cfg.events_file,
            "pending_review_reminder",
            channel=channel,
            post_id=post_id,
            pending=pending,
            idle_s=report.since_last_activity_s,
        )
        state["last_fired"] = now
        state["warned_no_destination"] = False
        _save_diagnose_state(cfg, state)
        return

    channel = _first_mm_channel()
    if not channel:
        # Without a destination there's nowhere to post. Warn ONCE per run
        # of "AP2_MM_CHANNELS is unset"; the flag is sticky in state so we
        # don't fill events.jsonl with the same line every tick.
        if not state.get("warned_no_destination"):
            events.append(
                cfg.events_file,
                "auto_diagnose_no_destination",
                reason="AP2_MM_CHANNELS unset",
                idle_s=report.since_last_activity_s,
            )
            state["warned_no_destination"] = True
            _save_diagnose_state(cfg, state)
        return

    text = diagnose.render_markdown(report)
    try:
        post_id = tools._mm_post(channel, text)
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "auto_diagnose_post_error",
            channel=channel,
            error=f"{type(e).__name__}: {e}",
        )
        return

    events.append(
        cfg.events_file,
        "auto_diagnose_fired",
        channel=channel,
        post_id=post_id,
        idle_s=report.since_last_activity_s,
        report_summary=text[:500],
    )
    state["last_fired"] = now
    state["warned_no_destination"] = False  # reset — destination is back
    _save_diagnose_state(cfg, state)


def _first_mm_channel() -> str:
    """Return the first channel id from `AP2_MM_CHANNELS`, or empty string.

    Mirrors `mattermost._channels_to_watch` parsing so the watchdog and the
    inbound poller agree on which env var defines "the project's channel(s)".
    """
    raw = os.environ.get("AP2_MM_CHANNELS", "").strip()
    for c in raw.split(","):
        c = c.strip()
        if c:
            return c
    return ""


def _load_diagnose_state(cfg: Config) -> dict:
    if not cfg.auto_diagnose_state_file.exists():
        return {}
    try:
        data = json.loads(cfg.auto_diagnose_state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_diagnose_state(cfg: Config, state: dict) -> None:
    cfg.auto_diagnose_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.auto_diagnose_state_file.write_text(json.dumps(state, indent=2, sort_keys=True))


def _import_sdk_or_die() -> None:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print(
            "Error: claude-agent-sdk not installed. "
            "Install with: uv pip install claude-agent-sdk",
            file=sys.stderr,
        )
        sys.exit(1)


def run(project_root: str | None = None) -> None:
    cfg = Config.load(project_root)
    asyncio.run(main_loop(cfg))


if __name__ == "__main__":
    run()
