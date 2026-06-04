"""Real-SDK smoke for the `mattermost` control kind, parametrized over BOTH
adapter backends (TB-378 / goal.md axis 7).

The Mattermost handler was routed through the adapter seam in the axis-6
migration (TB-365, via `daemon._run_control_agent` + `_control_kind_from_label`'s
`MM-<post-id>` → `mattermost` mapping), but no live smoke ever proved the handler
reaches its reply surface — `mattermost_reply` — on either backend.

This smoke closes that gap. Dispatching through the SAME seam production uses —
`select_adapter("mattermost", cfg)` + the streaming `AgentAdapter.run(...)` with
the production `MM_HANDLER_TOOLS` policy (the narrowed RESTRICTED toolset the
handler always runs with, post-TB-145), under `force_backend(..., "mattermost",
backend)` — it asks a real mattermost-kind agent to reply via `mattermost_reply`,
and asserts (for BOTH the claude and codex backends) that a `mattermost_reply`
tool call carrying non-empty text lands in the normalized event stream. The codex
variant rides TB-373's stdio-MCP bridge.

The reply need NOT reach a real Mattermost server: the throwaway temp project has
no MM credentials, so the handler returns a clean "not configured" error — but
the captured tool-USE args (what the agent sent) are still in the normalized
stream, exactly the surface `daemon._run_control_agent`'s `_log_message` walks.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors; a missing credential / transport hiccup flows through the
shared `call_with_transient_retry`-then-skip helper.

Bounded cost: trivial reply-only prompt, max_turns=5, single-call expectation.
"""
from __future__ import annotations

import os

import pytest

from ._adapter import (
    BACKENDS,
    force_backend,
    gate_backend,
    run_control_to_tool_calls,
)
from ._transient import call_with_transient_retry

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


_MATTERMOST_PROMPT = (
    "TEST SCENARIO — do NOT read files, inspect the board, or answer a real "
    "operator question. Your ONLY job is to exercise the reply path. Call "
    "the `mcp__autopilot__mattermost_reply` tool EXACTLY ONCE with these "
    "args:\n"
    "  channel: smoke-channel\n"
    '  text: "Reply (smoke): handler wiring OK"\n'
    "Then end your turn IMMEDIATELY — do NOT call mattermost_reply again and "
    "do NOT call any other tool, even if the call returns an error (the "
    "smoke project has no Mattermost server configured). The daemon only "
    "needs to confirm the handler reaches its reply surface."
)


@pytest.mark.parametrize("backend", BACKENDS)
def test_mattermost_reply_via_adapter(backend, monkeypatch, tmp_path):
    """A real mattermost-kind handler, dispatched through
    `select_adapter("mattermost", cfg)`, replies via `mattermost_reply` — its
    reply surface — for BOTH the claude and codex backends. Asserts a
    `mattermost_reply` call carrying non-empty text lands in the normalized
    stream."""
    gate_backend(backend)
    force_backend(monkeypatch, "mattermost", backend)

    from ap2.tools import MM_HANDLER_TOOLS

    # TB-351: transient transport/service error → retry once, then skip (not
    # error); a genuine wiring regression (mattermost_reply not called) flows
    # to the asserts below and still fails.
    tool_calls = call_with_transient_retry(
        lambda: run_control_to_tool_calls(
            kind="mattermost",
            backend=backend,
            prompt=_MATTERMOST_PROMPT,
            allowed_tools=MM_HANDLER_TOOLS,
            root=tmp_path,
        ),
        describe=f"mattermost reply smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    replies = [
        tc for tc in tool_calls
        if tc["name"] in ("mattermost_reply", "mcp__autopilot__mattermost_reply")
    ]
    assert replies, (
        f"[{backend}] mattermost handler did not call mattermost_reply. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )
    args = replies[-1]["input"]
    assert str(args.get("text") or "").strip(), (
        f"[{backend}] mattermost_reply carried no text: {args!r}"
    )
    print(f"[smoke:{backend}] PASS — mattermost_reply args={args!r}")
