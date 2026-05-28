"""TB-149: end-to-end check that the MM handler can call
`mattermost_thread_read` to fetch prior thread context, then compose a
reply that references the prior messages.

Pre-TB-149 a thread-reply like "yes" to a question the bot asked 10
minutes ago landed at the handler with no context — the handler only
saw `text="yes"` + `thread_id=<root>` + sender, no prior messages. So
the operator had to repeat the TB-N in their reply (e.g. "yes, TB-99").
This test simulates the in-thread approval: the bot's earlier message
in the thread asked "approve TB-99?", the operator replied "yes" in
the same thread, and we verify the handler:

  (a) Calls `mattermost_thread_read(thread_id=<root>)` exactly once.
  (b) Composes a `mattermost_reply` whose text references content from
      the prior messages (specifically: contains "TB-99", the task id
      from the bot's earlier question — proof that thread context
      flowed into the reply rather than the handler having to guess).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import AsyncIterator

from ap2 import events, tools
from ap2.components import mattermost  # TB-312
from ap2.daemon import handle_message

from ap2.tests.e2e._fakes import FakeSDK


def test_mm_handler_reads_thread_then_replies_with_context(
    e2e_project, monkeypatch,
):
    cfg = e2e_project()

    # Mattermost env required so the do_mattermost_thread_read /
    # do_mattermost_reply paths don't short-circuit on misconfig. The
    # actual API surface is stubbed below.
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")

    # Stub fetch_thread to return two prior messages — the bot's
    # earlier question (mentioning TB-99) plus a brief operator follow-
    # up. The thread root post is the bot's "approve TB-99?" prompt;
    # the operator's "yes" is the *current* message that triggered the
    # handler (it doesn't need to be in the stubbed thread payload —
    # the test exercises the handler's ability to look BACKWARDS for
    # context from the current message).
    prior = [
        {
            "post_id": "root1",
            "user": "claude-bot",
            "text": "approve TB-99? (yes/no)",
            "create_at": 1000,
        },
        {
            "post_id": "p2",
            "user": "claude-bot",
            "text": "(this will dispatch the ideation-gated task)",
            "create_at": 2000,
        },
    ]
    fetch_calls: list[tuple[str, int]] = []

    def fake_fetch_thread(_cfg, thread_id, *, max_messages=50):
        fetch_calls.append((thread_id, max_messages))
        return list(prior)

    monkeypatch.setattr(mattermost, "fetch_thread", fake_fetch_thread)

    # Capture mattermost_reply calls so we can assert on the composed text.
    posts: list[dict] = []

    def fake_mm_post(channel, text, thread_id=""):
        posts.append({"channel": channel, "text": text, "thread_id": thread_id})
        return "post-reply-id"

    monkeypatch.setattr(tools, "_mm_post", fake_mm_post)

    # The thread-reply message that lands at the handler. text="yes"
    # is the operator's in-thread approval; thread_id="root1" points at
    # the bot's earlier question.
    msg = {
        "id": "post-reply",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "yes",
        "thread_id": "root1",
    }

    # Scripted handler: simulates what the real agent does under the
    # TB-149 prompt — call `mattermost_thread_read` first, then
    # compose a reply that references the TB-N from the prior message.
    # We invoke the do_* handlers directly (matches the established
    # pattern in test_tb142_mm_queue_routing.py — the responder stands
    # in for the real Anthropic SDK turn).
    def factory(prompt, options):  # noqa: ARG001
        async def _gen() -> AsyncIterator:
            # Step 1: fetch the thread context (TB-149's load-bearing call).
            res = tools.do_mattermost_thread_read(
                cfg, {"thread_id": "root1", "max_messages": 50},
            )
            assert not res.get("isError"), res
            import json as _json
            body = _json.loads(res["content"][0]["text"])
            assert body["count"] == 2, body
            # Extract the TB-N from the prior bot message — proof that
            # the handler had access to it. A real agent would do this
            # via natural-language reasoning; we do it via regex.
            import re
            tb_match = None
            for p in body["posts"]:
                m = re.search(r"TB-\d+", p["text"])
                if m and p["user"] == "claude-bot":
                    tb_match = m.group(0)
                    break
            assert tb_match == "TB-99", body

            # Step 2: compose a reply that references the prior context.
            # The TB-N proves the agent pulled it from the thread read,
            # not from the current message (which is just "yes").
            tools.do_mattermost_reply(
                cfg,
                {
                    "channel": "ch-abc",
                    "text": (
                        f"Acknowledged — approving {tb_match}. "
                        "Queued for dispatch on the next tick."
                    ),
                    "thread_id": "root1",
                },
            )
            yield SimpleNamespace(content=[
                SimpleNamespace(text="(handler done)"),
            ])
        return _gen()

    sdk = FakeSDK()
    sdk.on("Incoming mattermost message", factory)

    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=msg))

    # (a) Tool called exactly once with the expected thread_id.
    assert len(fetch_calls) == 1, f"expected 1 fetch_thread call, got {fetch_calls}"
    assert fetch_calls[0][0] == "root1"

    # (b) The reply went through and references TB-99 from the prior
    # bot message — the handler couldn't have known TB-99 from the
    # incoming "yes" alone; the only place TB-99 lived was in the
    # thread context we stubbed above.
    assert len(posts) == 1, f"expected 1 reply, got {posts}"
    assert "TB-99" in posts[0]["text"]
    # And the reply landed in the same thread, not the channel root.
    assert posts[0]["thread_id"] == "root1"

    # Audit trail: mattermost_reply event landed (the do_mattermost_reply
    # path appends one). This pins the integration so a future refactor
    # that drops the audit event surfaces here.
    evts = events.tail(cfg.events_file, 50)
    replies = [e for e in evts if e["type"] == "mattermost_reply"]
    assert replies, f"no mattermost_reply event; got {[e['type'] for e in evts]}"
    assert "TB-99" in replies[-1].get("summary", "")
