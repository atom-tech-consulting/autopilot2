"""TB-144 end-to-end: chat-triggered status report shares the cron path.

Pre-TB-144 the MM handler composed status replies inline via freeform
text — format drifted from the canonical cron report and the audit shape
diverged (no `cron_start`/`cron_complete` events landed for chat-trigger
runs, so post-mortems couldn't tell on-demand from scheduled).

This test simulates the operator typing `@claude-bot status` into a
mattermost channel. We assert:

  (a) The MM handler invokes the new `mcp__autopilot__status_report_run`
      MCP tool (verified by the resulting routine running its own SDK
      sub-agent).
  (b) The routine emits `cron_start` AND `cron_complete` events with
      `trigger="chat"` so post-mortems can distinguish the source.
  (c) The chat trigger does NOT advance `cron_state[status-report]` —
      the next scheduled cron tick still fires on its normal interval.

The handler-side script and the status-report sub-agent are both
FakeSDK-driven; the wiring under test is the real `tools.py` →
`status_report.py` chain (no mocks, just a fake event-loop SDK).
"""
from __future__ import annotations

import asyncio
import json

from ap2 import events, status_report as sr_mod, tools
from ap2.daemon import handle_message

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg


def _fake_mm_api_status(new_post_create_at: int = 1000):
    """Stub mattermost API returning a single `@claude-bot status` post."""

    def fake_get(url, token, path):  # noqa: ARG001
        if "/channels/ch1/posts" in path:
            return {
                "order": ["p_status"],
                "posts": {
                    "p_status": {
                        "id": "p_status",
                        "user_id": "alice-id",
                        "message": "@claude-bot status",
                        "create_at": new_post_create_at,
                        "channel_id": "ch1",
                        "root_id": "",
                    },
                },
            }
        if path.endswith("/channels/ch1"):
            return {"id": "ch1", "name": "ap2"}
        if path.endswith("/users/alice-id"):
            return {"username": "alice"}
        return {}

    return fake_get


def test_chat_triggered_status_report_routes_through_shared_routine(
    e2e_project, monkeypatch,
):
    """Operator types `@claude-bot status`. The MM handler invokes
    `do_status_report_run` (the SDK-free entry point the MCP tool
    wraps), which dispatches a sub-agent through the shared routine.

    Pinned outcomes:
      - `cron_start` + `cron_complete` events land with `trigger="chat"`.
      - The status-report sub-agent's SDK turn is exercised exactly once.
      - `cron_state[status-report]` is NOT advanced (chat triggers don't
        silence the scheduled cron).
      - The handler invocation is audited as a `mattermost` event.
    """
    cfg = e2e_project()

    # Mattermost env so `check_new_messages` returns the canned status
    # message.
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch1")
    monkeypatch.setenv("AP2_MM_BOT_USER_ID", "bot-u")
    monkeypatch.setenv("AP2_MM_MENTION", "@claude-bot")

    cfg.mm_state_file.write_text(
        json.dumps(
            {
                "cursors": {"ch1": "p_seed", "ch1:ts": 500},
                "channel_names": {"ch1": "ap2"},
                "users": {},
                "thread_mentions": {},
            }
        )
    )
    monkeypatch.setattr("ap2.mattermost._api_get", _fake_mm_api_status())

    # Seed activity AFTER the most recent status-report cron_complete so
    # the skip-gate doesn't fire (we want the run path, not the skip
    # path — the skip path is exercised by the unit tests).
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
        summary="something happened",
    )

    # Stub the prompt builder so we don't depend on git/board state for
    # the status-report sub-agent's prompt — keeps the test focused on
    # the routing wiring under test.
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: f"stub control prompt for {name}",
    )

    # FakeSDK: two responders.
    #   1. MM handler (matches "## Incoming mattermost message"): calls
    #      `do_status_report_run` directly — the same surface the real
    #      handler agent would call via the MCP tool.
    #   2. Status-report sub-agent (matches the stubbed prompt's marker):
    #      no-op; we just need the sub-agent's SDK turn to be reached.
    sdk = FakeSDK()

    sub_agent_calls = {"count": 0}

    def status_subagent(prompt, options):  # noqa: ARG001
        async def _gen():
            sub_agent_calls["count"] += 1
            yield _FakeMsg("(status sub-agent did the thing)")
        return _gen()

    sdk.on(
        "stub control prompt for status-report",
        status_subagent,
    )

    # MM handler stand-in: invoke the MCP-tool entry point. The
    # `await tools.do_status_report_run(...)` mirrors what the real MCP
    # tool wrapper does inside the SDK adapter (see
    # `tools.build_mcp_server` → `status_report_run`).
    def mm_handler(prompt, options):  # noqa: ARG001
        async def _gen():
            res = await tools.do_status_report_run(
                cfg, {"reason": "alice asked for status"},
            )
            # The handler would also send a mattermost_reply confirming
            # the action, but the pinned audit checks below cover the
            # critical wiring; we skip the reply here to keep the test
            # focused on the routine itself.
            assert not res.get("isError"), res
            yield _FakeMsg("(mm handler invoked status_report_run)")
        return _gen()

    sdk.on("## Incoming mattermost message", mm_handler)

    # Configure the routine with our FakeSDK so the MCP tool can find
    # the sdk + mcp_server references at call time. In production
    # `daemon.main_loop` does this once at startup; in tests we do it
    # explicitly per test (no main_loop).
    sr_mod.configure(sdk, mcp_server=None)

    # Drive the MM-loop work for one tick: poll, dispatch handlers,
    # await all. Mirrors what `_mm_loop` does per cycle in production.
    from ap2.mattermost import check_new_messages

    msgs = check_new_messages(cfg)
    assert len(msgs) == 1, f"expected one status mention, got {msgs!r}"
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=msgs[0]))

    # (a) The status-report sub-agent's SDK turn ran exactly once.
    assert sub_agent_calls["count"] == 1, (
        f"expected exactly one sub-agent dispatch, got {sub_agent_calls['count']}"
    )

    # (b) cron_start AND cron_complete landed with trigger="chat".
    evts = events.tail(cfg.events_file, 200)
    chat_starts = [
        e for e in evts
        if e.get("type") == "cron_start"
        and e.get("job") == "status-report"
        and e.get("trigger") == "chat"
    ]
    chat_completes = [
        e for e in evts
        if e.get("type") == "cron_complete"
        and e.get("job") == "status-report"
        and e.get("trigger") == "chat"
    ]
    assert len(chat_starts) == 1, (
        f"expected one chat-trigger cron_start; got {chat_starts!r}"
    )
    assert chat_starts[0].get("reason") == "alice asked for status"
    assert len(chat_completes) == 1, (
        f"expected one chat-trigger cron_complete; got {chat_completes!r}"
    )

    # The MM handler invocation itself was audited.
    handler_evts = [e for e in evts if e.get("type") == "mattermost"]
    assert handler_evts, "MM handler invocation should land a `mattermost` event"

    # (c) cron_state was NOT advanced — chat trigger doesn't silence cron.
    if cfg.cron_state_file.exists():
        state = json.loads(cfg.cron_state_file.read_text())
        assert "status-report" not in state, (
            f"chat trigger must NOT advance cron_state; got {state!r}"
        )
