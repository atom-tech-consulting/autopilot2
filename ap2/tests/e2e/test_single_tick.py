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


# TB-46: empty-board auto-ideation

_TEST_IDEATION_PROMPT = "Propose new tasks."


def test_tick_runs_ideation_when_board_is_empty(e2e_project, monkeypatch):
    """Empty board + cooldown elapsed → step-4 fires ap2.ideation._maybe_ideate."""
    import time
    from ap2.cron import save_state

    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "3600")
    cfg = e2e_project(ideation_prompt=_TEST_IDEATION_PROMPT)
    save_state(cfg.cron_state_file, {"ideation": time.time() - 7200})

    sdk = FakeSDK()
    sdk.on("Propose new tasks.", text_respond("proposed 3 tasks"))

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "ideation_empty_board" in kinds
    assert "cron_start" in kinds
    assert "cron_complete" in kinds
    assert kinds.index("ideation_empty_board") < kinds.index("cron_start")


def test_tick_skips_ideation_if_ready_has_work(e2e_project):
    """Ready has a task → board not empty → ideation does NOT fire."""
    cfg = e2e_project(
        ready_task=("TB-5", "Run the thing"),
        ideation_prompt=_TEST_IDEATION_PROMPT,
    )

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        text_respond("RESULT:\nstatus: blocked\nsummary: needs human\n"),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "ideation_empty_board" not in kinds
    assert "cron_start" not in kinds


def test_tick_ideation_honors_cooldown(e2e_project, monkeypatch):
    """A recent ideation run in cron_state.json suppresses the trigger."""
    import time
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "3600")
    cfg = e2e_project(ideation_prompt=_TEST_IDEATION_PROMPT)

    from ap2.cron import save_state
    save_state(cfg.cron_state_file, {"ideation": time.time() - 10})

    sdk = FakeSDK()
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 20)
    kinds = [e["type"] for e in evts]
    assert "ideation_empty_board" not in kinds
    assert "cron_start" not in kinds


def test_tick_auto_promote_skips_blocked_backlog_task(e2e_project):
    """Top-of-Backlog has unmet `blocked on:` → daemon picks next eligible."""
    cfg = e2e_project()
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-7",
        title="Depends on TB-99",
        description="(blocked on: TB-99)",
    )
    board.add("Backlog", task_id="TB-8", title="Clear path")
    board.save()

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-8",
        text_respond("RESULT:\nstatus: complete\ncommit: def0\nsummary: ok\n"),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-8")[0] == "Complete"
    # TB-7 remained in Backlog because its blocker is unmet.
    assert board.find("TB-7")[0] == "Backlog"

    evts = events.tail(cfg.events_file, 20)
    promo = next(e for e in evts if e["type"] == "backlog_auto_promoted")
    assert promo["task"] == "TB-8"


def test_tick_first_run_fires_ideation_with_default_prompt(e2e_project, monkeypatch):
    """No project override + no prior cron_state + cooldown elapsed → ideation
    fires using `ap2/ideation.default.md`. Replaces the old
    `skipped_when_no_ideation_cron_configured` test: under the new design
    ideation is always available."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "3600")
    cfg = e2e_project()
    sdk = FakeSDK()
    # The default prompt contains "ideation_state_write" prominently — match
    # on that so we don't tie the test to a less-stable substring.
    sdk.on("ideation_state_write", text_respond("proposed"))
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    kinds = [e["type"] for e in events.tail(cfg.events_file, 20)]
    assert "ideation_empty_board" in kinds
    assert "cron_complete" in kinds
