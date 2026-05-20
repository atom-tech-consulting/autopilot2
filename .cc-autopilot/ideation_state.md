# Ideation State

_Last updated: 2026-05-20T08:08Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 1B / 0P / 144C / 3F — TB-272
(`validator_judge_noisy` discriminator in auto-approve `pause_reason`
chain) landed at 8c80438 (2026-05-20T06:29Z) closing the axis-1+3
cross-cut safety-floor gap that motivated last cycle's gap (2). TB-273
(preventive ideation-prompt shell-pitfall sync) entered Backlog at the
prior cycle (2026-05-20T06:08Z) and is awaiting operator review. The
four-axis end-to-end-automation focus is now at "pre-deployment
ready": every gate has landed (TB-223 → TB-272 series) but the
operator has not yet enabled `AP2_AUTO_APPROVE` / `AP2_AUTO_UNFREEZE`,
so there is no operational signal to surface new gaps from. Slot
count = 4.

Recent Completes considered (last ~4h):

- TB-272 (`8c80438`, 2026-05-20T06:29Z) — validator_judge_noisy added
  to auto-approve pause_reason chain; 1908 tests pass.
- TB-270 (`58a562e`, 2026-05-20T05:59Z) — validator-judge payload
  Goal+Scope slice; operator-unfreeze after the 3-retry briefing-shape
  storm (the `!`-miss that motivates TB-273).
- TB-269 (`e4f6f43`, 2026-05-20T04:40Z) — validator-judge timeout
  default 15→60s + `validator_judge_passed` event + doctor audit.
- TB-271 (`59148ca`, 2026-05-20T02:12Z) — hot-reload tunable env at
  each daemon tick.
- TB-268 (`bdf1262`, 2026-05-20T01:40Z) — test_tools.py split mirror.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval bottleneck): TB-223/224/232/234/241/243/
      245/247/250/256/258/269/270/272 — auto-approve gate + dep-
      coherence judge + status surfaces + fail-open safety floor with
      `validator_judge_noisy` now wired all the way through to
      `pause_reason` (TB-272).
    - Axis 2 (failure-recovery): TB-225/229/233/239/236/252 — auto-
      unfreeze gate + `BriefingFix:` prefix teaching + doctor audits.
    - Axis 3 (cost/blast-radius): TB-224/227/228/234 — per-task +
      per-window token caps + `task_error` halt + visibility.
    - Axis 4 (multi-focus): TB-226/237/242/244/246 — focus pointer +
      `roadmap_complete` halt + status / cron-digest surfaces +
      `_maybe_ideate` skip gate.
    - Code-quality / agent-friendliness adjacent: TB-253/254/261/262/
      263/264/265/266/267/268/271 (test-shield, JSON util, five
      source-module splits, three test-file splits, hot-reload
      tunables).
    - Cross-axis observability: TB-248/255/257/258/259/260.
  - Gaps:
    (1) **TB-273 (ideation-prompt shell-pitfall sync) awaiting
        operator review** — in Backlog since 2026-05-20T06:08Z (~2h).
        Operator-decided cadence; closes the briefing-authoring class
        that caused TB-270's retry storm. No further proposal until
        approved/rejected.
    (2) **No operational data yet on `AP2_AUTO_APPROVE` /
        `AP2_AUTO_UNFREEZE`** — every gate (TB-223 → TB-272) has
        landed but `ap2 status` confirms `auto-approve: disabled`
        (knob unset). Until the operator turns one on and the loop
        runs through real cases, ideation has no signal to follow up
        on. Load-bearing observation for this cycle's
        proposals-count.
    (3) **TB-269/270 post-deployment re-measurement** — time-locked
        (≥7d wall-clock for the validator-judge 60s + sliced-payload
        window to accumulate). Current 24h figure (`0 fail, 13
        timeout`) is dominated by pre-fix events. TB-257 artifact
        will receive its final calibration verdict when the post-fix
        window has real data.
    (4) **Doctor warn for `AP2_AUTO_APPROVE=1` AND validator-judge
        noisy** — TB-272 sub-condition unlocked (TB-272 now in HEAD
        ✓) but pre-flight surface gap still hypothetical (operator
        hasn't enabled `AP2_AUTO_APPROVE` yet, so no observed
        confusion). `ap2 status` already shows `[noisy]` inline on
        the validator-judge line, partially covering the surface.
        Defer until operator either enables-and-reports-surprise or
        explicitly requests the doctor mirror.
    (5) **AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD recalibration
        (default 5)** — premature. Pre-fix baseline ~12-15
        timeouts/24h; post-TB-269/270 baseline TBD. Recalibrate only
        after ≥7d post-fix data so the threshold lands on real
        signal, not pre-fix noise.
    (6) **TB-255 `grep -cE` shell-bullet auto-unfreeze coverage** —
        still deferred. No n=2 recurrence (the TB-270 absence-`!`
        miss is n=1, addressed preventively via TB-273). Operator
        rejection patterns (TB-172, TB-240) still name the
        whack-a-mole risk explicitly.
  - Status: `in-progress`
  - Reasoning: All four axes are at "pre-deployment ready"; the next
    gap-surfacing depends on operational signal that doesn't exist
    yet. Quality-over-quantity holds: last cycle proposed 1 (TB-273
    in flight), this cycle proposes 0 because every remaining
    candidate is explicitly deferred with a concrete unblock-
    condition. Resist the temptation to fill the 4-slot quota with
    hypothetical work.

## Non-goal risk check

None. No drift toward generic task scheduling, goal auto-rotation,
or unconditional automation; every deferred candidate preserves the
opt-in / operator-curated trust-upgrade pattern goal.md constrains.

## Considered & deferred this cycle

- **Doctor warn when `AP2_AUTO_APPROVE=1` AND validator-judge
  noisy** — TB-272 in-HEAD sub-condition unlocked but pre-flight
  surface gap still hypothetical (see gap 4). Re-rank if operator
  enables `AP2_AUTO_APPROVE` and reports surprise or explicitly
  requests the doctor mirror.
- **Cron-digest pause-reason surface for `validator_judge_noisy`** —
  the status-report digest already covers `auto_approve_paused` events
  via the TB-228/TB-238 wiring TB-272 emits into; no separate task
  needed (surface-parity gap closed mechanically).
- **`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` recalibration** — premature;
  wait for post-TB-269/270 baseline (≥7d).
- **TB-269/270 post-deployment re-measurement evaluation task** —
  time-locked; current 24h data is dominated by pre-fix events.
  Re-rank in ≥7d when the post-fix window has real data.
- **`negate_grep_for_absent_assert` fix-shape to auto_unfreeze
  allowlist** — reactive complement to TB-273; operator rejection
  patterns (TB-172, TB-240) and ideation's own deferral of TB-255
  name the whack-a-mole risk. Preventive sync (TB-273) is the
  durable form. Re-rank only on n=2 of the `!`-miss class AFTER
  TB-273 lands.
- **Per-task validator-judge linkage on `ap2 status`** — count
  surface is actionable; per-task linkage adds complexity without
  clear operator demand.
- **Adaptive validator-judge timeout (auto-tune from observed P95)** —
  premature; TB-269 static bump is the simpler first move, adaptive
  only pays after the new baseline is observed for ≥7d.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator-
  acked defer until ≥3 ideation cycles after TB-188/TB-189 land
  (conditions met; no positive signal that operator wants it now).
- **Investigate `test-suite-slowness-2026-05-17.md` stale tldr** —
  housekeeping; the operator's TB-260→TB-268 streak already drove
  the test-suite work directly. Defer.

## Cycle observations

- TB-272 landing between cycles closes the axis-1+3 cross-cut safety
  floor that motivated last cycle's gap (2) deferral. New shape of
  axis-1: every gate is wired, every surface is wired, and the next
  major increment depends on the operator enabling a knob and the
  loop running through real cases.
- "All four axes pre-deployment ready" is load-bearing for this
  cycle: it explains WHY proposals = 0 is correct even though
  Backlog = 1 (below threshold 3). The next gap-surfacing depends on
  operational signal, not on more pre-deployment wiring — manufacturing
  more wiring here is exactly the pro-forma compliance the
  Mission's `Done when` calls out.
- Operator rejection patterns recap (carried, re-justified): TB-172
  + TB-240 reject briefing-validator expansion and agent self-fix
  that whacks moles instead of addressing root cause. TB-185 +
  TB-184 reject ap2-meta-polish unconnected to current focus
  signal. TB-273's preventive doc-sync matches the principled form
  (pull already-decided content from howto into the prompt that
  authors), and the `negate_grep_for_absent_assert` candidate is
  explicitly deferred on n=1 to avoid the whack-a-mole anti-pattern.

## Decisions needed from operator

(none — gap (1) is `ap2 approve TB-273` at operator's discretion;
gaps (2-6) are all deferred with explicit time-locked or
operational-signal-dependent conditions, none requiring operator
narrative judgment to unblock this cycle)

## Proposals this cycle

No new proposals this cycle. Backlog carries TB-273 pending review
(last cycle's preventive sync); every other candidate above is
deferred with a concrete unblock-condition (operational signal /
≥7d wall-clock / n=2 recurrence / operator request). Proposing
hypothetical fillers to use the remaining 3 slots would replay the
TB-172 / TB-185 rejection pattern.
