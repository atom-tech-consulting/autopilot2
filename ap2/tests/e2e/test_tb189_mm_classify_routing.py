"""TB-189 end-to-end: chat-triggered "@claude-bot classify TB-N <verdict>"
routes through `operator_queue_append` with the right `op="classify"`
payload (task_id + verdict + reason).

The static prompt-text pins in `tests/test_prompts.py` (added in the
same task) guard against a future edit silently dropping the verb
description. They do NOT, however, exercise the routing itself — i.e.
that a "@claude-bot classify TB-N pro-forma reason: ..." mention
dispatched through `handle_message` actually causes an
`operator_queue_append({"op": "classify", "task_id": "TB-N",
"verdict": "<v>", "reason": "..."})` call to land. This e2e test
closes that gap by synthesizing the mention end-to-end with a stubbed
SDK that captures the queue-append calls and asserts the recorded
shape.

The agent factory inspects the prompt's incoming-message text to
extract the TB-N + verdict + reason — same surface the real LLM-driven
handler would walk: read the prompt's `## Incoming mattermost message`
block, recognize the verb, call `do_operator_queue_append` with the
matching op.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

from ap2 import events, tools
from ap2.board import Board
from ap2.daemon import handle_message

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg


def _classify_msg(text: str) -> dict:
    """Synthesize a Mattermost mention with the given body text."""
    return {
        "id": "post-classify-1",
        "channel_id": "ch1",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": text,
        "thread_id": "",
    }


# Recognize "classify TB-N <verdict>" / "classify TB-N <verdict> reason: ..."
# in the incoming-message body. Mirrors what the prompt teaches the
# handler to do — not a regex the production handler would use, just
# enough to drive the test SDK toward the routing decision.
_CLASSIFY_RE = re.compile(
    r"classify\s+(TB-\d+)\s+(advanced-goal|pro-forma|negative|unclear)"
    r"(?:\s+reason:\s*(.+))?",
    re.IGNORECASE,
)


def _classify_routing_factory(captured: list[dict], cfg):
    """Return an SDK factory that inspects the prompt for the message text
    the test seeded, picks task_id + verdict + reason, and calls the
    real `do_operator_queue_append` (so we exercise the actual queue
    path, not just a recorded call). The capture list collects the
    args it routed with, plus the routing handler's return value.

    Detection mirrors what the prompt's TB-189 bullet teaches the
    handler to do — recognize "classify TB-N <verdict>" with optional
    "reason: ..." in the message body.
    """

    def factory(prompt: str, options):  # noqa: ARG001
        async def _gen() -> AsyncIterator:
            # Pull only the incoming-message fence so we don't accidentally
            # false-positive on the "classify TB-N <verdict>" phrasing
            # that lives inside the prompt's own teaching block.
            marker = "## Incoming mattermost message"
            i = prompt.find(marker)
            assert i != -1, "prompt missing the incoming-message section"
            after = prompt[i:]
            fence_open = after.find("```")
            fence_close = after.find("```", fence_open + 3)
            body = after[fence_open + 3:fence_close].strip()
            assert body, "incoming-message fence body is empty"

            m = _CLASSIFY_RE.search(body)
            assert m is not None, f"could not parse classify verb out of body: {body!r}"
            tb_id = m.group(1)
            verdict = m.group(2).lower()
            reason = (m.group(3) or "").strip().rstrip(".") or None

            args: dict = {
                "op": "classify",
                "task_id": tb_id,
                "verdict": verdict,
            }
            if reason is not None:
                args["reason"] = reason
            ret = tools.do_operator_queue_append(cfg, args)
            captured.append({"args": args, "ret": ret})

            yield _FakeMsg(
                f"(stubbed handler queued op=classify {tb_id} {verdict})"
            )

        return _gen()

    return factory


def _seed_board_task(cfg, tb_id: str, title: str = "shipped proposal") -> None:
    """Add a Complete-section task so the queue-append snapshot
    validation accepts the classify op (cmd_classify / drain require
    TB-N is on the board)."""
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id=tb_id, title=title)
    board.save()


def test_mm_handler_routes_classify_pro_forma(e2e_project, monkeypatch):
    """`@claude-bot classify TB-N pro-forma reason: ...` synthesized
    through `handle_message` lands a single `operator_queue_append`
    call with `op="classify"`, the right task_id / verdict / reason.
    The op is also persisted to the queue file (the real
    `do_operator_queue_append` was invoked end-to-end) and the audit
    `operator_queue_append` event fires.
    """
    cfg = e2e_project()
    _seed_board_task(cfg, "TB-1980")

    captured: list[dict] = []
    sdk = FakeSDK()
    sdk.on(
        "## Incoming mattermost message",
        _classify_routing_factory(captured, cfg),
    )

    asyncio.run(
        handle_message(
            cfg, sdk, mcp_server=None,
            msg=_classify_msg(
                "@claude-bot classify TB-1980 pro-forma reason: "
                "satisfied validators but no measurable goal motion"
            ),
        )
    )

    # Exactly one queue-append call landed, with the classify shape.
    assert len(captured) == 1, f"expected one classify route, got {captured!r}"
    rec = captured[0]
    assert rec["args"]["op"] == "classify"
    assert rec["args"]["task_id"] == "TB-1980"
    assert rec["args"]["verdict"] == "pro-forma"
    assert "satisfied validators" in rec["args"]["reason"]
    # The handler's return shape is the canonical _ok dict — not an error.
    ret = rec["ret"]
    assert not ret.get("isError"), ret
    # The op was persisted (single-record queue file).
    queue_path = tools.operator_queue_path(cfg)
    assert queue_path.exists(), "queue file was never written"
    lines = [
        json.loads(line)
        for line in queue_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["op"] == "classify"
    assert lines[0]["args"]["task_id"] == "TB-1980"
    assert lines[0]["args"]["verdict"] == "pro-forma"
    # The audit event fired (operator_queue_append).
    evts = events.tail(cfg.events_file, 50)
    appends = [
        e for e in evts
        if e.get("type") == "operator_queue_append" and e.get("op") == "classify"
    ]
    assert len(appends) == 1, (
        f"expected one operator_queue_append; got {appends!r}"
    )


def test_mm_handler_routes_classify_advanced_goal(e2e_project, monkeypatch):
    """Variant: `@claude-bot classify TB-N advanced-goal` (no reason)
    routes the same way with the matching verdict; the routing layer
    handles the optional-reason case symmetrically to cmd_classify."""
    cfg = e2e_project()
    _seed_board_task(cfg, "TB-1981")

    captured: list[dict] = []
    sdk = FakeSDK()
    sdk.on(
        "## Incoming mattermost message",
        _classify_routing_factory(captured, cfg),
    )

    asyncio.run(
        handle_message(
            cfg, sdk, mcp_server=None,
            msg=_classify_msg("@claude-bot classify TB-1981 advanced-goal"),
        )
    )

    assert len(captured) == 1, f"expected one classify route, got {captured!r}"
    rec = captured[0]
    assert rec["args"] == {
        "op": "classify",
        "task_id": "TB-1981",
        "verdict": "advanced-goal",
    }
    ret = rec["ret"]
    assert not ret.get("isError"), ret
    # Persisted with the right shape.
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(line)
        for line in queue_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["op"] == "classify"
    assert lines[0]["args"]["task_id"] == "TB-1981"
    assert lines[0]["args"]["verdict"] == "advanced-goal"
