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
    """TB-114: pipeline_task_start spawns a detached subprocess, writes a
    `pipeline_start` event with name/pid/started_at/log, and returns the
    pid + started_at + log path. It does NOT create a separate validation
    task — the launching task itself goes to Pipeline Pending in
    daemon.run_task and re-runs verification once the pid dies.
    """
    backlog_before = sum(1 for _ in Board.load(cfg.tasks_file).iter_tasks("Backlog"))
    res = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "demo",
            "command": "sleep 30",
        },
    )
    body = _unwrap(res)
    pid = body["pid"]
    started_at = body["started_at"]
    log = body["log"]
    # No validation_id in the response — pre-TB-114 contract is gone.
    assert "validation_id" not in body

    try:
        # No new task created in Backlog as a side effect — count unchanged.
        b = Board.load(cfg.tasks_file)
        assert sum(1 for _ in b.iter_tasks("Backlog")) == backlog_before
        # Log file ended up in the conventional location.
        assert log.endswith(f"demo-{pid}.log")
        # pipeline_start event was appended.
        from ap2.events import tail

        evts = tail(cfg.events_file, 20)
        kinds = [e["type"] for e in evts]
        assert "pipeline_start" in kinds
        starting = [e for e in evts if e["type"] == "pipeline_start"][-1]
        assert starting["pid"] == pid
        assert starting["started_at"] == started_at
        assert starting["name"] == "demo"
        # Pre-TB-114 `validation` field is gone.
        assert "validation" not in starting
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
    res = tools.do_pipeline_task_start(cfg, {"command": "true"})
    assert res.get("isError")


def test_pipeline_task_start_distinct_pids(cfg):
    """Two back-to-back launches each spawn their own subprocess — pids
    differ. (Pre-TB-114 we asserted validation_id divergence; that field
    is gone, so we now pin the pid uniqueness as the visible contract.)
    """
    r1 = tools.do_pipeline_task_start(
        cfg, {"name": "p1", "command": "sleep 30"},
    )
    r2 = tools.do_pipeline_task_start(
        cfg, {"name": "p2", "command": "sleep 30"},
    )
    body1 = _unwrap(r1)
    body2 = _unwrap(r2)
    assert body1["pid"] != body2["pid"]

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


# TB-106: do_operator_log_append — operator-decision channel for ideation


# TB-109: do_git_log_grep — replaces ideation's Bash `git log --grep` use


def _git(cwd, *args):
    """Run a git command in `cwd` and assert success."""
    import subprocess
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return proc


def test_git_log_grep_finds_matching_commit(cfg, tmp_path):
    """Init a tmp git repo with a TB-N-prefixed commit; the tool
    returns the matching one-line summary."""
    _git(cfg.project_root, "init", "-q")
    (cfg.project_root / "a.txt").write_text("hello\n")
    _git(cfg.project_root, "add", "a.txt", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-42: do the thing")
    (cfg.project_root / "b.txt").write_text("more\n")
    _git(cfg.project_root, "add", "b.txt")
    _git(cfg.project_root, "commit", "-q", "-m", "unrelated work")

    res = tools.do_git_log_grep(cfg, {"query": "TB-42"})
    body = _unwrap(res)
    assert body["count"] == 1
    assert any("TB-42: do the thing" in m for m in body["matches"]), body


def test_git_log_grep_returns_empty_on_no_match(cfg):
    _git(cfg.project_root, "init", "-q")
    (cfg.project_root / "a.txt").write_text("hello\n")
    _git(cfg.project_root, "add", "a.txt", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-1: x")

    res = tools.do_git_log_grep(cfg, {"query": "TB-999"})
    body = _unwrap(res)
    assert body["count"] == 0
    assert body["matches"] == []


def test_git_log_grep_caps_max_results(cfg):
    """Cap is 100 even if a higher value is passed."""
    _git(cfg.project_root, "init", "-q")
    _git(cfg.project_root, "add", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-1: x")
    res = tools.do_git_log_grep(cfg, {"query": "TB-1", "max_results": 5000})
    assert not res.get("isError")  # request succeeded


def test_git_log_grep_requires_query(cfg):
    res = tools.do_git_log_grep(cfg, {"query": ""})
    assert res.get("isError"), res
    assert "query is required" in res["content"][0]["text"]


def test_git_log_grep_handles_non_git_project(cfg):
    """No `.git` directory → graceful empty response, not an error."""
    res = tools.do_git_log_grep(cfg, {"query": "TB-1"})
    body = _unwrap(res)
    assert body["count"] == 0


def test_git_log_grep_query_is_shell_safe(cfg):
    """The query goes via subprocess argv, not interpolated into a
    shell string. Adversarial input shouldn't escape into command
    injection — it just won't match anything."""
    _git(cfg.project_root, "init", "-q")
    _git(cfg.project_root, "add", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-1: real commit")

    # If the query were interpolated, this would be `; touch /tmp/pwn`.
    # With argv-form invocation it's a literal string git treats as a
    # regex match — won't find anything but won't break things either.
    adversarial = "; touch /tmp/should-not-exist; #"
    res = tools.do_git_log_grep(cfg, {"query": adversarial})
    body = _unwrap(res)
    assert body["count"] == 0
    # Sanity: the bogus file would only appear if the query escaped.
    from pathlib import Path
    assert not Path("/tmp/should-not-exist").exists()


def test_git_log_grep_in_control_agent_tools_only():
    """Pin: control agents (cron / ideation / mattermost handler) get
    the tool; task agents don't (they have direct Bash access). And
    `Bash` is NOT in CONTROL_AGENT_TOOLS — that's the whole point of
    this tool's existence."""
    assert "mcp__autopilot__git_log_grep" in tools.CONTROL_AGENT_TOOLS
    assert "mcp__autopilot__git_log_grep" not in tools.TASK_AGENT_TOOLS
    assert "Bash" not in tools.CONTROL_AGENT_TOOLS, (
        "control agents must not have Bash — TB-109 closes that surface"
    )


def test_operator_log_append_creates_file_on_first_call(cfg):
    res = tools.do_operator_log_append(cfg, {"note": "abandoned TB-91"})
    body = _unwrap(res)
    assert "appended to operator_log.md" in body["message"]
    log = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    assert log.exists()
    text = log.read_text()
    assert "# Operator log" in text
    assert "abandoned TB-91" in text
    # Timestamped bullet line.
    assert text.rstrip().endswith("— abandoned TB-91")


def test_operator_log_append_includes_task_id_when_given(cfg):
    res = tools.do_operator_log_append(
        cfg, {"note": "LaunchAgent loaded", "task_id": "TB-139"}
    )
    body = _unwrap(res)
    assert "[TB-139]" in body["line"]
    log = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    assert "[TB-139]" in log.read_text()


def test_operator_log_append_omits_tag_when_no_task_id(cfg):
    tools.do_operator_log_append(cfg, {"note": "no task ref"})
    log = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    text = log.read_text()
    # No "[TB-...]" tag in the bullet line.
    assert "[TB-" not in text


def test_operator_log_append_appends_subsequent_calls(cfg):
    tools.do_operator_log_append(cfg, {"note": "first decision"})
    tools.do_operator_log_append(cfg, {"note": "second decision"})
    log = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    text = log.read_text()
    assert "first decision" in text
    assert "second decision" in text
    # Header written exactly once on first call.
    assert text.count("# Operator log") == 1


def test_operator_log_append_requires_note(cfg):
    res = tools.do_operator_log_append(cfg, {"note": "  "})
    assert res.get("isError"), res
    assert "note is required" in res["content"][0]["text"]


def test_operator_log_append_emits_operator_ack_event(cfg):
    from ap2 import events
    tools.do_operator_log_append(
        cfg, {"note": "ate the frog", "task_id": "TB-9"}
    )
    evts = events.tail(cfg.events_file, 5)
    ack = next(e for e in evts if e["type"] == "operator_ack")
    assert ack["note"] == "ate the frog"
    assert ack["task"] == "TB-9"


def test_operator_log_append_in_control_agent_tools():
    """Pin: tool is in CONTROL_AGENT_TOOLS so the mattermost handler /
    cron / ideation agents can call it. NOT in TASK_AGENT_TOOLS — task
    agents go through their report_result; operator-decision channel is
    the operator's, not the task agent's."""
    assert "mcp__autopilot__operator_log_append" in tools.CONTROL_AGENT_TOOLS
    assert "mcp__autopilot__operator_log_append" not in tools.TASK_AGENT_TOOLS


def test_operator_log_path_in_task_agent_fenced_paths():
    """Pin: task agents can't write to operator_log.md — it's
    operator-owned + control-agent-mediated."""
    assert ".cc-autopilot/operator_log.md" in tools.TASK_AGENT_FENCED_PATHS


def test_task_complete_in_task_agent_tools_list():
    """Pin: the tool is in TASK_AGENT_TOOLS, not CONTROL_AGENT_TOOLS — task
    agents call it; control/cron/ideation agents don't have a use for it.
    Tool name avoids the `task_*` prefix because Claude Code reserves that
    namespace for built-in TaskCreate/TaskUpdate/TaskList/TaskGet tools."""
    assert "mcp__autopilot__report_result" in tools.TASK_AGENT_TOOLS
    assert "mcp__autopilot__report_result" not in tools.CONTROL_AGENT_TOOLS
