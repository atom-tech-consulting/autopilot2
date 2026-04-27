"""E2E for TB-81: launch a real subprocess via `pipeline_task_start`, confirm
the validation task lands in Backlog with `(blocked on: pid:N@TS)`, then wait
for the process to exit and confirm `next_dispatchable("Backlog")` auto-promotes
the validation task. No SDK harness needed — this exercises the full board +
pipelines.is_blocking dispatch path end-to-end.
"""
from __future__ import annotations

import os
import signal
import time

from ap2 import pipelines, tools
from ap2.board import Board


def test_pipeline_launch_then_auto_promote_when_process_dies(e2e_project):
    cfg = e2e_project()

    # Use a short-lived sleep so the test doesn't have to wait long.
    res = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "demo",
            "command": "sleep 1",
            "validation_title": "Validate demo output",
            "validation_briefing": "# Validate demo\n\n## Verification\n- `true`\n",
        },
    )
    assert not res.get("isError"), res
    import json as _json
    body = _json.loads(res["content"][0]["text"])
    pid = body["pid"]
    val_id = body["validation_id"]

    # While the pipeline is alive, the validation task is NOT dispatchable.
    b = Board.load(cfg.tasks_file)
    assert b.find(val_id)[0] == "Backlog"
    assert b.next_dispatchable("Backlog") is None

    # Wait for the sleep to exit. The Popen handle wasn't retained, so the
    # child becomes a zombie when it exits — pipelines.is_blocking detects
    # zombie status via psutil and reports unblocked.
    blocker = f"pid:{pid}@{body['started_at']}"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not pipelines.is_blocking(blocker):
            break
        time.sleep(0.1)
    else:  # pragma: no cover — defensive
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise AssertionError("pipeline subprocess did not exit within 5s")

    # Pipeline is gone → validation task auto-promotes on the next call.
    b2 = Board.load(cfg.tasks_file)
    t = b2.next_dispatchable("Backlog")
    assert t is not None and t.id == val_id


def test_pipeline_log_file_captures_command_output(e2e_project, tmp_path):
    cfg = e2e_project()
    res = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "echo-test",
            "command": "echo hello-pipeline",
            "validation_title": "Validate echo",
            "validation_briefing": "# x\n\n## Verification\n- `true`\n",
        },
    )
    import json as _json
    body = _json.loads(res["content"][0]["text"])
    log_path = body["log"]

    # Brief poll: wait for echo to finish + flush.
    from pathlib import Path

    log = Path(log_path)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if log.exists() and "hello-pipeline" in log.read_text():
            break
        time.sleep(0.1)
    assert log.exists()
    assert "hello-pipeline" in log.read_text()
