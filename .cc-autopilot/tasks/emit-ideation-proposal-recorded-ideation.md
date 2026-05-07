# Emit `ideation_proposal_recorded` + `ideation_proposal_reconciled` events when TB-188 records are written/amended

## Goal

Current focus: ideation quality signal collection. TB-188 (commit
`93892da`) writes per-proposal records to
`.cc-autopilot/ideation_proposals/<TB-N>.json` and reconciles them
on terminal events, but those filesystem changes are silent — no
event lands in `events.jsonl`. Consequence: the ideation cron's
events block (filtered through `IDEATION_RELEVANT_EVENT_TYPES` per
TB-169) cannot see record creation or outcome reconciliation, and
the next cycle has no observable signal that the per-proposal
record substrate is even functioning. Adding two emit lines
surfaces the signal in the agent's own context window AND gives
the web /events page + `ap2 logs` a row per record event.

Why now: TB-188 landed at 01:04Z this cycle and has produced zero
records yet (the dir holds only `.gitkeep`). The first ideation-
authored proposals will create records THIS cycle (this ideation
run's TB-195 + TB-196 add_backlog calls); without events, those
records remain unobservable from any agent-readable surface
(operator_log.md captures only operator-driven actions, not
daemon-internal record writes). Adding the events now — before
signal volume accrues — means every record from TB-188's first
production write is observable end-to-end, not retrofitted later.

## Scope

Two emit sites in `ap2/tools.py` (TB-188 located the record-write
helpers there; if the impl placed them elsewhere, follow the
actual location and grep confirms the location in verification):

1. After a successful `write_ideation_proposal_record(...)`
   completes the atomic file write, emit:
   `_emit_event("ideation_proposal_recorded", task_id=tb_id,
   focus_anchor=<truncated to 80 chars>,
   why_now_chars=<len(why_now)>)`.
   Truncate `focus_anchor` so a long heading doesn't bloat the
   events.jsonl line; the full record on disk holds the un-
   truncated value.

2. After a successful outcome-block append in
   `_reconcile_proposal_outcome(...)` (or whichever helper TB-188
   used for the amend path), emit:
   `_emit_event("ideation_proposal_reconciled", task_id=tb_id,
   decision_kind=<kind>, decision_actor=<actor>, commit=<sha or
   None>)`.

Add both event-type strings to `IDEATION_RELEVANT_EVENT_TYPES` in
`ap2/ideation.py` (line 84) so the events block in the ideation
prompt header (TB-169) shows them. The status-report cron renders
the unfiltered events tail (per `_events_block` doc in
`ap2/prompts.py:380-383`), so it picks up both new types
automatically — no further allowlist edits needed.

Failure isolation: if either emit raises (e.g. events.jsonl
fenced / unwritable), log a warning but do NOT roll back the
record write — the record on disk is the source of truth, the
event is observability metadata.

## Design

- Reuse the existing event-emit helper from `ap2/daemon.py` (or
  whichever module owns the atomic events.jsonl append; locate by
  greping for an existing emit call site like
  `ideation_state_written` or `task_complete`).
- Both new event-type strings follow the snake_case `<noun>_<verb>`
  convention used by existing event types (`task_complete`,
  `verification_failed`, `ideation_state_written`).
- No new event-schema doc; the field set is self-explanatory and
  parallels `task_complete`'s shape (task_id + status fields).
- The two emits are unconditional within their respective success
  paths — no env knob, no opt-out. Observability is on by default;
  the ideation prompt's existing event-block trim and 6KB byte cap
  (per `ap2/ideation.py:480-490`) limits volume.
- The new event types should be added to `IDEATION_RELEVANT_EVENT_TYPES`
  in alphabetical order with a comment line above the additions
  noting the TB-N attribution (consistent with the existing block-
  comment pattern in that constant's source).

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "ideation_proposal_recorded" ap2/tools.py` — emit site
  visible at the record-write helper (at least one match).
- `grep -nE "ideation_proposal_reconciled" ap2/tools.py` — emit
  site visible at the reconciliation helper (at least one match).
- `grep -nE "ideation_proposal_recorded" ap2/ideation.py` —
  event type added to the `IDEATION_RELEVANT_EVENT_TYPES` allowlist.
- `grep -nE "ideation_proposal_reconciled" ap2/ideation.py` —
  event type added to the `IDEATION_RELEVANT_EVENT_TYPES` allowlist.
- New unit test
  `ap2/tests/test_ideation_proposals.py::test_record_write_emits_recorded_event`
  pins that calling `do_board_edit({"action": "add_backlog",
  "blocked_on": "review", ...})` produces an
  `ideation_proposal_recorded` event in events.jsonl with
  `task_id` set to the allocated TB-N and a non-empty
  `focus_anchor` field.
- New unit test
  `ap2/tests/test_ideation_proposals.py::test_reconcile_emits_reconciled_event_on_complete`
  pins that a `task_complete` event for a previously-recorded TB-N
  produces an `ideation_proposal_reconciled` event with
  `decision_kind == "completed"` and `commit` populated to the
  short SHA.
- New unit test
  `ap2/tests/test_ideation_proposals.py::test_reconcile_emits_reconciled_event_on_reject`
  pins that an operator-queue `reject TB-N` drain produces an
  `ideation_proposal_reconciled` event with `decision_kind ==
  "rejected"` and `decision_actor == "operator"`.
- New unit test
  `ap2/tests/test_prompts.py::test_ideation_proposal_event_types_in_ideation_block`
  pins that both new types render in the events block when
  `include_types=IDEATION_RELEVANT_EVENT_TYPES` is passed (parallel
  to the existing TB-169 allowlist tests).

## Out of scope

- Surfacing record activity in `ap2 status` text/JSON or the web
  home page (the events.jsonl rendering already covers it via the
  /events page; dedicated UI is premature without volume).
- Backfilling events for records written before this lands (TB-188
  has produced 0 records so far; nothing to backfill).
- Tier-based event severity / colorization changes in the web view
  (out of scope for the emit wiring; can layer later if needed).
- Forwarding the new event types into the cron status-report
  snapshot's allowlist (status-report renders the unfiltered tail
  per `_events_block` default; no allowlist edit needed there).
