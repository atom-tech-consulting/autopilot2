"""Real-SDK smoke for the `cron` control kind, parametrized over BOTH adapter
backends (TB-378 / goal.md axis 7).

LLM cron jobs were routed through the adapter seam in the axis-6 migration
(TB-365, via `daemon._run_control_agent` + `_control_kind_from_label`'s
`cron-<job>` → `cron` mapping), but no live smoke ever proved a cron control
agent runs and reaches a control-output tool on either backend.

This smoke closes that gap. Dispatching through the SAME seam production uses —
`select_adapter("cron", cfg)` + the streaming `AgentAdapter.run(...)` with the
production `CONTROL_AGENT_TOOLS` policy, under `force_backend(..., "cron",
backend)` — it asks a real cron-kind agent to record a marker via `log_event`,
and asserts (for BOTH the claude and codex backends) that a `log_event` tool
call carrying the expected `type` lands in the normalized event stream. An LLM
cron job is a generic control agent (its prompt varies per job); `log_event` is
a representative, side-effect-light control-output tool both backends can drive,
and asserting its specific `type` arg proves a real output, not just "the agent
said something". The codex variant rides TB-373's stdio-MCP bridge.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors; a missing credential / transport hiccup flows through the
shared `call_with_transient_retry`-then-skip helper.

Bounded cost: trivial single-call prompt, max_turns=5, single-call expectation.
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


_CRON_EVENT_TYPE = "smoke_cron_probe"
_CRON_PROMPT = (
    "TEST SCENARIO — do NOT do any real cron work, read files, or run "
    "commands. Your ONLY job is to exercise a control-agent output tool. "
    "Call the `mcp__autopilot__log_event` tool EXACTLY ONCE with these "
    "args:\n"
    f"  type: {_CRON_EVENT_TYPE}\n"
    '  summary: "cron control-agent wiring OK"\n'
    "Then end your turn IMMEDIATELY — do NOT call log_event again and do "
    "NOT call any other tool. The daemon only needs to confirm the cron "
    "control agent reaches a control-output tool."
)


@pytest.mark.parametrize("backend", BACKENDS)
def test_cron_log_event_via_adapter(backend, monkeypatch, tmp_path):
    """A real cron-kind control agent, dispatched through `select_adapter("cron",
    cfg)`, produces output via `log_event` for BOTH the claude and codex
    backends. Asserts a `log_event` call carrying the expected `type` lands in
    the normalized stream."""
    gate_backend(backend)
    force_backend(monkeypatch, "cron", backend)

    from ap2.tools import CONTROL_AGENT_TOOLS

    # TB-351: transient transport/service error → retry once, then skip (not
    # error); a genuine wiring regression (log_event not called) flows to the
    # asserts below and still fails.
    tool_calls = call_with_transient_retry(
        lambda: run_control_to_tool_calls(
            kind="cron",
            backend=backend,
            prompt=_CRON_PROMPT,
            allowed_tools=CONTROL_AGENT_TOOLS,
            root=tmp_path,
        ),
        describe=f"cron log_event smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    logs = [
        tc for tc in tool_calls
        if tc["name"] in ("log_event", "mcp__autopilot__log_event")
    ]
    assert logs, (
        f"[{backend}] cron agent did not call log_event. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )
    args = logs[-1]["input"]
    assert args.get("type") == _CRON_EVENT_TYPE, (
        f"[{backend}] log_event carried an unexpected type: {args!r}"
    )
    assert str(args.get("summary") or "").strip(), (
        f"[{backend}] log_event carried no summary: {args!r}"
    )
    print(f"[smoke:{backend}] PASS — log_event args={args!r}")
