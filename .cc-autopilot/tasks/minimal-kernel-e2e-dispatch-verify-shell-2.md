# Minimal-kernel e2e: dispatch → verify (shell) → report with every component disabled

## Goal

Pin the final unproven Progress signal of the **Current focus: get the component boundary right — loop-level participants only**: "a task dispatches → verifies (shell) → reports in that minimal-kernel config" (goal L234-236), backing the Done-when criterion that "the full test suite passes in the default configuration AND in an 'every component disabled' configuration" (goal L68-70). `ap2/tests/test_components_disabled.py` already smoke-tests core SURFACES (board parse, briefing validators, operator-queue drain, status-report compose, channel routing) under the all-components-disabled env, but nothing exercises a full daemon TICK — dispatch a Ready task, run the shell-bullet verifier, move it to Complete, emit the `task_verify` verdict — in that minimal kernel. Once TB-391 makes ideation a disable-able component, "every component disabled" includes ideation, so this e2e must run with ideation off too (TB-391 is recorded as a blocker on this task's codespan).

Why now: with the component-boundary axes landed (incl. TB-391's ideation extraction), the kernel is finally minimal; this is the moment to lock a regression-pin proving the dispatch-verify-report loop still works with zero components, so a future refactor can't silently break the walk-away promise in the minimal config.

## Scope

- Add `ap2/tests/e2e/test_minimal_kernel_tick.py`: build a fresh project, disable every env-flag-bearing component via the shared `enumerate_disabled_env_flags()` helper (imported from `ap2/tests/test_components_disabled.py`) plus set `AP2_IDEATION_DISABLED`, seed one Ready task whose briefing has a passing shell-bullet (e.g. `## Verification` with `test -f <path>` the stubbed agent creates), run one `daemon._tick` with the agent run stubbed/faked exactly as the existing e2e tests do (no real SDK), and assert the task lands in Complete with a `task_verify` event of `verdict=pass`.
- Reuse the existing e2e harness/fixtures (`ap2/tests/e2e/`) and the stubbed-adapter pattern from `test_single_tick.py` / `test_walk_away_loop.py` so the test is hermetic and needs no live SDK.
- Keep the disable list registry-driven (via the helper) so the test auto-tracks new components instead of hardcoding names.

## Design

Reuse `enumerate_disabled_env_flags(Registry.discover())` from `ap2/tests/test_components_disabled.py` to monkeypatch every env-flag-bearing component to its disabled polarity, then additionally set `AP2_IDEATION_DISABLED=1` (post-TB-391 it is a component kill switch the helper already covers, but set it explicitly so the test is robust to ordering), and `_reset_default_registry()` so the tick sees the disabled set. Follow `test_single_tick.py`'s harness: init a project, seed a Ready task + briefing with a trivially-passing shell `## Verification` bullet, monkeypatch the agent dispatch (`select_adapter`/`adapter.run`) to a fake that writes the expected artifact + commits, run one `await daemon._tick(...)`, then load the board and the events tail. Assert the task moved Ready→Complete and a `task_verify` event with `verdict=pass` was emitted. No production code changes — if the tick is found broken under the minimal kernel, that fix is a separate task.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/e2e/test_minimal_kernel_tick.py` — the new minimal-kernel e2e passes.
- `ap2/tests/e2e/test_minimal_kernel_tick.py` Prose: the test disables every component via `enumerate_disabled_env_flags()` (registry-driven, not hardcoded) plus `AP2_IDEATION_DISABLED`, runs one daemon tick with a stubbed agent, and asserts a Ready task dispatches, shell-verifies, and lands in Complete with a `task_verify verdict=pass` event; judge confirms via Read.

## Out of scope

- Any production-code change — this is a test-only regression pin (if the minimal-kernel tick is found broken, fixing it is a separate task).
- Real-SDK execution — the e2e stubs the agent like the existing tick e2es.
- Component behavior changes.