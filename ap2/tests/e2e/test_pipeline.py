"""E2E for TB-114: launch a real subprocess via `pipeline_task_start`,
confirm `pipeline_start` event fires with name/pid/started_at/log, no
side-effect Backlog task is created (pre-TB-114 contract retired), and
the log file captures stdout/stderr.

Pipeline-Pending parking + sweep-driven verification on pid death is
covered in test_state_violation/test_daemon_pipeline_pending — those
involve the full daemon.run_task flow, not just the tool.
"""
from __future__ import annotations

import json
import os
import signal
import time

from ap2 import tools
from ap2.board import Board


def test_pipeline_task_start_emits_event_no_validation_task(e2e_project):
    cfg = e2e_project()

    res = tools.do_pipeline_task_start(
        cfg,
        {"name": "demo", "command": "sleep 1"},
    )
    assert not res.get("isError"), res
    body = json.loads(res["content"][0]["text"])
    pid = body["pid"]
    started_at = body["started_at"]

    # No new task in Backlog — TB-114 retired the auto-validation pattern.
    b = Board.load(cfg.tasks_file)
    assert sum(1 for _ in b.iter_tasks("Backlog")) == 0
    assert sum(1 for _ in b.iter_tasks("Pipeline Pending")) == 0

    # pipeline_start event recorded with the right fields.
    from ap2.events import tail
    evts = tail(cfg.events_file, 20)
    starts = [e for e in evts if e["type"] == "pipeline_start"]
    assert len(starts) == 1
    assert starts[0]["name"] == "demo"
    assert starts[0]["pid"] == pid
    assert starts[0]["started_at"] == started_at
    assert "validation" not in starts[0]

    # Wait for the sleep to exit so we don't leak.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_pipeline_log_file_captures_command_output(e2e_project, tmp_path):
    cfg = e2e_project()
    res = tools.do_pipeline_task_start(
        cfg,
        {"name": "echo-test", "command": "echo hello-pipeline"},
    )
    body = json.loads(res["content"][0]["text"])
    log_path = body["log"]

    from pathlib import Path
    log = Path(log_path)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if log.exists() and "hello-pipeline" in log.read_text():
            break
        time.sleep(0.1)
    assert log.exists()
    assert "hello-pipeline" in log.read_text()
