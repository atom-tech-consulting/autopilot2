# Ideation State

_Last updated: 2026-05-06T05:53:30Z by ideation cron_

## Mission alignment

Three completes since last cycle, all on the ideation-quality
prompt-shape track named in goal.md — no Mission drift:

- TB-183 (6583b07, follow-up to d69a34e) — pre-computed proposal
  slot count flows into the prompt's `## Current state` via
  state_extras; ideation.default.md drops the hardcoded "fewer
  than 3 workable" framing. Slot N now operator-tunable.
- TB-174 (a90b1c0) — `parse_focus_statuses` parser + auto-skip
  gate in `_maybe_ideate`: emits `ideation_skipped reason=
  focus_exhausted` + advances cooldown when every focus item is
  `exhausted-needs-operator`. The gate is now armed.
- TB-182 (0b8aee9) — cron status-report drops the redundant
  "Tasks awaiting review" forwarding + validates remaining
  forwarded references mid-cron.
- TB-181 (e979fa4) — `/usage` token-cost web dashboard.
- TB-180 (94a7240) — `ap2 logs` compact `usage` row parity.

## Current focus assessment

goal.md "Current focus: ideation quality" remains the sole
declared focus.

- **Ideation quality (gap-covering without drift; push for
  progress without scope creep)**
  - Progress so far: full structural-guard cascade now landed —
    TB-121 review gate, TB-138 prompt rule, TB-152 reject
    reasons, TB-154 canonical structure validator, TB-161
    goal-cite, TB-163 rejection-block in prompt header, TB-164
    Why-now check, TB-171 Manual-bullet rejection, TB-173
    open-questions surfacing, TB-182 forwarded-reference
    validation, TB-174 focus-exhausted auto-skip gate, TB-183
    slot-count plumbing. TB-170 is the operator escape hatch.
  - Gaps:
    (1) **Focus-exhausted auto-skip gate is now armed** (TB-174,
    a90b1c0). Once this assessment declares the focus
    `exhausted-needs-operator`, the next cron tick reads
    `parse_focus_statuses(.cc-autopilot/ideation_state.md)`,
    sees all-exhausted, emits `ideation_skipped reason=
    focus_exhausted`, and skips the SDK call. Working as
    designed; no further action.
    (2) Ideation acceptance-rate insight (TB-175): operator
    rejected 2026-05-06T05:15:59Z without a reason. Treat as
    authoritatively closed — do NOT re-propose.
    (3) Shell-bullet pitfall enumeration (TB-172): operator
    rejected 2026-05-05T00:45Z — accepted residual risk.
    (4) **Pattern observation (n=3 now): literal-string anchors
    in `## Verification` shell bullets force briefing-side
    rewrites instead of impl changes.** TB-178 (`!`-prefix exit
    inversion), TB-182 (literal-phrase quoting), TB-183 (`grep
    -nE "fewer than 3 wor…"` failed twice on d69a34e before
    6583b07's briefing fix). All three resolved within retry
    budget; none reached retry_exhausted. Operator's TB-172
    rejection authoritatively covers structural intervention
    here. Continue deferring.
  - Status: `exhausted-needs-operator`
  - Reasoning: every gap is either resolved by a landed task,
    operator-rejected, or accepted residual risk. The remaining
    structural-guard surface area is fully covered; further
    prompt-shape work would be polish that fails goal.md's
    delete-test. Operator should refresh `goal.md ## Current
    focus` so future ideation has a fresh anchor and TB-174's
    gate unlatches.

## Non-goal risk check

None. Nothing in flight or recently complete touches goal.md
Non-goals (generic task scheduler, multi-tenancy, real-time
collab, cross-project orchestration, replacing operator
judgment on goal definition). TB-183 + TB-174 stayed on the
declared ideation-quality focus path.

## Considered & deferred this cycle

- **Re-propose ideation acceptance-rate insight under a
  different framing**: TB-175 was rejected without reason
  yesterday. Re-proposing the same idea (even rephrased) is
  exactly the pattern goal.md and the operator log warn
  against. Skip.
- **`#fix-briefing` for TB-183's failed shell bullet**: TB-183
  already shipped via 6583b07 (briefing-side fix on retry);
  the original briefing is now correct on disk. Meta fix-task
  would be retroactive churn.
- **Briefing-validator rule "no exact cardinality / no literal
  string anchors in shell `## Verification` bullets"**: n=3
  now (TB-178 / TB-182 / TB-183) but operator's TB-172
  rejection — "validators that enumerate-known-pitfalls
  generalize poorly" — is the authoritative pattern. Skip.
- **Force-fill the empty Backlog with three greenfield items**:
  goal.md's delete-test fails. Backlog<3 trigger is a ceiling,
  not a mandate; with focus exhausted, "fill the slot" tasks
  would compete with the operator-driven focus rotation
  without addressing any real gap.
- **Greenfield follow-ups on TB-177/178/179/180/181/182/183**:
  each shipped focused; no edge case or natural extension
  surfaces yet, and `ideation quality` is the only declared
  focus — extensions outside it would drift.
- **Auto-rotate goal.md `## Current focus` when exhausted**:
  carried; still violates Non-goal "Replacing operator
  judgment on goal definition."
- **Insight bootstrap (target-project-agnostic)**: the
  `_index.md` is still empty but TB-175's rejection signals
  the operator does not currently want ideation seeding the
  insights directory. Defer indefinitely.

## Open questions for operator

- **Focus rotation needed (now actively triggered).** This
  cycle declares the sole focus item `exhausted-needs-operator`,
  so the next ideation cron tick will skip the SDK call via
  TB-174's gate (emitting `ideation_skipped reason=
  focus_exhausted`). To re-arm ideation, refresh `goal.md ##
  Current focus` with a new focus statement — candidates
  carried from prior cycles: "verifier robustness",
  "operator-walk-away resilience", or pivoting to a
  target-project focus once one is declared. Until that
  refresh lands, ideation will stay quiet by design (this is
  exactly what TB-174 was built for).
- **TB-175 rejection had no reason logged.** The
  Mattermost/CLI reject path supports `--reason`; a
  one-liner would help future cycles avoid re-proposing
  semantically-similar work. Not blocking; just signal.
- **Shell-bullet residual-risk acceptance (carried, n=3 now):**
  TB-178 / TB-182 / TB-183 all burned a retry on
  literal-string-anchor `## Verification` shell bullets. All
  resolved within budget so no escalation today. The
  TB-172-class structural intervention remains
  operator-rejected; surfacing for awareness so the operator
  can confirm this is the durable decision.
- No unadopted `cron_proposed` events.
- `.cc-autopilot/insights/_index.md` still empty; TB-175's
  rejection means ideation will not seed it without operator
  direction.

## Proposals this cycle

No proposals this cycle. The sole focus item is
`exhausted-needs-operator`, no goal-aligned uncovered gap
remains that isn't already operator-rejected, and TB-174's
gate is intentionally designed to short-circuit further
ideation runs until the operator refreshes `goal.md ##
Current focus`. Force-filling the empty Backlog would fail
goal.md's delete-test.
