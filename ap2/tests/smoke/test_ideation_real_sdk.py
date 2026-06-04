"""Real-SDK smoke for the `ideation` control kind, parametrized over BOTH
adapter backends (TB-378 / goal.md axis 7).

`ideation` was routed through the adapter seam in the axis-6 migration (TB-365),
but its load-bearing `board_edit` propose path — how an ideation cycle's output
actually reaches the board — was exercised by NO live smoke on either backend.
"Codex can propose a task" (and even "a claude-backed ideation agent reaches
`board_edit` end-to-end") was unproven.

This smoke closes that gap. Dispatching through the SAME seam production uses —
`select_adapter("ideation", cfg)` + the streaming `AgentAdapter.run(...)` with
the production `IDEATION_TOOLS` policy, under `force_backend(..., "ideation",
backend)` — it asks a real ideation-kind agent to invoke `board_edit` to propose
a Backlog task, and asserts (for BOTH the claude and codex backends) that a
`board_edit` tool call with an add-* (propose) action + a non-empty title lands
in the normalized event stream. The codex variant rides TB-373's stdio-MCP
bridge that delivers ap2's toolset to a live codex agent.

The proposal need NOT be accepted: a trivial briefing is rejected cheaply by the
structural validator (`_validate_briefing_structure`) before any TASKS.md write,
so the smoke stays side-effect-free in its throwaway temp project. The assertion
is on the captured tool-USE args (what the agent sent), which the adapter's
normalized stream carries regardless of the handler's result — exactly the
surface `daemon.run_task._log_message` walks.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors; a missing credential / transport hiccup flows through the
shared `call_with_transient_retry`-then-skip helper.

Bounded cost: trivial propose-only prompt, max_turns=5, single-call expectation.
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


_IDEATION_PROMPT = (
    "TEST SCENARIO — do NOT assess the project, read files, or write "
    "ideation_state. Your ONLY job is to exercise the board_edit propose "
    "path. Call the `mcp__autopilot__board_edit` tool EXACTLY ONCE with "
    "these args to propose a new Backlog task:\n"
    "  action: add_backlog\n"
    "  title: Smoke parity probe\n"
    '  tags: ["#smoke"]\n'
    '  briefing: "## Goal\\n\\nSmoke probe.\\n"\n'
    "Then end your turn IMMEDIATELY — do NOT call board_edit again and do "
    "NOT call any other tool, even if the call returns an error. The daemon "
    "only needs to confirm the propose-path wiring delivers the call."
)


@pytest.mark.parametrize("backend", BACKENDS)
def test_ideation_board_edit_propose_via_adapter(backend, monkeypatch, tmp_path):
    """A real ideation-kind agent, dispatched through `select_adapter("ideation",
    cfg)`, invokes `board_edit` to propose a task — the load-bearing untested
    tool — for BOTH the claude and codex backends. Asserts a `board_edit` call
    with an add-* (propose) action + a non-empty title lands in the normalized
    stream."""
    gate_backend(backend)
    force_backend(monkeypatch, "ideation", backend)

    from ap2.tools import IDEATION_TOOLS

    # TB-351: a transient SDK transport/service error (or a missing credential)
    # is *raised* out of the adapter drain — retry once, then skip (not error).
    # A genuine wiring regression (board_edit not called) flows to the asserts
    # below and still fails.
    tool_calls = call_with_transient_retry(
        lambda: run_control_to_tool_calls(
            kind="ideation",
            backend=backend,
            prompt=_IDEATION_PROMPT,
            allowed_tools=IDEATION_TOOLS,
            root=tmp_path,
        ),
        describe=f"ideation board_edit propose smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    # The backend delivers the MCP-server-prefixed tool name; accept both forms.
    edits = [
        tc for tc in tool_calls
        if tc["name"] in ("board_edit", "mcp__autopilot__board_edit")
    ]
    assert edits, (
        f"[{backend}] ideation agent did not call board_edit. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )

    args = edits[-1]["input"]
    action = str(args.get("action") or "")
    assert action.startswith("add"), (
        f"[{backend}] board_edit was not a propose/add action: {args!r}"
    )
    assert str(args.get("title") or "").strip(), (
        f"[{backend}] board_edit propose carried no title: {args!r}"
    )
    print(f"[smoke:{backend}] PASS — board_edit propose args={args!r}")
