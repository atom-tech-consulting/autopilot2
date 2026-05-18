"""TB-252: `ap2 doctor` warns when `AP2_VERIFY_TIMEOUT_S` is configured
below the observed-typical successful full-suite verifier duration.

Axis-2 preventive surface for the failure-recovery promise in goal.md
L88-100. Sibling shape to TB-234's `auto_approve_audit` and TB-239's
`auto_unfreeze_audit`: a single new audit function in `ap2/doctor.py`,
WARN (not FAIL) per goal.md L184-186, internal constants (no new env
knobs).

Anchored to the 2026-05-17 retry_exhausted cascade where five tasks
(TB-245 / 246 / 247 / 249 / 250) hit
`exit_code=None duration_s=600.01` on the project-wide verifier while
the suite actually takes 1320-1349s on a healthy commit. The
underlying signal is `verify_passed` events (TB-252; emitted on
successful project-wide verify in daemon.py); the audit pulls the
recent tail and compares the worst-case successful duration against
`cfg.verify_timeout_s`.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from ap2 import doctor as doctor_mod
from ap2.config import Config, DEFAULT_VERIFY_TIMEOUT_S, EVENTS_FILE
from ap2.doctor import (
    _VERIFY_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES,
    diagnose,
    verify_timeout_audit,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_cfg(project_root: Path, verify_timeout_s: int) -> Config:
    """Construct a minimal Config without sourcing project env.

    Tests synthesize their own events.jsonl under `project_root /
    .cc-autopilot/`, so we don't want `Config.load()` to read the real
    project's `env` file and bleed unrelated env values into the run.
    """
    events_file = project_root / EVENTS_FILE
    events_file.parent.mkdir(parents=True, exist_ok=True)
    return Config(
        project_root=project_root,
        tasks_file=project_root / "TASKS.md",
        progress_file=project_root / ".cc-autopilot" / "progress.md",
        tasks_dir=project_root / ".cc-autopilot" / "tasks",
        events_file=events_file,
        cron_file=project_root / ".cc-autopilot" / "cron.yaml",
        pid_file=project_root / ".cc-autopilot" / "daemon.pid",
        pause_flag=project_root / ".cc-autopilot" / "paused",
        cron_state_file=project_root / ".cc-autopilot" / "cron_state.json",
        mm_state_file=project_root / ".cc-autopilot" / "mm_state.json",
        retry_state_file=project_root / ".cc-autopilot" / "retry_state.json",
        auto_diagnose_state_file=project_root / ".cc-autopilot" / "auto_diagnose_state.json",
        next_task_id=1,
        tick_interval_s=30,
        mm_tick_interval_s=10,
        event_context_size=50,
        task_timeout_s=1200,
        control_timeout_s=300,
        max_retries=3,
        verify_cmd="uv run pytest -q",
        verify_timeout_s=verify_timeout_s,
        auto_diagnose_idle_threshold_s=10800,
        auto_diagnose_cooldown_s=21600,
    )


def _seed_verify_events(
    events_file: Path,
    *,
    durations: list[float],
    base_ts: _dt.datetime | None = None,
    step: _dt.timedelta = _dt.timedelta(hours=1),
) -> None:
    """Write `verify_passed` events with the given durations, one per line.

    Timestamps walk forward from `base_ts` (default: 1 day before now,
    so the seeded events fit comfortably inside the audit's 7-day
    window) at `step` intervals.
    """
    base_ts = base_ts or (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
    )
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a") as f:
        for i, dur in enumerate(durations):
            ts = (base_ts + step * i).strftime("%Y-%m-%dT%H:%M:%SZ")
            evt = {
                "ts": ts,
                "type": "verify_passed",
                "task": f"TB-{100 + i}",
                "command": "uv run pytest -q ap2/tests/",
                "exit_code": 0,
                "duration_s": float(dur),
            }
            f.write(json.dumps(evt) + "\n")


def _levels(res) -> list[str]:
    return [lvl for lvl, _ in res.messages]


def _texts(res) -> list[str]:
    return [txt for _, txt in res.messages]


# ===========================================================================
# Branch 1: WARN when timeout below typical (the headline TB-252 case)
# ===========================================================================


def test_verify_timeout_audit_warns_when_timeout_below_typical(
    tmp_path: Path,
):
    """Headline case the TB-245-250 cascade exemplifies: 5 successful-
    verify samples of 900s wall-clock duration vs. a 600s timeout
    means the next run is one slow tick away from
    `exit_code=None duration_s=600.01`. Audit must surface a WARN
    with the one-line fix-shape recommendation."""
    cfg = _make_cfg(tmp_path, verify_timeout_s=600)
    _seed_verify_events(cfg.events_file, durations=[900.0] * 5)

    res = verify_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert levels.count("WARN") == 1, levels
    warn_msg = next(t for lvl, t in res.messages if lvl == "WARN")
    # WARN body names the misconfiguration shape, the fix env knob,
    # and points the operator at the unfreeze companion step for any
    # 600s-shape Frozen tasks already on the board.
    assert "AP2_VERIFY_TIMEOUT_S=600s" in warn_msg
    assert "recommend `export AP2_VERIFY_TIMEOUT_S=" in warn_msg
    assert "below observed-typical successful verify duration" in warn_msg
    # 900s * 1.5 = 1350s → recommendation should be at least 1350.
    assert "1350" in warn_msg, warn_msg
    # n / sample-days attribution is in the body.
    assert "n=5" in warn_msg
    # WARN doesn't FAIL the report (operator authority preserved per
    # goal.md L184-186).
    assert res.ok


# ===========================================================================
# Branch 2: INFO when sample size is below the insufficient-data floor
# ===========================================================================


def test_verify_timeout_audit_info_when_insufficient_samples(
    tmp_path: Path,
):
    """Fresh installs with <3 samples must NOT trip the WARN — a
    single anomalous slow run shouldn't push an operator to retune
    the env. Audit emits INFO so the operator knows the audit ran
    but didn't have enough data."""
    cfg = _make_cfg(tmp_path, verify_timeout_s=600)
    # Two samples — below the floor of 3.
    _seed_verify_events(cfg.events_file, durations=[1500.0, 1800.0])

    res = verify_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert "WARN" not in levels, levels
    assert "INFO" in levels
    info_msg = next(t for lvl, t in res.messages if lvl == "INFO")
    assert "insufficient data" in info_msg
    assert "AP2_VERIFY_TIMEOUT_S" in info_msg
    assert f">={_VERIFY_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES}" in info_msg
    assert res.ok


# ===========================================================================
# Branch 3: INFO "comfortable headroom" when timeout well above typical
# ===========================================================================


def test_verify_timeout_audit_info_when_comfortable_headroom(
    tmp_path: Path,
):
    """Production-healthy case: 600s timeout, 200s typical — 3×
    headroom. Audit emits INFO confirming the configuration is
    comfortable and no nudge is needed."""
    cfg = _make_cfg(tmp_path, verify_timeout_s=600)
    _seed_verify_events(cfg.events_file, durations=[180.0, 200.0, 195.0, 210.0, 190.0])

    res = verify_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert "WARN" not in levels, levels
    assert levels == ["INFO"], levels
    info_msg = next(t for lvl, t in res.messages if lvl == "INFO")
    assert "comfortable headroom" in info_msg
    assert "AP2_VERIFY_TIMEOUT_S=600s" in info_msg
    # observed-typical = max(durations) = 210
    assert "210" in info_msg
    assert res.ok


# ===========================================================================
# Branch 4: missing events file → INFO (graceful degradation)
# ===========================================================================


def test_verify_timeout_audit_handles_missing_events_file(
    tmp_path: Path,
):
    """Fresh checkout / clean install: no events.jsonl on disk. The
    audit must not crash and must emit INFO ("insufficient data")
    rather than a WARN — the operator hasn't done anything wrong;
    there's just no telemetry yet."""
    cfg = _make_cfg(tmp_path, verify_timeout_s=600)
    # Explicitly DO NOT seed any events; events.jsonl does not exist.
    assert not cfg.events_file.exists()

    res = verify_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert "WARN" not in levels, levels
    assert levels == ["INFO"]
    info_msg = next(t for lvl, t in res.messages if lvl == "INFO")
    assert "insufficient data" in info_msg
    assert res.ok


# ===========================================================================
# Bonus: tight-headroom band emits INFO with the bump nudge
# ===========================================================================


def test_verify_timeout_audit_info_tight_headroom_band(tmp_path: Path):
    """In the tight band (1.0 ≤ ratio < 1.5): timeout above worst-case
    but below the recommended safety margin. Audit nudges the
    operator with INFO, not WARN, so the surface stays calm — the
    timeout isn't actively breaking anything."""
    # typical=500s, timeout=600s → ratio=1.2 (in [1.0, 1.5))
    cfg = _make_cfg(tmp_path, verify_timeout_s=600)
    _seed_verify_events(cfg.events_file, durations=[480.0, 490.0, 500.0, 495.0, 470.0])

    res = verify_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert "WARN" not in levels, levels
    assert levels == ["INFO"], levels
    info_msg = next(t for lvl, t in res.messages if lvl == "INFO")
    # The tight-band nudge names the headroom percent + the "consider
    # bumping" recommendation. Distinct from the comfortable-headroom
    # text so we can tell the two INFO branches apart at a glance.
    assert "consider bumping" in info_msg
    assert "headroom" in info_msg
    assert "AP2_VERIFY_TIMEOUT_S=600s" in info_msg


# ===========================================================================
# diagnose() wiring: end-to-end the section appears under the doctor report
# ===========================================================================


def test_diagnose_includes_verify_timeout_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """End-to-end: `diagnose()` assembles a "verify timeout headroom"
    section so `ap2 doctor` prints it alongside the existing audits.
    The section sits next to the "verify gate" section so the
    operator sees the gate's command (what runs) and the gate's
    timeout fit (how long it has to run) as a paired block."""
    # Stub out sandbox-user probes so we don't depend on the real machine.
    monkeypatch.setattr(doctor_mod, "_user_exists", lambda u: False)
    monkeypatch.setattr(
        doctor_mod, "_sandbox_clone_path", lambda root, user: None
    )
    # Seed a synthesized project skeleton so `_project_init_state`
    # doesn't fail and gate further sections.
    (tmp_path / "CLAUDE.md").write_text("## Autopilot\n- Next task ID: TB-1\n")
    (tmp_path / "TASKS.md").write_text("# Tasks\n")
    (tmp_path / ".cc-autopilot").mkdir()
    (tmp_path / ".cc-autopilot" / "progress.md").write_text("# Progress\n")
    (tmp_path / ".cc-autopilot" / "tasks").mkdir()

    cfg = _make_cfg(tmp_path, verify_timeout_s=DEFAULT_VERIFY_TIMEOUT_S)

    report = diagnose(tmp_path, user="ghost", cfg=cfg)
    titles = [t for t, _ in report.sections]
    assert "verify timeout headroom" in titles
    # Pairing: verify-timeout section is directly after the verify-gate
    # section so an operator scanning the report sees the two as a
    # block.
    vg_idx = titles.index("verify gate")
    vt_idx = titles.index("verify timeout headroom")
    assert vt_idx == vg_idx + 1, (
        f"verify-timeout section should follow verify-gate section "
        f"directly; got titles={titles}"
    )
