"""TB-110: post-hoc state-file violation detection in daemon.run_task.

Hash-snapshots fenced files after `move_to_active`, compares against post-
agent state. Any mismatch (committed by the agent or just dirtied in the
working tree) triggers `task_state_violation` + `git reset --hard <pre_run_head>`
+ Backlog/Frozen routing.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, Callable

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg, tool_call_respond


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True, text=True, check=True,
    )


def _git_init(cwd: Path) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "user.name", "Test"], cwd)
    _git(["commit", "--allow-empty", "-m", "init"], cwd)
    _git(["add", "TASKS.md", "CLAUDE.md"], cwd)
    _git(["commit", "-m", "baseline"], cwd)


def _mutate_then_respond(
    fenced_paths_to_dirty: list[tuple[str, str]],
    *,
    commit: bool = False,
    project_root: Path,
    tool_payload: dict | None = None,
    tool_name: str = "report_result",
) -> Callable:
    """Async-gen factory simulating an agent that mutates fenced files then
    yields a `report_result` tool_use block.

    `fenced_paths_to_dirty` is a list of (relpath, new_content) tuples; each
    is written into project_root before the tool call.
    `commit=True` git-adds + commits the changes with a `<TB-N>:` subject.
    """
    payload = tool_payload or {"status": "complete", "summary": "did it"}

    async def _gen() -> AsyncIterator:
        for rel, content in fenced_paths_to_dirty:
            p = project_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        if commit:
            _git(["add", "--"] + [rel for rel, _ in fenced_paths_to_dirty],
                 project_root)
            _git(["commit", "-m", "TB-5: agent mutation"], project_root)
        yield SimpleNamespace(content=[
            SimpleNamespace(name=tool_name, input=payload, id="t1"),
        ])

    def factory(prompt, options):  # noqa: ARG001
        return _gen()

    return factory


# ---------------------------------------------------------------------------
# 1. Clean run — no violation, normal completion.

def test_clean_run_no_violation(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "clean work"))
    _git_init(cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        tool_call_respond(
            "report_result",
            {"status": "complete", "summary": "ok"},
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"

    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "task_state_violation" for e in evts)


# ---------------------------------------------------------------------------
# 2. Agent commits a fenced-file change → violation, reset, Backlog.

def test_agent_commits_fenced_change_triggers_violation(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "tampering"))
    _git_init(cfg.project_root)
    pre_head = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        _mutate_then_respond(
            [("TASKS.md", "tampered\n")],
            commit=True,
            project_root=cfg.project_root,
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # Repo head is back at pre_run_head (or the post-failure state commit on
    # top of it). Either way, the tampered TASKS.md is gone.
    tasks_md = (cfg.project_root / "TASKS.md").read_text()
    assert "tampered" not in tasks_md

    evts = events.tail(cfg.events_file, 50)
    violations = [e for e in evts if e["type"] == "task_state_violation"]
    assert len(violations) == 1
    assert violations[0]["task"] == "TB-5"
    assert "TASKS.md" in violations[0]["fenced_files"]
    assert violations[0]["pre_run_head"] == pre_head

    # Task is in Backlog after first violation (retry budget remaining).
    board = Board.load(cfg.tasks_file)
    loc = board.find("TB-5")
    assert loc[0] == "Backlog"


# ---------------------------------------------------------------------------
# 3. Agent dirties working-tree fenced file (no commit) → violation.

def test_agent_dirties_working_tree_only(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "wt-dirty"))
    _git_init(cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        _mutate_then_respond(
            [("CLAUDE.md", "## Autopilot\nrewritten by agent\n")],
            commit=False,
            project_root=cfg.project_root,
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # Working tree restored — daemon's reset --hard cleaned it up.
    claude_md = (cfg.project_root / "CLAUDE.md").read_text()
    assert "rewritten by agent" not in claude_md

    evts = events.tail(cfg.events_file, 50)
    violations = [e for e in evts if e["type"] == "task_state_violation"]
    assert len(violations) == 1
    assert "CLAUDE.md" in violations[0]["fenced_files"]


# ---------------------------------------------------------------------------
# 4. Multiple fenced files → all listed in event.

def test_multiple_fenced_files_all_listed(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "multi"))
    _git_init(cfg.project_root)

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-5",
        _mutate_then_respond(
            [
                ("TASKS.md", "tampered\n"),
                ("goal.md", "rewritten\n"),
                (".cc-autopilot/operator_log.md", "spoofed\n"),
            ],
            commit=False,
            project_root=cfg.project_root,
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 50)
    violations = [e for e in evts if e["type"] == "task_state_violation"]
    assert len(violations) == 1
    fenced = violations[0]["fenced_files"]
    assert set(fenced) == {"TASKS.md", "goal.md", ".cc-autopilot/operator_log.md"}
    # Listed in sorted order so the audit trail is stable.
    assert fenced == sorted(fenced)


# ---------------------------------------------------------------------------
# 5. Non-git project → no-op, normal flow.

def test_non_git_project_no_violation_check(e2e_project):
    cfg = e2e_project(ready_task=("TB-5", "no git"))
    # Deliberately do NOT call _git_init.

    sdk = FakeSDK()
    # Even if the agent dirties TASKS.md in a non-git project, no rollback is
    # possible — the helper is a no-op and the task should complete normally
    # via the report_result tool call. The hash check still runs and fires
    # the event, but the rollback step skips silently. We assert the FUNCTIONAL
    # outcome: clean tool call → Complete; no rollback_error.
    sdk.on(
        "## Task\nTB-5",
        tool_call_respond(
            "report_result", {"status": "complete", "summary": "ok"},
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Complete"

    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "rollback_error" for e in evts)


# ---------------------------------------------------------------------------
# 6. Repeated violations exhaust retries → Frozen.

def test_repeated_violations_exhaust_retries_to_frozen(e2e_project, monkeypatch):
    monkeypatch.setenv("AP2_MAX_RETRIES", "2")
    cfg = e2e_project(ready_task=("TB-5", "repeated tamper"))
    _git_init(cfg.project_root)

    sdk = FakeSDK()
    # Each tick the agent dirties fenced files → violation → Backlog.
    # On the (max_retries)-th attempt the daemon moves the task to Frozen.
    sdk.on(
        "## Task\nTB-5",
        _mutate_then_respond(
            [("TASKS.md", "tampered\n")],
            commit=False,
            project_root=cfg.project_root,
        ),
    )

    # Tick repeatedly. After each Backlog-bounce a fresh tick promotes from
    # Backlog → Ready (backlog_auto_promoted) and re-dispatches.
    for _ in range(5):
        asyncio.run(_tick(cfg, sdk, mcp_server=None))
        board = Board.load(cfg.tasks_file)
        loc = board.find("TB-5")
        if loc and loc[0] == "Frozen":
            break

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-5")[0] == "Frozen"
    evts = events.tail(cfg.events_file, 100)
    exhausts = [e for e in evts if e["type"] == "retry_exhausted"]
    assert exhausts, "expected retry_exhausted on the final violation"
    assert exhausts[-1]["task"] == "TB-5"
    assert exhausts[-1]["last_status"] == "state_violation"


# ---------------------------------------------------------------------------
# 7. Bonus: violation event is in FAILURE_EVENT_TYPES (TB-110 wiring).

def test_state_violation_in_failure_event_types():
    from ap2.diagnose import FAILURE_EVENT_TYPES
    assert "task_state_violation" in FAILURE_EVENT_TYPES
