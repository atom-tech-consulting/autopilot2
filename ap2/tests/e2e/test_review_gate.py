"""E2E for TB-121: ideation-proposed tasks are gated behind operator
review before dispatch.

Two paths exercised end-to-end against the real `_tick`:
  1. A review-gated Backlog task is NOT auto-promoted (the daemon
     ticks past it; the board section stays unchanged).
  2. After `ap2 approve TB-N` runs through the operator queue and the
     daemon drains it, the next tick auto-promotes the task to Ready
     and dispatches it like any other Backlog item.

These pin the load-bearing assertion: ideation's autonomous proposal
pipeline can't reach the dispatch loop without operator action.
"""
from __future__ import annotations

import asyncio
from argparse import Namespace

from ap2 import events, tools
from ap2.board import Board
from ap2.cli import cmd_approve
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, tool_call_respond


def _seed_review_gated(cfg, *, task_id="TB-50", title="ideation proposal"):
    """Seed Backlog with one review-gated task, mimicking ideation."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id=task_id,
        title=title,
        meta={"blocked": "review"},
    )
    board.save()


def test_tick_does_not_promote_review_gated_backlog(e2e_project):
    """A `@blocked:review` task in Backlog stays in Backlog across a
    tick — auto-promotion's `_is_blocker_satisfied("review")` is False.
    """
    cfg = e2e_project()
    _seed_review_gated(cfg, task_id="TB-50")

    sdk = FakeSDK()
    # No "## Task\nTB-50" handler — if the daemon DID try to dispatch
    # it, FakeSDK would error out unhandled.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # Section unchanged — task neither moved nor mutated.
    assert board.find("TB-50")[0] == "Backlog"
    t = board.get("TB-50")
    assert t is not None
    assert t.blocked_on == ["review"]

    # No task lifecycle events (since dispatch never started).
    evts = events.tail(cfg.events_file, n=20)
    kinds = [e["type"] for e in evts]
    assert "task_start" not in kinds
    assert "backlog_auto_promoted" not in kinds


def test_approve_then_tick_promotes_and_dispatches(e2e_project):
    """`ap2 approve TB-N` queues the strip; one tick drains the queue
    AND (in the same tick — drain runs before dispatch) auto-promotes
    the now-ungated task. The dispatch path matches a normal Backlog
    item's lifecycle."""
    cfg = e2e_project()
    _seed_review_gated(cfg, task_id="TB-60", title="now approve me")

    rc = cmd_approve(cfg, Namespace(task_id="TB-60"))
    assert rc == 0
    # CLI didn't drain the queue — TASKS.md is still unchanged at this point.
    raw_pre_tick = cfg.tasks_file.read_text()
    assert "`@blocked:review`" in raw_pre_tick

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-60",
        tool_call_respond(
            "report_result",
            {
                "status": "complete",
                "commit": "facade12",
                "summary": "ran the approved task",
                "files_changed": "",
                "tests_passed": "true",
            },
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # Drain stripped the codespan, auto-promotion picked it up, dispatch
    # ran, completion event landed.
    raw_post_tick = cfg.tasks_file.read_text()
    assert "`@blocked:review`" not in raw_post_tick
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-60")[0] == "Complete"

    evts = events.tail(cfg.events_file, n=20)
    kinds = [e["type"] for e in evts]
    assert "ideation_approved" in kinds
    assert "task_start" in kinds
    assert "task_complete" in kinds
    # Approved before dispatch (drain runs before backlog promotion).
    assert kinds.index("ideation_approved") < kinds.index("task_start")
