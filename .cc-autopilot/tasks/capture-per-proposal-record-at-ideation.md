# Capture per-proposal record at ideation `add_backlog`; reconcile outcome on terminal events

## Goal

Current focus: ideation quality signal collection requires per-proposal
structured records that persist across cycles and link proposal-time
context to terminal outcome. Today, when ideation calls
`do_board_edit({"action": "add_backlog", ...})` (per
`ap2/ideation.default.md` L262), the artifacts are: a TB-N row in
`TASKS.md`, a briefing file in `.cc-autopilot/tasks/`, and an audit
line in `operator_log.md`. None of these capture the structured cycle
context (which goal.md anchor was cited, what gap the proposal
addressed, what the why-now sentence was) in a queryable form, and
none link the proposal back to its eventual outcome (operator-approved?
operator-rejected? task-completed with `status=complete` or
`status=verification_failed`?).

This task creates the seed record. Without it, every downstream
signal-collection task (acceptance-rate aggregation, retrospective
classifier, track-record prompt block, rejection-pattern miner) has
nothing to query.

Why now: goal.md's failure-mode statement (lines 66-76) explicitly
names "goal-shaped pro-forma compliance" as the failure mode signal
collection exists to detect, with the delete-test (lines 61-70) as
the diagnostic. The delete-test can only be applied retrospectively
against a structured record that ties proposal context to landed
outcome — the operator's 18:07Z pivot called out signal volume as the
bottleneck, and this is the upstream record that produces the
volume.

## Scope

When `do_board_edit` (or its wrapper) handles an `add_backlog` whose
`blocked_on` field contains the literal token `review` (the TB-121
ideation marker — operator-driven adds via `ap2 add` typically don't
carry it), write a JSON record at
`.cc-autopilot/ideation_proposals/<TB-N>.json` containing:

- `tb_id`: the allocated TB-N (e.g. `"TB-188"`).
- `proposed_at`: UTC ISO-8601 timestamp of the queue-append.
- `focus_anchor`: the goal.md `## Current focus` heading or `## Done
  when` bullet substring matched by the existing TB-161 anchor logic
  in `_validate_briefing_structure` (`ap2/tools.py`); reuse the
  matcher rather than duplicating it.
- `why_now`: the line-anchored `Why now:` paragraph extracted by
  TB-164 logic in the same validator; reuse the existing extractor.
- `briefing_path`: relative path to the briefing file under
  `.cc-autopilot/tasks/`.
- `blocked_on`: the raw `blocked_on` codespan body so legacy / mixed-
  blocker proposals (`review,TB-N` per TB-187) round-trip.

On any subsequent terminal event for the same TB-N — `task_complete`,
`task_deleted`, drained operator-queue `approve`, drained operator-
queue `reject` — read the existing JSON record (skip if absent: legacy
proposals from before this lands), append an `outcome` block, and
atomically rewrite. The block:

- `decision_kind`: one of `approved`, `rejected`, `deleted`,
  `completed`, `verification_failed`.
- `decision_ts`: UTC ISO-8601 timestamp of the terminal event.
- `decision_actor`: `operator` (approve/reject/delete via queue),
  `verifier` (verification_failed), or `daemon` (completed).
- `commit`: short SHA when applicable (task_complete commit), else
  `null`.
- `reason`: the free-text rejection reason from the matching
  `operator_log.md` line for `rejected`/`deleted`; empty string
  otherwise.

Add `.cc-autopilot/ideation_proposals/` to `TASK_AGENT_FENCED_PATHS`
(parallel to how `operator_queue.jsonl` is fenced per TB-143). Add
the directory to `.cc-autopilot/.gitignore` if state-files convention
matches existing fenced-record paths; otherwise commit records as
state files via the TB-126 narrowed-state-commit path.

The wiring point for terminal-event reconciliation can be the
existing daemon hooks that emit `task_complete` and the operator-
queue drain handler in `ap2/daemon.py` / `ap2/tools.py` — pick
whichever surface co-locates cleanly. A single helper
`_reconcile_proposal_outcome(tb_id, decision_kind, ...)` should be
the only write path.

## Design

- Skip silently when `blocked_on` does not contain `review`: this is
  the operator-driven path (e.g. `ap2 add TB-N`) where the proposal
  is not ideation-authored. Tested by `test_no_record_for_non_review_add_backlog`.
- Records are append-once-then-amend: first write at proposal time,
  one append on terminal event. No multi-amend / no event log inside
  the record (events.jsonl is the audit log).
- Atomic write via `tempfile.NamedTemporaryFile` + rename, mirroring
  `ideation_state_write`'s contract (TB-90 precedent).
- Field extraction reuses the existing TB-161 / TB-164 validators in
  `ap2/tools.py:_validate_briefing_structure` rather than re-parsing.
  If the validator's helpers are private, expose them via a small
  public `extract_goal_anchor(briefing) -> str | None` /
  `extract_why_now(briefing) -> str | None` API kept in
  `ap2/tools.py`.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `test -d .cc-autopilot/ideation_proposals` — directory created at
  first proposal (test fixtures populate this in unit tests).
- New unit test `ap2/tests/test_ideation_proposals.py::test_record_written_on_add_backlog_with_review_blocker`
  pins that calling `do_board_edit({"action": "add_backlog",
  "blocked_on": "review", ...})` produces a JSON file with the six
  required keys (`tb_id`, `proposed_at`, `focus_anchor`, `why_now`,
  `briefing_path`, `blocked_on`).
- New unit test `ap2/tests/test_ideation_proposals.py::test_outcome_reconciled_on_task_complete`
  pins that a `task_complete` event for the proposal's TB-N appends
  an `outcome` block with `decision_kind == "completed"` and the
  commit SHA populated.
- New unit test `ap2/tests/test_ideation_proposals.py::test_outcome_reconciled_on_operator_reject`
  pins that an operator-queue `reject TB-N --reason "..."` drains
  to an `outcome` block with `decision_kind == "rejected"` and the
  reason text from operator_log.md.
- New unit test `ap2/tests/test_ideation_proposals.py::test_no_record_for_non_review_add_backlog`
  pins that `do_board_edit({"action": "add_backlog", "blocked_on":
  ""})` does NOT write a record file.
- `grep -nE "ideation_proposals" ap2/tools.py ap2/daemon.py` — write
  paths visible in code (one or both files; at least one match).
- `grep -nE "ideation_proposals" ap2/sandbox.py` — directory wired
  into `TASK_AGENT_FENCED_PATHS`.
- New unit test pins that `extract_goal_anchor` and `extract_why_now`
  helpers are public on `ap2.tools` and round-trip a representative
  briefing string to the expected substrings.

## Out of scope

- Insight regeneration / dashboard surfaces (separate proposal —
  closes gap (3) of the focus assessment).
- Operator CLI to inspect records (`ap2 proposal show TB-N`) —
  records are JSON; `cat` works.
- Schema migration for proposals already on disk before this lands;
  only NEW proposals get records.
- Per-proposal record reads from the ideation prompt header
  (track-record block) — depends on signal volume; deferred.
## Attempts

### 2026-05-07 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `test -d .cc-autopilot/ideation_proposals` — directory created atfirst proposal (test fixtures populate this in unit tes
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260507T002033Z-TB-188.prompt.md`, `stream: .cc-autopilot/debug/20260507T002033Z-TB-188.stream.jsonl`, `messages: .cc-autopilot/debug/20260507T002033Z-TB-188.messages.jsonl`
