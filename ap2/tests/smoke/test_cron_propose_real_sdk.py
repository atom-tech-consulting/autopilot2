"""Real-SDK tool-round-trip for the `cron_propose` MCP tool, parametrized over
BOTH adapter backends (TB-123; adapter-routed + backend-parametrized in TB-374).

Mirrors `test_report_result_real_sdk.py`. Validates what FakeSDK can't, now for
both Claude AND codex:

  1. The `@tool` decorator's inputSchema for `cron_propose` is acceptable to the
     backend.
  2. The MCP server registration delivers the tool to the agent (it's visible in
     the deferred-tools surface — the same Claude Code filtering issue that
     motivated dropping `task_*` prefixes for `report_result` could regress
     here).
  3. A real agent, given the production task-agent prompt + an explicit
     instruction to call `cron_propose`, actually calls it.
  4. The tool_use block lands in the adapter's normalized event stream in the
     shape `daemon.run_task._log_message` walks for (`.name`, `.input`) — the
     Claude `ToolUseBlock` shape AND the codex `mcpToolCall` shape.
  5. The captured args dict has the four expected keys (name, schedule, prompt,
     rationale) — the cron_propose structured payload.

TB-374: this smoke dispatches through the production `AgentAdapter` seam —
`select_adapter("task", cfg)` + `adapter.run(...)` with the backend-neutral
`AgentTools` / `AgentOptions` — instead of hardcoding `claude_agent_sdk`'s
`sdk.query`, and is parametrized over the `claude` and `codex` backends so the
SAME assertion runs against both (the codex variant rides the TB-373 stdio-MCP
bridge that delivers ap2's toolset to a live codex agent).

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip`
in `gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors; a missing credential / transport hiccup flows through the
shared transient-retry-then-skip helper.

Bounded cost: trivial task body, max_turns=5, single-call expectation.
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


@pytest.mark.parametrize("backend", BACKENDS)
def test_cron_propose_round_trip_via_adapter(backend, monkeypatch):
    """Real agent + real MCP server, dispatched through the `AgentAdapter` seam.
    Asserts the cron_propose tool call lands in the normalized stream and carries
    the four expected args — for BOTH the claude and codex backends."""
    import asyncio

    gate_backend(backend)
    force_backend(monkeypatch, "task", backend)

    from ap2.adapters import AgentOptions, AgentTools, select_adapter
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go() -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)

            # Dispatch flows through the production seam: the per-kind backend
            # resolver + the streaming `AgentAdapter.run(...)`.
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
                options.effort = "low"
                options.extra = {"sandbox": "read-only"}

            tool_calls: list[dict] = []
            async for ev in adapter.run(prompt, tools, options):
                if ev.result is not None:
                    continue
                tool_calls.extend(extract_tool_calls(ev.raw))
            return tool_calls

    # TB-351: a transient SDK transport/service error (or a missing credential)
    # is *raised* out of the adapter drain — retry once, then skip (not error). A
    # genuine wiring regression (cron_propose not called) flows to the
    # `assert proposals` below and still fails.
    tool_calls = call_with_transient_retry(
        lambda: asyncio.run(go()),
        describe=f"cron_propose round-trip smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    # The backend delivers the MCP-server-prefixed tool name; accept both forms.
    proposals = [
        tc for tc in tool_calls
        if tc["name"] in ("cron_propose", "mcp__autopilot__cron_propose")
    ]
    assert proposals, (
        f"[{backend}] agent did not call cron_propose. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )

    args = proposals[-1]["input"]
    assert args.get("name") == "weekly-smoke", args
    assert args.get("schedule") == "1d", args
    assert args.get("prompt"), args
    assert args.get("rationale"), args
    print(
        f"[smoke:{backend}] PASS — cron_propose args={args!r}"
    )
