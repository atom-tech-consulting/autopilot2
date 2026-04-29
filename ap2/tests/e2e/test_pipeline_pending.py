"""TB-114: a task agent that calls `pipeline_task_start` then
`report_result(status='complete')` is parked in `Pipeline Pending`.
The daemon's per-tick sweep verifies the briefing's `## Verification`
once every spawned pid is dead, then routes to Complete or Backlog.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, Callable

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK


def _git_init(cwd: Path) -> None:
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=cwd, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=cwd, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=cwd, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=cwd,
                   check=True, capture_output=True)
    subprocess.run(["git", "add", "TASKS.md", "CLAUDE.md"], cwd=cwd, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=cwd, check=True,
                   capture_output=True)


def _spawn_pipeline_then_report(name: str, command: str, summary: str) -> Callable:
    """Async-gen factory: a task agent that calls pipeline_task_start once,
    then report_result(complete). The MCP server (real, supplied by FakeSDK
    via daemon.run_task — not just a mock) actually spawns the subprocess
    and returns its pid, which we then echo into a `tool_use_id`-paired
    tool_result block so the daemon's capture logic can correlate.

    Since FakeSDK is a no-MCP scripted stub, we simulate the full message
    shape: ToolUseBlock for pipeline_task_start, then a ToolResultBlock
    with the daemon-side `_ok(...)` shape (JSON in `text`), then a
    ToolUseBlock for report_result. The daemon's `_log_message` walks
    these and captures both the args and the result payload.
    """
    pass  # Replaced by inline factories below — function kept as a marker.


def _tool_use_block(name: str, inp: dict, tool_id: str):
    return SimpleNamespace(name=name, input=inp, id=tool_id)


def _tool_result_block(tool_use_id: str, payload: dict):
    """Mimic the SDK's ToolResultBlock shape. The block has tool_use_id,
    is_error=False, and content as a string (JSON-encoded payload)."""
    import json as _json
    return SimpleNamespace(
        tool_use_id=tool_use_id,
        is_error=False,
        content=_json.dumps(payload),
    )


def _msg(blocks):
    return SimpleNamespace(content=blocks)


def test_pipeline_dispatch_parks_task_in_pipeline_pending(e2e_project):
    """Agent dispatches a real subprocess + reports complete → daemon
    parks the task in Pipeline Pending and emits task_pipeline_pending."""
    cfg = e2e_project(ready_task=("TB-5", "long fetch"))
    _git_init(cfg.project_root)

    # We need a real subprocess pid. Spawn one ourselves (sleep 30) and
    # have the FakeSDK script feed back tool_result blocks pretending the
    # MCP tool produced this pid.
    proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        sdk = FakeSDK()

        async def gen(prompt, options):  # noqa: ARG001
            # Tool use: pipeline_task_start
            yield _msg([_tool_use_block(
                "mcp__autopilot__pipeline_task_start",
                {"name": "fetch-spy", "command": "sleep 30"},
                "tu_pipeline_1",
            )])
            # Tool result with the pid we just spawned
            yield _msg([_tool_result_block(
                "tu_pipeline_1",
                {"message": "ok", "pid": proc.pid,
                 "started_at": int(time.time()), "log": "/tmp/x.log"},
            )])
            # Tool use: report_result
            yield _msg([_tool_use_block(
                "mcp__autopilot__report_result",
                {"status": "complete", "summary": "dispatched fetch-spy",
                 "commit": ""},
                "tu_report_1",
            )])

        sdk.on("## Task\nTB-5", lambda p, o: gen(p, o))

        asyncio.run(_tick(cfg, sdk, mcp_server=None))

        # Task is parked in Pipeline Pending — NOT Complete, NOT Backlog.
        b = Board.load(cfg.tasks_file)
        assert b.find("TB-5") is not None
        assert b.find("TB-5")[0] == "Pipeline Pending"

        # task_pipeline_pending event was emitted with the captured pid.
        evts = events.tail(cfg.events_file, 30)
        pp = [e for e in evts if e["type"] == "task_pipeline_pending"]
        assert len(pp) == 1
        assert pp[0]["task"] == "TB-5"
        pls = pp[0]["pipelines"]
        assert len(pls) == 1
        assert pls[0]["pid"] == proc.pid
        assert pls[0]["name"] == "fetch-spy"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass


def test_pipeline_pending_sweep_completes_when_pid_dies(e2e_project):
    """After the agent parks the task in Pipeline Pending, the daemon's
    next-tick sweep notices the pid is dead and runs verification. With
    no per-task verification briefing AND no AP2_VERIFY_CMD, the sweep
    short-circuits to Complete.
    """
    cfg = e2e_project(ready_task=("TB-5", "fast pipeline"))
    _git_init(cfg.project_root)

    proc = subprocess.Popen(["true"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()  # exits immediately
    dead_pid = proc.pid
    started_at = int(time.time())

    sdk = FakeSDK()

    async def gen(prompt, options):  # noqa: ARG001
        yield _msg([_tool_use_block(
            "mcp__autopilot__pipeline_task_start",
            {"name": "p", "command": "true"},
            "tu_p_1",
        )])
        yield _msg([_tool_result_block(
            "tu_p_1",
            {"message": "ok", "pid": dead_pid, "started_at": started_at,
             "log": "/tmp/x.log"},
        )])
        yield _msg([_tool_use_block(
            "mcp__autopilot__report_result",
            {"status": "complete", "summary": "dispatched", "commit": ""},
            "tu_r_1",
        )])

    sdk.on("## Task\nTB-5", lambda p, o: gen(p, o))

    # First tick parks in Pipeline Pending.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Pipeline Pending"

    # Second tick: pid is dead → sweep moves to Complete.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    b = Board.load(cfg.tasks_file)
    assert b.find("TB-5")[0] == "Complete"

    evts = events.tail(cfg.events_file, 50)
    completes = [e for e in evts if e["type"] == "task_complete"
                 and e.get("task") == "TB-5"]
    # Two task_complete events (the launch's pipeline_pending one + the
    # sweep's complete one). Sweep one carries source="pipeline_pending".
    sweep_complete = [e for e in completes if e.get("source") == "pipeline_pending"]
    assert len(sweep_complete) == 1
    assert sweep_complete[0]["status"] == "complete"


def test_pipeline_pending_sweep_waits_until_all_pids_die(e2e_project):
    """A task that dispatches multiple pipelines stays in Pipeline Pending
    until every pid has died. Two pids: one dead, one alive → still
    pending. After the live one dies → Complete.
    """
    cfg = e2e_project(ready_task=("TB-5", "two pipelines"))
    _git_init(cfg.project_root)

    dead = subprocess.Popen(["true"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dead.wait()
    alive = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ts = int(time.time())
        sdk = FakeSDK()

        async def gen(prompt, options):  # noqa: ARG001
            for i, (pid, name) in enumerate([(dead.pid, "p1"), (alive.pid, "p2")], 1):
                yield _msg([_tool_use_block(
                    "mcp__autopilot__pipeline_task_start",
                    {"name": name, "command": "x"},
                    f"tu_p_{i}",
                )])
                yield _msg([_tool_result_block(
                    f"tu_p_{i}",
                    {"message": "ok", "pid": pid, "started_at": ts,
                     "log": f"/tmp/{name}.log"},
                )])
            yield _msg([_tool_use_block(
                "mcp__autopilot__report_result",
                {"status": "complete", "summary": "dispatched 2", "commit": ""},
                "tu_r_1",
            )])

        sdk.on("## Task\nTB-5", lambda p, o: gen(p, o))

        asyncio.run(_tick(cfg, sdk, mcp_server=None))
        assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Pipeline Pending"

        # Sweep with the second pid still alive — task stays pending.
        asyncio.run(_tick(cfg, sdk, mcp_server=None))
        assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Pipeline Pending"

        # Kill the live one and sweep again.
        alive.terminate()
        alive.wait(timeout=2)
        asyncio.run(_tick(cfg, sdk, mcp_server=None))
        assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Complete"
    finally:
        try:
            alive.terminate()
            alive.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass


def test_pipeline_pending_sweep_routes_to_backlog_on_verification_failure(
    e2e_project, monkeypatch,
):
    """Project-wide verifier fails post-pipeline → task goes Backlog (with
    retry-counter bump), not Complete. Briefing-less task uses only the
    project-wide gate.
    """
    monkeypatch.setenv("AP2_VERIFY_CMD", "false")  # always fails
    cfg = e2e_project(ready_task=("TB-5", "verify fails"))
    _git_init(cfg.project_root)

    proc = subprocess.Popen(["true"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    dead_pid = proc.pid
    ts = int(time.time())

    sdk = FakeSDK()

    async def gen(prompt, options):  # noqa: ARG001
        yield _msg([_tool_use_block(
            "mcp__autopilot__pipeline_task_start",
            {"name": "p", "command": "true"},
            "tu_p_1",
        )])
        yield _msg([_tool_result_block(
            "tu_p_1",
            {"message": "ok", "pid": dead_pid, "started_at": ts,
             "log": "/tmp/x.log"},
        )])
        yield _msg([_tool_use_block(
            "mcp__autopilot__report_result",
            {"status": "complete", "summary": "dispatched", "commit": ""},
            "tu_r_1",
        )])

    sdk.on("## Task\nTB-5", lambda p, o: gen(p, o))

    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Pipeline Pending"

    asyncio.run(_tick(cfg, sdk, mcp_server=None))
    # The sweep emitted `verification_failed` with source=pipeline_pending
    # (the contract being tested). Note: in this test the same FakeSDK
    # script keeps re-firing on auto-promote, so the task may bounce
    # Backlog → Ready → Active → Pipeline Pending again within the same
    # tick — the end-board state isn't pinned, but the failure event is.
    evts = events.tail(cfg.events_file, 100)
    failures = [e for e in evts if e["type"] == "verification_failed"
                and e.get("task") == "TB-5"]
    assert failures
    assert any(e.get("source") == "pipeline_pending" for e in failures)


def test_pipeline_pending_section_in_init_template():
    """Pin the new section into the init template. Existing projects
    (predating TB-114) lack the header but the parser tolerates that —
    they just have an empty Pipeline Pending in-memory until the daemon
    moves something there, at which point save() rewrites the file with
    the header included.
    """
    from ap2.init import TASKS_TEMPLATE
    assert "## Pipeline Pending" in TASKS_TEMPLATE
    # Order: Backlog → Pipeline Pending → Complete.
    bl = TASKS_TEMPLATE.index("## Backlog")
    pp = TASKS_TEMPLATE.index("## Pipeline Pending")
    cp = TASKS_TEMPLATE.index("## Complete")
    assert bl < pp < cp


def test_pipeline_pending_in_diagnose_meaningful_events():
    from ap2.diagnose import MEANINGFUL_EVENT_TYPES
    assert "task_pipeline_pending" in MEANINGFUL_EVENT_TYPES
