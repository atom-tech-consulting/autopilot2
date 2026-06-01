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
from pathlib import Path

from . import (
    diagnose,
    env_reload,
    events,
    ideation,
    ideation_halt,
    prompts,
    retry,
    rollback,
    tools,
    verify,
    web,
)
from .registry import Phase, default_registry
from .board import Board, board_file_lock
from .config import Config, DEFAULT_TASK_MAX_TURNS
from .cron import (
    CronJob,
    bootstrap as bootstrap_cron,
    due_jobs,
    load_jobs,
    load_state,
    mark_run,
)
# TB-312: `check_new_messages` and the Mattermost HTTP client moved to
# `ap2/components/mattermost/` (axis-(5) bundled with axis-(3)
# channel-adapter abstraction). Core looks them up via the registry
# rather than importing the component directly (axis-(6)
# import-direction gate, TB-311). See `_check_inbound_messages` below
# for the lookup site that replaces the pre-TB-312 direct import.
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

    # TB-364 (axis-6 migration): resolve the task-agent backend through the
    # per-kind selector and drive the streaming `AgentAdapter.run(...)` instead
    # of calling `sdk.query` directly. `select_adapter` reads the merged
    # `[agent_backends]` config for the `task` kind (`AP2_AGENT_BACKEND_TASK`
    # env override > `[agent_backends]` table > the all-`claude` default);
    # under the default map the resolved adapter is a `ClaudeCodeAdapter`
    # wrapping the daemon's already-imported `sdk` handle, so options, the
    # per-message logging, and usage/commit extraction are bit-for-bit the
    # pre-migration `sdk.query` dispatch path. An operator can set `task=codex` to
    # route just the task agent to the Codex backend while every other kind
    # stays on Claude.
    from .adapters.claude_code import ClaudeCodeAdapter
    from .adapters.select import select_adapter

    adapter = select_adapter("task", cfg)
    # Wrap the injected `sdk` handle (the daemon's already-imported
    # `claude_agent_sdk` module in production; the FakeSDK stub in the
    # daemon-recovery unit tests) so the Claude path stays hermetic and
    # bit-for-bit. Only the Claude backend carries an injectable handle — a
    # codex-backed `task` kind ignores it. `cfg` is always present in
    # `run_task`, so the cfg-less `ClaudeCodeAdapter()` fallback the scrub
    # canary (`ideation_scrub._resolve_scrub_adapter`) carries isn't needed.
    if sdk is not None and isinstance(adapter, ClaudeCodeAdapter):
        adapter._sdk = sdk

    # TB-355 (axis 3): the task agent's full MCP toolset is already registered
    # through the adapter — `mcp_server` was built by
    # `tools.build_mcp_server(cfg)` → `ClaudeCodeAdapter.build_tool_server(...)`
    # — so handing it back through `AgentTools.mcp_servers` exposes the same
    # toolset regardless of backend. `allowed` / `disallowed` carry the
    # per-site tool policy verbatim.
    agent_tools = AgentTools(
        allowed=TASK_AGENT_TOOLS,
        disallowed=_TASK_DISALLOWED_TOOLS,
        mcp_servers={"autopilot": mcp_server},
    )
    # Backend-neutral options carrying every dispatch knob the pre-migration
    # `ClaudeAgentOptions(...)` call did. `timeout_s` is intentionally left
    # unset: the per-run timeout stays on the outer
    # `asyncio.wait_for(_consume())` below (NOT `options.timeout_s`, which only
    # `run_to_result` honors), so the streaming `run()`'s
    # `TimeoutError -> _infer_result_from_head` recovery is preserved verbatim.
    agent_options = AgentOptions(
        cwd=str(cfg.project_root),
        permission_mode="bypassPermissions",
        max_turns=int(cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS)),
        setting_sources=["project"],
        stderr=_stderr_sink,
        # TB-344: no inline `default=` — the schema
        # (`CORE_CONFIG_SCHEMA["agent_model"]`) is the single source of truth
        # for the `claude-opus-4-7` default.
        model=cfg.get_core_value("agent_model"),
        # TB-356: graceful degradation — resolves the base `agent_effort`
        # stepped down by this task's per-task downshift level (bumped only on
        # the thinking-block-immutability 400 failure class). Level 0 / kill
        # switch set → base effort unchanged. The Claude adapter maps this onto
        # `extra_args={"effort": ...}`.
        effort=_resolve_task_effort(cfg, task.id),
    )

    async def _consume() -> str:
        text = ""
        # TB-364: map each normalized `AgentEvent` back onto the pre-migration
        # per-message handlers. `ev.raw` is the backend-native envelope
        # `_log_message` walked before (report_result / pipeline_task_start
        # capture + the stream/messages debug dumps); `ev.text` is the same
        # `_extract_text(msg)` result that drove the returned final text. The
        # terminal `type="result"` event carries no raw envelope — usage /
        # commit are read from `stream_log` + the `report_result` tool args
        # exactly as before — so it is skipped here.
        async for ev in adapter.run(prompt, agent_tools, agent_options):
            if ev.result is not None:
                continue
            _log_message(ev.raw)
            if ev.text:
                text = ev.text
        return text

    # If consume hits a timeout / opaque SDK crash without HEAD salvage we
    # defer the corresponding `_handle_failure` until AFTER the TB-110
    # violation check below — so a fenced-file mutation made before the
    # crash still gets the rollback + state_violation routing instead of
    # whatever generic failure status the consume branch would otherwise
    # have stamped.
    early_failure_status: str | None = None
    early_failure_extras: dict[str, str] = {}
    # TB-356: set True when the deferred `task_error` failure classifies as
    # the thinking-block-immutability 400. Plumbed into `_handle_failure`
    # below so ONLY that class bumps the per-task effort-downshift level.
    early_failure_thinking_block = False
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
            # TB-356: classify the thinking-block-immutability 400 from the
            # run's tail (the same `last_messages` the event records) and the
            # error string. A match steps effort down on the retry; a miss
            # retries at unchanged effort.
            # TB-361: classify BEFORE emitting `task_error` so the event can
            # carry a `thinking_block_corruption` flag — the auto-approve
            # breaker reads that one flag to EXEMPT this handled class from
            # the `task_error` cost/blast-radius halt (it's a retry-with-
            # downshift, not an infrastructure failure). One classifier,
            # one source of truth for both the retry downshift and the
            # window-pause exemption.
            early_failure_thinking_block = (
                _is_thinking_block_corruption(stream_log)
                or _is_thinking_block_corruption(f"{type(e).__name__}: {e}")
            )
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
                thinking_block_corruption=early_failure_thinking_block,
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
            thinking_block_corruption=early_failure_thinking_block,
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
            # TB-252: emit a `verify_passed` audit event on successful
            # project-wide verify so the doctor's `verify_timeout_audit`
            # has a per-run duration signal to size `AP2_VERIFY_TIMEOUT_S`
            # against. Mirror of `verification_failed` payload shape
            # (task, command, exit_code, duration_s) so events.jsonl
            # carries the same fields for both terminal paths; the
            # difference is only the type discriminator + the
            # `passed=True` invariant. Skipped when the gate is
            # unconfigured (`verify_res is None`).
            if verify_res is not None:
                events.append(
                    cfg.events_file,
                    "verify_passed",
                    task=task.id,
                    command=verify_res.command,
                    exit_code=verify_res.exit_code,
                    duration_s=round(verify_res.duration_s, 2),
                )
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


# ---------------------------------------------------------------------------
# TB-356: graceful-degradation effort step-down for the bundled-CLI
# thinking-block-immutability 400 failure class.
#
# Long, thinking-heavy runs intermittently fail when Claude Code empties a
# prior thinking block's text (keeping its signature) during a context pass,
# then replays it — the API rejects the modified block with a 400 whose body
# names "thinking or redacted_thinking blocks in the latest assistant message
# cannot be modified". ap2 surfaces this as a generic `task_error`. It is
# load-correlated: higher effort makes each thinking block larger, so xhigh
# runs trip it most. There is no upstream fix. The mitigation is fewer/smaller
# thinking blocks → lower effort — but only for THIS failure class (a blind
# same-effort retry just re-trips it; other failures need full capability to
# fix a real problem). So the first attempt runs at full effort and only a
# match on this exact 400 steps the effort down one tier on the retry.
# ---------------------------------------------------------------------------
_EFFORT_LADDER: tuple[str, ...] = ("xhigh", "high", "medium", "low")


def _step_down_effort(base: str, level: int) -> str:
    """Step `base` effort down `level` tiers along the xhigh→high→medium→low
    ladder, floored at `low`.

    Level 0 (or below) returns `base` unchanged. A `base` not on the ladder
    (e.g. an operator-set `max` or `""`) is returned unchanged — there's no
    known safe step-down path for it, so we don't guess.
    """
    if level <= 0:
        return base
    try:
        idx = _EFFORT_LADDER.index(base)
    except ValueError:
        return base
    return _EFFORT_LADDER[min(idx + level, len(_EFFORT_LADDER) - 1)]


def _thinking_block_drop_disabled(cfg: Config) -> bool:
    """True iff the TB-356 effort-downshift kill switch is set truthy.

    Routes through `cfg.get_core_value("thinking_block_effort_drop_disabled")`
    (sectioned env > flat `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED` > TOML >
    schema default `False`), evaluated at call time so a hot-reload toggling
    the knob takes effect on the next dispatch. When True the daemon retries
    at constant effort (pre-TB-356 behavior). Truthy enumeration mirrors the
    sibling kill switches: `1` / `true` / `yes` / `on` (case-insensitive);
    the TOML-typed `True` / `False` is honored directly.
    """
    raw = cfg.get_core_value("thinking_block_effort_drop_disabled", default=False)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _message_text_fragments(item) -> list[str]:
    """Pull the human-readable text fragments out of one stream-log element.

    TB-361: the thinking-block-immutability 400 signature lands in a
    message's *text* (`text_preview` / `result` / a content block's `text`),
    never in the message's structural metadata. This helper extracts those
    fragments from BOTH shapes the classifier may be handed:

      - a per-message *summary dict* — the shape `_summarize_message`
        produces and the `task_error` event records under `last_messages`
        (`{type, text_preview?, result?, tool_results?, content?, ...}`); and
      - a *raw SDK message object* — whose `text` / `result` live on
        attributes (`.content[*].text`, `.result`) that a blind
        `json.dumps(..., default=str)` would drop, emitting a useless
        `<AssistantMessage ...>` repr instead.

    Walking the fragments explicitly (rather than relying on the lossy
    object repr) is the core of TB-361's classifier fix: the same
    last-messages payload now matches whether it arrives as dicts or as
    raw objects.
    """
    frags: list[str] = []
    # Summary-dict shape (what `task_error`'s `last_messages` records).
    if isinstance(item, dict):
        for key in ("text_preview", "result", "text", "error"):
            v = item.get(key)
            if isinstance(v, str):
                frags.append(v)
        for tr in item.get("tool_results") or []:
            if isinstance(tr, dict):
                p = tr.get("preview")
                if isinstance(p, str):
                    frags.append(p)
        for blk in item.get("content") or []:
            if isinstance(blk, dict):
                for key in ("text", "content"):
                    v = blk.get(key)
                    if isinstance(v, str):
                        frags.append(v)
        return frags
    # Raw SDK message object shape — pull `.result` + each content block's
    # `.text` (the same fields `_extract_text` / `_walk_blocks` read).
    result = getattr(item, "result", None)
    if isinstance(result, str):
        frags.append(result)
    content = getattr(item, "content", None)
    if isinstance(content, list):
        for blk in content:
            t = getattr(blk, "text", None)
            if isinstance(t, str):
                frags.append(t)
    return frags


def _failure_text_blob(stream_log_or_error) -> str:
    """Flatten a failed run's tail into a single searchable lower-priority
    text blob for `_is_thinking_block_corruption`.

    Robust to BOTH inputs the classifier is fed (TB-361):
      - a bare error string (the wrapping exception text); and
      - the structured `stream_log` / `last_messages` — a list of
        per-message summary dicts OR raw SDK message objects.

    For a list/tuple we pull each element's text fragments via
    `_message_text_fragments` (the lossless path), THEN append the
    `json.dumps(..., default=str)` dump as a backstop. The dump can only
    ADD matchable text (for an unanticipated dict field), never remove the
    fragment matches — so a raw-object list, whose dump is the useless
    `<AssistantMessage ...>` repr, still matches via the walked fragments.
    """
    if isinstance(stream_log_or_error, str):
        return stream_log_or_error
    items = (
        stream_log_or_error
        if isinstance(stream_log_or_error, (list, tuple))
        else [stream_log_or_error]
    )
    parts: list[str] = []
    for item in items:
        parts.extend(_message_text_fragments(item))
    try:
        parts.append(json.dumps(stream_log_or_error, default=str))
    except (TypeError, ValueError):
        parts.append(str(stream_log_or_error))
    return "\n".join(p for p in parts if p)


def _is_thinking_block_corruption(stream_log_or_error) -> bool:
    """True iff a failed run's tail carries the thinking-block-immutability
    400 signature.

    Narrow by design: the substring ``cannot be modified`` must co-occur with
    one of ``thinking`` / ``redacted_thinking`` / ``blocks in the latest
    assistant message``. This keeps unrelated `task_error`s and real
    `verification_failed`s from matching — only THIS failure class downshifts
    effort (a real failure needs full capability to fix, and dropping effort
    there would hurt).

    TB-361: matches against the message-tail *text* (`text_preview` /
    `result` / content-block `text`) via `_failure_text_blob`, NOT a bare
    `json.dumps(..., default=str)` object repr. Accepts the structured
    `stream_log` as either summary dicts (what the `task_error` event
    records as `last_messages`) OR raw SDK message objects — the prior
    repr-only path matched the dict shape but silently MISSED the
    raw-object shape (the signature hid behind `<AssistantMessage ...>`),
    so the recovery path could go inert depending on what the call site
    passed.
    """
    low = _failure_text_blob(stream_log_or_error).lower()
    if "cannot be modified" not in low:
        return False
    return (
        "thinking" in low
        or "redacted_thinking" in low
        or "blocks in the latest assistant message" in low
    )


def _resolve_task_effort(cfg: Config, task_id: str) -> str:
    """Effort label for the TASK-agent dispatch, applying the TB-356 per-task
    downshift when the thinking-block degradation path is active.

    Base is `agent_effort` (default `xhigh`). Each prior thinking-block-
    corruption failure on this task bumped a per-task downshift level
    (`retry.downshift_level`); the effort is that base stepped down `level`
    tiers (xhigh→high→medium→low, floored at low). Level 0 = base. The kill
    switch `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED` forces constant base
    effort. Control / judge agents resolve their effort elsewhere and are out
    of scope.
    """
    base = cfg.get_core_value("agent_effort", default="xhigh")
    if _thinking_block_drop_disabled(cfg):
        return base
    level = retry.downshift_level(cfg.retry_state_file, task_id)
    return _step_down_effort(base, level)


def _handle_failure(
    cfg: Config,
    task,
    *,
    status: str,
    parsed: TaskResult | None = None,
    debug_paths: dict[str, str] | None = None,
    extras: dict[str, str] | None = None,
    thinking_block_corruption: bool = False,
) -> None:
    """Move a failed task to Backlog, or Frozen if it has exhausted retries.

    TB-114: ALWAYS appends a `## Attempts` entry to the briefing — for
    every failure mode (timeout, error, state_violation, verification_failed,
    incomplete/blocked/failed). The next attempt's agent can `Read` the
    briefing and see the full failure narrative + debug-dump paths to
    pick up where the prior attempt left off.

    TB-356: when `thinking_block_corruption=True` (the caller classified the
    failure as the thinking-block-immutability 400) AND the kill switch is
    unset, bump this task's per-task effort-downshift level so the next
    dispatch (`_resolve_task_effort`) steps effort down one tier, and emit an
    `effort_downshift` observability event. Other failure classes pass the
    default `False` and retry at unchanged effort.
    """
    attempts = retry.bump_attempt(cfg.retry_state_file, task.id)
    if thinking_block_corruption and not _thinking_block_drop_disabled(cfg):
        base = cfg.get_core_value("agent_effort", default="xhigh")
        new_level = retry.bump_downshift(cfg.retry_state_file, task.id)
        events.append(
            cfg.events_file,
            "effort_downshift",
            task=task.id,
            # `from` is a Python keyword — pass the from/to pair via dict
            # unpacking so the event payload still reads naturally.
            **{
                "from": _step_down_effort(base, new_level - 1),
                "to": _step_down_effort(base, new_level),
            },
            reason="thinking_block_corruption",
            level=new_level,
        )
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
        max_turns=int(cfg.get_core_value("control_max_turns", default=15)),
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


def _control_kind_from_label(label: str) -> str:
    """Map a control-agent ``label`` onto its canonical ``AGENT_KINDS``
    selector key (TB-365 / goal.md axis 6).

    The four control surfaces that share ``_run_control_agent`` each pass a
    stable ``label`` that already identifies which surface is dispatching:

      - ``"ideation"``           → ``"ideation"``      (``ideation._run_ideation``)
      - ``"cron-status-report"`` → ``"status_report"`` (``status_report.run_status_report``)
      - ``"MM-<post-id>"``       → ``"mattermost"``    (``daemon.handle_message``)
      - ``"cron-<job>"``         → ``"cron"``          (``daemon.run_cron`` LLM crons)

    Returning the canonical kind lets ``select_adapter(kind, cfg)`` resolve
    each surface's own backend (``AP2_AGENT_BACKEND_<KIND>`` env override >
    ``[agent_backends]`` table > the all-``claude`` default). The
    ``cron-status-report`` check precedes the generic ``cron-`` prefix so the
    status-report surface keeps its own ``status_report`` selector key rather
    than collapsing into ``cron``. An unrecognized label (e.g. a unit-test
    stub) falls back to ``"ideation"``; under the default all-``claude`` map
    every kind resolves to the same ``ClaudeCodeAdapter``, so the fallback is
    behavior-neutral.
    """
    if label == "ideation":
        return "ideation"
    if label == "cron-status-report":
        return "status_report"
    if label.startswith("MM-"):
        return "mattermost"
    if label.startswith("cron-"):
        return "cron"
    return "ideation"


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
    was dumped and the stream was discarded via a bare
    ``async for _ in ...: pass`` consume loop — per-message detail and
    token cost were unrecoverable for ideation, status-report, and MM.
    The label-specific events (``ideation_timeout`` / ``cron_error`` /
    ``mattermost_error`` / etc.) keep firing from the caller; the new
    event is purely additive so ``events.jsonl`` greps for the existing
    vocabulary stay valid.

    TB-365 (axis-6 migration): dispatch now resolves
    ``select_adapter(<kind>, cfg)`` for the specific control surface this
    call is running — ``ideation`` / ``status_report`` / ``cron`` /
    ``mattermost``, derived from ``label`` via ``_control_kind_from_label``
    — and drives the streaming ``adapter.run(...)`` instead of a direct
    ``sdk.query`` consume loop, so each surface is independently
    backend-selectable (``AP2_AGENT_BACKEND_<KIND>`` > ``[agent_backends]``
    table > the all-``claude`` default). Under the default map the resolved
    adapter is a ``ClaudeCodeAdapter`` wrapping the injected ``sdk`` handle,
    so the tool policy, ``max_turns`` / ``model`` / ``effort`` options, the
    per-message logging, and the ``control_run_usage`` emission are
    bit-for-bit the pre-migration path.
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
        else cfg.get_core_value("agent_effort", default="xhigh")
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

    # TB-365 (axis-6 migration): resolve THIS control surface's backend
    # through the per-kind selector and drive the streaming
    # `AgentAdapter.run(...)` instead of a direct `sdk.query` consume loop.
    # `_control_kind_from_label` maps the already-threaded `label` onto the
    # canonical `ideation` / `status_report` / `cron` / `mattermost` selector
    # key, so `select_adapter` reads each surface's own merged backend id
    # (`AP2_AGENT_BACKEND_<KIND>` env override > `[agent_backends]` table >
    # the all-`claude` default). Under the default map the resolved adapter is
    # a `ClaudeCodeAdapter` wrapping the daemon's already-imported `sdk` handle
    # (the option/stream stubs in the control-agent unit tests), so options,
    # the per-message logging, and the `control_run_usage` emission below are
    # bit-for-bit the pre-migration path.
    from .adapters.claude_code import ClaudeCodeAdapter
    from .adapters.select import select_adapter

    adapter = select_adapter(_control_kind_from_label(label), cfg)
    # Only the Claude backend carries an injectable handle — a codex-backed
    # kind ignores it. `cfg` is always present at the `_run_control_agent`
    # call boundary, so the cfg-less `ClaudeCodeAdapter()` fallback the scrub
    # canary (`ideation_scrub._resolve_scrub_adapter`) carries isn't needed.
    if sdk is not None and isinstance(adapter, ClaudeCodeAdapter):
        adapter._sdk = sdk

    # Backend-neutral tool policy + options carrying every dispatch knob the
    # pre-migration `ClaudeAgentOptions(...)` call did. `mcp_servers` exposes
    # ap2's custom MCP toolset; `allowed` carries the per-site tool policy
    # verbatim (control agents pass no `disallowed_tools`). `timeout_s` is
    # left unset: the per-run timeout stays on the outer
    # `asyncio.wait_for(_consume())` below, preserving the
    # `TimeoutError -> control_run_usage(status="timeout")` path verbatim.
    agent_tools = AgentTools(
        allowed=allowed_tools,
        mcp_servers={"autopilot": mcp_server},
    )
    agent_options = AgentOptions(
        cwd=str(cfg.project_root),
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        setting_sources=["project"],
        stderr=stderr_sink,
        # TB-344: schema is the single source of truth for the
        # agent_model default (see CORE_CONFIG_SCHEMA).
        model=cfg.get_core_value("agent_model"),
        effort=resolved_effort,
    )

    async def _consume() -> None:
        # TB-365: map each normalized `AgentEvent` back onto the existing
        # per-message handler. `ev.raw` is the backend-native envelope
        # `_log_message` walked before (stream/messages debug dumps + the
        # `control_run_usage` usage walk over `stream_log`). The terminal
        # `type="result"` event carries no raw envelope — usage is still read
        # from `stream_log` below — so it is skipped.
        async for ev in adapter.run(prompt, agent_tools, agent_options):
            if ev.result is not None:
                continue
            _log_message(ev.raw)

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
        # TB-309: janitor became a registry-discovered component. The
        # registry's manifest pins `tick_hook` to `run_janitor`; the
        # call site here no longer imports the module directly.
        from .registry import default_registry

        janitor_tick_hook = default_registry().hook(
            "tick_hook", component="janitor",
        )

        events.append(cfg.events_file, "cron_start", job=job.name)
        try:
            await janitor_tick_hook(cfg, sdk)
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

    # TB-350: `real-sdk-smoke` jobs run the live-API smoke suite as a
    # timeout-bounded subprocess via `ap2.smoke_runner.run_smoke_check`.
    # Like `janitor`, the work is a deterministic shell action (running
    # pytest), not an LLM task — and control / cron agents have no Bash
    # anyway — so this dispatches a Python routine rather than building a
    # control prompt. The routine itself emits the
    # `smoke_check_skipped` / `smoke_check_passed` / `smoke_check_failed`
    # outcome events + posts the failure-only Mattermost alert; we bookend
    # with `cron_start` / `cron_complete` (job=real-sdk-smoke) and advance
    # `cron_state[real-sdk-smoke].last_run` exactly as the janitor branch
    # does. `job.prompt` is an ignored stub (same as status-report).
    if job.name == "real-sdk-smoke":
        from . import smoke_runner as _smoke_runner_mod

        events.append(cfg.events_file, "cron_start", job=job.name)
        try:
            await _smoke_runner_mod.run_smoke_check(cfg)
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


# TB-263: per-envelope SDK message serialization helpers (TB-85 / TB-157)
# live in `ap2.message_dump`. Re-exported here so existing test paths
# (`daemon._summarize_message`, etc.) and the orchestrator's call sites
# (`run_task` / `_run_control_agent` stream-dump emission) resolve
# through one name.
from .message_dump import (
    _extract_tool_result_payload,
    _serialize_message_full,
    _stringify_block_content,
    _summarize_message,
    _truncate,
    _walk_blocks,
)
from .adapters.base import AgentOptions, AgentTools, usage_from_summary


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


# TB-263: project-wide + per-task verification harness (TB-66 / TB-69)
# lives in `ap2.verify_harness`. Re-exported here so existing call sites
# in `run_task` / `_sweep_pipeline_pending` (via late-binding) resolve
# through one name. The new module owns the regression-gate command
# execution + per-task verifier dispatch.
from .verify_harness import VerifyResult, _maybe_per_task_verify, _run_verify


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
    # Axis 2 (TB-354): build the payload from the one normalized usage record
    # rather than re-indexing the raw SDK-derived summary dict per field. The
    # `usage_from_summary` -> `AgentUsage.event_payload()` path reproduces the
    # exact keys / values (incl. the `stream_incomplete` note on the no-result
    # path) so the emitted event is byte-for-byte unchanged.
    usage = usage_from_summary(last_result)
    payload: dict = {
        "task": task.id,
        "run_id": run_id,
        "status": status,
        "duration_s": round(duration_s, 3),
    }
    payload.update(usage.event_payload())
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
    # Axis 2 (TB-354): same normalized-record relocation as
    # `_emit_task_run_usage` — the payload's usage block is built from
    # `AgentUsage.event_payload()`, leaving the `error` / `stderr_tail`
    # non-success fields appended after it exactly as before.
    usage = usage_from_summary(last_result)
    payload: dict = {
        "label": label,
        "run_id": run_id,
        "status": status,
        "duration_s": round(duration_s, 3),
    }
    payload.update(usage.event_payload())
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


# TB-263: state-file commit machinery (constants + helpers) lives in
# `ap2.state_commit`. Re-exported here so existing test paths
# (`daemon._STATE_FILE_NAMES`, `daemon._commit_state_files`, etc.) and
# the orchestrator's own internal call sites resolve through one name.
from .state_commit import (
    _STATE_DIRS,
    _STATE_FILE_NAMES,
    _changed_state_paths,
    _commit_state_files,
    _filter_state_paths,
    _snapshot_state_paths,
    _task_state_paths,
)


# TB-263: pipeline-pending sweep (TB-178) lives in `ap2.pipeline_sweep`.
# Re-exported here so existing test paths (`daemon._sweep_pipeline_pending`,
# `daemon._pipeline_alive`) and the orchestrator's tick-loop call in
# `_tick` resolve through one name. The sweep late-binds the verify
# harness (`_run_verify` / `_handle_failure` / `_maybe_per_task_verify` /
# `_append_progress`) through daemon so test monkeypatches on those
# helpers continue to take effect across the lift.
from .pipeline_sweep import _pipeline_alive, _sweep_pipeline_pending


# TB-263: daemon-start event + env-mtime capture + per-process daemon-state
# stash (TB-139 / TB-260) live in `ap2.daemon_state`. Re-exported here so
# existing test paths (`daemon._emit_daemon_start`,
# `daemon._capture_env_mtime_at_start`) and main_loop's startup hook
# resolve through one name.
from .daemon_state import (
    _capture_env_mtime_at_start,
    _emit_daemon_start,
    _load_daemon_state,
    _save_daemon_state,
)


def _validate_toml_config_at_start(cfg: Config) -> None:
    """Daemon-start TOML schema gate (TB-321 axis 1).

    Walks `.cc-autopilot/config.toml` (if present) and validates every
    `[components.<name>]` sub-table against the union of all
    manifests' `config_schema` declarations. Raises `SystemExit` on
    schema mismatch — fail-fast shape, no auto-correction (goal.md
    L312-313). The error message names the bad key path so the
    operator can grep their config file directly:

        [components.janitor] disabled = 'yes': expected bool, got str

    Missing `config.toml` is the no-op path — existing installs see
    zero behavior change. The function is intentionally synchronous +
    side-effect-free (apart from the SystemExit raise) so a unit
    test can exercise it directly without spinning up the loop.

    Implementation lives in `ap2.config_loader.validate_config`; this
    is the daemon-side adapter that loads the registry and translates
    a `ConfigSchemaError` into a clean exit (stderr-printed message
    rather than an asyncio traceback).
    """
    from . import config_loader as _config_loader
    from .config import CONFIG_TOML_FILE

    toml_path = cfg.project_root / CONFIG_TOML_FILE
    if not toml_path.exists():
        return
    try:
        raw = _config_loader.parse_toml(toml_path)
        _config_loader.validate_config(raw, default_registry())
    except _config_loader.ConfigSchemaError as exc:
        # Print the message + exit cleanly so the operator sees a
        # one-line error instead of an asyncio traceback. The
        # daemon's bootstrap shell would otherwise just log the
        # stack trace, which is harder to spot in a tail of
        # `daemon_start` / cron events.
        print(
            f"ap2: config schema error in {toml_path}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


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
    # TB-321 (axis 1): if `.cc-autopilot/config.toml` exists, validate
    # its `[components.<name>]` sub-tables against the union of every
    # manifest's `config_schema` BEFORE any tick fires. Fail-fast on
    # schema mismatch — operator-fix-first shape (goal.md L312-313),
    # no auto-correction. The error message names the bad key path so
    # the operator can grep their config file directly. A missing
    # config.toml is the no-op path (existing installs see zero
    # behavior change). `Config.load` has already attempted to parse
    # the file once if present, so a syntax error would have surfaced
    # earlier — this hook is the schema gate.
    _validate_toml_config_at_start(cfg)
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
    if not web.is_web_disabled(cfg=cfg):
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
    start_port = web.daemon_web_port(cfg=cfg)
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
                for msg in _check_inbound_messages(cfg):
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


def _check_inbound_messages(cfg: Config) -> list[dict]:
    """Poll for inbound messages via the registry's `inbound_poll` hook
    (TB-312 axis-(5) migration).

    Pre-TB-312 this was a direct call to
    `from .mattermost import check_new_messages`. The git-move of
    `ap2/mattermost.py` into `ap2/components/mattermost/__init__.py`
    forbids a static import from core (axis-(6) gate, TB-311); this
    helper instead looks up the `inbound_poll` hook on every enabled
    component that declares one. Today only the mattermost component
    does — but the lookup shape is uniform so a future migration
    (e.g. Slack inbound) can join the list by declaring its own
    `inbound_poll` hook on its manifest.

    Returns the concatenation of all enabled adapters' poll results
    in deterministic component-name-sorted order. Empty list means
    "no component is configured to poll for inbound messages" — the
    `_mm_loop` simply sleeps until the next interval.
    """
    out: list[dict] = []
    for manifest in default_registry().enabled_components(cfg):
        poll = manifest.hook_points.get("inbound_poll")
        if poll is None:
            continue
        try:
            msgs = poll(cfg)
        except Exception as e:  # noqa: BLE001
            # Per-adapter poll failures emit a generic `mm_poll_error`
            # event upstream in `_mm_loop`'s outer try/except so the
            # operator sees ONE coherent failure rather than two
            # nested ones; here we just re-raise to that surface.
            raise
        if msgs:
            out.extend(msgs)
    return out


def _deliver(cfg: Config, text: str, **meta) -> list[dict]:
    """Walk `registry.channel_adapters(cfg)` and best-effort `.post()`
    on each (TB-312 axis (3)).

    Replaces the pre-TB-312 direct Mattermost-`_mm_post` call sites
    in `daemon.py` (attention immediate push) and `watchdog.py`
    (auto-diagnose + pending-review reminder).
    Each call site now passes the destination via `meta["channel"]`
    if it has one (legacy `_first_mm_channel` semantics — the
    watchdog and attention paths historically picked the first entry
    from `AP2_MM_CHANNELS`; the Mattermost adapter falls back to
    that env knob when `channel` is unset, preserving observable
    behavior).

    Returns the list of `{adapter, ...}` result dicts from each
    adapter's `.post()`. A `None` return from an adapter means
    "unconfigured" — silently skipped, not added to the result. A
    raise from an adapter is RE-RAISED so the caller's existing
    `*_error` audit-event path (e.g. `attention_push_error`,
    `auto_diagnose_post_error`) still fires per the pre-TB-312
    contract. The caller decides whether one adapter's failure
    aborts further iteration; today's call sites surface the first
    error and continue with the remaining adapters.
    """
    adapters = default_registry().channel_adapters(cfg)
    results: list[dict] = []
    for adapter in adapters:
        outcome = adapter.post(text, **meta)
        if outcome is not None:
            results.append(outcome)
    return results


async def _interruptible_sleep(total_s: int) -> None:
    """Sleep up to `total_s` seconds, breaking promptly on SIGTERM/SIGINT.

    Used by both `_main_tick_loop` and `_mm_loop` so a shutdown signal
    doesn't stall behind the longer tick interval.
    """
    slept = 0
    while slept < total_s and RUNNING:
        await asyncio.sleep(1)
        slept += 1


# TB-310 (axis 2): the registry-walked tick-hook contract replaces the
# pre-TB-310 dotted-relative imports of `auto_approve`, `auto_unfreeze`,
# and the focus-advance module that lived here as re-export blocks.
# Daemon._tick now walks `registry.tick_hooks(phase)` instead of
# importing each module by relative-dotted path; the component
# manifests under `ap2/components/<name>/` register the tick-callable
# hooks the daemon dispatches by phase.
#
# Test back-compat: existing test paths
# (`daemon._maybe_auto_unfreeze`, `daemon._auto_approve_paused`, ...)
# resolve through module-level aliases below. Each alias points at the
# canonical implementation in `ap2.<flat_module>`; tests can still
# write `daemon.<name>` without churn. The aliases live in this single
# block (not scattered across imports) so the test-compat surface is
# obvious at a glance — when axis (5) relocates each flat module into
# its component subpackage, the aliases retarget and tests stay
# unchanged.
# TB-318 (axis 5): auto_approve was relocated from the flat module path
# at `ap2/auto_approve` to the subpackage `ap2/components/auto_approve/`.
# Core must not statically import from `ap2/components/` (TB-311
# import-direction gate), so the module-level aliases below resolve via
# the registry's manifest hook_points at module-load time. Tests that
# monkey-patch `daemon._auto_approve_paused` (or any other alias here)
# still work — the rebind happens once here, and the test's setattr on
# the daemon module overrides this attribute for the duration of the
# test.
_auto_approve_manifest = default_registry().get("auto_approve")
_AUTO_APPROVE_FAILURE_STATUSES = _auto_approve_manifest.hook_points[
    "_AUTO_APPROVE_FAILURE_STATUSES"
]
_AUTO_APPROVE_UNFREEZE_TOKEN = _auto_approve_manifest.hook_points[
    "_AUTO_APPROVE_UNFREEZE_TOKEN"
]
_AUTO_APPROVE_WINDOW_RESUME_TOKEN = _auto_approve_manifest.hook_points[
    "_AUTO_APPROVE_WINDOW_RESUME_TOKEN"
]
_AUTO_APPROVE_WINDOW_S = _auto_approve_manifest.hook_points[
    "_AUTO_APPROVE_WINDOW_S"
]
_append_decisions_needed_bullet = _auto_approve_manifest.hook_points[
    "_append_decisions_needed_bullet"
]
_auto_approve_already_halted = _auto_approve_manifest.hook_points[
    "_auto_approve_already_halted"
]
_auto_approve_check_violations = _auto_approve_manifest.hook_points[
    "_auto_approve_check_violations"
]
_auto_approve_freeze_threshold = _auto_approve_manifest.hook_points[
    "_auto_approve_freeze_threshold"
]
_auto_approve_paused = _auto_approve_manifest.hook_points[
    "_auto_approve_paused"
]
_auto_approve_window_resume_idx = _auto_approve_manifest.hook_points[
    "_auto_approve_window_resume_idx"
]
_auto_approved_task_ids = _auto_approve_manifest.hook_points[
    "_auto_approved_task_ids"
]
_event_combined_tokens = _auto_approve_manifest.hook_points[
    "_event_combined_tokens"
]
_parse_event_ts = _auto_approve_manifest.hook_points["_parse_event_ts"]
_per_task_token_cap = _auto_approve_manifest.hook_points[
    "_per_task_token_cap"
]
_validator_judge_noisy_paused = _auto_approve_manifest.hook_points[
    "_validator_judge_noisy_paused"
]
_was_auto_approved = _auto_approve_manifest.hook_points["_was_auto_approved"]
_window_token_cap = _auto_approve_manifest.hook_points["_window_token_cap"]
evaluate_auto_approve_decision = _auto_approve_manifest.hook_points[
    "evaluate_auto_approve_decision"
]
del _auto_approve_manifest

# TB-314 (axis 5): auto_unfreeze was relocated from the flat module
# `ap2/auto_unfreeze.py` to the subpackage
# `ap2/components/auto_unfreeze/`. Core must not statically import
# from `ap2/components/` (TB-311 import-direction gate), so the
# module-level aliases below resolve via the registry's manifest
# hook_points at module-load time. Tests that monkey-patch
# `daemon._maybe_auto_unfreeze` (or any other alias here) still work
# — the rebind happens once here, and the test's setattr on the
# daemon module overrides this attribute for the duration of the
# test.
_auto_unfreeze_manifest = default_registry().get("auto_unfreeze")
_AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT = _auto_unfreeze_manifest.hook_points[
    "AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT"
]
_AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT = _auto_unfreeze_manifest.hook_points[
    "AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT"
]
_AUTO_UNFREEZE_WINDOW_S = _auto_unfreeze_manifest.hook_points[
    "AUTO_UNFREEZE_WINDOW_S"
]
_apply_auto_unfreeze_patch = _auto_unfreeze_manifest.hook_points[
    "apply_auto_unfreeze_patch"
]
_auto_unfreeze_allowlist = _auto_unfreeze_manifest.hook_points[
    "auto_unfreeze_allowlist"
]
_auto_unfreeze_dry_run = _auto_unfreeze_manifest.hook_points[
    "auto_unfreeze_dry_run"
]
_auto_unfreeze_max_per_day = _auto_unfreeze_manifest.hook_points[
    "auto_unfreeze_max_per_day"
]
_auto_unfreeze_max_per_task = _auto_unfreeze_manifest.hook_points[
    "auto_unfreeze_max_per_task"
]
_count_auto_unfreeze_applied_for_task = _auto_unfreeze_manifest.hook_points[
    "count_auto_unfreeze_applied_for_task"
]
_count_auto_unfreeze_applied_in_window = _auto_unfreeze_manifest.hook_points[
    "count_auto_unfreeze_applied_in_window"
]
_maybe_auto_unfreeze = _auto_unfreeze_manifest.hook_points[
    "maybe_auto_unfreeze"
]
_most_recent_blocked_complete_for = _auto_unfreeze_manifest.hook_points[
    "most_recent_blocked_complete_for"
]
_shared_parse = _auto_unfreeze_manifest.hook_points["shared_parse"]
del _auto_unfreeze_manifest

# TB-345: the focus-advance component was merged into the core module
# `ap2/ideation_halt.py` (ideation-exhaustion halt is core ideation
# lifecycle, not an opt-in component). The daemon imports it directly
# (core→core) and calls `ideation_halt.maybe_halt_on_exhaustion(cfg)`
# from the PRE_DISPATCH phase below — no registry hook_points
# indirection, no module-level alias rebind.


# TB-315 (axis 5): attention was relocated from the flat module
# `ap2/attention.py` to the subpackage `ap2/components/attention/`.
# Core must not statically import from `ap2/components/` (TB-311
# import-direction gate), so the module-level aliases below resolve
# via the registry's manifest hook_points at module-load time. The
# attention surface is wider than focus_advance / auto_unfreeze: the
# alias block spans both the original detector layer
# (`detect_attention_conditions`, `should_suppress`, `AttentionCondition`,
# debounce / approach-pct env helpers) and the daemon-side wire-up
# helpers (`_maybe_emit_attention_events`, `_maybe_push_attention`,
# and the push-state file helpers) which TB-315 also relocated from
# daemon.py into the subpackage so the manifest's tick hook can call
# them body-locally. Tests that monkey-patch
# `daemon._maybe_emit_attention_events` (or any other alias here)
# still work — the rebind happens once here, and the test's setattr
# on the daemon module overrides this attribute for the duration of
# the test.
_attention_manifest = default_registry().get("attention")
AttentionCondition = _attention_manifest.hook_points["AttentionCondition"]
detect_attention_conditions = _attention_manifest.hook_points[
    "detect_attention_conditions"
]
find_last_attention_fire = _attention_manifest.hook_points[
    "find_last_attention_fire"
]
should_suppress = _attention_manifest.hook_points["should_suppress"]
_parse_ts = _attention_manifest.hook_points["parse_ts"]
_task_stuck_threshold_s = _attention_manifest.hook_points[
    "task_stuck_threshold_s"
]
_task_frozen_recency_s = _attention_manifest.hook_points[
    "task_frozen_recency_s"
]
_cost_approach_pct = _attention_manifest.hook_points["cost_approach_pct"]
_attention_debounce_s = _attention_manifest.hook_points[
    "attention_debounce_s"
]
_maybe_emit_attention_events = _attention_manifest.hook_points[
    "maybe_emit_attention_events"
]
_maybe_push_attention = _attention_manifest.hook_points[
    "maybe_push_attention"
]
_attention_push_state_path = _attention_manifest.hook_points[
    "attention_push_state_path"
]
_load_attention_push_state = _attention_manifest.hook_points[
    "load_attention_push_state"
]
_save_attention_push_state = _attention_manifest.hook_points[
    "save_attention_push_state"
]
_is_attention_immediate_push_enabled = _attention_manifest.hook_points[
    "is_attention_immediate_push_enabled"
]
del _attention_manifest


def _auto_promote_gate_halts(cfg: Config, candidate) -> bool:
    """TB-361: True iff the auto-approve gate chain halts dispatch of
    `candidate` this tick (emitting the matching observability event as it
    does today), else False.

    Extracted verbatim (same three checks, same order, same events) from
    `_tick`'s auto-promote step so the promoter can ITERATE dispatchable
    Backlog candidates and skip past a gated head to the first candidate the
    gate does NOT halt — instead of nulling the single `next_dispatchable`
    head and ending the tick. The old shape froze operator-added (`ap2 add`)
    and operator-approved (`ap2 approve`) Backlog work queued BEHIND a gated
    auto-approved task, defeating the daemon-comment invariant that
    operator-originated work "must always drain".

    Only auto-approved tasks (`_was_auto_approved`) are subject to the gate;
    a non-auto-approved candidate is NEVER halted here and dispatches
    normally. Check order is preserved exactly:
      1. TB-272 validator-judge-noisy safety-floor pause (checked first so
         the upstream-check failure outranks the post-hoc halts below);
      2. TB-223 cumulative-regression circuit breaker;
      3. TB-224 cost + blast-radius caps + `task_error` single-event halt.
    Each gated candidate emits its `auto_approve_skipped` /
    `auto_approve_paused` event so the per-task observability the operator
    playbook keys off is unchanged; the `auto_approve_halted` one-shot stays
    deduped via `_auto_approve_already_halted` across candidates within a
    tick (the first emission lands on disk before the next candidate's
    check reads it). Pure except for the events it appends; no board
    mutation (the caller promotes after the loop).
    """
    if not _was_auto_approved(cfg, candidate.id):
        return False
    # 1) TB-272 validator-judge-noisy safety-floor pause.
    noisy = _validator_judge_noisy_paused(cfg)
    if noisy is not None:
        fail_count, timeout_count, threshold = noisy
        events.append(
            cfg.events_file,
            "auto_approve_skipped",
            task=candidate.id,
            reason="validator_judge_noisy",
            fail_count_24h=fail_count,
            timeout_count_24h=timeout_count,
            threshold=threshold,
        )
        return True
    # 2) TB-223 cumulative-regression circuit breaker.
    if _auto_approve_paused(cfg):
        events.append(
            cfg.events_file,
            "auto_approve_paused",
            task=candidate.id,
            threshold=_auto_approve_freeze_threshold(cfg),
            reason=(
                "consecutive task failures landed in "
                "retry_exhausted; auto-promote of auto-approved "
                "tasks halted until operator emits "
                "`ap2 ack auto_approve_unfreeze`"
            ),
        )
        return True
    # 3) TB-224 cost + blast-radius guards (+ task_error single-event halt).
    violation = _auto_approve_check_violations(cfg)
    if violation is not None:
        reason, total_used, cap, trigger_task, detail = violation
        if not _auto_approve_already_halted(cfg):
            payload: dict = {
                "task": trigger_task or candidate.id,
                "reason": reason,
            }
            if reason in ("per_task_cap", "window_cap"):
                payload["used"] = total_used
                payload["cap"] = cap
            if reason == "window_cap":
                payload["window_used"] = total_used
            if reason == "task_error" and detail:
                payload["error_excerpt"] = detail
            events.append(
                cfg.events_file,
                "auto_approve_halted",
                **payload,
            )
            if reason == "task_error" and trigger_task:
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
            task=candidate.id,
            reason=reason,
        )
        return True
    return False


async def _tick(cfg: Config, sdk, mcp_server) -> None:
    # 0a. Hot-reload `.cc-autopilot/env` (TB-271). Runs at the VERY TOP
    # of the tick — before operator-queue drain, auto-unfreeze, focus
    # advance, cron, pipeline sweep, ideation, task dispatch — so every
    # downstream stage on THIS tick reads the refreshed tunable values
    # (timeouts, max-turns, model/effort, auto-approve thresholds,
    # verify gate, tick intervals). mtime-gated: cheap no-op when the
    # env file is unchanged, so the 30s tick rhythm doesn't burn cycles
    # re-parsing a static file. The reload mutates cfg in-place for the
    # tunable Config fields and overwrites os.environ for file-sourced
    # keys (preserving "shell export wins" for keys the file never set).
    # Fixed knobs (web binding, MM channels) still need a restart and
    # are documented in `env_reload.FIXED_KNOBS` — TB-260's
    # stale-warning persists for them so the operator sees the nudge
    # for the changes that hot-reload can't apply.
    try:
        env_reload.maybe_reload_env(cfg)
    except Exception as e:  # noqa: BLE001
        # Defensive: a parse / state-file hiccup must not take the
        # daemon down. Surface as an event so a regression in the
        # reload helper is observable; the rest of the tick continues
        # on whatever cfg state survived the partial reload.
        events.append(
            cfg.events_file,
            "env_reload_error",
            error=f"{type(e).__name__}: {e}",
        )

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

    # 0.5. PRE_DISPATCH tick-hook phase (TB-310 axis 2). Walks the
    # registry-discovered component manifests and dispatches each
    # hook registered on `Phase.PRE_DISPATCH` in deterministic
    # name-sorted order. Today's sole registered hook (preserved
    # bit-for-bit):
    #
    #   - `auto_unfreeze` — TB-225 / TB-233 sweep that auto-applies
    #     agent-diagnosed briefing-shape fixes to Frozen tasks. Queues
    #     `update` + `unfreeze` ops back onto the operator queue;
    #     they drain at the START of the NEXT tick, by design (audit-
    #     trail symmetry with TB-153 operator-applied edits). No-op
    #     when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset.
    #
    # The hook fires BEFORE cron / pipeline / dispatch / ideation so
    # its effects (drain-ready unfreeze ops) are visible to every
    # later stage on this tick. Per-hook error handling lives inside
    # the manifest's wrapper function
    # (`ap2/components/auto_unfreeze/manifest.py`): the
    # `auto_unfreeze_skipped reason=sweep_error` event. The walk has
    # no outer try/except so the wrapper's observable-error shape is
    # not swallowed by a uniform handler.
    for hook in default_registry().tick_hooks(Phase.PRE_DISPATCH):
        result = hook(cfg, sdk)
        if asyncio.iscoroutine(result):
            await result

    # 0.6. Ideation-exhaustion halt (TB-345 — merged from the old
    # `focus_advance` PRE_DISPATCH component hook into core ideation
    # lifecycle). Counts consecutive empty ideation cycles since the
    # last `goal_updated` and, at the `AP2_IDEATION_HALT_EMPTY_CYCLES`
    # threshold, emits `roadmap_complete` once to park the ideation
    # trigger (or, when `AP2_IDEATION_HALT_DISABLED` is set, surfaces a
    # decisions-needed bullet for manual halt). Called directly here at
    # the same point the `focus_advance` hook previously occupied —
    # after the auto_unfreeze sweep, before cron — so the freshly-set
    # pointer / `roadmap_complete` event is visible to every later
    # stage on this tick. Self-handles its own error surface; the bare
    # try/except mirrors the pre-TB-345 component wrapper's stderr line.
    try:
        ideation_halt.maybe_halt_on_exhaustion(cfg)
    except Exception as e:  # noqa: BLE001
        print(
            f"[ap2] maybe_halt_on_exhaustion error: {type(e).__name__}: {e}",
            file=sys.stderr,
        )

    # 0.7. ATTENTION_EMISSION tick-hook phase (TB-310 axis 2). Walks
    # registry hooks registered on `Phase.ATTENTION_EMISSION`. Today's
    # single registered hook:
    #
    #   - `attention` — TB-282 proactive `attention_raised` detector
    #     sweep. Runs the detector module, debounces each candidate
    #     against the most recent matching `attention_raised` event
    #     within `AP2_ATTENTION_DEBOUNCE_S` (default 6h, per-(type,
    #     key)), and emits one event per fresh condition. Optional
    #     immediate-Mattermost-push piggybacks on the same debounce
    #     when `AP2_ATTENTION_IMMEDIATE_PUSH` is set.
    #
    # The phase fires AFTER the ideation-halt check (step 0.6) and
    # BEFORE cron (step 1) so a status-report cron firing on this same tick sees
    # freshly-emitted `attention_raised` events both in its prompt
    # tail AND in the interesting-types skip-gate (the event type is
    # listed in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` so a
    # fresh fire un-skips the dedup/idle gate). Per-hook error
    # handling lives inside the manifest wrapper
    # (`ap2/components/attention/manifest.py`): the
    # `[ap2] _maybe_emit_attention_events error: ...` stderr line.
    for hook in default_registry().tick_hooks(Phase.ATTENTION_EMISSION):
        result = hook(cfg, sdk)
        if asyncio.iscoroutine(result):
            await result

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
            # TB-275: roadmap_complete is an IDEATION-trigger gate
            # only (see `_maybe_ideate` in `ap2/ideation.py` — when
            # `goal.roadmap_exhausted(cfg)` is True, ideation parks
            # itself and emits `ideation_skipped reason=roadmap_complete`).
            # It is NOT a dispatch gate. The TB-226 dispatch halt that
            # used to live here froze operator-added (`ap2 add`) and
            # operator-approved (`ap2 approve`) Backlog tasks that have
            # nothing to do with the roadmap, manufacturing an
            # intervention for work the operator had already greenlit
            # (this bit live on 2026-05-20 with TB-273/TB-274 frozen
            # for hours behind the halt). Once ideation is gated, no
            # new speculative work can enter the Backlog anyway — so
            # everything queued is operator-originated or already-
            # proposed and must always drain. A genuine full-stop is
            # `ap2 pause`, a separate explicit mechanism.
            # TB-232: dry-run on-ramp. The auto-approve gate chain
            # (tags / freeze-threshold / per-task-token-cap /
            # window-token-cap) lives behind a single entry point —
            # `evaluate_auto_approve_decision(cfg, tags=...)` above —
            # called from `tools.do_board_edit`'s `add_backlog` branch
            # at proposal time. The TB-223 `_was_auto_approved` + TB-224
            # `_auto_approve_check_violations` + TB-272 noisy-pause gates
            # (now all inside `_auto_promote_gate_halts`) remain a
            # defense-in-depth check at promote time even though
            # `evaluate_auto_approve_decision` already consulted them at
            # proposal time — the events tail can shift between the two
            # phases (a long-running task may have emitted a
            # `task_run_usage` after the auto_approved row was added but
            # before the promote tick).
            #
            # TB-361: SKIP PAST a gated head. The gate chain
            # (validator-judge-noisy → freeze-threshold →
            # cost/blast-radius + task_error, in `_auto_promote_gate_halts`)
            # only HALTS the auto-approved layer; operator-added /
            # operator-approved / not-auto-approved tasks were always meant
            # to drain past it (see the TB-275 "must always drain" note
            # above). The pre-TB-361 shape inspected ONLY the single
            # `next_dispatchable` head and, when the gate halted it, nulled
            # the candidate and ended the tick — so a gated auto-approved
            # head FROZE every non-gated task queued behind it (observed
            # 2026-05-31: human-authored TB-361 itself couldn't promote with
            # a free Active slot because gated TB-359/TB-360 sat ahead). The
            # fix iterates dispatchable Backlog candidates in board order
            # and promotes the FIRST one the gate does NOT halt. Invariants
            # preserved: at most ONE promotion per tick (break on first
            # non-halted); bounded iteration (the dispatchable-Backlog
            # length); auto-approved tasks stay held while paused (only
            # NON-halted candidates dispatch); each gated candidate still
            # emits its `auto_approve_skipped` / `auto_approve_paused`
            # observability event (inside the helper). `iter_dispatchable`
            # keeps `next_dispatchable`'s ordering rule and unmet-`blocked
            # on:` skipping — we only step past the GATED candidates now,
            # never re-sort the queue.
            backlog = None
            for candidate in board.iter_dispatchable("Backlog"):
                if _auto_promote_gate_halts(cfg, candidate):
                    continue
                backlog = candidate
                break
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

    # 3.5. POST_DISPATCH tick-hook phase (TB-310 axis 2). Walks
    # registry hooks registered on `Phase.POST_DISPATCH`. The
    # auto_approve component registers a no-op placeholder here
    # because its gate logic remains inline in the dispatch block
    # above (the gates evaluate per-task state and emit per-task
    # events — extracting them into a single tick-callable belongs
    # to axis (5)). The walk-every-phase uniformity matters: when
    # axis (5) replaces the stub with the real gate-application
    # function, the daemon-side walk does not change. Per-hook
    # error handling lives inside each manifest wrapper.
    for hook in default_registry().tick_hooks(Phase.POST_DISPATCH):
        result = hook(cfg, sdk)
        if asyncio.iscoroutine(result):
            await result

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


# TB-263: idle watchdog (TB-71) lives in `ap2.watchdog`. Re-exported
# here so existing test paths (`daemon._maybe_auto_diagnose`,
# `daemon._first_mm_channel`, `daemon._load_diagnose_state`,
# `daemon._save_diagnose_state`) and the orchestrator's tick-loop call
# in `_tick` resolve through one name.
from .watchdog import (
    _first_mm_channel,
    _load_diagnose_state,
    _maybe_auto_diagnose,
    _save_diagnose_state,
)

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
