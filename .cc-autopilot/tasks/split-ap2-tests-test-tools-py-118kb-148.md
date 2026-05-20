# Split `ap2/tests/test_tools.py` (118KB / 148 tests) to mirror the TB-262 source split into validator/judge/queue/board modules

Tags: #autopilot #refactor #tests #modularity #agent-friendliness #regression-pin

## Goal

`ap2/tests/test_tools.py` is 118KB / 148 test functions and now structurally mismatched with its source. TB-262 split `ap2/tools.py` into `briefing_validators.py` / `validator_judge.py` / `operator_queue.py` / `board_edits.py` (plus the remaining MCP-dispatch `tools.py`), but the test file stayed monolithic. An agent touching one surface (e.g. briefing validators) loads all 118KB of tool tests, and test-location no longer maps to source-location.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Oversized test files inflate context load on any tools-touching task → longer agent runs → higher verify-timeout / max-turns exposure (the TB-247 / TB-250 / TB-255 retry-exhaustion class). Mirroring the source split restores the test-location-to-source-location map.

Why now: pairs directly with TB-262 (tools.py split, already complete). The test file should mirror the source structure that just landed; the `@blocked:TB-262` codespan records that source dependency (already satisfied since TB-262 shipped).

## Scope

- Split `ap2/tests/test_tools.py` into sibling test modules mirroring the TB-262 source split, at the flat `ap2/tests/` level:
  - `ap2/tests/test_briefing_validators.py` — tests for `_validate_briefing_structure` + goal-anchor / Why-now / Manual-bullet validation + section regexes.
  - `ap2/tests/test_validator_judge.py` — tests for `_judge_dep_coherence_default` + dep-coherence + validator-judge response parsing.
  - `ap2/tests/test_operator_queue.py` — tests for `do_operator_queue_append` + drain helpers.
  - `ap2/tests/test_board_edits.py` — tests for `do_board_edit` + helpers.
  - `ap2/tests/test_tools.py` (remains) — MCP tool dispatch / registration tests + anything not tied to the above surfaces.
- Move each test to the module mirroring the source surface it exercises. (Note: some existing test_tb<N>_*.py modules already cover specific validator/judge behaviors — leave those alone; this task only re-homes tests currently inside the monolithic test_tools.py.)
- Carry shared fixtures / helpers: cross-module fixtures move to `ap2/tests/conftest.py`; single-use ones move with their module.

## Design

- Flat structure only — sibling test modules at `ap2/tests/`, NO `ap2/tests/tools/` subpackage.
- Mirror the source split — test module names map 1:1 to the TB-262 source modules.
- Pure mechanical move — NO test logic changes, NO new/removed/renamed tests. Each test lands in exactly one new home with identical body.
- Shared fixtures go to `conftest.py` rather than cross-test-module imports.
- Imports travel with their tests.
- Do NOT touch the existing standalone `test_tb<N>_*.py` regression-pin modules — they already live at the right granularity; this task only re-homes the monolithic `test_tools.py` body.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes.
- `[ "$(uv run pytest ap2/tests/ --collect-only -q 2>/dev/null | grep -oE '[0-9]+ tests collected' | grep -oE '^[0-9]+')" -ge 1868 ]` — collected test count is non-decreasing (no tests silently dropped; the critical safety net).
- `wc -c ap2/tests/test_tools.py | awk '$1 < 50000 { exit 0 } { exit 1 }'` — `test_tools.py` reduced to under 50KB after the split.
- `ls ap2/tests/test_briefing_validators.py ap2/tests/test_validator_judge.py ap2/tests/test_operator_queue.py 2>/dev/null | wc -l | awk '$1 >= 3 { exit 0 } { exit 1 }'` — at minimum three of the mirror test modules exist.
- Prose: each new test module contains tests for the surface owned by the matching TB-262 source module, and no test logic was changed (only relocated). The judge confirms by reading a sample of the new modules and checking the test bodies match relocated originals.

## Out of scope

- Subpackage creation (`ap2/tests/tools/`) — flat-structure principle.
- Adding, removing, renaming, or rewriting any test — pure relocation.
- Touching the existing standalone `test_tb<N>_*.py` regression-pin modules — they stay as-is.
- Changing `tools.py` or the TB-262 source modules — tests-only task.
- Splitting `test_cli.py` / `test_web.py` — separate TBs in this batch.
- Refactoring shared test helpers beyond moving them to conftest.py — mechanical move only.
