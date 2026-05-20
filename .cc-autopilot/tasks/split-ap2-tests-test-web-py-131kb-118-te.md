# Split `ap2/tests/test_web.py` (131KB / 118 tests) to mirror the TB-265 web-prefixed route-group source split

Tags: #autopilot #refactor #tests #modularity #agent-friendliness #regression-pin

## Goal

`ap2/tests/test_web.py` is 131KB / 118 test functions and now structurally mismatched with its source. TB-265 split `ap2/web.py` into route-group modules (`web_home.py` / `web_events.py` / `web_tasks.py` / `web_stats.py` / `web_insights.py` / `web_chrome.py` / `web_usage.py`), but the test file stayed monolithic. An agent touching one route group loads all 131KB of web tests to find relevant coverage, and test-location no longer maps to source-location.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Oversized test files inflate context load on web-touching tasks → longer agent runs → higher verify-timeout / max-turns exposure. Mirroring the source split restores the test-location-to-source-location map.

Why now: pairs directly with TB-265 (web.py route-group split, already complete). The test file should mirror the source structure that just landed; the `@blocked:TB-265` codespan records that source dependency (already satisfied since TB-265 shipped).

## Scope

- Split `ap2/tests/test_web.py` into sibling test modules mirroring the TB-265 route-group source split, at the flat `ap2/tests/` level. Target one test module per route-group source module that has meaningful coverage:
  - `ap2/tests/test_web_home.py` — home page + env-stale warning rendering (TB-260/TB-265).
  - `ap2/tests/test_web_events.py` — `/events` page + JSON sub-endpoint.
  - `ap2/tests/test_web_tasks.py` — `/task-run/<id>` page + stream JSON (TB-129).
  - `ap2/tests/test_web_stats.py` — `/stats` + `/stats.json` (TB-255).
  - `ap2/tests/test_web_insights.py` — insight pages.
  - Additional mirror modules (`test_web_chrome.py`, `test_web_usage.py`) as the existing test coverage warrants.
  - `ap2/tests/test_web.py` (remains, if anything) — app-construction / middleware / router-composition tests not tied to a single route group.
- Move each test to the module mirroring the route group it exercises.
- Carry shared fixtures (test client construction, sample-state setup): if used across multiple new modules, move to `ap2/tests/conftest.py`; if single-use, move with that module.

## Design

- Flat structure only — sibling test modules at `ap2/tests/`, NO `ap2/tests/web/` subpackage.
- Mirror the source split — test module names map 1:1 to the web-prefixed route-group source modules.
- Pure mechanical move — NO test logic changes, NO new/removed/renamed tests. Each test lands in exactly one new home with identical body.
- The FastAPI test-client fixture is almost certainly shared across route-group tests — move it to `conftest.py` so all new modules get it via pytest auto-discovery rather than cross-importing.
- Imports travel with their tests.
- If a single route-group source module has only 1-2 tests, it's fine to land them in the nearest sibling rather than create a near-empty module — favor coherent grouping over rigid 1:1 when coverage is sparse.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes.
- `[ "$(uv run pytest ap2/tests/ --collect-only -q 2>/dev/null | grep -oE '[0-9]+ tests collected' | grep -oE '^[0-9]+')" -ge 1868 ]` — collected test count is non-decreasing (no tests silently dropped; the critical safety net).
- `wc -c ap2/tests/test_web.py | awk '$1 < 40000 { exit 0 } { exit 1 }'` — `test_web.py` reduced to under 40KB after the split.
- `ls ap2/tests/test_web_home.py ap2/tests/test_web_events.py ap2/tests/test_web_stats.py 2>/dev/null | wc -l | awk '$1 >= 3 { exit 0 } { exit 1 }'` — at minimum three of the mirror test modules exist.
- Prose: each new web-prefixed test module contains tests for the route group owned by the matching web-prefixed source module, and no test logic was changed (only relocated). The judge confirms by reading a sample of the new modules.
- Prose: TB-260's env-stale-warning web rendering test (the test that TB-265's retry restored) lands in the home-page test module and still passes. The judge confirms by reading the relocated test.

## Out of scope

- Subpackage creation (`ap2/tests/web/`) — flat-structure principle.
- Adding, removing, renaming, or rewriting any test — pure relocation.
- Changing `web.py` or the web-prefixed route-group source modules — tests-only task.
- Splitting `test_cli.py` / `test_tools.py` — separate TBs in this batch.
- Refactoring shared test helpers beyond moving them to conftest.py — mechanical move only.
