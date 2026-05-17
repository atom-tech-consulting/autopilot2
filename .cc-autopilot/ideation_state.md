# Ideation State

_Last updated: 2026-05-17T14:57:51Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 2B / 0P / 116C / 8F. Auto-approve flipped
to **disabled** (validator-judge 24h: 6 fail, 1 timeout — noisy). Five
tasks (TB-245, TB-246, TB-247, TB-249, TB-250) cascaded into Frozen
in the last 4h, ALL via the SAME root cause: per-bullet shell command
`uv run pytest -q ap2/tests/` timing out at exactly 600.01s (the
`AP2_VERIFY_TIMEOUT_S` default), while the agent's own
post-commit re-run measures the suite at **1320-1349s** (TB-245 +
TB-250 summaries name the exact figures: 1734 tests / 1320s / 1349s).
The work for all five is on HEAD (commits 125d64a, fe1dfa6, 64e760b,
11898cf, dd623ae) — only the verifier's 600s budget vs 22-min suite
gap blocks completion bookkeeping. Loop is currently dead-in-the-water
for any task carrying the project-wide regression-pin bullet.

Recent Completes considered:

- TB-244 (`aa971f8`, 2026-05-17T00:09Z) — status-report cron axis-4
  digest extension.
- TB-243 (`647b771`, 2026-05-16T23:59Z) — `ap2 status` validator-judge
  fail-open 24h counts. Now generating the **noisy** count visible in
  the status header (6 fail / 1 timeout / 24h).
- TB-242 (`6704ed52`, 2026-05-16T21:59Z) — axis-4 focus-pointer state.
- TB-241 (`fc14fe3`, 2026-05-16T21:50Z) — dry-run readiness in status/web.
- TB-238 (`d861d83`) — status-report dry-run readiness extension.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (manual-approval): TB-223 + TB-224 + TB-232 + TB-234 +
      TB-241 + TB-243. TB-247-frozen would close the validator-judge
      observability gap.
    - Axis 2 (failure-recovery): TB-225 + TB-229 + TB-233 + TB-239 +
      TB-236.
    - Axis 3 (cost/blast-radius): TB-224 + TB-227 + TB-228 + TB-234.
    - Axis 4 (multi-focus): TB-226 + TB-237 + TB-242 + TB-244 +
      TB-246-frozen.
    - Cross-axis e2e: TB-230 + TB-237 + TB-238.
  - Gaps (URGENT — this cycle is dominated by one fresh operator-blocking
    regression):
    (1) **`AP2_VERIFY_TIMEOUT_S` (default 600s, `ap2/config.py:39`) <
        observed full-suite runtime (~1349s)** — 5 tasks froze in <4h
        with exit_code=None / duration_s=600.01 on the same shell
        bullet `uv run pytest -q ap2/tests/` (TB-245/246/247/249/250
        events 12:57:39Z / 13:31:39Z / 13:47:24Z / 12:06:34Z /
        14:22:42Z respectively). Test suite has grown to 1734 tests
        / ~22min; default has not moved. Direct goal.md L88-100
        impact: the loop's per-cycle failure-recovery automation
        (axis 2) doesn't even GET to run — every regression-pin
        bullet returns "timeout" indistinguishable from a real test
        failure, so neither the agent's BriefingFix path nor the
        operator's "trivial unfreeze" path applies. Walk-away promise
        is fiction the moment a single task's full-suite bullet hits
        the timeout. Critical blocker: NO new ideation-proposed task
        with that regression-pin bullet can ship until this is fixed.
    (2) **No doctor/preventive surface for the timeout regression** —
        TB-234 + TB-239 cover `AP2_AUTO_APPROVE` + `AP2_AUTO_UNFREEZE`
        misconfiguration but NOT verifier-timeout-vs-suite-runtime.
        Had a doctor WARN existed measuring recent successful
        `verify_run` durations against the configured timeout, this
        regression would have surfaced weeks before the cascade.
        Exact TB-234/TB-239 shape: "knob is set too tight for current
        workload."
    (3) **Validator-judge "noisy" + auto-approve auto-disabled** —
        TB-247's strict-JSON + raw-dump fix is IN HEAD (commit
        64e760b) but bookkeeping-frozen so the loop can't credit it
        to the noisy counter; once unfrozen, the next 24h should
        show the 6/1 numbers stabilizing.
    (4) **Dry-run interesting-types coverage** — defer rationale
        unchanged.
    (5) **Auto-unfreeze fix-shape coverage view** — frozen pile just
        jumped 3→8; the timeout-shape isn't a TB-225 candidate (no
        briefing patch fixes it). Revisit after timeout regression
        closes and the frozen pile reverts to ~3.
  - Status: `in-progress`
  - Reasoning: the loop is operator-blocked on a single env-knob
    regression; the right move is to surface that to the operator AND
    propose one preventive task that would have caught it. Greenfield
    parity proposals against a dead loop don't pay rent.

## Non-goal risk check

None. The proposed doctor warning is preventive observability on a
single existing env knob — same shape as TB-234 / TB-239, no auto-
mutation, no goal.md change, no new automation surface.

## Considered & deferred this cycle

- **Bump `DEFAULT_VERIFY_TIMEOUT_S` from 600 → 1800 in `ap2/config.py`** —
  the surgical 1-line fix. Deferred from ideation: the operator can
  set `AP2_VERIFY_TIMEOUT_S=1800` env-side without a code change
  (operator-blessed env-knob path); raising the project-wide default
  is a goal-direction call, not an ideation call.
- **Auto-unfreeze the 5 timeout-shape Frozen tasks** — not a TB-225
  candidate; no briefing patch fixes a verifier timeout. Operator
  unfreeze is the right path.
- **Investigate WHY validator-judge keeps returning non-dict** —
  rejected last cycle, same reason: wait for TB-247's raw dump to
  unfreeze and accumulate diagnostic data first.
- **TB-175 / TB-185 / TB-184 / TB-240 / TB-172-shape ideation
  proposals** — all rejected n=2-6+ times; no new evidence.
- **Doctor warning when frozen-task count exceeds threshold (e.g. >5
  in 24h)** — adjacent to gap (1) but reactive not preventive;
  catches the cascade after the fact rather than the misconfiguration
  before. Defer until preventive surface (this cycle's proposal)
  ships and we measure whether reactive is still needed.

## Cycle observations

- The "freshness of seed" carry-forward from last cycle holds again:
  this cycle proposes ONE task anchored to **post-shipping wild-
  failure data** (n=5 retry_exhausted in 4h, same root-cause shell
  bullet, same duration_s=600.01 fingerprint). Defending the carry:
  the n=5 failures are direct goal.md L88-100 axis-2 evidence,
  literally "every retry-exhausted task requires operator unfreeze"
  multiplied by 5 in one afternoon — exactly the failure mode the
  axis exists to close.
- Operator-action gap: last drain was 2026-05-17T06:45Z, ~8h ago
  (TB-251 add_backlog). The 5 retry_exhausted events accumulated
  after, with no operator intervention since. Surfacing the
  unfreeze ask + env-knob bump as an explicit operator decision
  this cycle is essential — the queue isn't going to drain on its
  own.

## Decisions needed from operator

- Decision needed: Bump `AP2_VERIFY_TIMEOUT_S` from 600s to >=1800s
  (operator action: `export AP2_VERIFY_TIMEOUT_S=1800` in the
  daemon's env + restart, OR raise `DEFAULT_VERIFY_TIMEOUT_S` in
  `ap2/config.py:39`), then `ap2 unfreeze TB-245 TB-246 TB-247 TB-249
  TB-250` — work for all 5 is in HEAD per agent summaries (commits
  125d64a / fe1dfa6 / 64e760b / 11898cf / dd623ae); only the 600s
  verifier budget vs 1349s suite runtime gap blocks completion. Without
  this the loop is dead: every regression-pin task hits the same
  timeout and auto-approve stays disabled. Unblock condition: next
  ideation cycle sees the frozen pile revert to ~3 (TB-119/120/133)
  and auto-approve eligible to re-enable once validator-judge counts
  stabilize.

## Proposals this cycle

- TB-252 — Doctor warning when `AP2_VERIFY_TIMEOUT_S` is configured
  below the observed-typical successful `verify_run` duration
  (rolling window over recent events). TB-234/TB-239-shape preventive
  surface that would have caught this cascade weeks earlier.
