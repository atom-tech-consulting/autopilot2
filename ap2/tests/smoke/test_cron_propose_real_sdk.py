"""Real-SDK round-trip for the `cron_propose` MCP tool (TB-123).

Mirrors `test_report_result_real_sdk.py`. Validates what FakeSDK can't:

  1. The `@tool` decorator's inputSchema for `cron_propose` is acceptable
     to the SDK.
  2. The MCP server registration delivers the tool to the agent (it's
     visible in the deferred-tools surface — the same Claude Code
     filtering issue that motivated dropping `task_*` prefixes for
     `report_result` could regress here).
  3. A real Claude agent, given the production task-agent prompt + an
     explicit instruction to call `cron_propose`, actually calls it.
  4. The tool_use block lands in the daemon's stream in the shape
     `daemon._log_message` walks for (`.name`, `.input`).
  5. The captured args dict has the four expected keys (name, schedule,
     prompt, rationale).

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK
is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

Bounded cost: trivial task body, max_turns=5, single-call expectation.
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
        title="cron_propose smoke",
        section="Active",
        description=(
            "TEST SCENARIO — do not do any work. Do not read or edit any "
            "files. Do not run any commands. Just call the "
            "`mcp__autopilot__cron_propose` tool ONCE with these args:\n"
            "  name: weekly-smoke\n"
            "  schedule: 1d\n"
            '  prompt: "Run the smoke probe"\n'
            '  rationale: "Daily smoke covers the cron-propose wiring"\n'
            "Then call `mcp__autopilot__report_result` ONCE with:\n"
            "  status: complete\n"
            "  commit: \"\"\n"
            "  summary: \"smoke test ok\"\n"
            "  files_changed: \"\"\n"
            "  tests_passed: \"true\"\n"
            "Then end your turn. The daemon needs to confirm the tool "
            "wiring works end-to-end."
        ),
    )


def test_cron_propose_round_trip_via_real_sdk():
    """Real Claude agent + real MCP server. Asserts the cron_propose tool
    call lands and carries the four expected args."""
    import asyncio

    import claude_agent_sdk as sdk

    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go() -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)
            mcp_server = build_mcp_server(cfg)
            prompt = build_task_prompt(cfg, _fake_task())

            tool_calls: list[dict] = []

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
            return tool_calls

    tool_calls = asyncio.run(go())

    print(f"\n[smoke] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    # Real SDK delivers the MCP-server-prefixed tool name; accept both forms.
    proposals = [
        tc for tc in tool_calls
        if tc["name"] in ("cron_propose", "mcp__autopilot__cron_propose")
    ]
    assert proposals, (
        f"agent did not call cron_propose. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )

    args = proposals[-1]["input"]
    assert args.get("name") == "weekly-smoke", args
    assert args.get("schedule") == "1d", args
    assert args.get("prompt"), args
    assert args.get("rationale"), args
    print(
        f"[smoke] PASS — cron_propose args={args!r}"
    )
