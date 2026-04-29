"""Real-SDK round-trip for the `report_result` MCP tool (TB-101).

Validates what FakeSDK can't:

  1. The `@tool` decorator's inputSchema is acceptable to the SDK.
  2. The MCP server registration delivers the tool to the agent.
  3. A real Claude agent, given the production task-agent prompt + an
     explicit instruction to call `report_result`, actually calls it.
  4. The tool_use block lands in the daemon's stream in the shape
     `daemon._log_message` walks for (`.name`, `.input`).
  5. The captured args dict produces a valid `TaskResult` via
     `daemon._task_result_from_tool_args`.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK
is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The task is intentionally trivial ("don't do any work, just call the
tool") to bound cost and isolate the wiring test from any
implementation-related agent reasoning.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

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


def test_report_result_round_trip_via_real_sdk():
    """Real Claude agent + real MCP server. Asserts the tool call lands and
    converts to a valid TaskResult."""
    import asyncio

    import claude_agent_sdk as sdk

    from ap2.daemon import _task_result_from_tool_args
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go() -> tuple[list[dict], str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)
            mcp_server = build_mcp_server(cfg)
            prompt = build_task_prompt(cfg, _fake_task())

            tool_calls: list[dict] = []
            final_text = ""

            opts = sdk.ClaudeAgentOptions(
                cwd=str(root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=TASK_AGENT_TOOLS,
                disallowed_tools=["Bash(git push*)", "Bash(rm -rf *)"],
                permission_mode="bypassPermissions",
                max_turns=5,
                setting_sources=["project"],
            )
            async for msg in sdk.query(prompt=prompt, options=opts):
                content = getattr(msg, "content", None) or []
                for part in content:
                    name = getattr(part, "name", None)
                    inp = getattr(part, "input", None)
                    if name and inp is not None:
                        tool_calls.append({"name": name, "input": inp})
                    text = getattr(part, "text", None)
                    if isinstance(text, str) and text.strip():
                        final_text = text
            return tool_calls, final_text

    tool_calls, final_text = asyncio.run(go())

    print(f"\n[smoke] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    # Real SDK delivers the MCP-server-prefixed tool name; accept both forms
    # so the test pins against whichever the SDK reports today.
    completes = [
        tc for tc in tool_calls
        if tc["name"] in ("report_result", "mcp__autopilot__report_result")
    ]
    assert completes, (
        f"agent did not call report_result. Final text: {final_text[:500]!r}. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )

    args = completes[-1]["input"]
    result = _task_result_from_tool_args(args)
    assert result.status == "complete", result
    assert "smoke" in result.summary.lower(), result
    print(f"[smoke] PASS — TaskResult.status={result.status!r}, "
          f"summary={result.summary!r}")
