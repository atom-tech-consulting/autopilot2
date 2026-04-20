"""Tests for orphan recovery, SDK query timeout, and retry bounds in run_task.

The SDK is stubbed with a lightweight fake so these tests don't need the real
claude_agent_sdk installed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events, retry
from ap2.board import Board
from ap2.config import Config
from ap2.daemon import _recover_orphans, run_task


# ---------- fixtures ----------


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-5** **Victim** `#x` — Will be run. [→ brief](brief.md)\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    # Keep retries low so the retry-exhaustion test is quick.
    import os
    os.environ["AP2_MAX_RETRIES"] = "2"
    os.environ["AP2_TASK_TIMEOUT_S"] = "60"
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    yield cfg_
    os.environ.pop("AP2_MAX_RETRIES", None)
    os.environ.pop("AP2_TASK_TIMEOUT_S", None)


# ---------- fake SDK ----------


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


def _make_sdk(behavior):
    """Build a stub with the minimum surface run_task uses.

    `behavior` is a callable that returns an async iterator (or raises).
    """
    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    def _query(prompt, options):  # noqa: ARG001
        return behavior()

    return SimpleNamespace(query=_query, ClaudeAgentOptions=_Options)


def _sdk_yielding(text: str):
    async def gen():
        yield _FakeMsg(text)

    return _make_sdk(gen)


def _sdk_hanging(sleep_s: float = 10.0):
    async def gen():
        await asyncio.sleep(sleep_s)
        yield _FakeMsg("(unreachable)")

    return _make_sdk(gen)


def _sdk_raising(exc: Exception):
    async def gen():
        if False:  # make it a generator
            yield None
        raise exc

    return _make_sdk(gen)


# ---------- orphan recovery ----------


def test_recover_orphans_moves_active_to_ready(cfg, tmp_path):
    board = Board.load(cfg.tasks_file)
    board.move("TB-5", "Active")
    board.save()
    assert board.find("TB-5")[0] == "Active"

    _recover_orphans(cfg)

    b2 = Board.load(cfg.tasks_file)
    assert b2.find("TB-5")[0] == "Ready"
    evts = events.tail(cfg.events_file, 10)
    assert any(e["type"] == "orphan_recovery" and e["task"] == "TB-5" for e in evts)


def test_recover_orphans_noop_when_no_active(cfg):
    _recover_orphans(cfg)
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Ready"
    evts = events.tail(cfg.events_file, 10)
    assert not any(e["type"] == "orphan_recovery" for e in evts)


# ---------- timeout ----------


def test_task_timeout_moves_to_backlog(cfg, monkeypatch):
    cfg.task_timeout_s = 1  # force fast timeout
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    sdk = _sdk_hanging(sleep_s=5)
    asyncio.run(run_task(cfg, sdk, task))

    b2 = Board.load(cfg.tasks_file)
    # After 1 failure (max_retries=2), task should be in Backlog, not Frozen.
    assert b2.find("TB-5")[0] == "Backlog"
    evts = events.tail(cfg.events_file, 20)
    assert any(e["type"] == "task_timeout" and e["task"] == "TB-5" for e in evts)
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1


# ---------- retry bound ----------


def test_retry_exhaustion_moves_to_frozen(cfg):
    cfg.task_timeout_s = 1
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    # max_retries=2: first failure → Backlog, second → Frozen.
    sdk = _sdk_raising(RuntimeError("boom"))

    # Run once. Task goes to Backlog.
    asyncio.run(run_task(cfg, sdk, task))
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Backlog"
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1

    # Move back to Ready (daemon would pick it off Ready next tick) and try again.
    from ap2.tools import do_board_edit
    do_board_edit(cfg, {"action": "move_to_ready", "task_id": "TB-5"})
    task2 = Board.load(cfg.tasks_file).get("TB-5")
    asyncio.run(run_task(cfg, sdk, task2))

    b2 = Board.load(cfg.tasks_file)
    assert b2.find("TB-5")[0] == "Frozen"
    evts = events.tail(cfg.events_file, 30)
    assert any(e["type"] == "retry_exhausted" and e["task"] == "TB-5" for e in evts)


def test_successful_run_resets_attempt_counter(cfg):
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    # Pre-seed one prior failed attempt.
    retry.bump_attempt(cfg.retry_state_file, "TB-5")
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 1

    sdk = _sdk_yielding(
        "RESULT:\nstatus: complete\ncommit: abc12345\nsummary: did it\n"
    )
    asyncio.run(run_task(cfg, sdk, task))

    b2 = Board.load(cfg.tasks_file)
    assert b2.find("TB-5")[0] == "Complete"
    assert retry.attempt_count(cfg.retry_state_file, "TB-5") == 0
