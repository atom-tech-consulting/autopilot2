"""TB-54: one full daemon._tick iteration with a single Ready task.

No mattermost channels, no cron jobs — the simplest E2E shape. Exercises
`_tick` routing: mm poll short-circuits, cron sweep is a no-op, Ready task
is picked and runs to Complete.
"""
from __future__ import annotations

import asyncio

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, text_respond


def test_single_tick_completes_ready_task(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        text_respond(
            "RESULT:\n"
            "status: complete\n"
            "commit: abc12345\n"
            "summary: did it\n"
            "files_changed: a.py\n"
            "tests_passed: true\n"
        ),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"

    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "task_start" in kinds
    assert "task_complete" in kinds
    assert kinds.index("task_start") < kinds.index("task_complete")

    end = next(e for e in reversed(evts) if e["type"] == "task_complete")
    assert end["task"] == "TB-5"
    assert end["status"] == "complete"
    assert end["commit"] == "abc12345"


def test_single_tick_no_mm_no_cron(e2e_project):
    """With no channels and no cron, neither branch of `_tick` fires events."""
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    sdk.on("## Task\nTB-5", text_respond("RESULT:\nstatus: complete\nsummary: ok\n"))

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 50)
    kinds = [e["type"] for e in evts]
    assert not any(k.startswith("mattermost") for k in kinds)
    assert not any(k.startswith("cron_") for k in kinds)
    assert not cfg.cron_state_file.exists()


def test_single_tick_empty_board_is_a_noop(e2e_project):
    """If Ready is empty and there's nothing else to do, _tick quietly exits."""
    cfg = e2e_project()  # no ready / frozen / cron

    sdk = FakeSDK()
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 50)
    assert evts == []
    board = Board.load(cfg.tasks_file)
    for section in ("Active", "Ready", "Backlog", "Complete", "Frozen"):
        assert list(board.iter_tasks(section)) == []


def test_single_tick_blocked_status_goes_to_backlog(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "Run the thing"))

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        text_respond(
            "RESULT:\nstatus: blocked\nsummary: needs human input\n"
        ),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Backlog"


def test_single_tick_auto_promotes_backlog_when_ready_empty(e2e_project):
    """If Ready is empty, top-of-Backlog is auto-promoted and dispatched."""
    cfg = e2e_project()
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-7", title="First backlog item")
    board.add("Backlog", task_id="TB-8", title="Second backlog item")
    board.save()

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-7",
        text_respond(
            "RESULT:\nstatus: complete\ncommit: abc12345\nsummary: did it\n"
        ),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-7")[0] == "Complete"
    assert board.find("TB-8")[0] == "Backlog"

    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "backlog_auto_promoted" in kinds
    assert kinds.index("backlog_auto_promoted") < kinds.index("task_start")
    promo = next(e for e in evts if e["type"] == "backlog_auto_promoted")
    assert promo["task"] == "TB-7"
