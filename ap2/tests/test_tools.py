"""Tests for the SDK-free tool implementations (do_board_edit, do_cron_edit, …).

The SDK wiring in tools.build_mcp_server is not exercised here — the adapters
just delegate to these functions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2.board import Board
from ap2.config import Config
from ap2 import tools


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "- [ ] **TB-5** **Existing** `#x` — An old task.\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    return Config.load(tmp_path)


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def test_board_edit_add_ready_assigns_id(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {"action": "add_ready", "title": "Brand new", "tags": ["#auto"]},
    )
    body = _unwrap(res)
    assert body["task_id"] == "TB-10"
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-10") == ("Ready", 0)
    # CLAUDE.md next_task_id bumped to 11
    assert "TB-11" in (tmp_path / "CLAUDE.md").read_text()


def test_board_edit_add_writes_briefing(cfg):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "With brief",
            "briefing": "# Brief\n\nDo this.",
        },
    )
    body = _unwrap(res)
    brief_path = cfg.project_root / body["briefing_path"]
    assert brief_path.exists()
    assert "Do this" in brief_path.read_text()


def test_board_edit_move(cfg):
    tools.do_board_edit(cfg, {"action": "move_to_ready", "task_id": "TB-5"})
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Ready"


def test_board_edit_invalid_action(cfg):
    res = tools.do_board_edit(cfg, {"action": "bogus"})
    assert res.get("isError")


def test_board_edit_move_missing_id(cfg):
    res = tools.do_board_edit(cfg, {"action": "move_to_complete", "task_id": "TB-999"})
    assert res.get("isError")


def test_cron_edit_add_and_remove(cfg):
    res = tools.do_cron_edit(
        cfg,
        {"action": "add", "name": "hourly", "interval": "1h", "prompt": "report"},
    )
    body = _unwrap(res)
    assert "hourly" in body["jobs"]

    # duplicate
    res2 = tools.do_cron_edit(
        cfg,
        {"action": "add", "name": "hourly", "interval": "1h", "prompt": "report"},
    )
    assert res2.get("isError")

    tools.do_cron_edit(cfg, {"action": "remove", "name": "hourly"})
    from ap2.cron import load_jobs

    assert load_jobs(cfg.cron_file) == []


def test_log_event(cfg):
    res = tools.do_log_event(cfg, {"type": "note", "summary": "hello"})
    assert not res.get("isError")
    from ap2.events import tail

    evts = tail(cfg.events_file, 1)
    assert evts[0]["type"] == "note"
    assert evts[0]["summary"] == "hello"


def test_daemon_pause_resume(cfg):
    tools.do_daemon_control(cfg, {"action": "pause", "reason": "maintenance"})
    assert cfg.pause_flag.exists()
    tools.do_daemon_control(cfg, {"action": "resume"})
    assert not cfg.pause_flag.exists()
