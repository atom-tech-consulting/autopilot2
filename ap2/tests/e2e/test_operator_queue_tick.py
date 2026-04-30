"""TB-131: daemon `_tick` drains the operator queue as its first stage.

The point: by the time the per-tick task / cron / ideation stages read the
board, every queued operator op has already landed. Concretely we queue an
add_backlog and a Ready task in the same project, then run one tick. After
the tick the new TB-N is on the board (drained) AND the Ready task ran to
Complete (downstream stages observed the post-drain board).
"""
from __future__ import annotations

import asyncio

from ap2 import events, tools
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, tool_call_respond


def test_tick_drains_operator_queue_before_task(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "the task"))

    # Operator queues an add_backlog. ID is pre-allocated synchronously,
    # but TASKS.md is not yet mutated.
    body = tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": "queued by operator"}
    )
    import json
    qbody = json.loads(body["content"][0]["text"])
    queued_id = qbody["task_id"]
    # Pre-tick: queued task is NOT on the board yet.
    assert Board.load(cfg.tasks_file).find(queued_id) is None

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        tool_call_respond(
            "report_result",
            {
                "status": "complete",
                "commit": "deadbeef",
                "summary": "ran with drained queue",
                "files_changed": "",
                "tests_passed": "true",
            },
        ),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # Queued op was drained and landed in Backlog.
    assert board.find(queued_id)[0] == "Backlog"
    # The Ready task's stage observed the post-drain board and ran to Complete.
    assert board.find("TB-5")[0] == "Complete"

    # The drain-side audit event landed in events.jsonl.
    evts = events.tail(cfg.events_file, 30)
    drained = [e for e in evts if e["type"] == "operator_queue_drained"]
    assert len(drained) == 1
    assert drained[0]["applied"] == 1


def test_tick_with_empty_queue_is_a_noop_and_safe(e2e_project):
    """No queued ops → drain returns 0, no event spam, the rest of the
    tick proceeds normally. Smoke-tests the empty-queue path so the
    drain step doesn't accidentally regress when queue work is absent.
    """
    cfg = e2e_project()

    sdk = FakeSDK()
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 30)
    # No drained event when queue was empty (the drain stage is silent).
    assert not any(e["type"] == "operator_queue_drained" for e in evts)
    assert not any(e["type"] == "operator_queue_drain_error" for e in evts)
