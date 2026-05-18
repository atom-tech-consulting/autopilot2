# Add `ap2/tests/conftest.py` shield: set `AP2_VALIDATOR_JUDGE_DISABLED=1` by default for unit tests (mirror existing e2e shield; TB-253 Option 1)

Tags: `#autopilot` `#tests` `#performance` `#code-quality` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (3) **Cost / blast-radius guards** axis by closing the leaked LLM-judge cost surface in the unit-test suite. TB-253's investigation artifact at `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md` identifies the root cause: **18 of the top-20 slowest tests** in `uv run pytest -q ap2/tests/` exercise `tools.do_board_edit({"action": "add_*"})` or `tools.do_operator_queue_append({"op": "add_*"})` in unit-test files that do NOT set `AP2_VALIDATOR_JUDGE_DISABLED=1`. The `add_*` path runs TB-235's `_check_dependency_coherence` LLM judge (Haiku-4.5 SDK call, 10-18s per call). Tests iterating over 3 add_* variants pay ~30-40s of judge latency for what should be ~1s of unit-test work. `ap2/tests/e2e/conftest.py` (line 66) already disables the judge for the entire e2e directory — with an explicit docstring naming this exact failure mode ("would dispatch a real Haiku-4.5 SDK call ... slow, costly, and potentially makes a live API call from CI"). The unit-test surface has no equivalent shield, so unit tests accidentally inherit the production default. This TB ships the surgical mirror: a top-level `ap2/tests/conftest.py` that sets `AP2_VALIDATOR_JUDGE_DISABLED=1` by default, leaving the two intentional-judge-exercising test modules (`test_dep_validator_judge.py`, `test_tb243_validator_judge_surface.py`) free to override per-test as they do today.

Why now: the 2026-05-17 n=5 retry-exhaust cascade (TB-245/246/247/249/250) all hit the 600s `AP2_VERIFY_TIMEOUT_S` wall because the per-task project-wide verify now takes ~1336s. The operator bumped the knob to 1800s to unblock, but the per-task cost remains silent overhead under `AP2_AUTO_APPROVE=1`. TB-253 projects **300-500s shaved** if this fix lands — bringing suite back to ~800-1000s, well under the original 600s default cap (the timeout bump becomes excess headroom rather than required survival).

## Scope

(1) **Add `ap2/tests/conftest.py`** as a new file with one effect: set `os.environ["AP2_VALIDATOR_JUDGE_DISABLED"] = "1"` at module import time. This makes the env var present for the entire pytest session under `ap2/tests/`, before any test or fixture runs. Module docstring should mirror the explicit framing of `ap2/tests/e2e/conftest.py`'s docstring — name the failure mode ("would dispatch real Haiku-4.5 SDK calls per `do_board_edit({add_*})` / `do_operator_queue_append({op: add_*})` invocation; expensive in cumulative test wall-clock and potentially makes live API calls from CI") and the surgical-mirror provenance (TB-253 investigation pointed here).

(2) **Don't modify `ap2/tests/e2e/conftest.py`** — its existing shield stays as-is. The two shields are now redundant for e2e tests (the unit-test conftest applies to subdirectories too) but the duplication is harmless and the e2e conftest's docstring is a primary reference operators read; removing it would lose that documentation.

(3) **Verify the two intentional-judge-exercising test modules still work**:
  - `ap2/tests/test_dep_validator_judge.py` — currently sets/overrides `AP2_VALIDATOR_JUDGE_DISABLED=0` or stubs the judge function explicitly. After this conftest lands, those local overrides become the only way the judge runs; tests must still pass.
  - `ap2/tests/test_tb243_validator_judge_surface.py` — same shape. Verifies the validator-judge fail/timeout counter surfaces.
  Run both modules explicitly to confirm: `uv run pytest -q ap2/tests/test_dep_validator_judge.py ap2/tests/test_tb243_validator_judge_surface.py` exits 0.

(4) **Tests for the conftest itself** (`ap2/tests/test_conftest_judge_shield.py`):
  - `test_validator_judge_disabled_env_is_set_during_test_session`: assert `os.environ.get("AP2_VALIDATOR_JUDGE_DISABLED")` is truthy when the test runs (sanity that the conftest applied).
  - `test_do_board_edit_add_does_not_invoke_judge_under_shield`: simulate an `add_*` op with a mock SDK; assert the SDK is NOT called (shield works); the mock raises if called.
  - `test_local_override_unsets_shield`: in a test where `monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED")` is called, assert subsequent `add_*` does invoke the judge (per-test override path still works for the intentional modules).

(5) **Measure the speed-up** (artifact-side, not committed code):
  - After landing, run `uv run pytest -q ap2/tests/ --durations=20` and capture the new top-20 + total suite runtime.
  - Append the measurement to `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md` under a new "## Post-fix measurement (TB-N)" section, or write a new dated artifact `.cc-autopilot/insights/test-suite-slowness-2026-05-XX-post-fix.md` — implementer's call.
  - The new measurement is the empirical proof that the fix did what TB-253's projection claimed (300-500s saved).

(6) **Don't touch the `_check_dependency_coherence` function itself, the validator's prompt, or the `AP2_VALIDATOR_JUDGE_DISABLED` knob's read/parse logic** — orthogonal to the fix. The shield works at the env-var layer; no code change in tools.py is needed.

(7) **Don't lower `AP2_VERIFY_TIMEOUT_S` back to 600s** as part of this TB — separate operator decision after the post-fix measurement lands. The bumped 1800s timeout has no harm in being headroom; rolling back is a follow-up if the operator chooses.

## Design

**Why module-level `os.environ.setdefault()` vs pytest fixture**: setting at conftest import time guarantees the env var is set BEFORE any test or fixture runs (pytest imports conftest.py once per session, before collecting tests). A pytest fixture with `autouse=True` and `scope="session"` would also work but adds a slight indirection (the fixture activates on first test, not at import) — the env-var-at-import is simpler and matches the existing `e2e/conftest.py` pattern.

**Use `setdefault` not direct assignment**: `os.environ.setdefault("AP2_VALIDATOR_JUDGE_DISABLED", "1")` lets an operator override via shell env (`AP2_VALIDATOR_JUDGE_DISABLED=0 uv run pytest -q ap2/tests/`) if they want to verify the validator IS firing locally. Direct assignment would shadow operator intent.

**Why not just delete TB-235's LLM judge entirely**: TB-235 has real value at queue-append time for ideation-generated briefings — it catches hard-dep mismatches operators might miss under auto-approve. The cost is justified at the production add path; it's not justified at unit-test setup-only add invocations. The shield surgically separates the two without removing TB-235's production value.

**Goal-anchor**: the Done-when bullet "an operator can point ap2 at a fresh project, paste a `goal.md`, and walk away for a week without intervention" depends on per-task verification cost being bounded. A 1336s suite means every task dispatch pays ~22 min of pytest wall-clock; under auto-approve at ~50 tasks/day, that's ~18 hours/day of CI-equivalent compute. Cutting to 800-1000s drops it to ~12 hours/day. Real walk-away cost reduction.

**Why the post-fix measurement (Scope §5) lives in an artifact, not a test assertion**: the measurement is empirical and depends on the test box's CPU/IO speed; pinning a specific runtime in a test would be brittle (CI environment variance). The artifact captures the measurement for operator review without coupling tests to specific timing.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0); critically, the test count and pass/fail outcomes are unchanged vs pre-fix (the shield doesn't change test behavior, only test runtime).
- `uv run pytest -q ap2/tests/test_dep_validator_judge.py ap2/tests/test_tb243_validator_judge_surface.py` — exit 0; the two intentional-judge-exercising modules still test what they're supposed to.
- `uv run pytest -q ap2/tests/test_conftest_judge_shield.py` — new test module's 3 cases (per Scope §4) all pass.
- `test -f ap2/tests/conftest.py` — exit 0; the new file exists at the expected path.
- `grep -nE "AP2_VALIDATOR_JUDGE_DISABLED" ap2/tests/conftest.py` — exit 0; the shield sets the expected env var.
- `grep -nE "setdefault" ap2/tests/conftest.py` — exit 0; the env-var setting uses `setdefault` (not direct assignment), preserving operator-shell override.
- `test -f .cc-autopilot/insights/test-suite-slowness-2026-05-17.md` — exit 0; the original TB-253 artifact still exists (regression-pin against accidental deletion).
- `[ "$(grep -cE '^[0-9]+\.[0-9]+s' .cc-autopilot/insights/test-suite-slowness-2026-05-17.md)" -ge 20 ]` — the original artifact's top-20 measurements remain (regression-pin).
- Prose: a post-fix measurement is captured in `.cc-autopilot/insights/` — either appended to the 2026-05-17 file under a new section, or as a new dated file. The measurement reports the new suite total runtime AND a new top-20 (which should NOT contain the same `add_*`-shape tests dominating the list). Judge confirms via `Read` of whichever artifact the implementer chose.
- Prose: the new `ap2/tests/conftest.py`'s module docstring names the failure mode (real-SDK call on `add_*` paths, expensive in cumulative wall-clock, potentially live API calls from CI) AND references TB-253 as the investigation that motivated the shield. Mirrors the explicit framing of the existing `ap2/tests/e2e/conftest.py` docstring. Judge confirms via `Read`.

## Out of scope

- Removing or modifying `ap2/tests/e2e/conftest.py`'s existing shield — redundant but harmless; the e2e docstring is a primary reference.
- Deleting or refactoring TB-235's `_check_dependency_coherence` LLM judge — production-path validator is unchanged; only test invocations are shielded.
- Adding `AP2_VALIDATOR_JUDGE_DISABLED` to other test-related env-knob shields (e.g. `AP2_VERIFY_JUDGE_DISABLED`, `AP2_JANITOR_JUDGE_DISABLED`) — separate observability question; this TB closes the documented n=18 leakage, not speculative future ones.
- Rolling back `AP2_VERIFY_TIMEOUT_S` from 1800s to 600s — separate operator decision; the bumped value is harmless headroom.
- Architectural refactor of `do_board_edit` to separate "validate" and "commit" code paths — bigger redesign; the conftest shield is the surgical fix without refactoring production code.
- Adding LLM-judge-invocation-count surfacing (per the "silent successful invocations" observation from TB-253's writeup) — separate observability TB; cost of LLM calls in production is bounded by the existing `AP2_VALIDATOR_JUDGE_TIMEOUT_S` + fail-open posture.
- Auto-running the post-fix measurement on a recurring cron — single-shot measurement is sufficient; operator runs the investigation TB again later if the suite grows.
