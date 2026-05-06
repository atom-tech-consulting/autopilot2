# Ideation State

_Last updated: 2026-05-06T01:46:42Z by ideation cron_

## Mission alignment

Recent 5 completes continue serving the walk-away promise via
operator-readable observability + janitor recovery:

- TB-180 (94a7240) — `ap2 logs` CLI gets the same compact `usage` row
  rendering as the web `/events` table (TB-179 parity).
- TB-179 (910ee0a) — compact `usage`-blob renderer for token/cost
  events in the web events table; identity-prefix per event-type.
- TB-178 (0c2bba7) — janitor LLM judge classifies each finding as
  `real_strand` / `operator_draft` / `ambiguous` with per-finding
  reasoning; CLI/status-report split surfacing.
- TB-177 (6c59ee6) — janitor cron job detects stranded git state in
  ap2 target projects and surfaces for review.
- TB-176 (9df5a15) — `ideate [force]` chat-verb parity with
  `ap2 ideate [--force]` in the MM handler.

TB-181 (/usage dashboard) just landed an implementation in 67871f9
but failed verification on bullet 4 (per-task observability — covered
in Gaps below) and was rolled back to Backlog with retry budget 1/3.

## Current focus assessment

goal.md "Current focus: ideation quality" remains the sole declared focus.

- **Ideation quality (gap-covering without drift; push for progress
  without scope creep)**
  - Progress so far: structural guards still cover every gap last
    week's assessments named (TB-121 review gate, TB-138 prompt rule,
    TB-152 reject reasons, TB-154 canonical structure validator,
    TB-161 goal-cite, TB-163 rejection-block in prompt header, TB-164
    Why-now check, TB-171 Manual-bullet rejection, TB-173 open-questions
    surfacing). TB-170 is the operator escape hatch.
  - Gaps:
    (1) `parse_focus_statuses` + auto-skip wiring is **proposed** as
    TB-174 and awaiting `ap2 approve` since 2026-05-05T01:09Z
    (~24.6h pending). Still the right answer to the "every cycle this
    focus stays in-progress, ideation keeps firing on cooldown"
    pattern; not yet observable as a no-op streak this cycle (operator
    has been adding tasks, ideation has had work to assess).
    (2) Shell-bullet pitfall enumeration: rejected by operator on
    2026-05-05T00:45Z (TB-172) — accepted residual risk; not re-proposed.
    (3) Ideation acceptance-rate insight is **proposed** as TB-175
    and awaiting `ap2 approve` (~24.6h pending). Until it lands, no
    quantitative signal on whether the structural-gate cascade moved
    acceptance rate vs the pre-gate baseline.
    (4) **New observation (TB-181 retry 1/3, 67871f9):** prose
    bullet 4's "fixture containing **10** task_run_usage events,
    **5** control_run_usage, **30** judge_call" specified exact event
    counts the test fixture (`_tb181_seed_seven_day_mix`) didn't
    match (it produced 7 / 7 / 21 — daily multiples × 7 days). Judge
    correctly failed the count clause even though the implementation
    is complete. Same failure-shape as TB-122's manual bullet (overly
    specific anchor that the agent doesn't satisfy literally) but in
    a new flavor: **specific cardinality** instead of **manual step**.
    Not yet a pattern (n=1) — flagged for monitoring; do not propose
    a structural validator rule on n=1 (operator vetoed enumerate-known
    -cases linters per TB-172).
  - Status: `in-progress`
  - Reasoning: gaps #1 and #3 are awaiting operator review; gap #4
    is in retry budget and will resolve naturally (agent rewrites
    fixture counts on retry, or hits the bullet again and budget
    decides). Actionable next step is operator review of pending
    proposals, not a fresh proposal from ideation.

## Non-goal risk check

None. TB-181's `/usage` dashboard is squarely in goal.md's
walk-away-promise path (operators returning after a week need cost
visibility); not drift. No in-flight work strays into Non-goals.

## Considered & deferred this cycle

- **Meta fix-task to rewrite TB-181 bullet 4** (`#fix-briefing` shape
  from the failure-review playbook): defer. Retry budget is 1/3;
  agent likely rewrites the fixture counts to 10/5/30 on attempt 2
  (cheaper than ideation intervening). Re-evaluate if TB-181 hits
  retry-exhausted on the same bullet.
- **Re-proposing anything covered by TB-174/TB-175**: both still in
  Backlog blocked on review (~24.6h). A third proposal addressing
  the same gaps would be drift and would compete for operator attention.
- **Briefing-validator rule "no exact cardinality numerals in prose
  Verification bullets"**: tempting from TB-181 fail-shape, but n=1
  and operator's TB-172 reject pattern is "validators that
  enumerate-known-cases generalize poorly." Wait for a second
  occurrence before considering structural intervention.
- **Shell-bullet pitfall validator (any flavor)**: TB-172 reject
  remains authoritative.
- **Greenfield follow-ups on TB-177/178/179/180**: each shipped
  focused improvements; no edge case or natural extension surfaces
  yet that isn't already covered by pending proposals.
- **Auto-rotate goal.md `## Current focus` when exhausted**: violates
  Non-goal "Replacing operator judgment on goal definition."
- **Cross-cycle deferral aging tracker**: carried; still no signal
  long-stale deferrals are a problem in themselves. Defer.
- **Force-propose a third item just to fill Backlog**: Backlog is
  already at 3 (TB-174, TB-175, TB-181). The "Backlog<3 is a ceiling,
  not a mandate" rule applies cleanly.
- **Surface daemon_pause/_resume audit, focus-statuses in `ap2 status`,
  ideation no-op self-throttle**: all carried; no fresh signal.

## Open questions for operator

- **Tasks awaiting review (`ap2 approve` / `ap2 reject`)**: TB-174,
  TB-175. Both gated `@blocked:review` per TB-121, pending since
  2026-05-05T01:09Z (~24.6h). TB-174 still the lever for cleanly
  surfacing `exhausted-needs-operator`; TB-175 bootstraps the first
  insight + gives quantitative signal on whether the structural-gate
  cascade moved acceptance rate.
- **Focus-rotation candidate** (carried): after TB-174/TB-175 land +
  approve, "Current focus: ideation quality" is plausibly
  `exhausted-needs-operator`. Operator may want to refresh goal.md
  `## Current focus` (e.g. "verifier robustness", "operator-walk-away
  resilience", or a target-project focus) so future ideation has a
  fresh anchor and TB-174's auto-skip gate unlatches.
- **TB-181 retry watch (n=1 prose-bullet over-specification)**: the
  failure shape (exact event counts in a fixture-shape prose bullet)
  is a fresh data point. If TB-181 retry-exhausts on the same bullet,
  the right move is `#fix-briefing` to rewrite bullet 4 as either
  shell anchors (`grep -c task_run_usage tests/...`) or a coarser
  prose claim ("fixture mixes ≥3 statuses, ≥2 labels, ≥3 verdicts").
  Do not pre-empt — wait for the retry signal.
- **Shell-bullet residual-risk acceptance** (carried): TB-172 reject
  implies the shell-bullet pitfall class stays a verifier-side gate
  (no queue-append linting). Confirm that's the durable decision.
- No unadopted `cron_proposed` events.
- `.cc-autopilot/insights/_index.md` still empty; TB-175 bootstraps
  the first insight on approval.

## Proposals this cycle

Backlog already populated (3 items: TB-174, TB-175 awaiting review,
TB-181 in retry budget 1/3). No new proposals — adding a fourth
would compete with the two queued for operator attention without
addressing any gap not already covered, and would violate goal.md's
"push for progress without scope creep" delete-test.
