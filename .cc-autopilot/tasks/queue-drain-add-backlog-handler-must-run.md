# Queue-drain `add_backlog` handler must run auto-approve gate (closes review-token stranding)

Tags: #autopilot #auto-approve #operator-queue #regression-pin #bug

## Goal

Run the auto-approve gate chain — `evaluate_auto_approve_decision` →
`_approve_review_token` strip + `auto_approved` event — at the queue-drain
side of the `add_backlog` op in `ap2/operator_queue.py:_apply_operator_op`,
mirroring the gate chain that `ap2/board_edits.py:do_board_edit`'s
`add_backlog` branch already runs at the direct-add side. Closes the
goal.md `## Done when` failure mode "Ideation reliably proposes
goal-aligned next steps that substantively advance the goal (not just
goal-shaped pro-forma compliance)" — without the gate running on the
queue path, a real ideation proposal queued through `operator_queue_append
op=add_backlog` lands with `@blocked:review` intact and is stranded
indefinitely (no `auto_approved` event ever fires, so the per-tick
auto-promote gate never sees the task as auto-approved-eligible); the
proposal's substantive contribution is effectively dropped on the floor.

Why now: 2026-05-26 incident — TB-290 (ideation-proposed
`cost_cap_approach` attention detector, closing the last named-in-goal.md
attention-condition axis) was queued via `operator_queue_append
op=add_backlog` and sat stranded in Backlog for ~10 hours despite
`AP2_AUTO_APPROVE=1` and the docstring claim at `focus_advance.py:30-36`
that "task dispatch is NOT affected" by roadmap_complete. Tracing the
queue-drain path showed the bug: `_apply_operator_op`'s `add_backlog`
branch at `operator_queue.py:1293-1308` calls `board.add(...)` and
returns — no `evaluate_auto_approve_decision` call, no
`_approve_review_token` strip, no `auto_approved` event. Compare with
`do_board_edit`'s `add_backlog` branch at `board_edits.py:175-244` which
runs the full TB-232 gate chain (tags → freeze-threshold →
per-task-token-cap → window-token-cap → dry-run vs strip terminal
decision) before adding. TB-291's tool fence closes this path for
ideation specifically, but the queue path remains callable by the
Mattermost handler and any future control agent, leaving the asymmetry
as latent surface that will silently strand the next review-bearing
queue-routed add.

## Scope

(1) `ap2/operator_queue.py` `_apply_operator_op`: in the `add_backlog`
branch (around L1293-1308), after the `board.add(...)` call, check
whether the meta dict carries a `blocked` key containing a `review`
token (matching `do_board_edit`'s detection at `board_edits.py:175-186`).
If yes, delegate to `daemon.evaluate_auto_approve_decision(cfg, tags=...)`
exactly as `do_board_edit` does. Branch on the returned decision:
  - `"strip"`: rewrite the just-added task's `@blocked:` codespan via
    `_approve_review_token` (or an equivalent in-place edit on the
    `Task.meta["blocked"]` field), then emit `auto_approved` with the
    same payload shape `do_board_edit` uses (`task=<id>`, `knob=<knob>`).
  - `"dry_run"`: emit `would_auto_approve` (`dry_run=True`); leave
    the review token in place — same monitor-only on-ramp semantics
    `do_board_edit` honors.
  - `"noop"`: no event, no strip; the task remains gated on `ap2
    approve` — same surface an operator-driven `ap2 add` would
    produce.

(2) Preserve the existing add-side write_ideation_proposal_record
call shape at `board_edits.py:278-288` — i.e. the queue path should
seed the per-proposal record (TB-188) just like the direct add path
does, for the same `add_backlog + blocked_on=review` condition.

(3) Apply only to `add_backlog` ops. The sibling `add_ready` /
`add_frozen` ops do NOT carry review blockers in any current code
path (Ready means already-approved by definition; Frozen is for
retry-exhausted) — adding the gate to those branches would be dead
code.

(4) Regression-pin module `ap2/tests/test_queue_drain_auto_approve.py`
covers:
  - Queue-drain `add_backlog` with `meta={'blocked': 'review'}` and
    auto-approve gates all passing → review token stripped from the
    landed task; `auto_approved` event fired.
  - Queue-drain `add_backlog` with `meta={'blocked': 'review'}` and
    `AP2_AUTO_APPROVE` unset → review token preserved; no event.
  - Queue-drain `add_backlog` with `meta={'blocked': 'review'}` and
    `AP2_AUTO_APPROVE_DRY_RUN=1` → review token preserved;
    `would_auto_approve dry_run=True` event fired.
  - Queue-drain `add_backlog` with NO review blocker (meta empty or
    blocked carries only TB-N tokens) → no gate evaluation, no
    `auto_approved` event, task lands with whatever blockers it
    came in with.
  - Queue-drain `add_backlog` blocked by a freeze-threshold trip (TB-223
    cumulative regression) → review token preserved; no
    `auto_approved` event (gate decision is `"noop"`).
  - `add_ready` / `add_frozen` ops never invoke the gate (assert
    no `auto_approved` event fires regardless of blockers).

(5) Update existing tests in `ap2/tests/test_tb145_operator_queue.py`
(or the closest existing operator-queue regression module — check the
existing module names) that assert add_backlog drain behavior. Any
test that previously asserted "queue-drain add_backlog produces
task with @blocked:review" needs to either set `AP2_AUTO_APPROVE`
unset (preserves prior assertion) or accept the strip + event in the
new shape.

## Design

The fix mirrors `do_board_edit`'s gate chain in the queue-drain handler
so the two paths are semantically equivalent for the auto-approve
question. The asymmetry today is a code-history artifact: TB-131
introduced the queue path for the Mattermost handler, where reviews
weren't a typical concern (operator-typed chat adds don't carry
`@blocked:review`); TB-223 / TB-224 / TB-232 layered the auto-approve
gate chain onto the direct path. The queue path missed the gate
chain because no one then needed it. Today's incident exposed the
latent surface: when ideation routes through the queue path (TB-131's
TOCTOU-defense recommendation), review-bearing tasks strand silently.

Place the call AFTER `board.add(...)` rather than before, mirroring
`do_board_edit` which adds first, then strips via `_approve_review_token`.
Reason: the strip needs the row to exist on the board to find by
`task_id`; ordering the strip pre-add would require duplicating the
meta-blocked rewrite logic.

Lazy-import `daemon.evaluate_auto_approve_decision` from inside the
branch (not at module top) — same pattern `do_board_edit` uses (`from
. import daemon as _daemon`) to avoid the operator_queue ⇄ daemon
load-time cycle.

Out-of-scope alternative considered + rejected: refactoring
`board.add` to host the gate chain (so any caller goes through it
automatically). Rejected because `board.add` is a thin file-mutation
helper and the auto-approve gate is policy — coupling them would
break the existing layering (`board.py` is dumb persistence;
`board_edits.py` / `operator_queue.py` host policy). The lateral fix
(mirror the gate in the queue handler) preserves the layering.

## Verification

- `grep -q 'evaluate_auto_approve_decision' ap2/operator_queue.py` — gate call wired into queue handler.
- `grep -q '_approve_review_token\|auto_approved' ap2/operator_queue.py` — strip + audit-event surface present.
- `uv run python -c "import inspect, ap2.operator_queue; src = inspect.getsource(ap2.operator_queue._apply_operator_op); assert 'evaluate_auto_approve_decision' in src, 'queue-drain add_backlog must invoke the auto-approve gate'"` — handler invocation verified by source inspection.
- `test -f ap2/tests/test_queue_drain_auto_approve.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_queue_drain_auto_approve.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Centralizing the auto-approve gate inside `board.add` itself —
  rejected design alternative (see Design).
- TB-291 tool fence reversal (the fence stays — even with this fix
  landed, ideation should still prefer the direct path because the
  TOCTOU race the queue path defends against doesn't exist during
  ideation, per the fence TB's rationale).
- Bug 1 counter restructuring — separate TB (already landed as TB-292).
- Bug 3 operator-pointer-rewind not emitting `focus_advanced` event
  for the counter's cutoff — separate TB.
- TB-284 scrub mechanism silent-timeout — separate TB.
- Per-proposal record seeding (`write_ideation_proposal_record`) for
  non-ideation queue adds — out of scope; record seeding stays scoped
  to add_backlog + review-bearing proposals, matching the direct-add
  semantics.
- Auditing other queue ops (`unfreeze` / `move_to_backlog` / `delete`)
  for parallel asymmetries — those ops don't carry review blockers
  in any current code path.
