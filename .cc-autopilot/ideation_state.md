# Ideation State

_Last updated: 2026-05-07T01:05:30Z by ideation cron_

## Mission alignment

~2h since prior cycle (22:53Z); operator was active 23:35Z–01:04Z
landing TB-192 (`f271953`, 23:45Z, insights-index commit cohesion),
TB-193 (`01e2d81`, 00:20Z, `ap2 update-goal` op), TB-194 (`cb09e91`,
00:54Z, defer ideate Active-check to drain), and TB-188's re-attempt
(`93892da`, 01:04Z) — the foundational per-proposal record capture
is now live. Approve→add cadence: TB-188 + TB-189 approved 00:17Z;
TB-189 still `@blocked:TB-188` in Backlog (auto-promotes on next
tick now that TB-188 is Complete). Mission alignment improved this
cycle: the focus has its first shipped seam (TB-188), and TB-193
gives the operator a runtime channel to clarify scope without daemon
restart.

Latest 5 completes considered:

- TB-188 (`93892da`, 01:04Z) — per-proposal records at
  `ideation_proposals/<TB-N>.json` + outcome reconciliation; public
  `extract_goal_anchor` / `extract_why_now` helpers exposed
- TB-194 (`cb09e91`, 00:54Z) — operator-queue ideate Active-check
  deferred from append to drain time
- TB-193 (`01e2d81`, 00:20Z) — `update_goal` op + `ap2 update-goal`
  CLI; goal.md safely refreshable while daemon runs
- TB-192 (`f271953`, 23:45Z) — insights/_index.md regen rides along
  in `state: ideation` commit
- TB-191 (`2ca1f0e`, 19:59Z) — ideation_state schema rewrite

First TB shipped *for* the focus item (TB-188); supporting infra
clusters tightly around it.

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: **TB-188 landed** (`93892da`, 01:04Z) — per-
    proposal records at `.cc-autopilot/ideation_proposals/<TB-N>.json`
    + outcome reconciliation on `task_complete` / `task_deleted` /
    drained `approve` / drained `reject`; public
    `extract_goal_anchor` + `extract_why_now` helpers in
    `ap2/tools.py` for reuse. **TB-189** still in Backlog
    (`@blocked:TB-188`; auto-promotes next tick) — operator-authored
    delete-test verdict surface. **TB-193** (`01e2d81`) gives the
    operator a runtime goal.md edit channel — strengthens the
    operator-intent signal path.
  - Gaps:
    (1) **No historical signal in records** — TB-188 only writes
    records for NEW ideation `add_backlog` calls; ~50+ historical
    ideation-authored TB-Ns since TB-121 (visible in
    operator_log.md) have no record. TB-189's classifier will only
    apply to TB-N ≥ 195 unless backfilled. Addressed by Proposal A
    this cycle.
    (2) **Record activity invisible in events.jsonl** — TB-188's
    record write + reconciliation produce no events, so the
    ideation prompt's events block (TB-169 allowlist) and the web
    /events page can't see record activity. Addressed by
    Proposal B this cycle.
    (3) **Track-record feedback into ideation prompt header**
    (carries) — once records accumulate (Proposal A backfill +
    natural growth), ideation should read its own track record at
    proposal time. Premature until ≥10-20 records exist.
    (4) **Insight aggregator from records → `ideation_quality.md`**
    (carries; gap blocked on operator sequencing decision; see
    Decisions needed).
  - Status: `in-progress`
  - Reasoning: focus is ~7h old; the foundational seam landed this
    cycle; gaps (1) + (2) are addressed below; gap (3) waits on
    volume; gap (4) waits on operator clarification.

## Non-goal risk check

None. Both proposals anchor strictly to ideation signal capture
(per-proposal records, observable events). Proposal A is operator-
driven CLI (one-off, not auto-cron), respecting the operator-in-
the-loop constraint. No drift toward generic-task-scheduler,
replace-operator-judgment, multi-tenancy, real-time, or cross-
project Non-goals.

## Considered & deferred this cycle

- **Re-prop TB-175 framing (insight regenerator).** Carried; still
  gated on operator clarification of the 05:15Z reject + needs
  records to aggregate. See Decisions needed.
- **Track-record prompt-header block (gap 3).** Premature: 0
  records exist (`ideation_proposals/` has only `.gitkeep`).
  Re-evaluate after Proposal A lands + 2-3 cycles of fresh
  proposals.
- **TB-188 outcome-block re-attempt edge case.** Brief says
  "append-once-then-amend"; behavior on a second `task_complete`
  for an already-reconciled TB-N (re-attempt scenario) is
  ambiguous. Speculative without reading the impl; defer to next
  cycle if records show inconsistencies.
- **Operator-facing CLI to inspect records** (`ap2 proposals show
  TB-N`). TB-188's brief explicitly puts CLI inspection in Out of
  scope; `cat <file>` works.
- **`ap2 ideate --hint`** (TB-184), **`ap2 frozen`** (TB-185),
  **wack-a-mole bullet linter** (TB-172): authoritative rejects;
  will not re-propose.

Rejection-pattern note (n=4, unchanged): rejections cluster on
"creates parallel surface OR doesn't generalize OR off-focus."
Both proposals this cycle pass the filter — backfill is a one-off
operator CLI feeding TB-188's existing record format (no parallel
surface; reuses extractors); event emission reuses events.jsonl +
the existing IDEATION_RELEVANT_EVENT_TYPES allowlist (no parallel
surface, generalizes the existing event-routing pattern).

## Cycle observations

- Backfill heuristic for ideation-authored vs operator-authored
  TB-Ns: post-approval the `@blocked:review` codespan is stripped
  from TASKS.md, so we can't distinguish from current board state.
  Cleanest test: structural — briefing passes BOTH TB-161 anchor
  AND TB-164 Why-now validators (operator `--skip-goal-alignment`
  adds bypass both). Documented in Proposal A's Design.
- TB-188 first-attempt verification_failed (`6fbcef5`) hit a shell-
  bullet trap: `test -d .cc-autopilot/ideation_proposals` checks a
  directory the impl only creates on first use; re-attempt seeded
  with `.gitkeep` to pass. Class of pitfall (auto-verify gate for
  lazily-created artifacts) — too narrow to mechanical-fix per the
  TB-172 wack-a-mole lesson, but worth carrying once: if a similar
  trap re-appears in the next 2 cycles, promote to a structured
  discussion.

(Dropped from prior cycle: "TB-188 lacks events.jsonl emission" —
promoted to Proposal B. ".cc-autopilot/insights/_index.md still
empty" — same condition, but no longer informing this cycle
differently; gap is structurally tied to record accumulation,
which Proposal A addresses.)

## Decisions needed from operator

- Decision needed: TB-175 re-proposal sequencing — TB-188 has now
  landed (`93892da`), so the data substrate for an insight
  aggregator over operator_log.md + record outcomes exists in
  principle (records start accumulating this cycle; if Proposal A
  lands + gets approved, ~50 historical proposals get backfilled
  in one shot). Should ideation (a) wait 2-3 cycles for record
  accumulation then re-propose a TB-175-shape insight regenerator,
  or (b) treat the 05:15Z reject as definitive and stop considering
  the path? Operator action: now that `ap2 update-goal` (TB-193) is
  live, edit goal.md `## Current focus` body to scope insight
  aggregation in or out, OR queue a clarifying line via
  `ap2 reject` / operator_log append. Unblock-condition: the
  deferred slot stops compounding indefinitely — ideation either
  commits a future proposal slot to the work or drops it from the
  candidate pool. (Carried; re-articulated this cycle because
  TB-188 has shipped — substrate exists — and TB-193 now provides
  the runtime operator-side channel to clarify scope.)

## Proposals this cycle

4 slots available; proposing 2:

1. **TB-195** Backfill `.cc-autopilot/ideation_proposals/<TB-N>.json`
   records for historical ideation-authored proposals — addresses
   gap (1).
2. **TB-196** Emit `ideation_proposal_recorded` +
   `ideation_proposal_reconciled` events when TB-188 records are
   written / amended — addresses gap (2).

Slots 3 + 4 left unused: candidates considered above either tread
already-rejected territory, depend on signal volume that doesn't
yet exist (gap 3), or risk the parallel-surface / off-focus
rejection pattern. Quality > slot-fill given the n=4 rejection
density.
