"""Real-SDK round-trip for the `pipeline_task_start` MCP tool (TB-81).

Validates the full chain that `pipeline_start` events in stoch's
events.jsonl prove implicitly:

  1. The tool is advertised by the autopilot MCP server (covered by
     `test_mcp_inventory.py` without a real SDK; this pins it via the
     real-SDK round-trip too).
  2. A real Claude agent, given a briefing that asks for a pipeline
     launch, calls the tool with the structured args.
  3. The tool spawns a real OS subprocess, captures its pid, creates a
     Backlog validation task with `(blocked on: pid:N@TS)`, and emits a
     `pipeline_start` event.

The pipeline command is intentionally trivial (`sleep 0.5`) so the test
finishes in seconds. We assert on the side effects (events.jsonl entry,
Backlog task) — that's the canonical contract `Board.next_dispatchable`
+ the validation task auto-promote rely on.

OPT-IN via `AP2_REAL_SDK=1`.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

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
        "## Complete\n\n## Frozen\n"
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
            "  validation_title: Validate smoke pipeline output\n"
            "  validation_briefing: |\n"
            "    ## Goal\n"
            "    Confirm the smoke pipeline ran.\n\n"
            "    ## Verification\n\n"
            "    - `true` — placeholder\n"
            "Do NOT do any real work. Do NOT read or edit other files. "
            "Just call the two MCP tools and return."
        ),
    )


def test_pipeline_task_start_round_trip_via_real_sdk():
    """Real Claude agent + real MCP server → real subprocess + real
    Backlog validation task."""
    import claude_agent_sdk as sdk

    from ap2 import events as ev_mod
    from ap2.board import Board
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _bootstrap_project(root)
            mcp_server = build_mcp_server(cfg)
            prompt = build_task_prompt(cfg, _fake_pipeline_task())

            tool_calls: list[dict] = []
            opts = sdk.ClaudeAgentOptions(
                cwd=str(root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=TASK_AGENT_TOOLS,
                disallowed_tools=["Bash(git push*)", "Bash(rm -rf *)"],
                permission_mode="bypassPermissions",
                max_turns=8,
                setting_sources=["project"],
            )
            async for msg in sdk.query(prompt=prompt, options=opts):
                for part in (getattr(msg, "content", None) or []):
                    name = getattr(part, "name", None)
                    inp = getattr(part, "input", None)
                    if name and inp is not None:
                        tool_calls.append({"name": name, "input": inp})

            # Side effects to pin
            evts = ev_mod.tail(cfg.events_file, n=20)
            pipe_starts = [e for e in evts if e.get("type") == "pipeline_start"]
            board = Board.load(cfg.tasks_file)
            backlog_tasks = list(board.iter_tasks(section="Backlog"))
            return tool_calls, pipe_starts, backlog_tasks, root

    tool_calls, pipe_starts, backlog_tasks, root = asyncio.run(go())

    print(f"\n[smoke] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    pipeline_calls = [
        tc for tc in tool_calls
        if tc["name"] in (
            "pipeline_task_start", "mcp__autopilot__pipeline_task_start"
        )
    ]
    assert pipeline_calls, (
        f"agent did not call pipeline_task_start. Tools used: "
        f"{[tc['name'] for tc in tool_calls]}"
    )

    # The handler emitted exactly one pipeline_start event.
    assert len(pipe_starts) == 1, (
        f"expected exactly 1 pipeline_start event, got {len(pipe_starts)}"
    )
    started = pipe_starts[0]
    assert started.get("name") == "smoke-sleep", started
    assert isinstance(started.get("pid"), int), started
    print(f"[smoke] pipeline_start: pid={started['pid']} "
          f"validation={started.get('validation')}")

    # The handler created exactly one Backlog validation task gated on
    # the pid.
    pid_blockers = [
        t for t in backlog_tasks
        if any(b.startswith("pid:") for b in t.blocked_on)
    ]
    assert len(pid_blockers) == 1, (
        f"expected 1 Backlog task with pid:N@TS blocker, got "
        f"{len(pid_blockers)} (all backlog: "
        f"{[(t.id, t.title) for t in backlog_tasks]})"
    )
    val_task = pid_blockers[0]
    assert val_task.id == started["validation"], (
        f"validation task id mismatch: event says {started['validation']}, "
        f"board has {val_task.id}"
    )
    print(f"[smoke] PASS — validation task {val_task.id} "
          f"blocked_on={val_task.blocked_on}")

    # Wait briefly for the sleep 0.5 subprocess to exit so we don't leave
    # zombies in the test runner's process tree.
    time.sleep(1.0)
