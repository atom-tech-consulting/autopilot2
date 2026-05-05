"""TB-176 end-to-end: chat-triggered "@claude-bot ideate" routes through
`operator_queue_append` with the right `force` flag.

The static prompt-text pins in `tests/test_prompts.py` (added in the
same task) guard against a future edit silently dropping the verb
description / phrasing. They do NOT, however, exercise the routing
itself — i.e. that an "@claude-bot ideate" mention dispatched through
`handle_message` actually causes an `operator_queue_append({"op":
"ideate", "force": <bool>})` call to land. This e2e test closes that
gap by synthesizing the mention end-to-end with a stubbed SDK that
captures the queue-append calls and asserts the recorded shape.

The agent factory inspects the prompt's incoming-message text to pick
the right `force` flag — same surface the real LLM-driven handler
would walk: read the prompt's `## Incoming mattermost message` block,
recognize the verb (and the `force` / `--force` syntactic variant the
prompt teaches), call `do_operator_queue_append` with the matching op.

Two variants:
  - "@claude-bot ideate"        → `force=False`  (default; refuses if Active).
  - "@claude-bot ideate force"  → `force=True`   (operator escape hatch).

Both use `e2e_project()` with NO Active task so the unforced variant
isn't refused by the Active gate (the gate is exercised directly in
the TB-159 unit tests for `do_operator_queue_append`; here we're
testing the routing path, not the gate).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from ap2 import events, tools
from ap2.daemon import handle_message

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg


def _ideate_msg(text: str) -> dict:
    """Synthesize a Mattermost mention with the given body text."""
    return {
        "id": "post-ideate-1",
        "channel_id": "ch1",
        "channel_name": "ap2",
        "user": "alice",
        "text": text,
        "thread_id": "",
    }


def _ideate_routing_factory(captured: list[dict], cfg):
    """Return an SDK factory that inspects the prompt for the message text
    the test seeded, picks the right `force` value, and calls the real
    `do_operator_queue_append` (so we exercise the actual queue path,
    not just a recorded call). The capture list collects the args it
    routed with, plus the routing handler's return value.

    The detection mirrors what the prompt's TB-176 bullet teaches the
    handler to do — recognize the verb in the incoming message, pick
    `force=true` only when the user added `force` / `--force`.
    """

    def factory(prompt: str, options):  # noqa: ARG001
        async def _gen() -> AsyncIterator:
            # Find the incoming-message block; extract the text body
            # the prompt fenced in. Mirror the prompt's recognition
            # logic: bare "ideate" → force=False; "ideate force" or
            # "ideate --force" → force=True.
            lower = prompt.lower()
            # Pull only the incoming-message fence so we don't
            # accidentally false-positive on the "ideate force" / "--force"
            # phrasing that lives inside the prompt's own teaching block.
            marker = "## incoming mattermost message"
            i = lower.find(marker)
            assert i != -1, "prompt missing the incoming-message section"
            # The text body is fenced by triple-backticks after the
            # `- thread:` line. Slice out the first fenced block after
            # the marker.
            after = prompt[i:]
            fence_open = after.find("```")
            fence_close = after.find("```", fence_open + 3)
            body = after[fence_open + 3:fence_close].strip().lower()
            assert body, "incoming-message fence body is empty"

            force = ("ideate force" in body) or ("ideate --force" in body)
            args = {"op": "ideate", "force": force}
            ret = tools.do_operator_queue_append(cfg, args)
            captured.append({"args": args, "ret": ret})

            yield _FakeMsg(
                f"(stubbed handler queued op=ideate force={force})"
            )

        return _gen()

    return factory


def test_mm_handler_routes_ideate_unforced(e2e_project, monkeypatch):
    """`@claude-bot ideate` synthesized through `handle_message` lands
    a single `operator_queue_append` call with `op="ideate"` and
    `force=False`. The op is also persisted to the queue file (the
    real `do_operator_queue_append` was invoked end-to-end) and the
    audit `operator_queue_append` event fires.
    """
    cfg = e2e_project()

    captured: list[dict] = []
    sdk = FakeSDK()
    sdk.on(
        "## Incoming mattermost message",
        _ideate_routing_factory(captured, cfg),
    )

    asyncio.run(
        handle_message(
            cfg, sdk, mcp_server=None, msg=_ideate_msg("@claude-bot ideate"),
        )
    )

    # Exactly one queue-append call landed, with the unforced shape.
    assert len(captured) == 1, f"expected one ideate route, got {captured!r}"
    rec = captured[0]
    assert rec["args"] == {"op": "ideate", "force": False}
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
    assert lines[0]["op"] == "ideate"
    assert lines[0]["args"] == {"force": False}
    # The audit event fired (operator_queue_append).
    evts = events.tail(cfg.events_file, 50)
    appends = [
        e for e in evts
        if e.get("type") == "operator_queue_append" and e.get("op") == "ideate"
    ]
    assert len(appends) == 1, f"expected one operator_queue_append; got {appends!r}"


def test_mm_handler_routes_ideate_forced(e2e_project, monkeypatch):
    """`@claude-bot ideate force` synthesized through `handle_message`
    lands a single `operator_queue_append` call with `op="ideate"` and
    `force=True`. Mirrors the unforced variant but exercises the
    `force` syntactic recognition path the prompt advertises.
    """
    cfg = e2e_project()

    captured: list[dict] = []
    sdk = FakeSDK()
    sdk.on(
        "## Incoming mattermost message",
        _ideate_routing_factory(captured, cfg),
    )

    asyncio.run(
        handle_message(
            cfg, sdk, mcp_server=None,
            msg=_ideate_msg("@claude-bot ideate force"),
        )
    )

    assert len(captured) == 1, f"expected one ideate route, got {captured!r}"
    rec = captured[0]
    assert rec["args"] == {"op": "ideate", "force": True}
    ret = rec["ret"]
    assert not ret.get("isError"), ret
    # Persisted with force=True.
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        json.loads(line)
        for line in queue_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["op"] == "ideate"
    assert lines[0]["args"] == {"force": True}


def test_mm_handler_routes_ideate_dash_dash_force(e2e_project, monkeypatch):
    """`@claude-bot ideate --force` (CLI-style flag) routes the same as
    the bare `force` variant — `force=True`. Pin the second syntactic
    form the prompt teaches so a future edit dropping one shape trips
    the test rather than silently flipping the operator's intent to
    `force=False`.
    """
    cfg = e2e_project()

    captured: list[dict] = []
    sdk = FakeSDK()
    sdk.on(
        "## Incoming mattermost message",
        _ideate_routing_factory(captured, cfg),
    )

    asyncio.run(
        handle_message(
            cfg, sdk, mcp_server=None,
            msg=_ideate_msg("@claude-bot ideate --force"),
        )
    )

    assert len(captured) == 1, f"expected one ideate route, got {captured!r}"
    rec = captured[0]
    assert rec["args"] == {"op": "ideate", "force": True}
