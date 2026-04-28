"""Tests for `ap2.web` — the local read-only web UI (TB-99 → TB-93 thaw).

Exercises the renderers against a synthetic project. Skips spinning up a
real HTTP server; the handler is a thin urlsplit + dispatch wrapper around
the same renderers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import web, events as ev_mod
from ap2.board import Board
from ap2.config import Config


@pytest.fixture
def project(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n- [ ] **TB-1** **Active task**\n"
        "## Ready\n\n"
        "## Backlog\n\n- [ ] **TB-2** **Backlog task** `#tag`\n"
        "## Complete\n\n- [x] **TB-3** **Done thing** — summary text\n"
        "## Frozen\n\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    ev_mod.append(cfg.events_file, "daemon_start")
    ev_mod.append(cfg.events_file, "task_complete", task="TB-3", status="complete",
                  commit="abc12345", summary="finished it")
    ev_mod.append(cfg.events_file, "task_error", task="TB-2", error="ValueError: x")
    ev_mod.append(cfg.events_file, "ideation_empty_board", cooldown_s=7200)
    return cfg


def test_home_renders(project: Config):
    html = web._render_home(project)
    assert "<!DOCTYPE html>" in html
    assert "TB-3" in html or "Active" in html  # board section labels present
    assert "daemon" in html.lower()
    # All four events surface in the events table
    assert "task_complete" in html
    assert "task_error" in html
    assert "ideation_empty_board" in html
    assert "daemon_start" in html


def test_home_marks_failure_class(project: Config):
    html = web._render_home(project)
    # task_error is in FAILURE_EVENT_TYPES → row gets the `failure` class
    assert 'class="failure"' in html


def test_verification_partial_gets_warning_tint(project: Config):
    """`verification_partial` lands tasks in Complete, so it's not a daemon-
    health failure (and isn't in diagnose.FAILURE_EVENT_TYPES). But the
    operator should still notice it — partial verdicts often signal a prose
    bullet the SDK judge can't evaluate. Web UI gives it a `warning` class
    distinct from `failure`."""
    ev_mod.append(project.events_file, "verification_partial",
                  task="TB-3", criterion="some prose claim")
    html = web._render_events(project, typ=None, n=50)
    assert 'class="warning"' in html
    # Sanity: verification_partial did NOT also pick up the failure class.
    rows = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    partial_row = next(r for r in rows.split("<tr ") if "verification_partial" in r)
    assert 'class="warning"' in partial_row
    assert 'class="failure"' not in partial_row


def test_verification_partial_in_quick_filters(project: Config):
    """The events page exposes a `verification_partial` quick-filter button so
    the operator can see all of them at once without typing the URL."""
    html = web._render_events(project, typ=None, n=10)
    assert "?type=verification_partial" in html


def test_events_renders_full_text(project: Config):
    """Truncation-free rendering — long values land verbatim in the page.

    This is the whole point of the web UI vs `ap2 logs`: the latter caps each
    field at 120 chars (`_short`), the former shows the entire value.
    """
    long_summary = "x" * 1000
    ev_mod.append(project.events_file, "cron_complete", job="status-report",
                  summary=long_summary)
    html = web._render_events(project, typ=None, n=50)
    assert long_summary in html  # full text, no ellipsis truncation


def test_events_filters_by_type(project: Config):
    html = web._render_events(project, typ="task_error", n=50)
    assert "task_error" in html
    # task_complete shouldn't appear in the events table when filtered to task_error.
    # (It still appears in the filter button list — guard against false hits there.)
    rows_block = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0] if "<tbody>" in html else ""
    assert "task_complete" not in rows_block
    assert "ideation_empty_board" not in rows_block


def test_tasks_groups_by_section(project: Config):
    html = web._render_tasks(project)
    assert "Active" in html and "TB-1" in html
    assert "Backlog" in html and "TB-2" in html
    assert "Complete" in html and "TB-3" in html
    # Frozen is empty → "(empty)" placeholder
    assert "Frozen" in html


def test_task_detail_pulls_related_events(project: Config):
    html = web._render_task(project, "TB-3")
    assert "TB-3" in html and "Done thing" in html
    # The related-events scan should match task_complete (task=TB-3)
    assert "task_complete" in html
    # An unrelated event (TB-2's task_error) should NOT appear in this task's view
    related_block = html.split("related events", 1)[1] if "related events" in html else ""
    assert "TB-2" not in related_block


def test_task_detail_includes_briefing_when_present(project: Config, tmp_path: Path):
    briefing_path = tmp_path / ".cc-autopilot" / "tasks" / "tb4-thing.md"
    briefing_path.parent.mkdir(parents=True, exist_ok=True)
    briefing_path.write_text("# TB-4 brief\n\n## Goal\nDo the thing.\n## Verification\n- pytest\n")
    board = Board.load(project.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-4",
        title="With briefing",
        briefing=".cc-autopilot/tasks/tb4-thing.md",
    )
    board.save()
    html = web._render_task(project, "TB-4")
    assert "## Goal" in html
    assert "Do the thing." in html
    assert "## Verification" in html


def test_task_detail_404_for_missing(project: Config):
    html = web._render_task(project, "TB-999")
    assert "TB-999" in html
    assert "Not on the board" in html


# --------- TB-93 thaw: pipelines / insights / ideation_state / commits ---------


def test_pipelines_lists_pipeline_starts(project: Config):
    ev_mod.append(
        project.events_file, "pipeline_start",
        name="my-sweep", pid=99999, command="uv run python scripts/sweep.py",
        validation="TB-7", log="/tmp/my-sweep-99999.log",
    )
    html = web._render_pipelines(project)
    assert "my-sweep" in html
    assert "TB-7" in html
    assert "/task/TB-7" in html
    assert "uv run python scripts/sweep.py" in html
    # PID 99999 is almost certainly not alive in CI
    assert "dead/exited" in html


def test_pipelines_empty_state(project: Config):
    html = web._render_pipelines(project)
    assert "no pipeline_start events" in html


def test_insights_shows_files(project: Config, tmp_path):
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "alpha.md").write_text(
        "---\n"
        "tldr: Alpha decay observed in regime X\n"
        "updated: 2026-04-28T10:00:00Z\n"
        "updated_by: TB-50\n"
        "cites: [TB-49, TB-50]\n"
        "---\n\n"
        "Body content.\n"
    )
    html = web._render_insights(project)
    assert "alpha.md" in html
    assert "Alpha decay observed in regime X" in html
    assert "TB-50" in html


def test_insights_empty_state(project: Config):
    html = web._render_insights(project)
    assert "no insights dir" in html or "empty" in html


def test_insight_detail_shows_full_content(project: Config, tmp_path):
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    body = "---\ntldr: x\nupdated: 2026-04-28\nupdated_by: op\ncites: []\n---\n# Header\n\nBody.\n"
    (insights_dir / "alpha.md").write_text(body)
    html = web._render_insight(project, "alpha.md")
    assert "Header" in html
    assert "Body." in html


def test_insight_404(project: Config):
    html = web._render_insight(project, "nonexistent.md")
    assert "not found" in html


def test_insight_blocks_path_traversal(project: Config):
    html = web._render_insight(project, "../../../../etc/passwd")
    assert "invalid" in html.lower()


def test_ideation_state_shows_file_and_summary(project: Config, tmp_path):
    state_path = tmp_path / ".cc-autopilot" / "ideation_state.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("# Ideation State\n\n## Mission alignment\nAll good.\n")
    ev_mod.append(
        project.events_file, "ideation_complete",
        summary="Cycle 4: proposed TB-100, TB-101, TB-102.",
    )
    ev_mod.append(project.events_file, "ideation_state_updated", bytes=120)
    html = web._render_ideation_state(project)
    assert "All good." in html
    assert "Cycle 4" in html
    assert "120" in html


def test_ideation_state_no_file(project: Config):
    html = web._render_ideation_state(project)
    assert "not yet written" in html or "ideation_state.md" in html


def test_commits_in_non_git_dir(project: Config):
    html = web._render_commits(project)
    assert "not a git repo" in html


def test_event_extra_handles_dict_and_list(project: Config):
    """Nested values — common shape for stderr_tail, last_messages,
    files_changed — render as JSON not raw repr, and don't break HTML."""
    e = {
        "ts": "2026-04-28T12:00:00Z",
        "type": "task_error",
        "task": "TB-2",
        "stderr_tail": "line1\nline2",
        "last_messages": [{"role": "assistant", "text": "hi"}],
    }
    out = web._event_extra(e)
    assert "stderr_tail" in out
    assert "last_messages" in out
    # newlines collapsed for the one-line summary
    assert "\n" not in out
