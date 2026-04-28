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
