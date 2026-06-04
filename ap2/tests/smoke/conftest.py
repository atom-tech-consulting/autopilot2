"""Session-scoped codex-coverage guard for the real-SDK smoke harness (TB-375).

Kills "green-by-skipping": when codex was EXPECTED to run (`AP2_REAL_SDK` set,
`openai_codex` importable, a codex credential present) but a codex-parametrized
smoke variant nonetheless reported `skipped`, this conftest fails the WHOLE
smoke run — a single skipped codex variant is loud, run-level, so partial silent
coverage erosion can't accumulate behind a passing 6h cron. When codex is
legitimately absent (handle not installed or no codex credential), the codex
variants skip as designed and this guard stays quiet, so a Claude-only box still
passes.

The decision logic lives in `_codex_guard` (import-only, hermetically tested);
this file only wires it to pytest:

  - A small plugin accumulates the nodeids of every report that reported
    `skipped` over the run.
  - At `pytest_sessionfinish`, it asks `_codex_guard.evaluate_guard_from_env`
    whether codex was expected-but-skipped. If so it prints the
    `CODEX_SKIP_GUARD_MARKER` detail line (which `ap2.smoke_runner.run_smoke_check`
    greps to emit its distinct `smoke_check_codex_coverage_missing` alarm) and
    forces a non-zero session exit, so the cron sees a FAILURE, not a pass.

This guard operates at the RUN level on the codex-expected condition; it does
NOT change the per-test `call_with_transient_retry` semantics for a genuinely
transient single-call hiccup — that retry-then-skip stays as is.
"""
from __future__ import annotations

import pytest

from ._codex_guard import evaluate_guard_from_env


class _CodexCoverageGuard:
    """Session plugin: collect skipped nodeids, fail the run on expected-but-
    skipped codex coverage."""

    def __init__(self) -> None:
        self._skipped_nodeids: list[str] = []

    def pytest_runtest_logreport(self, report) -> None:
        # A skipif marker skips at `setup`; an in-test `pytest.skip()` (e.g. the
        # codex `importorskip` / the transient-retry skip) skips at `call`.
        # Either way `report.skipped` is the run-level signal we want.
        if report.skipped and report.nodeid not in self._skipped_nodeids:
            self._skipped_nodeids.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus) -> None:  # noqa: ARG002
        detail = evaluate_guard_from_env(self._skipped_nodeids)
        if detail is None:
            # Codex legitimately absent, or every codex variant actually ran —
            # leave the session exit status untouched (a Claude-only box passes).
            return
        # Print the marker line so the cron's `run_smoke_check` can recognize
        # this as missing-codex-coverage (not an ordinary test failure) and
        # force a non-zero exit so the run is a FAILURE, never a pass.
        print(detail)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config) -> None:
    config.pluginmanager.register(_CodexCoverageGuard(), "ap2_codex_coverage_guard")
