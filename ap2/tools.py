"""Custom SDK MCP tools for control agents.

The mattermost handler and cron agents call these to mutate the board, the cron
registry, and send Mattermost replies. Task agents do NOT get these tools — they
just code, commit, and exit.

Tools close over a Config so the daemon can wire paths at startup without the
agent having to know them.

TB-262 split: the briefing-structure validators, validator-judge LLM call,
operator-queue / board-edit handlers, and per-proposal record helpers
moved to dedicated sibling modules — `briefing_validators.py`,
`validator_judge.py`, `operator_queue.py`, `board_edits.py`. This file
keeps the MCP-dispatch boundary (`build_mcp_server`), the per-tool
handlers that didn't fit elsewhere (`do_cron_propose`,
`do_task_complete`, `do_pipeline_task_start`, `do_cron_edit`,
`do_git_log_grep`, `do_log_event`, `do_ideation_state_write`,
`do_status_report_run`, `do_daemon_control`, `do_mattermost_*`), the
agent toolset / fenced-paths constants, and the small MCP-response /
contextvar plumbing that every sibling module reuses (`_ok`, `_err`,
`slugify`, `_task_id_ctx`).

Backward compat: every symbol moved by TB-262 is re-exported from this
module by the import block below — `from ap2.tools import do_board_edit`
still works for the existing test suite, `daemon.py`, `cli.py`, etc.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import events
from .config import Config
from .cron import update_job


# TB-123: contextvar plumb so `do_cron_propose` can stamp the calling task's
# TB-id onto the `cron_proposed` event without forcing the agent to pass its
# own id through the tool args. `daemon.run_task` sets this before awaiting
# `sdk.query(...)` and resets it on exit. The MCP tool handlers run in the
# same asyncio task as run_task, so the value is visible during dispatch.
# Tests that call `do_cron_propose` directly (no daemon) see the default ""
# and the event simply omits `proposed_by_task` — that's fine for the unit
# shape; the e2e test exercises the daemon-set path.
_task_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ap2_task_id", default="",
)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def _ok(text: str, **fields: Any) -> dict:
    body = {"message": text}
    body.update(fields)
    return {
        "content": [{"type": "text", "text": json.dumps(body)}],
    }


def _err(text: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
        "isError": True,
    }


# ---------------- TB-262 re-exports (sibling modules) ----------------
#
# These imports come AFTER `_ok`, `_err`, `slugify`, `_task_id_ctx` so
# the sibling modules can resolve their `from .tools import _ok, _err,
# slugify` lines against this module's partial-load state. The names
# below are the public surface (or test-touched private surface)
# pre-TB-262 callers expected to find on `ap2.tools`.
from .briefing_validators import (  # noqa: E402  re-export below `_ok`/`_err` definitions
    IDEATION_PROPOSALS_DIR,
    IMPACT_VERDICTS,
    SINGLE_LINE_ERR,
    TITLE_NO_ASTERISK_ERR,
    _ANCHOR_NORMALIZE_RE,
    _atomic_write_json,
    _blocked_on_has_review,
    _BRIEFING_SECTION_RE,
    _BRIEFING_STRUCTURE_HINT,
    _briefing_section_body,
    _briefing_section_names,
    _BULLET_LINE_RE,
    _bullet_anchor_phrase,
    _goal_md_anchors,
    _goal_md_anchors_from_text,
    _GOAL_HEADING_RE,
    _MANUAL_BULLET_RE,
    _normalize_anchor,
    _PROPOSAL_DECISION_KINDS,
    _validate_briefing_structure,
    _validate_single_line,
    _validate_update_args,
    _why_now_paragraph,
    _WHY_NOW_MARKER_RE,
    extract_goal_anchor,
    extract_why_now,
    ideation_proposals_dir,
    proposal_record_path,
    reconcile_proposal_outcome,
    write_ideation_proposal_record,
)
# TB-316: the validator_judge LLM dep-coherence surface ships as a
# component subpackage (`ap2/components/validator_judge/`); core resolves
# the symbols `tools.py` historically re-exported through the registry's
# `hook_points` dict rather than via a static `from .validator_judge
# import …`. The TB-311 import-direction gate forbids the latter (a core
# module statically importing from `ap2/components/` is a build-failure
# leak). The pre-TB-316 attribute names (`_DEP_JUDGE_PARSE_ERRORS`,
# `_DepJudgeOutcome`, …) are preserved verbatim on the `tools` module
# so `from ap2.tools import _DepJudgeTimeout` etc. keeps working in the
# >50 test modules that touch this surface.
#
# Resolution is via module-level `__getattr__` (PEP 562) rather than
# eager assignment at import time. The eager-assignment path triggered a
# circular-import bug: `auto_unfreeze/__init__.py` does `from ap2 import
# tools`, and at that point `tools` is mid-load; the eager registry
# call here would recursively kick off another `default_registry()`
# pass which then tries to re-import `auto_unfreeze/__init__.py` —
# which is still partially initialized — and the inner `from . import
# (...)` block fails with `ImportError: cannot import name ... from
# partially initialized module`. The lazy `__getattr__` form binds the
# registry resolution to first attribute access, which by that point
# is post-import for both `tools` and every component subpackage.
_VJ_SYMBOL_MAP: "dict[str, str]" = {
    "_DEP_JUDGE_PARSE_ERRORS": "DEP_JUDGE_PARSE_ERRORS",
    "_DepJudgeOutcome": "DepJudgeOutcome",
    "_DepJudgeTimeout": "DepJudgeTimeout",
    "_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL": (
        "VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL"
    ),
    "_VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED": (
        "VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED"
    ),
    "_VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT": (
        "VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT"
    ),
    "_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT": "VALIDATOR_JUDGE_MAX_TURNS_DEFAULT",
    "_VALIDATOR_JUDGE_MODEL": "VALIDATOR_JUDGE_MODEL",
    "_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT": "VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT",
    "_check_dependency_coherence": "check_dependency_coherence",
    "_judge_dep_coherence_default": "judge_dep_coherence_default",
    "_parse_dep_judge_response": "parse_dep_judge_response",
}


def __getattr__(name: str):
    """PEP 562 module-level attribute hook for the TB-316 backward-
    compatibility re-exports of the validator_judge component's
    `hook_points`.

    Triggered on `tools.<name>` (or `from ap2.tools import <name>`)
    when the import-time module scope doesn't already carry that
    attribute. We resolve the value from the registry's manifest
    hook_points dict and return it; we do NOT cache on the tools
    module's `__dict__` because monkeypatched stubs (a few tests
    override `tools._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()`
    or similar) need to see the live container the component module
    publishes — caching here would freeze a snapshot.
    """
    if name in _VJ_SYMBOL_MAP:
        from .registry import default_registry

        return default_registry().get(
            "validator_judge",
        ).hook_points[_VJ_SYMBOL_MAP[name]]
    raise AttributeError(f"module 'ap2.tools' has no attribute {name!r}")
from .operator_queue import (  # noqa: E402
    OPERATOR_QUEUE_OPS,
    _APPROVE_LEGACY_REVIEW_RE,
    _allocate_id,
    _append_operator_audit_line,
    _apply_operator_ack,
    _apply_operator_op,
    _approve_review_token,
    _compact_operator_queue,
    _load_operator_queue_applied,
    _max_preallocated_id_in_queue,
    _save_operator_queue_applied,
    classifications_last_30d_by_verdict,
    do_operator_queue_append,
    drain_operator_queue,
    enqueue_operator_ack,
    operator_queue_path,
    operator_queue_pending_count,
    operator_queue_state_path,
)
from .board_edits import do_board_edit  # noqa: E402


# ---------------- MCP tool implementations (this module's own) ----------------


def do_pipeline_task_start(cfg: Config, args: dict) -> dict:
    """Launch a long-running pipeline as a detached OS subprocess (TB-114).

    Spawns the command and writes a `pipeline_start` event with name + pid +
    started_at + command + log path. Returns immediately. The daemon
    correlates the spawned pid back to the launching task by walking the
    SDK message stream during `_consume` (see `daemon.run_task` — captures
    `pipeline_task_start` tool calls). After the launch agent emits
    `report_result(status="complete", ...)`, the daemon moves the task to
    the `Pipeline Pending` board section. Each tick, the Pipeline-Pending
    sweep checks every pid's liveness; once all of a task's pipelines have
    died, the daemon runs the original briefing's `## Verification` against
    the now-populated working tree, routing to Complete (pass) or Backlog
    (fail) via `_handle_failure`.

    Pre-TB-114 history: previously took `validation_title` /
    `validation_briefing` and created a separate Backlog validation task
    blocked on `pid:<N>@<TS>`. That two-task pattern was retired — the
    launch task now carries verification itself.
    """
    name = (args.get("name") or "").strip()
    command = (args.get("command") or "").strip()
    if not name or not command:
        return _err("name and command are required")

    log_dir = cfg.project_root / ".cc-autopilot" / "pipelines"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_tmp = log_dir / f"{name}.log.tmp"
    log_handle = log_tmp.open("a")
    try:
        # `start_new_session=True` puts the child in its own session/process
        # group so a parent (daemon) exit doesn't take it down.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cfg.project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    try:
        import psutil

        started_at = int(psutil.Process(proc.pid).create_time())
    except Exception:  # noqa: BLE001
        # Process may have died instantly, or psutil isn't importable. Fall
        # back to wall clock so we still record SOMETHING. PID recycling
        # detection downstream relies on the (pid, started_at) pair.
        started_at = int(time.time())

    log_path = log_dir / f"{name}-{proc.pid}.log"
    try:
        log_tmp.rename(log_path)
    except OSError:
        log_path = log_tmp

    events.append(
        cfg.events_file,
        "pipeline_start",
        name=name,
        pid=proc.pid,
        started_at=started_at,
        command=command,
        log=str(log_path),
    )
    return _ok(
        f"pipeline {name!r} started (pid {proc.pid})",
        pid=proc.pid,
        started_at=started_at,
        log=str(log_path),
    )


def do_cron_edit(cfg: Config, args: dict) -> dict:
    action = args.get("action", "")
    name = args.get("name")
    if not name:
        return _err("name is required")
    try:
        msg, jobs = update_job(
            cfg.cron_file,
            action,
            name=name,
            interval=args.get("interval"),
            prompt=args.get("prompt"),
            active_when=args.get("active_when"),
            max_turns=args.get("max_turns"),
        )
        return _ok(msg, jobs=[j.name for j in jobs])
    except (KeyError, ValueError) as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def do_task_complete(cfg: Config, args: dict) -> dict:
    """Acknowledge a `task_complete` tool call from a task agent (TB-101).

    The structured payload (status / commit / summary / files_changed /
    tests_passed) is captured by `daemon.run_task` walking the SDK
    message stream — this handler exists only to give the SDK a valid
    response so the agent doesn't loop or treat the call as failed. No
    state mutation here; the daemon owns the routing decision after the
    query returns.

    TB-123: cron-proposal moved off `report_result` and into a dedicated
    `cron_propose` MCP tool — the `cron` arg is no longer part of the
    schema. Pre-existing `cron_proposed` event semantics are preserved
    via `do_cron_propose`.

    Replaces the `RESULT:\\n status: ...` free-text contract that
    `result.py` parsed via regex.
    """
    status = args.get("status", "")
    if not isinstance(status, str) or not status.strip():
        return _err("status is required")
    return _ok(f"task_complete acknowledged (status={status})")


def do_cron_propose(cfg: Config, args: dict) -> dict:
    """Propose a recurring cron job for operator review (TB-123).

    Task agents call this to surface "while doing X I noticed Y should
    fire on a schedule" without mutating `cron.yaml` directly. Pre-TB-123
    this lived as a JSON-stringified `cron=` field on `report_result`;
    the dedicated tool gets:
      - structured args (`name` / `schedule` / `prompt` / `rationale`),
        no in-string JSON escaping,
      - per-proposal `cron_proposed` events with rationale (the operator
        review surface — `ap2 cron list` etc. — is what makes them live),
      - failure isolation: a malformed call doesn't take down the
        result-reporting path.

    Pre-TB-146, control agents (cron / ideation) had `cron_edit` for
    direct mutation; that surface was retired (no agent has `cron_edit`
    anymore — operator-CLI-only via `ap2 cron edit`). Task agents
    continue to use this proposal layer; the operator promotes via
    review.

    Args:
      name: short stable identifier, e.g. "weekly-perf-snapshot"
      schedule: interval string ("1h" / "1d" / "30m") — same vocabulary
        cron.yaml accepts; not parsed/validated here, just recorded for
        the operator's read.
      prompt: the prompt body the cron job will use when fired.
      rationale: one short sentence on why this should fire on a
        schedule. Becomes part of the audit trail.

    Emits `cron_proposed` event with all four fields plus
    `proposed_by_task` (taken from the daemon-set contextvar). Does NOT
    mutate `cron.yaml` — the operator review layer handles promotion.
    """
    name = (args.get("name") or "").strip()
    schedule = (args.get("schedule") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    rationale = (args.get("rationale") or "").strip()

    missing = [
        label
        for label, value in (
            ("name", name), ("schedule", schedule),
            ("prompt", prompt), ("rationale", rationale),
        )
        if not value
    ]
    if missing:
        return _err(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required"
        )

    # `proposed_by_task` is sourced from the daemon's contextvar plumb. If
    # not set (unit tests that bypass the daemon, or a control-agent
    # context), `task_id` is "" and the field is omitted.
    task_id = _task_id_ctx.get()
    payload: dict = {
        "name": name,
        "schedule": schedule,
        "prompt": prompt,
        "rationale": rationale,
    }
    if task_id:
        payload["proposed_by_task"] = task_id
    events.append(cfg.events_file, "cron_proposed", **payload)
    return _ok(
        f"proposed cron job {name!r} ({schedule}) for review",
        name=name,
        schedule=schedule,
    )


def do_git_log_grep(cfg: Config, args: dict) -> dict:
    """Search the project's git log for commits whose message matches `query`.

    Replaces the ad-hoc `Bash("git log --grep=...")` that ideation Step
    1.5 used to call (TB-109). Narrow MCP tool means control agents
    don't need shell access for this — `Bash` was the only legitimate
    dependency in CONTROL_AGENT_TOOLS, and dropping it closes the
    shell-redirect-into-fenced-file corruption surface (TB-108 case).

    Returns one line per match: `<short-sha> <subject>`. Capped at 100.
    Subprocess runs git with arg-list (no `shell=True`), so the query
    is shell-safe — it's a single argument to `--grep`, not interpolated.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    try:
        max_results = int(args.get("max_results") or 20)
    except (TypeError, ValueError):
        max_results = 20
    max_results = max(1, min(max_results, 100))

    if not (cfg.project_root / ".git").exists():
        return _ok("not a git repo", matches=[], count=0)

    try:
        proc = subprocess.run(
            [
                "git",
                "-c", "safe.directory=*",
                "-C", str(cfg.project_root),
                "log",
                "--grep", query,
                "--oneline",
                "-n", str(max_results),
            ],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _err("git log timed out after 10s")
    except FileNotFoundError:
        return _err("git not on PATH")
    if proc.returncode != 0:
        return _err(f"git log failed: {proc.stderr.strip()[-300:]}")

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return _ok(
        f"{len(lines)} commit(s) matched {query!r}",
        matches=lines,
        count=len(lines),
    )


def do_ideation_state_write(cfg: Config, args: dict) -> dict:
    """Overwrite `.cc-autopilot/ideation_state.md` with a fresh assessment (TB-90).

    Called by the ideation cron in Step 0 to land the per-cycle progress
    assessment introduced by TB-87. The content is written verbatim — schema
    correctness is the prompt's responsibility, not the tool's. Atomic write
    (tmpfile + rename) so a concurrent reader can't observe a partial file.

    Reads stay through the existing `Read` tool — this tool only wraps the
    write path. Same pattern as `board_edit` / `cron_edit`: broad reads,
    narrow writes.
    """
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return _err("content is required")
    # Soft cap to surface runaway prompts. The TB-87 schema aims for ~200
    # lines (~10-20KB); 50KB leaves headroom for legitimate verbose
    # assessments without letting the file grow unbounded.
    if len(content) > 50_000:
        return _err(
            f"content too long ({len(content)} bytes); aim for <50KB. "
            "Trim to highest-signal items per the prompt's length cap."
        )
    target = (
        cfg.project_root
        / ".cc-autopilot"
        / "ideation_state.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(target)
    events.append(
        cfg.events_file,
        "ideation_state_updated",
        bytes=len(content),
    )
    return _ok(
        f"wrote {len(content)} bytes to ideation_state.md",
        bytes=len(content),
    )


def do_log_event(cfg: Config, args: dict) -> dict:
    typ = args.get("type") or "info"
    summary = args.get("summary") or ""
    evt = events.append(cfg.events_file, typ, summary=summary)
    return _ok(f"logged {typ}", event=evt)


async def do_status_report_run(cfg: Config, args: dict) -> dict:
    """Trigger an on-demand status report (TB-144).

    Routes the operator's "@claude-bot status" through the same shared
    `ap2.status_report.run_status_report` callable the cron tick uses, so
    chat-triggered reports get the same prompt body, freshness contract,
    and skip-if-idle gate as scheduled ones — and so the audit trail in
    events.jsonl shows `cron_start` / `cron_complete` (with
    `trigger="chat"`) the same way cron-triggered runs do.

    Pre-TB-144 the MM handler composed status-shaped replies inline; the
    format drifted from the canonical cron report and the audit shape
    diverged (no cron_start/complete events landed for chat triggers).
    Routing through the shared routine eliminates both gaps.

    Behavior:
      - If the daemon is paused, returns an error rather than running.
        Mirrors cron semantics — paused daemons skip due jobs; chat
        triggers should not bypass that signal.
      - If the skip-gate fires (no activity since the last report),
        returns a `_ok` summary noting the skip — the operator sees
        "no new activity since <ts>" instead of a duplicate report.
      - On a real run, the routine emits the `cron_start` /
        `cron_complete` events; this handler returns a one-line summary
        carrying the run's outcome so the handler agent can mention it
        in its mattermost_reply.
      - The chat path explicitly does NOT advance
        `cron_state[status-report].last_run` (an operator-triggered
        report at 11:00 must not silence the scheduled noon cron).

    Async because the underlying routine is async (it dispatches a
    sub-agent via `await sdk.query(...)`). Tests drive it through
    `asyncio.run(tools.do_status_report_run(cfg, args))`; the MCP tool
    adapter in `build_mcp_server` just awaits it.
    """
    # Lazy import to keep tools.py independent of the status_report ↔
    # daemon import chain at module load.
    from . import status_report as _sr

    reason = (args.get("reason") or "").strip()
    if not reason:
        return _err(
            "reason is required (one short sentence — what triggered "
            "this on-demand report; lands in events.jsonl for audit)"
        )

    if cfg.pause_flag.exists():
        return _err(
            "daemon is paused; on-demand status reports are deferred "
            "until the operator resumes (mirrors cron semantics — "
            "paused daemons skip due jobs)"
        )

    try:
        sdk, mcp_server = _sr._resolved_sdk_refs()
    except RuntimeError as e:
        return _err(str(e))

    result = await _sr.run_status_report(
        cfg, sdk, mcp_server, trigger="chat", reason=reason,
    )
    if result.skipped:
        return _ok(
            "status_report_run skipped (no activity since last report)",
            skipped=True,
            reason=result.reason or "",
            trigger="chat",
        )
    if result.timed_out:
        return _ok(
            "status_report_run timed out (event audit trail intact)",
            skipped=False,
            timed_out=True,
            trigger="chat",
        )
    if result.error:
        return _ok(
            f"status_report_run errored: {result.error}",
            skipped=False,
            error=result.error,
            trigger="chat",
        )
    return _ok(
        "status_report_run dispatched; cron_complete event emitted",
        skipped=False,
        trigger="chat",
    )


def do_daemon_control(cfg: Config, args: dict) -> dict:
    action = args.get("action")
    reason = args.get("reason") or ""
    if action == "pause":
        cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
        cfg.pause_flag.write_text(reason + "\n")
        events.append(cfg.events_file, "daemon_pause", reason=reason)
        return _ok("daemon paused")
    if action == "resume":
        if cfg.pause_flag.exists():
            cfg.pause_flag.unlink()
        events.append(cfg.events_file, "daemon_resume", reason=reason)
        return _ok("daemon resumed")
    return _err(f"unknown action {action!r}")


# ---------------- TB-312: mattermost handlers moved to component ----------------
#
# `do_mattermost_reply`, `do_mattermost_thread_read`, `_mm_post`,
# `_mm_lookup_channel`, `_mm_user_team`, and the `_TEAM_CACHE`
# module-level cache all moved to `ap2/components/mattermost/__init__.py`
# in TB-312 (goal.md L184-186 bundles the HTTP client, env knobs, and
# `mattermost_reply` MCP tool together). The MCP server in
# `build_mcp_server` below looks the handlers up via the registry's
# `hook_points["mcp_tool_reply"]` / `["mcp_tool_thread_read"]` slots
# rather than statically importing the component (axis-(6)
# import-direction gate, TB-311).
#
# `_mm_post` survives as a thin shim that defers to the component's
# live implementation via `importlib.import_module` (a dynamic import
# — the import-direction gate's AST walk does not flag `Call` nodes
# referencing the components package as a string). The shim exists so
# pre-TB-312 tests that monkeypatched `tools._mm_post` (e.g.
# `test_tb297_attention_immediate_push.py`,
# `e2e/test_auto_diagnose.py`) keep working: the
# `MattermostChannelAdapter.post()` body calls `tools._mm_post(...)`
# via a late `from ap2 import tools` so the monkeypatched callable is
# what actually runs. Tests authored post-TB-312 should prefer
# monkeypatching the component's `_mm_post` directly (or, cleaner,
# stubbing a `MattermostChannelAdapter` subclass), but the shim keeps
# the existing surface stable.


def _mm_post(channel: str, text: str, thread_id: str = "") -> str:
    """Backwards-compat shim — see TB-312 module-level note.

    Resolves `ap2.components.mattermost._mm_post` via `importlib`
    every call so a registry rebuild (e.g. `_reset_default_registry()`
    between tests) or a hot-swapped component implementation is
    picked up.
    """
    import importlib
    mod = importlib.import_module("ap2.components.mattermost")
    return mod._mm_post(channel, text, thread_id)


def do_mattermost_reply(cfg: Config, args: dict) -> dict:
    """Backwards-compat shim for the MCP-tool handler that moved to
    `ap2/components/mattermost/__init__.py` in TB-312. Tests that
    invoke the handler directly (e.g. `e2e/test_tb149_mm_thread_read.py`)
    keep working; the MCP server itself looks the handler up via the
    registry in `build_mcp_server` below — not via this shim.
    """
    import importlib
    mod = importlib.import_module("ap2.components.mattermost")
    return mod.do_mattermost_reply(cfg, args)


def do_mattermost_thread_read(cfg: Config, args: dict) -> dict:
    """Backwards-compat shim for the MCP-tool handler that moved to
    `ap2/components/mattermost/__init__.py` in TB-312.
    """
    import importlib
    mod = importlib.import_module("ap2.components.mattermost")
    return mod.do_mattermost_thread_read(cfg, args)


# ---------------- SDK wiring ----------------


def build_mcp_server(cfg: Config, adapter=None):
    """Build the in-process MCP server exposing the custom tools.

    Imported lazily so unit tests don't need the SDK.

    TB-355 (axis 3): the `@tool` definitions below remain ap2's canonical
    custom tool set, but the `create_sdk_mcp_server(...)` assembly that turns
    them into the backend-native MCP server now lives behind the
    `AgentAdapter` (`ClaudeCodeAdapter.build_tool_server`) so both backends
    expose one toolset. The tool set is handed to the adapter as a unit; the
    adapter records the registered short-names for
    `adapter.registered_tool_names()`. `adapter` defaults to a fresh
    `ClaudeCodeAdapter`; callers (e.g. the axis-3 contract test) may pass their
    own instance to inspect that enumeration.
    """
    # TB-366: import the `@tool` schema decorator from the adapter layer (a
    # lazy re-export of `claude_agent_sdk.tool`) rather than from
    # `claude_agent_sdk` directly, so the SDK is imported only inside
    # `ap2/adapters/` (the import-direction gate `test_sdk_import_boundary`).
    from .adapters import tool

    @tool(
        "board_edit",
        "Add, move, or remove tasks on the TASKS.md board.",
        {
            "action": str,
            "task_id": str,
            "title": str,
            "tags": list,
            "briefing": str,
            "description": str,
            "blocked_on": str,
        },
    )
    async def board_edit(args):
        return do_board_edit(cfg, args)

    @tool(
        "cron_edit",
        "Add, remove, or update a scheduled cron job. Operator-CLI use "
        "via `ap2 cron edit`; not exposed to control agents (TB-146). "
        "Use `cron_propose` for agent-side proposals — task agents emit "
        "`cron_proposed` events; operator promotes via review.",
        {
            "action": str,
            "name": str,
            "interval": str,
            "prompt": str,
            "active_when": str,
            "max_turns": int,
        },
    )
    async def cron_edit(args):
        return do_cron_edit(cfg, args)

    # TB-312: Mattermost MCP-tool handlers come from the `mattermost`
    # component's manifest (axis-(6) import-direction gate forbids a
    # static `from ap2.components.mattermost import …` here). The
    # handler bodies live in `ap2/components/mattermost/__init__.py`;
    # this lookup grabs them via the registry's hook-point slots so
    # the tool's external name (`mattermost_reply`) stays stable per
    # goal.md L184-186 even though the implementation moved.
    from .registry import default_registry as _default_registry
    try:
        _mm_manifest = _default_registry().get("mattermost")
        _do_mattermost_reply = _mm_manifest.hook_points["mcp_tool_reply"]
        _do_mattermost_thread_read = _mm_manifest.hook_points["mcp_tool_thread_read"]
    except (KeyError, Exception):  # noqa: BLE001
        # Component absent (e.g. someone removed the subpackage from
        # the tree) — stub handlers that return a clean MCP error so
        # the agent gets a coherent "not configured" signal rather
        # than an MCP-server import-time crash.
        def _do_mattermost_reply(_cfg, _args):
            return _err("mattermost component is not installed")
        def _do_mattermost_thread_read(_cfg, _args):
            return _err("mattermost component is not installed")

    @tool(
        "mattermost_reply",
        "Send a message to a Mattermost channel or thread.",
        {"channel": str, "text": str, "thread_id": str},
    )
    async def mattermost_reply(args):
        return _do_mattermost_reply(cfg, args)

    @tool(
        "mattermost_thread_read",
        "Fetch all messages in a Mattermost thread (root + replies). Use "
        "when the user's incoming message is a thread reply and you need "
        "context from earlier in the conversation (e.g. the operator "
        "replied 'yes' in a thread where the bot earlier asked 'approve "
        "TB-N?'). `thread_id` is the post id of the thread root — pass "
        "the `thread_id` field from the incoming message verbatim. "
        "`max_messages` defaults to 50 and truncates from the OLDEST end "
        "(most-recent N posts are kept). Returns `posts` as a "
        "chronologically-ordered list of {user, text, create_at, "
        "post_id} dicts. This is a local-only HTTP call to the Mattermost "
        "server — not Anthropic-side tool budget — so it's cheap; still, "
        "one call per turn is enough (no point re-reading the same "
        "thread). Returns an error if MATTERMOST_URL / MATTERMOST_TOKEN "
        "are unset; in that case fall back to a `mattermost_reply` "
        "explaining you can't read thread history right now.",
        {"thread_id": str, "max_messages": int},
    )
    async def mattermost_thread_read(args):
        return _do_mattermost_thread_read(cfg, args)

    @tool(
        "log_event",
        "Append an event to the autopilot event log.",
        {"type": str, "summary": str},
    )
    async def log_event(args):
        return do_log_event(cfg, args)

    @tool(
        "daemon_control",
        "Pause or resume the autopilot daemon.",
        {"action": str, "reason": str},
    )
    async def daemon_control(args):
        return do_daemon_control(cfg, args)

    @tool(
        "ideation_state_write",
        "Overwrite .cc-autopilot/ideation_state.md with a fresh per-cycle "
        "progress assessment (TB-87 Step 0). Body is written verbatim — the "
        "ideation prompt is responsible for schema correctness. Returns the "
        "byte count written. Path is fixed; no path arg.",
        {"content": str},
    )
    async def ideation_state_write(args):
        return do_ideation_state_write(cfg, args)

    # Tool name avoids the `task_*` prefix because Claude Code reserves that
    # namespace for its built-in TaskCreate/TaskUpdate/TaskList/TaskGet
    # subagent dispatch tools. Real-SDK smoke runs against `task_complete`
    # showed Claude Code's tool surface filtered the name out — `ToolSearch`
    # returned 0 results for `mcp__autopilot__task_complete` even though the
    # MCP server registered it. Renamed to `report_result` (no `task_`
    # prefix) so the namespace doesn't collide.
    @tool(
        "git_log_grep",
        "Search the project's git log for commits whose message matches "
        "`query` (passed verbatim to `git log --grep=...`). Returns up to "
        "`max_results` (default 20, capped at 100) one-line summaries. "
        "Replaces the ad-hoc `Bash('git log --grep=...')` pattern — "
        "control agents do not have Bash (TB-109).",
        {"query": str, "max_results": int},
    )
    async def git_log_grep(args):
        return do_git_log_grep(cfg, args)

    @tool(
        "operator_log_append",
        "Queue a timestamped operator-decision line for the daemon to "
        "append to .cc-autopilot/operator_log.md at the next tick "
        "(TB-106, TB-201). Use ONLY for operator-mediated messages — "
        "e.g. when an operator says `@claude-bot done: <action>` or "
        "`@claude-bot decided: <choice>`. Args: note (required, one "
        "sentence), task_id (optional TB-N). Ideation reads this log "
        "in Step 0 and treats entries as authoritative; logged decisions "
        "are not re-proposed. "
        "TB-201: the call queues an `ack` op on the operator queue "
        "rather than writing operator_log.md synchronously. Pre-TB-201 "
        "the synchronous write raced running task agents (operator_log.md "
        "is fenced and not exempt from TB-110's post-hoc snapshot check), "
        "tripping false-positive state violations and rolling back "
        "legitimate task work. The drain-side handler performs the actual "
        "write at tick boundary under the daemon's board lock. The MCP "
        "tool's external name and arg shape are unchanged from pre-TB-201.",
        {"note": str, "task_id": str},
    )
    async def operator_log_append(args):
        return enqueue_operator_ack(cfg, args)

    @tool(
        "operator_queue_append",
        "Stage an operator board op for the daemon to apply at the next "
        "tick (TB-131). Routes around the rollback / read-stale-board race "
        "that direct `board_edit` exposes during in-flight task or ideation "
        "runs: queued ops aren't in HEAD until between runs, so "
        "`git reset --hard <pre_run_head>` rollback never wipes them and "
        "long-running SDK turns can't read a board snapshot that shifts "
        "underneath them. Use this — instead of `board_edit` — when "
        "@claude-bot is asked to add/move/unfreeze/delete/approve a task "
        "and a task agent is currently active. (TB-142: `board_edit` is "
        "removed from the MM handler's RESTRICTED toolset, so this is "
        "the ONLY board-mutation surface mid-task.) For `add_*` ops, the "
        "TB-N ID is pre-allocated synchronously (so you can mention it "
        "in your reply) and the briefing file is pre-written; only the "
        "TASKS.md insertion is deferred. "
        "TB-154 BRIEFING STRUCTURE — for `add_*` ops AND for `update` "
        "ops that include a `briefing` payload, the `briefing` arg "
        "MUST use exactly these `##`-level section names (case-sensitive, "
        "any order): `## Goal`, `## Scope`, `## Design`, `## Verification`, "
        "`## Out of scope`. The validator rejects any other section names "
        "(e.g. `## Acceptance` instead of `## Verification`, or a "
        "top-level `## Files to touch` block) before allocating a TB-N "
        "(for adds) or before overwriting the slug-stable briefing file "
        "(for updates) — the per-task verifier (TB-69) parses the "
        "briefing's `## Verification` section literally, so the "
        "structural shape is load-bearing. Extra `##`-level sections "
        "(e.g. `## Decision log`, `## Why`) are fine; the "
        "`## Verification` section needs at least one bullet (backticked "
        "shell command, test name, or judge-checkable prose claim). "
        "TB-161 GOAL ANCHOR — the `## Goal` body MUST cite (as a "
        "substring) one of `goal.md`'s `## Current focus` / `## Done "
        "when` heading titles or a Done-when bullet. The validator "
        "rejects briefings whose Goal body cites no anchor, so quote "
        "the focus-item heading verbatim or paste 4-6 words of a "
        "Done-when bullet into the Goal text. Closes the gap-covering-"
        "without-drift failure mode (a structurally-canonical briefing "
        "whose value is only ap2-meta-polish, unconnected to any "
        "operator-stated focus item). Skipped when goal.md is missing "
        "or all-placeholder. "
        "TB-164 WHY-NOW RATIONALE — the `## Goal` body MUST include a "
        "line-anchored `Why now:` paragraph (≥40 chars after the "
        "marker) answering goal.md's delete-test (\"if we delete this "
        "and the goal still ships, was it useful?\"). The validator "
        "rejects briefings whose Goal body has no `Why now` marker OR "
        "a trivial one (e.g. `Why now: yes`). Name the failure mode "
        "this closes or the gap it fills, not just \"this would be "
        "nice to have\". Closes the push-for-progress-without-scope-"
        "creep failure mode (goal.md lines 61-70). "
        "Args: op (one of add_ready, "
        "add_backlog, add_frozen, move_to_backlog, unfreeze, delete, "
        "approve, update); task_id (TB-N for non-add ops); title / tags "
        "(comma-separated string) / description / briefing / blocked_on "
        "(for add ops); force (true/false, for delete from Active/Ready/"
        "Pipeline Pending, OR for update on Active/Pipeline Pending — "
        "but briefing-content edits to a running task are hard-refused "
        "regardless). For `update` ops (TB-153): the same fields apply "
        "(title / tags / description / briefing) but `blocked` (CSV) "
        "replaces `blocked_on`, and explicit `clear_tags` / "
        "`clear_blocked` (true/false) clear those fields — an omitted "
        "flag means unchanged. At least one field must be set.",
        {
            "op": str,
            "task_id": str,
            "title": str,
            "tags": str,
            "description": str,
            "briefing": str,
            "blocked_on": str,
            "blocked": str,
            "clear_tags": str,
            "clear_blocked": str,
            "force": str,
        },
    )
    async def operator_queue_append(args):
        # Normalize string-shaped args to the dict shape do_operator_queue_append
        # expects: tags is a comma-separated string here but a list inside.
        normalized = dict(args)
        # TB-193: `update_goal` is operator-CLI-only. The MM handler /
        # control agents have no path to mutate goal.md — `prompts.py`
        # already documents the design intent ("operator-curated; if
        # you think it needs updating, raise the recommendation in
        # your RESULT summary; do NOT rewrite"). Refuse here at the
        # MCP boundary so the op enum surfaced to the agent doesn't
        # include this verb regardless of what `OPERATOR_QUEUE_OPS`
        # advertises. Same precedent as `cron_edit` / `board_edit`
        # being CLI-only after TB-145 / TB-146.
        if (normalized.get("op") or "").strip() == "update_goal":
            return _err(
                "update_goal is operator-CLI-only "
                "(`ap2 update-goal --file <path>`); refusing the MCP "
                "surface. If you think goal.md needs updating, raise "
                "the recommendation in your RESULT summary."
            )
        raw_tags = normalized.get("tags")
        if isinstance(raw_tags, str):
            if raw_tags.strip():
                normalized["tags"] = [
                    t.strip() for t in raw_tags.split(",") if t.strip()
                ]
            else:
                # TB-153: for `update` ops, distinguish "tags omitted"
                # (don't touch tags) from "tags=''" (clear). Operators
                # who really mean "clear" should use `clear_tags=true`,
                # so an empty string here is treated as "omitted" by
                # dropping the key. For other ops the existing
                # behavior (treat empty as []) is preserved by the
                # add-side handler defaulting via `args.get("tags") or []`.
                normalized.pop("tags", None)
        force = normalized.get("force")
        if isinstance(force, str):
            normalized["force"] = force.strip().lower() in ("1", "true", "yes")
        # TB-153: explicit-clear flags ride as strings on the MCP wire
        # (the schema is all-string for SDK compatibility); coerce to
        # bools so the queue-append handler's truthy checks land cleanly.
        for flag in ("clear_tags", "clear_blocked"):
            v = normalized.get(flag)
            if isinstance(v, str):
                normalized[flag] = v.strip().lower() in ("1", "true", "yes")
        return do_operator_queue_append(cfg, normalized)

    @tool(
        "report_result",
        "Report task completion to the autopilot daemon. Call this ONCE at "
        "the end of your run instead of emitting a `RESULT:` text block. "
        "Args: status='complete'|'incomplete'|'blocked'|'failed' (required); "
        "commit=<7-40 char sha or empty>; summary=<one sentence>; "
        "files_changed=<comma-separated paths>; tests_passed='true'|'false'. "
        "To propose a recurring cron job, call `cron_propose` separately — "
        "it is not bundled into this result (TB-123).",
        # All-string schema — every other MCP tool in this server uses str-
        # only fields. `list` / `bool` types in the schema correlated with
        # Claude Code refusing to surface the tool in earlier smoke runs;
        # strings round-trip cleanly and the daemon-side capture parses
        # `tests_passed` / `files_changed` from their string forms.
        #
        # TB-123: `cron` field dropped — proposals are now their own MCP
        # tool (`cron_propose`) so each proposal gets a structured arg
        # surface, its own event, and failure isolation from result
        # reporting.
        {
            "status": str,
            "commit": str,
            "summary": str,
            "files_changed": str,
            "tests_passed": str,
        },
    )
    async def report_result(args):
        return do_task_complete(cfg, args)

    @tool(
        "cron_propose",
        "Propose a recurring cron job for operator review (TB-123). Use this "
        "when, while working on a task, you notice that some operation should "
        "fire on a schedule (e.g. a weekly perf snapshot, an hourly health "
        "check). The proposal is queued for operator review — it does NOT "
        "mutate cron.yaml directly. `cron_edit` (the direct-mutation tool) "
        "is operator-CLI-only post-TB-146; no agent — cron, ideation, MM "
        "handler, or task — can adopt a proposal automatically. "
        "Each call emits a `cron_proposed` event with the calling task's "
        "TB-id, so you can call it multiple times in one task — each "
        "proposal is independent. Args: name (short stable identifier, "
        "e.g. 'weekly-perf-snapshot'); schedule (interval like '1h' / '1d' "
        "/ '30m'); prompt (the prompt body the cron job will use); "
        "rationale (one short sentence on why this should fire on a "
        "schedule — part of the operator's review).",
        {
            "name": str,
            "schedule": str,
            "prompt": str,
            "rationale": str,
        },
    )
    async def cron_propose(args):
        return do_cron_propose(cfg, args)

    @tool(
        "status_report_run",
        "Trigger an on-demand autopilot status report (TB-144). Use when "
        "the operator explicitly asks for a status report (e.g. "
        "\"@claude-bot status\", \"@claude-bot what's going on\"). The call "
        "dispatches a sub-agent through the same shared routine the "
        "scheduled status-report cron uses, so chat-triggered reports get "
        "the same prompt body, freshness contract, and skip-if-idle gate "
        "as cron-triggered ones; events.jsonl gains a `cron_start` / "
        "`cron_complete` pair with `trigger=\"chat\"` so post-mortems can "
        "distinguish on-demand vs. scheduled runs. Don't call repeatedly "
        "— the routine has its own skip-if-idle gate, so calling more "
        "often than that won't get you a fresher report. Args: reason "
        "(one short sentence; what the operator asked for, lands in the "
        "audit event). The chat trigger does NOT advance "
        "`cron_state[status-report].last_run` — the next scheduled cron "
        "still fires on its normal interval.",
        {"reason": str},
    )
    async def status_report_run(args):
        return await do_status_report_run(cfg, args)

    @tool(
        "pipeline_task_start",
        "Launch a long-running pipeline as a detached OS subprocess. Use this "
        "when your task's work will exceed ~5 minutes of wall-clock time — "
        "Polygon/Polygon-class data fetches, full-history backtests, "
        "parameter sweeps, ML training. The daemon dispatches one task at a "
        "time inside a single `await sdk.query(...)` slot, so a 30-min inline "
        "run holds the only task slot for 30 min and risks tripping "
        "AP2_TASK_TIMEOUT_S (default 1h). After this call returns, finish "
        "your turn with `report_result(status='complete', ...)` summarizing "
        "what you dispatched. The daemon will move the task to "
        "`Pipeline Pending` and re-run your briefing's `## Verification` "
        "against the post-pipeline working tree once every pid you spawned "
        "has died. You can call this multiple times for parallel pipelines; "
        "the daemon waits for all of them.",
        {
            "name": str,
            "command": str,
        },
    )
    async def pipeline_task_start(args):
        return do_pipeline_task_start(cfg, args)

    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        version = _pkg_version("claude-automation")
    except PackageNotFoundError:
        version = "unknown"

    # TB-355 (axis 3): hand ap2's custom tool set to the AgentAdapter, which
    # owns the `create_sdk_mcp_server(...)` assembly (relocated into
    # `ClaudeCodeAdapter.build_tool_server`) so Claude tool exposure flows
    # through the adapter and a future Codex backend can register the same set.
    if adapter is None:
        from .adapters.claude_code import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
    return adapter.build_tool_server(
        [
            board_edit,
            cron_edit,
            mattermost_reply,
            mattermost_thread_read,
            log_event,
            daemon_control,
            ideation_state_write,
            git_log_grep,
            operator_log_append,
            operator_queue_append,
            report_result,
            cron_propose,
            status_report_run,
            pipeline_task_start,
        ],
        server_name="autopilot",
        version=version,
    )


# Control agents (cron, ideation, mattermost handler) read project state
# via `Read`/`Glob`/`Grep` and mutate it via narrow MCP tools. They do
# NOT get `Bash` (TB-109) — the only legitimate use was ideation's
# `git log --grep=<TASK_ID>` in Step 1.5, replaced by the `git_log_grep`
# MCP tool. Dropping shell access closes the corruption surface that bit
# stoch's TASKS.md (TB-108): a control agent's `Bash("echo > TASKS.md")`
# bypassed every fence we'd built for task agents.
CONTROL_AGENT_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "mcp__autopilot__board_edit",
    # TB-146: `cron_edit` is NOT exposed to control agents. The only
    # in-workflow programmatic use was ideation auto-adopting
    # `cron_proposed` events from task agents — that bypassed the
    # operator-in-the-loop pattern TB-121 establishes for ideation-
    # proposed *tasks* (which require `ap2 approve` to dispatch). With
    # `cron_edit` hidden from agents, cron schedule mutation is
    # operator-CLI-only (`ap2 cron edit ...`); ideation may still
    # SURFACE unadopted `cron_proposed` events in its per-cycle
    # assessment but cannot adopt them. Task agents continue to use
    # `cron_propose` to emit proposals (no change). Re-add here only
    # alongside an explicit justification + a review gate.
    "mcp__autopilot__mattermost_reply",
    "mcp__autopilot__log_event",
    "mcp__autopilot__daemon_control",
    "mcp__autopilot__ideation_state_write",
    "mcp__autopilot__git_log_grep",
    "mcp__autopilot__operator_log_append",
    # TB-131: queue-based board mutation. The MM handler uses this in
    # place of `board_edit` (which is filtered out of MM_HANDLER_TOOLS
    # below — TB-145).
    "mcp__autopilot__operator_queue_append",
    # TB-144: on-demand status report trigger. Available to control
    # agents in general (not just the MM handler) so a future cron job
    # can also fire one without re-implementing the routine; the MM
    # handler is the immediate consumer (operator-triggered reports).
    "mcp__autopilot__status_report_run",
]

# TB-291: ideation's toolset is `CONTROL_AGENT_TOOLS` minus
# `operator_queue_append`. The queue path exists to defend against a TOCTOU
# race — direct `board_edit` mutates `TASKS.md` immediately, which is unsafe
# while a task agent is mid-run because TB-110's snapshot-window check would
# flag the concurrent state mutation. The MM handler defers to the queue
# precisely because it can fire mid-task. Ideation, by contrast, is
# sequential with task execution by construction: `_maybe_ideate` only fires
# when Active is empty, and the daemon's tick loop holds back new tasks
# until ideation's run commits. The TOCTOU race the queue path defends
# against therefore cannot occur during ideation, so the tool is
# unnecessary surface — and the agent will defensively prefer it over
# direct `board_edit` because the queue-tool's own docstring recommends it
# as the safer choice when a task agent might be active. That defensive
# preference desynced the empty-cycles counter on 2026-05-26: the queue
# path emits `operator_queue_append op=add_backlog`, NOT
# `ideation_proposal_recorded` — and the counter in
# `ap2/ideation_halt.py:_consecutive_empty_ideation_cycles` only treats
# `ideation_proposal_recorded` as a reset signal. One productive cycle
# (TB-290) routed via the queue ticked the counter as if empty, falsely
# advancing the focus to ROADMAP_COMPLETE. Fencing the toolset forces
# ideation down the direct `board_edit` path the counter expects, aligning
# prompt + tool surface + event vocabulary on one consistent shape. Other
# control agents (cron jobs) keep `operator_queue_append` in their
# `CONTROL_AGENT_TOOLS` — only ideation needs the fence.
IDEATION_TOOLS = [
    t for t in CONTROL_AGENT_TOOLS
    if t != "mcp__autopilot__operator_queue_append"
]

# TB-145: the Mattermost handler ALWAYS runs with this single (narrowed)
# toolset, regardless of whether a task agent is currently in flight. The
# previous TB-122 design picked between FULL and RESTRICTED variants based on
# a snapshot of `Board.iter_tasks("Active")` at handler-spawn time, but that
# check was a TOCTOU race in two ways:
#   1. Stale-at-spawn — the daemon's main tick loop could promote a Backlog
#      task and start its run while the handler was mid-turn (handler picked
#      FULL, then a new task started and the handler's `cron_edit` /
#      `board_edit` calls landed against the running task's snapshot
#      window, tripping TB-110's state-violation check).
#   2. Stale-at-tool-call — even with a race-free snapshot, the toolset
#      decision is anchored at handler-spawn time but the actual tool call
#      may fire 30s later. There's no way to re-evaluate "is a task active"
#      at every tool-call boundary without serializing the MM handler with
#      the main tick.
# Always-RESTRICTED removes both surfaces. Convenience cost: `cron_edit` and
# `ideation_state_write` are no longer reachable from chat — operator uses
# `ap2 cron list/edit` and direct `ideation_state.md` edits via the CLI
# instead. The save-busy-task safety win is constant; the convenience loss
# is rare. Post-TB-141/142/143, queue-routed board ops via
# `operator_queue_append` are the primary mutation path anyway, so the
# handler's day-to-day capability isn't materially reduced.
# What's in MM_HANDLER_TOOLS:
#   - read tools (Read/Glob/Grep/git_log_grep) so the agent can answer
#     questions and reason about state.
#   - `operator_queue_append` so the operator can still queue add / move /
#     unfreeze / delete / approve ops; the daemon drains them at the next
#     tick boundary, so the running task's window never sees the mutation.
#   - `mattermost_reply` / `log_event` so the handler can finish its turn.
#   - `daemon_control` so "@claude-bot pause" works mid-task (pause takes
#     effect on the next tick; the running task completes normally).
#   - `operator_log_append` so "@claude-bot ack: …" still lands in the
#     operator log (ideation reads it in Step 0 — the operator's veto
#     channel must stay open even mid-task).
#   - `status_report_run` (TB-144) so chat-triggered status reports use the
#     same routine as the cron job.
# What's dropped (relative to CONTROL_AGENT_TOOLS):
#   - `ideation_state_write` — would rewrite the per-cycle assessment
#     ideation was acting on. CLI alternative: edit `ideation_state.md`
#     directly while the daemon is idle.
#   - `board_edit` — direct TASKS.md mutation during an in-flight run trips
#     TB-110's state-violation check. Route via `operator_queue_append`
#     instead.
# `cron_edit` is NOT listed here because TB-146 removed it from
# CONTROL_AGENT_TOOLS entirely (no agent — cron, ideation, or MM handler —
# can mutate cron.yaml; it's operator-CLI-only via `ap2 cron edit`). The
# explicit filter is kept as a defense-in-depth no-op so a future
# re-introduction into CONTROL_AGENT_TOOLS doesn't silently leak the tool
# back into the MM handler without re-evaluating the race surface.
MM_HANDLER_TOOLS = [
    t for t in CONTROL_AGENT_TOOLS
    if t not in (
        "mcp__autopilot__cron_edit",  # defensive (already absent post-TB-146)
        "mcp__autopilot__ideation_state_write",
        "mcp__autopilot__board_edit",
    )
] + [
    # TB-149: thread-context read for the MM handler. NOT in
    # CONTROL_AGENT_TOOLS because cron jobs and ideation don't have a
    # thread to read — the handler is the only agent that receives a
    # `thread_id` in its prompt. Kept off TASK_AGENT_TOOLS for the same
    # reason (task agents have no chat surface). Added explicitly here
    # rather than via CONTROL_AGENT_TOOLS so we don't widen the cron /
    # ideation toolset for a tool they can't use.
    "mcp__autopilot__mattermost_thread_read",
]

# `pipeline_task_start` is the first MCP tool task agents can call directly
# (TB-81). The privilege increase is narrow: one tool, atomic, well-scoped to
# launching long-running work that the daemon can't host inside a single
# `await sdk.query(...)` slot. Keep this list otherwise minimal — task agents
# are not control agents and shouldn't gain blanket access to `board_edit`,
# `cron_edit`, etc. via this list.
TASK_AGENT_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "mcp__autopilot__pipeline_task_start",
    "mcp__autopilot__report_result",
    # TB-123: cron-proposal lifted out of report_result's args into a dedicated
    # tool. Task agents call `cron_propose(name, schedule, prompt, rationale)`
    # one or more times to surface "this should fire on a schedule" without
    # bundling it into the result reporting. Symmetric with control agents'
    # `cron_edit` — task agents propose, operator promotes via review.
    "mcp__autopilot__cron_propose",
]


# Files the task agent must NOT edit. Two enforcement layers wrap each
# entry: (1) `prompts._TASK_HEADER` lists each file with a one-line
# explanation so a well-behaved agent skips them, (2) `daemon.run_task`
# adds `Edit(<path>)` + `Write(<path>)` to `disallowed_tools` so the SDK
# rejects direct calls if the agent tries anyway.
#
# Defense-in-depth, not airtight: a determined agent could still write
# via `Bash` (`echo > path`, `sed -i`, `python -c "open(...).write(...)"`).
# Those rely on prompt compliance — globbing every shell shape that
# touches a fenced file is a losing arms race.
#
# Categories:
#   - Daemon-owned state: TASKS.md, progress.md, events.jsonl,
#     ideation_state.md, CLAUDE.md (the daemon bumps Next task ID).
#   - Daemon-owned config: cron.yaml (operator edits via `ap2 cron edit`
#     → `do_cron_edit`; no agent toolset has `cron_edit` post-TB-146).
#   - Operator-curated: goal.md — the project mission. Ideation reads it
#     for grounding; if a task could rewrite it, ideation would
#     effectively rewrite its own constraints. Tasks that *want* to update
#     goal.md should surface the recommendation in their RESULT summary
#     instead, leaving the operator to apply.
TASK_AGENT_FENCED_PATHS = (
    "TASKS.md",
    "CLAUDE.md",
    "goal.md",
    ".cc-autopilot/progress.md",
    ".cc-autopilot/events.jsonl",
    ".cc-autopilot/ideation_state.md",
    ".cc-autopilot/cron.yaml",
    ".cc-autopilot/operator_log.md",
    # TB-143: `operator_queue.jsonl` lives in the defense-layers list
    # (prompt-header reminder + SDK `Edit`/`Write` reject) but is
    # explicitly excluded from TB-110's post-hoc snapshot check via
    # `rollback._VIOLATION_CHECK_EXCLUDED_PATHS`. Same shape as
    # `events.jsonl`: the daemon / operator legitimately append to it
    # during in-flight task runs (every `ap2 add`, `unfreeze`,
    # `delete`, `move_to_backlog`, `approve` issued while a task is
    # active writes a record), so a hash-snapshot diff would
    # false-positive and roll back legitimate work — TB-141 narrowly
    # fixed that by dropping the path from the fence entirely, but
    # that conflated the two distinct purposes the fence list
    # serves. Re-listing here restores defense-in-depth without
    # re-introducing the false-positive.
    ".cc-autopilot/operator_queue.jsonl",
    ".cc-autopilot/operator_queue_state.json",
    # TB-188: per-proposal records (one JSON per ideation-authored
    # proposal, written at `add_backlog` time and reconciled with an
    # `outcome` block on the first terminal event). Daemon-owned audit
    # trail — task agents must NOT edit a record (a malicious or
    # confused agent could otherwise rewrite its own proposal's
    # `focus_anchor` / `why_now` mid-run to cover scope drift). The
    # directory is treated as a unit; the prompt-header rendering walks
    # it so any individual `<TB-N>.json` under it is fenced.
    ".cc-autopilot/ideation_proposals",
    # TB-198: per-task briefing markdown files (one `<slug>.md` per
    # backlog entry, authored by `ap2 add` or by ideation via
    # `do_board_edit`'s add-* branch). The per-task verifier reads
    # `## Verification` from these files at verification time — a task
    # agent rewriting its own briefing's Verification section mid-run
    # could weaken the criteria the verifier evaluates against, and
    # editing an unrelated task's briefing could silently corrupt a
    # future verification gate. Mirrors the `ideation_proposals/` shape
    # (whole-directory fence, content-dependent slug filenames make a
    # per-file enumeration impossible).
    ".cc-autopilot/tasks",
    # TB-198: the auto-regenerated insights index. `insights.maybe_
    # regenerate_index(cfg)` (pre-fire from `_maybe_ideate`) is the
    # sole authorized writer; task-agent edits would corrupt the
    # regeneration's input/output invariants and confuse ideation's
    # Step 0.5 read. NOTE: the surrounding `.cc-autopilot/insights/`
    # directory stays writable — `#evaluation`-tagged task agents
    # legitimately CREATE / EDIT per-topic `<topic>.md` files per the
    # ideation prompt's Step 0.5 contract. Only `_index.md` is daemon-
    # owned, so the fence is a single-file path (not a directory like
    # `tasks/` / `ideation_proposals/`).
    ".cc-autopilot/insights/_index.md",
    # TB-226: focus-list pointer state (which `## Current focus:`
    # heading in goal.md the daemon's runtime pointer points at, plus
    # the heuristic-fallback empty-cycles counter and the
    # `roadmap_complete` ack bookkeeping). Daemon-owned: a task agent
    # rewriting its own focus pointer mid-run could short-circuit the
    # roadmap-exhaustion halt or fast-forward through an unfinished
    # focus to skip its Done-when criteria. The runtime pointer is
    # in-memory state only — goal.md itself stays operator-owned
    # (goal.md L187-191 "Goal.md auto-rotation" Non-goal).
    ".cc-autopilot/focus_pointer.json",
)
