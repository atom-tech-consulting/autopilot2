"""Tests for the project-wide AP2_VERIFY_CMD regression gate (TB-66).

Six paths covered:
  - verify pass → task lands in Complete (gate is transparent on green)
  - verify fail → task lands in Backlog with a `verification_failed` event
  - verify timeout → exit_code is None, treated as fail
  - gate skipped when AP2_VERIFY_CMD is unset (preserves pre-TB-66 behavior)
  - gate skipped per-task when the task carries `#no-verify`
  - retry budget burns through verify failures → eventual Frozen
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from ap2 import events, retry
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, text_respond


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True, text=True, check=True,
    )


def _git_init(cwd: Path) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "user.name", "Test"], cwd)
    _git(["commit", "--allow-empty", "-m", "init"], cwd)


def _ready_with_complete_result(sdk: FakeSDK, task_id: str) -> None:
    sdk.on(
        f"## Task\n{task_id}",
        text_respond(
            f"RESULT:\nstatus: complete\ncommit: abc12345\n"
            f"summary: implemented {task_id}\nfiles_changed: a.py\n"
        ),
    )


def test_verify_pass_moves_to_complete(e2e_project, monkeypatch):
    """Exit-0 verify command → task proceeds to Complete unchanged."""
    monkeypatch.setenv("AP2_VERIFY_CMD", "true")
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    _ready_with_complete_result(sdk, "TB-5")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"
    evts = events.tail(cfg.events_file, 30)
    # No verification_failed should fire on a green run.
    assert all(e["type"] != "verification_failed" for e in evts)


def test_verify_fail_moves_to_backlog(e2e_project, monkeypatch):
    """Non-zero verify exit → Backlog with a verification_failed event carrying
    the command, exit_code, stderr_tail, and duration_s."""
    monkeypatch.setenv("AP2_VERIFY_CMD", "echo boom 1>&2; false")
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    _ready_with_complete_result(sdk, "TB-5")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Backlog"

    evts = events.tail(cfg.events_file, 30)
    failures = [e for e in evts if e["type"] == "verification_failed"]
    assert len(failures) == 1
    f = failures[0]
    assert f["task"] == "TB-5"
    assert "false" in f["command"]
    assert f["exit_code"] != 0 and f["exit_code"] is not None
    assert "boom" in f["stderr_tail"]
    assert f["duration_s"] >= 0

    # The terminal task_complete event should reflect verification_failed,
    # not "complete" — otherwise diagnose tools (TB-71) would mis-classify.
    completes = [e for e in evts if e["type"] == "task_complete"]
    assert completes and completes[-1]["status"] == "verification_failed"


def test_verify_timeout_treated_as_fail(e2e_project, monkeypatch):
    """A verify command that exceeds AP2_VERIFY_TIMEOUT_S → exit_code=None,
    task moves to Backlog as a verification failure."""
    monkeypatch.setenv("AP2_VERIFY_CMD", "sleep 30")
    monkeypatch.setenv("AP2_VERIFY_TIMEOUT_S", "1")
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    _ready_with_complete_result(sdk, "TB-5")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Backlog"

    evts = events.tail(cfg.events_file, 30)
    failures = [e for e in evts if e["type"] == "verification_failed"]
    assert len(failures) == 1
    assert failures[0]["exit_code"] is None  # timeout sentinel


def test_verify_skipped_when_unset(e2e_project, monkeypatch):
    """Backward-compat regression: with AP2_VERIFY_CMD unset, the gate is a
    no-op and tasks complete the same way they did pre-TB-66.

    The conftest fixture already deletes AP2_VERIFY_CMD; this test pins the
    behavior so a future change that makes the gate run-by-default would
    fail loudly here rather than silently affecting every existing project.
    """
    # Explicit double-clear in case some upstream sets it.
    monkeypatch.delenv("AP2_VERIFY_CMD", raising=False)
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    _ready_with_complete_result(sdk, "TB-5")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"
    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "verification_failed" for e in evts)


def test_verify_skipped_for_no_verify_tag(e2e_project, monkeypatch):
    """Per-task opt-out: a `#no-verify` tag bypasses the gate even when
    AP2_VERIFY_CMD is set to a command that would otherwise fail."""
    monkeypatch.setenv("AP2_VERIFY_CMD", "false")
    cfg = e2e_project()
    # Seed Ready with a task carrying the opt-out tag. We bypass the e2e
    # factory's ready_task helper because it doesn't take tags.
    board = Board.load(cfg.tasks_file)
    board.add("Ready", task_id="TB-5", title="Doc-only change",
              tags=["#no-verify"])
    board.save()

    sdk = FakeSDK()
    _ready_with_complete_result(sdk, "TB-5")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"
    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "verification_failed" for e in evts)


def test_cli_no_verify_flag_writes_tag(e2e_project):
    """`ap2 add --no-verify` plumbs through to a `#no-verify` tag on the
    rendered task line, so the runtime check (`"#no-verify" in task.tags`)
    actually finds it after a daemon round-trip."""
    from argparse import Namespace
    from ap2.cli import cmd_add

    cfg = e2e_project()
    args = Namespace(
        title="docs-only change",
        section="Backlog",
        tags=None,
        description="",
        briefing_file=None,
        no_verify=True,
    )
    rc = cmd_add(cfg, args)
    assert rc == 0

    board = Board.load(cfg.tasks_file)
    backlog = list(board.iter_tasks("Backlog"))
    assert len(backlog) == 1
    assert "#no-verify" in backlog[0].tags


def test_verify_failure_burns_retry_budget(e2e_project, monkeypatch):
    """Three consecutive verify failures burn the retry budget → task moves
    to Frozen with `retry_exhausted`. Pins the routing through
    `_handle_failure` so verify failures aren't a special low-cost path."""
    monkeypatch.setenv("AP2_VERIFY_CMD", "false")
    monkeypatch.setenv("AP2_MAX_RETRIES", "3")
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    _ready_with_complete_result(sdk, "TB-5")

    # Three ticks, each: Backlog → auto-promote → Ready → Active → verify
    # fails → Backlog (attempts++). On the third failure, attempts == 3 ==
    # max_retries → move_to_frozen + retry_exhausted event.
    for _ in range(3):
        asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Frozen"

    evts = events.tail(cfg.events_file, 60)
    assert any(
        e["type"] == "retry_exhausted" and e["task"] == "TB-5"
        for e in evts
    )
    # Counter is bumped on every failure including the one that triggered Frozen.
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 3
