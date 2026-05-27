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
    """A Backlog task with `@blocked:TB-X` where TB-X is in Frozen will never
    auto-promote — the watchdog must call this out as a stalled blocker."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-90", title="Frozen blocker")
    board.add(
        "Backlog",
        task_id="TB-91",
        title="Stalled task",
        meta={"blocked": "TB-90"},
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
    state["status-report"] = fake_now - (20 * 3600)  # 20h > 8h*2 = 16h → overdue
    cfg.cron_state_file.write_text(json.dumps(state))

    report = diagnose.build_report(cfg, now=fake_now)
    by_name = {c["name"]: c for c in report.cron_status}
    assert "ideation" not in by_name
    assert by_name["status-report"]["overdue"] is True  # 20h > 16h (2 * 8h)


# ---------------------------------------------------------------------------
# TB-121: pending-review distinction. The board health check separates
# `pending_review` (operator-approval gate; soft state) from
# `unsatisfiable_blocks` (structural dead-end; hard state). Watchdog
# uses `is_wholly_pending_review` to suppress auto-diagnose when every
# Backlog task is review-gated and nothing else is in flight.

def test_pending_review_distinct_from_unsatisfiable(tmp_path: Path):
    """A Backlog task gated on `@blocked:review` lands in
    `pending_review`, NOT `unsatisfiable_blocks`. A separate task
    gated on a Frozen TB-N still lands in `unsatisfiable_blocks`."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    # Pending operator approval — soft state.
    board.add(
        "Backlog",
        task_id="TB-200",
        title="ideation proposal",
        meta={"blocked": "review"},
    )
    # Hard state — gated on a Frozen TB-N that won't progress.
    board.add("Frozen", task_id="TB-201", title="frozen blocker")
    board.add(
        "Backlog",
        task_id="TB-202",
        title="depends on frozen",
        meta={"blocked": "TB-201"},
    )
    board.save()

    report = diagnose.build_report(cfg)
    assert report.board_health["pending_review"] == ["TB-200"]
    assert report.board_health["unsatisfiable_blocks"] == ["TB-202"]


def test_pending_review_mixed_blockers_count_as_unsatisfiable_when_other_dead(tmp_path: Path):
    """A task with `review` AND a Frozen-TB-N blocker is still gated on
    the structural deadlock, so the watchdog should flag it as
    unsatisfiable rather than letting the soft-state bucket hide it."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-301", title="frozen blocker")
    board.add(
        "Backlog",
        task_id="TB-302",
        title="mixed",
        meta={"blocked": "review,TB-301"},
    )
    board.save()

    report = diagnose.build_report(cfg)
    # Not in pending_review (mixed blockers).
    assert "TB-302" not in report.board_health["pending_review"]
    # The TB-301 part of the mix triggers the unsatisfiable flag.
    assert "TB-302" in report.board_health["unsatisfiable_blocks"]


def test_is_wholly_pending_review_true_when_only_review_in_backlog(tmp_path: Path):
    """Active=0, Ready=0, every Backlog task is review-only → True.
    Drives the watchdog to suppress auto-diagnose."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-400", title="prop a",
        meta={"blocked": "review"},
    )
    board.add(
        "Backlog", task_id="TB-401", title="prop b",
        meta={"blocked": "review"},
    )
    board.save()

    report = diagnose.build_report(cfg)
    assert diagnose.is_wholly_pending_review(report) is True


def test_is_wholly_pending_review_false_when_active_has_work(tmp_path: Path):
    """Even with all Backlog tasks review-gated, an Active task means
    the daemon is doing work — not "operator AFK"."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-500", title="in flight")
    board.add(
        "Backlog", task_id="TB-501", title="prop",
        meta={"blocked": "review"},
    )
    board.save()

    report = diagnose.build_report(cfg)
    assert diagnose.is_wholly_pending_review(report) is False


def test_is_wholly_pending_review_false_when_unblocked_backlog_present(tmp_path: Path):
    """A Backlog task with no blockers at all is auto-promotable, so
    the gate is NOT the only thing holding the daemon back."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-600", title="prop",
        meta={"blocked": "review"},
    )
    board.add("Backlog", task_id="TB-601", title="ungated")
    board.save()

    report = diagnose.build_report(cfg)
    # TB-601 mixes into Backlog without a review token, so the
    # "wholly pending review" classification doesn't fit.
    assert diagnose.is_wholly_pending_review(report) is False


def test_is_wholly_pending_review_false_for_empty_board(tmp_path: Path):
    """Empty Backlog → no proposals → not pending review (it's just
    idle). The watchdog falls through to its normal threshold check."""
    cfg = _project(tmp_path)
    report = diagnose.build_report(cfg)
    assert diagnose.is_wholly_pending_review(report) is False


def test_render_markdown_surfaces_pending_review_count(tmp_path: Path):
    """The diagnose markdown surface lists pending-review count + the
    `ap2 approve TB-N` suggestion so an operator skimming a watchdog
    post can act without scrolling."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-700", title="prop",
        meta={"blocked": "review"},
    )
    board.save()

    report = diagnose.build_report(cfg)
    text = diagnose.render_markdown(report)
    assert "pending operator review" in text or "pending review" in text.lower()
    assert "TB-700" in text
    assert "ap2 approve" in text


def test_meaningful_event_set_includes_resume_class():
    """Pin the resume-class events explicitly so a future trim doesn't drop
    the backward-compat guarantee for stoch."""
    must_include = {"daemon_start", "daemon_resume", "task_complete",
                    "cron_complete", "mattermost"}
    assert must_include <= diagnose.MEANINGFUL_EVENT_TYPES
