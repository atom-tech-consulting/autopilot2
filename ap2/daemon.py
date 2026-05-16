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

from . import diagnose, events, goal, ideation, prompts, retry, rollback, tools, verify, web
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
    # TB-165: wall-clock baseline so the per-run `task_run_usage` event can
    # report `duration_s` regardless of which terminal path the run takes.
    run_t0 = time.monotonic()
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
    # TB-165: stable per-run identifier shared with the debug-dump filenames
    # (`<compact_ts>-<task_id>`). Lets `task_run_usage` events grep-link to
    # `.cc-autopilot/debug/<run_id>.*` artifacts.
    run_id = prompt_dump.name.removesuffix(".prompt.md")
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
        _emit_task_run_usage(
            cfg, task,
            run_id=run_id,
            status="state_violation",
            duration_s=time.monotonic() - run_t0,
            stream_log=stream_log,
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
        _emit_task_run_usage(
            cfg, task,
            run_id=run_id,
            status=early_failure_status,
            duration_s=time.monotonic() - run_t0,
            stream_log=stream_log,
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
        _emit_task_run_usage(
            cfg, task,
            run_id=run_id,
            status="pipeline_pending",
            duration_s=time.monotonic() - run_t0,
            stream_log=stream_log,
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
                # TB-165: per-run debug dumps (prompt.md / stream.jsonl /
                # messages.jsonl) are now retained on success too, so
                # cross-run cost / cache analysis (`adhoc/token_breakdown.py`,
                # `/task-run/<run-id>` web detail) covers clean runs. Pre-
                # TB-165 the success branch unlinked all three; the
                # `task_run_usage` event below additionally persists run-
                # level totals to events.jsonl for cheap aggregation.
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
    _emit_task_run_usage(
        cfg, task,
        run_id=run_id,
        status=final_status,
        duration_s=time.monotonic() - run_t0,
        stream_log=stream_log,
    )
    events.append(
        cfg.events_file,
        "task_complete",
        task=task.id,
        status=final_status,
        commit=commit_hash,
        summary=parsed.summary[:300],
    )
    # TB-188: terminal-event reconciliation for the per-proposal record
    # (no-op when no record exists for this TB-N — legacy proposals from
    # before TB-188 landed, or operator-driven adds without the `review`
    # marker). Only the two terminal-from-the-proposal's-perspective
    # statuses reconcile: `complete` (the task shipped) and
    # `verification_failed` (the verifier rejected the agent's diff).
    # `incomplete` / `blocked` / `failed` / timeout / state_violation
    # all route the task back to Backlog with retries remaining — the
    # proposal is still alive, so leave the record's `outcome` unset
    # until a truly-terminal event fires.
    if final_status == "complete":
        tools.reconcile_proposal_outcome(
            cfg, task.id,
            decision_kind="completed",
            decision_actor="daemon",
            commit=(commit_hash or "")[:8] or None,
        )
    elif final_status == "verification_failed":
        tools.reconcile_proposal_outcome(
            cfg, task.id,
            decision_kind="verification_failed",
            decision_actor="verifier",
            commit=(commit_hash or "")[:8] or None,
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

    # TB-166: route MM handler runs through the shared control-agent helper
    # so the SDK stream gets dumped to disk (`stream.jsonl` + `messages.jsonl`)
    # and a `control_run_usage` event lands per fire — same instrumentation
    # ideation and status-report now get. Label encodes the triggering
    # post id so `adhoc/token_breakdown.py`'s `classify_label` (`MM-<post-id>`
    # → `mm-handler`) keeps grouping these runs correctly. Existing
    # `mattermost_timeout` / `mattermost_error` events still fire from
    # this caller — the new event is purely additive.
    post_id = (msg.get("id") or "").strip() or "unknown"
    timed_out, error, _stderr_tail, _prompt_dump = await _run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label=f"MM-{post_id}",
        prompt=prompt,
        allowed_tools=MM_HANDLER_TOOLS,
        max_turns=int(os.environ.get("AP2_CONTROL_MAX_TURNS", 15)),
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "mattermost_timeout",
            timeout_s=cfg.control_timeout_s,
            thread_id=msg.get("thread_id"),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "mattermost_error",
            error=error,
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
    """SDK plumbing for control-agent runs (cron jobs, ideation, MM handler).

    Returns ``(timed_out, error, stderr_tail, prompt_dump_path)``. On
    success: ``(False, None, "", path)``. On timeout: ``(True, None,
    tail, path)``. On any other exception: ``(False, "<Type>: <msg>",
    tail, path)``. The caller owns the surrounding event vocabulary
    (``ideation_timeout`` / ``cron_error`` / ``mattermost_timeout`` /
    etc.), cooldown bookkeeping, and state commit.

    TB-156: ``effort`` lets a caller override the reasoning-effort budget
    for this specific invocation. When ``None`` (the default) we fall back
    to the global ``AP2_AGENT_EFFORT`` env (default ``xhigh``) so existing
    callers keep their pre-TB-156 behavior. Per-call-site lowering (e.g.
    status-report) is opt-in: the caller computes its own effort using
    its own per-site env knob and passes it explicitly.

    TB-166: every envelope from the SDK stream is now captured to
    ``<run_id>.stream.jsonl`` + ``<run_id>.messages.jsonl`` (parity with
    ``run_task``), and a ``control_run_usage`` event is emitted on every
    terminal path (success / timeout / error). Pre-TB-166 only the prompt
    was dumped and the stream was discarded via
    ``async for _ in sdk.query(...): pass`` — per-message detail and
    token cost were unrecoverable for ideation, status-report, and MM.
    The label-specific events (``ideation_timeout`` / ``cron_error`` /
    ``mattermost_error`` / etc.) keep firing from the caller; the new
    event is purely additive so ``events.jsonl`` greps for the existing
    vocabulary stay valid.
    """
    run_t0 = time.monotonic()
    prompt_dump, stream_dump, messages_dump = _prep_debug_dumps(cfg, label)
    # TB-166: stable per-run identifier shared with the debug-dump filenames
    # (`<compact_ts>-<label>`). Lets `control_run_usage` events grep-link to
    # `.cc-autopilot/debug/<run_id>.*` artifacts.
    run_id = prompt_dump.name.removesuffix(".prompt.md")
    prompt_dump.write_text(prompt)
    # TB-166: touch the stream + messages files up front so they exist on
    # disk even when the SDK errors / times out before yielding a single
    # envelope. Without this, an operator who greps a `control_run_usage`
    # event for `<run_id>` and then `ls .cc-autopilot/debug/<run_id>.*`
    # would see only the prompt — confusing for forensic inspection.
    stream_dump.touch()
    messages_dump.touch()
    stderr_lines, stderr_sink = _make_stderr_sink()

    resolved_effort = (
        effort if effort is not None
        else os.environ.get("AP2_AGENT_EFFORT", "xhigh")
    )

    # TB-166: same two-layer message log as `run_task`. `.stream.jsonl`
    # holds compact per-envelope summaries (preview + tool calls + tool
    # results); `.messages.jsonl` mirrors the same `seq` ordering with
    # FULL content. Diagnose by scanning the stream, then `jq
    # 'select(.seq==N)'` on messages.jsonl for the full body.
    stream_log: list[dict] = []
    seq = [0]

    def _log_message(msg) -> None:
        idx = seq[0]
        seq[0] += 1
        summary = {"seq": idx, **_summarize_message(msg)}
        stream_log.append(summary)
        if len(stream_log) > 200:
            del stream_log[: len(stream_log) - 200]
        with stream_dump.open("a") as f:
            f.write(json.dumps(summary, default=str) + "\n")
        with messages_dump.open("a") as f:
            full = {"seq": idx, **_serialize_message_full(msg)}
            f.write(json.dumps(full, default=str) + "\n")

    async def _consume() -> None:
        async for msg in sdk.query(
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
            _log_message(msg)

    timed_out = False
    error: str | None = None
    try:
        await asyncio.wait_for(_consume(), timeout=cfg.control_timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    stderr_tail = "\n".join(stderr_lines[-30:])
    if timed_out:
        status = "timeout"
    elif error is not None:
        status = "error"
    else:
        status = "complete"

    _emit_control_run_usage(
        cfg,
        label=label,
        run_id=run_id,
        status=status,
        duration_s=time.monotonic() - run_t0,
        stream_log=stream_log,
        error=error,
        stderr_tail=stderr_tail if (timed_out or error is not None) else "",
    )

    if timed_out:
        return True, None, stderr_tail, prompt_dump
    if error is not None:
        return False, error, stderr_tail, prompt_dump
    # Success: preserve the pre-TB-166 contract of returning "" for stderr_tail.
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

    # TB-177 + TB-178: `janitor` jobs run a deterministic git-state
    # detection pass (subprocess + `git status --porcelain` parsing),
    # then layer an LLM judge over the candidate findings to classify
    # each as real_strand / operator_draft / ambiguous. The judge step
    # makes async SDK calls but stays out of `_run_control_agent`'s
    # control-prompt plumbing — janitor's prompt is purpose-built per
    # finding (see `janitor._judge_finding`). We still bookend with
    # `cron_start`/`cron_complete` so post-mortems can trace the run
    # through the same event vocabulary as every other cron job.
    # `job.prompt` is intentionally ignored — the work is hard-coded.
    if job.name == "janitor":
        from . import janitor as _janitor

        events.append(cfg.events_file, "cron_start", job=job.name)
        try:
            await _janitor.run_janitor(cfg, sdk)
        except Exception as e:  # noqa: BLE001
            events.append(
                cfg.events_file,
                "cron_error",
                job=job.name,
                error=f"{type(e).__name__}: {e}",
            )
        mark_run(cfg.cron_state_file, job.name)
        events.append(cfg.events_file, "cron_complete", job=job.name)
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


def _emit_task_run_usage(
    cfg: Config,
    task,
    *,
    run_id: str,
    status: str,
    duration_s: float,
    stream_log: list[dict],
) -> None:
    """TB-165: emit a `task_run_usage` event capturing run-level totals.

    Persists token / cache / cost / turn counts to events.jsonl on every
    terminal path (success, verification failure, pipeline pending, state
    violation, timeout, SDK crash) so cross-run aggregation surfaces
    (`adhoc/token_breakdown.py`, ad-hoc `jq` queries) survive regardless
    of debug-dump retention. Pre-TB-165 only `judge_call` events landed
    in events.jsonl; task-agent token cost was recoverable only from the
    `.stream.jsonl` archive — and successful runs had theirs deleted.

    Source of usage data: the trailing `ResultMessage` envelope already
    captured by `_summarize_message` and recorded in `stream_log`. We
    walk the in-memory list backwards to find the LAST entry carrying
    `usage` / `total_cost_usd` (defensive against the unlikely case of
    multiple ResultMessages in a single run). If no ResultMessage was
    captured (SDK error before stream end), the event still fires with
    empty usage and `note=stream_incomplete` — so cross-run aggregators
    don't silently drop the run.

    `run_id` matches the `<compact_ts>-<task_id>` filename prefix of the
    debug dumps, so an operator can `ls .cc-autopilot/debug/<run_id>.*`
    after grepping for the event.
    """
    last_result: dict | None = None
    for s in reversed(stream_log):
        if "usage" in s or "total_cost_usd" in s:
            last_result = s
            break
    payload: dict = {
        "task": task.id,
        "run_id": run_id,
        "status": status,
        "duration_s": round(duration_s, 3),
    }
    if last_result is None:
        payload["usage"] = {}
        payload["model_usage"] = {}
        payload["total_cost_usd"] = 0.0
        payload["num_turns"] = 0
        payload["model"] = ""
        payload["note"] = "stream_incomplete"
    else:
        payload["usage"] = last_result.get("usage") or {}
        payload["model_usage"] = last_result.get("model_usage") or {}
        payload["total_cost_usd"] = last_result.get("total_cost_usd") or 0.0
        payload["num_turns"] = last_result.get("num_turns") or 0
        payload["model"] = last_result.get("model") or ""
    events.append(cfg.events_file, "task_run_usage", **payload)


def _emit_control_run_usage(
    cfg: Config,
    *,
    label: str,
    run_id: str,
    status: str,
    duration_s: float,
    stream_log: list[dict],
    error: str | None = None,
    stderr_tail: str = "",
) -> None:
    """TB-166: emit a `control_run_usage` event capturing run-level
    totals for non-task SDK runs (ideation, cron jobs, MM handler).

    Parallel to TB-165's `_emit_task_run_usage` but with a `label`
    field instead of `task` (control runs aren't bound to a board task)
    and optional `error` / `stderr_tail` fields for non-success paths.
    Persists token / cache / cost / turn counts to events.jsonl on every
    terminal path so cross-run aggregation surfaces (`adhoc/token_breakdown.py`,
    ad-hoc `jq` queries) survive regardless of debug-dump retention.

    Source of usage data: the trailing `ResultMessage` envelope already
    captured by `_summarize_message` and recorded in `stream_log`. Walks
    the in-memory list backwards to find the LAST entry carrying `usage`
    / `total_cost_usd` (defensive against the unlikely case of multiple
    ResultMessages in a single run). If no ResultMessage was captured
    (SDK error / timeout before stream end), the event still fires with
    empty usage and `note=stream_incomplete` — same pattern TB-165 used
    for crash paths so cross-run aggregators don't silently drop the run.

    `run_id` matches the `<compact_ts>-<label>` filename prefix of the
    debug dumps, so an operator can `ls .cc-autopilot/debug/<run_id>.*`
    after grepping for the event.
    """
    last_result: dict | None = None
    for s in reversed(stream_log):
        if "usage" in s or "total_cost_usd" in s:
            last_result = s
            break
    payload: dict = {
        "label": label,
        "run_id": run_id,
        "status": status,
        "duration_s": round(duration_s, 3),
    }
    if last_result is None:
        payload["usage"] = {}
        payload["model_usage"] = {}
        payload["total_cost_usd"] = 0.0
        payload["num_turns"] = 0
        payload["model"] = ""
        payload["note"] = "stream_incomplete"
    else:
        payload["usage"] = last_result.get("usage") or {}
        payload["model_usage"] = last_result.get("model_usage") or {}
        payload["total_cost_usd"] = last_result.get("total_cost_usd") or 0.0
        payload["num_turns"] = last_result.get("num_turns") or 0
        payload["model"] = last_result.get("model") or ""
    if error is not None:
        payload["error"] = error
    if stderr_tail:
        payload["stderr_tail"] = stderr_tail
    events.append(cfg.events_file, "control_run_usage", **payload)


def _prep_debug_dumps(cfg: Config, task_id: str) -> tuple[Path, Path, Path]:
    """Build paths for the per-run prompt + stream + messages dumps (TB-85).

    `.cc-autopilot/debug/` isn't tracked. Files named with UTC timestamp +
    task id so concurrent tasks (if ever allowed) don't clobber each other.
    All three files survive both successful and failed runs (TB-165 — the
    pre-TB-165 success path deleted them via `run_task`'s `unlink` block,
    which made post-hoc cost / cache analysis impossible for clean runs).
    Pruning is operator-managed; see briefing for `find -mtime` cleanup.

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
    # TB-193: `goal.md` becomes daemon-mutable via the `update_goal`
    # operator-queue op so refreshing the project mission while the
    # daemon runs no longer requires `ap2 daemon-control --pause`. Once
    # mutable, rollback cohesion demands it be in the snapshot baseline
    # — otherwise an `ap2 rollback` past an `update_goal` commit would
    # leave goal.md at the new content while every other state file
    # reverts (the same failure mode TB-192 catches for `_index.md`).
    # Adding it here also means out-of-band edits during a pause get
    # auto-picked up by the next snapshot/diff cron commit (acceptable:
    # pause-edits are still rare post-TB-193 and the auto-commit
    # eliminates an entire class of "operator forgot to commit goal.md"
    # footgun).
    "goal.md",
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
    # TB-188: per-proposal records (one JSON per ideation-authored
    # proposal, keyed on TB-N). Daemon-owned audit trail — written at
    # `add_backlog` time and reconciled with an `outcome` block on the
    # first terminal event (task_complete / operator approve / reject /
    # delete). Bundled into the state-dirs set so signal-collection
    # follow-ups (TB-189 delete-test verdict, acceptance-rate
    # aggregation, retrospective classifier) can query history across
    # cycles, and so an `ap2 rollback` past a state commit reverts the
    # records alongside the board / progress / cron state they were
    # paired with.
    ".cc-autopilot/ideation_proposals",
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
        # TB-188: terminal-event reconciliation, mirroring `run_task`.
        # The pipeline_pending sweep is the second of two task_complete
        # emission sites; both must reconcile so a proposal whose work
        # rode through the pipeline path doesn't end up with an empty
        # `outcome` block in its record.
        sweep_commit = result_summary.get("commit", "") or ""
        if final_status == "complete":
            tools.reconcile_proposal_outcome(
                cfg, task.id,
                decision_kind="completed",
                decision_actor="daemon",
                commit=sweep_commit[:8] or None,
            )
        elif final_status == "verification_failed":
            tools.reconcile_proposal_outcome(
                cfg, task.id,
                decision_kind="verification_failed",
                decision_actor="verifier",
                commit=sweep_commit[:8] or None,
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
    - The TB-188 proposal record (`.cc-autopilot/ideation_proposals/<TB-N>.json`):
      `reconcile_proposal_outcome` appends an `outcome` block on
      `task_complete` (status=complete or status=verification_failed).
      The path is included unconditionally — `_filter_state_paths`
      drops it for tasks without a record (legacy / non-ideation
      proposals), and `git diff --cached --quiet` drops it for
      no-op reconciliations.

    Files that exist but weren't actually modified are filtered downstream
    by `git diff --cached --quiet`, so passing a fixed superset is safe.
    """
    paths = [
        "TASKS.md",
        ".cc-autopilot/progress.md",
        ".cc-autopilot/retry_state.json",
        f".cc-autopilot/ideation_proposals/{task.id}.json",
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


# TB-223: cumulative-regression circuit-breaker for the opt-in
# `AP2_AUTO_APPROVE` mode. When N consecutive `task_complete` events
# end with a failure status AND ultimately route to `retry_exhausted`,
# the daemon halts auto-promotion of tasks that were auto-approved by
# ideation. Operator-approved tasks (those that went through
# `ap2 approve` after `@blocked:review` was preserved) continue to
# dispatch normally — the pause is targeted, not blanket.
#
# Unfreeze: the operator runs `ap2 ack auto_approve_unfreeze --reason
# "..."` (the existing `ap2 ack` verb + queue plumbing per TB-106 /
# TB-201). The drain-side emits an `operator_ack` event with the note
# carrying the `auto_approve_unfreeze` token; `_auto_approve_paused`
# below treats that as a state reset — subsequent failure counting
# starts from the next `task_complete`.
#
# Tunable via `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` (default 3). Setting
# it to 0 (or any non-positive integer) effectively disables the
# circuit-breaker (the freeze check immediately returns False), which
# is the escape hatch for operators who explicitly trust the upstream
# gates beyond this layer.
_AUTO_APPROVE_FAILURE_STATUSES: frozenset[str] = frozenset(
    {"verification_failed", "blocked", "error", "failed"},
)
_AUTO_APPROVE_UNFREEZE_TOKEN = "auto_approve_unfreeze"


def _auto_approve_freeze_threshold() -> int:
    """Effective threshold for the auto-approve cumulative-regression
    circuit-breaker, env-overridable via `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`.

    Default 3 (`ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT`).
    Non-int / empty values silently fall back to the default; a value
    `<= 0` is treated as "circuit-breaker disabled" (see
    `_auto_approve_paused` which returns False in that case so an
    operator who wants the auto-approve dispatch without the safety
    net can configure that explicitly). Same permissive-parse shape
    as `ideation._cooldown_s`.
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "").strip()
    if not raw:
        return ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT


def _auto_approve_paused(cfg: Config) -> bool:
    """True iff the auto-approve dispatch path should be halted now.

    Reads the tail of `events.jsonl` and looks at the most recent
    `task_complete` events. The path is paused when:
      - The threshold N (= `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, default
        3) is positive, AND
      - The last N `task_complete` events all carry a failure status
        in `_AUTO_APPROVE_FAILURE_STATUSES`, AND
      - The most recent of those was followed by a `retry_exhausted`
        event for the same task (the briefing's "end in
        `retry_exhausted`" qualifier — the failure chain ultimately
        froze a task rather than just looping a single TB through
        retries), AND
      - The operator has NOT emitted an `operator_ack` whose `note`
        contains `auto_approve_unfreeze` AFTER the failure window
        started (the explicit reset signal).

    Threshold `<= 0` short-circuits to False (operator opted out of
    the circuit-breaker explicitly — see the parser comment).

    Pure / no I/O beyond the events.jsonl tail read; safe to call from
    `_tick` without taking the board lock.
    """
    threshold = _auto_approve_freeze_threshold()
    if threshold <= 0:
        return False
    if not cfg.events_file.exists():
        return False
    # Tail-window must be big enough to cover the threshold-N
    # completions plus interleaved noise (status_report, cron, judge
    # calls). 500 is a generous default; production events.jsonl tail
    # is dominated by observability lines, so a bigger window is cheap
    # (events.tail is bounded by the file).
    tail = events.tail(cfg.events_file, 500)
    # Reset state at the most recent unfreeze ack: anything before it
    # is "old water under the bridge" and doesn't count toward the
    # current consecutive-failure window.
    last_unfreeze_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") != "operator_ack":
            continue
        note = str(e.get("note") or "")
        if _AUTO_APPROVE_UNFREEZE_TOKEN in note:
            last_unfreeze_idx = i
    relevant = tail[last_unfreeze_idx + 1:]
    # Collect `task_complete` events in order.
    completes = [e for e in relevant if e.get("type") == "task_complete"]
    if len(completes) < threshold:
        return False
    window = completes[-threshold:]
    if not all(
        str(e.get("status", "")).strip() in _AUTO_APPROVE_FAILURE_STATUSES
        for e in window
    ):
        return False
    # The "end in retry_exhausted" qualifier: the most recent failing
    # task_complete must have been followed by a retry_exhausted event
    # for the same task (i.e. the failure chain actually froze a task,
    # not just looped a single TB through one retry). Scan the
    # `relevant` slice forward from the last-window-completion onward.
    final_complete = window[-1]
    final_task = str(final_complete.get("task") or "")
    if not final_task:
        return False
    # Find the index of `final_complete` in `relevant` and scan after.
    try:
        final_idx = next(
            i for i, e in enumerate(relevant) if e is final_complete
        )
    except StopIteration:
        return False
    for e in relevant[final_idx:]:
        if (
            e.get("type") == "retry_exhausted"
            and str(e.get("task") or "") == final_task
        ):
            return True
    return False


def _was_auto_approved(cfg: Config, task_id: str) -> bool:
    """True iff `task_id` has an `auto_approved` event in events.jsonl
    AND no subsequent `ideation_approved` event for the same TB-N
    (which would indicate the operator subsequently `ap2 approve`'d
    the task, promoting it to the operator-approved bucket).

    Drives the per-task gate at `_tick`'s auto-promote step: when the
    circuit-breaker is active (`_auto_approve_paused`), we still want
    to let operator-approved tasks through. Distinguishing
    auto-approved (event = `auto_approved`) from operator-approved
    (event = `ideation_approved`) lets the gate apply at the right
    granularity.

    A task that was auto-approved AND later operator-approved counts
    as operator-approved (the operator's explicit decision overrides
    the auto layer). Pure / events.jsonl tail read only.
    """
    if not cfg.events_file.exists():
        return False
    tail = events.tail(cfg.events_file, 1000)
    auto_seen = False
    for e in tail:
        if str(e.get("task") or "") != task_id:
            continue
        typ = e.get("type")
        if typ == "auto_approved":
            auto_seen = True
        elif typ == "ideation_approved":
            # Operator explicitly approved → no longer in the
            # auto-approved bucket regardless of prior auto stamp.
            auto_seen = False
    return auto_seen


# ============================================================================
# TB-224: cost + blast-radius guards layered on TB-223's auto-approve gate.
#
# Two env knobs (per-task + 24h-rolling-window token caps) plus a single-
# event `task_error` halt. All three halt conditions share the same
# auto-promote-paused state and resume via the same operator ack verb
# `ap2 ack auto_approve_window_resume`. Defaults are unset on both knobs
# → no caps applied (current behavior preserved); the operator opts in
# alongside flipping `AP2_AUTO_APPROVE=1`.
#
# Why two knobs, not one:
#   - `per_task_cap` catches the single-runaway pattern: one task in an
#     infinite tool-call loop burning $50 of tokens before the verifier
#     even runs.
#   - `window_cap` catches the drift pattern: 50 small tasks each within
#     the per-task cap but cumulatively unbounded.
# Orthogonal failure modes; both must be operator-tunable. Same shape as
# TB-223's per-task vs. cumulative-regression layering.
#
# Why "post-hoc" detection (vs. predictive estimator): `task_run_usage`
# events emit only at terminal paths (TB-165), so the cap fires AFTER the
# offending task finished — not mid-stream. The auto-promote-stream halt
# is what catches the "one more task in this loop would be unsafe"
# pattern at the right moment (next tick, before the next auto-approved
# task would dispatch). The briefing's "halt the in-flight task" framing
# in Scope (1) is forward-looking — practically the daemon detects after
# completion and gates the NEXT auto-promote. Same shape, slightly
# delayed actuation. The briefing explicitly excludes predictive cost
# estimation from this task's scope.
#
# Why one shared ack verb (`auto_approve_window_resume`) instead of one
# per cap: the operator's mental model collapses to "auto-promote
# paused" regardless of which cap tripped — three distinct resume verbs
# would be unnecessary friction. The audit trail's
# `auto_approve_halted reason=...` event field preserves the forensic
# distinction so an offline reader can still tell which cap tripped.
#
# Why `task_error` is single-event (no N threshold like TB-223): a
# `task_error` event indicates infrastructure failure (SDK timeout,
# agent OOM, briefing read failure) per `ap2/events.py` conventions. It
# is structurally rare in steady-state (the verifier's normal failure
# path is `verification_failed`, not `task_error`); a single event
# indicates infrastructure breakage that benefits from operator
# attention immediately, not after N similar events. Distinct from
# TB-223's cumulative-regression N=3 default which is calibrated for
# the noisier `verification_failed` channel.
# ============================================================================

_AUTO_APPROVE_WINDOW_RESUME_TOKEN = "auto_approve_window_resume"
_AUTO_APPROVE_WINDOW_S = 24 * 3600  # 24h rolling window


def _per_task_token_cap() -> int:
    """Effective per-task token cap from `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`.

    Returns `0` (cap disabled) when the env var is unset / empty /
    non-integer / non-positive. Operators who haven't budgeted their
    project don't get a hardcoded cap surprising them; the explicit
    way to disable is to leave the knob unset (or set it to `0`).
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
    except ValueError:
        return 0
    return v if v > 0 else 0


def _window_token_cap() -> int:
    """Effective 24h rolling-window token cap from
    `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`.

    Returns `0` (cap disabled) when the env var is unset / empty /
    non-integer / non-positive. Same parse shape as
    `_per_task_token_cap` so the two knobs share one mental model.
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
    except ValueError:
        return 0
    return v if v > 0 else 0


def _event_combined_tokens(event: dict) -> int:
    """Combined `input_tokens + output_tokens` from a `task_run_usage`
    event's `usage` blob (TB-165 schema). Robust against missing
    fields or a non-dict `usage` (returns 0 in those cases — matches
    the defensive shape of `events.summarize_usage_event`).
    """
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return 0
    inp = int(usage.get("input_tokens", 0) or 0)
    outp = int(usage.get("output_tokens", 0) or 0)
    return inp + outp


def _parse_event_ts(ts: object) -> float | None:
    """Parse an event `ts` field (ISO8601 with `Z` suffix, per
    `_shared.now()`) to epoch seconds. Returns `None` on parse
    failure — events.jsonl shape has been stable but a malformed
    line shouldn't crash the auto-promote step.
    """
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _auto_approve_window_resume_idx(tail: list[dict]) -> int:
    """Index of the most recent `operator_ack` whose `note` contains
    the `auto_approve_window_resume` token. Returns `-1` when absent.

    Same shape as `_auto_approve_paused`'s `last_unfreeze_idx` scan
    (TB-223), but on a distinct token. Two distinct ack tokens because
    the auto-promote-paused state has two semantically-distinct entry
    paths (TB-223 cumulative-regression vs. TB-224 cost+blast-radius)
    and operators benefit from a forensic record of which class of
    issue triggered the pause.
    """
    last = -1
    for i, e in enumerate(tail):
        if e.get("type") != "operator_ack":
            continue
        note = str(e.get("note") or "")
        if _AUTO_APPROVE_WINDOW_RESUME_TOKEN in note:
            last = i
    return last


def _auto_approved_task_ids(tail: list[dict]) -> set[str]:
    """Set of TB-Ns that ideation auto-approved within `tail`, with
    subsequent `ideation_approved` events removing them (a task the
    operator subsequently `ap2 approve`'d is no longer in the auto
    bucket — same rule as `_was_auto_approved`).

    Materialized as a set so the per-task / window scans below can
    filter `task_run_usage` events with O(1) lookups instead of
    re-scanning the tail per event.
    """
    auto: set[str] = set()
    for e in tail:
        tid = str(e.get("task") or "").strip()
        if not tid:
            continue
        typ = e.get("type")
        if typ == "auto_approved":
            auto.add(tid)
        elif typ == "ideation_approved":
            auto.discard(tid)
    return auto


def _auto_approve_check_violations(
    cfg: Config,
) -> tuple[str, int, int, str, str] | None:
    """Inspect recent events for TB-224 cost / blast-radius violations.

    Returns `None` when no halt condition fires, or a 5-tuple:
        (reason, total_used, cap, trigger_task, detail)

    where:
      - `reason` is one of `"per_task_cap"`, `"window_cap"`,
        `"task_error"`.
      - `total_used` is the token count that tripped the cap (or `0`
        for `task_error`).
      - `cap` is the effective env-knob value (`0` for `task_error`).
      - `trigger_task` is the offending TB-N (`""` if no single task
        is "the" trigger — today the window-cap path may have this
        shape when the sum tips over from interleaved tasks).
      - `detail` is a short excerpt (used for `task_error`).

    Order of precedence: `task_error` first (infrastructure issue —
    immediate attention), then `per_task_cap` (single runaway), then
    `window_cap` (drift sum). The first match short-circuits — only
    one halt event fires per tick regardless of how many conditions
    overlap.

    Resume semantics: the most recent `operator_ack` carrying the
    `auto_approve_window_resume` token resets all three checks to a
    fresh post-ack window. Events before the ack don't count; the
    operator explicitly cleared the halt and we trust that decision.

    Pure / events.jsonl tail-read only. Safe to call from `_tick`
    without taking the board lock.
    """
    if not cfg.events_file.exists():
        return None
    # 2000-event tail comfortably covers 24h of activity for typical
    # ap2 projects (a tight ideation+task loop emits ~30 events per
    # hour). Bigger than `_auto_approve_paused`'s 500 because the
    # window-cap sum legitimately spans 24h of `task_run_usage`
    # arrivals interleaved with cron / status-report observability
    # noise.
    tail = events.tail(cfg.events_file, 2000)
    if not tail:
        return None
    resume_idx = _auto_approve_window_resume_idx(tail)
    relevant = tail[resume_idx + 1:]
    if not relevant:
        return None

    # Auto-approved task ids: scan the FULL tail (a task auto-approved
    # before the ack still belongs to the auto bucket — the ack
    # resets the halt state, not the per-task category).
    auto_ids = _auto_approved_task_ids(tail)

    # 1) `task_error` on an auto-approved task — single-event halt.
    #    Distinct from `verification_failed` (TB-223 regression-pause
    #    condition) because infrastructure failures aren't noise.
    for e in relevant:
        if e.get("type") != "task_error":
            continue
        tid = str(e.get("task") or "").strip()
        if not tid or tid not in auto_ids:
            continue
        detail = str(e.get("error") or "")[:160]
        return ("task_error", 0, 0, tid, detail)

    per_task_cap = _per_task_token_cap()
    window_cap = _window_token_cap()

    # 2) `per_task_cap` — any task_run_usage for an auto-approved task
    #    whose tokens exceed the cap.
    if per_task_cap > 0:
        for e in relevant:
            if e.get("type") != "task_run_usage":
                continue
            tid = str(e.get("task") or "").strip()
            if not tid or tid not in auto_ids:
                continue
            used = _event_combined_tokens(e)
            if used > per_task_cap:
                return ("per_task_cap", used, per_task_cap, tid, "")

    # 3) `window_cap` — sum of input+output tokens across all
    #    auto-approved `task_run_usage` events within the last 24h
    #    (post-ack). Same shape `ap2 status-report`'s recent-events
    #    surface uses: tail scan, no new state file, no new
    #    persistence contract.
    if window_cap > 0:
        now_s = time.time()
        total = 0
        for e in relevant:
            if e.get("type") != "task_run_usage":
                continue
            tid = str(e.get("task") or "").strip()
            if not tid or tid not in auto_ids:
                continue
            ts = _parse_event_ts(e.get("ts"))
            if ts is None:
                continue
            if now_s - ts > _AUTO_APPROVE_WINDOW_S:
                continue
            total += _event_combined_tokens(e)
        if total > window_cap:
            return ("window_cap", total, window_cap, "", "")

    return None


def _auto_approve_already_halted(cfg: Config) -> bool:
    """True iff an `auto_approve_halted` event has already fired since
    the most recent `auto_approve_window_resume` operator ack.

    Dedupe gate so each triggering episode emits exactly ONE
    `auto_approve_halted` event (the "first-time" halt notification)
    even when the daemon's auto-promote step re-detects the same
    violation on every tick. Subsequent ticks still emit
    `auto_approve_skipped` per preempted promotion attempt.
    """
    if not cfg.events_file.exists():
        return False
    tail = events.tail(cfg.events_file, 2000)
    resume_idx = _auto_approve_window_resume_idx(tail)
    relevant = tail[resume_idx + 1:]
    for e in relevant:
        if e.get("type") == "auto_approve_halted":
            return True
    return False


def _append_decisions_needed_bullet(cfg: Config, bullet: str) -> None:
    """Append a bullet to the `## Decisions needed from operator`
    section of `.cc-autopilot/ideation_state.md`. Creates the section
    at end-of-file if absent. Atomic write (tmpfile + rename) mirroring
    `do_ideation_state_write`'s shape.

    Used by TB-224's `task_error` halt to surface the failing TB-N +
    error excerpt as an actionable decision on `ap2 status` /
    `ap2 logs` / the web home page without waiting for the next
    ideation cron — the same `parse_operator_decisions` reader the
    three surfaces consume picks up the new bullet automatically.

    Caller responsibility: pass a clean bullet body (no leading `- `).
    The function adds the bullet marker. Newlines inside `bullet` are
    preserved as continuation lines (callers should keep entries
    single-line for the existing parser's bullet-extraction shape).
    """
    import re as _re
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text()
    else:
        text = "# Ideation State\n\n"
    header_re = _re.compile(
        r"^##\s+Decisions needed from operator\s*$", _re.M,
    )
    next_re = _re.compile(r"^##\s+", _re.M)
    m = header_re.search(text)
    bullet_line = f"- {bullet.strip()}\n"
    if m is None:
        # No section yet — append fresh `## Decisions needed from operator`
        # at end-of-file. Two leading newlines to keep section spacing
        # consistent with the ideation prompt's schema.
        sep = ""
        if text and not text.endswith("\n"):
            sep = "\n\n"
        elif text.endswith("\n") and not text.endswith("\n\n"):
            sep = "\n"
        new_text = (
            text + sep + "## Decisions needed from operator\n\n" + bullet_line
        )
    else:
        # Insert the new bullet at the end of the existing section
        # body (just before the next `## ` header or EOF). Preserves
        # any sibling sections that follow.
        body_start = m.end()
        next_m = next_re.search(text, body_start)
        section_end = next_m.start() if next_m else len(text)
        body = text[body_start:section_end]
        body_rstripped = body.rstrip("\n")
        # One blank line between header and bullets when the body was
        # empty; otherwise just append after the existing bullets.
        if not body_rstripped.strip():
            new_body = "\n\n" + bullet_line + "\n"
        else:
            new_body = body_rstripped + "\n" + bullet_line + "\n"
        new_text = text[:body_start] + new_body + text[section_end:]
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)


# ============================================================================
# TB-225: Auto-apply agent-diagnosed briefing-shape fixes from `task_complete
# status=blocked` summaries.
#
# Recurring failure mode: a Frozen task whose root cause is a briefing-shape
# regression the agent already diagnosed in its `task_complete blocked`
# summary (e.g. TB-204's `grep -lE` → `grep -rlE`, TB-207's literal-backtick
# in shell bullets). The agent emits a structured `BriefingFix: <shape> at
# <path>:<line>: <from> -> <to>` line; the daemon parses it, verifies the
# briefing-line literal match (closes the operator-edit-during-failure
# data-race window), patches the briefing via the operator-queue `update` op
# (TB-153 lineage — same audit-trail + atomic-with-redispatch contract as
# operator-applied edits), and unfreezes the task.
#
# Allowlist-driven: `AP2_AUTO_UNFREEZE_FIX_SHAPES` names the trust contract.
# Unknown shapes still require manual `ap2 unfreeze`. Per-task + per-day caps
# bound blast radius. Default-unset on the shapes knob → feature opt-in only;
# operators upgrade trust by adding shape tokens, never by removing safeties.
#
# Separate event audit trail: `auto_unfreeze_applied` (success) and
# `auto_unfreeze_skipped` (any guarded skip — `knob_unset` is implicit and
# does NOT emit per-tick to avoid noise; the other reasons all emit so the
# operator can see why a Frozen task stayed Frozen).
# ============================================================================

_AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT = 1
_AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT = 3
_AUTO_UNFREEZE_WINDOW_S = 24 * 3600  # rolling 24h, mirrors TB-224's window


def _auto_unfreeze_allowlist() -> frozenset[str]:
    """Effective allowlist parsed from `AP2_AUTO_UNFREEZE_FIX_SHAPES`.

    Comma-separated shape tokens. Default unset → empty set, which the
    `_maybe_auto_unfreeze` caller treats as "feature disabled" (no
    auto-unfreeze attempts, no skip events). Operators opt in by listing
    shape tokens; the env-knob string IS the trust contract.

    Whitespace around tokens is trimmed; empty tokens (e.g. trailing
    comma) are dropped. The frozenset return makes the value safe to
    pass around or compare against without defensive copies.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_FIX_SHAPES", "").strip()
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _auto_unfreeze_dry_run() -> bool:
    """TB-233: True iff `AP2_AUTO_UNFREEZE_DRY_RUN` is set to a truthy
    value (`"1"` / `"true"` / `"yes"`, case-insensitive).

    Monitor-only on-ramp for the auto-unfreeze loop (TB-225), sibling
    of `automation_status._is_auto_approve_dry_run` (TB-232) on the
    axis-1 side. When both `AP2_AUTO_UNFREEZE_FIX_SHAPES` (non-empty)
    AND `AP2_AUTO_UNFREEZE_DRY_RUN=1` are set, `_maybe_auto_unfreeze`
    runs the entire guard chain (allowlist + per-task cap + per-day
    cap + briefing-line match) but, instead of calling
    `_apply_auto_unfreeze_patch`, emits a `would_auto_unfreeze` audit
    event with the same payload shape as `auto_unfreeze_applied`. The
    briefing file is NOT mutated and no operator-queue ops are
    appended; the per-day-count counter does NOT increment in dry-run
    (no real application). Operator observes the simulated decisions
    in `ap2 logs --type would_auto_unfreeze` (and the status-report's
    automation-loop digest from TB-228) for a window, gains confidence
    on the live Frozen set, then flips the dry-run knob off to engage
    real patching.

    Default unset → False (current TB-225 behavior; byte-identical to
    pre-TB-233 when the knob has never been set). Permissive parse
    mirrors the boolean shape used by `_is_truthy` in
    `automation_status.py` so operators tuning the autopilot env file
    see one consistent convention across knobs.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_DRY_RUN", "").strip().lower()
    return raw in ("1", "true", "yes")


def _auto_unfreeze_max_per_task() -> int:
    """Per-task cap from `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default 1).

    Bounds oscillation when an auto-applied patch still fails: a task
    that's been auto-unfrozen once and re-frozen falls back to manual
    `ap2 unfreeze`. Default 1 because the typical recurrence is "fix
    once, succeeds on retry"; >1 indicates the patched form ALSO
    failed and the operator should see it.

    Permissive parse: empty / non-int / negative falls back to the
    default. Zero is honored (caps disabled = unbounded retries) but
    is intentionally NOT the default — disabling the per-task cap
    should be an explicit operator decision.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "").strip()
    if not raw:
        return _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT
    return v if v >= 0 else _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT


def _auto_unfreeze_max_per_day() -> int:
    """Per-day cap from `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default 3).

    Rolling 24h cap on total auto-unfreeze applications across all
    tasks. Bounds the "systemic regression cascades through 10 tasks
    before operator notices" failure mode. When exceeded, the daemon
    halts and surfaces a `## Decisions needed from operator` bullet so
    the operator sees a systemic-regression signal rather than a silent
    burn.

    Default 3 calibrated for the observed steady-state recurrence rate
    on this codebase (TB-204 + TB-207 = 2 instances in one week);
    higher values invite the silent-burn failure mode, lower values
    invite operator-toil from over-frequent caps.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "").strip()
    if not raw:
        return _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT
    return v if v >= 0 else _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT


def _most_recent_blocked_complete_for(
    tail: list[dict], task_id: str,
) -> dict | None:
    """Return the most recent `task_complete status=blocked` event for
    `task_id` in the events tail, or None when no such event exists.

    Tail is ordered oldest-first (per `events.tail`); we scan forward
    and keep the last match. `blocked` is the specific agent-emitted
    status the parser contract attaches to — the agent's own
    `report_result(status="blocked", summary=...)` carries the
    `BriefingFix:` line in `summary`. Other failure statuses
    (`verification_failed`, `failed`, `incomplete`) do not — they're
    daemon-synthesized or agent-emitted-without-fix-shape paths.
    """
    last: dict | None = None
    for e in tail:
        if e.get("type") != "task_complete":
            continue
        if str(e.get("task") or "") != task_id:
            continue
        if str(e.get("status") or "").strip() != "blocked":
            continue
        last = e
    return last


def _count_auto_unfreeze_applied_for_task(
    tail: list[dict], task_id: str,
) -> int:
    """Count `auto_unfreeze_applied` events for `task_id` over the full
    tail. The per-task cap fires when this hits
    `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default 1).

    No window — a task that's been auto-unfrozen even once long ago
    must NOT silently re-cycle through auto-unfreeze attempts after
    every fresh freeze. The operator's manual `ap2 unfreeze` is the
    expected escape after the per-task cap trips; that emits a
    `task_unfrozen` event but does NOT reset this counter (intentional
    — the per-task cap is about "this task is auto-unfreeze-eligible
    over its whole lifetime," not "since the last operator touch").
    """
    return sum(
        1
        for e in tail
        if e.get("type") == "auto_unfreeze_applied"
        and str(e.get("task") or "") == task_id
    )


def _count_auto_unfreeze_applied_in_window(
    tail: list[dict], *, now_s: float | None = None,
) -> int:
    """Count `auto_unfreeze_applied` events whose `ts` falls within the
    last `_AUTO_UNFREEZE_WINDOW_S` (24h). The per-day cap fires when
    this hits `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default 3).

    Rolling window (not calendar day) to match TB-224's
    cost-cap-window shape — same operator-rhythm rationale, no
    timezone ambiguity. Events with unparseable `ts` are skipped
    rather than counted (defensive; matches `_parse_event_ts`'s
    convention).
    """
    if now_s is None:
        now_s = time.time()
    count = 0
    for e in tail:
        if e.get("type") != "auto_unfreeze_applied":
            continue
        ts = _parse_event_ts(e.get("ts"))
        if ts is None:
            continue
        if now_s - ts > _AUTO_UNFREEZE_WINDOW_S:
            continue
        count += 1
    return count


def _apply_auto_unfreeze_patch(
    cfg: Config,
    *,
    task_id: str,
    fix: dict,
) -> str | None:
    """Apply the agent-diagnosed line replacement to the briefing file
    and queue the `update` + `unfreeze` ops on the operator queue.
    Returns None on success, or a `reason` token on guarded skip.

    Guards (in order):
      - `briefing_path_missing`: the `file` named in the fix doesn't
        exist on disk (briefing was renamed / deleted between failure
        and freeze handling).
      - `briefing_mismatch`: the named line doesn't literally contain
        the `from` pattern. The agent's diagnosis is stale (e.g. the
        operator hand-edited the briefing mid-failure to try fixing
        it themselves). The fail-safe is to leave the task Frozen.
      - `queue_error`: the operator-queue `update` or `unfreeze` op
        rejected our payload (structural validation, board-state
        mismatch). Surfaces the underlying `_err` text so post-hoc
        forensics can grep for the rejection reason.

    The patch is applied as the operator-queue `update` op with
    `briefing=<full new content>` and `skip_goal_alignment=True` (the
    briefing was already goal-validated at add time; a mechanical
    single-line fix doesn't change the goal anchor). The `unfreeze` op
    moves the task from Frozen → Backlog and resets the retry counter.
    Both ops drain on the NEXT tick — one-tick delay before the task
    is dispatchable, the trade-off for the audit-trail symmetry with
    operator-applied edits (TB-153 lineage).
    """
    briefing_path = cfg.project_root / fix["file"]
    if not briefing_path.exists():
        return "briefing_path_missing"
    try:
        content = briefing_path.read_text()
    except OSError:
        return "briefing_path_missing"
    lines = content.splitlines(keepends=True)
    line_no = fix["line"]
    if line_no < 1 or line_no > len(lines):
        return "briefing_mismatch"
    target_line = lines[line_no - 1]
    if fix["from"] not in target_line:
        return "briefing_mismatch"
    new_line = target_line.replace(fix["from"], fix["to"], 1)
    if new_line == target_line:
        # Replacement was a no-op (from == to, or from empty). Still
        # a mismatch in spirit — refuse to spend an auto-unfreeze slot
        # on a no-op patch.
        return "briefing_mismatch"
    lines[line_no - 1] = new_line
    new_content = "".join(lines)
    # Queue the update + unfreeze ops. Order matters: the drain applies
    # them in queue order, so the briefing patch lands before the
    # unfreeze (which makes the task dispatchable). A failed unfreeze
    # after a successful update leaves the briefing patched but the
    # task Frozen — the operator can still manually unfreeze with the
    # patched briefing already in place, which is the right fail-safe.
    update_res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": task_id,
            "briefing": new_content,
            "skip_goal_alignment": True,
        },
    )
    if update_res.get("isError"):
        return "queue_error"
    unfreeze_res = tools.do_operator_queue_append(
        cfg,
        {"op": "unfreeze", "task_id": task_id},
    )
    if unfreeze_res.get("isError"):
        return "queue_error"
    return None


def _maybe_auto_unfreeze(cfg: Config) -> None:
    """Sweep Frozen tasks for agent-diagnosed briefing-shape fixes and
    apply any that pass the allowlist + cap + briefing-match guards
    (TB-225).

    Pure / side-effect-bounded: writes events + queues operator ops,
    never touches TASKS.md / briefings directly. Safe to call from
    `_tick` without taking the board lock (operator-queue append takes
    its own narrow lock, board reads are crash-tolerant).

    The function is a no-op when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is
    unset (the feature's master switch — opt-in only; no skip events
    fire in this branch to avoid `events.jsonl` noise from operators
    who haven't engaged the feature). All OTHER guarded skips emit a
    structured `auto_unfreeze_skipped reason=<token>` event so the
    operator can see, via `ap2 logs`, why a Frozen task stayed Frozen.

    Per-task cap (`AP2_AUTO_UNFREEZE_MAX_PER_TASK`, default 1): once
    exceeded, the task falls back to manual `ap2 unfreeze`. Per-day
    cap (`AP2_AUTO_UNFREEZE_MAX_PER_DAY`, default 3): once exceeded,
    the daemon halts further auto-unfreeze applications on the tick
    AND surfaces a `## Decisions needed from operator` bullet so the
    operator sees a systemic-regression signal rather than a silent
    burn.
    """
    allowlist = _auto_unfreeze_allowlist()
    if not allowlist:
        return
    if not cfg.tasks_file.exists():
        return
    try:
        board = Board.load(cfg.tasks_file)
    except Exception:  # noqa: BLE001
        return
    frozen_tasks = list(board.iter_tasks("Frozen"))
    if not frozen_tasks:
        return
    tail = events.tail(cfg.events_file, 2000)
    per_task_cap = _auto_unfreeze_max_per_task()
    per_day_cap = _auto_unfreeze_max_per_day()
    day_count = _count_auto_unfreeze_applied_in_window(tail)

    for task in frozen_tasks:
        last_blocked = _most_recent_blocked_complete_for(tail, task.id)
        if last_blocked is None:
            # No diagnosed fix-shape — silently leave Frozen. The
            # operator-manual path is the expected route for non-
            # blocked failure-statuses (verification_failed, error,
            # timeout) and for tasks that simply haven't surfaced a
            # diagnosable summary yet. No skip event: this is the
            # baseline state for most Frozen tasks.
            continue
        summary = str(last_blocked.get("summary") or "")
        fix = _shared_parse(summary)
        if fix is None:
            # Malformed / missing `BriefingFix:` prefix. The agent's
            # summary lacked a structured diagnosis — fall back to
            # today's manual-unfreeze path. No skip event for the
            # same reason as the no-blocked-complete case: baseline.
            continue
        if fix["shape"] not in allowlist:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason="shape_not_in_allowlist",
                shape=fix["shape"],
            )
            continue
        prior_for_task = _count_auto_unfreeze_applied_for_task(
            tail, task.id,
        )
        if per_task_cap > 0 and prior_for_task >= per_task_cap:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason="per_task_cap",
                applied=prior_for_task,
                cap=per_task_cap,
            )
            continue
        if per_day_cap > 0 and day_count >= per_day_cap:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason="per_day_cap",
                applied=day_count,
                cap=per_day_cap,
            )
            # TB-233: in dry-run the per-day cap halt is still the
            # right signal that the allowlist would generate more
            # applications than the safety floor allows — surface it
            # pre-flight. The decisions-needed bullet AND the
            # `## Decisions needed from operator` mutation, however,
            # belong to the real-application path only; dry-run is
            # monitor-only and must NOT touch board / state. Skip the
            # bullet append in dry-run and short-circuit the same way
            # the real path does. Operator sees the skip event +
            # (over the dry-run window) the would_auto_unfreeze stream
            # and infers the systemic-regression signal directly from
            # the auto_unfreeze_skipped count.
            if _auto_unfreeze_dry_run():
                return
            try:
                _append_decisions_needed_bullet(
                    cfg,
                    (
                        f"Auto-unfreeze daily cap reached "
                        f"({day_count}/{per_day_cap}) — systemic-regression "
                        f"signal. Recent Frozen tasks are exhausting the "
                        f"briefing-shape auto-heal budget; inspect via "
                        f"`ap2 logs --type auto_unfreeze_applied` and "
                        f"either bump `AP2_AUTO_UNFREEZE_MAX_PER_DAY` or "
                        f"investigate why so many briefing-shape regressions "
                        f"are landing."
                    ),
                )
            except OSError:
                pass
            # Halt: no further auto-unfreeze attempts this tick. The
            # remaining Frozen tasks (if any) stay Frozen until the
            # window rolls forward or the operator intervenes.
            return
        # TB-233: dry-run check happens AFTER all skip-emission so
        # the operator's dry-run window observes the same
        # `auto_unfreeze_skipped` events it would see live — the only
        # change in dry-run is the WRITE step: instead of calling
        # `_apply_auto_unfreeze_patch` (which queues `update` +
        # `unfreeze` ops on the operator queue and mutates the
        # briefing file), emit a `would_auto_unfreeze` audit event
        # with the same payload shape as `auto_unfreeze_applied` and
        # continue. The per-day-count + per-task-prior-count
        # counters do NOT increment in dry-run (no real application),
        # so a dry-run window can observe MORE simulated decisions
        # than the per-day cap would normally allow — that's the
        # right shape (the operator wants to see the full Frozen-set
        # decision before flipping the switch). When dry-run is off,
        # behavior is byte-identical to pre-TB-233.
        if _auto_unfreeze_dry_run():
            events.append(
                cfg.events_file,
                "would_auto_unfreeze",
                task=task.id,
                shape=fix["shape"],
                file=fix["file"],
                line=fix["line"],
                **{"from": fix["from"], "to": fix["to"]},
            )
            continue
        skip_reason = _apply_auto_unfreeze_patch(
            cfg, task_id=task.id, fix=fix,
        )
        if skip_reason is not None:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason=skip_reason,
                shape=fix["shape"],
            )
            continue
        events.append(
            cfg.events_file,
            "auto_unfreeze_applied",
            task=task.id,
            shape=fix["shape"],
            **{"from": fix["from"], "to": fix["to"]},
        )
        day_count += 1


def _shared_parse(summary: str) -> dict | None:
    """Thin wrapper over `ap2._shared.parse_blocked_summary_fix_shape`
    so the daemon's call site keeps `parse_blocked_summary_fix_shape`
    in the daemon's module-text (TB-225 verification gate looks for
    the helper name in `ap2/daemon.py`). Same import-time module-text
    consumption pattern as TB-220's `_shared.now` / `_shared.read_pid`
    consumers.
    """
    from ap2._shared import parse_blocked_summary_fix_shape
    return parse_blocked_summary_fix_shape(summary)


# ============================================================================
# TB-226 axis 4: focus-list pointer advance.
#
# Reads goal.md's multi-`## Current focus:` heading list + the runtime
# pointer (`focus_pointer.json`). Advances the in-memory pointer when:
#   - The active focus carries an explicit `Done when:` sub-block AND a
#     short LLM-judge call rules the bullets substantively met (one judge
#     call per advance attempt, cost knob `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT`).
#   - The active focus has NO explicit `Done when:` sub-block AND the
#     heuristic-fallback empty-cycles counter has reached
#     `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3). The counter
#     increments on each tick where ideation produced 0 proposals against
#     the active focus (the empty-board signal).
# When all foci exhaust, emit `roadmap_complete` (once) + a
# `## Decisions needed from operator` bullet so `ap2 status` and the web
# home page surface the halt; the dispatch path's roadmap-complete check
# blocks Backlog auto-promote until the operator extends the roadmap
# and acks via `ap2 ack roadmap_complete`.
#
# Goal.md itself is NEVER mutated (goal.md L187-191 Non-goal). The
# pointer file lives at `.cc-autopilot/focus_pointer.json`; it's both
# fenced from task agents (TASK_AGENT_FENCED_PATHS) and gitignored so
# rollbacks don't re-fire stale `focus_advanced` events.
#
# Kill-switch: `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` short-circuits the
# advance attempt even when criteria are met. The daemon surfaces a
# `## Decisions needed from operator` bullet instead so the operator
# can advance manually via `ap2 update-goal`.
# ============================================================================


_FOCUS_RECENT_TAIL_N = 200


def _ideation_empty_against_focus(tail: list[dict], focus_title: str) -> int:
    """Count consecutive recent ideation cycles that produced 0 proposals
    against `focus_title`. Walks `tail` (newest events at the end)
    backwards; resets the count at the first cycle that DID propose
    something against the focus (an `ideation_complete` whose summary
    mentions a TB-N proposal against the focus title, OR any
    `ideation_proposal_recorded` event in the window).

    Counting policy (deliberately permissive — the briefing's heuristic
    is "N consecutive 0-proposal cycles against the active focus"):
      - `ideation_empty_board` and `ideation_complete` events with no
        proposal-recorded counterpart in the same window count toward
        the empty-cycles total.
      - `ideation_proposal_recorded` resets the counter (a fresh
        proposal landed against the active focus; the focus isn't
        exhausted).
      - Events older than the most recent `focus_advanced from=<title>`
        are ignored (the prior focus's cycles don't count against the
        new active focus's freshness).
    """
    # Reset cutoff: the most recent `focus_advanced` event marks the
    # start of the current focus's window.
    cutoff_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") == "focus_advanced" and str(e.get("to") or "") == focus_title:
            cutoff_idx = i
    relevant = tail[cutoff_idx + 1:]
    count = 0
    for e in relevant:
        typ = e.get("type")
        if typ == "ideation_proposal_recorded":
            # A real proposal landed → reset.
            count = 0
            continue
        if typ in ("ideation_empty_board", "ideation_complete"):
            count += 1
    return count


async def _maybe_advance_focus(cfg: Config, sdk) -> None:
    """Focus-list advance pass (TB-226 axis 4).

    Reads goal.md's focus list + the pointer state file. If the active
    focus is exhausted, advance to the next; if all foci are exhausted,
    emit `roadmap_complete` + a decisions-needed bullet (once) so the
    dispatch path's halt check fires on subsequent ticks until the
    operator extends the roadmap + acks.

    Pure / side-effect-bounded: writes events + the pointer file +
    (rarely) one decisions-needed bullet. Does NOT mutate goal.md
    itself. Tolerates a missing goal.md / empty focus list gracefully
    (early return; the daemon's other gates handle the pre-focus-list
    state).

    The SDK Done-when judge is invoked at most once per tick (only when
    the active focus has explicit `Done when:` bullets). Cost is bounded
    by `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default `medium` — cheaper
    than the verifier's `high` because the question is one-shot and
    coarse-grained).
    """
    foci = goal.read_focus_list(cfg)
    if not foci:
        # Pre-pivot goal.md with no `## Current focus:` headings, or
        # missing goal.md entirely. Nothing to advance against.
        return

    pointer = goal.load_pointer(cfg)
    active_idx = pointer["active_index"]

    if active_idx >= len(foci):
        # Pointer already past the last focus.
        if not pointer.get("roadmap_complete_emitted"):
            # First detection of exhaustion → emit the audit event +
            # decisions-needed bullet. Subsequent ticks short-circuit
            # here (the dispatch path's `roadmap_exhausted` check
            # continues to gate Backlog promotion).
            events.append(
                cfg.events_file,
                "roadmap_complete",
                exhausted_count=len(foci),
                trigger="pointer_past_last",
            )
            try:
                _append_decisions_needed_bullet(
                    cfg,
                    (
                        f"Roadmap complete: all {len(foci)} `## Current "
                        f"focus:` heading(s) in `goal.md` are exhausted. "
                        f"Auto-promote of Backlog tasks is halted until "
                        f"the operator extends the roadmap (add new "
                        f"`## Current focus:` headings via `ap2 "
                        f"update-goal`) AND emits `ap2 ack "
                        f"roadmap_complete` to clear the halt."
                    ),
                )
            except OSError:
                pass
            pointer["roadmap_complete_emitted"] = True
            try:
                goal.save_pointer(cfg, pointer)
            except OSError:
                pass
        return

    # Active focus is in-bounds. Sync `active_title` (cheap forward-
    # compat: a hand-edited pointer with a stale title gets corrected
    # without bouncing the pointer).
    active = foci[active_idx]
    if pointer.get("active_title") != active.title:
        pointer["active_title"] = active.title
        try:
            goal.save_pointer(cfg, pointer)
        except OSError:
            pass

    # Kill-switch: even if criteria would advance, do NOT advance —
    # surface a decisions-needed bullet so the operator advances
    # manually. Idempotent via the bullet's prefix (we don't dedup;
    # the operator-decisions reader handles repeated bullets fine —
    # same shape TB-225 uses for per_day_cap halts).
    advance_disabled = goal.auto_advance_disabled()

    advance_trigger: str | None = None

    if active.has_done_when() and active.done_when_bullets:
        # Done-when judge path. Pure / SDK call only when the focus
        # has bullets to evaluate against. An empty Done-when sub-
        # block ("operator authored the heading but no criteria yet")
        # falls through to the heuristic path: there's nothing to
        # judge yet.
        verdict = await _judge_done_when(cfg, sdk, active)
        if verdict == "yes":
            advance_trigger = "done_when_judge"
        # `no` / `insufficient_evidence` / judge-error → no advance.
    else:
        # Heuristic-fallback path: count consecutive ideation cycles
        # that produced 0 proposals against the active focus.
        threshold = goal.advance_empty_cycles_threshold()
        tail = events.tail(cfg.events_file, _FOCUS_RECENT_TAIL_N)
        empty_cycles = _ideation_empty_against_focus(tail, active.title)
        # Keep the pointer's empty_cycles field in sync (forensic /
        # observability surface for `ap2 status` / web UI).
        if pointer.get("empty_cycles") != empty_cycles:
            pointer["empty_cycles"] = empty_cycles
            try:
                goal.save_pointer(cfg, pointer)
            except OSError:
                pass
        if empty_cycles >= threshold:
            advance_trigger = "empty_cycles_heuristic"

    if advance_trigger is None:
        return

    if advance_disabled:
        # Criteria are met but the operator killed auto-advance.
        # Surface as a decisions-needed bullet (one per tick attempt
        # — acceptable noise floor; the operator is expected to
        # respond promptly to a kill-switched advance).
        try:
            _append_decisions_needed_bullet(
                cfg,
                (
                    f"Focus auto-advance is disabled "
                    f"(`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`) but the "
                    f"active focus `{active.title}` would advance via "
                    f"`{advance_trigger}`. Advance manually by editing "
                    f"`goal.md` via `ap2 update-goal`, or unset the "
                    f"kill-switch to let the daemon advance "
                    f"automatically."
                ),
            )
        except OSError:
            pass
        return

    # Advance: move pointer to the next focus. Bookkeeping bumps
    # `exhausted_titles` so the operator-CLI surface can render the
    # full advance history without a separate event-log walk.
    old_title = active.title
    new_idx = active_idx + 1
    new_title = foci[new_idx].title if new_idx < len(foci) else ""
    exhausted = list(pointer.get("exhausted_titles") or [])
    if old_title and old_title not in exhausted:
        exhausted.append(old_title)
    pointer["active_index"] = new_idx
    pointer["active_title"] = new_title
    pointer["empty_cycles"] = 0
    pointer["exhausted_titles"] = exhausted
    # Reset `roadmap_complete_emitted` so a future re-exhaustion (e.g.
    # operator extends the roadmap → advance to a new focus → that
    # one also exhausts → fresh `roadmap_complete` event) re-fires
    # cleanly.
    pointer["roadmap_complete_emitted"] = False
    try:
        goal.save_pointer(cfg, pointer)
    except OSError:
        pass
    events.append(
        cfg.events_file,
        "focus_advanced",
        **{"from": old_title, "to": new_title},
        trigger=advance_trigger,
        new_index=new_idx,
        total_foci=len(foci),
    )


async def _judge_done_when(cfg: Config, sdk, focus: "goal.FocusItem") -> str:
    """Invoke a short SDK judge call to evaluate whether a focus's
    `Done when:` bullets are substantively met.

    Returns one of `"yes"`, `"no"`, `"insufficient_evidence"`, or
    `"judge_error"`. The caller only advances on `"yes"`; all other
    verdicts are conservative (leave the pointer alone).

    Cost is bounded by `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default
    `medium`). The prompt is a compact stand-alone block: focus title +
    Done-when bullets + the last ~10 `task_complete` titles + the head
    of `ideation_state.md`. No filesystem reads beyond those — the
    judge gets a finite context window per advance attempt.

    Test seam: the SDK call is mocked in `test_tb226_focus_rotation.py`
    by monkey-patching this function to return a fixed verdict. The
    function is async so the test stub can be an `async def`.
    """
    bullets = focus.done_when_bullets or []
    if not bullets:
        # Defensive: caller should already check `has_done_when()` +
        # non-empty bullets, but if we get here we can't make a
        # judgment.
        return "insufficient_evidence"

    # Build the prompt body. Compact — the brief stipulates a SHORT
    # judge call, not a full agent.
    tail = events.tail(cfg.events_file, 200)
    recent_completes: list[str] = []
    for e in tail:
        if e.get("type") != "task_complete":
            continue
        tid = str(e.get("task") or "")
        status = str(e.get("status") or "")
        summary = str(e.get("summary") or "")[:200]
        if tid:
            recent_completes.append(f"- {tid} [{status}] {summary}")
    recent_completes = recent_completes[-10:]

    ideation_state_path = (
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if ideation_state_path.exists():
        try:
            ideation_head = ideation_state_path.read_text()[:3000]
        except OSError:
            ideation_head = ""
    else:
        ideation_head = ""

    bullet_block = "\n".join(f"- {b}" for b in bullets)
    completes_block = "\n".join(recent_completes) or "(none in window)"
    prompt = (
        f"You are evaluating whether the focus `{focus.title}` in "
        f"goal.md is substantively done.\n\n"
        f"## Done-when bullets\n\n{bullet_block}\n\n"
        f"## Recent task completes (last 10)\n\n{completes_block}\n\n"
        f"## Ideation state (head)\n\n{ideation_head}\n\n"
        f"Are the Done-when bullets substantively met? Reply with one "
        f"of `yes` / `no` / `insufficient_evidence` on the FIRST line "
        f"of your response, followed by a single sentence of "
        f"rationale. The daemon parses the first token only."
    )

    effort = goal.done_when_judge_effort()
    text = ""
    try:
        options = sdk.ClaudeAgentOptions(
            cwd=str(cfg.project_root),
            allowed_tools=[],
            permission_mode="bypassPermissions",
            # 4 turns is enough for the verdict + rationale; the judge
            # has no tools so it cannot ramble across many turns. Kept
            # as a small int literal (not an env knob) because the
            # briefing names only the three TB-226 knobs as new surface
            # and adding a fourth dilutes the operator-facing knob list.
            max_turns=4,
            setting_sources=["project"],
            model=os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7"),
            extra_args={"effort": effort},
        )
        async for msg in sdk.query(prompt=prompt, options=options):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    t = getattr(part, "text", None)
                    if isinstance(t, str) and t.strip():
                        text = t.strip()
            else:
                t = getattr(msg, "result", None)
                if isinstance(t, str) and t.strip():
                    text = t.strip()
    except Exception:  # noqa: BLE001
        return "judge_error"

    if not text:
        return "insufficient_evidence"
    first = text.splitlines()[0].strip().lower()
    # Tolerate `**yes**` / `Yes.` / ``"yes"`` shapes.
    first = first.strip("*`'\".:, ")
    if first.startswith("yes"):
        return "yes"
    if first.startswith("no"):
        return "no"
    if "insufficient" in first:
        return "insufficient_evidence"
    return "insufficient_evidence"


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
    drain_res: dict = {}
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

    # 0.5. Auto-apply agent-diagnosed briefing-shape fixes to Frozen
    # tasks (TB-225). Runs AFTER the drain so we see the latest
    # operator-applied state of TASKS.md + briefings before deciding
    # whether to attempt an auto-unfreeze. Queues `update` + `unfreeze`
    # ops back onto the operator queue — they drain at the START of the
    # NEXT tick, by design (audit-trail symmetry with TB-153
    # operator-applied edits). No-op when `AP2_AUTO_UNFREEZE_FIX_SHAPES`
    # is unset (feature is opt-in).
    try:
        _maybe_auto_unfreeze(cfg)
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "auto_unfreeze_skipped",
            reason="sweep_error",
            error=f"{type(e).__name__}: {e}",
        )

    # 0.6. Focus-list pointer advance (TB-226 axis 4). Runs AFTER the
    # auto-unfreeze sweep (step 0.5) and BEFORE cron / pipeline /
    # dispatch / ideation so a freshly-advanced pointer is visible to
    # every later stage on this tick. Reads `goal.md`'s focus list and
    # `.cc-autopilot/focus_pointer.json`; advances the in-memory pointer
    # when the active focus's `Done when:` bullets are substantively met
    # (LLM-judge) or the heuristic-fallback empty-cycles threshold is
    # tripped (no explicit Done-when). Emits `focus_advanced` per
    # advance, `roadmap_complete` + decisions-needed bullet when all
    # foci exhaust. Auto-advance is opt-out via
    # `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` (kill-switch). Goal.md itself
    # is never mutated — pointer is in-memory state only (goal.md
    # L187-191 "Goal.md auto-rotation" Non-goal).
    try:
        await _maybe_advance_focus(cfg, sdk)
    except Exception as e:  # noqa: BLE001
        # Defensive swallow: the focus-advance pass is best-effort and
        # the daemon's other stages must continue running on a failure
        # here. No dedicated event type because the briefing's
        # event-registry surface is just `focus_advanced` +
        # `roadmap_complete`; an exception in this code path is a bug
        # that surfaces via stderr / debug dumps, not a recurring event
        # the operator should monitor.
        print(
            f"[ap2] _maybe_advance_focus error: {type(e).__name__}: {e}",
            file=sys.stderr,
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
            # TB-226 axis 4: roadmap-complete halt. When the focus
            # pointer has advanced past the last `## Current focus:`
            # heading in goal.md AND the operator hasn't acked the
            # `roadmap_complete` halt for the current foci-list length,
            # block auto-promotion of Backlog tasks. The audit-trail
            # `roadmap_complete` event + decisions-needed bullet were
            # emitted on the tick the exhaustion was first detected
            # (see `_maybe_advance_focus`); the halt persists across
            # ticks until the operator extends the roadmap (adding new
            # foci via `ap2 update-goal`) AND emits
            # `ap2 ack roadmap_complete`. Mirrors TB-223's
            # `_auto_approve_paused` shape: manually-Ready tasks
            # (operator `ap2 approve` already moved them past the
            # Backlog auto-promote gate) still dispatch via
            # `board.next_ready()` above — the halt is targeted at the
            # auto-promote-from-Backlog path only.
            if backlog is not None and goal.roadmap_exhausted(cfg):
                backlog = None
            # TB-232: dry-run on-ramp. When `AP2_AUTO_APPROVE_DRY_RUN=1`
            # is set alongside `AP2_AUTO_APPROVE=1`, the auto-approve
            # gate chain still runs but lives at the proposal-time
            # site (`tools.do_board_edit`'s `add_backlog` branch):
            # instead of stripping `@blocked:review` and emitting
            # `auto_approved`, it emits a `would_auto_approve` audit
            # event and preserves the codespan. That means in dry-run
            # mode the daemon's freeze-threshold / token-cap branches
            # below NEVER fire for the simulated decisions —
            # `_was_auto_approved(cfg, ...)` returns False (no
            # `auto_approved` event in the tail), so the
            # would-have-promoted task simply sits in Backlog awaiting
            # operator-manual `ap2 approve`. Operator observes the
            # `would_auto_approve` event stream + the
            # `would_auto_approve_count_24h` counter
            # (`collect_auto_approve_state`) for ≥24h, then unsets the
            # dry-run knob to engage real dispatch.
            #
            # TB-223: AP2_AUTO_APPROVE cumulative-regression circuit
            # breaker — when `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`
            # consecutive task failures land in `retry_exhausted`, halt
            # auto-promotion of tasks that ideation auto-approved (the
            # `auto_approved` audit event identifies them).
            # Operator-approved tasks (those that went through `ap2
            # approve` with `ideation_approved`) continue to dispatch
            # normally — the freeze is targeted at the auto layer.
            # Unfreeze: operator runs `ap2 ack auto_approve_unfreeze
            # --reason "..."` to reset the failure counter.
            if (
                backlog is not None
                and _was_auto_approved(cfg, backlog.id)
                and _auto_approve_paused(cfg)
            ):
                events.append(
                    cfg.events_file,
                    "auto_approve_paused",
                    task=backlog.id,
                    threshold=_auto_approve_freeze_threshold(),
                    reason=(
                        "consecutive task failures landed in "
                        "retry_exhausted; auto-promote of auto-approved "
                        "tasks halted until operator emits "
                        "`ap2 ack auto_approve_unfreeze`"
                    ),
                )
                backlog = None
            # TB-224: cost + blast-radius guards. Three halt conditions
            # checked against the same auto-promote-paused state,
            # sharing the ack verb `ap2 ack auto_approve_window_resume`:
            #   - `per_task_cap`: an auto-approved task's
            #     `task_run_usage` event reported input+output tokens
            #     exceeding `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`
            #     (catches the single-runaway pattern — one task in
            #     an infinite tool-call loop).
            #   - `window_cap`: cumulative tokens across all
            #     auto-approved tasks in the last 24h exceeded
            #     `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` (catches the
            #     drift pattern — many small wasteful tasks summing
            #     to unbounded spend).
            #   - `task_error`: an `auto_approved` task emitted a
            #     `task_error` event; one occurrence is enough to
            #     halt because infrastructure failures aren't noise
            #     the way `verification_failed` is.
            # Defaults unset on both knobs → no caps applied (current
            # behavior preserved for operators who haven't budgeted).
            # Manual `ap2 approve` continues to dispatch even while
            # halted — the halt is targeted at the auto-approved
            # bucket only, mirroring TB-223's freeze-threshold shape.
            if (
                backlog is not None
                and _was_auto_approved(cfg, backlog.id)
            ):
                violation = _auto_approve_check_violations(cfg)
                if violation is not None:
                    reason, total_used, cap, trigger_task, detail = violation
                    if not _auto_approve_already_halted(cfg):
                        payload: dict = {
                            "task": trigger_task or backlog.id,
                            "reason": reason,
                        }
                        if reason in ("per_task_cap", "window_cap"):
                            payload["used"] = total_used
                            payload["cap"] = cap
                        if reason == "window_cap":
                            # Briefing-explicit payload shape: name
                            # the rolling-window total alongside the
                            # cap so operators don't have to recompute
                            # from `ap2 logs`.
                            payload["window_used"] = total_used
                        if reason == "task_error" and detail:
                            payload["error_excerpt"] = detail
                        events.append(
                            cfg.events_file,
                            "auto_approve_halted",
                            **payload,
                        )
                        if reason == "task_error" and trigger_task:
                            # Surface the infrastructure failure as a
                            # `## Decisions needed from operator`
                            # bullet so `ap2 status` / `ap2 logs` /
                            # the web home page render it without
                            # waiting for the next ideation cron.
                            _append_decisions_needed_bullet(
                                cfg,
                                (
                                    f"Auto-approved task {trigger_task} hit "
                                    f"`task_error` (infrastructure failure): "
                                    f"{detail[:200] if detail else '(no excerpt)'}. "
                                    f"Inspect via `ap2 logs` and resume "
                                    f"auto-promote via `ap2 ack "
                                    f"auto_approve_window_resume` once the "
                                    f"infrastructure issue is resolved."
                                ),
                            )
                    events.append(
                        cfg.events_file,
                        "auto_approve_skipped",
                        task=backlog.id,
                        reason=reason,
                    )
                    backlog = None
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

    # 4. Ideation. Two parallel triggers:
    #
    # (a) Natural empty-board path — `_maybe_ideate` fires when
    #     AP2_IDEATION_DISABLED is unset, Active is empty, Ready+Backlog
    #     count is below AP2_IDEATION_TRIGGER_TASK_COUNT, and the
    #     AP2_IDEATION_COOLDOWN_S cooldown elapsed. Owned by
    #     `ap2.ideation`; see that module for the prompt + override
    #     mechanism.
    #
    # (b) Forced operator trigger (TB-159) — `force_ideate` fires when
    #     `ap2 ideate` was queued and drained on this tick. Bypasses
    #     the disable knob, the cooldown, AND the queue-depth gate (the
    #     Active-task gate is enforced at queue-append time).
    #
    # We run BOTH if both fire on the same tick (rare: requires the
    # operator to queue an `ap2 ideate` on the exact tick where the
    # natural cooldown also unlatches). The forced path runs first so
    # the natural path's `mark_run` doesn't reset the cooldown out from
    # under it; the natural path then no-ops (cooldown is now fresh).
    if drain_res.get("force_ideate"):
        try:
            await ideation.force_ideate(cfg, sdk, mcp_server)
        except Exception as e:  # noqa: BLE001
            events.append(
                cfg.events_file, "ideation_error",
                error=f"{type(e).__name__}: {e}",
            )
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
