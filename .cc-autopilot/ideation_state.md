# Ideation State

_Last updated: 2026-05-07T05:20:00Z by ideation cron_

## Mission alignment

~2h since prior cycle (03:14Z). Operator drained two approvals at
04:08:05Z (TB-195 + TB-196), both auto-promoted and shipped clean
first-attempt: **TB-195** Complete at 04:24:14Z (`f356e20`,
`ap2 backfill-proposals [--dry-run]` CLI + `ap2/backfill.py`),
**TB-196** Complete at 04:35:16Z (`c48b6cb`,
`ideation_proposal_recorded` + `_reconciled` event emits + 7 new
tests). The signal-collection foundation is now FOUR seams deep —
TB-188 (records), TB-189 (classify CLI), TB-195 (backfill CLI),
TB-196 (event emits) — and the Backlog is empty. Mission alignment
strong: every shipped seam directly cites goal.md L38-76
(ideation-quality-signal-collection focus); none drift toward
ap2-meta polish.

Latest 5 completes considered:

- TB-196 (`c48b6cb`, 04:35Z) — `ideation_proposal_recorded` +
  `ideation_proposal_reconciled` events emitted from
  `write_ideation_proposal_record` / `reconcile_proposal_outcome`;
  added to `IDEATION_RELEVANT_EVENT_TYPES` per TB-169
- TB-195 (`f356e20`, 04:24Z) — `ap2 backfill-proposals [--dry-run]`
  operator CLI; scans operator_log.md + briefings + events.jsonl,
  reuses TB-188 helpers (`extract_goal_anchor` / `extract_why_now`)
- TB-189 (`a49763b`, 01:45Z) — `ap2 classify TB-N --impact <verdict>`
  CLI + chat verb routed via operator_queue_append
- TB-188 (`93892da`, 01:04Z) — per-proposal records at
  `.cc-autopilot/ideation_proposals/<TB-N>.json`; outcome
  reconciliation on 4 terminal events
- TB-194 (`cb09e91`, 00:54Z) — operator-queue ideate Active-check
  deferred from append to drain time

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: Foundation FULLY shipped at 4 seams. **TB-188**
    (`93892da`, 01:04Z) writes per-proposal records and reconciles
    outcomes on `task_complete` / `task_deleted` / drained `approve` /
    drained `reject`. **TB-189** (`a49763b`, 01:45Z) gives the
    operator a retrospective `--impact` verdict surface. **TB-195**
    (`f356e20`, 04:24Z) ships the backfill CLI to seed records for
    ~50 historical ideation-authored TB-Ns since TB-121. **TB-196**
    (`c48b6cb`, 04:35Z) emits `ideation_proposal_recorded` /
    `ideation_proposal_reconciled` events and adds them to
    `IDEATION_RELEVANT_EVENT_TYPES` so record activity surfaces in
    events.jsonl, the web /events page, and future ideation prompt
    event blocks. The goal.md L57-65 dual-purpose substrate
    (near-term evaluation + long-term agent-context) now has live
    capture paths from both sides AND historical-seed AND event
    visibility.
  - Gaps:
    (1) **Volume**: `ideation_proposals/` directory still holds only
    `.gitkeep` (verified 05:19Z); operator hasn't yet invoked
    `ap2 backfill-proposals`. 0 `ideation_proposal_recorded` events
    in the tail. Operator's call when to run; not a decision
    needing narrative judgment from ideation.
    (2) **Track-record feedback into the ideation prompt header**
    (carries) — premature, prior cycle's wait-condition unchanged:
    "TB-195 backfill landing + 2-3 cycles of organic growth." TB-195
    has now LANDED (CLI available), but neither the manual-run nor
    the organic-growth half is satisfied. Wait at least 2-3 more
    cycles after the operator runs backfill.
    (3) **Insight aggregator from records → `ideation_quality.md`**
    (TB-175-shape) — operator-acked deferral at 01:57:58Z still
    authoritative; off-table for ~3+ ideation cycles after TB-188
    lands. We're at ~4h post-TB-188 landing (cycles 2-3 elapsed
    by clock); insufficient by both clock and accumulation criteria
    (0 records, 0 verdicts).
  - Status: `in-progress`
  - Reasoning: foundation is shipped 4-deep; remaining gaps are all
    accumulation-blocked or operator-deferred. No new high-confidence
    structural gap surfaced this cycle that wouldn't compete with
    already-deferred work or trip the rejection-pattern filter (n=4:
    parallel surface / doesn't generalize / off-focus / wack-a-mole).

## Non-goal risk check

None. No drift toward generic-task-scheduler, replace-operator-judgment,
multi-tenancy, real-time, or cross-project Non-goals.

## Considered & deferred this cycle

- **Inject "Recent ideation proposals (last N)" block into ideation
  prompt header** — TB-163-pattern mirrored for the new TB-188
  records stream. Closes gap (2) directly. Deferred: prior cycle's
  wait-condition is "TB-195 backfill landing + 2-3 cycles of organic
  growth"; only the first half is satisfied. Re-propose after the
  operator runs backfill AND ≥2 organic ideation cycles produce
  records.
- **`ap2 proposals [--unclassified]` operator CLI to list/filter
  records** — sibling of `ap2 status`'s pending-review surface
  (TB-151-shape). Risk: parallel-surface to file-cat; impact gated
  on records existing on disk (currently 0). Defer until ≥10
  records exist (likely after backfill runs).
- **Surface "unclassified proposals" count + TB-Ns in `ap2 status`
  + cron status-report** — TB-151-pattern observability extension.
  Gated on records existing on disk; pre-backfill the surface would
  always be empty. Defer until backfill has run at least once.
- **Auto-run `ap2 backfill-proposals` on daemon startup as
  idempotent migration** — would close the operator-toil gap, but
  steps on operator-owned migration timing (operator may want to
  review `--dry-run` output first) and adds startup-time complexity.
  Defer; operator's call.
- **Ideation self-evaluates delete-test before queueing each
  proposal** (semantic check beyond TB-164's structural Why-now
  marker). Risk: agent self-grading is unreliable until operator
  classify-verdict ground-truth exists; would compete with the
  signal-collection focus rather than serve it. Defer until ≥10
  operator verdicts exist.
- **`ap2 classify --next` interactive bulk walk-through** — bulk-
  classify CLI over unclassified records. Parallel-surface-adjacent
  (operator can use `ap2 classify TB-N` per-item today); defer until
  the operator surfaces bulk-classify pain.
- **TB-175 re-prop** (carries) — operator-acked deferral at
  01:57:58Z; off-table for ~3+ cycles AND requires record/verdict
  volume that doesn't exist yet.
- **`ap2 ideate --hint`** (TB-184), **`ap2 frozen`** (TB-185),
  **briefing-bullet linter** (TB-172): authoritative rejects;
  will not re-propose.

Rejection-pattern note (n=4, unchanged): rejections cluster on
"creates parallel surface OR doesn't generalize OR off-focus OR
wack-a-mole." With 0 proposals this cycle, the filter isn't
exercised — but the deferred candidates above were all filtered
against it (proposals-CLI / unclassified-surface / self-evaluator
/ bulk-classify all trip parallel-surface or premature-without-
volume).

## Cycle observations

- TB-195 + TB-196 both landed clean first-attempt (no
  verification_failed / verification_partial), suggesting the
  briefing-quality regression after the recent TB-188/TB-189
  shell-bullet pitfalls (`test -d` pre-creation gap on TB-188;
  unquoted Python literal on TB-189) was a one-cycle blip rather
  than a class signal. Carrying once: if the next 3 task runs all
  land clean first-attempt, drop this observation entirely as a
  resolved blip.

(Dropped from prior cycle: TB-189 Python-shell-bullet trap — TB-195
+ TB-196 didn't recur the pattern; situation resolved without
carry-over need. Rejection-pattern note promoted into Considered &
deferred section's filter framing.)

## Decisions needed from operator

(none this cycle.)

## Proposals this cycle

0 proposals.

Foundation is shipped 4-deep (TB-188 + TB-189 + TB-195 + TB-196);
all identified next-step gaps are accumulation-blocked (no records
on disk, no verdicts in operator_log) or operator-deferred (TB-175
~3+ cycles post-TB-188). Proposing now would either:

(a) duplicate work the existing seams already do (the Considered
    & deferred candidates each substantially overlap existing
    surfaces); or
(b) trip the n=4 rejection-pattern filter (parallel-surface /
    premature-without-volume / off-focus / wack-a-mole); or
(c) front-run the operator's natural next action (`ap2
    backfill-proposals`) which they may want to review via
    `--dry-run` before committing.

Quality > slot-fill. The signal-collection focus is in a
deliberate accumulation phase; the right ideation behavior is to
wait for record/verdict volume before proposing the next layer
(track-record-into-prompt / aggregator / unclassified-surface).
