"""Operator-queue append + drain (TB-131 / TB-141 / TB-142 / TB-143 et al).

Hosts the queue-append boundary (`do_operator_queue_append`), the tick-
boundary drain (`drain_operator_queue` + `_apply_operator_op`), the
operator-log audit-line writer (`_append_operator_audit_line`), and the
applied-uuid bookkeeping helpers. Also owns the cross-process TB-N
allocator (`_allocate_id` + `_max_preallocated_id_in_queue`) and the
`approve`-op review-token stripper (`_approve_review_token`) — both
shared by the synchronous `do_board_edit` path.

Moved out of `ap2/tools.py` by TB-262: the queue-append / drain pair
(plus its applied-state bookkeeping and the audit-log writer) is one
coherent surface — every TB-N touching the operator-queue mid-task
fence (`task_state_violation` false-positives, `update_goal` / `ack` /
`classify` / `audit_skip` retrofits) loaded the full 224KB `tools.py`
when only the queue surface needed editing.

Public symbols (still re-exported from `ap2.tools` for backward compat):
- Path helpers: `operator_queue_path`, `operator_queue_state_path`.
- Op vocabulary: `OPERATOR_QUEUE_OPS`.
- Allocator + approve helpers (shared with `board_edits.do_board_edit`):
  `_allocate_id`, `_max_preallocated_id_in_queue`,
  `_APPROVE_LEGACY_REVIEW_RE`, `_approve_review_token`.
- Append + drain: `do_operator_queue_append`, `drain_operator_queue`,
  `_apply_operator_op`, `_append_operator_audit_line`.
- Applied-state bookkeeping: `_load_operator_queue_applied`,
  `_save_operator_queue_applied`, `_compact_operator_queue`.
- Drain-time `ack` handler: `_apply_operator_ack`, `enqueue_operator_ack`.
- Surface readers: `operator_queue_pending_count`,
  `classifications_last_30d_by_verdict`.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import uuid as _uuid
from pathlib import Path
from typing import Any

from . import events, retry
from .board import Board, board_file_lock, locked_board, parse_task_line
from .briefing_validators import (
    IMPACT_VERDICTS,
    _atomic_write_json,
    _goal_md_anchors_from_text,
    _validate_briefing_structure,
    _validate_single_line,
    _validate_update_args,
    proposal_record_path,
    reconcile_proposal_outcome,
)
from .config import Config, bump_next_task_id
# `_ok` / `_err` / `slugify` live in `ap2/tools.py`; that module imports
# this one for re-export, so the cross-reference works via Python's
# standard partial-import resolution (tools.py defines the helpers
# BEFORE importing this module).
from .tools import _err, _ok, slugify


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


# TB-142 (TB-121 cross-ref): the `approve` semantic strips the `review`
# blocker token from a task — both the structural `@blocked:review`
# codespan (TB-132's metadata surface) and any legacy `(blocked on:
# review)` description prose authored before TB-132 landed. Idempotent
# re-render: a task already free of the review token rewrites identically
# (modulo the legacy-description scrub). Shared by:
#   - `do_board_edit({"action":"approve",...})` — the idle-path entry,
#     used by the MM handler's FULL toolset and by direct CLI/control
#     callers.
#   - `_apply_operator_op` for queued `op="approve"` records — the
#     in-flight-task path, where the MM handler RESTRICTED toolset routes
#     through `operator_queue_append` to side-step TB-110's snapshot
#     check (drains run between agent runs, never during).
_APPROVE_LEGACY_REVIEW_RE = re.compile(
    r"\s*\(blocked on:\s*review\s*\)\s*", re.IGNORECASE,
)


def _approve_review_token(board: Board, task_id: str) -> "Task":  # type: ignore[name-defined]
    """Strip the `review` blocker from a task's `@blocked:` codespan AND
    any legacy `(blocked on: review)` description prose. Mutates `board`
    in place. Idempotent — a task without the review token rewrites to
    its current state minus the legacy description clause (cosmetic).

    Raises RuntimeError if the task is not on the board, or if the line
    fails to parse (malformed_lines case — should never happen for tasks
    Board.find returns).
    """
    loc = board.find(task_id)
    if loc is None:
        raise RuntimeError(f"{task_id} not on board")
    section, idx = loc
    line = board.sections[section][idx]
    t = parse_task_line(line, section)
    if t is None:
        raise RuntimeError(f"{task_id}: malformed task line")

    # Codespan: drop the `review` token (case-insensitive). If it was the
    # only token, drop the `@blocked:` codespan entirely so Task.render
    # emits a clean line with no leftover empty span.
    blocked = t.meta.get("blocked", "")
    if blocked:
        kept = [
            tok.strip()
            for tok in blocked.split(",")
            if tok.strip() and tok.strip().lower() != "review"
        ]
        if kept:
            t.meta["blocked"] = ",".join(kept)
        else:
            t.meta.pop("blocked", None)

    # Legacy `(blocked on: review)` description prose — TB-132 moved
    # blockers off description-regex onto codespans, but pre-TB-132 tasks
    # still in flight may carry the prose form. Stripping it keeps the
    # rendered description tidy; structurally it's already a no-op since
    # TB-132 (the legacy fallback only fires when no codespan is set).
    new_desc = _APPROVE_LEGACY_REVIEW_RE.sub(" ", t.description).strip()
    # Normalize whitespace runs that the substitution left behind.
    new_desc = re.sub(r"\s{2,}", " ", new_desc).strip()
    t.description = new_desc

    board.sections[section][idx] = t.render()
    return t


def _apply_operator_ack(cfg: Config, args: dict) -> dict:
    """Apply a queued `ack` op at drain time — append a timestamped
    operator-decision line to `.cc-autopilot/operator_log.md` (TB-106).

    Operator-owned channel for decisions ideation can't observe via the
    filesystem (e.g. "decided to keep FRAGILE plists as references" or
    "considered the universe-expansion question, deferred"). Ideation
    reads the log in Step 0 and treats logged items as authoritative —
    won't re-propose them in subsequent cycles.

    TB-201: this is a drain-only internal helper. The two write paths
    (`ap2 ack` CLI + `operator_log_append` MCP tool) used to call this
    function synchronously, which wrote operator_log.md mid-task and
    tripped TB-110's post-hoc fenced-file snapshot check — burning real
    SDK cost on false-positive rollbacks. They now route through the
    operator queue (`enqueue_operator_ack`); only `drain_operator_queue`
    invokes this helper, which runs at tick boundary under the daemon's
    board lock and never races a task agent's snapshot window.

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

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tb_tag = f" [{task_id}]" if task_id else ""
    line = f"- {ts}{tb_tag} — {note}\n"
    with log_path.open("a") as f:
        f.write(line)

    payload: dict = {"note": note[:500]}
    if task_id:
        payload["task"] = task_id
    events.append(cfg.events_file, "operator_ack", **payload)

    # TB-226: if the ack carries the `roadmap_complete` token, also
    # bump the focus-pointer's forensic `roadmap_complete_ack_idx` to
    # the current focus-list length so `ap2 status` / web UI render
    # the cleared state without an events-scan side-effect. Best-
    # effort: `goal.roadmap_exhausted` (the dispatch-path gate) also
    # consults the events.jsonl token directly, so a pointer-write
    # failure here doesn't change the cleared verdict — defense in
    # depth, not the canonical authority.
    from ap2 import goal as _goal
    if _goal.ROADMAP_COMPLETE_ACK_TOKEN in note:
        try:
            pointer = _goal.load_pointer(cfg)
            foci = _goal.read_focus_list(cfg)
            pointer["roadmap_complete_ack_idx"] = len(foci)
            _goal.save_pointer(cfg, pointer)
        except OSError:
            pass

    return _ok(f"appended to {log_path.name}", line=line.strip())


def enqueue_operator_ack(cfg: Config, args: dict) -> dict:
    """Queue an operator ack for the daemon to apply at the next tick
    (TB-201). Mirrors the TB-189 `classify` / TB-193 `update_goal`
    retrofits — surface-vs-apply split.

    Shared by both ack write paths post-TB-201:
      - operator-side: `ap2 ack [-t TB-N] "<note>"` (CLI)
      - mattermost-handler-side: `operator_log_append` MCP tool when the
        operator sends `@claude-bot done: ...` style messages.

    Validates `note` (required) and forwards to
    `do_operator_queue_append` with `op="ack"`. The synchronous
    operator_log.md write that used to fire here is deferred to drain
    time in `_apply_operator_ack` — that's the whole point of the
    routing change. Pre-TB-201 the synchronous write would land
    mid-task and trip TB-110's post-hoc fenced-file snapshot check,
    rolling back legitimate task work and burning the SDK cost (the
    bug was demonstrated live at 2026-05-12T06:40-07:14Z on post-train:
    three runs / ~$12.55 lost to false-positive rollbacks).
    """
    note = str(args.get("note") or "").strip()
    if not note:
        return _err("note is required")
    task_id = str(args.get("task_id") or "").strip()
    return do_operator_queue_append(
        cfg, {"op": "ack", "note": note, "task_id": task_id}
    )


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
    # TB-142: approving an ideation-proposed task (strip `@blocked:review`)
    # is the second mutation surface the MM handler exposes via chat
    # commands (`@claude-bot approve TB-N`). Routing it through the queue
    # closes the second instance of the false-positive
    # `task_state_violation` class — TB-141 closed the operator-side `ap2
    # add` instance; this closes the chat-driven `board_edit({"action":
    # "approve",...})` instance. Drain-side handler shares the
    # `_approve_review_token` helper with `do_board_edit`.
    "approve",
    # TB-153: in-place edit of an existing task's `title` / `tags` /
    # `@blocked` codespan / `description` and/or its briefing file.
    # Routed through the same queue-drain path as `add_*` / `delete` /
    # `unfreeze` / `approve` so it never lands inside a task agent's
    # snapshot window. Preserves TB-N (vs. delete + re-add which would
    # orphan every prior reference) and the briefing's slug-stable
    # filename (vs. allocating a new slug, which would orphan git
    # history of `.cc-autopilot/tasks/<slug>.md`).
    "update",
    # TB-152: explicit operator rejection of an ideation-proposed task.
    # Removal semantics mirror `delete` (drop the row + briefing file +
    # emit `task_deleted`) but the audit trail is richer: the drain-side
    # writes `<ts> — rejected ideation proposal → TB-N (<title>):
    # <reason>` to operator_log.md so ideation Step 0 has a signal to
    # avoid re-proposing the same idea next cycle. The `delete` verb
    # remains the generic "remove a task" path; `reject` is specifically
    # "I considered this ideation proposal and decided against it." Pre-
    # validation in `cmd_reject` / chat-side limits the verb to
    # Backlog + `@blocked:review` tasks (ideation proposals); other
    # sections route the operator at `ap2 delete`.
    "reject",
    # TB-159: manual operator trigger for an ideation pass that bypasses
    # the natural empty-board / cooldown / `AP2_IDEATION_DISABLED`
    # gates. Routed through the queue (rather than CLI-spinning its own
    # SDK) so the daemon stays the single owner of the control-agent
    # SDK slot. The drain-side does NOT invoke ideation directly (that
    # would block the board lock for minutes); instead it records an
    # `ideation_forced` event and signals via `drain_operator_queue`'s
    # return dict that the daemon should run `force_ideate` on this
    # tick after the drain completes. TB-194: the queue-append handler
    # has NO board-state read for `ideate` — Active-emptiness is a
    # loop-topology invariant by drain time (the prior `_tick`'s
    # synchronous `run_task` cleared Active back to Complete/Backlog/
    # Frozen before the next `_tick`'s drain stage runs) and `_tick`
    # sequences the post-drain `force_ideate` SDK call before any new
    # task dispatch, so the previously-feared "concurrent task-agent +
    # control-agent SDK runs" interleaving is unreachable. The `force`
    # arg is preserved on the queue payload as audit metadata only.
    "ideate",
    # TB-193: full-file replacement of `goal.md`. Routed through the
    # queue (rather than letting the operator edit goal.md directly
    # while the daemon is running) because ideation reads goal.md
    # mid-cycle (anchors injected into the prompt; `_goal_md_anchors`
    # consulted by `_validate_briefing_structure` at queue-append time
    # for TB-161), and the per-task verifier (TB-69) reads it as part
    # of the rollback-cohesion state surface — an in-place edit racing
    # a snapshot-window write tears against any of those readers. The
    # op carries the new file content + an optional reason; the drain-
    # side performs an atomic tmpfile + `os.replace` write under
    # `board_file_lock` and lands the change in the next `state:
    # drained N operator op(s)` commit. Operator-CLI-only by design —
    # the MM-handler `operator_queue_append` MCP wrapper refuses this
    # op (same precedent as `cron_edit` / `board_edit` being CLI-only
    # post-TB-146 / TB-145). `prompts.py` already documents the design
    # intent: handlers that think goal.md needs updating raise the
    # recommendation in their RESULT summary; the operator applies via
    # `ap2 update-goal`.
    "update_goal",
    # TB-189: operator-authored retrospective verdict on a shipped
    # proposal. The op carries `task_id`, `verdict` (one of
    # `IMPACT_VERDICTS`), and an optional `reason`. The drain-side
    # handler appends an `impact` block to the per-proposal record from
    # TB-188 (`.cc-autopilot/ideation_proposals/<TB-N>.json`) AND writes
    # the standard audit-line to operator_log.md (`<ts> — classified
    # TB-N impact=<verdict>: <reason>`). Tolerates missing record file
    # (legacy proposals from before TB-188 landed): the operator_log
    # line is the authoritative trail; the per-proposal record is the
    # structured signal feeding ideation's later track-record block.
    # Goal anchor: this is the operator-authored signal stream goal.md
    # L61-76 names — the strongest signal in the focus items signal-
    # collection program because the operator IS the source of truth
    # for the impact verdict. No LLM auto-classification path by
    # design.
    "classify",
    # TB-201: operator decision-log append (the `ap2 ack` / chat
    # `@claude-bot done:` / `@claude-bot decided:` surface). Was
    # `do_operator_log_append` writing operator_log.md synchronously
    # — but operator_log.md is in TASK_AGENT_FENCED_PATHS (and NOT in
    # rollback._VIOLATION_CHECK_EXCLUDED_PATHS), so a mid-task ack
    # tripped TB-110's post-hoc snapshot check and rolled back the
    # task's legitimate work, burning the SDK cost. Routing through
    # the queue defers the write to drain time (tick boundary, under
    # the daemon's board lock); a task agent's snapshot window never
    # encloses the operator_log.md write again. The drain-side
    # `_apply_operator_ack` performs the actual append + emits
    # `operator_ack`; the standard `applied operator-queued ack →
    # TB-N` audit line is ALSO emitted (so a single ack produces two
    # lines: the operator's rich note and the verb-vs-other-ops
    # audit pointer). Mirrors TB-189 (`classify`) / TB-193
    # (`update_goal`) retrofit shape.
    "ack",
    # TB-248: operator-CLI-only `audit-skip` op recorded during an
    # `ap2 audit --interactive` walk when the operator wants to mark
    # a shipped task as "I looked at this but have no verdict to
    # record right now" without rolling back / classifying. The op
    # carries `task_id` (required) and `reason` (optional, single-
    # line). Drain-side handler is metadata-only — no board
    # mutation — and emits the richer `<ts> — audit-skipped TB-N:
    # <reason>` line via `_append_operator_audit_line` so the audit
    # state-derivation grep (the reviewed-set parser in
    # `ap2/audit.py`) picks it up alongside `classified TB-N` and
    # `rejected TB-N` entries as a third "operator has weighed in"
    # signal. Routed through the queue (rather than written
    # synchronously by the CLI) for the same reason TB-201
    # retrofitted `ack`: operator_log.md is fenced + protected by
    # `board_file_lock` at drain time, so a CLI-side mid-task write
    # would race the daemon's own writes. Symmetric to `ack` /
    # `classify` / `reject` audit shape.
    "audit_skip",
)


def operator_queue_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"


def operator_queue_state_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "operator_queue_state.json"


def do_operator_queue_append(cfg: Config, args: dict) -> dict:
    """Append an operator board op to the daemon-drained queue (TB-131).

    Two write paths share this handler:
      - operator-side: `ap2 add` / `ap2 backlog` / `ap2 unfreeze` /
        `ap2 delete` / `ap2 ack` (TB-201) route here instead of
        mutating TASKS.md / operator_log.md directly.
      - MM-handler-side: the `operator_queue_append` MCP tool — for
        when @claude-bot is asked to add/move/unfreeze/delete a task
        during an in-flight run, where direct `board_edit` exposes the
        change to `git reset --hard <pre_run_head>` rollback. The
        TB-201 `operator_log_append` MCP tool also funnels here (op=ack).

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

        # TB-154: structural gate. Runs before `_allocate_id` /
        # briefing-file write — a rejected add must not leak a TB-N
        # nor materialize an orphan briefing under `.cc-autopilot/tasks/`.
        # TB-161: also passes `goal_md_path` so the goal-anchor check
        # fires here (queue-append-time hard gate).
        # TB-170: `skip_goal_alignment=True` (operator-CLI-only) skips
        # the TB-161 + TB-164 goal-alignment gates while running every
        # other check unchanged. The flag rides on the queue payload
        # so the drain side can re-validate symmetrically.
        skip_goal_alignment = bool(args.get("skip_goal_alignment"))
        # TB-235: feed `description`, `blocked_csv`, and `events_file`
        # so check #7 (LLM-judge dependency coherence) gates this
        # primary queue-append surface — ideation, MM handler, and
        # operator CLI all reach the validator through here.
        struct_err = _validate_briefing_structure(
            briefing_content or "",
            goal_md_path=cfg.project_root / "goal.md",
            skip_goal_alignment=skip_goal_alignment,
            description=description,
            blocked_csv=blocked_on,
            events_file=cfg.events_file,
        )
        if struct_err:
            return _err(struct_err)

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
            # TB-170: persist the operator's bypass intent on the queue
            # record so the drain-side audit line can decorate the
            # `applied operator-queued add_backlog → TB-N` line with
            # `(goal-alignment check skipped)` when set. Default-false
            # preserves the historical record shape — only operator-CLI
            # adds with `--skip-goal-alignment` carry the flag.
            if skip_goal_alignment:
                rec_args["skip_goal_alignment"] = True
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
    elif op == "ideate":
        # TB-159 / TB-194: manual ideation trigger. The op carries no
        # task_id — append-time validation is intentionally minimal
        # (no board-state read). The drain-side does NOT invoke
        # ideation (that would hold the board lock for minutes); it
        # only emits the `ideation_forced` audit event and signals the
        # daemon to run `force_ideate` after the drain completes.
        #
        # TB-194: the prior at-append-time Active hard gate (with
        # `force=true` as escape hatch) has been removed. The
        # rationale was guarding "concurrent task-agent + control-
        # agent SDK runs share the same in-process slot", but the
        # interleaving is benign by current loop topology: the drain
        # runs as `_tick`'s first stage, BEFORE task dispatch, AFTER
        # the previous tick's synchronous `run_task` already cleared
        # Active back to Complete/Backlog/Frozen. The post-drain
        # `force_ideate` SDK call also runs within the same `_tick`,
        # sequentially before task dispatch — there's no path for it
        # to overlap a task-agent SDK run on the same loop. The
        # `force` arg is captured on the queue payload as audit-only
        # metadata (kept for one release; deprecation can come later
        # if the noise accumulates).
        force = bool(args.get("force"))
        rec_args = {"force": force}
    elif op == "classify":
        # TB-189: operator-authored retrospective verdict on a shipped
        # proposal. The op carries `task_id`, `verdict` (one of
        # `IMPACT_VERDICTS`), and an optional `reason`. No board-state
        # mutation — this is a metadata-only op (writes the per-proposal
        # record's `impact` block + an operator_log.md line at drain
        # time). We DO snapshot-validate the task_id is on the board so
        # an obviously wrong TB-N is rejected immediately at append
        # time; the verb is meaningful regardless of section (the
        # proposal may be Complete, Frozen post-failure, etc.) so we
        # don't gate on section.
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required for classify")
        verdict = (args.get("verdict") or "").strip()
        if verdict not in IMPACT_VERDICTS:
            return _err(
                f"verdict must be one of {list(IMPACT_VERDICTS)}; "
                f"got {verdict!r}"
            )
        raw_reason = args.get("reason")
        reason = (raw_reason if raw_reason is not None else "").strip()
        if reason:
            err = _validate_single_line("reason", reason)
            if err:
                return _err(err)
        # Snapshot validation under the board lock — symmetry with
        # other task-keyed ops. The drain-side tolerates a missing
        # per-proposal record (legacy / non-ideation tasks); but a
        # totally unknown TB-N at append time is operator error and
        # we surface it now.
        with board_file_lock(cfg.tasks_file):
            board = Board.load(cfg.tasks_file)
            loc = board.find(task_id)
        if loc is None:
            return _err(f"{task_id} not on board")
        rec_args = {
            "task_id": task_id,
            "verdict": verdict,
            "reason": reason,
        }
    elif op == "update_goal":
        # TB-193: full-file replacement of `goal.md`. The op carries the
        # full file content (no diff/patch — symmetric to how `add_*` ops
        # carry the full briefing payload, atomic-write semantics are
        # simpler than 3-way merge, and goal.md is small enough that the
        # size cost is negligible). `reason` is optional, single-line per
        # TB-134, and feeds the operator-log audit line.
        goal_content = args.get("goal_content")
        if not isinstance(goal_content, str) or not goal_content.strip():
            return _err(
                "goal_content is required for update_goal (non-empty "
                "string; whitespace-only is rejected)"
            )
        # Parser sanity-check: a goal.md whose anchor extraction blows up
        # would silently break TB-161 / ideation prompts later. Empty
        # anchor list is OK — placeholder goal.md is a documented valid
        # state per `check.py:226-271`; a parser exception is not.
        try:
            _goal_md_anchors_from_text(goal_content)
        except Exception as e:  # noqa: BLE001
            return _err(
                f"goal_content failed to parse "
                f"({type(e).__name__}: {e}); refusing to queue"
            )
        raw_reason = args.get("reason")
        reason = (raw_reason if raw_reason is not None else "").strip()
        if reason:
            err = _validate_single_line("reason", reason)
            if err:
                return _err(err)
        rec_args = {
            "goal_content": goal_content,
            "reason": reason,
        }
    elif op == "ack":
        # TB-201: operator decision-log append. Routed through the
        # queue (rather than letting the CLI / MCP tool body write
        # operator_log.md synchronously) because operator_log.md is in
        # TASK_AGENT_FENCED_PATHS and NOT in
        # `rollback._VIOLATION_CHECK_EXCLUDED_PATHS`, so a mid-task
        # ack would trip TB-110's post-hoc snapshot check and roll
        # back the task agent's legitimate work — burning real SDK
        # cost (the failure mode was demonstrated live on post-train
        # at 2026-05-12T06:40-07:14Z: three runs / ~$12.55 lost).
        # The op carries the operator's `note` (required) and an
        # optional `task_id` reference; the drain-side handler
        # `_apply_operator_ack` performs the actual operator_log.md
        # write + emits `operator_ack` at tick boundary, under the
        # daemon's board lock. The `note` field is treated as opaque
        # prose — no `_validate_single_line` (an ack genuinely can be
        # a paragraph; the historical synchronous write did not gate
        # on shape either).
        note = (args.get("note") or "").strip()
        if not note:
            return _err("note is required for ack")
        task_id = (args.get("task_id") or "").strip()
        rec_args = {"note": note, "task_id": task_id}
    elif op == "audit_skip":
        # TB-248: `[s]kip` action inside `ap2 audit --interactive`.
        # Metadata-only at drain time — no board mutation; the verb
        # records "operator considered this task and chose not to
        # classify right now" so the audit reviewed-set grep
        # (`ap2/audit.py::parse_reviewed_set`) skips it on the next
        # `ap2 audit` walk. Snapshot-validate `task_id` is on the
        # board (Complete + Frozen tasks are the audit targets;
        # but `find()` matches every section so we don't gate on
        # section here — operator may legitimately skip a still-
        # Active task if their audit walk catches it mid-run).
        # Reason is optional + single-line per TB-134; empty reason
        # collapses to `(no reason given)` at drain time, mirroring
        # the reject branch's signal-vs-silence distinction.
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required for audit_skip")
        raw_reason = args.get("reason")
        reason = (raw_reason if raw_reason is not None else "").strip()
        if reason:
            err = _validate_single_line("reason", reason)
            if err:
                return _err(err)
        with board_file_lock(cfg.tasks_file):
            board = Board.load(cfg.tasks_file)
            loc = board.find(task_id)
        if loc is None:
            return _err(f"{task_id} not on board")
        rec_args = {"task_id": task_id, "reason": reason}
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
            existing = board.get(task_id) if loc else None
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
        if op == "reject":
            # TB-152: `reject` is the explicit "operator considered this
            # ideation proposal and decided against it" path. The verb is
            # narrower than `delete` by design — it only fires on
            # Backlog tasks with the `@blocked:review` codespan still
            # present, i.e. unapproved ideation proposals. Anything else
            # (Active runs, Ready dispatches, already-approved tasks,
            # Frozen failures) routes the operator at `ap2 delete`,
            # which carries the generic remove semantics. This keeps
            # the audit-line distinction clean: `rejected ideation
            # proposal → TB-N: <reason>` only ever describes a real
            # ideation rejection.
            blocked_csv = (existing.meta.get("blocked", "") if existing else "")
            blocked_tokens = [
                tok.strip().lower() for tok in blocked_csv.split(",") if tok.strip()
            ]
            if section != "Backlog" or "review" not in blocked_tokens:
                return _err(
                    f"{task_id} is not a pending-review proposal "
                    f"(section={section}, "
                    f"@blocked={blocked_csv or '(none)'}) — "
                    f"use `ap2 delete {task_id}` instead. `reject` is "
                    f"reserved for Backlog tasks still gated by "
                    f"`@blocked:review` (ideation proposals)."
                )
        rec_args = {"task_id": task_id}
        if op == "delete":
            rec_args["force"] = bool(args.get("force"))
        if op == "reject":
            # TB-152: snapshot the title under the board lock so the
            # drain-side audit line ("<ts> — rejected ideation proposal
            # → TB-N (<title>): <reason>") doesn't have to re-look it
            # up after `board.remove` has dropped the row. Reason is
            # single-line per TB-134; the placeholder `(no reason
            # given)` is itself a signal — ideation can spot the
            # difference between rejected-with-reason and rejected-
            # silently and decide whether to re-propose.
            raw_reason = args.get("reason")
            reason = (raw_reason if raw_reason is not None else "").strip()
            if reason:
                err = _validate_single_line("reason", reason)
                if err:
                    return _err(err)
            else:
                reason = "(no reason given)"
            rec_args["title"] = existing.title if existing else ""
            rec_args["reason"] = reason
        if op == "update":
            # TB-153: in-place edit. Translate the public CLI / MCP shape
            # (title / tags / blocked / description / briefing flags +
            # explicit `clear_tags` / `clear_blocked`) into the queue
            # record's update_kwargs dialect (title / tags / description /
            # briefing / meta_set / meta_clear) the drain branch consumes
            # via `Board.update`.
            #
            # Field-presence convention: a key in `args` with a non-None
            # value means "set this field"; a missing key means "leave
            # unchanged." `clear_tags` and `clear_blocked` are explicit
            # bools so an operator who really means "clear" doesn't have
            # to encode that as `--tags ""` (ambiguous: typo vs intent).
            update_err = _validate_update_args(args)
            if update_err:
                return _err(update_err)

            # Per-target fence (TB-153 design): mirrors `delete`'s fence —
            # keyed on the target's section, not directory-wide. Other
            # tasks running is fine; what matters is whether THIS task is
            # in flight (Active or Pipeline Pending).
            briefing_content = args.get("briefing")
            has_briefing_edit = (
                briefing_content is not None
                and str(briefing_content).strip() != ""
            )
            if section in ("Active", "Pipeline Pending"):
                if has_briefing_edit:
                    # Hard-refused with no `--force` escape — the agent
                    # may re-read its briefing mid-run via `Read` and
                    # TB-110's snapshot may hash the file. Deferred-draft
                    # handling is carved out as a follow-up; the fence
                    # covers the 90% case where edits target Backlog /
                    # Ready / Frozen.
                    return _err(
                        f"{task_id} is in {section} — briefing-content "
                        f"edits to a running task are refused (the agent "
                        f"may re-read its briefing mid-run; TB-110 "
                        f"snapshot hash). Wait for the task to leave "
                        f"{section}, or update only board-line fields."
                    )
                if not args.get("force"):
                    return _err(
                        f"{task_id} is in {section} — refusing update "
                        f"without --force. Pass --force to edit "
                        f"board-line fields (title / tags / blocked / "
                        f"description); briefing-content edits remain "
                        f"refused."
                    )

            # TB-154: structural gate on briefing-content edits. Runs
            # before the briefing file is written below so a rejected
            # update doesn't materialize a partial / invalid briefing
            # on disk (the slug-stable write would otherwise overwrite
            # the prior good briefing with the rejected payload). Same
            # rule as the `add_*` boundary — `## Goal`, `## Scope`,
            # `## Design`, `## Verification`, `## Out of scope`, plus a
            # parseable & non-empty Verification section. Closes the
            # symmetric hole flagged by the per-task verifier on
            # TB-154's first attempt: a briefing replaced via `update`
            # could otherwise still slip past the structural check the
            # `add_*` paths now enforce.
            # TB-170: `skip_goal_alignment=True` from the CLI bypasses
            # TB-161 + TB-164 on briefing-content edits as well. Runs
            # every other validation (TB-154 canonical sections,
            # parseable + non-empty Verification) unchanged.
            update_skip_goal_alignment = bool(args.get("skip_goal_alignment"))
            if has_briefing_edit:
                # TB-235: feed `description`, `blocked_csv`, and
                # `events_file` so check #7 (LLM-judge dependency
                # coherence) also fires on briefing-content updates.
                # For an update we score the EFFECTIVE post-update
                # description + blocked codespan, not the prior
                # values — what the briefing now claims has to match
                # what the codespan WILL declare after the drain.
                # Falls back to the existing values when the update
                # payload doesn't touch those fields.
                if args.get("clear_blocked"):
                    eff_blocked = ""
                elif "blocked" in args and args["blocked"] is not None:
                    eff_blocked = str(args["blocked"])
                else:
                    eff_blocked = (
                        existing.meta.get("blocked", "") if existing else ""
                    )
                if "description" in args and args["description"] is not None:
                    eff_description = str(args["description"])
                else:
                    eff_description = (
                        existing.description if existing else ""
                    )
                struct_err = _validate_briefing_structure(
                    str(briefing_content),
                    goal_md_path=cfg.project_root / "goal.md",
                    skip_goal_alignment=update_skip_goal_alignment,
                    description=eff_description,
                    blocked_csv=eff_blocked,
                    events_file=cfg.events_file,
                )
                if struct_err:
                    return _err(struct_err)

            # Build the update payload + the `fields=[...]` diff list
            # the drain emits on the `task_updated` event.
            fields: list[str] = []
            if "title" in args and args["title"] is not None:
                rec_args["title"] = str(args["title"])
                fields.append("title")
            if args.get("clear_tags"):
                rec_args["tags"] = []
                fields.append("tags")
            elif "tags" in args and args["tags"] is not None:
                rec_args["tags"] = list(args["tags"])
                fields.append("tags")
            if "description" in args and args["description"] is not None:
                rec_args["description"] = str(args["description"])
                fields.append("description")
            if args.get("clear_blocked"):
                rec_args["meta_clear"] = ["blocked"]
                fields.append("blocked")
            elif "blocked" in args and args["blocked"] is not None:
                rec_args["meta_set"] = {"blocked": str(args["blocked"])}
                fields.append("blocked")

            # Briefing path resolution: write at queue-append time so the
            # update is durable across daemon restarts. Slug-stable —
            # overwrite the existing file when the task already has a
            # briefing path; allocate a fresh slug (from the CURRENT
            # title, not the new one) only for legacy / pre-TB-135 tasks
            # that have no briefing on disk yet. Title changes never
            # rename the briefing — file-name staleness is the accepted
            # trade-off for keeping git history of the briefing file
            # contiguous (TB-153 design's "Locked decisions").
            if has_briefing_edit:
                if existing and existing.briefing:
                    brief_path = cfg.project_root / existing.briefing
                    briefing_rel = existing.briefing
                else:
                    slug = slugify(existing.title if existing else task_id)
                    brief_path = cfg.tasks_dir / f"{slug}.md"
                    n = 2
                    while brief_path.exists():
                        brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                        n += 1
                    briefing_rel = str(brief_path.relative_to(cfg.project_root))
                brief_path.parent.mkdir(parents=True, exist_ok=True)
                brief_path.write_text(briefing_content)
                rec_args["briefing"] = briefing_rel
                fields.append("briefing")

            if not fields:
                return _err(
                    "update op requires at least one field to change "
                    "(title / tags / blocked / description / briefing). "
                    "Pass `clear_tags=true` / `clear_blocked=true` for "
                    "explicit clears."
                )
            rec_args["fields"] = fields
            # TB-170: persist the bypass intent on the queue record. Only
            # meaningful when the update carried a briefing edit (the
            # validator only fires on briefing-content updates), but
            # storing it unconditionally keeps the audit-line shape
            # consistent across record types.
            if update_skip_goal_alignment:
                rec_args["skip_goal_alignment"] = True

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


def classifications_last_30d_by_verdict(cfg: Config) -> dict[str, int]:
    """Count `task_classified` events (TB-189) in the last 30 days, by
    verdict. Always returns a dict with a key per `IMPACT_VERDICTS` value
    (zeros when empty). Counts are based on the event's `ts` field; the
    window endpoints use UTC (matches `events.append`'s `now()`).

    Reads up to 1000 recent events — comfortably more than any plausible
    30-day classification volume even at one classification per shipped
    proposal. The walk is O(N) over the tail; surfaced by `cmd_status`
    which already pays for an event-tail read.
    """
    counts: dict[str, int] = {v: 0 for v in IMPACT_VERDICTS}
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    for evt in events.tail(cfg.events_file, n=1000):
        if evt.get("type") != "task_classified":
            continue
        ts = str(evt.get("ts") or "")
        if not ts or ts < cutoff_str:
            continue
        verdict = str(evt.get("verdict") or "")
        if verdict in counts:
            counts[verdict] += 1
    return counts


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

    Returns a dict with `applied` (count), `touched_paths` (state
    files dirtied), and `force_ideate` (TB-159 — set to True if any
    drained op was an `ideate` signal, telling the daemon to run
    `ideation.force_ideate` on this same tick after the drain releases
    the board lock).
    """
    queue_path = operator_queue_path(cfg)
    state_path = operator_queue_state_path(cfg)
    if not queue_path.exists() or queue_path.stat().st_size == 0:
        return {"applied": 0, "touched_paths": [], "force_ideate": False}

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
        return {"applied": 0, "touched_paths": [], "force_ideate": False}

    applied_count = 0
    touched: set[str] = set()
    highest_alloc = 0
    # TB-159: track whether any drained op was an `ideate` signal so the
    # daemon can run the forced ideation pass on this same tick (after
    # the drain releases the board lock). Consumed by `_tick` via the
    # return dict's `force_ideate` key.
    force_ideate_pending = False
    with board_file_lock(cfg.tasks_file):
        for rec in pending:
            try:
                board = Board.load(cfg.tasks_file)
                _apply_operator_op(cfg, board, rec)
                board.save()
                _append_operator_audit_line(cfg, rec)
                applied_count += 1
                if rec.get("op") == "ideate":
                    force_ideate_pending = True
                touched.update(
                    [
                        "TASKS.md",
                        "CLAUDE.md",
                        ".cc-autopilot/retry_state.json",
                        ".cc-autopilot/operator_log.md",
                        ".cc-autopilot/tasks",
                        # TB-188: drain-side `approve` / `reject` /
                        # `delete` may amend the per-proposal record's
                        # `outcome` block. Listed here unconditionally
                        # so `_commit_state_files` lands the rewrite in
                        # the same `state: drained N operator op(s)`
                        # commit. `_filter_state_paths` drops the dir
                        # when nothing inside it changed (the typical
                        # case for ops on non-ideation tasks).
                        ".cc-autopilot/ideation_proposals",
                    ]
                )
                # TB-193: `update_goal` writes the new goal.md content
                # under the lock; surface the path so the drain-side
                # `_commit_state_files` allowlist (TB-126) lands the
                # change in the same `state: drained N operator op(s)`
                # commit. Conditional rather than unconditional so a
                # drain pass that didn't actually touch goal.md doesn't
                # try to stage a clean working copy of it.
                if rec.get("op") == "update_goal":
                    touched.add("goal.md")
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
    return {
        "applied": applied_count,
        "touched_paths": sorted(touched),
        "force_ideate": force_ideate_pending,
    }


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
        # TB-188: terminal-event reconciliation. No-op when no proposal
        # record exists (legacy / non-ideation tasks); otherwise stamps
        # `outcome.decision_kind=deleted` with the operator actor. Reason
        # stays empty for `delete` — the matching operator_log.md line
        # carries no free-text reason (the verb itself is the audit).
        try:
            reconcile_proposal_outcome(
                cfg, args["task_id"],
                decision_kind="deleted",
                decision_actor="operator",
            )
        except OSError:
            pass
        return
    if op == "reject":
        # TB-152: shares `delete`'s removal codepath — drop the row +
        # briefing file (briefing-file removal is implicit: `Board.remove`
        # only drops the line; the briefing under `.cc-autopilot/tasks/`
        # is unlinked here so a future re-add doesn't collide on slug).
        # Emits `task_deleted` (same event shape as `delete` — the
        # operator-log.md line is what carries the reject-vs-delete
        # distinction). The `<ts> — rejected ideation proposal → TB-N
        # (<title>): <reason>` line is written by
        # `_append_operator_audit_line`'s reject branch using the title +
        # reason snapshotted into the queue record at append time.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("reject op missing task_id")
        loc = board.find(tid)
        if loc is None:
            raise RuntimeError(f"{tid} not on board")
        section = loc[0]
        existing = board.get(tid)
        title = existing.title if existing else args.get("title", "")
        briefing_rel = existing.briefing if existing else None
        board.remove(tid)
        if briefing_rel:
            brief_path = cfg.project_root / briefing_rel
            try:
                brief_path.unlink()
            except FileNotFoundError:
                pass
        events.append(
            cfg.events_file,
            "task_deleted",
            task=tid,
            section=section,
            title=title,
        )
        # TB-188: terminal-event reconciliation. The reject path always
        # carries a `reason` arg (snapshotted into the queue record at
        # append time, defaulting to "(no reason given)"). Stamp it into
        # the record's `outcome.reason` so the same operator-authored
        # rationale lives in two places: the human-readable
        # operator_log.md line AND the structured per-proposal record
        # the signal-collection follow-ups (TB-189) query.
        try:
            reconcile_proposal_outcome(
                cfg, tid,
                decision_kind="rejected",
                decision_actor="operator",
                reason=str(args.get("reason") or ""),
            )
        except OSError:
            pass
        return
    if op == "ideate":
        # TB-159: drain-side `ideate` is a signal, not an action. The
        # actual ideation run is dispatched by the daemon's `_tick`
        # AFTER the drain releases the board lock — running the SDK
        # call here would hold `board_file_lock` for minutes and
        # serialize every other operator op + the cron / task /
        # status-report stages behind it. Emit the audit event so
        # post-hoc inspection distinguishes manual fires from natural
        # ones; the operator-queue-drain return dict ferries the
        # `force_ideate` signal up to `_tick`.
        events.append(
            cfg.events_file,
            "ideation_forced",
            force=bool(args.get("force")),
        )
        return
    if op == "update_goal":
        # TB-193: full-file replacement of `goal.md`. Atomic write —
        # tmpfile + `os.replace` — so a concurrent reader (ideation
        # mid-cycle, the per-task verifier reading the rollback-cohesion
        # state surface) can't observe a partial file. We hold
        # `board_file_lock` for the full drain (caller's responsibility),
        # so the rename plus the `state: drained N operator op(s)`
        # commit together form a single observable transition for any
        # subsequent reader.
        import os
        goal_content = args.get("goal_content")
        reason = args.get("reason") or ""
        if not isinstance(goal_content, str) or not goal_content.strip():
            raise RuntimeError(
                "update_goal op missing non-empty goal_content"
            )
        target = cfg.project_root / "goal.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(goal_content)
        os.replace(tmp, target)
        events.append(
            cfg.events_file,
            "goal_updated",
            reason=reason,
            bytes=len(goal_content),
        )
        return
    if op == "classify":
        # TB-189: operator-authored retrospective verdict. Two writes
        # at drain time:
        #   1. The audit-line to operator_log.md
        #      (`<ts> — classified TB-N impact=<verdict>: <reason>`),
        #      symmetric to the reject branch's richer audit line —
        #      written by `_append_operator_audit_line`'s `classify`
        #      branch using the verdict / reason from the queue record.
        #      Standalone authoritative trail; ideation Step 0 reads
        #      operator_log.md and learns from this line.
        #   2. The per-proposal record's `impact` block under
        #      `.cc-autopilot/ideation_proposals/<TB-N>.json` (TB-188).
        #      Tolerates missing record file (legacy proposals from
        #      before TB-188 landed; operator-driven adds without the
        #      `review` marker; etc.) — emits `classify_record_missing`
        #      to events.jsonl and proceeds. The operator_log line is
        #      authoritative; the per-proposal record is a structured
        #      signal feeding ideation's later track-record block.
        # No board mutation — the verb is metadata-only. We still emit
        # a `task_classified` event so events.jsonl carries a structured
        # audit trail (read by `cmd_status` to count classifications in
        # the last 30 days).
        tid = args.get("task_id", "")
        verdict = args.get("verdict", "")
        reason = args.get("reason", "") or ""
        if not tid:
            raise RuntimeError("classify op missing task_id")
        if verdict not in IMPACT_VERDICTS:
            # Defensive — the queue-append handler validates this, but
            # a hand-crafted record (legacy / partial-write recovery)
            # could carry a bad verdict. Reject loudly here so a future
            # ideation cycle doesn't read a corrupted `impact` block.
            raise RuntimeError(
                f"classify op verdict {verdict!r} not in IMPACT_VERDICTS"
            )
        events.append(
            cfg.events_file,
            "task_classified",
            task=tid,
            verdict=verdict,
            reason=reason,
        )
        # Per-proposal record amend. Best-effort: a missing record
        # (legacy TB-N from before TB-188 landed) is logged + skipped.
        target = proposal_record_path(cfg, tid)
        if not target.exists():
            events.append(
                cfg.events_file,
                "classify_record_missing",
                task=tid,
                verdict=verdict,
            )
            return
        try:
            record = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError):
            events.append(
                cfg.events_file,
                "classify_record_unreadable",
                task=tid,
                verdict=verdict,
            )
            return
        if not isinstance(record, dict):
            events.append(
                cfg.events_file,
                "classify_record_unreadable",
                task=tid,
                verdict=verdict,
            )
            return
        record["impact"] = {
            "verdict": verdict,
            "classified_at": _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "reason": reason,
        }
        _atomic_write_json(target, record)
        return
    if op == "audit_skip":
        # TB-248: drain-side audit-skip. Metadata-only — no board
        # mutation. The rich `<ts> — audit-skipped TB-N: <reason>`
        # line is appended by `_append_operator_audit_line`'s
        # audit_skip branch at end-of-drain (under the same
        # `board_file_lock` the rest of the drain holds, so a
        # concurrent reader of operator_log.md never observes a
        # partial line). Emit a structured `task_audit_skipped`
        # event so events.jsonl carries the same audit trail shape
        # as `task_classified` (TB-189) for symmetric downstream
        # consumption.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("audit_skip op missing task_id")
        reason = args.get("reason", "") or ""
        events.append(
            cfg.events_file,
            "task_audit_skipped",
            task=tid,
            reason=reason,
        )
        return
    if op == "ack":
        # TB-201: drain-side ack. Performs the actual operator_log.md
        # append the pre-TB-201 synchronous `do_operator_log_append`
        # used to do, but at tick boundary under the daemon's board
        # lock — never inside a task agent's snapshot window, so the
        # write no longer trips TB-110's post-hoc fenced-file check.
        # `_apply_operator_ack` writes the bullet line and emits the
        # `operator_ack` event; the standard `applied operator-queued
        # ack → TB-N` audit line lands separately via
        # `_append_operator_audit_line`. Two lines total per drained
        # ack: the operator's rich note (the actionable content) +
        # the verb-vs-other-ops audit pointer.
        result = _apply_operator_ack(cfg, args)
        if result.get("isError"):
            raise RuntimeError(
                result["content"][0]["text"]
                if result.get("content")
                else "ack op failed"
            )
        return
    if op == "approve":
        # TB-142: drain-side approve. Shares `_approve_review_token` with
        # `do_board_edit({"action":"approve",...})` (the idle-path entry)
        # so both routes leave the task in the same state — codespan
        # `@blocked:review` stripped, legacy `(blocked on: review)` prose
        # scrubbed. Audit event mirrors the direct-call path.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("approve op missing task_id")
        _approve_review_token(board, tid)
        events.append(cfg.events_file, "ideation_approved", task=tid)
        # TB-188: terminal-event reconciliation for the operator-approval
        # path. The approve verb strips `@blocked:review` and lets the
        # task become dispatchable — from the proposal's perspective the
        # operator has weighed in and said "yes." Subsequent
        # task_complete events for this TB-N find the outcome already
        # set and silently no-op (idempotent first-write wins).
        try:
            reconcile_proposal_outcome(
                cfg, tid,
                decision_kind="approved",
                decision_actor="operator",
            )
        except OSError:
            pass
        return
    if op == "update":
        # TB-153: drain-side update. The queue-append handler already
        # wrote the briefing file (slug-stable) when `briefing` was in
        # the update payload, so this branch only mutates the task line
        # via `Board.update`. The `fields=[...]` list is what the
        # queue-append handler computed — it's the diff the operator's
        # CLI / MM-handler call asked for, and we forward it verbatim
        # onto the audit event so post-mortems can grep
        # `task_updated fields=[blocked]` etc.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("update op missing task_id")
        update_kwargs: dict[str, Any] = {}
        if "title" in args:
            update_kwargs["title"] = args["title"]
        if "tags" in args:
            update_kwargs["tags"] = list(args["tags"] or [])
        if "description" in args:
            update_kwargs["description"] = args["description"]
        if "briefing" in args:
            update_kwargs["briefing"] = args["briefing"]
        if args.get("meta_set"):
            update_kwargs["meta_set"] = dict(args["meta_set"])
        if args.get("meta_clear"):
            update_kwargs["meta_clear"] = list(args["meta_clear"])
        try:
            board.update(tid, **update_kwargs)
        except KeyError:
            raise RuntimeError(f"{tid} not on board")
        fields = list(args.get("fields") or [])
        events.append(
            cfg.events_file,
            "task_updated",
            task=tid,
            fields=fields,
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
    # TB-159: distinguish manual ideation fires from natural cron-driven
    # ones in the operator log (`applied operator-queued ideate →
    # (forced)` vs no log line at all for the natural path). Ideation
    # Step 0 reads operator_log.md as ground truth on operator
    # decisions; the `(forced)` decoration is the human-readable signal.
    if op == "ideate":
        arrow = " → (forced)"
    # TB-170: when the operator-CLI bypass flag was set on an add_* /
    # update op, decorate the audit line with `(goal-alignment check
    # skipped)` so future ideation cycles can grep operator_log.md for
    # the `goal-alignment check skipped` substring and decide whether
    # to count the task toward "operator-validated work" vs
    # "operator-bypassed-validation work" — useful signal for the
    # rejection-reasons loop (TB-152) without a separate event type.
    suffix = ""
    if args.get("skip_goal_alignment"):
        suffix = " (goal-alignment check skipped)"
    lines: list[str] = [
        f"- {ts} — applied operator-queued {op}{arrow}{suffix}\n"
    ]
    if op == "update_goal":
        # TB-193: in addition to the standard `applied operator-queued
        # update_goal` line above (the verb-vs-other-ops distinction),
        # emit the richer `<ts> — operator updated goal.md (<reason>)`
        # line that future ideation cycles read as a "goal drift event"
        # signal. Empty reason collapses to `<ts> — operator updated
        # goal.md` (no parens).
        reason = (args.get("reason") or "").strip()
        reason_part = f" ({reason})" if reason else ""
        lines.append(
            f"- {ts} — operator updated goal.md{reason_part}\n"
        )
    if op == "reject":
        # TB-152: in addition to the standard `applied operator-queued
        # reject → TB-N` audit line above (so the reject vs. delete
        # distinction shows up in the verb), emit the richer
        # `<ts> — rejected ideation proposal → TB-N (<title>): <reason>`
        # line that ideation Step 0 reads as ground truth on operator
        # decisions. Title + reason were snapshotted into the queue
        # record at append time so this branch doesn't have to re-look
        # them up post-`board.remove`.
        title = args.get("title", "") or ""
        reason = args.get("reason", "") or "(no reason given)"
        title_part = f" ({title})" if title else ""
        lines.append(
            f"- {ts} — rejected ideation proposal{arrow}"
            f"{title_part}: {reason}\n"
        )
    if op == "classify":
        # TB-189: in addition to the standard `applied operator-queued
        # classify → TB-N` audit line above (so the classify vs. other
        # ops distinction shows up in the verb), emit the richer
        # `<ts> — classified TB-N impact=<verdict>: <reason>` line that
        # future ideation cycles read as the operator-authored signal
        # stream goal.md L61-76 names. Empty reason renders without a
        # trailing colon-space-empty (collapses to just `impact=<verdict>`).
        verdict = args.get("verdict", "") or ""
        c_reason = (args.get("reason") or "").strip()
        c_reason_part = f": {c_reason}" if c_reason else ""
        lines.append(
            f"- {ts} — classified {task} impact={verdict}{c_reason_part}\n"
        )
    if op == "audit_skip":
        # TB-248: in addition to the standard `applied operator-queued
        # audit_skip → TB-N` audit line above, emit the richer
        # `<ts> — audit-skipped TB-N: <reason>` line that the audit
        # state-derivation grep (`ap2/audit.py::parse_reviewed_set`)
        # reads as the third "operator has weighed in" signal
        # alongside `classified TB-N` and `rejected TB-N`. Empty
        # reason collapses to `(no reason given)` so the line is
        # always a single-shape grep target (the placeholder is
        # itself a signal — operator skipped without articulating
        # why; future audit walks can spot the difference).
        s_reason = (args.get("reason") or "").strip() or "(no reason given)"
        lines.append(
            f"- {ts} — audit-skipped {task}: {s_reason}\n"
        )
    with log_path.open("a") as f:
        for line in lines:
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
