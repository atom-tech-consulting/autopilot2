"""TB-55: multi-tick with a `status-report` cron and a Frozen follow-up
that graduates to Complete once the cron sees the pipeline finished.

Scenario:
  * tick 1: cron fires first (pipeline still Ready, no unfreeze),
            pipeline runs and completes.
  * tick 2 at +30s: cron interval (60s) not yet elapsed, no-op.
  * tick 3 at +90s: cron fires again, sees pipeline Complete,
            unfreezes TB-6; `_tick`'s step-3 then picks TB-6 and runs it.

The order in `daemon._tick` is mattermost → cron → next Ready, so TB-6
reaches Complete in the *same* tick where it's unfrozen.
"""
from __future__ import annotations

import asyncio
import json

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick
from ap2.tools import do_board_edit

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg, text_respond, tool_call_respond


def _cron_unfreeze_when_pipeline_done(cfg, pipeline_id: str, follow_up_id: str):
    """Async-gen factory — simulates a cron agent that unfreezes TB-6
    once the pipeline is Complete."""

    async def gen(prompt, options):  # noqa: ARG001
        board = Board.load(cfg.tasks_file)
        pipeline_done = any(
            t.id == pipeline_id and t.section == "Complete"
            for t in board.iter_tasks()
        )
        follow_up = board.find(follow_up_id)
        if pipeline_done and follow_up and follow_up[0] == "Frozen":
            do_board_edit(
                cfg,
                {"action": "move_to_ready", "task_id": follow_up_id},
            )
        yield _FakeMsg("(cron agent done)")

    return gen


def test_multi_tick_cron_unfreezes_follow_up(e2e_project, clock):
    cfg = e2e_project(
        ready_task=("TB-5", "Pipeline"),
        frozen_task=("TB-6", "Follow-up", "TB-5"),
        cron_jobs=[
            {
                "name": "status-report",
                "interval": "60s",
                "prompt": "check pipeline status",
                "max_turns": 5,
            },
        ],
    )

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        tool_call_respond(
            "report_result",
            {
                "status": "complete", "commit": "pipe0001",
                "summary": "pipeline done", "tests_passed": "true",
            },
        ),
    )
    sdk.on(
        "## Task\nTB-6",
        tool_call_respond(
            "report_result",
            {
                "status": "complete", "commit": "foll0002",
                "summary": "follow-up done", "tests_passed": "true",
            },
        ),
    )
    sdk.on(
        "## Control job: status-report",
        _cron_unfreeze_when_pipeline_done(cfg, "TB-5", "TB-6"),
    )

    # --- tick 1: cron fires (pipeline still Ready), then pipeline runs. ---
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"
    assert board.find("TB-6")[0] == "Frozen"  # cron ran before pipeline done
    assert cfg.cron_state_file.exists()
    cron_state_1 = json.loads(cfg.cron_state_file.read_text())
    assert cron_state_1["status-report"] == clock.now()  # type: ignore[attr-defined]

    # --- tick 2 at +30s: interval (60s) not yet elapsed. ---
    clock(30)
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-6")[0] == "Frozen"
    cron_state_2 = json.loads(cfg.cron_state_file.read_text())
    assert cron_state_2["status-report"] == cron_state_1["status-report"]

    # --- tick 3 at +90s: cron fires (>=60s), unfreezes TB-6,
    #     then step-3 picks TB-6 and runs it to Complete in the SAME tick.
    clock(60)
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-6")[0] == "Complete"
    cron_state_3 = json.loads(cfg.cron_state_file.read_text())
    assert cron_state_3["status-report"] > cron_state_1["status-report"]

    # --- event-chain invariants ---
    evts = events.tail(cfg.events_file, 200)
    kinds = [e["type"] for e in evts]
    # Pipeline ran once; follow-up ran once → two task_start entries.
    task_starts = [e for e in evts if e["type"] == "task_start"]
    assert [e["task"] for e in task_starts] == ["TB-5", "TB-6"]
    # Cron fired on tick 1 and tick 3 (not tick 2).
    cron_starts = [e for e in evts if e["type"] == "cron_start"]
    assert len(cron_starts) == 2
    # Ordering: pipeline completes before TB-6 starts.
    pipe_complete = next(
        i for i, e in enumerate(evts)
        if e["type"] == "task_complete" and e["task"] == "TB-5"
    )
    foll_start = next(
        i for i, e in enumerate(evts)
        if e["type"] == "task_start" and e["task"] == "TB-6"
    )
    assert pipe_complete < foll_start


def test_cron_noop_when_pipeline_still_running(e2e_project, clock):
    """Cron agent should NOT unfreeze TB-6 while pipeline is still Ready."""
    cfg = e2e_project(
        ready_task=("TB-5", "Pipeline"),
        frozen_task=("TB-6", "Follow-up", "TB-5"),
        cron_jobs=[
            {"name": "status-report", "interval": "60s",
             "prompt": "check status", "max_turns": 5},
        ],
    )

    sdk = FakeSDK()
    # Pipeline never responds with complete — simulate incomplete.
    sdk.on(
        "## Task\nTB-5",
        tool_call_respond(
            "report_result",
            {"status": "incomplete", "summary": "still working"},
        ),
    )
    sdk.on(
        "## Control job: status-report",
        _cron_unfreeze_when_pipeline_done(cfg, "TB-5", "TB-6"),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    # Pipeline bounced back to Backlog (incomplete); TB-6 should stay Frozen.
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-6")[0] == "Frozen"

    # Even after the cron interval passes and fires again, TB-5 still isn't
    # Complete, so TB-6 stays Frozen.
    clock(90)
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    assert Board.load(cfg.tasks_file).find("TB-6")[0] == "Frozen"
