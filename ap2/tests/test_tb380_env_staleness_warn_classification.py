"""TB-380: the stale-env WARN on `ap2 status` must mean "a restart is
actually required" and must not mislabel the env-load timestamp as the
daemon's start time.

Two bugs observed live 2026-06-08:

  1. Editing a hot-reloadable knob (`AP2_IDEATION_DISABLED`, in
     `env_reload.HOT_RELOADABLE_KNOBS`) triggered the "restart to apply"
     WARN — but that knob applies on the next tick's `env_reload`; no
     restart is needed. Telling the operator to restart for a
     hot-reloadable edit is a false alarm that would needlessly kill an
     in-flight task.
  2. The message printed "after daemon start at Y", but `Y` is
     `env_file_mtime_at_start` — the env file's mtime captured when the
     daemon LOADED it, not the daemon's start time.

This module pins:

  (a) hot-reloadable-only change → NO restart WARN (info note instead).
  (b) a FIXED (non-hot-reloadable) knob change → WARN naming that knob.
  (c) a mixed change (hot + fixed) → WARN still fires, names the fixed knob.
  (d) an unknown/unrecognized changed key → treated conservatively as
      fixed → WARN.
  (e) a bare touch / value-identical edit → no WARN and no info note.
  (f) the wording: "after the daemon loaded it at", never "daemon start at".
  (g) `collect_env_changed_knobs` returns the documented shape / classification.
  (h) `_capture_env_mtime_at_start` stashes the per-knob hash baseline.
"""
from __future__ import annotations

import hashlib
import json as _json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import automation_status
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Initialized project with the scaffolded env template removed so
    each test writes its own `.cc-autopilot/env` content (mirrors the
    TB-260 fixture contract)."""
    init_project(tmp_path)
    env_file = tmp_path / ".cc-autopilot" / "env"
    if env_file.exists():
        env_file.unlink()
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_env(cfg: Config, content: str) -> None:
    cfg.env_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.env_file.write_text(content)


def _pin_daemon_state(
    cfg: Config,
    *,
    mtime: float,
    knob_hashes: dict[str, str] | None,
) -> None:
    """Write `daemon_state.json` with a caller-controlled mtime baseline
    AND per-knob hash baseline — what `_capture_env_mtime_at_start`
    stashes at boot, but deterministic so tests drive the
    `current > at_start` + per-knob diff without sleeping / os.utime.
    """
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_state_file.write_text(
        _json.dumps(
            {
                "env_file_mtime_at_start": mtime,
                "env_file_knob_hashes_at_start": knob_hashes,
            },
            indent=2,
        ),
    )


def _clean_env(monkeypatch) -> None:
    """Strip noise knobs so the only WARN: / env: lines come from the
    env-staleness surface under test."""
    for name in (
        "AP2_AUTO_APPROVE",
        "AP2_AUTO_APPROVE_DRY_RUN",
        "AP2_AUTO_UNFREEZE_DRY_RUN",
        "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)


# ===========================================================================
# (a) Hot-reloadable-only change → NO restart WARN.
# ===========================================================================


def test_hot_reloadable_only_change_suppresses_restart_warn(
    cfg: Config, capsys, monkeypatch,
):
    """Operator flipped `AP2_IDEATION_DISABLED` (hot-reloadable) since
    the daemon loaded the env file. `ap2 status` must NOT emit the
    restart WARN — the knob applies on the next tick. A low-key `env:`
    info note names the knob instead."""
    from ap2.cli import cmd_status

    _clean_env(monkeypatch)
    _write_env(cfg, "AP2_IDEATION_DISABLED=1\n")
    env_mtime = cfg.env_file.stat().st_mtime
    # Baseline reflects the OLD value ("0") + an earlier mtime → stale.
    _pin_daemon_state(
        cfg,
        mtime=env_mtime - 10.0,
        knob_hashes={"AP2_IDEATION_DISABLED": _hash("0")},
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" not in out, out
    assert "ap2 stop && ap2 start" not in out, out
    assert "restart" not in out, out
    # The hot-reloadable edit is acknowledged via a low-key info note
    # naming the knob that will apply on the next tick.
    assert "env:" in out, out
    assert "AP2_IDEATION_DISABLED" in out, out
    assert "next tick" in out, out


# ===========================================================================
# (b) A FIXED knob change → WARN naming that knob.
# ===========================================================================


def test_fixed_knob_change_emits_warn_naming_knob(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_WEB_PORT` is in `env_reload.FIXED_KNOBS` — it binds a socket
    at startup and genuinely needs a restart. The WARN must fire AND name
    the offending knob so the operator knows exactly why."""
    from ap2.cli import cmd_status

    _clean_env(monkeypatch)
    _write_env(cfg, "AP2_WEB_PORT=8730\n")
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_daemon_state(
        cfg,
        mtime=env_mtime - 10.0,
        knob_hashes={"AP2_WEB_PORT": _hash("8729")},
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" in out, out
    assert "AP2_WEB_PORT" in out, out
    assert "ap2 stop && ap2 start" in out, out
    # Honest label — never "daemon start at".
    assert "after the daemon loaded it at" in out, out
    assert "daemon start at" not in out, out


# ===========================================================================
# (c) Mixed change (hot + fixed) → WARN still fires, names the fixed knob.
# ===========================================================================


def test_mixed_change_warns_and_names_only_fixed_knob(
    cfg: Config, capsys, monkeypatch,
):
    """A fixed knob changing alongside hot-reloadable ones must NOT be
    suppressed — the presence of one fixed knob means a restart is
    required. The WARN names the fixed knob (AP2_WEB_PORT), not the
    hot-reloadable one."""
    from ap2.cli import cmd_status

    _clean_env(monkeypatch)
    _write_env(cfg, "AP2_IDEATION_DISABLED=1\nAP2_WEB_PORT=8730\n")
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_daemon_state(
        cfg,
        mtime=env_mtime - 10.0,
        knob_hashes={
            "AP2_IDEATION_DISABLED": _hash("0"),
            "AP2_WEB_PORT": _hash("8729"),
        },
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" in out, out
    assert "AP2_WEB_PORT" in out, out
    assert "ap2 stop && ap2 start" in out, out


# ===========================================================================
# (d) Unknown/unrecognized changed key → conservatively fixed → WARN.
# ===========================================================================


def test_unknown_changed_key_treated_as_fixed_and_warns(
    cfg: Config, capsys, monkeypatch,
):
    """An unrecognized key (in neither HOT_RELOADABLE_KNOBS nor
    FIXED_KNOBS) is treated conservatively as fixed — better to
    false-warn than to silently clear after refreshing a knob whose
    semantics we can't classify."""
    from ap2.cli import cmd_status

    _clean_env(monkeypatch)
    _write_env(cfg, "AP2_SOMETHING_BRAND_NEW=2\n")
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_daemon_state(
        cfg,
        mtime=env_mtime - 10.0,
        knob_hashes={"AP2_SOMETHING_BRAND_NEW": _hash("1")},
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" in out, out
    assert "AP2_SOMETHING_BRAND_NEW" in out, out
    assert "ap2 stop && ap2 start" in out, out


# ===========================================================================
# (e) Bare touch / value-identical edit → no WARN AND no info note.
# ===========================================================================


def test_value_identical_edit_emits_nothing(
    cfg: Config, capsys, monkeypatch,
):
    """The env file mtime bumped (a comment edit / re-save) but no knob
    VALUE differs from what the daemon loaded → no restart is needed and
    there's nothing to apply, so neither the WARN nor the info note
    appears."""
    from ap2.cli import cmd_status

    _clean_env(monkeypatch)
    _write_env(cfg, "AP2_IDEATION_DISABLED=0\n")
    env_mtime = cfg.env_file.stat().st_mtime
    # Baseline value is identical ("0"); only the mtime is older → stale
    # by mtime but no knob actually changed.
    _pin_daemon_state(
        cfg,
        mtime=env_mtime - 10.0,
        knob_hashes={"AP2_IDEATION_DISABLED": _hash("0")},
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" not in out, out
    assert ".cc-autopilot/env modified" not in out, out


# ===========================================================================
# (g) collect_env_changed_knobs shape / classification.
# ===========================================================================


def test_collect_env_changed_knobs_reports_diff(cfg: Config):
    """The helper returns the sorted set of KEYs whose value differs from
    the at-start hash baseline, plus `loaded_values_available: True`."""
    _write_env(cfg, "AP2_IDEATION_DISABLED=1\nAP2_TASK_TIMEOUT_S=1800\n")
    _pin_daemon_state(
        cfg,
        mtime=cfg.env_file.stat().st_mtime - 10.0,
        knob_hashes={
            "AP2_IDEATION_DISABLED": _hash("0"),      # changed 0 -> 1
            "AP2_TASK_TIMEOUT_S": _hash("1800"),       # unchanged
        },
    )
    out = automation_status.collect_env_changed_knobs(cfg)
    assert out["loaded_values_available"] is True
    assert out["changed_knobs"] == ["AP2_IDEATION_DISABLED"]


def test_collect_env_changed_knobs_detects_added_and_removed(cfg: Config):
    """Added (in file, not baseline) and removed (in baseline, not file)
    keys both count as changed."""
    _write_env(cfg, "AP2_IDEATION_DISABLED=1\n")  # NEW knob added
    _pin_daemon_state(
        cfg,
        mtime=cfg.env_file.stat().st_mtime - 10.0,
        knob_hashes={"AP2_WEB_PORT": _hash("8729")},  # removed from file
    )
    out = automation_status.collect_env_changed_knobs(cfg)
    assert out["loaded_values_available"] is True
    assert out["changed_knobs"] == ["AP2_IDEATION_DISABLED", "AP2_WEB_PORT"]


def test_collect_env_changed_knobs_no_baseline_is_unavailable(cfg: Config):
    """A pre-TB-380 daemon stashed no `env_file_knob_hashes_at_start` →
    the helper reports `loaded_values_available: False` so the WARN path
    falls back to the conservative restart posture."""
    _write_env(cfg, "AP2_IDEATION_DISABLED=1\n")
    # daemon_state.json with ONLY the mtime baseline (pre-TB-380 shape).
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_state_file.write_text(
        _json.dumps({"env_file_mtime_at_start": 1.0}),
    )
    out = automation_status.collect_env_changed_knobs(cfg)
    assert out["loaded_values_available"] is False
    assert out["changed_knobs"] == []


# ===========================================================================
# (h) _capture_env_mtime_at_start stashes the per-knob hash baseline.
# ===========================================================================


def test_capture_stashes_knob_hashes(cfg: Config):
    """The daemon-start hook writes `env_file_knob_hashes_at_start` (a
    `{KEY: sha256hex}` map) so a separate-process `ap2 status` can diff
    against it. Hashes — not raw values — so a knob carrying a secret
    never lands in the state file."""
    from ap2.daemon import _capture_env_mtime_at_start

    _write_env(cfg, "AP2_IDEATION_DISABLED=1\nAP2_TASK_TIMEOUT_S=1800\n")
    _capture_env_mtime_at_start(cfg)

    data = _json.loads(cfg.daemon_state_file.read_text())
    hashes = data["env_file_knob_hashes_at_start"]
    assert hashes == {
        "AP2_IDEATION_DISABLED": _hash("1"),
        "AP2_TASK_TIMEOUT_S": _hash("1800"),
    }
    # The raw value must NOT appear anywhere in the serialized state.
    assert "1800" not in _json.dumps(hashes)


def test_capture_stashes_null_knob_hashes_when_no_env_file(cfg: Config):
    """No env file at startup → `env_file_knob_hashes_at_start` is null
    (parity with the mtime stash's null shape)."""
    from ap2.daemon import _capture_env_mtime_at_start

    _capture_env_mtime_at_start(cfg)
    data = _json.loads(cfg.daemon_state_file.read_text())
    assert data["env_file_knob_hashes_at_start"] is None
