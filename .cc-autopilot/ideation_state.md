# Ideation State

_Last updated: 2026-05-12T09:36:25Z by ideation cron_

## Mission alignment

41st consecutive 0-proposal cycle. ~2h since prior assessment
(07:35Z); intervening window saw operator-authored TB-201 + TB-202
land Complete (07:49Z, 08:02Z) and one status-report cron post at
08:47Z. ~127h since TB-195 (`ap2 backfill-proposals` CLI) shipped
without an operator run; carried backfill Decision now in 25th
cycle in promoted shape. `.cc-autopilot/ideation_proposals/` still
`.gitkeep`-only (0 records); insights index still empty. Slot count
= 5 (0-backlog under threshold); available-aligned work = 0.

Latest 5 completes considered:
- TB-202 (`b09e3bc`, 2026-05-12T08:02Z) — refuse `ap2
  backfill-proposals` + `ap2 cron edit` when a task is Active
  (operator-authored)
- TB-201 (`03c4fc1`, 2026-05-12T07:49Z) — queue-route `ap2 ack` +
  `operator_log_append` MCP tool (operator-authored)
- TB-200 (`7d7c142`, 2026-05-12T00:39Z) — `## Authoring goal.md`
  in `ap2/howto.md` (operator-authored)
- TB-199 (`e24f294`, 2026-05-12T00:23Z) — `## Done when` in
  `GOAL_TEMPLATE` (operator-authored)
- TB-198 (`0040f6b`, 2026-05-11T23:44Z) — fence
  `.cc-autopilot/tasks/` + `insights/_index.md` (operator-authored)

## Current focus assessment

- **Ideation quality signal collection (goal.md L38-76)**
  - Progress so far: foundation 4-deep — TB-188, TB-189, TB-195,
    TB-196 — plus cadence-observability complement TB-197. Newly
    landed TB-201 + TB-202 are operator-surface hardening (queue-
    routing `ap2 ack` to close a false-positive state-violation
    class; refuse-if-Active gate on `ap2 backfill-proposals` itself
    + on `ap2 cron edit`). Both reinforce the operator-in-the-loop
    constraint named in goal.md L115-117; neither itself writes a
    proposal record to disk.
  - Gaps:
    (1) **Volume**: 0 records on disk, 0
    `ideation_proposal_recorded` events, 0 delete-test verdicts.
    `ap2 backfill-proposals` ~127h unrun. Operator-decision-shaped
    (CLI exists, only operator runs it). TB-202 just
    de-risked the run (refuses if Active is non-empty + names the
    blocking TB-N in stderr) — the safety story now matches the
    operator-in-the-loop posture, but the decision itself is
    unchanged.
    (2) **Track-record feedback into ideation prompt header**
    (TB-163-pattern, carries) — gated on Gap (1).
    (3) **Insight aggregator records → `ideation_quality.md`**
    (TB-175-shape, operator-deferred 2026-05-07T01:57Z, carries)
    — gated on Gap (1).
  - Status: `in-progress`
  - Reasoning: foundation shipped; remaining work blocked on
    volume gap; recent operator burst (TB-198→TB-202, five tasks
    in ~9h) confirms engagement bandwidth and routing toward
    surface-hardening over backfill execution. NOT
    `exhausted-needs-operator` — the present deadlock is
    "volume-blocked", not "exhausted"; flipping it would
    indefinitely skip ideation (TB-174 gate) when the right move is
    to keep surfacing the narrow operator ask.

## Non-goal risk check

None. Empty pipeline (0A/0R/0B/0P); no in-flight risk. TB-201 +
TB-202 both reinforce goal.md L115-117 (operator-in-the-loop where
work is irreversible) — anti-drift, not drift. No movement into
generic-task-scheduler / replace-operator-judgment / multi-tenancy
/ real-time / cross-project axes.

## Considered & deferred this cycle

- **Mirror TB-197 with "ideation proposals recorded (last 7d)" web
  card** (carries) — pre-volume the surface always reads "0".
  Re-evaluate after ≥10 records exist.
- **Inject "Recent ideation proposals (last N)" block into ideation
  prompt header** (carries) — TB-163-pattern mirrored for TB-188
  records. Gated on Gap (1).
- **`ap2 proposals [--unclassified]` operator CLI** (carries) —
  gated on records existing.
- **Surface "unclassified proposals" count in `ap2 status` + cron
  status-report** (carries) — TB-151-pattern; gated on Gap (1).
- **Auto-run `ap2 backfill-proposals` on daemon startup** (carries)
  — pre-empts the operator's carried verdict; do not propose.
  TB-202's refuse-if-Active gate makes a daemon-side auto-run
  technically safer but the verdict is still operator-owned.
- **Ideation self-evaluates delete-test pre-queue** (carries) —
  defer until ≥10 operator verdicts exist for ground-truth.
- **`ap2 classify --next` interactive bulk walk-through** (carries)
  — defer until operator surfaces bulk-classify pain.
- **TB-175 re-prop** (carries) — operator-acked deferral
  authoritative.
- **TB-184 / TB-185 / TB-172**: authoritative rejects; will not
  re-propose.
- **`blocked-on-operator-decision` focus-status flag (TB-174-shape
  extension)** (carries) — parallel surface; cheaper path remains
  operator running backfill or `ap2 reject TB-195`.
- **Verifier escaping fix for embedded backticks in shell bullets**
  (carries) — TB-172-rejection-shape; orthogonal to signal focus.
- **Pre-flight-gate parity sweep across other operator CLIs**
  (NEW) — TB-202 added refuse-if-Active to `backfill-proposals` +
  `cron edit`; one could imagine extending to other ops. Deferred:
  no operator pain reported, scope is "wack-a-mole enumerate other
  cases" (TB-172 rejection-pattern shape); strictly off-focus.

Rejection-pattern note (n=4, unchanged): "creates parallel surface
OR doesn't generalize OR off-focus OR wack-a-mole." All deferred
candidates filtered against this; no candidate this cycle clears
the filter without a volume precondition first.

## Cycle observations

(Triage from prior cycle: prior had "(no carried bullets this
cycle)". No new observations this cycle that don't fit the
structured sections — TB-202's de-risking effect on the carried
backfill ask belongs in `## Decisions needed from operator` where
the ask is re-articulated, not here.)

- (no carried bullets this cycle)

## Decisions needed from operator

- Decision needed: run `ap2 backfill-proposals` to seed
  `.cc-autopilot/ideation_proposals/` from historical TB-Ns, OR
  `ap2 reject TB-195` / append an operator_log line stating "wait
  for organic flow only"? 25th cycle in promoted shape; CLI shipped
  ~127h ago, dry-run identified ~14 candidates; TB-202 (08:02Z)
  added a refuse-if-Active pre-flight gate that names the blocking
  TB-N in stderr, so the run is now safe to invoke even with the
  daemon ticking. Unblock condition: either outcome lets the next
  cycle re-evaluate the volume-blocked proposal family (TB-175
  aggregator, prompt-header track-record injection, web
  records-counter card, `ap2 proposals` CLI). Without a verdict,
  those four candidates stay carried indefinitely while the focus
  headline stays "signal collection" with 0 signals on disk.

## Proposals this cycle

0 proposals.

41st consecutive 0-proposal cycle. Slot count = 5;
available-aligned work = 0. Every carried candidate is
volume-blocked, operator-deferred, or rejection-pattern adjacent;
the one new candidate this cycle (pre-flight-gate parity sweep)
filters out on the wack-a-mole shape. Goal.md L50-55: "the
bottleneck is signal volume, not prompt-language craft." Slot-fill
against an empty data set is exactly the "goal-shaped pro-forma
compliance" failure mode L66-76 names. Quality > slot-fill;
carrying the narrow backfill Decision instead of inventing
parallel-surface work.
