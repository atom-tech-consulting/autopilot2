# Ideation State

_Last updated: 2026-05-20T03:51Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 2B / 0P / 141C / 3F — last cycle's two
proposals (TB-269 timeout calibration, TB-270 payload slimming) sit
in Backlog pending review (queued ~2h ago), and the operator landed
TB-271 (hot-reload tunable env knobs) in the same window. The
TB-271 implementation explicitly includes `AP2_VALIDATOR_JUDGE_
TIMEOUT_S` in `HOT_RELOADABLE_KNOBS` (ap2/env_reload.py:79), so
TB-269's bump is now retunable without daemon restart once approved.
The mission focus (end-to-end automation) remains healthy — TB-271
addresses the operator-restart bottleneck (every tunable change
required `ap2 stop && ap2 start` previously, scaling poorly across
walk-away). Backlog refill is the right move; slot count = 3.

Recent Completes considered (last ~24h):

- TB-271 (`59148ca`, 2026-05-20T02:12Z) — hot-reload tunable env at
  each tick; AP2_VALIDATOR_JUDGE_TIMEOUT_S included in
  HOT_RELOADABLE_KNOBS verified via `grep`.
- TB-268 (`bdf1262`, 2026-05-20T01:40Z) — split test_tools.py
  (118KB → 37KB) into validator/board/queue sibling test modules.
- TB-267 (`9d2e1f8`, 2026-05-20T01:14Z) — split test_web.py
  (131KB → 18KB) into 7 web-prefixed sibling modules.
- TB-266 (`ce24c21`, 2026-05-20T00:51Z) — split test_cli.py
  (133KB → 4KB) into four cli-prefixed sibling modules.
- TB-265 (`84db3ad`, 2026-05-19T23:08Z) — env-stale WARN render on
  web home closing TB-260 surface gap.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223/224/232/234/241/243/245/247/
      250/256/258/269/270 (auto-approve mode + dep-coherence judge +
      surfaces; 269/270 in flight to drive fail-open rate down).
    - Axis 2 (failure-recovery): TB-225/229/233/239/236/252.
    - Axis 3 (cost/blast-radius): TB-224/227/228/234.
    - Axis 4 (multi-focus): TB-226/237/242/244/246.
    - Code-quality / agent-friendliness: TB-253/254/261/262/263/264/
      265/266/267/268/271 (test-shield, JSON util, five module
      splits, three test-file splits, hot-reload tunables).
    - Cross-axis observability: TB-248/255/257/258/259/260.
  - Gaps:
    (1) **`validator_judge_noisy` state is purely cosmetic, doesn't
        gate auto-approve** — `automation_status.validator_judge_
        noisy_threshold` (default 5) drives a `[noisy]` suffix on
        `ap2 status` text + warn-tint on the web automation card
        (TB-243), but `pause_reason` (automation_status.py:325) has
        NO `validator_judge_noisy` discriminator and `auto_approve.
        py` has zero references to `validator_judge_*` events
        (verified via `grep`). Current `ap2 status` line
        "auto-approve: disabled (validator-judge 24h: 0 fail, 11
        timeout [noisy])" — an operator flipping `AP2_AUTO_
        APPROVE=1` today gets 11/11 fail-open against the load-
        bearing axis-1 dep-coherence gate that goal.md L82-85
        commits as the safety floor. TB-269/270 attack the
        timeout root cause but the noisy-pause closes the
        belt-and-suspenders safety floor independently — even if
        calibration regresses or a future SDK update reopens the
        timeout window, the operator never gets a silent axis-1
        bypass. Mirrors the existing `consecutive_freezes` pause
        shape exactly (zero new operator-facing surfaces; reuses
        the `ap2 ack auto_approve_unfreeze` verb).
    (2) **TB-269 timeout calibration awaiting operator review** —
        the calibration follow-up the TB-257 artifact named as
        deferred is in Backlog pending review. No further proposal
        until operator approves or rejects.
    (3) **TB-255 `grep -cE` shell-bullet auto-unfreeze coverage** —
        still deferred. No n=2 recurrence. Operator rejection
        patterns (TB-172, TB-240) name the whack-a-mole risk
        explicitly.
    (4) **Dry-run interesting-types coverage** — same defer
        rationale as last cycle.
  - Status: `in-progress`
  - Reasoning: One concrete fillable gap (1) grounded in `ap2
    status` data and code grep; gap (2) intentionally not
    re-proposed (waiting on operator), gaps (3)/(4) explicitly
    deferred. Quality over quantity is the right call — last cycle
    proposed 2 (in flight); this cycle proposes 1 (axis-1+3
    cross-cut safety-floor closure).

## Non-goal risk check

None. Gap (1) lands inside axis 1's dep-coherence safety floor and
axis 3's cost/blast-radius guard simultaneously; doesn't drift
toward generic task scheduling, goal auto-rotation, or
unconditional automation. Reuses existing pause-discriminator
vocabulary so no new operator surface.

## Considered & deferred this cycle

- **Per-task validator-judge linkage on `ap2 status` (which
  queue-append got fail-open'd?)** — count surface (TB-243) is
  already actionable; per-task linkage adds complexity without
  clear operator demand.
- **Adaptive validator-judge timeout (auto-tune from observed
  P95)** — premature; TB-269 static bump is the simpler first
  move, adaptive only pays after a baseline calibration lands.
- **`ap2 doctor` warn when `AP2_AUTO_APPROVE=1` AND validator-judge
  noisy** — sibling-shape mirror of TB-234/TB-239 doctor warns.
  Worthwhile but conditional on TB-272 (gap (1)) landing first;
  doctor warn becomes a redundant pre-flight signal if the pause
  itself catches the hazard at runtime. Defer to a follow-up
  cycle if TB-272 lands and the pause-only catch leaves a
  pre-flight surface gap.
- **Investigate `test-suite-slowness-2026-05-17.md` insight stale
  tldr (`(no tldr — needs update)` on _index.md)** — code-quality
  housekeeping; operator's TB-260→TB-268 streak already drove the
  test-suite work directly. Defer; would compete with operator picks.
- **Split `ap2/operator_queue.py` (85KB) or `ap2/status_report.py`
  (67KB) following TB-262 pattern** — operator approved
  operator_queue.py at 85KB without flagging. Defer until
  observed agent-friendliness pain in those modules.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  ack on 2026-05-07T01:57Z said defer until ≥3 ideation cycles
  after TB-188/TB-189 land. Conditions still met but no positive
  signal that operator wants it now.

## Cycle observations

- TB-271 (hot-reload) landed inside the gap between last cycle's
  ideation and this one — operator-curated, addresses the
  walk-away "tunable knob change requires restart" friction that
  every TB-260-shape stale-env warning surfaced but didn't
  close. Strong signal that hot-reload is the right shape: the
  knob-change loop now completes inside one tick. `AP2_VALIDATOR_
  JUDGE_TIMEOUT_S` is in the hot-reloadable set, so TB-269's bump
  applies on next reload.
- `auto-approve: disabled (...11 timeout [noisy])` in current `ap2
  status` confirms TB-243's noisy surface IS firing per the
  threshold — what's missing is the pause-side wiring. The
  observability + the gating are correctly decoupled; only the
  gating is absent.
- TB-269/TB-270 still pending review at 2h-old mark. Operator
  approval cadence varies (TB-256 approved within ~25min, TB-257
  within ~10h). Don't re-propose; trust the gap.

## Decisions needed from operator

(none — gap (1) is mechanical safety-floor wiring grounded in
`ap2 status` + code-grep evidence, not operator-judgment
escalation; gaps (2-4) deferred with explicit conditions or
in-flight)

## Proposals this cycle

- TB-272 — Add `validator_judge_noisy` discriminator to the
  auto-approve `pause_reason` chain (axis-1+3 cross-cut
  safety-floor closure for the load-bearing dep-coherence judge
  fail-open hazard).
