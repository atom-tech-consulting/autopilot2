"""TB-234: `ap2 doctor` warns when `AP2_AUTO_APPROVE=1` is set but token
caps are unset (axis-3 misconfiguration-floor).

`_per_task_token_cap` / `_window_token_cap` (daemon.py:2581-2614)
deliberately return 0 ("disabled") on unset by design, so enabling
auto-approve without setting the caps leaves the cost ceiling OFF
silently. The doctor surface is the natural pre-flight place to make
that misconfiguration loud — WARN, not FAIL, per goal.md L184-186
(operator authority preserved).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import doctor as doctor_mod
from ap2.doctor import auto_approve_audit, diagnose


CAP_ENVS = (
    "AP2_AUTO_APPROVE",
    "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
    "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP",
)


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in CAP_ENVS:
        monkeypatch.delenv(k, raising=False)


def _levels(res) -> list[str]:
    return [lvl for lvl, _ in res.messages]


def _texts(res) -> list[str]:
    return [txt for _, txt in res.messages]


def test_auto_approve_unset_emits_info_only(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch)
    res = auto_approve_audit()
    assert _levels(res) == ["INFO"]
    assert "AP2_AUTO_APPROVE" in res.messages[0][1]
    assert "disabled" in res.messages[0][1].lower()
    # No WARN: an operator who hasn't opted in shouldn't see a misconfiguration nudge.
    assert "WARN" not in _levels(res)
    assert res.ok


def test_auto_approve_on_with_no_caps_emits_three_warns(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    res = auto_approve_audit()
    levels = _levels(res)
    assert levels.count("WARN") == 3, levels
    texts = " ".join(_texts(res))
    assert "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP" in texts
    assert "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP" in texts
    assert "safety floor OFF" in texts
    assert "goal.md L102-113" in texts
    # WARN doesn't FAIL the report (operator authority preserved).
    assert res.ok


def test_auto_approve_on_with_only_per_task_cap_set(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "500000")
    res = auto_approve_audit()
    levels = _levels(res)
    assert levels.count("OK") == 1
    assert levels.count("WARN") == 1
    # The single OK names the per-task cap; the single WARN names the window cap.
    ok_msg = next(t for lvl, t in res.messages if lvl == "OK")
    warn_msg = next(t for lvl, t in res.messages if lvl == "WARN")
    assert "PER_TASK_TOKEN_CAP" in ok_msg
    assert "500000" in ok_msg
    assert "WINDOW_TOKEN_CAP" in warn_msg
    # No summary WARN when at least one cap is set.
    assert "safety floor OFF" not in " ".join(_texts(res))
    assert res.ok


def test_auto_approve_on_with_both_caps_set_is_ok(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "500000")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "5000000")
    res = auto_approve_audit()
    levels = _levels(res)
    assert levels.count("OK") == 2
    assert "WARN" not in levels
    assert "safety floor OFF" not in " ".join(_texts(res))
    assert res.ok


@pytest.mark.parametrize("val", ["1", "true", "True", "YES", "yes"])
def test_auto_approve_truthy_parse_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, val: str
):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", val)
    res = auto_approve_audit()
    # Truthy value with no caps → 3 WARN lines (per-task, window, summary).
    assert _levels(res).count("WARN") == 3, (val, _levels(res))


def test_cap_value_zero_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    """Mirrors `_per_task_token_cap`'s `v > 0 else 0` shape
    (daemon.py:2596 / 2614): an explicit "0" is the documented disable.
    """
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "0")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "0")
    res = auto_approve_audit()
    levels = _levels(res)
    # "0" caps look like unset → 3 WARN, no OK.
    assert levels.count("WARN") == 3, levels
    assert "OK" not in levels


def test_cap_value_non_integer_treated_as_unset(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "not-an-int")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "5000000")
    res = auto_approve_audit()
    levels = _levels(res)
    # non-int per-task cap → WARN; valid window cap → OK; no summary WARN
    # (window cap is set, so not "both unset").
    assert levels.count("WARN") == 1
    assert levels.count("OK") == 1


def test_diagnose_includes_auto_approve_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """End-to-end: diagnose() assembles the new section under the
    expected title so `ap2 doctor` prints it."""
    _clean_env(monkeypatch)
    # Stub out sandbox-user probes so we don't depend on the real machine.
    monkeypatch.setattr(doctor_mod, "_user_exists", lambda u: False)
    monkeypatch.setattr(
        doctor_mod, "_sandbox_clone_path", lambda root, user: None
    )

    report = diagnose(tmp_path, user="ghost")
    titles = [t for t, _ in report.sections]
    assert "auto-approve safety floor" in titles
