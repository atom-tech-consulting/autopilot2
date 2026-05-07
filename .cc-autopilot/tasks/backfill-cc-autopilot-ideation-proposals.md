# Backfill `.cc-autopilot/ideation_proposals/<TB-N>.json` records for historical ideation-authored proposals

## Goal

Current focus: ideation quality signal collection. TB-188
(`93892da`) captures per-proposal records prospectively for new
ideation `add_backlog` calls but produces no records for the ~50
historical ideation-authored TB-Ns since TB-121's review-gate
landed (TB-138 through TB-194, visible as `applied operator-queued
add_backlog → TB-N` lines in operator_log.md). Without backfill,
TB-189's delete-test classifier only operates on TB-N ≥ 195,
leaving every recent rejection (TB-172, TB-175, TB-184, TB-185)
and every recent ideation-authored Complete (TB-186, TB-187,
TB-191, TB-192, TB-194, TB-188 itself) outside the structured-
record scope the focus exists to instrument.

Why now: TB-188 landed at 01:04Z this cycle. The operator's pivot
at 2026-05-06T18:07Z explicitly named signal volume — not prompt
craft — as the bottleneck. Backfilling turns existing
operator_log.md + briefing files + events.jsonl into the first
batch of structured records the focus needs, so TB-189's verdict
surface has historical proposals to classify the moment it lands.
Without this, signal volume builds at the natural ideation cadence
(2-4 proposals per cycle), delaying any track-record-aware
prompt-header work (gap 3 of the focus assessment) by weeks.

## Scope

Add a new `ap2 backfill-proposals [--dry-run]` operator CLI
subcommand (operator-driven; not exposed via the operator queue,
not auto-triggered by daemon ticks).

Detection: enumerate every TB-N referenced by an `applied
operator-queued add_backlog → TB-N` line in `operator_log.md`,
then classify ideation-authored vs operator-authored using a
structural test on the corresponding briefing file at
`.cc-autopilot/tasks/<slug>.md` — a TB-N is ideation-authored iff
its briefing passes BOTH:

- `extract_goal_anchor(briefing)` returns non-None (TB-161 anchor
  present)
- `extract_why_now(briefing)` returns non-None (TB-164 Why-now
  paragraph present)

Reuse the public helpers from `ap2/tools.py` (TB-188 exposed both).
TB-Ns whose briefings fail either test (typical of `ap2 add
--skip-goal-alignment` adds) are skipped. If a record already
exists at `.cc-autopilot/ideation_proposals/<TB-N>.json` the TB-N
is skipped (idempotent re-runs).

For each detected ideation-authored TB-N without a record:

1. Read its briefing → extract `focus_anchor`, `why_now`,
   `briefing_path`. `blocked_on` defaults to `review` for
   backfilled records (the historical add WAS gated on review;
   the codespan was simply stripped on approval).
2. Use the matching `applied operator-queued add_backlog` line's
   timestamp as `proposed_at`.
3. Write the base record via the existing TB-188
   `write_ideation_proposal_record` helper (reuse, do not
   duplicate).
4. Reconcile the outcome by inspecting current state + events:
   - If TB-N is in `## Complete`, find the LAST `task_complete`
     event for that TB-N in events.jsonl; write outcome with
     `decision_kind` = `completed` (or `verification_failed` if
     the latest task_complete carries that status),
     `decision_actor` = `daemon` (or `verifier`), `commit`
     populated.
   - Else if `rejected ideation proposal → TB-N` line exists in
     operator_log.md, write `decision_kind=rejected`,
     `decision_actor=operator`, `reason` extracted from the log
     line.
   - Else if `applied operator-queued delete → TB-N` line exists,
     write `decision_kind=deleted`, `decision_actor=operator`.
   - Else if `applied operator-queued approve → TB-N` line exists
     but TB-N is not yet Complete/Rejected/Deleted, write
     `decision_kind=approved`, `decision_actor=operator` (a future
     terminal event re-reconciles via TB-188's normal path —
     idempotent, since the helper is shared).
   - Else (proposed but no terminal evidence): leave the record
     without an outcome block (in-flight).

`--dry-run` reads everything but writes nothing — prints to stdout
a one-line summary per TB-N (`<TB-N> would write record
focus=<anchor-prefix> outcome=<kind>`) and an aggregate count.

Exit codes: 0 on success (or no-op when nothing to backfill),
non-zero on read errors.

## Design

- Single new file `ap2/backfill.py` exposing
  `backfill_proposals(cfg, dry_run: bool = False) -> BackfillReport`,
  called from `cli.cmd_backfill_proposals`.
- Reuse `write_ideation_proposal_record` and the outcome-block
  schema from TB-188's implementation in `ap2/tools.py`; do NOT
  re-implement the JSON shape.
- Reuse `extract_goal_anchor` + `extract_why_now` from
  `ap2/tools.py`.
- Read events.jsonl once into memory (~few hundred events today),
  build a `dict[TB-N, list[event]]` index, then iterate ideation-
  authored TB-Ns. Avoids O(N×M) scanning.
- Operator-log parsing: line-shape regex matching the existing
  audit-line format (no changes to the log format — purely a
  reader). Centralize in a small `parse_operator_log_lines(path)`
  helper inside `ap2/backfill.py`; tests pin the regex against
  every audit-line shape currently in the file (`add_backlog`,
  `approve`, `reject`, `delete`, `unfreeze`, `update`,
  `add_ready`, `move_to_backlog`, `ideate`, `rejected ideation
  proposal`).
- No daemon-tick hook, no cron registration, no MCP exposure. The
  backfill is invoked once by the operator after this lands;
  subsequent proposals get records via TB-188's normal write path.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "backfill-proposals|cmd_backfill_proposals" ap2/cli.py`
  — CLI subcommand registered.
- `test -f ap2/backfill.py` — new module present.
- `grep -nE "backfill_proposals" ap2/backfill.py` — helper
  function present.
- `grep -nE "extract_goal_anchor|extract_why_now|write_ideation_proposal_record" ap2/backfill.py`
  — TB-188 helpers reused, not duplicated.
- New unit test
  `ap2/tests/test_backfill_proposals.py::test_backfill_writes_record_for_ideation_authored_complete`
  — fixture project with operator_log line `applied operator-
  queued add_backlog → TB-X`, briefing file with anchor + Why-now,
  events.jsonl with `task_complete` for TB-X status=complete; after
  `backfill_proposals(cfg)`, file
  `.cc-autopilot/ideation_proposals/TB-X.json` exists with both
  base fields AND `outcome` block (`decision_kind=completed`).
- New unit test
  `ap2/tests/test_backfill_proposals.py::test_backfill_writes_outcome_for_rejected_proposal`
  — fixture with operator_log lines `applied operator-queued
  add_backlog → TB-Y` AND `rejected ideation proposal → TB-Y
  (...): <reason>`; after backfill, record outcome has
  `decision_kind=rejected` and `reason` populated from the log
  line.
- New unit test
  `ap2/tests/test_backfill_proposals.py::test_backfill_skips_operator_authored_briefings`
  — fixture briefing missing the Why-now paragraph; backfill
  writes no record for that TB-N.
- New unit test
  `ap2/tests/test_backfill_proposals.py::test_backfill_is_idempotent`
  — running twice in a row produces identical disk state on the
  second pass; second pass's report names zero new records.
- New unit test
  `ap2/tests/test_backfill_proposals.py::test_dry_run_writes_nothing`
  — after `backfill_proposals(cfg, dry_run=True)`, the records
  directory is unchanged; the function's report still names TB-Ns
  it would have written.

## Out of scope

- Auto-running backfill on daemon start or via cron (operator-
  driven one-off only — pairs with the `ap2 install` / migrate
  pattern).
- Backfilling TB-Ns from BEFORE the TB-121 review gate landed (no
  briefing-validator structure existed; classifier signal too
  noisy).
- Retroactive `ideation_proposal_recorded` event emission
  (Proposal B handles forward emission; backfill records are
  intentionally silent on the events stream to avoid retroactively
  flooding the ideation prompt's events block).
- New CLI subcommands beyond `backfill-proposals` (no `ap2
  proposals list`, no inspection UI — TB-188's Out-of-scope
  holds).
