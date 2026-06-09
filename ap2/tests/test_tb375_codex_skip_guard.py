"""TB-375: kill green-by-skipping — fail the real-SDK smoke run when codex
variants skip despite codex being EXPECTED to run.

Two layers, both hermetic (NO live SDK, NO subprocess spawn):

  1. The pure guard verdict (`ap2.tests.smoke._codex_guard`): given faked
     credential/handle/skip presence, the guard FAILS (returns a marker detail)
     only when codex was expected AND a codex variant skipped; it is QUIET when
     codex is legitimately absent or when every codex variant ran. This is the
     run-level honesty rule — a single skipped codex variant is loud.

  2. The cron surface (`ap2.smoke_runner.run_smoke_check`): a non-zero pytest
     exit carrying the guard's `CODEX_SKIP_GUARD_MARKER` is treated as a DISTINCT
     smoke FAILURE (`smoke_check_codex_coverage_missing`), never a pass, and
     posts an alert naming the skipped codex coverage — while an ordinary
     non-zero exit (no marker) still emits the generic `smoke_check_failed`.
"""
from __future__ import annotations

import asyncio

from ap2 import smoke_runner
from ap2.smoke_runner import CODEX_SKIP_GUARD_MARKER
from ap2.tests.smoke import _codex_guard
from ap2.tests.test_smoke_runner import (
    _FakeCompleted,
    _events_of,
    _patch_mm_post,
    _patch_subprocess,
    _project,
)

# A representative codex variant nodeid (the `[codex]` tool-round-trip
# parametrization) and the standalone codex dispatch smoke's nodeid.
_CODEX_PARAM = (
    "ap2/tests/smoke/test_report_result_real_sdk.py::"
    "test_report_result_round_trip_via_adapter[codex]"
)
_CODEX_STANDALONE = (
    "ap2/tests/smoke/test_codex_real_sdk.py::"
    "test_codex_dispatch_round_trip_via_real_sdk"
)
_CLAUDE_PARAM = (
    "ap2/tests/smoke/test_report_result_real_sdk.py::"
    "test_report_result_round_trip_via_adapter[claude]"
)


# ---------------------------------------------------------------------------
# is_codex_variant / codex_variants_skipped — nodeid classification.
# ---------------------------------------------------------------------------


def test_is_codex_variant_matches_param_and_standalone_not_claude():
    assert _codex_guard.is_codex_variant(_CODEX_PARAM)
    assert _codex_guard.is_codex_variant(_CODEX_STANDALONE)
    # The claude parametrization must NEVER read as codex coverage.
    assert not _codex_guard.is_codex_variant(_CLAUDE_PARAM)
    assert not _codex_guard.is_codex_variant(
        "ap2/tests/smoke/test_prose_judge_real_sdk.py::test_judge"
    )


def test_codex_variants_skipped_filters_and_dedupes():
    skipped = [_CLAUDE_PARAM, _CODEX_PARAM, _CODEX_PARAM, _CODEX_STANDALONE]
    out = _codex_guard.codex_variants_skipped(skipped)
    assert out == [_CODEX_PARAM, _CODEX_STANDALONE]


# ---------------------------------------------------------------------------
# (a) guard FAILS when codex expected (creds+handle present) but a variant skipped.
# ---------------------------------------------------------------------------


def test_guard_fails_when_codex_expected_and_variant_skipped():
    detail = _codex_guard.evaluate_guard(
        skipped_nodeids=[_CODEX_PARAM],
        real_sdk=True,
        importable=True,
        creds=True,
    )
    assert detail is not None
    # The detail begins with the marker (so the cron can grep it) and names the
    # skipped coverage.
    assert detail.startswith(CODEX_SKIP_GUARD_MARKER)
    assert _CODEX_PARAM in detail


def test_guard_fails_for_standalone_codex_smoke_skip():
    detail = _codex_guard.evaluate_guard(
        skipped_nodeids=[_CLAUDE_PARAM, _CODEX_STANDALONE],
        real_sdk=True,
        importable=True,
        creds=True,
    )
    assert detail is not None
    assert _CODEX_STANDALONE in detail


# ---------------------------------------------------------------------------
# (b) guard PASSES (quiet) when the codex variants ran.
# ---------------------------------------------------------------------------


def test_guard_quiet_when_no_codex_variant_skipped():
    # Codex expected and ran — only a claude variant skipped (separate concern).
    assert (
        _codex_guard.evaluate_guard(
            skipped_nodeids=[_CLAUDE_PARAM],
            real_sdk=True,
            importable=True,
            creds=True,
        )
        is None
    )


def test_guard_quiet_when_nothing_skipped():
    assert (
        _codex_guard.evaluate_guard(
            skipped_nodeids=[],
            real_sdk=True,
            importable=True,
            creds=True,
        )
        is None
    )


# ---------------------------------------------------------------------------
# (c) guard QUIET when codex legitimately absent (any presence signal missing),
#     even if a codex variant skipped.
# ---------------------------------------------------------------------------


def test_guard_quiet_when_credential_absent():
    assert (
        _codex_guard.evaluate_guard(
            skipped_nodeids=[_CODEX_PARAM],
            real_sdk=True,
            importable=True,
            creds=False,
        )
        is None
    )


def test_guard_quiet_when_handle_absent():
    assert (
        _codex_guard.evaluate_guard(
            skipped_nodeids=[_CODEX_PARAM],
            real_sdk=True,
            importable=False,
            creds=True,
        )
        is None
    )


def test_guard_quiet_when_real_sdk_unset():
    # AP2_REAL_SDK unset → the module-level skipif skips EVERY variant; the guard
    # must not fire on a non-opted-in box.
    assert (
        _codex_guard.evaluate_guard(
            skipped_nodeids=[_CODEX_PARAM, _CODEX_STANDALONE],
            real_sdk=False,
            importable=True,
            creds=True,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Presence-signal plumbing: env, importability, and credential reuse.
# ---------------------------------------------------------------------------


def test_real_sdk_set_polarity():
    assert _codex_guard.real_sdk_set({"AP2_REAL_SDK": "1"})
    assert not _codex_guard.real_sdk_set({})
    assert not _codex_guard.real_sdk_set({"AP2_REAL_SDK": ""})
    assert not _codex_guard.real_sdk_set({"AP2_REAL_SDK": "   "})


def test_credentials_present_delegates_to_auth_gate_helper(monkeypatch):
    """The guard MUST reuse the daemon-start auth gate's presence check — one
    source of truth for 'a codex credential is present'."""
    import ap2.cli_daemon as cli_daemon

    calls = {"n": 0}

    def _fake_present() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(cli_daemon, "_codex_credentials_present", _fake_present)
    assert _codex_guard.credentials_present() is True
    assert calls["n"] == 1


def test_evaluate_guard_from_env_wires_live_signals(monkeypatch):
    """`evaluate_guard_from_env` reads the three presence signals from the live
    env / handle / auth-gate helper."""
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setattr(_codex_guard, "codex_importable", lambda: True)
    monkeypatch.setattr(_codex_guard, "credentials_present", lambda: True)

    detail = _codex_guard.evaluate_guard_from_env([_CODEX_PARAM])
    assert detail is not None and _CODEX_PARAM in detail

    # Flip credential presence off → quiet, even with the codex variant skipped.
    monkeypatch.setattr(_codex_guard, "credentials_present", lambda: False)
    assert _codex_guard.evaluate_guard_from_env([_CODEX_PARAM]) is None


# ---------------------------------------------------------------------------
# Cron surface: run_smoke_check treats the marker as a DISTINCT smoke FAILURE.
# ---------------------------------------------------------------------------


def test_run_smoke_check_emits_distinct_event_on_codex_skip_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")
    cfg = _project(tmp_path)
    # The guard forces a non-zero exit AND prints the marker line.
    marker_line = (
        f"{CODEX_SKIP_GUARD_MARKER}: codex was expected to run but these codex "
        f"smoke variant(s) reported skipped: {_CODEX_PARAM}"
    )
    pytest_tail = (
        f"{marker_line}\n"
        "4 passed, 2 skipped in 41.0s\n"
    )
    _patch_subprocess(monkeypatch, result=_FakeCompleted(1, stdout=pytest_tail))
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))
    # TB-389: the alert is enqueued; the communication tick delivers it.
    from ap2.components.communication import run_outbound_tick
    run_outbound_tick(cfg)

    # DISTINCT alarm event — NOT smoke_check_passed, NOT the generic failed.
    missing = _events_of(cfg, "smoke_check_codex_coverage_missing")
    assert len(missing) == 1
    assert missing[0]["reason"] == "codex_expected_but_skipped"
    assert _CODEX_PARAM in missing[0]["skipped_coverage"]
    assert missing[0]["exit_code"] == 1
    assert _events_of(cfg, "smoke_check_passed") == []
    assert _events_of(cfg, "smoke_check_failed") == []

    # Exactly one alert, naming the skipped codex coverage.
    assert len(posts) == 1
    assert _CODEX_PARAM in posts[0]["text"]
    assert "codex" in posts[0]["text"].lower()


def test_run_smoke_check_ordinary_failure_unaffected_by_guard(tmp_path, monkeypatch):
    """A non-zero exit WITHOUT the marker is still the generic
    `smoke_check_failed` — the codex guard doesn't swallow real failures."""
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")
    cfg = _project(tmp_path)
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompleted(
            1, stdout="FAILED ...::test_judge - AssertionError\n1 failed\n"
        ),
    )
    _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))

    assert _events_of(cfg, "smoke_check_codex_coverage_missing") == []
    assert len(_events_of(cfg, "smoke_check_failed")) == 1


def test_run_smoke_check_pass_unaffected_when_no_marker(tmp_path, monkeypatch):
    """A clean exit-0 run (no marker) still passes — the guard only fires on the
    marker, so a legitimately-absent-codex box is unaffected."""
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")
    cfg = _project(tmp_path)
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompleted(0, stdout="3 passed, 2 skipped in 30.0s\n"),
    )
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))

    assert len(_events_of(cfg, "smoke_check_passed")) == 1
    assert _events_of(cfg, "smoke_check_codex_coverage_missing") == []
    assert posts == []


# ---------------------------------------------------------------------------
# conftest plugin wiring: skipped-report accumulation + sessionfinish exit code.
# ---------------------------------------------------------------------------


class _FakeReport:
    def __init__(self, nodeid: str, skipped: bool):
        self.nodeid = nodeid
        self.skipped = skipped


class _FakeSession:
    def __init__(self):
        self.exitstatus = 0  # ExitCode.OK


def test_conftest_plugin_fails_session_and_prints_marker(monkeypatch, capsys):
    """The real conftest plugin accumulates skipped nodeids, hands them to the
    guard, and — when the guard fires — prints the marker line AND forces a
    non-zero session exit so the cron sees a FAILURE."""
    import pytest

    from ap2.tests.smoke import conftest as smoke_conftest

    seen: dict = {}

    def _fake_guard(skipped_nodeids):
        seen["nodeids"] = list(skipped_nodeids)
        return f"{CODEX_SKIP_GUARD_MARKER}: boom {_CODEX_PARAM}"

    monkeypatch.setattr(smoke_conftest, "evaluate_guard_from_env", _fake_guard)

    plugin = smoke_conftest._CodexCoverageGuard()
    # A skipped codex report + a skipped claude report + a passing report; only
    # skipped nodeids accumulate, de-duplicated.
    plugin.pytest_runtest_logreport(_FakeReport(_CODEX_PARAM, skipped=True))
    plugin.pytest_runtest_logreport(_FakeReport(_CODEX_PARAM, skipped=True))
    plugin.pytest_runtest_logreport(_FakeReport(_CLAUDE_PARAM, skipped=True))
    plugin.pytest_runtest_logreport(_FakeReport("x::passes", skipped=False))

    session = _FakeSession()
    plugin.pytest_sessionfinish(session, 0)

    # The guard saw exactly the skipped nodeids (deduped), and the run failed.
    assert seen["nodeids"] == [_CODEX_PARAM, _CLAUDE_PARAM]
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert CODEX_SKIP_GUARD_MARKER in capsys.readouterr().out


def test_conftest_plugin_quiet_leaves_exitstatus(monkeypatch, capsys):
    """When the guard is quiet (codex absent / variants ran), the plugin leaves
    the session exit status untouched and prints no marker."""
    from ap2.tests.smoke import conftest as smoke_conftest

    monkeypatch.setattr(
        smoke_conftest, "evaluate_guard_from_env", lambda nodeids: None
    )

    plugin = smoke_conftest._CodexCoverageGuard()
    plugin.pytest_runtest_logreport(_FakeReport(_CODEX_PARAM, skipped=True))

    session = _FakeSession()
    plugin.pytest_sessionfinish(session, 0)

    assert session.exitstatus == 0
    assert CODEX_SKIP_GUARD_MARKER not in capsys.readouterr().out
