"""Unit tests for `ap2/diagnose.py` — the pure inspector module (TB-71).

The diagnose module has no SDK or network dependencies, so these tests use
plain pytest (no e2e fixtures). They cover the report-shape contract that
both the watchdog (daemon._maybe_auto_diagnose) and any future ad-hoc
inspector tool depend on.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ap2 import diagnose, events
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


def _project(tmp_path: Path) -> Config:
    """Initialize a minimal ap2 project under tmp_path and return its Config."""
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _emit(cfg: Config, typ: str, **kw) -> dict:
    return events.append(cfg.events_file, typ, **kw)


def test_build_report_empty_project(tmp_path: Path):
    """No events, no tasks → report has Nones / zeros, never raises."""
    cfg = _project(tmp_path)
    report = diagnose.build_report(cfg)

    assert report.since_last_activity_s is None
    assert report.last_meaningful_event is None
    assert report.board_summary["Active"] == 0
    assert report.board_summary["Ready"] == 0
    assert report.board_summary["Backlog"] == 0
    assert report.board_summary["active_task"] is None
    assert report.recent_failures == []
    # Cron status is empty when no cron.yaml is bootstrapped yet.
    assert report.cron_status == []


def test_since_last_activity_ignores_failure_events(tmp_path: Path):
    """A daemon stuck in a failure loop (only `task_error` events for hours)
    must trip the watchdog — failures don't count as meaningful activity."""
    cfg = _project(tmp_path)
    _emit(cfg, "task_error", error="boom")
    _emit(cfg, "task_error", error="boom again")

    report = diagnose.build_report(cfg)
    # No meaningful event in the tail → since_last_activity_s should be None.
    assert report.since_last_activity_s is None
    # But the failures must surface in the report.
    assert len(report.recent_failures) == 2


def test_since_last_activity_resets_on_daemon_start(tmp_path: Path):
    """Backward-compat for stoch's resume-after-pause: a `daemon_start` event
    counts as meaningful activity, so the first post-resume tick won't fire
    the watchdog even if the prior session's last activity is hours old."""
    cfg = _project(tmp_path)
    _emit(cfg, "task_complete", task="TB-1", status="complete")
    # Simulate hours of nothing, then a fresh daemon_start "now".
    _emit(cfg, "daemon_start", pid=12345)

    # Report `now` close to when daemon_start was emitted.
    report = diagnose.build_report(cfg, now=time.time())
    assert report.since_last_activity_s is not None
    assert report.since_last_activity_s < 60  # well under the 3h threshold
    assert report.last_meaningful_event["type"] == "daemon_start"


def test_recent_failures_filtered_correctly(tmp_path: Path):
    """Only failure-class events (task_error, retry_exhausted, malformed,
    verification_failed, ...) land in `recent_failures`. Successes and ticks
    are excluded."""
    cfg = _project(tmp_path)
    _emit(cfg, "task_complete", task="TB-1", status="complete")
    _emit(cfg, "task_error", task="TB-2", error="oops")
    _emit(cfg, "verification_failed", task="TB-3", exit_code=1)
    _emit(cfg, "cron_complete", job="status-report")
    _emit(cfg, "board_malformed_line", section="Complete", line="garbage")
    _emit(cfg, "retry_exhausted", task="TB-4", attempts=3)

    report = diagnose.build_report(cfg)
    types = [f["type"] for f in report.recent_failures]
    assert types == [
        "task_error",
        "verification_failed",
        "board_malformed_line",
        "retry_exhausted",
    ]
    # task_complete and cron_complete are NOT failures.
    assert "task_complete" not in types
    assert "cron_complete" not in types


def test_board_health_surfaces_malformed_lines(tmp_path: Path):
    """A `(<sha>)` annotation between **TB-N** and **Title** breaks
    TASK_LINE_RE (the stoch TB-59 case). diagnose must surface it."""
    cfg = _project(tmp_path)
    cfg.tasks_file.write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n"
        "- [x] **TB-59** (7735de2) **broken line** — annotation breaks regex\n"
        "\n## Frozen\n"
    )

    report = diagnose.build_report(cfg)
    assert len(report.board_health["malformed_lines"]) == 1
    section, line = report.board_health["malformed_lines"][0]
    assert section == "Complete"
    assert "TB-59" in line


def test_board_health_detects_unsatisfiable_block(tmp_path: Path):
    """A Backlog task `(blocked on: TB-X)` where TB-X is in Frozen will never
    auto-promote — the watchdog must call this out as a stalled blocker."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-90", title="Frozen blocker")
    board.add(
        "Backlog",
        task_id="TB-91",
        title="Stalled task",
        description="(blocked on: TB-90)",
    )
    board.save()

    report = diagnose.build_report(cfg)
    assert "TB-91" in report.board_health["unsatisfiable_blocks"]


def test_render_markdown_under_2k_chars(tmp_path: Path):
    """Even with many failure events, the rendered markdown stays compact —
    Mattermost has a 16k limit but we want diagnose posts to be skimmable."""
    cfg = _project(tmp_path)
    _emit(cfg, "task_complete", task="TB-1", status="complete")
    for i in range(50):
        _emit(cfg, "task_error", task=f"TB-{i + 100}",
              error="x" * 200)  # bloat to ensure truncation is exercised

    report = diagnose.build_report(cfg)
    text = diagnose.render_markdown(report)
    assert len(text) < 2000, f"render_markdown produced {len(text)} chars"


def test_render_markdown_skips_no_data_when_empty(tmp_path: Path):
    """An entirely empty project still produces a sensible (non-empty) report."""
    cfg = _project(tmp_path)
    report = diagnose.build_report(cfg)
    text = diagnose.render_markdown(report)
    assert "ap2 watchdog" in text
    assert tmp_path.name in text
    assert "no meaningful events yet" in text


def test_cron_status_overdue_detection(tmp_path: Path):
    """A cron job whose `last_fired` is more than 2 intervals ago is overdue.
    Ideation is no longer a cron job (see ap2/ideation.py) so only
    status-report is exercised here."""
    cfg = _project(tmp_path)
    from ap2.cron import bootstrap, load_state
    bootstrap(cfg.cron_file)

    fake_now = time.time()
    state = load_state(cfg.cron_state_file)
    state["status-report"] = fake_now - (5 * 3600)  # 5h > 2h*2 = 4h → overdue
    cfg.cron_state_file.write_text(json.dumps(state))

    report = diagnose.build_report(cfg, now=fake_now)
    by_name = {c["name"]: c for c in report.cron_status}
    assert "ideation" not in by_name
    assert by_name["status-report"]["overdue"] is True  # 5h > 4h (2 * 2h)


def test_meaningful_event_set_includes_resume_class():
    """Pin the resume-class events explicitly so a future trim doesn't drop
    the backward-compat guarantee for stoch."""
    must_include = {"daemon_start", "daemon_resume", "task_complete",
                    "cron_complete", "mattermost"}
    assert must_include <= diagnose.MEANINGFUL_EVENT_TYPES
