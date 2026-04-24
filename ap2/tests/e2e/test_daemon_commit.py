"""Tests for `daemon._commit_state_files` — the daemon's state-file commit hook.

Covers the pure helper behavior (no-git no-op, nothing-to-commit no-op, dirty-
file commit, git-failure logs an event) plus one end-to-end tick that verifies
a task completion produces a state commit in the project's git log.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ap2 import events
from ap2.board import Board
from ap2.daemon import _commit_state_files, _tick

from ap2.tests.e2e._fakes import FakeSDK, crash_respond, text_respond


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True, text=True, check=True,
    )


def _git_init(cwd: Path) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "user.name", "Test"], cwd)
    # Empty seed commit so HEAD exists and diff --cached works.
    _git(["commit", "--allow-empty", "-m", "init"], cwd)


def test_commit_state_files_noop_without_git(e2e_project, tmp_path):
    """When the project isn't a git repo, the helper silently returns."""
    cfg = e2e_project()
    (cfg.project_root / "TASKS.md").write_text("modified content\n")
    _commit_state_files(cfg, "state: TB-1 → Complete")
    evts = events.tail(cfg.events_file, 10)
    assert not any(e["type"] == "state_commit_error" for e in evts)


def test_commit_state_files_no_op_when_clean(e2e_project):
    """Git repo present but nothing modified → no commit, no event."""
    cfg = e2e_project()
    _git_init(cfg.project_root)
    # Commit TASKS.md as baseline.
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "baseline"], cfg.project_root)
    sha_before = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()

    _commit_state_files(cfg, "state: nothing changed")

    sha_after = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    assert sha_before == sha_after
    evts = events.tail(cfg.events_file, 10)
    assert not any(e["type"] == "state_commit_error" for e in evts)


def test_commit_state_files_commits_dirty_files(e2e_project):
    cfg = e2e_project()
    _git_init(cfg.project_root)
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "baseline"], cfg.project_root)

    # Dirty TASKS.md post-baseline.
    (cfg.project_root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n- [x] new\n\n## Frozen\n"
    )
    _commit_state_files(cfg, "state: TB-5 → Complete")

    log = _git(["log", "--oneline", "-1"], cfg.project_root).stdout
    assert "state: TB-5 → Complete" in log
    # Working tree clean after the commit.
    status = _git(["status", "--porcelain"], cfg.project_root).stdout.strip()
    assert status == ""


def test_tick_creates_state_commit_on_task_complete(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))
    _git_init(cfg.project_root)
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "baseline"], cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        text_respond(
            "RESULT:\nstatus: complete\ncommit: abc12345\n"
            "summary: did it\nfiles_changed: a.py\n"
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    log = _git(["log", "--oneline"], cfg.project_root).stdout
    # Most recent commit should be the state commit reflecting TB-5 Complete.
    assert "state: TB-5 → Complete" in log
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"


def test_tick_state_commit_reflects_backlog_on_blocked_task(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))
    _git_init(cfg.project_root)
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "baseline"], cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        text_respond("RESULT:\nstatus: blocked\nsummary: needs human\n"),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    log = _git(["log", "--oneline", "-1"], cfg.project_root).stdout
    assert "state: TB-5 → Backlog" in log


# ---------------------------------------------------------------------------
# Per-task debug dumps (instrumentation for "empty stderr_tail" crashes)

def test_successful_task_cleans_up_debug_dumps(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "t"))

    sdk = FakeSDK()
    sdk.on("## Task\nTB-5",
           text_respond("RESULT:\nstatus: complete\nsummary: ok\n"))

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    leftover = list(debug_dir.glob("*TB-5*")) if debug_dir.exists() else []
    assert leftover == []


def test_implicit_commit_recovery_on_unknown_status(e2e_project):
    """If the agent committed with `<TB-N>:` subject but emitted no RESULT,
    daemon infers complete from HEAD and records a `task_implicit_commit` event."""
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))
    _git_init(cfg.project_root)
    # Pre-stage and commit the agent's "work" with a properly-tagged subject.
    work = cfg.project_root / "work.py"
    work.write_text("# work\n")
    _git(["add", "TASKS.md", "CLAUDE.md", "work.py"], cfg.project_root)
    _git(["commit", "-m", "TB-5: implement the thing"], cfg.project_root)

    sdk = FakeSDK()
    # Agent talks but doesn't emit a RESULT block — parse_result → status=unknown.
    sdk.on(
        "## Task\nTB-5",
        text_respond("All done — committed and tests pass.\n"),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"

    evts = events.tail(cfg.events_file, 30)
    kinds = [e["type"] for e in evts]
    assert "task_implicit_commit" in kinds
    implicit = next(e for e in evts if e["type"] == "task_implicit_commit")
    assert implicit["task"] == "TB-5"
    assert "TB-5" in implicit["subject"]


def test_implicit_commit_recovers_from_sdk_crash(e2e_project):
    """SDK subprocess raises mid-stream after the agent has committed —
    daemon should still recognize task as complete via HEAD inference."""
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))
    _git_init(cfg.project_root)
    work = cfg.project_root / "work.py"
    work.write_text("# work\n")
    _git(["add", "TASKS.md", "CLAUDE.md", "work.py"], cfg.project_root)
    _git(["commit", "-m", "TB-5: real work"], cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        crash_respond(RuntimeError("Command failed with exit code 1")),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"

    evts = events.tail(cfg.events_file, 30)
    implicit = [e for e in evts if e["type"] == "task_implicit_commit"]
    assert implicit, "expected task_implicit_commit on crash recovery"
    assert implicit[-1]["reason"] == "error_recovered"
    # task_error should NOT have fired — the recovery suppressed it.
    assert not any(e["type"] == "task_error" for e in evts)


def test_sdk_crash_without_matching_commit_falls_through_to_error(e2e_project):
    """Same crash, but HEAD doesn't mention the task — daemon must NOT auto-
    promote; falls through to task_error/Backlog as before."""
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))
    _git_init(cfg.project_root)
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "unrelated"], cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        crash_respond(RuntimeError("Command failed with exit code 1")),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Backlog"
    evts = events.tail(cfg.events_file, 30)
    assert any(e["type"] == "task_error" for e in evts)
    assert all(e["type"] != "task_implicit_commit" for e in evts)


def test_implicit_commit_skipped_when_subject_missing_task_id(e2e_project):
    """HEAD's subject without the task ID → no implicit-complete; standard
    failure path takes over (move to Backlog, retry counter bumped)."""
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))
    _git_init(cfg.project_root)
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "unrelated change"], cfg.project_root)

    sdk = FakeSDK()
    sdk.on("## Task\nTB-5", text_respond("oops\n"))
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Backlog"
    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "task_implicit_commit" for e in evts)


def test_blocked_task_keeps_debug_dumps(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "t"))

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        text_respond("RESULT:\nstatus: blocked\nsummary: stuck\n"),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    prompt_dumps = list(debug_dir.glob("*TB-5.prompt.md"))
    stream_dumps = list(debug_dir.glob("*TB-5.stream.jsonl"))
    assert len(prompt_dumps) == 1
    assert len(stream_dumps) == 1
    assert "TB-5" in prompt_dumps[0].read_text()
    stream_lines = [l for l in stream_dumps[0].read_text().splitlines() if l.strip()]
    assert len(stream_lines) >= 1
