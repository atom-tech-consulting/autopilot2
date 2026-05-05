# Ideation State

_Last updated: 2026-05-05T02:02:00Z by ideation cron_

## Mission alignment

Recent 5 completes all serve the meta-mission of making the
ideation→approve→dispatch loop trustworthy:

- TB-173 (aee515e) — surface ideation_state.md "Open questions for
  operator" in `ap2 status` text + JSON + web home + cron status-report
  state_extras (closes operator-side surfacing gap from prior cycle).
- TB-171 (4344cc2) — `_validate_briefing_structure` rejects `Manual:` /
  `[manual]` bullets in `## Verification` at queue-append time
  (mirrors TB-138 prompt rule + `ap2 check` warning into the
  pre-allocation gate).
- TB-170 (a47328e) — `--skip-goal-alignment` flag on `ap2 add` /
  `ap2 update` — operator escape hatch from TB-161/164 strict checks.
- TB-169 (0d4fd53) — trim ideation `_events_block` to a curated
  allowlist of event types.
- TB-168 (c113f4c) — trim ideation `_current_state_block` (drop board
  counts + recent commits, keep `now:`).

No drift; each ships a visible step toward operator-walks-away
reliability of the ideation cron.

## Current focus assessment

goal.md "Current focus: ideation quality" is the sole declared focus.

- **Ideation quality (gap-covering without drift; push for progress
  without scope creep)**
  - Progress so far: structural guards now ship every gap last
    cycle's assessment named — TB-161 enforces goal-cite at
    queue-append, TB-164 enforces line-anchored Why-now ≥40 chars,
    TB-163 surfaces operator rejections in the prompt header,
    TB-171 rejects Manual bullets, TB-173 surfaces ideator open
    questions in `ap2 status` + web + cron. Earlier scaffolding
    (TB-121 review gate, TB-138 prompt rule, TB-152 reject reasons,
    TB-154 canonical structure validator) all in place. TB-170 adds
    the operator escape hatch.
  - Gaps:
    (1) The ideator's self-declared `Status: exhausted-needs-operator`
    is just text in `.cc-autopilot/ideation_state.md` — no Python
    parses it. The natural ideation cron keeps firing (cooldown +
    queue-depth gate only) even when the prior cycle's assessment
    explicitly said "no actionable gaps left." Each such tick spends
    ~$0.10–$1.00 on increasingly thin proposals; operator's only
    stop-signal today is `AP2_IDEATION_DISABLED=1` (manual env knob).
    TB-173 just landed the surfacing pipe (parse_open_questions); the
    parallel parse_focus_statuses + auto-skip wiring isn't in place.
    (2) Shell-bullet pitfall enumeration (TB-172 path) was rejected
    by the operator on 2026-05-05T00:45Z as "wack-a-mole fix that
    only enumerate limited cases and generalize poorly to other
    project." That's authoritative — the gap is now **accepted
    residual risk**: TB-138 prompt rule + post-hoc verifier remain
    the only guards on shell-bullet shape. Don't re-propose this
    gap (also see "Considered & deferred" below).
    (3) No quantitative signal exists on whether the structural-gate
    cascade (TB-121/138/154/161/163/164/171) actually moved
    acceptance rate vs the pre-gate baseline. operator_log.md has
    the data (`approve` vs `rejected ideation proposal` lines from
    TB-152) but `_index.md` is empty and no insight ever computed
    the ratio. Without that grounded signal the operator's "is this
    focus exhausted?" call is intuition-only.
  - Status: `in-progress`
  - Reasoning: gaps #1 and #3 are non-trivial, mechanically
    actionable, and structurally distinct from TB-172's whack-a-mole
    shape. After they land, "Current focus: ideation quality" is
    plausibly `exhausted-needs-operator` and the operator may want
    to refresh goal.md `## Current focus`.

## Non-goal risk check

None. Nothing in flight strays into goal.md's Non-goals (generic task
scheduler, multi-tenancy, real-time collab, cross-project
orchestration, replacing operator judgment on goal definition).
TB-174 deliberately stays read-only on goal.md — it parses ideator's
self-reported focus statuses, never the goal definition itself.

## Considered & deferred this cycle

- **Shell-bullet pitfall validator (any flavor)**: operator rejected
  TB-172 on 2026-05-05 with "wack-a-mole … generalizes poorly."
  That's authoritative — including `bash -n` / shellcheck / actually-
  execute-in-sandbox variants. Closes-with-different-approach is
  still "enumerate cases" in disguise. Don't re-propose.
- **Trivial Why-now content judge** (length-passing but content-thin
  Why-nows): same shape as last cycle's deferral; no signal of thin
  Why-nows actually slipping through TB-164's gate.
- **Surface focus statuses in `ap2 status`** (per-focus-item
  status row): lower leverage than auto-skip — TB-173 already shows
  open questions to the operator, and the ideation_skipped event
  itself is an explicit stop-signal. Defer until TB-174 lands and
  reveals whether the per-focus surfacing is needed beyond
  operator-driven `cat .cc-autopilot/ideation_state.md`.
- **Auto-rotate goal.md `## Current focus` when exhausted**: violates
  Non-goal "Replacing operator judgment on goal definition." Operator
  owns focus rotation; ideation surfaces and stops.
- **Cross-cycle deferral aging tracker**: still no signal that
  long-stale deferrals are a problem; defer.
- **Recurring rejection topics**: only one `rejected ideation
  proposal` line in operator_log.md tail (TB-172 today). TB-163's
  surfacing exists; no pattern to call out yet.

## Open questions for operator

- **Tasks awaiting review this cycle (`ap2 approve` / `ap2 reject`)**:
  TB-174, TB-175. Both gated `@blocked:review` per TB-121.
- **Focus-rotation candidate**: after TB-174/TB-175 land + approve,
  "Current focus: ideation quality" is plausibly
  `exhausted-needs-operator`. Operator may want to refresh goal.md
  `## Current focus` (e.g. "verifier robustness",
  "operator-walk-away resilience", or a target-project focus item)
  so future ideation has a fresh anchor and TB-174's auto-skip gate
  unlatches.
- **Shell-bullet residual-risk acceptance**: TB-172 reject implies
  the shell-bullet pitfall class stays a verifier-side gate (no
  queue-append linting). Confirm that's the durable decision
  (or thaw via fresh design proposal — but not as a wack-a-mole
  enumerator).
- No unadopted `cron_proposed` events.
- `.cc-autopilot/insights/_index.md` still empty; TB-175 (proposed
  this cycle) bootstraps the first insight by computing the
  post-TB-121 ideation acceptance rate.

## Proposals this cycle

- TB-174: `parse_focus_statuses` helper + auto-skip ideation cron
  when ALL focus items are `exhausted-needs-operator`. Closes
  gap #1.
- TB-175: `#evaluation` insight task — compute post-TB-121
  ideation acceptance/reject rate from operator_log.md and write
  `.cc-autopilot/insights/ideation_quality.md` with proper YAML
  front matter. Closes gap #3 (and bootstraps the empty insights
  directory). One #evaluation per cycle per Step 0.5 rule.
