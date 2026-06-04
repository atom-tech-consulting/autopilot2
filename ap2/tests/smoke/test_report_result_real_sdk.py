"""Real-SDK tool-round-trip for the `report_result` MCP tool, parametrized over
BOTH adapter backends (TB-101; adapter-routed + backend-parametrized in TB-374).

Validates what FakeSDK can't, now for both Claude AND codex:

  1. The `@tool` decorator's inputSchema is acceptable to the backend.
  2. The MCP server registration delivers the tool to the agent.
  3. A real agent, given the production task-agent prompt + an explicit
     instruction to call `report_result`, actually calls it.
  4. The tool_use block lands in the adapter's normalized event stream in the
     shape `daemon.run_task._log_message` walks for (`.name`, `.input`) — the
     Claude `ToolUseBlock` shape AND the codex `mcpToolCall` shape.
  5. The captured args dict produces a valid `complete` `TaskResult` via
     `daemon._task_result_from_tool_args`.

TB-374: this smoke dispatches through the production `AgentAdapter` seam —
`select_adapter("task", cfg)` + `adapter.run(...)` with the backend-neutral
`AgentTools` / `AgentOptions` — instead of hardcoding `claude_agent_sdk`'s
`sdk.query`, and is parametrized over the `claude` and `codex` backends so the
SAME assertion runs against both. Pointing the `task` kind at codex
(`AP2_AGENT_BACKEND_TASK=codex`, the operator capability `force_backend` sets)
now actually exercises a live codex agent CALLING `report_result` over the
TB-373 stdio-MCP bridge — coverage the name-only parity test and the no-tool
codex dispatch smoke both omit.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip`
in `gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors; a missing credential / transport hiccup flows through the
shared transient-retry-then-skip helper, identical to the Claude path.

The task is intentionally trivial ("don't do any work, just call the tool") to
bound cost and isolate the wiring test from any implementation-related agent
reasoning.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ._adapter import BACKENDS, extract_tool_calls, force_backend, gate_backend
from ._transient import call_with_transient_retry

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


def _bootstrap_project(root: Path):
    """Minimal project skeleton needed by Config.load + build_task_prompt."""
    from ap2.config import Config

    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (root / "CLAUDE.md").write_text(
        "# Smoke project\n\n## Autopilot\n\n- Next task ID: TB-2\n"
    )
    cfg = Config.load(root)
    cfg.ensure_dirs()
    return cfg


def _fake_task():
    from ap2.board import Task

    return Task(
        id="TB-1",
        title="report_result smoke",
        section="Active",
        description=(
            "TEST SCENARIO — do not do any work. Do not read or edit any "
            "files. Do not run any commands. Just call the "
            "`mcp__autopilot__report_result` tool ONCE with these args:\n"
            "  status: complete\n"
            "  commit: \"\"\n"
            "  summary: \"smoke test ok\"\n"
            "  files_changed: \"\"\n"
            "  tests_passed: \"true\"\n"
            "Then end your turn. The daemon needs to confirm the tool "
            "wiring works end-to-end."
        ),
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_report_result_round_trip_via_adapter(backend, monkeypatch):
    """Real agent + real MCP server, dispatched through the `AgentAdapter` seam.
    Asserts the tool call lands in the normalized stream and converts to a valid
    `complete` `TaskResult` — for BOTH the claude and codex backends."""
    import asyncio

    gate_backend(backend)
    # Pin the `task` kind to this backend so `select_adapter("task", cfg)`
    # resolves to the matching adapter — the operator's "set the kind's backend
    # to codex and run the existing smoke" capability.
    force_backend(monkeypatch, "task", backend)

    from ap2.adapters import AgentOptions, AgentTools, select_adapter
    from ap2.daemon import _task_result_from_tool_args
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go() -> tuple[list[dict], str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)

            # Dispatch flows through the SAME seam production uses: the per-kind
            # backend resolver + the streaming `AgentAdapter.run(...)`, with the
            # full ap2 toolset registered through the selected adapter.
            adapter = select_adapter("task", cfg)
            assert adapter.backend == backend, adapter.backend
            mcp_server = build_mcp_server(cfg, adapter=adapter)
            prompt = build_task_prompt(cfg, _fake_task())

            tools = AgentTools(
                allowed=TASK_AGENT_TOOLS,
                disallowed=["Bash(git push*)", "Bash(rm -rf *)"],
                mcp_servers={"autopilot": mcp_server},
            )
            options = AgentOptions(
                cwd=str(root),
                permission_mode="bypassPermissions",
                max_turns=5,
                setting_sources=["project"],
            )
            if backend == "codex":
                # Bound cost + a safe read-only sandbox, mirroring the codex
                # dispatch smoke; the report_result handler runs in the stdio
                # MCP bridge subprocess, not under codex's sandbox.
                options.effort = "low"
                options.extra = {"sandbox": "read-only"}

            tool_calls: list[dict] = []
            final_text = ""
            async for ev in adapter.run(prompt, tools, options):
                if ev.result is not None:
                    continue
                tool_calls.extend(extract_tool_calls(ev.raw))
                if ev.text:
                    final_text = ev.text
            return tool_calls, final_text

    # TB-351: a transient SDK transport/service error (or a missing credential)
    # is *raised* out of the adapter drain — retry once, then skip (not error).
    # A genuine wiring regression (tool not called) flows to the `assert
    # completes` below and still fails.
    tool_calls, final_text = call_with_transient_retry(
        lambda: asyncio.run(go()),
        describe=f"report_result round-trip smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    # The backend delivers the MCP-server-prefixed tool name; accept both forms
    # so the test pins against whichever the backend reports today.
    completes = [
        tc for tc in tool_calls
        if tc["name"] in ("report_result", "mcp__autopilot__report_result")
    ]
    assert completes, (
        f"[{backend}] agent did not call report_result. Final text: "
        f"{final_text[:500]!r}. Tools used: {[tc['name'] for tc in tool_calls]}"
    )

    args = completes[-1]["input"]
    result = _task_result_from_tool_args(args)
    assert result.status == "complete", result
    assert "smoke" in result.summary.lower(), result
    print(f"[smoke:{backend}] PASS — TaskResult.status={result.status!r}, "
          f"summary={result.summary!r}")
