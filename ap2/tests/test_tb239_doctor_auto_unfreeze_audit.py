"""TB-239: `ap2 doctor` warns when `AP2_AUTO_UNFREEZE_DRY_RUN=1` is set
but `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty (axis-2 misconfiguration
floor).

Symmetric mirror of TB-234's `auto_approve_audit()`. `_maybe_auto_unfreeze`
(daemon.py:3301-3303) silently early-returns when
`AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty — EVEN when
`AP2_AUTO_UNFREEZE_DRY_RUN=1` is set, because the allowlist gate fires
BEFORE the dry-run check at daemon.py:3416. So an operator who flips the
dry-run knob expecting observation gets a silent no-op: zero
`would_auto_unfreeze` events, zero `auto_unfreeze_skipped` events, no
doctor warning. The doctor surface is the natural pre-flight place to make
that misconfiguration loud — WARN, not FAIL, per goal.md L184-186
(operator authority preserved).

Asymmetry vs TB-234 is intentional: axis-1 defaults are permissive (caps
default to 0 = disabled = unbounded), axis-2 defaults are conservative
(allowlist defaults to empty = no-op). The misconfiguration shapes
differ; the doctor warnings reflect that.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import doctor as doctor_mod
from ap2.doctor import auto_unfreeze_audit, diagnose


UNFREEZE_ENVS = (
    "AP2_AUTO_UNFREEZE_FIX_SHAPES",
    "AP2_AUTO_UNFREEZE_DRY_RUN",
    "AP2_AUTO_UNFREEZE_MAX_PER_TASK",
    "AP2_AUTO_UNFREEZE_MAX_PER_DAY",
)


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in UNFREEZE_ENVS:
        monkeypatch.delenv(k, raising=False)


def _levels(res) -> list[str]:
    return [lvl for lvl, _ in res.messages]


def _texts(res) -> list[str]:
    return [txt for _, txt in res.messages]


# ===========================================================================
# Branch 1: default-off — no allowlist, no dry-run → single INFO line.
# ===========================================================================


def test_default_off_emits_info_only(monkeypatch: pytest.MonkeyPatch):
    """No env knobs set → feature is fully off, audit emits one INFO line
    and no WARN. Most operators see this on a fresh setup; the WARN
    surface should stay quiet until they opt in."""
    _clean_env(monkeypatch)
    res = auto_unfreeze_audit()
    assert _levels(res) == ["INFO"]
    msg = res.messages[0][1]
    assert "disabled" in msg.lower()
    assert "allowlist" in msg.lower()
    assert "AP2_AUTO_UNFREEZE_FIX_SHAPES" in msg
    # No WARN: an operator who hasn't opted in shouldn't see a
    # misconfiguration nudge.
    assert "WARN" not in _levels(res)
    assert res.ok


# ===========================================================================
# Branch 2: dry-run set without allowlist — silent-no-op misconfiguration
# → WARN. This is the headline case TB-239 exists for.
# ===========================================================================


def test_dry_run_without_allowlist_emits_silent_no_op_warn(
    monkeypatch: pytest.MonkeyPatch,
):
    """The exact misconfiguration shape: operator flips
    `AP2_AUTO_UNFREEZE_DRY_RUN=1` expecting to observe auto-unfreeze
    decisions, but the allowlist gate at daemon.py:3301-3303 early-
    returns silently. Doctor surfaces the silent-no-op as a WARN that
    names the location and the fix."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    res = auto_unfreeze_audit()
    levels = _levels(res)
    assert levels.count("WARN") == 1, levels
    warn_msg = next(t for lvl, t in res.messages if lvl == "WARN")
    # WARN body names the misconfiguration shape ("silent no-op"), the
    # daemon early-return location, and the fix env var.
    assert "silent no-op" in warn_msg
    assert "ap2/daemon.py:3301-3303" in warn_msg
    assert "AP2_AUTO_UNFREEZE_FIX_SHAPES" in warn_msg
    # WARN doesn't FAIL the report (operator authority preserved per
    # goal.md L184-186).
    assert res.ok


@pytest.mark.parametrize("val", ["1", "true", "True", "YES", "yes"])
def test_dry_run_truthy_parse_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, val: str,
):
    """Truthy parse mirrors the daemon's `_auto_unfreeze_dry_run()`
    contract: `1` / `true` / `yes`, case-insensitive. The WARN fires
    on any of them when the allowlist is unset."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", val)
    res = auto_unfreeze_audit()
    assert _levels(res).count("WARN") == 1, (val, _levels(res))


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_dry_run_falsy_values_do_not_trigger_warn(
    monkeypatch: pytest.MonkeyPatch, val: str,
):
    """Falsy / garbage / empty `AP2_AUTO_UNFREEZE_DRY_RUN` with no
    allowlist parses as the default-off case — INFO only, no WARN."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", val)
    res = auto_unfreeze_audit()
    levels = _levels(res)
    assert "WARN" not in levels, (val, levels)
    assert levels == ["INFO"]


def test_dry_run_without_allowlist_empty_shapes_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    """Whitespace-only / comma-only allowlist values parse as empty
    (mirror of `_auto_unfreeze_allowlist`'s split-and-strip shape).
    The WARN still fires."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_FIX_SHAPES", "  ,  ,  ")
    res = auto_unfreeze_audit()
    assert _levels(res).count("WARN") == 1, _levels(res)


# ===========================================================================
# Branch 3: dry-run armed correctly — allowlist non-empty + dry-run set
# → INFO summary naming the shape count + caps.
# ===========================================================================


def test_dry_run_armed_with_allowlist_emits_info_summary(
    monkeypatch: pytest.MonkeyPatch,
):
    """Correctly-configured dry-run on-ramp: allowlist non-empty AND
    dry-run set → operator gets an INFO confirming the configuration
    is the intended observability state, with the shape count + caps
    so they can sanity-check the bounds before flipping the knob off."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir,literal_backtick_in_shell_bullet",
    )
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    res = auto_unfreeze_audit()
    levels = _levels(res)
    assert levels == ["INFO"], levels
    msg = res.messages[0][1]
    assert "dry-run armed" in msg
    assert "2 shapes" in msg
    # Defaults: per-task=1, per-day=3.
    assert "per-task cap 1" in msg
    assert "per-day cap 3" in msg
    assert res.ok


def test_dry_run_armed_honors_custom_caps(
    monkeypatch: pytest.MonkeyPatch,
):
    """Custom cap values are echoed in the INFO summary so the operator
    sees the same numbers the daemon will enforce."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "5")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "10")
    res = auto_unfreeze_audit()
    msg = res.messages[0][1]
    assert "1 shapes" in msg
    assert "per-task cap 5" in msg
    assert "per-day cap 10" in msg


# ===========================================================================
# Branch 4: live mode — allowlist set, dry-run unset → INFO summary.
# ===========================================================================


def test_live_mode_with_allowlist_emits_info_summary(
    monkeypatch: pytest.MonkeyPatch,
):
    """Production state: allowlist populated, dry-run unset. INFO line
    names the live mode + cap values so the operator can confirm the
    production configuration."""
    _clean_env(monkeypatch)
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir,literal_backtick_in_shell_bullet,"
        "missing_bang_on_absence_check",
    )
    res = auto_unfreeze_audit()
    levels = _levels(res)
    assert levels == ["INFO"], levels
    msg = res.messages[0][1]
    assert "live" in msg
    assert "dry-run" not in msg  # not the dry-run-armed branch
    assert "3 shapes" in msg
    assert "per-task cap 1" in msg
    assert "per-day cap 3" in msg
    assert res.ok


# ===========================================================================
# diagnose() wiring: end-to-end the section title + ordering.
# ===========================================================================


def test_diagnose_includes_auto_unfreeze_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
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
    assert "auto-unfreeze safety floor" in titles


def test_diagnose_section_ordering_pairs_axis1_and_axis2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Axis-pairing preserved: the auto-unfreeze safety floor section
    appears directly after the auto-approve safety floor section so an
    operator scanning `ap2 doctor` output sees the misconfiguration
    floors as a paired unit (design point from the TB-239 briefing)."""
    _clean_env(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_user_exists", lambda u: False)
    monkeypatch.setattr(
        doctor_mod, "_sandbox_clone_path", lambda root, user: None
    )

    report = diagnose(tmp_path, user="ghost")
    titles = [t for t, _ in report.sections]
    aa_idx = titles.index("auto-approve safety floor")
    au_idx = titles.index("auto-unfreeze safety floor")
    assert au_idx == aa_idx + 1, (
        f"auto-unfreeze should be directly after auto-approve; got "
        f"titles={titles}"
    )
