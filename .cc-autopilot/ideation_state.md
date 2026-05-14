# Ideation State

_Last updated: 2026-05-14T23:22:18Z by ideation cron_

## Mission alignment

Prior cycle (21:09:40Z) rebuilt the assessment around the 21:09Z pivot
to "Current focus: end-to-end automation" (goal.md L38-151, four
axes). Since then, all three highest-priority axes shipped: TB-223
(axis 1: `AP2_AUTO_APPROVE` gate, `a46c461`), TB-224 (axis 3: per-task
+ window token caps + `task_error` halt, `7e5a400`), TB-225 (axis 2:
auto-unfreeze on agent-diagnosed `BriefingFix:` shapes, `b8af9b5`) —
plus pre-pivot residuals TB-221 / TB-222 (`9b3f5a5` / `7b64617`). 3
most recent Completes considered:

- TB-225 (`b8af9b5`, 2026-05-14T22:47Z) — `_maybe_auto_unfreeze`
  sweep + 3 env knobs + parser in `_shared.py`.
- TB-224 (`7e5a400`, 2026-05-14T22:30Z) — token-cap + halt knobs
  with shared `auto_approve_window_resume` ack.
- TB-223 (`a46c461`, 2026-05-14T22:11Z) — `AP2_AUTO_APPROVE` knob,
  `auto_approved` + `auto_approve_paused` events.

The limiting factor on mission progress now shifts from "axes 1-3
deliverables don't exist" to "axes 1-3 have zero operator-facing
observability and axis 4 is unstarted." The walk-away promise stays
fictional until the operator can SEE whether the loop is healthy
without `ap2 logs` archeology.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (Manual-approval bottleneck): TB-223 shipped 3 env
      knobs + `auto_approved` / `auto_approve_paused` events + 13
      behavioral tests (commit `a46c461`).
    - Axis 2 (Failure-recovery operator dependency): TB-225 shipped
      `parse_blocked_summary_fix_shape` helper, 3 `AP2_AUTO_UNFREEZE_*`
      env knobs, `_maybe_auto_unfreeze` sweep, 17 tests (commit
      `b8af9b5`).
    - Axis 3 (Cost + blast-radius guards): TB-224 shipped per-task +
      window token caps, `task_error` single-event halt, shared
      `auto_approve_window_resume` ack verb (commit `7e5a400`).
    - Axis 4 (Multi-focus sequential execution): NOTHING shipped.
      Only `parse_focus_statuses` from TB-174 exists (reads
      `ideation_state.md`, not goal.md focus list).
  - Gaps:
    (1) **Axis 4: goal.md focus-list parser + pointer + advance
        heuristic** — goal.md L115-138 design names a concrete
        deliverable surface (multi-`## Current focus:` heading parser
        + per-focus `Done when:` sub-block + pointer state file +
        `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` heuristic + `focus_advanced`
        event + `roadmap_complete` halt + `ap2 ack roadmap_complete`
        resume) but none of it exists yet. Today goal.md carries a
        single `## Current focus:` heading; the parser ingests one
        focus only. Addressed by proposal 1 this cycle.
    (2) **Auto-approve/auto-unfreeze loop has zero operator-facing
        status surface** — `grep -n auto_approve ap2/cli.py
        ap2/web.py` returns empty. `ap2 status` exposes board counts
        + pending-review IDs + queue depth + janitor + decisions
        needed but NOT whether `AP2_AUTO_APPROVE=1`, whether the
        loop is paused (waiting on `ap2 ack
        auto_approve_window_resume`), the `consecutive_freezes`
        counter vs `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, or
        cumulative window-token spend vs cap. Operator returning
        Monday morning has to `ap2 logs | grep auto_approve` to
        learn loop health — walk-away promise blocked (goal.md
        L28-29 "walk away for a week without intervention").
        Addressed by proposal 2 this cycle.
    (3) **Status-report cron doesn't include axis 1/2/3 digest** —
        Operator's walk-away surface IS the scheduled status-report
        Mattermost post (per TB-144 shared routine + TB-128 fresh
        snapshot). Today it summarizes board counts + recent
        completes but never mentions the auto-approve/auto-unfreeze
        loop activity. A weekly walk-away digest needs: N tasks
        auto-approved (M succeeded, K froze), L tasks auto-unfrozen
        (P succeeded, Q re-froze), paused/healthy state. Addressed
        by proposal 3 this cycle.
    (4) **`BriefingFix:` emitter is unprompted; auto-unfreeze stays
        cold** — TB-225 wired the parser, sweep, and four bootstrap
        shapes into `AP2_AUTO_UNFREEZE_FIX_SHAPES`, but the
        per-task agent that emits `task_complete blocked` summaries
        doesn't know about the `BriefingFix:` prefix convention.
        `skills/ap2-task/SKILL.md` doesn't teach it. Same shape as
        TB-219 → TB-221 (verifier learned `Prose:`, but until the
        prompt taught it the convention stayed cold). Without
        upstream emission, `_maybe_auto_unfreeze` has nothing to
        parse → axis-2 delete-test stays fictional. Addressed by
        proposal 4 this cycle.
  - Status: `in-progress`
  - Reasoning: 3/4 axes shipped within the last hour; 4 substantive
    follow-up gaps identified, all 4 within ranking budget.

## Non-goal risk check

None. All four proposals stay inside axes 1-4. Observability
(proposals 2, 3) doesn't expand opt-in defaults — purely surfaces
existing state. The `BriefingFix:` teaching (proposal 4) compounds
TB-225 without enlarging its scope. Axis 4 (proposal 1) explicitly
respects goal.md L187-191 "Goal.md auto-rotation" Non-goal: pointer
is runtime-only, never mutates goal.md.

## Considered & deferred this cycle

- **Default values for `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` /
  `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`** — Both ship unset (cap
  inactive until operator sets a value). Changing defaults to a
  hard non-zero would alter semantics (silent-cap-enable contradicts
  goal.md L183-186 "OPT-IN env knobs"). Soft answer: howto
  guidance on recommended starter values; deferred until operator
  signal accumulates from at least one real walk-away cycle.
- **Historical-rate halt on cumulative verification_failed%** — Too
  early to define a threshold; TB-223's per-event consecutive-N
  halt covers the same failure shape with cheaper signal. Defer
  until at least 20 auto-approved completes exist (currently 0).
- **`ap2 audit auto-approve --window N` simulator CLI** — Useful
  for operator trust-upgrade before flipping `AP2_AUTO_APPROVE=1`,
  but overlaps with the existing TB-188/TB-189/TB-195 proposals
  pipeline; let proposal 2's status surface accumulate signal first.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=4
  authoritative reject (operator_log L80). Auto-unfreeze + the
  TB-219 classifier together generalize the recurring class.
- **TB-175-shape ideation-quality aggregator** — n=4 authoritative
  reject; signal still accumulating via TB-188 / TB-189 records.
- **`ap2 frozen TB-N` triage view** — n=4 authoritative reject
  (TB-185, 2026-05-06): "Frozen tasks are very rare."

## Cycle observations

- Prior-cycle pre-pivot residuals carry-bullet RESOLVED: operator
  approved TB-221 + TB-222 at 21:37:12Z (operator_log L137-138)
  and both shipped on 2026-05-14. Decisions-needed item drops; no
  replacement.
- Axis-rollout cadence on 2026-05-14 was unusually fast (TB-223
  → TB-225 in <90 min wall-clock), driven by the operator's
  force-ideate at 21:09Z + batch-approve at 21:37Z. The 4
  proposals this cycle deliberately add observability + axis-4
  foundation rather than racing more deliverables — without
  observability, the operator can't safely flip
  `AP2_AUTO_APPROVE=1` even with axes 1-3 shipped.

## Decisions needed from operator

(none this cycle — the pre-pivot residual triage RESOLVED via the
21:37:12Z approve batch; no carried bullets meet the actionable-
decision shape, and TB-N-awaiting-review surfacing is covered by
`ap2 status` / status-report mechanically per TB-151 / TB-173.)

## Proposals this cycle

- TB-226 — Axis 4 foundation: parse multiple `## Current focus:`
  headings from goal.md, per-focus `Done when:` sub-block, pointer
  state file, `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` heuristic,
  `focus_advanced` / `roadmap_complete` events, `ap2 ack
  roadmap_complete` resume verb — covers goal.md L115-138 axis-4
  gap (1).
- TB-227 — Auto-approve/auto-unfreeze visibility in `ap2 status`
  (text + JSON) + web home — covers gap (2), unblocks operator's
  walk-away check.
- TB-228 — Status-report cron digest block summarizing
  auto-approve/auto-unfreeze loop activity since last report — covers
  gap (3), serves Done-when-1's walk-away promise.
- TB-229 — Teach `BriefingFix:` prefix convention in
  `skills/ap2-task/SKILL.md` + per-task agent prompt; mirror TB-221's
  `Prose:` teaching pattern — covers gap (4), unblocks axis-2
  auto-unfreeze path.
