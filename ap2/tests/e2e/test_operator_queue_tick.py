"""TB-131: daemon `_tick` drains the operator queue as its first stage.

The point: by the time the per-tick task / cron / ideation stages read the
board, every queued operator op has already landed. Concretely we queue an
add_backlog and a Ready task in the same project, then run one tick. After
the tick the new TB-N is on the board (drained) AND the Ready task ran to
Complete (downstream stages observed the post-drain board).
"""
from __future__ import annotations

import asyncio

from ap2 import events, tools
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests._briefing_fixtures import canonical_briefing
from ap2.tests.e2e._fakes import FakeSDK, tool_call_respond


def test_tick_drains_operator_queue_before_task(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "the task"))

    # Operator queues an add_backlog. ID is pre-allocated synchronously,
    # but TASKS.md is not yet mutated.
    body = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "queued by operator",
            # TB-135: briefing is required for every add_*; the CLI
            # passes the buffer it read from --briefing-file.
            "briefing": canonical_briefing(
                "TB-600", title="queued by operator",
            ),
        },
    )
    import json
    qbody = json.loads(body["content"][0]["text"])
    queued_id = qbody["task_id"]
    # Pre-tick: queued task is NOT on the board yet.
    assert Board.load(cfg.tasks_file).find(queued_id) is None

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        tool_call_respond(
            "report_result",
            {
                "status": "complete",
                "commit": "deadbeef",
                "summary": "ran with drained queue",
                "files_changed": "",
                "tests_passed": "true",
            },
        ),
    )

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # Queued op was drained and landed in Backlog.
    assert board.find(queued_id)[0] == "Backlog"
    # The Ready task's stage observed the post-drain board and ran to Complete.
    assert board.find("TB-5")[0] == "Complete"

    # The drain-side audit event landed in events.jsonl.
    evts = events.tail(cfg.events_file, 30)
    drained = [e for e in evts if e["type"] == "operator_queue_drained"]
    assert len(drained) == 1
    assert drained[0]["applied"] == 1


def test_tick_with_empty_queue_is_a_noop_and_safe(e2e_project):
    """No queued ops → drain returns 0, no event spam, the rest of the
    tick proceeds normally. Smoke-tests the empty-queue path so the
    drain step doesn't accidentally regress when queue work is absent.
    """
    cfg = e2e_project()

    sdk = FakeSDK()
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 30)
    # No drained event when queue was empty (the drain stage is silent).
    assert not any(e["type"] == "operator_queue_drained" for e in evts)
    assert not any(e["type"] == "operator_queue_drain_error" for e in evts)


# ---------------------------------------------------------------------------
# TB-141: operator_queue.jsonl is NOT a fenced path. Pre-TB-141 this file
# was in TASK_AGENT_FENCED_PATHS, so any `do_operator_queue_append` call
# made during a task agent's run (e.g. operator running `ap2 add`)
# mutated a fenced path and tripped TB-110's snapshot check, rolling back
# legitimate task work (TB-139, 2026-05-01). These tests pin the fix at
# the snapshot-comparison layer and at the full daemon-tick layer.


def test_operator_queue_append_does_not_appear_in_fenced_snapshot(e2e_project):
    """Snapshot-level pin: take a snapshot, append to the operator queue,
    and confirm `detect_fenced_violations` returns []. Pre-TB-141 the
    queue.jsonl mutation showed up here as a violation."""
    from ap2 import rollback, tools

    cfg = e2e_project(ready_task=("TB-5", "in flight"))

    # Pretend the daemon just moved TB-5 to Active and snapshotted.
    pre = rollback.snapshot_fenced_files(cfg)

    # Operator runs `ap2 add` mid-flight (the TB-139 scenario). This
    # appends to .cc-autopilot/operator_queue.jsonl synchronously.
    body = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "queued during run",
            "briefing": canonical_briefing(
                "TB-601", title="queued during run",
            ),
        },
    )
    assert not body.get("isError")

    # No violation: queue.jsonl is no longer fenced (TB-141), and
    # CLAUDE.md is no longer bumped synchronously (deferred to drain).
    violations = rollback.detect_fenced_violations(cfg, pre)
    assert violations == [], (
        f"expected no fenced violations after operator_queue_append, "
        f"got: {violations}"
    )


def test_tb139_scenario_mid_run_ap2_add_does_not_trip_violation(e2e_project):
    """End-to-end TB-139 regression: a task is in flight, the operator
    runs `ap2 add` (which appends to queue.jsonl), the task completes
    cleanly via report_result. Pre-TB-141 this triggered
    task_state_violation against the in-flight task because both
    queue.jsonl AND CLAUDE.md mutated synchronously during the agent's
    window. Now the run completes normally.
    """
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace
    from typing import AsyncIterator

    from ap2 import tools

    def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(cwd)] + args,
            capture_output=True, text=True, check=True,
        )

    cfg = e2e_project(ready_task=("TB-5", "running while operator types ap2 add"))
    # _git_init equivalent — the violation check is only meaningful in a
    # git repo (the rollback helper short-circuits otherwise).
    _git(["init", "--initial-branch=main"], cfg.project_root)
    _git(["config", "user.email", "test@example.com"], cfg.project_root)
    _git(["config", "user.name", "Test"], cfg.project_root)
    _git(["commit", "--allow-empty", "-m", "init"], cfg.project_root)
    _git(["add", "TASKS.md", "CLAUDE.md"], cfg.project_root)
    _git(["commit", "-m", "baseline"], cfg.project_root)

    # The agent's behavior: while it's running, the operator appends to
    # the queue (simulating a concurrent `ap2 add`). Then the agent
    # emits report_result normally.
    def factory(prompt, options):  # noqa: ARG001
        async def _gen() -> AsyncIterator:
            tools.do_operator_queue_append(
                cfg,
                {
                    "op": "add_backlog",
                    "title": "operator typed this mid-run",
                    "briefing": canonical_briefing(
                        "TB-602", title="operator brief",
                    ),
                },
            )
            yield SimpleNamespace(content=[
                SimpleNamespace(
                    name="report_result",
                    input={"status": "complete", "summary": "did the work"},
                    id="t1",
                ),
            ])
        return _gen()

    sdk = FakeSDK()
    sdk.on("## Task\nTB-5", factory)

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # No violation event — queue.jsonl mutation no longer counts.
    evts = events.tail(cfg.events_file, 50)
    violations = [e for e in evts if e["type"] == "task_state_violation"]
    assert not violations, f"unexpected violation: {violations}"

    # TB-5 completed normally (not bounced to Backlog).
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"
