# Split `ap2/tests/test_cli.py` (133KB / 132 tests) to mirror the TB-264 cli-prefixed source split

Tags: #autopilot #refactor #tests #modularity #agent-friendliness #regression-pin

## Goal

`ap2/tests/test_cli.py` is 133KB / 132 test functions — the largest test file, and now structurally mismatched with its source. TB-264 split `ap2/cli.py` into `cli_daemon.py` / `cli_board.py` / `cli_review.py` / `cli_diagnostic.py`, but the test file stayed monolithic. An agent touching one CLI verb group has to load all 133KB of tests to find the relevant coverage, and the test-location no longer maps to source-location.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Oversized test files inflate context load on any test-touching task → longer agent runs → higher verify-timeout / max-turns exposure (the TB-247 / TB-250 / TB-255 retry-exhaustion class). Mirroring the source split restores the test-location-to-source-location map so future CLI tasks load only the relevant test module.

Why now: pairs directly with TB-264 (cli.py split, already shipped). The test file should mirror the source structure that just landed; deferring leaves a confusing mismatch where `cli_board.py` exists but its tests live in a 133KB grab-bag.

## Scope

- Split `ap2/tests/test_cli.py` into sibling test modules mirroring the TB-264 source split, at the flat `ap2/tests/` level:
  - `ap2/tests/test_cli_daemon.py` — tests for `cmd_start` / `cmd_stop` / `cmd_status` / `cmd_pause` / `cmd_resume` / `cmd_web`.
  - `ap2/tests/test_cli_board.py` — tests for `cmd_add` / `cmd_update` / `cmd_backlog` / `cmd_unfreeze` / `cmd_delete` / `cmd_reject` / `cmd_approve` / `cmd_classify`.
  - `ap2/tests/test_cli_review.py` — tests for `cmd_audit` / `cmd_ack` / `cmd_rollback` / `cmd_ideate` / `cmd_update_goal` / `cmd_backfill_proposals`.
  - `ap2/tests/test_cli_diagnostic.py` — tests for `cmd_doctor` / `cmd_check` / `cmd_logs` / `cmd_cron_list` / `cmd_cron_edit` / `cmd_init`.
  - `ap2/tests/test_cli.py` (remains, if anything) — argparse-layer wiring tests + anything not tied to a single verb group.
- Move each test function to the module mirroring the verb it exercises (the existing section-divider comments in test_cli.py already group tests this way — use them as the seam).
- Carry shared fixtures / helpers: if a fixture is used by tests landing in multiple new modules, move it to `ap2/tests/conftest.py`; if used by only one, move it with that module.

## Design

- Flat structure only — sibling test modules at `ap2/tests/`, NO `ap2/tests/cli/` subpackage.
- Mirror the source split exactly — test module names map 1:1 to the cli-prefixed source modules so an agent touching `cli_board.py` knows to look in `test_cli_board.py`.
- Pure mechanical move — NO test logic changes, NO new tests, NO removed tests, NO renamed test functions. Every test function lands in exactly one new home with identical body.
- Shared fixtures go to `conftest.py` (pytest auto-discovers it) rather than being imported across test modules — avoids cross-test-module import coupling.
- Imports travel with their tests — each new module imports exactly what its tests use.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes.
- `[ "$(uv run pytest ap2/tests/ --collect-only -q 2>/dev/null | grep -oE '[0-9]+ tests collected' | grep -oE '^[0-9]+')" -ge 1868 ]` — collected test count is non-decreasing (no tests silently dropped in the move; the critical safety net, since a dropped test leaves the suite green with fewer tests).
- `wc -c ap2/tests/test_cli.py | awk '$1 < 40000 { exit 0 } { exit 1 }'` — `test_cli.py` reduced to under 40KB after the split.
- `ls ap2/tests/test_cli_daemon.py ap2/tests/test_cli_board.py ap2/tests/test_cli_review.py 2>/dev/null | wc -l | awk '$1 >= 3 { exit 0 } { exit 1 }'` — at minimum three of the mirror test modules exist.
- Prose: each new cli-prefixed test module contains tests for the verbs owned by the matching cli-prefixed source module, and no test logic was changed (only relocated). The judge confirms by reading a sample of the new modules and checking the test bodies match relocated originals, not rewrites.

## Out of scope

- Subpackage creation (`ap2/tests/cli/`) — flat-structure principle.
- Adding, removing, renaming, or rewriting any test — pure relocation.
- Changing `cli.py` or the cli-prefixed source modules — tests-only task.
- Splitting `test_web.py` / `test_tools.py` — separate TBs in this batch.
- Refactoring shared test helpers beyond moving them to conftest.py — mechanical move only.
