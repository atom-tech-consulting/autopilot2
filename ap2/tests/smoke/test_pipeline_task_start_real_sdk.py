"""Real-SDK tool-round-trip for the `pipeline_task_start` MCP tool, parametrized
over BOTH adapter backends (TB-115; adapter-routed + parametrized in TB-374).

Validates the full chain end-to-end through the `AgentAdapter` seam (without
`daemon.run_task`'s capture / Pipeline Pending parking — those are covered by
the FakeSDK e2e in test_pipeline_pending.py), now for both Claude AND codex:

  1. The tool is advertised by the autopilot MCP server (covered by
     `test_mcp_inventory.py` without a real SDK; this pins it via the real-SDK
     round-trip too).
  2. A real agent, given a briefing that asks for a pipeline launch, calls
     `pipeline_task_start(name, command)` with the structured args — captured
     off the adapter's normalized event stream.
  3. The tool spawns a real OS subprocess, captures its pid, and emits a
     `pipeline_start` event with `name` + `pid` + `started_at` + `log`.

Pre-TB-115 the tool also created a Backlog validation task gated on `pid:N@TS`;
that pattern was retired (TB-115 + TB-117) — the launching task itself carries
verification, parked in `Pipeline Pending` by the daemon. This smoke only
exercises the MCP tool surface, so it doesn't involve the Pipeline Pending move.

TB-374: this smoke dispatches through the production `AgentAdapter` seam —
`select_adapter("task", cfg)` + `adapter.run(...)` with the backend-neutral
`AgentTools` / `AgentOptions` — instead of hardcoding `claude_agent_sdk`'s
`sdk.query`, and is parametrized over the `claude` and `codex` backends so the
SAME assertion (a real tool invocation + its structured-arg round-trip + the
real `pipeline_start` side effect) runs against both. The codex variant rides
the TB-373 stdio-MCP bridge that delivers ap2's toolset to a live codex agent.

The pipeline command is intentionally trivial (`sleep 0.5`) so the test
finishes in seconds.

OPT-IN via `AP2_REAL_SDK=1`. The codex variant carries a secondary
`openai_codex` `importorskip` gate (in `gate_backend`); a missing credential /
transport hiccup flows through the shared transient-retry-then-skip helper.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from ._adapter import BACKENDS, extract_tool_calls, force_backend, gate_backend
from ._transient import call_with_transient_retry

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


def _bootstrap_project(root: Path):
    """Project shell with a single Active task whose briefing asks for a
    pipeline_task_start call."""
    from ap2.config import Config

    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    (root / "CLAUDE.md").write_text(
        "# Smoke project\n\n## Autopilot\n\n- Next task ID: TB-2\n"
    )
    cfg = Config.load(root)
    cfg.ensure_dirs()
    return cfg


def _fake_pipeline_task():
    from ap2.board import Task

    return Task(
        id="TB-1",
        title="pipeline_task_start smoke",
        section="Active",
        description=(
            "TEST SCENARIO — call `mcp__autopilot__pipeline_task_start` "
            "exactly ONCE with these arguments, then call "
            "`mcp__autopilot__report_result(status='complete', "
            "summary='launched smoke pipeline')` and end your turn:\n"
            "  name: smoke-sleep\n"
            "  command: sleep 0.5\n"
            "Do NOT do any real work. Do NOT read or edit other files. "
            "Just call the two MCP tools and return."
        ),
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_pipeline_task_start_round_trip_via_adapter(backend, monkeypatch):
    """Real agent + real MCP server → real subprocess, dispatched through the
    `AgentAdapter` seam. Asserts the tool call + its structured args round-trip
    and the real `pipeline_start` side effect fires — for BOTH backends."""
    gate_backend(backend)
    force_backend(monkeypatch, "task", backend)

    from ap2 import events as ev_mod
    from ap2.adapters import AgentOptions, AgentTools, select_adapter
    from ap2.board import Board
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)

            # Dispatch flows through the production seam: the per-kind backend
            # resolver + the streaming `AgentAdapter.run(...)`.
            adapter = select_adapter("task", cfg)
            assert adapter.backend == backend, adapter.backend
            mcp_server = build_mcp_server(cfg, adapter=adapter)
            prompt = build_task_prompt(cfg, _fake_pipeline_task())

            tools = AgentTools(
                allowed=TASK_AGENT_TOOLS,
                disallowed=["Bash(git push*)", "Bash(rm -rf *)"],
                mcp_servers={"autopilot": mcp_server},
            )
            options = AgentOptions(
                cwd=str(root),
                permission_mode="bypassPermissions",
                max_turns=8,
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

            # Side effects to pin (the handler — in-process for claude, the
            # stdio bridge subprocess for codex — writes to the same project's
            # events file).
            evts = ev_mod.tail(cfg.events_file, n=20)
            pipe_starts = [e for e in evts if e.get("type") == "pipeline_start"]
            board = Board.load(cfg.tasks_file)
            return tool_calls, pipe_starts, board

    # TB-351: a transient SDK transport/service error (or a missing credential)
    # is *raised* out of the adapter drain — retry once, then skip (not error). A
    # genuine wiring regression (pipeline_task_start not called / no event) flows
    # to the asserts below and still fails.
    tool_calls, pipe_starts, board = call_with_transient_retry(
        lambda: asyncio.run(go()),
        describe=f"pipeline_task_start round-trip smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    pipeline_calls = [
        tc for tc in tool_calls
        if tc["name"] in (
            "pipeline_task_start", "mcp__autopilot__pipeline_task_start"
        )
    ]
    assert pipeline_calls, (
        f"[{backend}] agent did not call pipeline_task_start. Tools used: "
        f"{[tc['name'] for tc in tool_calls]}"
    )

    # The captured tool args round-trip the structured pipeline-launch payload.
    pargs = pipeline_calls[-1]["input"]
    assert isinstance(pargs, dict), pargs
    assert str(pargs.get("name") or "").strip() == "smoke-sleep", pargs
    assert str(pargs.get("command") or "").strip() == "sleep 0.5", pargs

    # The handler emitted exactly one pipeline_start event.
    assert len(pipe_starts) == 1, (
        f"[{backend}] expected exactly 1 pipeline_start event, got "
        f"{len(pipe_starts)}"
    )
    started = pipe_starts[0]
    assert started.get("name") == "smoke-sleep", started
    assert isinstance(started.get("pid"), int), started
    assert isinstance(started.get("started_at"), int), started
    assert started.get("log", "").endswith(f"smoke-sleep-{started['pid']}.log"), started
    # TB-115 contract: no `validation` field; no Backlog validation task.
    assert "validation" not in started, (
        f"pipeline_start event leaked retired `validation` field: {started}"
    )
    backlog_tasks = list(board.iter_tasks(section="Backlog"))
    assert backlog_tasks == [], (
        f"unexpected Backlog tasks created (pre-TB-115 contract): "
        f"{[(t.id, t.title) for t in backlog_tasks]}"
    )
    print(f"[smoke:{backend}] PASS — pipeline_start fired for "
          f"pid={started['pid']}; no Backlog side-effect (TB-115 contract)")

    # Wait briefly for the sleep 0.5 subprocess to exit so we don't leave
    # zombies in the test runner's process tree.
    time.sleep(1.0)
