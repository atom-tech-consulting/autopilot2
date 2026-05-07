# Ideation State

_Last updated: 2026-05-07T09:27:30Z by ideation cron_

## Mission alignment

~2h since the prior cycle (07:25Z). Zero new events of any kind in
that window — last `task_complete` was TB-196 at 04:35Z (5h ago);
last operator action was approve-TB-195/196 at 04:08Z. Mission
alignment unchanged: the foundation remains shipped 4-deep at
TB-188 + TB-189 + TB-195 + TB-196, all anchored to goal.md L38-76
(ideation-quality-signal-collection focus); zero drift toward
ap2-meta polish.

Latest 5 completes considered (carries from prior cycle, still the
freshest goal-anchored work; nothing newer exists):

- TB-196 (`c48b6cb`, 04:35Z) — `ideation_proposal_recorded` +
  `ideation_proposal_reconciled` event emits + `IDEATION_RELEVANT_
  EVENT_TYPES` allowlist
- TB-195 (`f356e20`, 04:24Z) — `ap2 backfill-proposals [--dry-run]`
  CLI + `ap2/backfill.py`
- TB-189 (`a49763b`, 01:45Z) — `ap2 classify TB-N --delete-test
  <verdict>` CLI + chat verb
- TB-188 (`93892da`, 01:04Z) — per-proposal records under
  `.cc-autopilot/ideation_proposals/<TB-N>.json` + outcome
  reconciliation
- TB-194 (`cb09e91`, 00:54Z) — operator-queue ideate Active-check
  deferred to drain time

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: Foundation FULLY shipped at 4 seams.
    **TB-188** (`93892da`, 01:04Z) writes per-proposal records and
    reconciles outcomes on `task_complete` / `task_deleted` /
    drained `approve` / drained `reject`. **TB-189** (`a49763b`,
    01:45Z) gives the operator a retrospective `--delete-test`
    verdict surface. **TB-195** (`f356e20`, 04:24Z) ships the
    backfill CLI to seed records for ~50 historical
    ideation-authored TB-Ns since TB-121. **TB-196** (`c48b6cb`,
    04:35Z) emits `ideation_proposal_recorded` /
    `ideation_proposal_reconciled` events and adds them to
    `IDEATION_RELEVANT_EVENT_TYPES` so record activity surfaces
    in events.jsonl, the web /events page, and future ideation
    prompt event blocks.
  - Gaps:
    (1) **Volume**: `ideation_proposals/` still `.gitkeep`-only at
    09:27Z (verified directly); 0 `ideation_proposal_recorded`
    events in the tail. Operator's call when to run
    `ap2 backfill-proposals`; not a decision needing narrative
    judgment from ideation.
    (2) **Track-record feedback into the ideation prompt header**
    (carries) — wait-condition unchanged: "TB-195 backfill landing
    + 2-3 cycles of organic growth." TB-195 has landed (CLI
    available) but neither the manual-run nor the organic-growth
    half is yet satisfied.
    (3) **Insight aggregator from records → `ideation_quality.md`**
    (TB-175-shape) — operator-acked deferral at 01:57:58Z
    (operator_log.md) authoritative; off-table for ~3+ cycles
    after TB-188 landing AND requires record / verdict volume
    that doesn't exist (0 records, 0 verdicts).
  - Status: `in-progress`
  - Reasoning: foundation is shipped 4-deep; remaining gaps are
    all accumulation-blocked or operator-deferred. Nothing
    structurally changed in the 2h gap since the prior cycle,
    and nothing at all in the 5h since TB-196 landed.

## Non-goal risk check

None. No drift toward generic-task-scheduler, replace-operator-
judgment, multi-tenancy, real-time, or cross-project Non-goals.

## Considered & deferred this cycle

- **Inject "Recent ideation proposals (last N)" block into ideation
  prompt header** (carries) — TB-163-pattern mirrored for the new
  TB-188 records stream. Closes gap (2). Wait-condition: TB-195
  backfill run + ≥2 organic ideation cycles producing records.
  Only the first half is currently satisfied (CLI available; not
  run).
- **`ap2 proposals [--unclassified]` operator CLI to list/filter
  records** (carries) — sibling of pending-review surface
  (TB-151-shape). Risk: parallel-surface to file-cat; impact
  gated on records existing on disk (0). Defer until ≥10 records
  exist (likely after backfill runs).
- **Surface "unclassified proposals" count + TB-Ns in `ap2 status`
  + cron status-report** (carries) — TB-151-pattern observability
  extension. Gated on records existing on disk; pre-backfill the
  surface would always be empty.
- **Auto-run `ap2 backfill-proposals` on daemon startup as
  idempotent migration** (carries) — would close the operator-toil
  gap, but steps on operator-owned migration timing (operator may
  want to review `--dry-run` output first) and adds startup-time
  complexity. Operator's call.
- **Ideation self-evaluates delete-test before queueing each
  proposal** (carries) — semantic check beyond TB-164's structural
  Why-now marker. Risk: agent self-grading is unreliable until
  operator classify-verdict ground-truth exists. Defer until ≥10
  operator verdicts exist.
- **`ap2 classify --next` interactive bulk walk-through** (carries)
  — parallel-surface-adjacent (operator can use `ap2 classify
  TB-N` per-item today); defer until the operator surfaces
  bulk-classify pain.
- **TB-175 re-prop** (carries) — operator-acked deferral at
  01:57:58Z; off-table for ~3+ cycles AND requires record /
  verdict volume that doesn't exist yet.
- **`ap2 ideate --hint`** (TB-184), **`ap2 frozen`** (TB-185),
  **briefing-bullet linter** (TB-172): authoritative rejects;
  will not re-propose.

Rejection-pattern note (n=4, unchanged): rejections cluster on
"creates parallel surface OR doesn't generalize OR off-focus OR
wack-a-mole." With 0 proposals this cycle the filter isn't
exercised — but each deferred candidate above was filtered against
it (proposals-CLI / unclassified-surface / self-evaluator /
bulk-classify all trip parallel-surface or premature-without-volume).

## Cycle observations

(none this cycle.)

## Decisions needed from operator

(none this cycle.)

## Proposals this cycle

0 proposals.

Foundation remains shipped 4-deep (TB-188 + TB-189 + TB-195 +
TB-196); all identified next-step gaps are still accumulation-
blocked (0 records on disk, 0 verdicts) or operator-deferred
(TB-175 ~3+ cycles post-TB-188). Nothing changed in the 2h since
the prior cycle, and nothing at all in the 5h since TB-196 landed.
Proposing now would either:

(a) duplicate work the existing seams already do (each Considered
    & deferred candidate substantially overlaps an existing
    surface);
(b) trip the n=4 rejection-pattern filter (parallel-surface /
    premature-without-volume / off-focus / wack-a-mole); or
(c) front-run the operator's natural next action
    (`ap2 backfill-proposals`).

Quality > slot-fill. The signal-collection focus is in a
deliberate accumulation phase; the right ideation behavior is to
wait for record / verdict volume before proposing the next layer
(track-record-into-prompt / aggregator / unclassified-surface).
