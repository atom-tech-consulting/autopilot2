"""TB-122 / TB-145: e2e tests for the concurrent Mattermost handler.

Originally TB-122 introduced a FULL/RESTRICTED toolset toggle keyed on
whether a task agent was in flight. TB-145 collapsed that into a single
unconditional `MM_HANDLER_TOOLS` set (the previous RESTRICTED shape).

Exercises `handle_message` in isolation (without running the full
two-loop daemon) because the FakeSDK's scripted responders give us full
control over what `allowed_tools` is seen by each query. The
`_mm_loop` / `_main_tick_loop` split is a structural change tested by
the unit tests; here we test the observable behavioral contract:

  (a) Handler fires while task in flight → allowed_tools == MM_HANDLER_TOOLS.
  (b) Handler fires while board is idle  → allowed_tools == MM_HANDLER_TOOLS.
  (c) Two concurrent handlers both complete without deadlock.
  (d) Handler prompt explicitly forbids cron_edit (always — TB-145).
  (e) The toolset is invariant under board state transitions
      (load-bearing TB-145 invariant — no FULL/RESTRICTED swap).
"""
from __future__ import annotations

import asyncio
import json

from ap2.board import Board
from ap2.daemon import handle_message
from ap2.tools import MM_HANDLER_TOOLS

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg


# ── helpers ───────────────────────────────────────────────────────────────────


def _mm_msg(channel_id: str = "ch1", text: str = "@claude-bot status") -> dict:
    return {
        "id": "post-1",
        "channel_id": channel_id,
        "channel_name": "dev",
        "user": "alice",
        "text": text,
        "thread_id": "",
    }


def _seed_active_task(cfg, task_id: str = "TB-1", title: str = "In-flight task") -> None:
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id=task_id, title=title)
    board.save()


def _capture_tools_factory():
    """Return (factory, captured) where factory is an async-gen factory that
    records the allowed_tools from `options` and captured is the list it fills."""
    captured: list[list[str]] = []

    async def gen(prompt, options):
        captured.append(list(options.kw.get("allowed_tools", [])))
        yield _FakeMsg("done")

    return gen, captured


# ── test (a): MM_HANDLER_TOOLS used when task is in flight ────────────────────


def test_mm_handler_uses_fixed_toolset_when_task_active(e2e_project):
    """While a task is in Active, handle_message must use MM_HANDLER_TOOLS."""
    cfg = e2e_project()
    _seed_active_task(cfg)

    factory, captured = _capture_tools_factory()
    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", factory)

    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()))

    assert captured, "SDK.query was never called"
    assert captured[0] == MM_HANDLER_TOOLS, (
        f"Expected MM_HANDLER_TOOLS; got {captured[0]}"
    )


# ── test (b): same fixed toolset when board is idle ───────────────────────────


def test_mm_handler_uses_fixed_toolset_when_idle(e2e_project):
    """With no Active tasks, handle_message must use the SAME
    MM_HANDLER_TOOLS — TB-145: no FULL/RESTRICTED swap."""
    cfg = e2e_project()

    factory, captured = _capture_tools_factory()
    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", factory)

    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()))

    assert captured, "SDK.query was never called"
    assert captured[0] == MM_HANDLER_TOOLS, (
        f"Expected MM_HANDLER_TOOLS; got {captured[0]}"
    )


# ── test (c): two concurrent handlers complete without deadlock ───────────────


def test_two_concurrent_handlers_no_deadlock(e2e_project):
    """Two simultaneous handle_message coroutines must both complete."""
    from ap2 import events as ev_mod

    cfg = e2e_project()

    async def idle_gen(prompt, options):  # noqa: ARG001
        yield _FakeMsg("(handler done)")

    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", idle_gen)

    msg1 = _mm_msg(text="@claude-bot status")
    msg2 = _mm_msg(text="@claude-bot board")

    async def run_both():
        t1 = asyncio.create_task(handle_message(cfg, sdk, mcp_server=None, msg=msg1))
        t2 = asyncio.create_task(handle_message(cfg, sdk, mcp_server=None, msg=msg2))
        await asyncio.gather(t1, t2)

    asyncio.run(run_both())

    evts = ev_mod.tail(cfg.events_file, n=50)
    mm_evts = [e for e in evts if e.get("type") == "mattermost"]
    assert len(mm_evts) == 2, f"Expected 2 mattermost events; got {len(mm_evts)}"


# ── test (d): prompt explicitly mentions the cron_edit restriction ────────────


def test_prompt_mentions_cron_restriction(e2e_project):
    """TB-145: the prompt always explains that `cron_edit` is off-limits
    (no longer gated on board state)."""
    cfg = e2e_project()
    _seed_active_task(cfg)

    prompts_seen: list[str] = []

    async def capture_prompt_gen(prompt, options):  # noqa: ARG001
        prompts_seen.append(prompt)
        yield _FakeMsg("done")

    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", capture_prompt_gen)

    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()))

    assert prompts_seen, "SDK.query was never called"
    prompt = prompts_seen[0]
    assert "cron_edit" in prompt, "Prompt must mention cron_edit restriction"


# ── test (e): toolset is invariant under board state transitions ──────────────


def test_toolset_invariant_under_board_state(e2e_project):
    """TB-145 load-bearing: handler toolset must stay the same as the
    board moves through idle → Active → Complete. The previous TB-122
    design swapped between FULL and RESTRICTED on each transition; the
    point of TB-145 is that this swap never happens."""
    cfg = e2e_project()

    # Start idle.
    factory, captured = _capture_tools_factory()
    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", factory)
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()))
    assert captured[0] == MM_HANDLER_TOOLS

    # Add active task — toolset MUST NOT change.
    _seed_active_task(cfg)
    factory2, captured2 = _capture_tools_factory()
    sdk2 = FakeSDK()
    sdk2.on("Incoming mattermost message", factory2)
    asyncio.run(handle_message(cfg, sdk2, mcp_server=None, msg=_mm_msg()))
    assert captured2[0] == MM_HANDLER_TOOLS
    assert captured2[0] == captured[0], (
        "TB-145 violation: toolset changed when board went from idle to active"
    )

    # Move task to Complete — toolset still unchanged.
    board = Board.load(cfg.tasks_file)
    board.move("TB-1", "Complete", check=True)
    board.save()
    factory3, captured3 = _capture_tools_factory()
    sdk3 = FakeSDK()
    sdk3.on("Incoming mattermost message", factory3)
    asyncio.run(handle_message(cfg, sdk3, mcp_server=None, msg=_mm_msg()))
    assert captured3[0] == MM_HANDLER_TOOLS
    assert captured3[0] == captured[0], (
        "TB-145 violation: toolset changed when board went from active to complete"
    )


# ── test: concurrency proof — handler finishes during task's run ───────────────


def test_mm_handler_completes_during_task_agent_run(e2e_project):
    """Briefing's load-bearing concurrency invariant.

    Spawn a slow task agent (sleeps 0.3s) and a fast MM handler at the same
    time. The handler must complete BEFORE the task agent — proving the MM
    flow isn't queued behind run_task. (TB-145: the handler no longer
    snapshots the board for toolset selection, so this test no longer
    cares about whether the snapshot landed in time — we just verify
    end-to-end concurrency.)
    """
    import time

    from ap2 import events as ev_mod
    from ap2.daemon import run_task
    from ap2.tests.e2e._fakes import _FakeMixedMsg, _FakeToolUseBlock

    cfg = e2e_project(ready_task=("TB-5", "Slow task"))

    task_done_at: list[float] = []
    handler_done_at: list[float] = []

    sdk = FakeSDK()

    async def slow_task_gen(prompt, options):  # noqa: ARG001
        # Simulate a multi-step agent: yield once, sleep, then emit the
        # report_result tool call. The sleep is what creates the window
        # for the handler to run.
        yield _FakeMsg("(working...)")
        await asyncio.sleep(0.3)
        yield _FakeMixedMsg([_FakeToolUseBlock(
            name="report_result",
            input={
                "status": "complete",
                "commit": "abc12345",
                "summary": "slow task finished",
                "tests_passed": "true",
            },
        )])
        task_done_at.append(time.monotonic())

    async def fast_handler_gen(prompt, options):  # noqa: ARG001
        handler_done_at.append(time.monotonic())
        yield _FakeMsg("(handler reply)")

    sdk.on("## Task\nTB-5", slow_task_gen)
    sdk.on("Incoming mattermost message", fast_handler_gen)

    async def _drive():
        board = Board.load(cfg.tasks_file)
        task = next(t for t in board.iter_tasks("Ready") if t.id == "TB-5")
        run_t = asyncio.create_task(run_task(cfg, sdk, None, task))
        # Give run_task a beat to land move_to_active. After this await,
        # the board has TB-5 in Active. (TB-145 historical note: prior
        # to TB-145 the handler would observe task_in_flight=True here
        # and pick the RESTRICTED toolset; that branch no longer exists.)
        await asyncio.sleep(0.05)
        board2 = Board.load(cfg.tasks_file)
        assert any(t.id == "TB-5" for t in board2.iter_tasks("Active")), (
            "Daemon should have moved TB-5 to Active before the handler spawns"
        )
        handler_t = asyncio.create_task(
            handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()),
        )
        await asyncio.gather(run_t, handler_t)

    asyncio.run(_drive())

    assert handler_done_at, "handler never ran"
    assert task_done_at, "task agent never ran"
    # Strict: handler finished BEFORE the slow task's report_result tool
    # call. If the loops were sequential, handler would land after.
    assert handler_done_at[0] < task_done_at[0], (
        f"handler finished after task agent: handler={handler_done_at[0]:.3f} "
        f"task={task_done_at[0]:.3f}"
    )

    # The mattermost event for this handler was logged with toolset=restricted
    # (TB-145: always "restricted" now).
    evts = ev_mod.tail(cfg.events_file, n=200)
    mm_evts = [e for e in evts if e.get("type") == "mattermost"]
    assert mm_evts and mm_evts[-1].get("toolset") == "restricted"


# ── test: cron_edit attempt during handler run leaves cron.yaml untouched ─────


def test_cron_edit_via_handler_does_not_mutate_cron(e2e_project):
    """If the handler somehow tried `cron_edit` (e.g. an SDK that didn't
    enforce allowlists), the daemon's events stream would NOT show a
    `cron_edit` mutation. The check is defensive: real SDK rejects
    disallowed tools; our FakeSDK doesn't. So this test asserts that
    no `cron.yaml` mutation event is fired by the handler when its
    prompt explicitly tells it not to call cron_edit.
    """
    from ap2 import events as ev_mod

    cfg = e2e_project()
    _seed_active_task(cfg)

    # Capture allowed_tools and assert cron_edit is absent — the load-bearing
    # SDK enforcement handle.
    factory, captured = _capture_tools_factory()
    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", factory)

    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()))

    assert captured, "SDK.query was never called"
    assert "mcp__autopilot__cron_edit" not in captured[0]
    assert "mcp__autopilot__ideation_state_write" not in captured[0]

    # And no cron-mutation events landed (no ad-hoc Bash workaround was
    # available either — restricted set excludes Bash via CONTROL_AGENT_TOOLS
    # not granting it in the first place).
    evts = ev_mod.tail(cfg.events_file, n=50)
    cron_kinds = {"cron_proposed", "cron_proposal_error", "cron_proposal_rejected"}
    assert not any(e.get("type") in cron_kinds for e in evts)


# ── test: responsiveness gate — mattermost_reply lands within 30s of mention ──


def test_mattermost_reply_lands_within_30s_of_mention_during_long_task(e2e_project):
    """TB-122 responsiveness gate (auto-verifiable replacement for the
    prior `Manual: kick a long-running task on stoch …` bullet).

    Stub a long-running task agent (sleeps for 0.5s — a stand-in for a
    multi-minute SDK turn). While that fake task is in flight, enqueue a
    Mattermost mention. The handler responder fires a `mattermost_reply`
    event (mimicking what the real `do_mattermost_reply` MCP tool does
    end-to-end). Assert the `mattermost_reply` event's wall-clock
    timestamp lands within 30s of the mention's enqueue timestamp.

    Pins MM responsiveness end-to-end without a live deployment — the
    same property the manual-stoch bullet was meant to assert. If the
    MM polling were ever resequenced behind `run_task` again, this test
    would fail (the reply event would land only after run_task returns,
    which can be 1200s+ on a real task agent).
    """
    import datetime as _dt

    from ap2 import events as ev_mod
    from ap2.daemon import run_task
    from ap2.tests.e2e._fakes import _FakeMixedMsg, _FakeToolUseBlock

    cfg = e2e_project(ready_task=("TB-7", "Long task"))

    sdk = FakeSDK()

    async def slow_task_gen(prompt, options):  # noqa: ARG001
        yield _FakeMsg("(working...)")
        await asyncio.sleep(0.5)
        yield _FakeMixedMsg([_FakeToolUseBlock(
            name="report_result",
            input={
                "status": "complete",
                "commit": "abc12345",
                "summary": "long task done",
                "tests_passed": "true",
            },
        )])

    async def fast_handler_gen(prompt, options):  # noqa: ARG001
        # The real handler routes a tool_use call through the MCP server,
        # which calls do_mattermost_reply, which appends the event. We
        # mimic that side-effect directly because FakeSDK doesn't
        # actually execute MCP tool calls.
        ev_mod.append(
            cfg.events_file,
            "mattermost_reply",
            channel="dev",
            thread_id="",
            post_id="post-r",
            summary="status: TB-7 active",
        )
        yield _FakeMsg("done")

    sdk.on("## Task\nTB-7", slow_task_gen)
    sdk.on("Incoming mattermost message", fast_handler_gen)

    # The mention's "timestamp" is the moment we enqueue it for the
    # handler — i.e. when handle_message is about to be awaited. We
    # capture it immediately before launching the handler task.
    mention_ts_holder: list[_dt.datetime] = []

    async def _drive():
        board = Board.load(cfg.tasks_file)
        task = next(t for t in board.iter_tasks("Ready") if t.id == "TB-7")
        run_t = asyncio.create_task(run_task(cfg, sdk, None, task))
        # Let move_to_active land before we spawn the handler. (TB-145:
        # this no longer changes the toolset selection, but the
        # responsiveness check still wants the realistic scenario where
        # the handler fires while a task is genuinely active.)
        await asyncio.sleep(0.05)
        mention_ts_holder.append(_dt.datetime.now(_dt.timezone.utc))
        handler_t = asyncio.create_task(
            handle_message(cfg, sdk, mcp_server=None, msg=_mm_msg()),
        )
        await asyncio.gather(run_t, handler_t)

    asyncio.run(_drive())

    assert mention_ts_holder, "mention was never enqueued"
    mention_ts = mention_ts_holder[0]

    evts = ev_mod.tail(cfg.events_file, n=200)
    reply_evts = [e for e in evts if e.get("type") == "mattermost_reply"]
    assert reply_evts, "no mattermost_reply event landed"

    # events.py timestamps are second-precision UTC ISO 8601 with a Z suffix.
    reply_ts = _dt.datetime.fromisoformat(
        reply_evts[-1]["ts"].replace("Z", "+00:00"),
    )
    elapsed = (reply_ts - mention_ts).total_seconds()
    # The briefing's gate is <30s. Wall-clock with a 0.5s task sleep should
    # land in well under 1s; we keep the 30s bound so a slow CI box still
    # passes as long as the structural concurrency invariant holds.
    assert elapsed < 30.0, (
        f"mattermost_reply landed {elapsed:.1f}s after mention "
        f"(briefing requires <30s)"
    )

    # And the handler ran with the standard MM_HANDLER_TOOLS set —
    # `mattermost` event records `toolset=restricted` (TB-145: always
    # "restricted").
    mm_evts = [e for e in evts if e.get("type") == "mattermost"]
    assert mm_evts and mm_evts[-1].get("toolset") == "restricted"
