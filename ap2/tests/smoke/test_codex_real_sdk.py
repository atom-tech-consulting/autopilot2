"""Real-SDK round-trip for the codex backend's tool-call wiring (TB-359 /
goal.md axis 7).

The codex sibling of `test_report_result_real_sdk.py`: it drives the
`CodexAdapter` (`ap2/adapters/codex.py`) end-to-end against the LIVE `codex`
backend, round-tripping a single `report_result` tool call through ap2's real
MCP toolset (exposed via the adapter's `build_tool_server` surface). Validates
what the hermetic parity suite (`test_adapter_parity.py`) can't: that the codex
backend actually delivers ap2's MCP tools to a live agent and that a real agent
calls one.

OPT-IN — same gate as the Claude real-SDK smokes: this test makes real API
calls. It only runs when `AP2_REAL_SDK` is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The default `pytest` invocation (and CI) skips it via the module-level
`pytestmark` skip marker — IDENTICAL to the Claude smokes
(`test_report_result_real_sdk.py`, `test_cron_propose_real_sdk.py`, …). It is
run on the 6h `real-sdk-smoke` cron routine (`ap2.smoke_runner.run_smoke_check`,
which executes the whole `ap2/tests/smoke/` directory when `AP2_REAL_SDK` is
set) — so dropping this file into `ap2/tests/smoke/` wires it onto that cron
alongside the Claude smokes.

Secondary gate: even with `AP2_REAL_SDK` set, the test skips cleanly when the
codex SDK handle isn't importable (the `CodexAdapter`'s lazy `import codex_sdk`)
— a box that opted into the live smokes but has no codex backend installed skips
rather than errors.

The task body is intentionally trivial ("don't do any work, just call the
tool") to bound cost and isolate the wiring test from agent reasoning, exactly
as the Claude `report_result` smoke does.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ._transient import call_with_transient_retry

# Same opt-in skip marker the Claude real-SDK smokes carry — skips by default
# and in CI; the 6h `real-sdk-smoke` cron is where the live round-trip runs.
pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


def _bootstrap_project(root: Path):
    """Minimal project skeleton needed by Config.load + build_task_prompt
    (mirrors the Claude smokes' helper)."""
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
        title="codex report_result smoke",
        section="Active",
        description=(
            "TEST SCENARIO — do not do any work. Do not read or edit any "
            "files. Do not run any commands. Just call the "
            "`mcp__autopilot__report_result` tool ONCE with these args:\n"
            "  status: complete\n"
            "  commit: \"\"\n"
            "  summary: \"codex smoke test ok\"\n"
            "  files_changed: \"\"\n"
            "  tests_passed: \"true\"\n"
            "Then end your turn. The daemon needs to confirm the codex "
            "tool wiring works end-to-end."
        ),
    )


def test_codex_report_result_round_trip_via_real_sdk():
    """Real codex backend + real ap2 MCP server, driven through the
    `CodexAdapter` seam. Asserts a single `report_result` tool call round-trips
    through the codex stream — the codex analogue of the Claude
    `report_result` smoke."""
    import asyncio

    # Secondary gate: skip cleanly when the codex SDK handle the adapter
    # imports lazily isn't installed (so AP2_REAL_SDK=1 on a box without the
    # codex backend skips rather than errors).
    codex_handle = pytest.importorskip(
        "codex_sdk",
        reason="codex SDK not installed; live codex round-trip unavailable",
    )

    from ap2.adapters import AgentOptions, AgentTools, CodexAdapter
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go() -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)

            adapter = CodexAdapter(codex=codex_handle)
            # ap2's real custom toolset, exposed to the codex backend through
            # the adapter's `build_tool_server` surface (axis 3) — the same
            # toolset the Claude adapter registers (axis-7 parity).
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
            )

            tool_calls: list[dict] = []
            async for ev in adapter.run(prompt, tools, options):
                for tc in ev.summary.get("tool_calls") or []:
                    tool_calls.append(tc)
            return tool_calls

    # A transient codex transport/service error is *raised* out of the drain —
    # retry once, then skip (not error). A genuine wiring regression
    # (report_result not called) flows to the `assert completes` below and
    # still fails.
    tool_calls = call_with_transient_retry(
        lambda: asyncio.run(go()),
        describe="codex report_result round-trip smoke",
    )

    print(f"\n[smoke] {len(tool_calls)} codex tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc.get('name')!r}: {str(tc.get('args_preview'))[:200]}")

    completes = [
        tc for tc in tool_calls
        if "report_result" in str(tc.get("name") or "")
    ]
    assert completes, (
        "codex agent did not call report_result through the adapter. "
        f"Tools used: {[tc.get('name') for tc in tool_calls]}"
    )
    print(
        f"[smoke] PASS — codex round-tripped "
        f"{len(completes)} report_result call(s)"
    )
