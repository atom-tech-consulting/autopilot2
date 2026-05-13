## Goal

Close the four-name env-knob coverage debt the **Current focus: code quality** (goal.md L38) testing axis (goal.md L58-63: "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path") inherited from TB-208's landing. TB-208's docstring (`ap2/tests/test_coverage_drift.py` L301-305) explicitly enumerates four env knobs — `AP2_TASK_MAX_TURNS`, `AP2_JANITOR_JUDGE_EFFORT`, `AP2_JANITOR_JUDGE_MAX_TURNS`, `AP2_MM_TEAM_ID` — as "follow-up coverage task[s] waiting to happen" whose ONLY test-file references today are the comment-block lines themselves. The substring drift gate passes by gate-satisfaction shim, not by any real assertion of default / override / invalid-value contract. Direct mirror of TB-205's approved pattern (4 SDK-cost knobs → `ap2/tests/test_env_knobs.py` with 17 tests pinning each knob's contract).

Why now: TB-208's docstring names this gap as a closed-set follow-up at exactly four entries — same count and shape as TB-205. Without it, every future refactor of `daemon._run_task` (uses AP2_TASK_MAX_TURNS), `janitor._judge` (uses both AP2_JANITOR_JUDGE_* knobs), or `sandbox.install_channel` (uses AP2_MM_TEAM_ID) can silently break the env-knob contract — the drift gate stays green via the shim while real behavior regresses. Replacing the shim with real tests closes the testing-axis gap that TB-208 explicitly tagged as "coverage debt, not exemptions" and removes a structurally-fragile gate-satisfaction shortcut in one shot.

## Scope

- Add a new test module `ap2/tests/test_tb210_env_knobs.py` (or extend `ap2/tests/test_env_knobs.py` — implementer's call; both keep the TB-205 shape) covering the four env knobs:
  - `AP2_TASK_MAX_TURNS` — read by `daemon.py:208` for task-agent `max_turns` (default 50).
  - `AP2_JANITOR_JUDGE_EFFORT` — read by `janitor.py:717` for judge SDK `effort`.
  - `AP2_JANITOR_JUDGE_MAX_TURNS` — read by `janitor.py:724` for judge SDK `max_turns` (default 12).
  - `AP2_MM_TEAM_ID` — read by `sandbox.py:817,943` for MM API team scope (default `None` / unset).
- Per knob, add at minimum:
  - One test pinning the default when the env var is unset (e.g. `AP2_TASK_MAX_TURNS` defaults to 50).
  - One test pinning an explicit override value flows through to the call site.
  - One test pinning fallback behavior on invalid input where applicable (non-int for the integer knobs; documented behavior — exception OR default fallback per call-site code path).
  - For `AP2_MM_TEAM_ID`, pin both the unset path (RuntimeError raised per `sandbox.py:817`) and the set path (value flows through).
- Remove the four matching rows from the discovered-at-landing comment block in `ap2/tests/test_coverage_drift.py` (L302-305 today). With real test references in place, the shim entries are redundant; leaving them creates a false "exempt" impression.
- Leave the 8 event-type debt rows in the comment block untouched (separate follow-up — different shape, deferred per this cycle's ideation_state.md "Considered & deferred").

## Design

- Mirror `ap2/tests/test_env_knobs.py`'s existing 17-test layout for the TB-205 knobs: one `def test_<knob>_<aspect>():` function per default / override / invalid contract, using `monkeypatch.setenv` / `monkeypatch.delenv` to scope env state. Import the call-site module and patch / assert against the actual int / str the call site uses (don't re-implement `os.environ.get` wrapping).
- For `AP2_TASK_MAX_TURNS` / `AP2_JANITOR_JUDGE_MAX_TURNS`: invalid-value contract uses `int(...)` directly at the call site, so `AP2_TASK_MAX_TURNS=abc` raises `ValueError`. Pin that explicitly with `pytest.raises`.
- For `AP2_JANITOR_JUDGE_EFFORT`: read via `os.environ.get(...)` and passed through as a string to the SDK; the contract is "if set, propagated verbatim; if unset, falls back to janitor module default." Pin both branches.
- For `AP2_MM_TEAM_ID`: invoked by sandbox install-channel; the contract is "if unset and the user has no teams, raise RuntimeError; if set, use as-is." Pin via direct call to the helper (do not require a live Mattermost server — use the same mocking pattern existing sandbox tests use).
- Removing the 4 comment-block rows is a 4-line deletion in `test_coverage_drift.py`. The drift gate continues to pass because the new test module references the knob names; the gate's substring check (`name in blob` across all test files) doesn't care which file mentions them.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the change.
- `uv run pytest -q -k "tb210 or task_max_turns or janitor_judge_effort or janitor_judge_max_turns or mm_team_id"` — the new tests run and pass; the `-k` selection yields ≥12 tests (3 contracts × 4 knobs minimum).
- `test -f ap2/tests/test_tb210_env_knobs.py` OR (if implementer chose to extend) the new test names exist in `ap2/tests/test_env_knobs.py`. Auto-verify via: `grep -E "def test_(task_max_turns|janitor_judge_effort|janitor_judge_max_turns|mm_team_id)_" ap2/tests/test_env_knobs.py ap2/tests/test_tb210_env_knobs.py 2>/dev/null | wc -l` returns a value `≥ 4` (one happy-path per knob at minimum).
- `grep -rE "AP2_TASK_MAX_TURNS|AP2_JANITOR_JUDGE_EFFORT|AP2_JANITOR_JUDGE_MAX_TURNS|AP2_MM_TEAM_ID" ap2/tests/` returns hits in BOTH `test_coverage_drift.py` (drift-gate machinery — may or may not retain comment rows depending on implementer cleanup) AND at least one OTHER test file under `ap2/tests/` (the new module / extension). Auto-verify: `[ "$(grep -lE 'AP2_TASK_MAX_TURNS|AP2_JANITOR_JUDGE_EFFORT|AP2_JANITOR_JUDGE_MAX_TURNS|AP2_MM_TEAM_ID' ap2/tests/*.py | wc -l)" -ge 2 ]`.
- `uv run pytest -q ap2/tests/test_coverage_drift.py` — drift gate stays green; the four knobs still resolve (now via the new test module, not the comment block).
- `grep -cE "AP2_(TASK_MAX_TURNS|JANITOR_JUDGE_EFFORT|JANITOR_JUDGE_MAX_TURNS|MM_TEAM_ID)" ap2/tests/test_coverage_drift.py` returns a value `≤ 4` — the four comment-block rows are removed or trimmed (zero is the target; allowing up to 4 accommodates implementer choice to keep the audit comment but strike the per-knob enumeration). The contract is "the shim no longer carries these names as the ONLY reference."
- Prose claim (judge-verifiable against `git diff` + working tree): the new tests assert on actual call-site behavior — not just `os.environ.get` round-trips — for each of the four knobs. The judge can confirm by reading the new test bodies and checking that each test references the call-site module symbol (e.g. `daemon._build_task_options`, `janitor._judge`, `sandbox.install_channel` or equivalent) rather than just stub-asserting on env state.

## Out of scope

- The 8 event-type coverage-debt rows (L307-315 in `test_coverage_drift.py`) — separate follow-up deferred per this cycle's ideation_state.md.
- Tightening the substring drift gate to an AST-walk semantics check ("test imports the symbol AND asserts against it") — TB-208's docstring explicitly defers this until the substring gate is observed missing a real pro-forma gap.
- Adding new env knobs OR changing default values — pure test additions; no source-of-truth changes to the four knobs' contracts.
- Mattermost integration testing — `AP2_MM_TEAM_ID` test mocks the team-resolution path; does not require a live server.
- Refactoring `test_env_knobs.py`'s existing 17 TB-205 tests — leave untouched.
