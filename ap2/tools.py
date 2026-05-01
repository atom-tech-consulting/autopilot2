"""Custom SDK MCP tools for control agents.

The mattermost handler and cron agents call these to mutate the board, the cron
registry, and send Mattermost replies. Task agents do NOT get these tools — they
just code, commit, and exit.

Tools close over a Config so the daemon can wire paths at startup without the
agent having to know them.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid as _uuid
from pathlib import Path
from typing import Any

from . import events, retry
from .board import Board, board_file_lock, locked_board
from .config import Config, bump_next_task_id
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
    import re

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


# ---------------- implementations (SDK-free, directly testable) ----------------


# TB-134: TASKS.md is a line-oriented format — TASK_LINE_RE in board.py is
# a per-line regex, so a multi-line title/description/tag silently splits the
# rendered task across physical lines, stranding the trailing `[→ brief](...)`
# link on a different line and dropping it from the parsed Task. The first
# line still parses; subsequent lines surface as `board_malformed_line`
# events but stay on disk. Hit on TB-132 / TB-133 (operator had to manually
# re-collapse). Auto-collapsing newlines to spaces would silently produce
# 400-char run-on lines that nobody actually wanted; rejecting forces the
# caller to choose between summarizing inline OR moving the rich content
# into the briefing file — the right semantic split.
SINGLE_LINE_ERR = (
    "{field} must be a single line — break long content into briefing.md "
    "instead, or summarize to one line"
)


def _validate_single_line(field: str, value: str | None) -> str | None:
    """Reject a multi-line operator input (newline / carriage-return).

    Returns an error message string if `value` contains \\n or \\r;
    returns `None` if the value is single-line (or empty / None).
    Used by `cmd_add` (CLI), `do_board_edit` (MCP), and
    `do_operator_queue_append` (MCP + CLI bridge) so every entry point
    that can land a task on TASKS.md hits the same gate.
    """
    if not value:
        return None
    if "\n" in value or "\r" in value:
        return SINGLE_LINE_ERR.format(field=field)
    return None


def _allocate_id(board: Board, cfg: Config) -> str:
    """Pure: pick the next TB-N from the existing high-water marks.

    The candidate is `max(board_max + 1, CLAUDE.md next_task_id,
    queue_preallocated_max + 1)` — the third term covers TB-N's that an
    earlier `do_operator_queue_append` reserved on this same tick but
    hasn't yet drained onto the board (so back-to-back `ap2 add` calls
    issue sequential IDs without any of them touching CLAUDE.md).

    TB-141 made this side-effect-free: previously this also wrote
    `cfg.next_task_id` back to CLAUDE.md, which fired
    `task_state_violation` on whichever task was in flight when an
    operator ran `ap2 add` (CLAUDE.md is a fenced path). Persisting the
    new high-water mark is now the caller's responsibility:
      - `do_board_edit` writes synchronously (used by ideation /
        control agents — no in-flight task fence applies).
      - `do_operator_queue_append` does NOT write; the bump is deferred
        to `drain_operator_queue`, which runs as the daemon's first
        tick stage between task agent runs.
    """
    queue_max = _max_preallocated_id_in_queue(cfg)
    candidate = max(board.max_id() + 1, cfg.next_task_id, queue_max + 1)
    # In-memory bookkeeping so a second _allocate_id in the same Config
    # instance doesn't alias the just-issued ID — the disk-side bump
    # happens out of band (caller / drain).
    cfg.next_task_id = candidate + 1
    return f"TB-{candidate}"


def _max_preallocated_id_in_queue(cfg: Config) -> int:
    """Highest `preallocated_task_id` numeric suffix across queue records.

    Returns 0 if the queue is missing / empty / has no preallocated IDs.
    Reads both pending and already-applied records — the operator queue
    is compacted at drain time, so a not-yet-compacted applied record
    still holds a real reservation we mustn't reissue.
    """
    queue_path = operator_queue_path(cfg)
    if not queue_path.exists():
        return 0
    best = 0
    try:
        text = queue_path.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = rec.get("preallocated_task_id") or ""
        if not isinstance(tid, str) or not tid.startswith("TB-"):
            continue
        try:
            n = int(tid[3:])
        except ValueError:
            continue
        if n > best:
            best = n
    return best


def do_board_edit(cfg: Config, args: dict) -> dict:
    action = args.get("action", "")
    task_id = args.get("task_id")
    title = (args.get("title") or "").strip()
    tags = args.get("tags") or []
    briefing = args.get("briefing")
    description = (args.get("description") or "").strip()
    blocked_on = (args.get("blocked_on") or "").strip()

    # TB-134: reject multi-line title / description / tags up-front so the
    # MCP-driven path (ideation, MM handler) sees the same gate as the CLI.
    # Briefing content is exempt — that's free-form prose and lives in its
    # own file, not on the TASKS.md task line.
    for field_name, value in (
        ("title", title),
        ("description", description),
        ("blocked_on", blocked_on),
    ):
        err = _validate_single_line(field_name, value)
        if err:
            return _err(err)
    for tag in tags:
        err = _validate_single_line("tag", tag)
        if err:
            return _err(err)

    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }
    move_map = {
        "move_to_ready": "Ready",
        "move_to_active": "Active",
        "move_to_frozen": "Frozen",
        "move_to_complete": "Complete",
        "move_to_backlog": "Backlog",
        "move_to_pipeline_pending": "Pipeline Pending",
    }

    # TB-135: briefing is now required for every add_* op. The auto-fill
    # skeleton path (TB-69) generated briefings whose `## Verification`
    # had only a placeholder bullet — the per-task verifier then
    # "passed" prose like "(additional shell or prose bullets)" through
    # the LLM judge with no real diff to score against, completing
    # tasks with zero scope-specific verification (TB-131 hit this on
    # 2026-04-30). Pushing authorship to the caller (CLI:
    # --briefing-file; ideation / MM handler: already construct the
    # payload) closes the gap. Validate BEFORE taking `locked_board`'s
    # save-on-exit lock so a rejected add doesn't side-effect TASKS.md
    # whitespace normalization.
    if action in add_map and not (briefing or "").strip():
        return _err(
            "briefing is required for add actions (TB-135). "
            "Author a briefing markdown with a real "
            "`## Verification` section and pass it as the "
            "`briefing` arg."
        )

    try:
        with locked_board(cfg.tasks_file) as board:
            if action in add_map:
                if not title:
                    return _err("title is required for add actions")
                new_id = _allocate_id(board, cfg)
                briefing_rel = None
                if briefing:
                    slug = slugify(title)
                    brief_path = cfg.tasks_dir / f"{slug}.md"
                    # collision avoidance
                    n = 2
                    while brief_path.exists():
                        brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                        n += 1
                    brief_path.parent.mkdir(parents=True, exist_ok=True)
                    brief_path.write_text(briefing)
                    briefing_rel = str(brief_path.relative_to(cfg.project_root))
                # TB-132: blocked_on goes onto the task line as a
                # `@blocked:<csv>` codespan (alongside `#tags`) rather
                # than being injected into the description as
                # `(blocked on: ...)`. The codespan lives in `meta` and
                # round-trips through Task.render() / parse_task_line.
                meta: dict[str, str] = {}
                if blocked_on:
                    meta["blocked"] = blocked_on
                board.add(
                    add_map[action],
                    task_id=new_id,
                    title=title,
                    tags=tags,
                    meta=meta,
                    description=description,
                    briefing=briefing_rel,
                )
                # TB-141: persist the new high-water mark to CLAUDE.md
                # synchronously here. `_allocate_id` no longer writes —
                # this path (ideation / control agents calling the
                # `board_edit` MCP tool) is never invoked while a task
                # agent is in flight, so the synchronous CLAUDE.md
                # mutation doesn't trip the fenced-file violation
                # check. The deferred-bump pattern only applies to the
                # operator-queue path (`do_operator_queue_append` →
                # `drain_operator_queue`).
                claude_md = cfg.project_root / "CLAUDE.md"
                if claude_md.exists():
                    bump_next_task_id(claude_md, cfg.next_task_id)
                return _ok(
                    f"{action} {new_id} {title!r}",
                    task_id=new_id,
                    briefing_path=briefing_rel,
                )

            if action in move_map:
                if not task_id:
                    return _err("task_id is required for move actions")
                to_section = move_map[action]
                checked = True if to_section == "Complete" else None
                try:
                    t = board.move(task_id, to_section, check=checked)
                except KeyError:
                    return _err(f"{task_id} not on board")
                return _ok(f"{action} {t.id}", task_id=t.id, section=t.section)

            if action == "remove":
                if not task_id:
                    return _err("task_id is required for remove")
                removed = board.remove(task_id)
                if removed is None:
                    return _err(f"{task_id} not on board")
                return _ok(f"removed {removed.id}", task_id=removed.id)

            return _err(f"unknown action {action!r}")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


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

    Symmetric with control agents' `cron_edit` (direct mutation, only
    for cron + ideation control agents — TB-101's privilege split). Task
    agents get the proposal layer; operator promotes via review.

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


def do_operator_log_append(cfg: Config, args: dict) -> dict:
    """Append a timestamped operator-decision line to
    `.cc-autopilot/operator_log.md` (TB-106).

    Operator-owned channel for decisions ideation can't observe via the
    filesystem (e.g. "decided to keep FRAGILE plists as references" or
    "considered the universe-expansion question, deferred"). Ideation
    reads the log in Step 0 and treats logged items as authoritative —
    won't re-propose them in subsequent cycles.

    Two write paths share this handler:
      - operator-side: `ap2 ack [-t TB-N] "<note>"` (CLI)
      - mattermost-handler-side: `operator_log_append` MCP tool when the
        operator sends `@claude-bot done: ...` style messages.

    Each call appends one bullet line. The file is created with a
    short header on first append. `operator_ack` event emitted for
    auditability.
    """
    note = str(args.get("note") or "").strip()
    if not note:
        return _err("note is required")
    task_id = str(args.get("task_id") or "").strip()

    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements. Append-only.\n"
            "Ideation reads this in Step 0; logged items are authoritative —\n"
            "ideation won't re-propose decisions logged here._\n\n"
        )

    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tb_tag = f" [{task_id}]" if task_id else ""
    line = f"- {ts}{tb_tag} — {note}\n"
    with log_path.open("a") as f:
        f.write(line)

    payload: dict = {"note": note[:500]}
    if task_id:
        payload["task"] = task_id
    events.append(cfg.events_file, "operator_ack", **payload)
    return _ok(f"appended to {log_path.name}", line=line.strip())


# ---------------- operator queue (TB-131) ----------------
#
# Operator board mutations (`ap2 add`, `ap2 backlog`, `ap2 unfreeze`,
# `ap2 delete`, plus the MM-handler counterpart) are appended to
# `.cc-autopilot/operator_queue.jsonl` and applied by the daemon's
# `_tick` first stage. This trades immediate write-through for
# serializability against in-flight task / ideation runs:
#   - `git reset --hard <pre_run_head>` rollback never wipes operator
#     adds, because the add isn't in HEAD until the daemon drains the
#     queue between runs.
#   - Ideation reads a stable board snapshot for an entire SDK turn —
#     a queued `ap2 add` arriving mid-thought lands BEFORE ideation's
#     next read, not during it.
#
# ID pre-allocation is done at queue-append time (under the board
# lock) so `ap2 add` can still print the new TB-N immediately. Only
# the TASKS.md insertion is deferred.
#
# TB-141: the queue file itself is intentionally NOT in
# TASK_AGENT_FENCED_PATHS — appends made by the operator while a task
# is in flight used to mis-trip the post-hoc fenced-file snapshot
# check (TB-110), rolling back legitimate task work. Agents have no
# write path to the queue: no Edit/Write permission, no MCP tool that
# emits records under their authority, and the drain-side uuid +
# applied-state bookkeeping ignores any forged record they could
# Bash-shell into the file. The matching CLAUDE.md `Next task ID`
# bump is also deferred — `_allocate_id` is now pure, and
# `drain_operator_queue` writes CLAUDE.md once at end-of-pass.

# Ops the operator-queue path knows how to drain. Shared between the
# CLI (`do_operator_queue_append`) and the drain side
# (`drain_operator_queue`).
OPERATOR_QUEUE_OPS = (
    "add_ready",
    "add_backlog",
    "add_frozen",
    "move_to_backlog",
    "unfreeze",
    "delete",
)


def operator_queue_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"


def operator_queue_state_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "operator_queue_state.json"


def do_operator_queue_append(cfg: Config, args: dict) -> dict:
    """Append an operator board op to the daemon-drained queue (TB-131).

    Two write paths share this handler, mirroring how
    `do_operator_log_append` shares CLI + MCP today:
      - operator-side: `ap2 add` / `ap2 backlog` / `ap2 unfreeze` /
        `ap2 delete` route here instead of mutating TASKS.md directly.
      - MM-handler-side: the `operator_queue_append` MCP tool — for
        when @claude-bot is asked to add/move/unfreeze/delete a task
        during an in-flight run, where direct `board_edit` exposes the
        change to `git reset --hard <pre_run_head>` rollback.

    For `add_*` ops, this briefly takes the board lock to (a) write
    the briefing file, (b) pre-allocate a TB-N via `_allocate_id`
    (pure read, no CLAUDE.md write — TB-141), (c) append the queued
    op carrying the pre-allocated TB-N. The operator still gets the
    new ID printed immediately — both the TASKS.md insertion AND the
    CLAUDE.md `next_task_id` bump are deferred to drain. Pre-TB-141
    the bump happened synchronously here, but that mutated a fenced
    path during in-flight task runs and was mis-attributed by TB-110
    as an agent violation (TB-139, 2026-05-01).

    For move/unfreeze/delete ops, validates the target task against
    the current board snapshot under the lock so obvious operator
    errors (typo'd TB-N, unfreeze-on-non-Frozen, delete-from-Active
    without --force) are rejected immediately. The drain path runs
    its own validation too (state may have shifted between queue and
    drain) and emits `operator_queue_error` for any op it can't apply.
    """
    op = (args.get("op") or "").strip()
    if op not in OPERATOR_QUEUE_OPS:
        return _err(
            f"unknown op {op!r}; valid: {list(OPERATOR_QUEUE_OPS)}"
        )

    rec_args: dict[str, Any] = {}
    preallocated_task_id: str | None = None

    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }

    if op in add_map:
        title = (args.get("title") or "").strip()
        if not title:
            return _err("title is required for add ops")
        tags = list(args.get("tags") or [])
        description = (args.get("description") or "").strip()
        blocked_on = (args.get("blocked_on") or "").strip()
        briefing_content = args.get("briefing")

        # TB-134: reject multi-line title / description / tags before
        # writing anything to disk — pre-allocating a TB-N or briefing
        # file for an input we're going to refuse would leak state.
        for field_name, value in (
            ("title", title),
            ("description", description),
            ("blocked_on", blocked_on),
        ):
            err = _validate_single_line(field_name, value)
            if err:
                return _err(err)
        for tag in tags:
            err = _validate_single_line("tag", tag)
            if err:
                return _err(err)

        # TB-135: briefing is required for every add_* op. The
        # auto-fill skeleton path is gone — without a real
        # `## Verification` section the per-task verifier scores prose
        # placeholders against an empty diff and "passes" with zero
        # scope-specific evidence. We refuse before allocating an ID
        # so a rejected add doesn't leak a hole in the TB-N sequence.
        if not (briefing_content or "").strip():
            return _err(
                "briefing is required for add ops (TB-135). Author a "
                "briefing markdown with a real `## Verification` "
                "section and pass it as the `briefing` arg."
            )

        # TB-132: blocked_on rides on the task line as a `@blocked:<csv>`
        # codespan, not as `(blocked on: ...)` in the description. The
        # drain side reads `meta` from the queue record and passes it to
        # `board.add(..., meta=...)`.
        meta: dict[str, str] = {}
        if blocked_on:
            meta["blocked"] = blocked_on

        # The briefing file isn't under the lock — slug collision
        # avoidance just walks `<slug>-N.md` until it finds a free
        # path; it doesn't depend on TB-N allocation order.
        briefing_rel: str | None = None
        if briefing_content:
            slug = slugify(title)
            brief_path = cfg.tasks_dir / f"{slug}.md"
            n = 2
            while brief_path.exists():
                brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                n += 1
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(briefing_content)
            briefing_rel = str(brief_path.relative_to(cfg.project_root))

        # Allocation + queue append happen under a single
        # `board_file_lock` block (TB-141) so concurrent CLI invocations
        # see each other's preallocations through the queue file:
        # process B's `_allocate_id` reads the queue and finds process
        # A's just-written `preallocated_task_id`, so it allocates
        # process A's id + 1.
        #
        # Pre-TB-141 this serialized implicitly through the synchronous
        # CLAUDE.md bump inside `_allocate_id`; that bump is now
        # deferred to drain (so an `ap2 add` issued during a task run
        # doesn't trip the fenced-file violation check), which removed
        # CLAUDE.md as the cross-process source of truth and pushed the
        # responsibility to the queue file itself.
        rec_uuid = str(_uuid.uuid4())
        rec_ts = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        queue_path = operator_queue_path(cfg)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with board_file_lock(cfg.tasks_file):
            board = Board.load(cfg.tasks_file)
            preallocated_task_id = _allocate_id(board, cfg)
            rec_args = {
                "task_id": preallocated_task_id,
                "title": title,
                "tags": tags,
                "description": description,
                "meta": meta,
                "briefing_path": briefing_rel,
            }
            rec: dict[str, Any] = {
                "uuid": rec_uuid,
                "op": op,
                "args": rec_args,
                "ts": rec_ts,
                "preallocated_task_id": preallocated_task_id,
            }
            with queue_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        events.append(
            cfg.events_file,
            "operator_queue_append",
            uuid=rec["uuid"],
            op=op,
            task=preallocated_task_id,
        )
        return _ok(
            f"queued {op} → {preallocated_task_id}",
            uuid=rec["uuid"],
            op=op,
            task_id=preallocated_task_id,
        )
    else:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err(f"task_id is required for {op}")
        # Snapshot validation under the board lock — the drain path
        # re-validates too (state may shift) but rejecting obvious
        # operator errors immediately keeps the UX honest.
        with board_file_lock(cfg.tasks_file):
            board = Board.load(cfg.tasks_file)
            loc = board.find(task_id)
        if loc is None:
            return _err(f"{task_id} not on board")
        section = loc[0]
        if op == "unfreeze" and section != "Frozen":
            return _err(
                f"{task_id} is in {section}, not Frozen — "
                f"use `ap2 backlog {task_id}` for non-frozen moves"
            )
        if op == "delete" and section in ("Active", "Ready", "Pipeline Pending") \
                and not args.get("force"):
            return _err(
                f"{task_id} is in {section} — refusing without force. "
                f"Use `ap2 backlog {task_id}` first, or pass --force."
            )
        rec_args = {"task_id": task_id}
        if op == "delete":
            rec_args["force"] = bool(args.get("force"))

    # Non-add ops: no preallocation, no lock needed for the queue write
    # (the record is opaque to `_allocate_id`'s queue-max scan).
    rec: dict[str, Any] = {
        "uuid": str(_uuid.uuid4()),
        "op": op,
        "args": rec_args,
        "ts": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    queue_path = operator_queue_path(cfg)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    events.append(
        cfg.events_file,
        "operator_queue_append",
        uuid=rec["uuid"],
        op=op,
        task=rec_args.get("task_id", ""),
    )
    return _ok(
        f"queued {op}",
        uuid=rec["uuid"],
        op=op,
        task_id=rec_args.get("task_id", ""),
    )


def operator_queue_pending_count(cfg: Config) -> int:
    """Number of queued ops that haven't yet been drained.

    Surfaced by `ap2 status` so operators can spot a stalled daemon
    (queue depth > 0 with the daemon not running == ops stuck pending).
    """
    queue_path = operator_queue_path(cfg)
    if not queue_path.exists():
        return 0
    applied = _load_operator_queue_applied(operator_queue_state_path(cfg))
    count = 0
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("uuid") in applied:
            continue
        count += 1
    return count


def drain_operator_queue(cfg: Config) -> dict:
    """Apply queued operator ops as the first stage of each daemon tick
    (TB-131).

    Holds `board_file_lock` for the duration of the drain so concurrent
    CLI / MCP appends serialize against application. Each op:

      1. Has its uuid checked against
         `.cc-autopilot/operator_queue_state.json` — already-applied
         uuids are skipped (idempotent across crash-restart).
      2. Is dispatched through `_apply_operator_op` to the
         appropriate primitive (board.add / board.move / board.remove
         + retry-state reset for unfreeze + audit events).
      3. Records its uuid into the state file BEFORE moving on (so a
         crash mid-drain doesn't re-apply the op next tick).
      4. Writes a one-line audit summary to operator_log.md.

    Failures (op references a task that vanished, etc.) are recorded
    with `operator_queue_error` events but the uuid is still marked
    applied — silently failing forever is worse than letting the
    operator see one error and move on.

    TB-141: at end-of-drain, also bumps CLAUDE.md's `Next task ID` to
    `max(highest preallocated TB-N this pass + 1, current next_id)`.
    The synchronous bump that used to live in `_allocate_id` was
    retired so an `ap2 add` issued during a task run doesn't trip the
    fenced-file violation check; this is the corollary that keeps
    CLAUDE.md current. One write per drain pass instead of one per
    add. Drains that applied only move/unfreeze/delete ops leave
    CLAUDE.md untouched.

    Returns a dict with `applied` (count) and `touched_paths` (state
    files dirtied) so the daemon-side caller can pass them to
    `_commit_state_files` for a coherent state-file commit.
    """
    queue_path = operator_queue_path(cfg)
    state_path = operator_queue_state_path(cfg)
    if not queue_path.exists() or queue_path.stat().st_size == 0:
        return {"applied": 0, "touched_paths": []}

    applied = _load_operator_queue_applied(state_path)
    pending: list[dict] = []
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("uuid") in applied:
            continue
        pending.append(rec)

    if not pending:
        # No new ops; opportunistically compact in case the queue file
        # has accumulated already-applied uuids.
        _compact_operator_queue(queue_path, applied)
        return {"applied": 0, "touched_paths": []}

    applied_count = 0
    touched: set[str] = set()
    highest_alloc = 0
    with board_file_lock(cfg.tasks_file):
        for rec in pending:
            try:
                board = Board.load(cfg.tasks_file)
                _apply_operator_op(cfg, board, rec)
                board.save()
                _append_operator_audit_line(cfg, rec)
                applied_count += 1
                touched.update(
                    [
                        "TASKS.md",
                        "CLAUDE.md",
                        ".cc-autopilot/retry_state.json",
                        ".cc-autopilot/operator_log.md",
                        ".cc-autopilot/tasks",
                    ]
                )
                # TB-141: track the highest preallocated TB-N across the
                # drain so we can bump CLAUDE.md once at the end (instead
                # of once per `_allocate_id` call inside
                # `do_operator_queue_append`).
                tid = rec.get("preallocated_task_id") or ""
                if isinstance(tid, str) and tid.startswith("TB-"):
                    try:
                        n = int(tid[3:])
                    except ValueError:
                        n = 0
                    if n > highest_alloc:
                        highest_alloc = n
            except Exception as e:  # noqa: BLE001
                events.append(
                    cfg.events_file,
                    "operator_queue_error",
                    uuid=rec.get("uuid", ""),
                    op=rec.get("op", ""),
                    error=f"{type(e).__name__}: {e}",
                )
            finally:
                # Mark applied (or attempted) regardless of success —
                # silently re-applying a broken op every tick is worse
                # than recording the error once and moving on. Operator
                # can inspect events.jsonl for the failure cause.
                applied.add(rec["uuid"])
                _save_operator_queue_applied(state_path, applied)
        _compact_operator_queue(queue_path, applied)

        # TB-141: end-of-drain CLAUDE.md bump. The synchronous bump
        # inside `_allocate_id` was retired so an `ap2 add` issued
        # while a task agent is in flight doesn't trip TB-110's
        # fenced-file violation check (CLAUDE.md is fenced; the
        # mid-flight mutation looks identical to an agent forging the
        # file). The drain runs as the daemon's first tick stage —
        # between agent runs — so the bump here is safe. We bump once
        # to the highest TB-N seen across this drain pass; sequential
        # drains compound naturally because each reads CLAUDE.md fresh
        # via `cfg.next_task_id`.
        if highest_alloc and applied_count:
            new_next = max(highest_alloc + 1, cfg.next_task_id)
            claude_md = cfg.project_root / "CLAUDE.md"
            if claude_md.exists():
                bump_next_task_id(claude_md, new_next)
            cfg.next_task_id = new_next

    if applied_count:
        events.append(
            cfg.events_file,
            "operator_queue_drained",
            applied=applied_count,
        )
    return {"applied": applied_count, "touched_paths": sorted(touched)}


def _apply_operator_op(cfg: Config, board: Board, rec: dict) -> None:
    op = rec.get("op", "")
    args = rec.get("args") or {}
    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }
    if op in add_map:
        if not args.get("task_id") or not args.get("title"):
            raise RuntimeError("add op missing task_id or title")
        board.add(
            add_map[op],
            task_id=args["task_id"],
            title=args["title"],
            tags=list(args.get("tags") or []),
            # TB-132: meta dict carries the `@blocked:...` codespan (and
            # any future `@<key>:<value>` structured fields). Defaults
            # to {} for queued ops authored before TB-132 landed.
            meta=dict(args.get("meta") or {}),
            description=args.get("description") or "",
            briefing=args.get("briefing_path"),
        )
        return
    if op == "move_to_backlog":
        try:
            board.move(args["task_id"], "Backlog")
        except KeyError:
            raise RuntimeError(f"{args.get('task_id', '?')} not on board")
        return
    if op == "unfreeze":
        loc = board.find(args.get("task_id", ""))
        if loc is None:
            raise RuntimeError(f"{args.get('task_id', '?')} not on board")
        if loc[0] != "Frozen":
            raise RuntimeError(
                f"{args['task_id']} is in {loc[0]}, not Frozen"
            )
        board.move(args["task_id"], "Backlog")
        retry.reset_attempt(cfg.retry_state_file, args["task_id"])
        events.append(cfg.events_file, "task_unfrozen", task=args["task_id"])
        return
    if op == "delete":
        loc = board.find(args.get("task_id", ""))
        if loc is None:
            raise RuntimeError(f"{args.get('task_id', '?')} not on board")
        section = loc[0]
        if section in ("Active", "Ready", "Pipeline Pending") and not args.get("force"):
            raise RuntimeError(
                f"{args['task_id']} is in {section}; refusing delete without force"
            )
        existing = board.get(args["task_id"])
        title = existing.title if existing else ""
        board.remove(args["task_id"])
        events.append(
            cfg.events_file,
            "task_deleted",
            task=args["task_id"],
            section=section,
            title=title,
        )
        return
    raise RuntimeError(f"unknown op {op!r}")


def _append_operator_audit_line(cfg: Config, rec: dict) -> None:
    """One-line audit entry to operator_log.md per TB-131 scope (5)."""
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements. Append-only.\n"
            "Ideation reads this in Step 0; logged items are authoritative —\n"
            "ideation won't re-propose decisions logged here._\n\n"
        )
    op = rec.get("op", "?")
    args = rec.get("args") or {}
    task = args.get("task_id", "")
    ts = rec.get("ts") or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    arrow = f" → {task}" if task else ""
    line = f"- {ts} — applied operator-queued {op}{arrow}\n"
    with log_path.open("a") as f:
        f.write(line)


def _load_operator_queue_applied(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    items = data.get("applied")
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items}


def _save_operator_queue_applied(state_path: Path, applied: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"applied": sorted(applied)}, indent=2))
    tmp.replace(state_path)


def _compact_operator_queue(queue_path: Path, applied: set[str]) -> None:
    """Rewrite the queue file dropping fully-applied uuids, keeping any
    un-applied lines (e.g. ones that arrived between two drains) intact.

    Called after each successful drain so the file doesn't grow
    unbounded. `applied` is the set of uuids known to have been applied
    (or attempted-and-recorded); anything not in it is preserved.
    """
    if not queue_path.exists():
        return
    pending_lines: list[str] = []
    for raw in queue_path.read_text().splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Preserve unparseable lines so an operator can inspect
            # them rather than silently losing the record.
            pending_lines.append(line)
            continue
        if rec.get("uuid") in applied:
            continue
        pending_lines.append(line)
    if pending_lines:
        queue_path.write_text("\n".join(pending_lines) + "\n")
    else:
        queue_path.write_text("")


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


def do_mattermost_reply(cfg: Config, args: dict) -> dict:
    channel = args.get("channel") or ""
    text = args.get("text") or ""
    thread_id = args.get("thread_id") or ""
    if not channel or not text:
        return _err("channel and text are required")
    try:
        post_id = _mm_post(channel, text, thread_id)
        events.append(
            cfg.events_file,
            "mattermost_reply",
            channel=channel,
            thread_id=thread_id,
            post_id=post_id,
            summary=text[:200],
        )
        return _ok(f"posted to {channel}", post_id=post_id)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def _mm_post(channel: str, text: str, thread_id: str = "") -> str:
    url = os.environ.get("MATTERMOST_URL")
    token = os.environ.get("MATTERMOST_TOKEN")
    if not url or not token:
        raise RuntimeError("MATTERMOST_URL and MATTERMOST_TOKEN must be set")
    # Resolve channel name → id if needed (names start without alnum restriction,
    # but IDs are 26-char base32). Best-effort: treat 26-char as id.
    channel_id = channel if len(channel) == 26 and channel.isalnum() else _mm_lookup_channel(url, token, channel)
    body = {"channel_id": channel_id, "message": text}
    if thread_id:
        body["root_id"] = thread_id
    req = urllib.request.Request(
        f"{url}/api/v4/posts",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("id", "")


def _mm_lookup_channel(url: str, token: str, name: str) -> str:
    name = name.lstrip("#")
    # Need a team id; we pick the user's first team as a default.
    team_id = _mm_user_team(url, token)
    req = urllib.request.Request(
        f"{url}/api/v4/teams/{team_id}/channels/name/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read())["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"channel {name!r} not found: {e}") from e


_TEAM_CACHE: str | None = None


def _mm_user_team(url: str, token: str) -> str:
    global _TEAM_CACHE
    if _TEAM_CACHE:
        return _TEAM_CACHE
    req = urllib.request.Request(
        f"{url}/api/v4/users/me/teams",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        teams = json.loads(resp.read())
    if not teams:
        raise RuntimeError("user has no mattermost teams")
    _TEAM_CACHE = teams[0]["id"]
    return _TEAM_CACHE


# ---------------- SDK wiring ----------------


def build_mcp_server(cfg: Config):
    """Build the in-process MCP server exposing the custom tools.

    Imported lazily so unit tests don't need the SDK.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

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
        "Add, remove, or update a scheduled cron job.",
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

    @tool(
        "mattermost_reply",
        "Send a message to a Mattermost channel or thread.",
        {"channel": str, "text": str, "thread_id": str},
    )
    async def mattermost_reply(args):
        return do_mattermost_reply(cfg, args)

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
        "Append a timestamped operator-decision line to "
        ".cc-autopilot/operator_log.md (TB-106). Use ONLY for "
        "operator-mediated messages — e.g. when an operator says "
        "`@claude-bot done: <action>` or `@claude-bot decided: <choice>`. "
        "Args: note (required, one sentence), task_id (optional TB-N). "
        "Ideation reads this log in Step 0 and treats entries as "
        "authoritative; logged decisions are not re-proposed.",
        {"note": str, "task_id": str},
    )
    async def operator_log_append(args):
        return do_operator_log_append(cfg, args)

    @tool(
        "operator_queue_append",
        "Stage an operator board op for the daemon to apply at the next "
        "tick (TB-131). Routes around the rollback / read-stale-board race "
        "that direct `board_edit` exposes during in-flight task or ideation "
        "runs: queued ops aren't in HEAD until between runs, so "
        "`git reset --hard <pre_run_head>` rollback never wipes them and "
        "long-running SDK turns can't read a board snapshot that shifts "
        "underneath them. Use this — instead of `board_edit` — when "
        "@claude-bot is asked to add/move/unfreeze/delete a task and a "
        "task agent is currently active. For `add_*` ops, the TB-N ID is "
        "pre-allocated synchronously (so you can mention it in your reply) "
        "and the briefing file is pre-written; only the TASKS.md "
        "insertion is deferred. Args: op (one of add_ready, add_backlog, "
        "add_frozen, move_to_backlog, unfreeze, delete); task_id (TB-N for "
        "non-add ops); title / tags (comma-separated string) / description "
        "/ briefing / blocked_on (for add ops); force (true/false, for "
        "delete from Active/Ready/Pipeline Pending).",
        {
            "op": str,
            "task_id": str,
            "title": str,
            "tags": str,
            "description": str,
            "briefing": str,
            "blocked_on": str,
            "force": str,
        },
    )
    async def operator_queue_append(args):
        # Normalize string-shaped args to the dict shape do_operator_queue_append
        # expects: tags is a comma-separated string here but a list inside.
        normalized = dict(args)
        raw_tags = normalized.get("tags") or ""
        if isinstance(raw_tags, str) and raw_tags.strip():
            normalized["tags"] = [
                t.strip() for t in raw_tags.split(",") if t.strip()
            ]
        else:
            normalized["tags"] = []
        force = normalized.get("force")
        if isinstance(force, str):
            normalized["force"] = force.strip().lower() in ("1", "true", "yes")
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
        "mutate cron.yaml directly. Symmetric with control agents' "
        "`cron_edit` (which DOES mutate, but is unavailable to task agents). "
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

    return create_sdk_mcp_server(
        name="autopilot",
        version=version,
        tools=[
            board_edit,
            cron_edit,
            mattermost_reply,
            log_event,
            daemon_control,
            ideation_state_write,
            git_log_grep,
            operator_log_append,
            operator_queue_append,
            report_result,
            cron_propose,
            pipeline_task_start,
        ],
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
    "mcp__autopilot__cron_edit",
    "mcp__autopilot__mattermost_reply",
    "mcp__autopilot__log_event",
    "mcp__autopilot__daemon_control",
    "mcp__autopilot__ideation_state_write",
    "mcp__autopilot__git_log_grep",
    "mcp__autopilot__operator_log_append",
    # TB-131: queue-based board mutation. The MM handler should prefer
    # this over `board_edit` when a task agent is in flight — see the
    # tool docstring + MM_HANDLER_TOOLS_RESTRICTED below.
    "mcp__autopilot__operator_queue_append",
]

# TB-122: when a task agent is in flight, the Mattermost handler runs with a
# narrower toolset. Cron schedule mutations (would change when the next
# status-report / ideation tick fires, possibly mid-edit on the running
# task's working tree) and ideation_state_writes (rewrites the per-cycle
# assessment that ideation was acting on when the running task was queued)
# are deferred until the daemon is idle. The keeps:
#   - read tools (Read/Glob/Grep/git_log_grep) so the agent can answer
#     questions and reason about state.
#   - board_edit so the operator can pause queued work, add new tasks,
#     delete unwanted ones, freeze problematic ones, and approve
#     ideation-proposed tasks (TB-121's `approve` action) mid-flight.
#   - mattermost_reply / log_event so the handler can finish its turn.
#   - daemon_control so "@claude-bot pause" works while a task runs (the
#     existing semantic: pause takes effect on the next tick boundary; the
#     in-flight task continues to completion, then no further dispatch).
#   - operator_log_append so "@claude-bot ack: …" still lands in the
#     operator log (ideation reads it in Step 0 — the operator's veto
#     channel must stay open even mid-task).
# Idle handler runs (no Active tasks) keep the full CONTROL_AGENT_TOOLS set.
MM_HANDLER_TOOLS_FULL = list(CONTROL_AGENT_TOOLS)
MM_HANDLER_TOOLS_RESTRICTED = [
    t for t in CONTROL_AGENT_TOOLS
    if t not in (
        "mcp__autopilot__cron_edit",
        "mcp__autopilot__ideation_state_write",
    )
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
#   - Daemon-owned config: cron.yaml (control agents edit via cron_edit).
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
    # TB-141: `operator_queue.jsonl` is intentionally NOT fenced.
    # Pre-TB-141 it was — but any operator `ap2 add` (or unfreeze /
    # delete / backlog) issued during a task run wrote to it
    # synchronously, which made TB-110's post-hoc snapshot check
    # mis-attribute the operator's mutation as an agent fence
    # violation and roll back legitimate work (TB-139 hit this on
    # 2026-05-01). The fence had zero security value here: agents
    # have no MCP path that mutates the queue, no `Edit`/`Write`
    # access, and even a `Bash`-shell write wouldn't drain because
    # the daemon's `drain_operator_queue` only applies records whose
    # uuid round-trips through `operator_queue_state.json`.
    ".cc-autopilot/operator_queue_state.json",
)
