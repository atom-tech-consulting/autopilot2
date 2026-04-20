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


# ---------- run_task invariants (TB-51) ----------


def test_run_task_emits_start_and_complete_events(cfg):
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding(
        "RESULT:\nstatus: complete\ncommit: deadbeef\nsummary: done\n"
    )
    asyncio.run(run_task(cfg, sdk, task))
    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "task_start" in kinds
    assert "task_complete" in kinds
    start = next(e for e in evts if e["type"] == "task_start")
    end = next(e for e in reversed(evts) if e["type"] == "task_complete")
    assert start["task"] == "TB-5"
    assert start["title"] == "Victim"
    assert end["task"] == "TB-5"
    assert end["status"] == "complete"
    assert end["commit"] == "deadbeef"


def test_run_task_does_not_bump_next_task_id(cfg, tmp_path):
    before = (tmp_path / "CLAUDE.md").read_text()
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding("RESULT:\nstatus: complete\nsummary: ok\n")
    asyncio.run(run_task(cfg, sdk, task))
    after = (tmp_path / "CLAUDE.md").read_text()
    assert "TB-10" in after
    assert before == after


def test_run_task_applies_cron_add_on_complete(cfg):
    from ap2.cron import load_jobs

    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding(
        "RESULT:\n"
        "status: complete\n"
        "commit: beefcafe\n"
        "summary: wired it up\n"
        "cron: add name=newjob interval=2h prompt=\"do thing\"\n"
    )
    asyncio.run(run_task(cfg, sdk, task))

    jobs = {j.name: j for j in load_jobs(cfg.cron_file)}
    assert "newjob" in jobs
    assert jobs["newjob"].interval_s == 2 * 3600
    assert jobs["newjob"].prompt == "do thing"
    evts = events.tail(cfg.events_file, 20)
    assert any(
        e["type"] == "cron_proposed" and e.get("name") == "newjob" for e in evts
    )


def test_run_task_skips_cron_on_incomplete(cfg):
    from ap2.cron import load_jobs

    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding(
        "RESULT:\n"
        "status: blocked\n"
        "summary: stuck\n"
        "cron: add name=shouldnotappear interval=1h prompt=\"noop\"\n"
    )
    asyncio.run(run_task(cfg, sdk, task))

    jobs = load_jobs(cfg.cron_file)
    assert not any(j.name == "shouldnotappear" for j in jobs)


def test_run_task_logs_rejected_cron_directive(cfg):
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")
    sdk = _sdk_yielding(
        "RESULT:\n"
        "status: complete\n"
        "summary: tried a bad directive\n"
        "cron: bogus-action name=x\n"
    )
    asyncio.run(run_task(cfg, sdk, task))
    evts = events.tail(cfg.events_file, 20)
    assert any(
        e["type"] == "cron_proposal_rejected" and "bogus-action" in e.get("reason", "")
        for e in evts
    )


def test_run_task_blocked_moves_to_backlog_and_writes_attempts(cfg, tmp_path):
    # Swap the fixture briefing for a real file so _append_attempts can write.
    brief = tmp_path / "brief.md"
    brief.write_text("# Existing\n")
    tasks_text = cfg.tasks_file.read_text().replace(
        "[→ brief](brief.md)", f"[→ brief]({brief.name})"
    )
    cfg.tasks_file.write_text(tasks_text)
    board = Board.load(cfg.tasks_file)
    task = board.get("TB-5")

    sdk = _sdk_yielding(
        "RESULT:\nstatus: blocked\nsummary: needs human input\n"
    )
    asyncio.run(run_task(cfg, sdk, task))

    b2 = Board.load(cfg.tasks_file)
    # max_retries=2 → first failure should park in Backlog, not Frozen.
    assert b2.find("TB-5")[0] == "Backlog"
    text = brief.read_text()
    assert "## Attempts" in text
    assert "blocked" in text
    assert "needs human input" in text
