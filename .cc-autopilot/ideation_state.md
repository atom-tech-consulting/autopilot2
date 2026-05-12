# Ideation State

_Last updated: 2026-05-12T19:49:50Z by ideation cron_

## Mission alignment

The cycle pivots on a fresh failure: TB-203 (commit `1ed8a03`, 19:18:57Z)
and TB-204 (commit `ecd5b2f`, 19:49:50Z) both `verification_failed` on
`uv run pytest -q ap2/tests/` not because their own work is wrong but
because the operator's 2026-05-12T17:02Z goal.md pivot left
`ap2/howto.md`'s `## Authoring goal.md` worked-example block (added by
TB-200, `7d7c142`) quoting the OLD `Current focus: ideation quality
signal collection` heading verbatim — exactly the string
`test_docs.py::test_worked_example_quotes_appear_verbatim_in_goal_md`
asserts present in `goal.md`. TB-204's commit message explicitly names
the diagnosis ("Two pre-existing test_docs.py failures (unrelated to
TB-204) reflect the operator's 2026-05-12 17:02Z goal.md pivot...").
TB-205 is queued next and will hit the same project-wide gate. All five
most recent Completes still serve the goal:

- TB-204 (`ecd5b2f`, 19:49Z) — canonical-valid briefing fixture +
  ~30+ inline migrations (reusability axis, scope-correct, gate-blocked)
- TB-203 (`1ed8a03`, 19:18Z) — docs-drift gate + howto/architecture
  resync (docs axis, scope-correct, gate-blocked)
- TB-202 (`b09e3bc`, 2026-05-12T08:02Z) — refuse `ap2 backfill-proposals`
  + `ap2 cron edit` mid-Active
- TB-201 (`03c4fc1`, 2026-05-12T07:49Z) — queue-route `ap2 ack` +
  `operator_log_append` MCP
- TB-200 (`7d7c142`, 2026-05-12T00:39Z) — `## Authoring goal.md` in
  `ap2/howto.md` (the surface that's now stale post-pivot)

Slot count = 2 (3 Backlog items occupy 3/5 of the operator threshold).
Insights index empty. No unadopted `cron_proposed` events.

## Current focus assessment

- **Current focus: code quality (goal.md L38-97, four axes)**
  - Progress so far: TB-203 (docs axis) and TB-204 (reusability axis)
    both committed scope-correct work — `test_docs_drift.py` 4 tests
    pass, `_briefing_fixtures.py` + ~30 migrations land, but both
    failed the project-wide pytest gate on 2 pre-existing failures.
    TB-205 (testing axis) approved + queued, awaiting promotion.
  - Gaps:
    (1) **Active blocker** — `test_docs.py::test_worked_example_quotes_appear_verbatim_in_goal_md`
    and `::test_worked_example_current_focus_satisfies_anchor_validator`
    fail because `ap2/howto.md` L149-156 quotes `Current focus:
    ideation quality signal collection` verbatim, which no longer
    appears in `goal.md` after the 17:02Z pivot. Every dispatched
    task now verification-fails its project-wide gate regardless of
    its own work. Without a fix, TB-203/204/205 cascade to
    retry-exhausted → Frozen.
    (2) **Structural coupling** — TB-200's verbatim-heading-quote
    pattern in the Current-focus worked example creates a hidden
    operator obligation: rotating focus requires simultaneously
    editing howto.md, otherwise pytest red-screens the daemon.
    Mission/Done-when/Non-goals/Constraints worked-example blocks
    don't rotate per-cycle, so their verbatim coupling is fine; only
    the Current-focus quote pays the rotation tax.
    (3) **Testing axis (untouched outside TB-205)** — `AP2_VERIFY_JUDGE_*`,
    `AP2_JANITOR_*`, `AP2_AUTO_DIAGNOSE_*`, `AP2_MM_*`, `AP2_REAL_SDK`,
    `AP2_AGENT_EFFORT`, `AP2_VERIFY_TIMEOUT_S` env knobs have no
    test-file references either; TB-205's 4-knob slice is intentional
    narrowing (operator-rejection pattern is anti-enumeration).
    (4) **Cleanness axis** — three of four named modules past
    threshold; deferred per goal.md L86-87 anti-speculative-refactor
    guardrail (unchanged from prior cycle).
  - Status: `in-progress`
  - Reasoning: focus is ~3h old; Gap (1) is an active operational
    blocker on three in-pipeline tasks — must fix this cycle.
    Structural Gap (2) is naturally addressed by the same proposal.
    Gaps (3) and (4) stay deferred against operator-rejection
    pattern and goal.md guardrail respectively.

## Non-goal risk check

None. The fix proposal stays inside ap2's own surface (howto.md,
test_docs.py) — no drift into generic-task-scheduler, replace-operator-
judgment, multi-tenancy, real-time, or cross-project axes.

## Considered & deferred this cycle

- **Auto-generating howto.md from goal.md** — would risk drift in the
  opposite direction (goal.md L70-72 names paraphrased docs as a
  failure mode). The right fix is a small-surface decoupling at the
  worked-example level, not full docs synthesis.
- **`ap2 update-goal` post-step that runs pytest** — parallel surface;
  the existing pytest gate at task verify time is the authority.
  Operator rejection pattern (TB-184 lineage) flags surface-duplication.
- **Extending TB-205's pattern to `AP2_VERIFY_JUDGE_*` / `AP2_JANITOR_*` /
  `AP2_AUTO_DIAGNOSE_*` / `AP2_MM_*` knobs (testing axis Gap 3)** — the
  operator's recurring rejection pattern (TB-172 "wack-a-mole that only
  enumerates limited cases", TB-185 "consolidated triage view of Frozen
  tasks") flags enumerative-coverage proposals as anti-pattern. Hold
  until TB-205 lands and reveals which specific behaviors hide
  silent-regression risk worth pinning.
- **Module decomposition for `ap2/tools.py` / `ap2/web.py` /
  `ap2/daemon.py` / `ap2/cli.py`** — goal.md L86-87 explicit
  anti-speculative-refactor guardrail; no operator-reported
  confidence-to-modify regression yet.
- **TB-172/TB-175/TB-184/TB-185** — authoritative rejects;
  will not re-propose. Rejection-pattern note (n=4, unchanged shape):
  "creates parallel surface OR enumerative wack-a-mole OR off-focus."
  TB-206 below avoids each: it removes a coupling (not parallel
  surface), targets a specific structural failure mode the focus
  rotation just exposed (not enumerative), and is explicitly
  code-quality docs-axis goal-anchored.

## Cycle observations

(Triage from prior cycle: prior had "(no carried bullets this cycle)";
nothing to triage forward. Current observations promote to structured
sections — Mission alignment carries the focus-pivot fresh-failure
narrative; Gap (1) and (2) carry the rotation-coupling story; no
agent-internal residual that doesn't fit a structured section.)

- (no carried bullets this cycle)

## Decisions needed from operator

(none this cycle — the proposal below addresses Gap (1) + (2)
directly and the cascade unblocks naturally. TB-203 and TB-204's
return-to-Backlog with verification_failed status means the daemon
will retry them on its own once the project-wide gate passes again;
no operator unfreeze needed unless they exhaust their retry budget
before TB-206 lands.)

## Proposals this cycle

1 proposal (slots=2):
- TB-206 — Resync `ap2/howto.md`'s Current-focus worked-example block
  with post-pivot `goal.md`; decouple the example from operator focus
  rotation so the same failure doesn't recur (Gap 1 + Gap 2,
  docs/testing axes)

Slot 2 intentionally unused: the remaining viable candidates either
re-trip the operator's enumerative-wack-a-mole rejection pattern
(env-knob coverage extensions) or hit goal.md's anti-speculative-
refactor guardrail (module decomposition). Better to land the
unblocker, observe TB-203/204/205 retries clear, and re-derive
proposals against any fresh failure modes next cycle.
