# Ideation State

_Last updated: 2026-05-20T06:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 1B / 0P / 143C / 3F — TB-269 (timeout
15→60 + `validator_judge_passed` emit + doctor audit) and TB-270
(payload Goal+Scope slice) both landed since last cycle, closing
TB-257's `timeout-too-tight` (dominant) + `prompt-too-heavy`
(secondary) factors on the load-bearing dep-coherence safety floor.
TB-272 (validator_judge_noisy pause discriminator) remains in Backlog
pending review — wires the noisy state into the auto-approve gate so
flipping `AP2_AUTO_APPROVE=1` no longer silently fail-opens on the
axis-1 floor. Mission focus (end-to-end automation, axis 1+3 trust
upgrade) is healthy; one concrete new gap surfaced by TB-270's retry
storm. Slot count = 4.

Recent Completes considered (last ~24h):

- TB-270 (`58a562e`, 2026-05-20T05:59Z) — validator-judge payload slice
  to Goal+Scope sections; required operator-unfreeze after 3-retry
  storm caused by absent `!` prefix on bullet 5's absence-check.
- TB-269 (`e4f6f43`, 2026-05-20T04:40Z) — timeout 15→60s default +
  `validator_judge_passed` event + doctor `validator_judge_timeout_
  audit`.
- TB-271 (`59148ca`, 2026-05-20T02:12Z) — hot-reload tunable env at
  each tick; AP2_VALIDATOR_JUDGE_TIMEOUT_S in HOT_RELOADABLE_KNOBS so
  TB-269's bump applies on next tick without daemon restart.
- TB-268 (`bdf1262`, 2026-05-20T01:40Z) — split test_tools.py
  (118KB → 37KB) into validator/board/queue siblings.
- TB-267 (`9d2e1f8`, 2026-05-20T01:14Z) — split test_web.py
  (131KB → 18KB) into 7 web-prefixed sibling modules.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223/224/232/234/241/243/245/247/
      250/256/258/269/270/272 (auto-approve mode + dep-coherence
      judge + surfaces; TB-269/270 just landed driving fail-open
      rate down; TB-272 in Backlog wiring noisy-state into pause).
    - Axis 2 (failure-recovery): TB-225/229/233/239/236/252.
    - Axis 3 (cost/blast-radius): TB-224/227/228/234.
    - Axis 4 (multi-focus): TB-226/237/242/244/246.
    - Code-quality / agent-friendliness: TB-253/254/261/262/263/264/
      265/266/267/268/271 (test-shield, JSON util, five module
      splits, three test-file splits, hot-reload tunables).
    - Cross-axis observability: TB-248/255/257/258/259/260.
  - Gaps:
    (1) **Ideation prompt's `## Shell-bullet pitfalls to AVOID`
        section is stale vs. `ap2/howto.md`'s authoritative
        four-pitfall list** — `ap2/ideation.default.md` L471-486
        lists 3 pitfalls (bare `python`, bare-path-as-command,
        multi-line bullets); `ap2/howto.md` L462-505 lists 4 (the
        TB-207 literal-backtick, the TB-270 absence-check `!`
        prefix, the TB-204 directory-walking `-r`, the TB-219
        `Prose:` prefix) with a worked example. **TB-270 demonstrated
        the cost concretely on 2026-05-20T04:54-05:53Z**: ideation-
        authored briefing (TB-270, last cycle) included a bullet 5
        `grep "briefing_markdown\":[[:space:]]*briefing_text" ...`
        intended as an absence assertion; missing `!` made `grep`
        exit 1 when the assignment was correctly absent →
        verification_failed → 3 retries each agent-blocked → retry
        exhausted at 05:05:25Z → operator-manual unfreeze at
        05:53:04Z → agent self-corrected bullet 5 with `!` →
        completed at 05:59:51Z. Direct axis-1 cost: every retry
        storm of this shape is operator-toil that walk-away promise
        forfeits. The howto fix exists and is durable; the ideation
        prompt is what AUTHORS briefings, so the prompt's pitfall
        list is where the prevention has to live. One-file edit
        (4 bullets → align with howto worked example), zero new
        code, compounds against every future ideation-authored
        briefing.
    (2) **TB-272 noisy-pause discriminator awaiting operator review**
        — in Backlog since 2026-05-20T03:58Z (~2h). No further
        proposal until operator approves or rejects.
    (3) **TB-269/270 post-deployment re-measurement** — TB-257
        artifact has `## Calibration applied (TB-269)` + `## Re-
        measurement after TB-270` immediate appends but no 7-day
        post-deployment verdict band. `ap2 status` line "0 fail | 12
        timeout (24h)" pre-fix figure will drift down naturally as
        events expire; the structured trust-upgrade signal is the
        artifact's final calibration verdict. Defer until ≥7 days
        wall-clock elapse so the data is real.
    (4) **TB-255 `grep -cE` shell-bullet auto-unfreeze coverage** —
        still deferred. No n=2 recurrence after TB-270's `!` miss
        (n=1, addressed preventively via gap (1)). Operator
        rejection patterns (TB-172, TB-240) name the whack-a-mole
        risk explicitly.
    (5) **Dry-run interesting-types coverage** — same defer
        rationale as last cycle.
  - Status: `in-progress`
  - Reasoning: One concrete fillable gap (1) grounded in the latest
    24h's retry-storm timeline + direct file diff between ideation
    prompt and howto; gaps (2-5) deferred with explicit conditions
    (in-flight, time-locked, or whack-a-mole-bounded). Quality over
    quantity holds: last cycle proposed 1 (in flight), this cycle
    also proposes 1 (preventive axis-1 closure grounded in
    just-observed failure).

## Non-goal risk check

None. Gap (1) is a documentation-sync inside a daemon-owned prompt
template (`ap2/ideation.default.md`). Doesn't drift toward generic
task scheduling, goal auto-rotation, or unconditional automation.
Reuses the existing pitfall vocabulary from `ap2/howto.md`
verbatim so no new authoring convention.

## Considered & deferred this cycle

- **Add `negate_grep_for_absent_assert` fix-shape to auto_unfreeze
  allowlist** — reactive complement to gap (1). Operator rejection
  patterns (TB-172, TB-240) and ideation's own deferral of TB-255
  name the whack-a-mole risk: adding fix-shapes one-at-a-time on
  n=1 evidence is the rejected pattern. Preventive sync (gap 1) is
  the durable form. If gap (1) lands and a second instance of the
  `!`-miss appears anyway, re-rank then.
- **Doctor warn when `AP2_AUTO_APPROVE=1` AND validator-judge noisy**
  — conditional on TB-272 landing (still in Backlog). Continues to
  defer until TB-272 is in HEAD AND a pre-flight surface gap is
  observed.
- **Per-task validator-judge linkage on `ap2 status`** — same
  reasoning as last cycle (count surface is actionable; per-task
  linkage adds complexity without clear operator demand).
- **Adaptive validator-judge timeout (auto-tune from observed P95)**
  — premature; TB-269 static bump is the simpler first move,
  adaptive only pays after the new baseline is observed for ≥7d.
- **TB-269/270 post-deployment re-measurement evaluation task** —
  time-locked (gap 3); the current 24h data is dominated by pre-fix
  events. Re-rank in ≥7d when the post-fix window has real data.
- **Investigate `test-suite-slowness-2026-05-17.md` stale tldr** —
  code-quality housekeeping; operator's TB-260→TB-268 streak
  already drove the test-suite work directly. Defer.
- **TB-175-shape ideation-acceptance-rate aggregator** — operator
  ack 2026-05-07T01:57Z said defer until ≥3 ideation cycles after
  TB-188/TB-189 land. Conditions met but no positive signal that
  operator wants it now.

## Cycle observations

- TB-270 retry-storm timeline (2026-05-20T04:54→05:59Z) is a
  concrete case study for axis-1's failure mode: a 4-character
  briefing-authoring oversight cost the operator one unfreeze
  intervention. Each prevented retry-storm of this shape IS
  walk-away time clawed back; the goal-anchor for gap (1)
  proposal writes the math out.
- Operator rejection patterns recap (carried, re-justified):
  TB-172 + TB-240 both reject briefing-validator expansion that
  whacks moles instead of addressing root cause. Preventive
  sync (gap 1) avoids that pattern because it pulls already-
  decided content from howto into the ideation prompt rather
  than inventing a new validator gate.
- The ideation prompt vs. howto divergence has compounded
  silently — howto has been updated with each pitfall as it
  was observed (TB-204 dir-walk, TB-207 literal-backtick,
  TB-270 absence-`!`), but the ideation prompt's TB-76 section
  hasn't been touched since the original three. Pattern worth
  noting for any future doc-pair: when prompt and runtime howto
  diverge, the prompt is what authors and so should be the
  source of truth at write-time.

## Decisions needed from operator

(none — gap (1) is mechanical doc-sync grounded in just-observed
TB-270 retry-storm and an explicit file diff between
`ap2/ideation.default.md` L471-486 and `ap2/howto.md` L462-505;
gaps (2-5) deferred with explicit conditions or in-flight)

## Proposals this cycle

- TB-273 — Sync `ap2/ideation.default.md`'s `## Shell-bullet
  pitfalls to AVOID` section with `ap2/howto.md`'s authoritative
  four-pitfall list (literal-backtick, absence-`!`, directory-`-r`,
  `Prose:` prefix) to prevent ideation-authored briefings from
  reproducing the TB-270 retry-storm class.
