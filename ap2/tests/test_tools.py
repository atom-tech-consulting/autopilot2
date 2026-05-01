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


_DEFAULT_BRIEFING = (
    "# Brand new\n\n"
    "## Goal\n\nDoes a thing.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n"
)


def test_board_edit_add_ready_assigns_id(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_ready", "title": "Brand new", "tags": ["#auto"],
            # TB-135: briefing payload is now required for every add_*.
            "briefing": _DEFAULT_BRIEFING,
        },
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
    """TB-132: blocked_on now lands on the task line as a `@blocked:<csv>`
    codespan (in `meta`), not as a `(blocked on: ...)` clause baked into
    the description prose. The blocker semantic is identical
    (`Task.blocked_on` returns the same tokens) — what changed is where
    the parser reads them from."""
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_ready", "title": "Waiter", "blocked_on": "TB-5",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]
    # Description prose is no longer the blocker carrier — TB-132 ended
    # the regex-on-description failure mode (TB-121's prose collision).
    assert "blocked on" not in t.description.lower()


def test_board_edit_add_backlog_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog", "title": "Waiter", "blocked_on": "TB-5",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]
    assert "blocked on" not in t.description.lower()


def test_board_edit_add_frozen_still_honors_blocked_on(cfg):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_frozen", "title": "Waiter", "blocked_on": "TB-5",
            "briefing": _DEFAULT_BRIEFING,
        },
    )
    body = _unwrap(res)
    b = Board.load(cfg.tasks_file)
    t = b.get(body["task_id"])
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]
    assert "blocked on" not in t.description.lower()


# ---------------------------------------------------------------------------
# TB-142 (TB-121 cross-ref): `approve` action on do_board_edit. The idle-path
# entry shared with the queue-routed `_apply_operator_op` for `op="approve"`.


def test_board_edit_approve_strips_review_codespan(cfg):
    """`do_board_edit({"action":"approve",...})` strips the `@blocked:review`
    codespan from a Backlog task so the task is dispatchable."""
    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-400",
        title="ideation gated",
        meta={"blocked": "review"},
    )
    b.save()

    res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-400"}
    )
    body = _unwrap(res)
    assert body["task_id"] == "TB-400"
    assert body["section"] == "Backlog"

    t = Board.load(cfg.tasks_file).get("TB-400")
    assert t is not None
    assert "blocked" not in t.meta
    assert t.blocked_on == []


def test_board_edit_approve_emits_ideation_approved_event(cfg):
    from ap2 import events

    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-401",
        title="audit me",
        meta={"blocked": "review"},
    )
    b.save()

    tools.do_board_edit(cfg, {"action": "approve", "task_id": "TB-401"})
    evts = events.tail(cfg.events_file, 5)
    approved = [e for e in evts if e["type"] == "ideation_approved"]
    assert len(approved) == 1
    assert approved[0]["task"] == "TB-401"


def test_board_edit_approve_preserves_other_blockers(cfg):
    """Only the `review` token is stripped — sibling TB-N blockers stay."""
    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-402",
        title="multi",
        meta={"blocked": "TB-5,review"},
    )
    b.save()

    tools.do_board_edit(cfg, {"action": "approve", "task_id": "TB-402"})
    t = Board.load(cfg.tasks_file).get("TB-402")
    assert t is not None
    assert t.meta.get("blocked") == "TB-5"
    assert t.blocked_on == ["TB-5"]


def test_board_edit_approve_strips_legacy_description_prose(cfg):
    """Pre-TB-132 transition: tasks authored before the codespan format
    landed may still carry `(blocked on: review)` as description prose.
    Approve scrubs it so the rendered line stays tidy."""
    b = Board.load(cfg.tasks_file)
    b.add(
        "Backlog",
        task_id="TB-403",
        title="legacy",
        description="legacy ideation task (blocked on: review)",
    )
    b.save()

    tools.do_board_edit(cfg, {"action": "approve", "task_id": "TB-403"})
    t = Board.load(cfg.tasks_file).get("TB-403")
    assert t is not None
    assert "blocked on: review" not in t.description.lower()


def test_board_edit_approve_requires_task_id(cfg):
    res = tools.do_board_edit(cfg, {"action": "approve"})
    assert res.get("isError")
    assert "task_id" in res["content"][0]["text"]


def test_board_edit_approve_rejects_unknown_task(cfg):
    res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-99999"}
    )
    assert res.get("isError")
    assert "not on board" in res["content"][0]["text"]


def test_board_edit_approve_idempotent_on_unblocked_task(cfg):
    """Already-approved task: approve is a no-op (modulo render). Useful
    so a second `ap2 approve TB-N` doesn't corrupt the line."""
    b = Board.load(cfg.tasks_file)
    b.add("Backlog", task_id="TB-404", title="not gated")
    b.save()

    res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": "TB-404"}
    )
    body = _unwrap(res)
    assert body["task_id"] == "TB-404"
    t = Board.load(cfg.tasks_file).get("TB-404")
    assert t is not None
    assert t.blocked_on == []


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


def test_operator_queue_jsonl_in_task_agent_fenced_paths():
    """TB-143: `operator_queue.jsonl` lives in TASK_AGENT_FENCED_PATHS
    for defense in depth — prompt-header reminder + SDK rejects
    `Edit`/`Write`. The TB-141 false-positive (operator `ap2 add`
    mid-run tripping TB-110's snapshot check) is now solved by
    `rollback._VIOLATION_CHECK_EXCLUDED_PATHS`, which exempts the
    path from the post-hoc snapshot diff while keeping it in the
    fence list."""
    assert ".cc-autopilot/operator_queue.jsonl" in tools.TASK_AGENT_FENCED_PATHS


def test_task_complete_in_task_agent_tools_list():
    """Pin: the tool is in TASK_AGENT_TOOLS, not CONTROL_AGENT_TOOLS — task
    agents call it; control/cron/ideation agents don't have a use for it.
    Tool name avoids the `task_*` prefix because Claude Code reserves that
    namespace for built-in TaskCreate/TaskUpdate/TaskList/TaskGet tools."""
    assert "mcp__autopilot__report_result" in tools.TASK_AGENT_TOOLS
    assert "mcp__autopilot__report_result" not in tools.CONTROL_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-123: cron_propose — task-agent MCP tool, replaces the JSON-stringified
# `cron=` arg on `report_result`. Validates four required string args, emits
# a `cron_proposed` event with all four fields, and (when run via the daemon)
# stamps `proposed_by_task` from the contextvar plumb. cron.yaml is NOT
# mutated — proposals are queued for operator review.


def test_cron_propose_emits_event_with_four_fields(cfg):
    res = tools.do_cron_propose(cfg, {
        "name": "x",
        "schedule": "1h",
        "prompt": "do x",
        "rationale": "y",
    })
    body = _unwrap(res)
    assert body["name"] == "x"
    assert body["schedule"] == "1h"

    from ap2.events import tail

    evts = tail(cfg.events_file, 5)
    proposals = [e for e in evts if e["type"] == "cron_proposed"]
    assert len(proposals) == 1
    p = proposals[0]
    assert p["name"] == "x"
    assert p["schedule"] == "1h"
    assert p["prompt"] == "do x"
    assert p["rationale"] == "y"


def test_cron_propose_does_not_mutate_cron_yaml(cfg):
    """The whole point of the proposal layer (vs. control agents'
    `cron_edit`) — task agents queue, operator promotes."""
    from ap2.cron import load_jobs

    tools.do_cron_propose(cfg, {
        "name": "shouldnotappear",
        "schedule": "1h",
        "prompt": "noop",
        "rationale": "test that nothing lands",
    })
    assert load_jobs(cfg.cron_file) == []


def test_cron_propose_requires_each_field(cfg):
    """Missing name / schedule / prompt / rationale → error, no event."""
    from ap2.events import tail

    base = {
        "name": "x", "schedule": "1h", "prompt": "p", "rationale": "r",
    }
    for missing in ("name", "schedule", "prompt", "rationale"):
        args = dict(base)
        args[missing] = ""
        res = tools.do_cron_propose(cfg, args)
        assert res.get("isError"), (missing, res)
        assert missing in res["content"][0]["text"]
    # No `cron_proposed` events leaked from the rejected calls.
    evts = tail(cfg.events_file, 10)
    assert not any(e["type"] == "cron_proposed" for e in evts)


def test_cron_propose_uses_contextvar_for_proposed_by_task(cfg):
    """When `tools._task_id_ctx` is set (the daemon's plumb during
    run_task), the emitted event carries `proposed_by_task=<TB-id>`.
    Outside that scope (this unit test, by default) the field is omitted.
    """
    from ap2.events import tail

    # Default: contextvar unset → no proposed_by_task in event.
    tools.do_cron_propose(cfg, {
        "name": "no-ctx", "schedule": "1h",
        "prompt": "p", "rationale": "r",
    })
    evts = tail(cfg.events_file, 5)
    p = next(e for e in evts if e.get("name") == "no-ctx")
    assert "proposed_by_task" not in p

    # Within a contextvar set → field present and correct.
    token = tools._task_id_ctx.set("TB-42")
    try:
        tools.do_cron_propose(cfg, {
            "name": "with-ctx", "schedule": "1h",
            "prompt": "p", "rationale": "r",
        })
    finally:
        tools._task_id_ctx.reset(token)
    evts = tail(cfg.events_file, 10)
    p = next(e for e in evts if e.get("name") == "with-ctx")
    assert p["proposed_by_task"] == "TB-42"


def test_cron_propose_in_task_agent_tools_only():
    """Pin: cron_propose is task-agent only — control agents have
    `cron_edit` (direct mutation) and don't need the proposal layer.
    Symmetric privilege split: task agents propose, control agents
    promote (or operator promotes via review)."""
    assert "mcp__autopilot__cron_propose" in tools.TASK_AGENT_TOOLS
    assert "mcp__autopilot__cron_propose" not in tools.CONTROL_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-134: do_board_edit / do_operator_queue_append reject multi-line input.
#
# The MCP-driven path (ideation, MM handler, future operator-queue ops)
# needs the same gate as the CLI — otherwise an MCP caller can still
# write a multi-line task line into TASKS.md and break the line-oriented
# parser. _err / isError lets the calling agent retry with a rephrasing.


def test_board_edit_rejects_newline_in_description(cfg, tmp_path):
    """do_board_edit({description: 'a\\nb'}) → isError; nothing landed
    on the board, no briefing file under .cc-autopilot/tasks."""
    tasks_dir = tmp_path / ".cc-autopilot" / "tasks"
    before_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    before_tasks = (tmp_path / "TASKS.md").read_text()

    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "valid", "description": "a\nb"},
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "single line" in msg
    assert "briefing" in msg  # nudge, not a silent auto-collapse
    # Board untouched.
    assert (tmp_path / "TASKS.md").read_text() == before_tasks
    after_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    assert after_briefings == before_briefings


def test_board_edit_rejects_newline_in_description_add_ready(cfg, tmp_path):
    """add_ready hits the same gate as add_backlog."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_ready", "title": "valid", "description": "a\nb"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_rejects_newline_in_description_add_frozen(cfg, tmp_path):
    """add_frozen hits the same gate."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_frozen", "title": "valid", "description": "a\nb"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_rejects_carriage_return_in_description(cfg, tmp_path):
    """\\r is the same hazard — reject with the same message."""
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "valid", "description": "a\rb"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


def test_board_edit_rejects_newline_in_title(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "title with\nnewline"},
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


def test_board_edit_rejects_newline_in_tag(cfg, tmp_path):
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "valid",
            "tags": ["#cli", "#bro\nken"],
        },
    )
    assert res.get("isError")
    assert "single line" in res["content"][0]["text"]


def test_board_edit_accepts_single_line_description(cfg):
    """Regression: single-line descriptions still go through."""
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog", "title": "ok",
            "description": "one line", "briefing": _DEFAULT_BRIEFING,
        },
    )
    assert not res.get("isError"), res


def test_operator_queue_append_rejects_newline_in_description(cfg, tmp_path):
    """The CLI `ap2 add` path now routes through do_operator_queue_append
    (TB-131); this is the gate that protects MM-handler + CLI alike."""
    before = (tmp_path / "TASKS.md").read_text()
    queue_path = tmp_path / ".cc-autopilot" / "operator_queue.jsonl"
    before_queue = queue_path.read_text() if queue_path.exists() else ""

    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "valid", "description": "a\nb"},
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "single line" in msg
    assert "briefing" in msg
    # Board untouched, nothing queued.
    assert (tmp_path / "TASKS.md").read_text() == before
    after_queue = queue_path.read_text() if queue_path.exists() else ""
    assert after_queue == before_queue


# ---------------------------------------------------------------------------
# TB-135: do_board_edit / do_operator_queue_append require an explicit
# briefing payload for every add_* op. The skeleton-template auto-fill that
# used to land for add_backlog is gone — a briefing whose `## Verification`
# was just a `(additional shell or prose bullets)` placeholder bypassed the
# per-task verifier (TB-131 hit this on 2026-04-30, "passed" on regression
# gate alone with zero scope-specific scoring). MCP-driven callers (ideation,
# MM handler) already construct the payload, so the gate doesn't break them;
# CLI authorship is now the operator's responsibility (cmd_add tests below).


def test_board_edit_add_backlog_requires_briefing(cfg, tmp_path):
    """Empty/missing briefing on add_backlog → isError; nothing landed
    on the board, no briefing file under .cc-autopilot/tasks."""
    tasks_dir = tmp_path / ".cc-autopilot" / "tasks"
    before_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    before_tasks = (tmp_path / "TASKS.md").read_text()

    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "needs briefing", "briefing": ""},
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "briefing is required" in msg
    # Board untouched, no briefing file written.
    assert (tmp_path / "TASKS.md").read_text() == before_tasks
    after_briefings = (
        sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else []
    )
    assert after_briefings == before_briefings


def test_board_edit_add_ready_requires_briefing(cfg, tmp_path):
    """Same gate fires on add_ready — pre-TB-135 only add_backlog
    auto-filled, but the new requirement covers every add_* action."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_ready", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_add_frozen_requires_briefing(cfg, tmp_path):
    """add_frozen also gated. Operators sometimes seed Frozen with
    superseded ideas; the briefing requirement prevents the same
    placeholder-verifier hole from showing up there."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg,
        {"action": "add_frozen", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_board_edit_add_with_briefing_text_succeeds(cfg, tmp_path):
    """Daemon-internal callers (ideation, MM handler) construct the
    briefing payload themselves — they're unaffected by TB-135 as long
    as they pass a non-empty `briefing`. Pin the happy path.
    """
    body = (
        "# Real briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "ideation-style", "briefing": body},
    )
    out = _unwrap(res)
    assert out["task_id"].startswith("TB-")
    # Briefing bytes round-trip into .cc-autopilot/tasks/<slug>.md.
    brief_path = cfg.project_root / out["briefing_path"]
    assert brief_path.exists()
    assert brief_path.read_text() == body


def test_board_edit_non_empty_briefing_payload_unaffected_for_daemon_callers(cfg):
    """TB-135 explicit pin: passing a non-empty `briefing` text payload
    still succeeds for every add_* op so daemon-internal callers
    (ideation, MM handler, operator-queue drain reconstructing add_*
    ops) keep working. The new requirement only rejects empty/missing
    briefing — non-empty briefings on add_ready / add_backlog /
    add_frozen all land normally.

    This is the symmetric happy-path companion to the three
    `*_requires_briefing` tests above: they prove empty briefings are
    rejected; this one proves non-empty briefings still go through.
    """
    body = (
        "# Daemon-built briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    for action, expected_section in (
        ("add_ready", "Ready"),
        ("add_backlog", "Backlog"),
        ("add_frozen", "Frozen"),
    ):
        res = tools.do_board_edit(
            cfg,
            {
                "action": action,
                "title": f"daemon-style {action}",
                "briefing": body,
            },
        )
        out = _unwrap(res)
        # TB-N issued, task lands in the expected section, briefing
        # round-trips to disk under .cc-autopilot/tasks/.
        assert out["task_id"].startswith("TB-"), (action, out)
        brief_path = cfg.project_root / out["briefing_path"]
        assert brief_path.exists(), (action, out)
        assert brief_path.read_text() == body, action
        b = Board.load(cfg.tasks_file)
        assert b.find(out["task_id"])[0] == expected_section, action


def test_operator_queue_append_non_empty_briefing_payload_succeeds(cfg, tmp_path):
    """TB-135 happy-path companion at the operator-queue layer (the path
    `ap2 add` and `@claude-bot add ...` both route through). Daemon
    callers (MM handler, future ideation operator-queue use) pass a
    real briefing payload; the queue accepts every add_* op and
    materializes the briefing under .cc-autopilot/tasks/."""
    body = (
        "# MM-style briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    for action in ("add_ready", "add_backlog", "add_frozen"):
        res = tools.do_operator_queue_append(
            cfg,
            {
                "op": action,
                "title": f"queued {action}",
                "briefing": body,
            },
        )
        out = _unwrap(res)
        assert out["task_id"].startswith("TB-"), (action, out)
    queue_path = tmp_path / ".cc-autopilot" / "operator_queue.jsonl"
    assert queue_path.exists()
    # Three records queued — one per add_* action.
    lines = [
        ln for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 3


def test_operator_queue_append_add_backlog_requires_briefing(cfg, tmp_path):
    """The CLI `ap2 add` and MM-handler `operator_queue_append` paths both
    route through here; the gate must fire BEFORE TB-N pre-allocation so
    a rejected add doesn't leak a hole in the TB-N sequence (the bump of
    CLAUDE.md's `Next task ID` happens inside `_allocate_id` and is not
    reversible)."""
    before_claude = (tmp_path / "CLAUDE.md").read_text()
    queue_path = tmp_path / ".cc-autopilot" / "operator_queue.jsonl"
    before_queue = queue_path.read_text() if queue_path.exists() else ""

    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "needs briefing"},
    )

    assert res.get("isError")
    msg = res["content"][0]["text"]
    assert "briefing is required" in msg
    # CLAUDE.md untouched (no TB-N allocated for the rejected add).
    assert (tmp_path / "CLAUDE.md").read_text() == before_claude
    # Nothing queued.
    after_queue = queue_path.read_text() if queue_path.exists() else ""
    assert after_queue == before_queue


def test_operator_queue_append_add_ready_requires_briefing(cfg):
    res = tools.do_operator_queue_append(
        cfg, {"op": "add_ready", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]


def test_operator_queue_append_add_frozen_requires_briefing(cfg):
    res = tools.do_operator_queue_append(
        cfg, {"op": "add_frozen", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]


def test_operator_queue_append_add_with_briefing_text_succeeds(cfg, tmp_path):
    """MM-handler / ideation pass the briefing as a payload field. Pin
    the happy path: the queue gets one record, the briefing bytes land
    on disk under .cc-autopilot/tasks/."""
    body = (
        "# MM-handler briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "mm-style", "briefing": body},
    )
    out = _unwrap(res)
    assert out["task_id"].startswith("TB-")
    queue_path = tmp_path / ".cc-autopilot" / "operator_queue.jsonl"
    assert queue_path.exists()
    # Briefing bytes round-trip into .cc-autopilot/tasks/<slug>.md.
    slug_files = sorted(
        p for p in (tmp_path / ".cc-autopilot" / "tasks").iterdir()
        if p.suffix == ".md"
    )
    assert any(p.read_text() == body for p in slug_files)


# ---------------------------------------------------------------------------
# TB-141: _allocate_id is now pure — no CLAUDE.md write. The bump is
# deferred to drain_operator_queue (for the queue path) or done by the
# caller (for the do_board_edit path). These tests pin the new contract:
# operator_queue_append doesn't touch CLAUDE.md, and back-to-back appends
# allocate sequential TB-N's via the queue file's preallocated_task_id.


def test_allocate_id_does_not_write_claude_md(cfg, tmp_path):
    """TB-141: `_allocate_id` is pure. Pre-TB-141 it bumped CLAUDE.md
    in-place; that synchronous mutation triggered TB-110 fenced-file
    violations on whichever task was in flight when an operator ran
    `ap2 add`. Now the bump is deferred to drain (queue path) or done
    explicitly by the caller (do_board_edit path).
    """
    claude_md = tmp_path / "CLAUDE.md"
    pre_text = claude_md.read_text()
    pre_mtime = claude_md.stat().st_mtime_ns

    from ap2.board import Board as _Board
    board = _Board.load(cfg.tasks_file)
    new_id = tools._allocate_id(board, cfg)

    # ID looks right.
    assert new_id.startswith("TB-")
    # CLAUDE.md is byte-identical and the mtime didn't budge.
    assert claude_md.read_text() == pre_text
    assert claude_md.stat().st_mtime_ns == pre_mtime


def test_operator_queue_append_does_not_write_claude_md(cfg, tmp_path):
    """TB-141 regression pin at the public API layer. The whole point
    of moving the bump to drain is that an `ap2 add` issued during a
    task run no longer mutates a fenced file. CLAUDE.md must be
    byte-identical after a successful append."""
    claude_md = tmp_path / "CLAUDE.md"
    pre_text = claude_md.read_text()
    pre_mtime = claude_md.stat().st_mtime_ns

    body = (
        "# briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "deferred", "briefing": body},
    )
    out = _unwrap(res)
    # ID was allocated and the queue gained a record …
    assert out["task_id"].startswith("TB-")
    # … but CLAUDE.md is untouched.
    assert claude_md.read_text() == pre_text
    assert claude_md.stat().st_mtime_ns == pre_mtime


def test_two_back_to_back_queue_appends_allocate_sequential_ids(cfg, tmp_path):
    """TB-141: with `_allocate_id` pure, sequential allocations rely on
    the queue file as the cross-call source of truth (CLAUDE.md is no
    longer bumped synchronously to disambiguate). The second append
    must read the first's `preallocated_task_id` from the queue and
    return id + 1 — without touching CLAUDE.md.
    """
    claude_md = tmp_path / "CLAUDE.md"
    pre_text = claude_md.read_text()

    body = (
        "# briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    r1 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "first", "briefing": body},
    ))
    r2 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "second", "briefing": body},
    ))
    n1 = int(r1["task_id"][3:])
    n2 = int(r2["task_id"][3:])
    assert n2 == n1 + 1, (r1, r2)
    # Neither call wrote CLAUDE.md (deferred to drain).
    assert claude_md.read_text() == pre_text


def test_drain_bumps_claude_md_once_to_highest_allocated_plus_one(cfg, tmp_path):
    """TB-141: drain writes CLAUDE.md exactly once at end-of-pass,
    setting `Next task ID` to highest_allocated + 1. Pre-TB-141 the
    bump happened per-add inside `_allocate_id`; the consolidated
    drain-time write is the corollary that keeps CLAUDE.md in sync
    without the in-flight fence collision.
    """
    body = (
        "# briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    r1 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "drain-1", "briefing": body},
    ))
    r2 = _unwrap(tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "drain-2", "briefing": body},
    ))
    highest = max(int(r1["task_id"][3:]), int(r2["task_id"][3:]))

    # Before drain, CLAUDE.md still has the pre-add value.
    pre_drain = (tmp_path / "CLAUDE.md").read_text()
    assert f"TB-{highest}" not in pre_drain  # not yet bumped past it

    summary = tools.drain_operator_queue(cfg)
    assert summary["applied"] == 2

    # After drain, CLAUDE.md's Next task ID is highest + 1 — single
    # write covering all add ops applied this drain pass.
    post_drain = (tmp_path / "CLAUDE.md").read_text()
    assert f"- Next task ID: TB-{highest + 1}" in post_drain


def test_drain_with_no_add_ops_leaves_claude_md_untouched(cfg, tmp_path):
    """The end-of-drain CLAUDE.md write only fires when the drain
    actually preallocated a TB-N. A drain that only applied
    move/unfreeze/delete ops shouldn't touch CLAUDE.md."""
    from ap2.board import Board
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-77", title="frozen one")
    board.save()

    claude_md = tmp_path / "CLAUDE.md"
    pre_text = claude_md.read_text()
    pre_mtime = claude_md.stat().st_mtime_ns

    tools.do_operator_queue_append(
        cfg, {"op": "unfreeze", "task_id": "TB-77"},
    )
    summary = tools.drain_operator_queue(cfg)
    assert summary["applied"] == 1

    assert claude_md.read_text() == pre_text
    assert claude_md.stat().st_mtime_ns == pre_mtime
