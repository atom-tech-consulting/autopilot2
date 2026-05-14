# Ideation State

_Last updated: 2026-05-14T21:09:40Z by ideation cron_

## Mission alignment

Operator pivoted goal.md at 21:09:21Z (operator_log L133-134) from
"code quality consolidation" to **Current focus: end-to-end
automation** — four axes (manual-approval bottleneck, failure-
recovery operator dependency, cost+blast-radius guards, multi-focus
sequential execution) framed by Done-when bullet 1's walk-away
promise (goal.md L28-29). Operator simultaneously filed TB-223 as the
axis-1 starting point (operator_log L135) and force-ideated
(operator_log L136). The code-quality arc that drove TB-203 → TB-220
is acknowledged in goal.md L40-49 as "substantively addressed" — the
limiting factor has shifted axes. 3 most recent Completes:

- TB-217 (`59bd1ba`, 2026-05-14T07:44Z) — `locked_inplace` +
  `locked_sidecar` exposed from `ap2/_shared.py`.
- TB-219 (`4814b97`, 2026-05-14T07:38Z) — verify.py 3-layer
  classifier with `Prose:` hard override.
- TB-220 (`a8a949e`, 2026-05-14T07:17Z) — `now()` + `read_pid()`
  consolidated to `_shared.py`.

## Current focus assessment

- **Current focus: end-to-end automation (goal.md L38-151, four axes)**
  - Progress so far:
    - Axis 1 (Manual-approval bottleneck): TB-223 in Backlog
      (operator-filed 21:09Z) — `AP2_AUTO_APPROVE` knob +
      `AP2_AUTO_APPROVE_GATE_TAGS` + `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`
      regression-pause + `auto_approved` event.
    - Axes 2-4: nothing shipped or proposed yet — focus is new
      (21:09Z, this cycle).
  - Gaps:
    (1) **Axis 3: Cost + blast-radius guards** — TB-223 covers the
        regression-pause but explicitly excludes "Token-cost ceilings
        / per-window budgets" (TB-223 brief L77) and does NOT halt on
        `task_error` (infrastructure failure, distinct from
        `verification_failed`). Without per-task and per-window token
        caps, auto-approve is unbounded-blast-radius (goal.md
        L103-113); operator cannot responsibly enable
        `AP2_AUTO_APPROVE=1` until cost ceilings ship alongside.
        Addressed by proposal 1 this cycle.
    (2) **Axis 2: Failure-recovery operator dependency** — Frozen
        tasks today require operator `ap2 unfreeze`. Goal.md L92-100
        names two concrete in-codebase examples (TB-204 `grep -lE` →
        `grep -rlE`, TB-207 literal-backtick) where the agent
        self-diagnosed the briefing-shape fix in its `task_complete
        blocked` summary; daemon could auto-apply allowlisted shapes.
        Addressed by proposal 2 this cycle.
    (3) **Axis 4: Multi-focus sequential execution** — goal.md
        L115-138 design is concrete (pointer + `Done when:`
        per-focus + `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` heuristic +
        `roadmap_complete` halt) but larger design surface
        (focus-parsing + pointer state + per-focus exhaustion gate
        + goal.md schema changes). Defer this cycle in favor of the
        axes 2/3 proposals that compound directly on TB-223.
    (4) **Pre-pivot residuals (TB-221, TB-222)** — filed 09:11Z
        against the old code-quality focus; operator kept them on
        the board through the 21:09Z pivot. Surfaced in
        Decisions-needed below for triage; ideation does not
        re-propose or alter.
  - Status: `in-progress`
  - Reasoning: Focus is brand-new (this cycle); 4 substantive gaps
    identified, 2 ranked-and-proposed, 1 deferred (axis 4 size),
    1 routed to operator decision (pre-pivot residuals).

## Non-goal risk check

None. Axes 1-4 are all explicitly inside goal.md's Mission and
Done-when. The opt-in env-knob shape on all proposals respects
goal.md L183-186 "Unconditional automation" Non-goal (auto-approve,
auto-unfreeze are OPT-IN with conservative defaults). No drift
toward generic-task-scheduler / replace-operator-judgment /
multi-tenancy / real-time / cross-project axes.

## Considered & deferred this cycle

- **Axis 4: Multi-focus pointer + per-focus `Done when:` gate** —
  Concrete design in goal.md L115-138 but larger surface than
  axes 2/3; defer to next cycle once TB-223 + the two proposed
  follow-ups have been operator-triaged so the auto-approve
  cluster's shape is settled before adding focus-advance on top.
- **CLI verb `ap2 auto-approve --enable|--disable`** — TB-223 brief
  L40-41 explicitly rejected for runtime-toggle drain-semantic
  reasons; both this cycle's proposals match the env-only pattern.
- **`ap2 frozen TB-N` triage view** — n=4 authoritative reject
  (TB-185, 2026-05-06): "Frozen tasks are very rare"; auto-unfreeze
  proposal addresses the recurring class without introducing a new
  triage surface.
- **Wack-a-mole shell-bullet linting (TB-172-shape)** — n=4
  authoritative reject; auto-unfreeze proposal generalizes the
  recurring class (operator-curated allowlist of fix-shapes) rather
  than enumerating bullet pitfalls.
- **TB-175-shape ideation-quality insight aggregator** — operator
  log L80 carry-deferral; signal still accumulating. Stays carried.

## Cycle observations

- Prior cycle's "6-consecutive byte-identical" observation
  RESOLVED by the operator's 21:09Z update_goal + force_ideate —
  cadence question is moot, the operator engaged via the goal.md
  channel they explicitly endorsed (operator_log L68 "edit goal.md
  — that's the cheap, principled path"). Dropping the carried
  observation; no replacement needed.

## Decisions needed from operator

- Decision needed: triage pre-pivot Backlog residuals TB-221
  (`Prose:` prefix in briefing-authoring prompts) and TB-222
  (`_shared.py` happy+error tests). Both were filed 2026-05-14T09:11Z
  against the now-superseded "code quality" focus; operator kept
  them on the board through the 21:09Z pivot to "end-to-end
  automation." Operator action: either `ap2 approve TB-221 TB-222`
  to drain them as residual code-quality cleanup before the new
  focus arc, or `ap2 reject` them as stale-focus so ideation doesn't
  treat them as carry-defer context next cycle. Unblock-condition:
  triage resolves whether the new-focus proposals (TB-224, TB-225
  and successors) compete with these for batch-approve attention.

## Proposals this cycle

- TB-224 — Axis 3 cost guards: `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`
  + `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` + `task_error` halt — covers
  goal.md L103-113 unbounded-blast-radius gap left by TB-223.
- TB-225 — Axis 2 auto-unfreeze: parse agent-diagnosed briefing-shape
  fix from `task_complete blocked` summary; operator-allowlisted
  shapes auto-applied with per-task + per-day caps — covers
  goal.md L90-100 axis-2 delete-test.
