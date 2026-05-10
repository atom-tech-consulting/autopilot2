# Ideation State

_Last updated: 2026-05-10T00:38:17Z by ideation cron_

## Mission alignment

13th consecutive 0-proposal cycle from ideation since TB-196 landed
(`c48b6cb`, 2026-05-07T04:35Z, ~67h ago). Operator broke a ~46h
silence at 2026-05-10T00:24Z by directly adding TB-197 via
`ap2 add` (operator-authored, not ideation-routed) — completed
14min later (`b6488d9`, 00:38Z). TB-197 added an always-rendered
"next scheduled ideation" gate-state card to the web `/` overview
with five tinted variants mirroring `_maybe_ideate`'s decision
logic. This is **orthogonal to last cycle's (a)/(b)/(c) ask**: not
backfill, not goal.md edit, not ack — instead, an observability
investment on the cadence-visibility axis. Implicit signal: the
operator is not blocked by the deliberate-pause posture, just
choosing to enrich gate-state visibility while volume accumulates.
Slot count this cycle is 5 (up from 3 — 0-backlog under the
configured threshold), but available-slots ≠ available-aligned-work.

Latest 5 completes considered:

- TB-197 (`b6488d9`, 00:38Z) — web `/` overview "next ideation"
  gate-state card (operator-authored)
- TB-196 (`c48b6cb`, 2026-05-07T04:35Z) — `ideation_proposal_recorded`
  + `ideation_proposal_reconciled` event emits + allowlist
- TB-195 (`f356e20`, 04:24Z) — `ap2 backfill-proposals [--dry-run]`
  CLI (dry-run shows 14 historical candidates)
- TB-189 (`a49763b`, 01:45Z) — `ap2 classify --delete-test <verdict>`
  CLI + chat verb
- TB-188 (`93892da`, 01:04Z) — per-proposal records under
  `.cc-autopilot/ideation_proposals/<TB-N>.json` + outcome
  reconciliation

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: foundation shipped 4-deep — TB-188 (records
    on add_backlog + reconcile on terminal events), TB-189 (operator
    delete-test verdict CLI/chat), TB-195 (backfill of ~14 historical
    ideation-authored TB-Ns), TB-196 (events.jsonl + prompt-block
    visibility for record activity). Adjacent cadence-visibility
    surface shipped via TB-197 (web `/` always-rendered gate-state
    card) — operator-authored complement to the foundation.
  - Gaps:
    (1) **Volume**: `ideation_proposals/` still `.gitkeep`-only at
    00:38Z (verified directly via `ls -la`). 0
    `ideation_proposal_recorded` events in events.jsonl. Backfill
    CLI shipped 67h ago, unrun. TB-197's web card now makes the
    accumulation state visible at a glance, but doesn't change the
    underlying volume.
    (2) **Track-record feedback into ideation prompt header**
    (TB-163-pattern, carries) — wait-condition unchanged: backfill
    landing + 2-3 cycles of organic record growth. Half-met (CLI
    available; not run; no organic records yet).
    (3) **Insight aggregator from records → `ideation_quality.md`**
    (TB-175-shape) — operator-acked deferral 2026-05-07T01:57:58Z.
    Volume precondition (records on disk + verdicts) unsatisfied.
  - Status: `in-progress`
  - Reasoning: foundation shipped; remaining gaps all
    accumulation-blocked or operator-deferred. The 67h since TB-196
    ladded include a deliberate operator engagement (TB-197) that
    invests in the observability complement rather than the data
    side — confirming the volume gap is the real bottleneck, not a
    surfacing one.

## Non-goal risk check

None. TB-197 (operator-authored web card) is observability of an
existing ap2-meta state, not generic-task-scheduler /
replace-operator-judgment / multi-tenancy / real-time /
cross-project drift. Empty pipeline (0A/0R/0B/0P) — no in-flight
risk either.

## Considered & deferred this cycle

- **Mirror TB-197's gate-state card with an "ideation proposals
  recorded (last 7d)" counter card on web `/`** — natural shape
  parallel to TB-197 and to TB-181's `/usage` dashboard. Deferred:
  pre-volume the surface would always read "0", and stamping more
  observability cards in front of an empty data set is exactly the
  "polish of meta-system surfaces unrelated to the project's
  outcome" failure mode goal.md L72-76 names. Re-evaluate after
  ≥10 records exist.
- **Inject "Recent ideation proposals (last N)" block into ideation
  prompt header** (carries) — TB-163-pattern mirrored for the
  TB-188 records stream. Closes gap (2). Wait-condition: backfill
  run + ≥2 organic cycles producing records. Half-met.
- **`ap2 proposals [--unclassified]` operator CLI** (carries) —
  sibling of pending-review surface (TB-151-shape). Impact gated on
  records existing on disk (0). Defer until ≥10 records exist.
- **Surface "unclassified proposals" count + TB-Ns in `ap2 status`
  + cron status-report** (carries) — TB-151-pattern. Gated on
  records on disk; pre-backfill the surface would always be empty.
- **Auto-run `ap2 backfill-proposals` on daemon startup** (carries)
  — steps on operator-owned migration timing (operator may want
  to review `--dry-run` first; 67h elapsed without running it
  reinforces the "operator's call" framing).
- **Ideation self-evaluates delete-test pre-queue** (carries) —
  semantic check beyond TB-164's structural Why-now marker. Risk:
  agent self-grading unreliable until operator classify-verdict
  ground-truth exists. Defer until ≥10 operator verdicts exist.
- **`ap2 classify --next` interactive bulk walk-through** (carries)
  — parallel-surface-adjacent (`ap2 classify TB-N` already works
  per-item); defer until operator surfaces bulk-classify pain.
- **TB-175 re-prop** (carries) — operator-acked deferral
  authoritative; volume precondition unsatisfied.
- **`ap2 ideate --hint`** (TB-184), **`ap2 frozen`** (TB-185),
  **briefing-bullet linter** (TB-172): authoritative rejects;
  will not re-propose.

Rejection-pattern note (n=4, unchanged): rejections cluster on
"creates parallel surface OR doesn't generalize OR off-focus OR
wack-a-mole." All deferred candidates above filtered against this.
TB-197's operator-authored web card is itself a parallel-surface
shape — but parallel surfaces are operator-authored-OK; ideation's
filter is on what *ideation* proposes, not on what kinds of work
land overall.

## Cycle observations

- TB-197's content choice (cadence visibility rather than data
  collection or goal.md edit) is the only fresh signal in 67h. It
  is consistent with the deliberate-pause posture — operator
  engaged on the visibility axis, not the action axis. Carry once
  to inform whether next cycle's "decisions needed" framing should
  shift from action-options to a pure observability ask.

## Decisions needed from operator

(none this cycle.)

Last cycle's `(a) run backfill / (b) edit goal.md / (c) ack pause`
multi-option ask was implicitly resolved by the operator's
2026-05-10T00:24Z TB-197 add — an orthogonal observability
investment that signals "not blocked, just choosing other work."
Re-articulating the same options after a clear non-(a)/(b)/(c)
engagement would be ignoring the signal. Drop. If volume stays at
zero across 2-3 more cycles, re-surface as a narrower
"backfill-or-not" ask.

## Proposals this cycle

0 proposals.

13th consecutive 0-proposal cycle. Slot count rose to 5 this cycle
(0-backlog under threshold) but available-aligned-work is still 0:
all candidates above are accumulation-blocked (volume precondition
unsatisfied: 0 records on disk, 0 verdicts), operator-deferred
(TB-175), or rejection-pattern adjacent (parallel-surface /
wack-a-mole / off-focus). Goal.md L50-55 explicitly names this
phase: "the bottleneck is signal volume, not prompt-language
craft." Slot-fill against an empty data set would be exactly the
"goal-shaped pro-forma compliance" failure mode the focus exists
to detect (L66-76). Quality > slot-fill.
