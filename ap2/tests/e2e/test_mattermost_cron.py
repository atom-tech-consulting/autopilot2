"""TB-56: full remote-start chain.

mattermost message → control agent queues pipeline → pipeline runs →
status-report cron observes completion → cron unfreezes Frozen follow-up →
follow-up runs → all tick events in causal order.

TB-122: MM polling is now in `_mm_loop`, not `_tick`. The test drives
each tick by polling MM once + running `_tick` to mirror what the two
concurrent loops do per cycle.
"""
from __future__ import annotations

import asyncio
import json

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick, handle_message
from ap2.mattermost import check_new_messages
from ap2.tools import do_board_edit

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg, text_respond, tool_call_respond


async def _drain_mm(cfg, sdk, mcp_server) -> None:
    """Run the MM-loop work for one tick: poll, dispatch handlers, await all.

    Production `_mm_loop` spawns each handler as a fire-and-forget
    `asyncio.create_task`; this helper awaits them so test assertions see
    a settled state, mirroring the wait the daemon does on shutdown.
    """
    msgs = check_new_messages(cfg)
    if not msgs:
        return
    await asyncio.gather(*(handle_message(cfg, sdk, mcp_server, m) for m in msgs))


# ---------- responder factories ----------


def _mm_handler_adds_pipeline(cfg):
    """Stand-in for the mattermost control agent: parses the message and
    queues a new Ready task via the real board-edit path."""

    async def gen(prompt, options):  # noqa: ARG001
        do_board_edit(
            cfg,
            {
                "action": "add_ready",
                "title": "Pipeline",
                "tags": ["#pipeline"],
                # TB-135: briefing is required for every add_*; the MM
                # handler in production builds one before calling the
                # tool, so the test stand-in does the same.
                # TB-154: structurally canonical briefing — `## Goal`,
                # `## Scope`, `## Design`, `## Verification`, `## Out of
                # scope`. `- `true`` is a no-op shell bullet so the
                # per-task verifier scores trivially without needing
                # the e2e fixture's working tree to carry a real pytest
                # target. This test is about the mm-add → cron-unfreeze
                # → pipeline-complete chain, not the verifier itself.
                "briefing": (
                    "# Pipeline\n\n"
                    "## Goal\nKick the pipeline.\n\n"
                    "## Scope\n- pipeline\n\n"
                    "## Design\nStub.\n\n"
                    "## Verification\n- `true` — trivially passes\n\n"
                    "## Out of scope\n- nothing\n"
                ),
            },
        )
        yield _FakeMsg("(mattermost handler done)")

    return gen


def _cron_unfreeze_pipeline_follow_up(cfg, follow_up_id: str):
    async def gen(prompt, options):  # noqa: ARG001
        board = Board.load(cfg.tasks_file)
        pipeline_done = any(
            "Pipeline" in t.title and t.section == "Complete"
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


def _fake_mm_api(new_post_create_at: int = 1000):
    """Return a mattermost `_api_get` stub.

    * `/channels/ch1/posts` → one new `@claude-bot` message from alice.
    * `/channels/ch1`       → `{"name": "dev"}`.
    * `/users/alice-id`     → `{"username": "alice"}`.
    """

    def fake_get(url, token, path):  # noqa: ARG001
        if "/channels/ch1/posts" in path:
            return {
                "order": ["p_msg"],
                "posts": {
                    "p_msg": {
                        "id": "p_msg",
                        "user_id": "alice-id",
                        "message": "@claude-bot start the pipeline",
                        "create_at": new_post_create_at,
                        "channel_id": "ch1",
                        "root_id": "",
                    },
                },
            }
        if path.endswith("/channels/ch1"):
            return {"id": "ch1", "name": "dev"}
        if path.endswith("/users/alice-id"):
            return {"username": "alice"}
        return {}

    return fake_get


# ---------- the test ----------


def test_remote_start_pipeline_end_to_end(e2e_project, clock, monkeypatch):
    cfg = e2e_project(
        # `blocked_on=TB-10` — the pipeline task gets TB-10 via the seeded
        # next_task_id. Once it lands in Complete (tick 1), TB-6 becomes
        # dispatchable. (Pre-TB-81 the parser only matched `TB-N` so any
        # narrative string here was a silent no-op; the new parser treats
        # all comma-separated tokens as real blockers.)
        frozen_task=("TB-6", "Follow-up", "TB-10"),
        cron_jobs=[
            {
                "name": "status-report",
                "interval": "60s",
                "prompt": "check pipeline status",
                "max_turns": 5,
            },
        ],
    )

    # --- mattermost env + pre-seeded cursor so first poll isn't seed-only. ---
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch1")
    monkeypatch.setenv("AP2_MM_BOT_USER_ID", "bot-u")
    monkeypatch.setenv("AP2_MM_MENTION", "@claude-bot")

    cfg.mm_state_file.write_text(
        json.dumps(
            {
                "cursors": {"ch1": "p_seed", "ch1:ts": 500},
                "channel_names": {"ch1": "dev"},
                "users": {},
                "thread_mentions": {},
            }
        )
    )
    monkeypatch.setattr("ap2.mattermost._api_get", _fake_mm_api())

    # --- scripted SDK. ---
    sdk = FakeSDK()
    # Register most-specific first so task prompts (which never contain these
    # headers) aren't misrouted.
    sdk.on("## Incoming mattermost message", _mm_handler_adds_pipeline(cfg))
    sdk.on("## Control job: status-report",
           _cron_unfreeze_pipeline_follow_up(cfg, "TB-6"))
    sdk.on(
        "**Pipeline**",  # unique to the pipeline task prompt title
        tool_call_respond(
            "report_result",
            {
                "status": "complete", "commit": "pipedead",
                "summary": "pipeline done", "tests_passed": "true",
            },
        ),
    )
    sdk.on(
        "## Task\nTB-6",
        tool_call_respond(
            "report_result",
            {
                "status": "complete", "commit": "follbeef",
                "summary": "follow-up done", "tests_passed": "true",
            },
        ),
    )

    # --- tick 1: mm → add Ready; cron fires but pipeline not yet done;
    #             step-3 picks the just-added pipeline and runs it. ---
    async def _run_tick():
        await _drain_mm(cfg, sdk, mcp_server=None)
        await _tick(cfg, sdk, mcp_server=None)

    asyncio.run(_run_tick())
    board = Board.load(cfg.tasks_file)
    pipeline = next(t for t in board.iter_tasks() if "Pipeline" in t.title)
    assert pipeline.section == "Complete"
    assert board.find("TB-6")[0] == "Frozen"

    # --- tick 2 at +90s: no new mm message (cursor advanced); cron fires
    #     again, sees pipeline Complete → unfreezes TB-6; step-3 runs TB-6. ---
    clock(90)
    # A second mm poll will return the same canned payload, but the cursor
    # advanced to "p_msg" in tick 1 so the loop sees `last_id == order[0]`
    # and breaks immediately — no duplicate pipeline add.
    asyncio.run(_run_tick())
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-6")[0] == "Complete"

    # --- event chain invariants. ---
    evts = events.tail(cfg.events_file, 200)
    kinds = [e["type"] for e in evts]

    # Exactly one mattermost message was processed.
    assert kinds.count("mattermost") == 1

    # Exactly one pipeline run (the mm-queued task) + one follow-up run.
    task_starts = [e for e in evts if e["type"] == "task_start"]
    assert len(task_starts) == 2
    assert "Pipeline" in task_starts[0]["title"]
    assert task_starts[1]["task"] == "TB-6"

    # Cron fired on both ticks (status-report has no last_run on tick 1, then
    # 90s > 60s interval between tick 1 and tick 2).
    assert kinds.count("cron_start") == 2

    # Strict ordering: mm event → pipeline start → pipeline complete → cron
    # unfreeze (evidenced by TB-6 start) → TB-6 complete.
    idx_mm = kinds.index("mattermost")
    idx_pipe_start = next(
        i for i, e in enumerate(evts)
        if e["type"] == "task_start" and "Pipeline" in e.get("title", "")
    )
    idx_pipe_complete = next(
        i for i, e in enumerate(evts)
        if e["type"] == "task_complete" and e.get("task") == task_starts[0]["task"]
    )
    idx_foll_start = next(
        i for i, e in enumerate(evts)
        if e["type"] == "task_start" and e.get("task") == "TB-6"
    )
    idx_foll_complete = next(
        i for i, e in enumerate(evts)
        if e["type"] == "task_complete" and e.get("task") == "TB-6"
    )
    assert idx_mm < idx_pipe_start < idx_pipe_complete < idx_foll_start < idx_foll_complete
