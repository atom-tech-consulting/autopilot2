"""Synchronous board mutation entry point (TB-153 et al).

Hosts `do_board_edit` — the direct-mutation MCP/CLI surface for adding,
moving, removing, and approving tasks. Distinct from the queue-routed
path (`do_operator_queue_append` in `operator_queue.py`): `do_board_edit`
mutates TASKS.md synchronously under the board lock, intended for paths
that run between in-flight task agents (ideation, control agents, idle
operator CLI). The MM-handler restricted toolset uses `operator_queue
_append` instead so chat-driven adds during in-flight runs don't trip
TB-110's snapshot check.

Moved out of `ap2/tools.py` by TB-262 — the synchronous board-edit
surface (per-target fence decisions, briefing-file write, allocator +
approve-token sharing with the queue drain) is one coherent concept
that benefits from sitting next to its siblings rather than mixed in
with MCP dispatch plumbing.

TB-383 (axis 3): the add_backlog path is POLICY-FREE — it no longer
evaluates the auto-approve gate inline. Proposals are born
`@blocked:review`; the `auto_approve` component's PRE_DISPATCH loop pass
(`run_auto_approve_pass`) strips the token for gate-clearing tasks
between agent runs.

Public symbols (re-exported from `ap2.tools` for backward compat):
- `do_board_edit` — the only symbol new code should import directly.

Shared helpers (`_allocate_id`, `_approve_review_token`,
`_validate_briefing_structure`, etc.) live in their canonical homes
(`operator_queue.py` / `briefing_validators.py`); this module imports
from them rather than re-defining.
"""
from __future__ import annotations

from . import events
from .board import locked_board
from .briefing_validators import (
    _validate_briefing_structure,
    _validate_single_line,
    reconcile_proposal_outcome,
    write_ideation_proposal_record,
)
from .config import Config, bump_next_task_id
from .operator_queue import _allocate_id, _approve_review_token
# `_ok` / `_err` / `slugify` live in `ap2/tools.py`; that module imports
# this one for re-export, so the cross-reference works via Python's
# standard partial-import resolution (tools.py defines the helpers
# BEFORE importing this module).
from .tools import _err, _ok, slugify


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

    # TB-154: structural validation runs after TB-135's non-empty gate
    # and before `_allocate_id`. A rejected add must not leak a TB-N or
    # write a briefing file to disk. Mirrors the placement of TB-134's
    # single-line check above.
    if action in add_map:
        # TB-235: pass `description`, `blocked_csv`, and `events_file` so
        # check #7 (LLM-judge dependency coherence) fires on this surface
        # too. `do_board_edit` is the legacy direct-board-mutation path;
        # the primary surface is `do_operator_queue_append`, but
        # integrating both keeps the validator-shape symmetric.
        struct_err = _validate_briefing_structure(
            briefing or "",
            goal_md_path=cfg.project_root / "goal.md",
            description=description,
            blocked_csv=blocked_on,
            events_file=cfg.events_file,
            # TB-331 axis-5: thread `cfg` through so the validator_judge
            # component's four cfg-routed knob reads
            # (`disabled` / `timeout_s` / `max_turns` / `max_tokens`)
            # resolve against the live Config rather than the manifest
            # adapter's synthetic back-compat fallback.
            cfg=cfg,
        )
        if struct_err:
            return _err(struct_err)

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
                # TB-383 (axis 3): `board_edit`'s add_backlog path is now
                # POLICY-FREE. Proposals are uniformly born `@blocked:review`;
                # the auto-approve decision (strip / dry-run / noop) NO LONGER
                # happens at this mutation-time site. It moved into the
                # `auto_approve` component's PRE_DISPATCH loop pass
                # (`run_auto_approve_pass`), which walks Backlog
                # `@blocked:review` tasks BETWEEN agent runs and emits the
                # `auto_approved` / `would_auto_approve` events with identical
                # payloads. Inverting the control flow this way means a
                # task-agent snapshot can never capture a half-applied board
                # mid-run, and the gate chain + `should_auto_approve` tags
                # policy live in the component that owns the
                # `AP2_AUTO_APPROVE*` knobs — untangling the cross-boundary
                # knot that blocked the ideation extraction (goal.md axis
                # 3/4). The `review` token simply rides onto the task line
                # here; the loop pass decides its fate next tick.
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
                # TB-188: seed a per-proposal record for ideation-authored
                # `add_backlog` (`blocked_on` carries the `review` token).
                # No-op for operator-driven adds (no review marker) and
                # for non-backlog adds. Failures are swallowed so a bad
                # write to the records dir doesn't unwind a successful
                # board edit; the daemon's audit trail (events.jsonl)
                # still carries the canonical `task_added` event.
                if action == "add_backlog" and blocked_on:
                    try:
                        write_ideation_proposal_record(
                            cfg,
                            tb_id=new_id,
                            blocked_on=blocked_on,
                            briefing_text=briefing or "",
                            briefing_rel=briefing_rel,
                        )
                    except OSError:
                        pass
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

            if action == "approve":
                # TB-142 (TB-121): strip the `review` blocker so an
                # ideation-proposed Backlog task becomes dispatchable.
                # `_approve_review_token` does the work; we wrap with the
                # `ideation_approved` audit event so the operator-review
                # surface (`ap2 status`, ideation Step 0) can spot the
                # promotion. Restricted-toolset MM handler routes via
                # `operator_queue_append({"op":"approve",...})` instead
                # — same helper, drain-side, post-task-window.
                if not task_id:
                    return _err("task_id is required for approve")
                try:
                    t = _approve_review_token(board, task_id)
                except RuntimeError as e:
                    return _err(str(e))
                events.append(
                    cfg.events_file, "ideation_approved", task=t.id,
                )
                # TB-188: terminal-event reconciliation for the synchronous
                # `do_board_edit` approve surface (matches the drain-side
                # branch in `_apply_operator_op` so both approve routes
                # land identical record-shape outcomes).
                try:
                    reconcile_proposal_outcome(
                        cfg, t.id,
                        decision_kind="approved",
                        decision_actor="operator",
                    )
                except OSError:
                    pass
                return _ok(
                    f"approve {t.id}", task_id=t.id, section=t.section,
                )

            return _err(f"unknown action {action!r}")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")
