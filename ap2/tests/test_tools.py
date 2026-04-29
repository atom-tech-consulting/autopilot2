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


def test_board_edit_add_ready_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {"action": "add_ready", "title": "Waiter", "blocked_on": "TB-5"},
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert "blocked on: TB-5" in t.description


def test_board_edit_add_backlog_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "Waiter", "blocked_on": "TB-5"},
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert "blocked on: TB-5" in t.description


def test_board_edit_add_frozen_still_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {"action": "add_frozen", "title": "Waiter", "blocked_on": "TB-5"},
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert "blocked on: TB-5" in t.description


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


# ---------------------------------------------------------------------------
# TB-81: pipeline_task_start atomically launches a detached process and
# creates a Backlog validation task gated on the process's liveness.

def _drain(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# TB-90: ideation_state_write — narrow MCP tool for overwriting the
# .cc-autopilot/ideation_state.md assessment file from the cron agent.

def test_ideation_state_write_happy_path(cfg):
    body = (
        "# Ideation State\n\n_Last updated: 2026-04-27T20:00:00Z by ideation cron_\n\n"
        "## Mission alignment\nServing the Mission per TB-87 / TB-89.\n"
    )
    res = tools.do_ideation_state_write(cfg, {"content": body})
    out = _unwrap(res)
    assert out["bytes"] == len(body)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    assert target.exists()
    assert target.read_text() == body


def test_ideation_state_write_emits_event(cfg):
    body = "# Ideation State\n\n## Mission alignment\nGrounded.\n"
    tools.do_ideation_state_write(cfg, {"content": body})
    from ap2.events import tail

    evts = tail(cfg.events_file, 5)
    updates = [e for e in evts if e["type"] == "ideation_state_updated"]
    assert len(updates) == 1
    assert updates[0]["bytes"] == len(body)


def test_ideation_state_write_overwrites_prior_content(cfg):
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Old assessment\n\nstale content\n")
    body = "# Fresh\n\n## Mission alignment\nNew text.\n"
    tools.do_ideation_state_write(cfg, {"content": body})
    assert target.read_text() == body
    assert "stale content" not in target.read_text()


def test_ideation_state_write_rejects_empty_content(cfg):
    res = tools.do_ideation_state_write(cfg, {"content": ""})
    assert res.get("isError")
    res = tools.do_ideation_state_write(cfg, {"content": "   \n  "})
    assert res.get("isError")
    res = tools.do_ideation_state_write(cfg, {})
    assert res.get("isError")


def test_ideation_state_write_rejects_oversized_content(cfg):
    body = "x" * 60_000
    res = tools.do_ideation_state_write(cfg, {"content": body})
    assert res.get("isError")
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    # Did NOT write the file when oversized.
    assert not target.exists() or "x" * 60_000 not in target.read_text()


def test_ideation_state_write_atomic_no_partial_on_failure(cfg, monkeypatch):
    """The tmpfile + rename pattern means a reader never sees a partial
    write. Hard to test directly without race injection, but we can verify
    no `.tmp` lingers after a successful write."""
    body = "# Ideation State\n\n## Mission alignment\nok.\n"
    tools.do_ideation_state_write(cfg, {"content": body})
    target_dir = cfg.project_root / ".cc-autopilot"
    assert not (target_dir / "ideation_state.md.tmp").exists()


def test_pipeline_task_start_happy_path(cfg, tmp_path):
    res = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "demo",
            "command": "sleep 30",
            "validation_title": "Validate demo output",
            "validation_briefing": "# Validate\n\n## Verification\n- `true`\n",
        },
    )
    body = _unwrap(res)
    pid = body["pid"]
    started_at = body["started_at"]
    val_id = body["validation_id"]
    log = body["log"]

    try:
        # Validation task lands in Backlog with the right blocked_on token.
        b = Board.load(cfg.tasks_file)
        loc = b.find(val_id)
        assert loc is not None and loc[0] == "Backlog"
        t = b.get(val_id)
        assert t.blocked_on == [f"pid:{pid}@{started_at}"]
        # Briefing file written under .cc-autopilot/tasks/.
        assert t.briefing
        brief_path = cfg.project_root / t.briefing
        assert brief_path.exists()
        assert "Validate" in brief_path.read_text()
        # Log file ended up in the conventional location.
        assert log.endswith(f"demo-{pid}.log")
        # pipeline_start event was appended.
        from ap2.events import tail

        evts = tail(cfg.events_file, 20)
        kinds = [e["type"] for e in evts]
        assert "pipeline_start" in kinds
        starting = [e for e in evts if e["type"] == "pipeline_start"][-1]
        assert starting["pid"] == pid
        assert starting["validation"] == val_id
        assert starting["name"] == "demo"
    finally:
        # Clean up the spawned process so the test doesn't leak.
        import os, signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_pipeline_task_start_missing_args_errors(cfg):
    res = tools.do_pipeline_task_start(cfg, {"name": "x"})
    assert res.get("isError")
    res = tools.do_pipeline_task_start(
        cfg, {"name": "x", "command": "true", "validation_title": ""}
    )
    assert res.get("isError")


def test_pipeline_task_start_assigns_id_via_locked_board(cfg):
    """The validation task gets a fresh TB-N via _allocate_id, same as
    add_backlog. Two back-to-back launches should produce different ids.
    """
    r1 = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "p1",
            "command": "sleep 30",
            "validation_title": "validate p1",
            "validation_briefing": "x",
        },
    )
    r2 = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "p2",
            "command": "sleep 30",
            "validation_title": "validate p2",
            "validation_briefing": "x",
        },
    )
    body1 = _unwrap(r1)
    body2 = _unwrap(r2)
    assert body1["validation_id"] != body2["validation_id"]

    import os, signal
    for pid in (body1["pid"], body2["pid"]):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


# TB-101: do_task_complete is a thin acknowledgement handler — the daemon-side
# capture in run_task is what actually consumes the structured payload. Pin
# the validation + acknowledgement shape here.


def test_task_complete_requires_status(cfg):
    res = tools.do_task_complete(cfg, {"summary": "missing status"})
    assert res.get("isError"), res
    assert "status" in res["content"][0]["text"].lower()


def test_task_complete_acknowledges(cfg):
    res = tools.do_task_complete(cfg, {
        "status": "complete",
        "commit": "abc12345",
        "summary": "ok",
    })
    body = _unwrap(res)
    assert "task_complete acknowledged" in body["message"]
    assert "complete" in body["message"]


def test_task_complete_in_task_agent_tools_list():
    """Pin: the tool is in TASK_AGENT_TOOLS, not CONTROL_AGENT_TOOLS — task
    agents call it; control/cron/ideation agents don't have a use for it.
    Tool name avoids the `task_*` prefix because Claude Code reserves that
    namespace for built-in TaskCreate/TaskUpdate/TaskList/TaskGet tools."""
    assert "mcp__autopilot__report_result" in tools.TASK_AGENT_TOOLS
    assert "mcp__autopilot__report_result" not in tools.CONTROL_AGENT_TOOLS
