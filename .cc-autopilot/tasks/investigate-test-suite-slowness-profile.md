# Investigate test-suite slowness: profile `uv run pytest -q ap2/tests/`, identify top-10 slowest tests, produce categorized investigation artifact

Tags: `#autopilot` `#tests` `#performance` `#investigation` `#code-quality`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (3) **Cost / blast-radius guards** axis by investigating WHY the project-wide test suite has grown from ~427 tests / ~57s (Apr 2026, per `.cc-autopilot/env` comment) to ~1500+ tests / ~1349s (May 2026, per TB-252's measured-runtime citation). The 23× test count growth + 23× wall-clock growth tracks linearly on its face, but the n=5 retry-exhaust cascade on 2026-05-17 (TB-245/246/247/249/250 all hit the 600s verify timeout) suggests the suite has crossed a threshold where each task's verification cost is now non-trivial — bumping `AP2_VERIFY_TIMEOUT_S` to 1800s (per the operator's 2026-05-17 env bump) unblocks the cascade but doesn't reduce per-task verification cost. Each task agent dispatch now incurs ~22 min of pytest wall-clock minimum, contributing to the 41-min total task lifetimes observed in TB-249 + TB-245. Goal.md L86-88's "unbounded blast radius" framing applies: per-task verification cost is the silent overhead operator pays on every dispatch, and under `AP2_AUTO_APPROVE=1` the cost stays invisible because no operator pause-and-review happens.

Why now: the cascade made the slowness operationally visible; TB-252 (ideation-proposed at 2026-05-17 ~09:30Z) adds the WARN surface for "timeout < observed runtime" but doesn't address the underlying slowness. Investigation BEFORE the operator decides on follow-up fixes (split suite, parallel pytest, mark slow tests, optimize specific tests) means follow-up TBs have data to scope against rather than guessing at top contributors.

## Scope

(1) **Run `uv run pytest -q ap2/tests/ --durations=20`** against the current HEAD and capture the top-20 slowest test durations.

(2) **Categorize each of the top-20 slowest tests** into one of these buckets (categorization is operator-facing; agent makes a first-pass call, operator can re-classify):
  - **essential-slow**: test genuinely needs the time (real SDK call per `AP2_REAL_SDK=1`, e2e end-to-end task lifecycle, heavy fixture setup that can't be amortized).
  - **fixable-slow**: test could be faster with concrete refactor (excessive `time.sleep`, redundant fixture setup, missed `@pytest.fixture(scope="module")` opportunity, expensive subprocess that could be mocked).
  - **candidate-for-removal**: test is redundant with other coverage (overlapping unit + e2e pair, deprecated path under test, low-signal regression-pin that doesn't pay rent per goal.md L60-63's delete-test).
  - **investigate-further**: agent couldn't categorize confidently; flagged for operator decision.

(3) **Produce an investigation artifact** at `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md` with:
  - Header naming the investigation date + suite-runtime measurement at investigation time.
  - Table of top-20 slowest tests (test path::name | duration | category | one-line rationale).
  - Aggregate counts: how many in each category, total seconds per category, % of total suite runtime per category.
  - **No fixes applied in this TB**; the artifact is the deliverable. Per-test or per-category fix TBs follow from operator review of the artifact.

(4) **Don't change pytest configuration, fixtures, or any test code in this TB.** Pure investigation. The `--durations=20` flag is invocation-only, not committed to a config file. Avoids scope creep + lets the operator decide direction with the artifact in hand.

(5) **Tests** (small — the deliverable IS the artifact):
  - `test_artifact_file_exists`: assert `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md` exists post-task.
  - `test_artifact_contains_durations_table`: assert the file contains the literal substring `## Top-20 slowest tests` AND at least 20 lines matching `^\s*\d+\.\d+s\s` (the duration prefix pattern).
  - `test_artifact_contains_category_aggregate`: assert each of the 4 category names appears at least once in the aggregate section.

## Design

**Why investigation-as-artifact rather than "investigate + propose fix in same TB"**: the slowness root cause might be 1 test, 10 tests, or a structural problem. The fix shape isn't knowable in advance. Producing a categorized artifact lets the operator review the data and either (a) file targeted fix TBs per category, (b) accept the 1800s timeout as the steady-state and not invest in optimization, or (c) decide on a structural change (split suite, parallel pytest, remove `AP2_REAL_SDK=1` from default). Bundling investigation + speculative fix into one TB risks fixing the wrong thing.

**Why `.cc-autopilot/insights/<date>.md`**: there's a precedent for insight files in this project. Date-stamped means the artifact stays useful as a historical record even if a follow-up investigation re-runs at a later date.

**Why the agent does first-pass categorization**: the agent has read access to the test files and can quickly tell "this calls `sdk.query(...)` with `AP2_REAL_SDK=1`" → essential-slow vs "this sleeps 5 seconds" → fixable-slow. Operator can override categorizations during follow-up review, but the first-pass reduces operator workload from "read 20 test files" to "review 20 one-liner categorizations and override the wrong ones."

**Goal-anchor**: the Done-when bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate to the operator" was just violated by the n=5 retry-exhaust cascade — the recovery WAS operator-manual (env bump + unfreeze 5 tasks). Reducing per-task verification cost prevents the next such cascade.

## Verification

- `uv run pytest -q ap2/tests/test_tb_investigate_suite_slow_artifact.py` — the small artifact-shape tests pass (3 cases per Scope §5).
- `test -f .cc-autopilot/insights/test-suite-slowness-2026-05-17.md` — the artifact exists on disk.
- `[ "$(grep -cE '^[0-9]+\.[0-9]+s' .cc-autopilot/insights/test-suite-slowness-2026-05-17.md)" -ge 20 ]` — at least 20 lines start with a duration-shaped prefix (the top-20 entries).
- `grep -nE "essential-slow|fixable-slow|candidate-for-removal|investigate-further" .cc-autopilot/insights/test-suite-slowness-2026-05-17.md` — exit 0; all 4 category names appear (at minimum in the legend; ideally in the aggregate counts too).
- `! git diff HEAD -- ap2/tests/ | head -1 | grep -q .` — exit 0 (no test code modified — pure investigation). The `!` inverts: zero diff output to head → grep -q . returns 1 → `!` flips to 0 → pass. (If ANY test was modified, head -1 returns content → grep matches → `!` flips → fail.)
- Prose: the artifact's top-20 table has one row per test with: test path::name, observed duration, category (one of the 4), and a one-line rationale for the categorization. Judge confirms via `Read` of the artifact and verifies the row shape.
- Prose: the artifact's aggregate section reports total runtime per category + percentage of suite runtime per category (so operator sees at a glance "essential-slow tests account for X% of the wall clock"). Judge confirms via `Read`.

## Out of scope

- Applying any fixes to slow tests — investigation artifact only. Per-category fix TBs follow from operator review.
- Changing pytest configuration files (pyproject.toml, conftest.py, pytest.ini) — orthogonal; if the artifact's findings point to config-level fixes, those land as separate TBs.
- Splitting the test suite into multiple invocations / using pytest-xdist for parallelism — structural change; needs its own design TB if the investigation findings recommend it.
- Modifying `AP2_REAL_SDK=1` policy (always-on vs opt-in vs nightly-only) — separate operator policy decision; investigation artifact informs but doesn't decide.
- Generalizing the artifact format into a reusable insights-schema — first-instance artifact; schema emerges from a third such file per goal.md L74-77's threshold-three rule.
- Re-running the investigation periodically (e.g. monthly cron job) — premature; operator runs the TB manually again later if the suite grows further.
