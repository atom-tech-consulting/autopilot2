"""Unit tests for ap2.rollback (TB-110 + TB-111 shared helpers)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ap2 import events, rollback
from ap2.config import Config


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


def _make_cfg(tmp_path: Path) -> Config:
    """Spin up a minimal ap2 project layout under tmp_path with git."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _git_init(tmp_path)
    _git(["add", "TASKS.md", "CLAUDE.md"], tmp_path)
    _git(["commit", "-m", "baseline"], tmp_path)
    return cfg


def _commit_state(cwd: Path, message: str, tasks_md: str) -> str:
    """Write TASKS.md, commit, return SHA."""
    (cwd / "TASKS.md").write_text(tasks_md)
    _git(["add", "TASKS.md"], cwd)
    _git(["commit", "-m", message], cwd)
    return _git(["rev-parse", "HEAD"], cwd).stdout.strip()


# ---------------------------------------------------------------------------
# git_head / is_ancestor

def test_git_head_returns_sha(tmp_path):
    cfg = _make_cfg(tmp_path)
    sha = rollback.git_head(cfg)
    assert sha is not None and len(sha) == 40


def test_git_head_returns_none_for_non_git_project(tmp_path, monkeypatch):
    (tmp_path / "TASKS.md").write_text("# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n## Frozen\n")
    (tmp_path / "CLAUDE.md").write_text("## Autopilot\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n")
    cfg = Config.load(tmp_path)
    assert rollback.git_head(cfg) is None


def test_is_ancestor_true_for_self_and_parent(tmp_path):
    cfg = _make_cfg(tmp_path)
    parent = rollback.git_head(cfg)
    _commit_state(tmp_path, "TB-1: do work", "# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n## Frozen\nx\n")
    head = rollback.git_head(cfg)
    assert rollback.is_ancestor(cfg, parent)
    assert rollback.is_ancestor(cfg, head)


def test_is_ancestor_false_for_unrelated_sha(tmp_path):
    cfg = _make_cfg(tmp_path)
    # An obviously-bogus sha — git rejects with non-zero rc, helper returns False.
    assert rollback.is_ancestor(cfg, "0" * 40) is False


# ---------------------------------------------------------------------------
# snapshot_fenced_files / detect_fenced_violations

def test_snapshot_includes_existing_fenced_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    snap = rollback.snapshot_fenced_files(cfg)
    assert "TASKS.md" in snap
    assert "CLAUDE.md" in snap
    # `goal.md` and other optional fenced files don't exist → omitted.
    assert "goal.md" not in snap


def test_snapshot_omits_events_jsonl_even_if_present(tmp_path):
    """events.jsonl is gitignored and out-of-scope per TB-110 briefing."""
    cfg = _make_cfg(tmp_path)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text('{"ts":"2026-01-01T00:00:00Z","type":"x"}\n')
    snap = rollback.snapshot_fenced_files(cfg)
    assert ".cc-autopilot/events.jsonl" not in snap


def test_violation_check_excludes_operator_queue_jsonl(tmp_path):
    """TB-143: `operator_queue.jsonl` is in `TASK_AGENT_FENCED_PATHS` (so the
    prompt fence + SDK Edit/Write rejects still apply) but explicitly
    excluded from the post-hoc snapshot check, alongside `events.jsonl`.
    Pre-TB-141 the path was in the violation-check set, which made any
    operator `ap2 add` issued during a task run roll back legitimate work
    (TB-139). TB-141 dropped the path from the fence entirely; TB-143
    restores defense-in-depth by re-fencing while keeping the
    snapshot-check exemption.
    """
    assert ".cc-autopilot/operator_queue.jsonl" not in rollback.FENCED_PATHS_FOR_VIOLATION_CHECK
    # Sanity: the events.jsonl exemption survives the generalization.
    assert ".cc-autopilot/events.jsonl" not in rollback.FENCED_PATHS_FOR_VIOLATION_CHECK
    # Both exempt paths still live in the upstream fence list — defense
    # layers (prompt header + SDK reject) apply even though the
    # snapshot-check is exempt.
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    assert ".cc-autopilot/operator_queue.jsonl" in TASK_AGENT_FENCED_PATHS
    assert ".cc-autopilot/events.jsonl" in TASK_AGENT_FENCED_PATHS


def test_detect_violations_ignores_operator_queue_append_between_snapshots(tmp_path):
    """TB-143 (regression — TB-139 scenario): an operator's `ap2 add`
    issued mid-run appends a record to `operator_queue.jsonl`. The
    daemon's pre-run snapshot was taken before that append; the post-run
    snapshot sees the new record. The post-hoc violation check must NOT
    flag this as an agent fence violation, because the daemon / operator
    legitimately writes here while a task is in flight.
    """
    from ap2 import tools

    cfg = _make_cfg(tmp_path)
    pre = rollback.snapshot_fenced_files(cfg)
    # Operator types `ap2 add` mid-run — the synchronous queue append.
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "queued mid-run",
            "briefing": (
                "# brief\n\n"
                "## Goal\n\nstub\n\n"
                "## Scope\n\n- foo.py\n\n"
                "## Design\n\nedit foo\n\n"
                "## Verification\n"
                "- `uv run pytest -q` — gates pass\n\n"
                "## Out of scope\n\n- nothing\n"
            ),
        },
    )
    assert not res.get("isError")
    # Post-snapshot diff: the queue path was modified, but it's exempt.
    assert rollback.detect_fenced_violations(cfg, pre) == []


def test_detect_violations_clean_run(tmp_path):
    cfg = _make_cfg(tmp_path)
    pre = rollback.snapshot_fenced_files(cfg)
    # No mutation between snapshots → no violations.
    assert rollback.detect_fenced_violations(cfg, pre) == []


def test_detect_violations_picks_up_modification(tmp_path):
    cfg = _make_cfg(tmp_path)
    pre = rollback.snapshot_fenced_files(cfg)
    (tmp_path / "TASKS.md").write_text("tampered\n")
    assert rollback.detect_fenced_violations(cfg, pre) == ["TASKS.md"]


def test_detect_violations_picks_up_creation(tmp_path):
    cfg = _make_cfg(tmp_path)
    pre = rollback.snapshot_fenced_files(cfg)
    # goal.md didn't exist at snapshot time. Agent creates it → violation.
    (tmp_path / "goal.md").write_text("# project mission\n")
    assert rollback.detect_fenced_violations(cfg, pre) == ["goal.md"]


def test_detect_violations_picks_up_deletion(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "goal.md").write_text("# project mission\n")
    pre = rollback.snapshot_fenced_files(cfg)
    (tmp_path / "goal.md").unlink()
    assert rollback.detect_fenced_violations(cfg, pre) == ["goal.md"]


def test_detect_violations_lists_multiple_files_sorted(tmp_path):
    cfg = _make_cfg(tmp_path)
    pre = rollback.snapshot_fenced_files(cfg)
    (tmp_path / "TASKS.md").write_text("a\n")
    (tmp_path / "CLAUDE.md").write_text("b\n")
    assert rollback.detect_fenced_violations(cfg, pre) == ["CLAUDE.md", "TASKS.md"]


# ---------------------------------------------------------------------------
# linear_rollback_to

def test_linear_rollback_resets_to_boundary(tmp_path):
    cfg = _make_cfg(tmp_path)
    boundary = rollback.git_head(cfg)
    _commit_state(tmp_path, "TB-5: agent commit", "agent change\n")
    state_sha = _commit_state(
        tmp_path, "state: TB-5 → Complete",
        "# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n- TB-5\n## Frozen\n",
    )
    assert state_sha != boundary
    rollback.linear_rollback_to(cfg, boundary)
    assert rollback.git_head(cfg) == boundary
    # Working tree clean post-reset.
    status = _git(["status", "--porcelain"], tmp_path).stdout.strip()
    assert status == ""


def test_linear_rollback_refuses_non_ancestor(tmp_path):
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ValueError):
        rollback.linear_rollback_to(cfg, "0" * 40)


def test_linear_rollback_no_op_for_non_git_project(tmp_path):
    (tmp_path / "TASKS.md").write_text("# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n## Frozen\n")
    (tmp_path / "CLAUDE.md").write_text("## Autopilot\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n")
    cfg = Config.load(tmp_path)
    # Doesn't raise even though there's nothing to do.
    rollback.linear_rollback_to(cfg, "irrelevant")


# ---------------------------------------------------------------------------
# Boundary resolvers

def _seed_two_task_completions(cfg: Config) -> tuple[str, str, str]:
    """Build a history that looks like a real ap2 project after two
    completions. Returns (boundary_before_TB5, boundary_before_TB6, head)."""
    root = cfg.project_root
    boundary_before_5 = rollback.git_head(cfg)
    _commit_state(root, "TB-5: implement A", "a\n")
    _commit_state(
        root, "state: TB-5 → Complete",
        "# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n- TB-5\n## Frozen\n",
    )
    boundary_before_6 = rollback.git_head(cfg)
    _commit_state(root, "TB-6: implement B", "b\n")
    _commit_state(
        root, "state: TB-6 → Complete",
        "# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n- TB-5\n- TB-6\n## Frozen\n",
    )
    head = rollback.git_head(cfg)
    return boundary_before_5, boundary_before_6, head


def test_resolve_by_n_one(tmp_path):
    cfg = _make_cfg(tmp_path)
    _b5, b6, _head = _seed_two_task_completions(cfg)
    assert rollback.resolve_boundary_by_n(cfg, 1) == b6


def test_resolve_by_n_two(tmp_path):
    cfg = _make_cfg(tmp_path)
    b5, _b6, _head = _seed_two_task_completions(cfg)
    assert rollback.resolve_boundary_by_n(cfg, 2) == b5


def test_resolve_by_n_returns_none_when_too_few(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_two_task_completions(cfg)
    assert rollback.resolve_boundary_by_n(cfg, 99) is None


def test_resolve_by_n_returns_none_for_zero(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_two_task_completions(cfg)
    assert rollback.resolve_boundary_by_n(cfg, 0) is None


def test_resolve_by_task_finds_oldest_match(tmp_path):
    cfg = _make_cfg(tmp_path)
    b5, _b6, _head = _seed_two_task_completions(cfg)
    # Linear rule: rolling back to TB-5 ALSO undoes TB-6.
    assert rollback.resolve_boundary_by_task(cfg, "TB-5") == b5


def test_resolve_by_task_recent_only(tmp_path):
    cfg = _make_cfg(tmp_path)
    _b5, b6, _head = _seed_two_task_completions(cfg)
    # TB-6 is the most recent shipment; boundary == before TB-6.
    assert rollback.resolve_boundary_by_task(cfg, "TB-6") == b6


def test_resolve_by_task_unknown_returns_none(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_two_task_completions(cfg)
    assert rollback.resolve_boundary_by_task(cfg, "TB-99") is None


# ---------------------------------------------------------------------------
# list_affected_commits / affected_task_ids

def test_list_affected_commits_returns_range_newest_first(tmp_path):
    cfg = _make_cfg(tmp_path)
    b5, _b6, _head = _seed_two_task_completions(cfg)
    affected = rollback.list_affected_commits(cfg, b5)
    subjects = [s for _sha, s in affected]
    # 4 commits between b5 and HEAD: TB-5: agent, state TB-5, TB-6: agent, state TB-6.
    assert len(subjects) == 4
    assert subjects[0].startswith("state: TB-6")
    assert subjects[-1].startswith("TB-5:")


def test_affected_task_ids_dedup_preserves_order(tmp_path):
    cfg = _make_cfg(tmp_path)
    b5, _b6, _head = _seed_two_task_completions(cfg)
    affected = rollback.list_affected_commits(cfg, b5)
    ids = rollback.affected_task_ids(affected)
    # Newest-first walk → TB-6 appears before TB-5.
    assert ids == ["TB-6", "TB-5"]


# ---------------------------------------------------------------------------
# list_alive_pipelines_in_range

def test_list_alive_pipelines_picks_up_live_pid(tmp_path):
    cfg = _make_cfg(tmp_path)
    boundary = rollback.git_head(cfg)
    # Make a commit so boundary..HEAD is non-empty (events live in window).
    _commit_state(tmp_path, "TB-5: work", "x\n")

    # Append a synthetic pipeline_start event for the test runner's own pid
    # — guaranteed alive while the test executes.
    events.append(
        cfg.events_file, "pipeline_start",
        name="vwap-wfo", pid=os.getpid(),
        started_at="2099-01-01T00:00:00Z",
        log="/tmp/fake.log",
    )

    alive = rollback.list_alive_pipelines_in_range(cfg, boundary)
    assert len(alive) == 1
    assert alive[0]["pid"] == os.getpid()
    assert alive[0]["name"] == "vwap-wfo"


def test_list_alive_pipelines_skips_dead_pid(tmp_path):
    cfg = _make_cfg(tmp_path)
    boundary = rollback.git_head(cfg)
    _commit_state(tmp_path, "TB-5: work", "x\n")
    # PID 1 (init / launchd) is owned by root — `os.kill(1, 0)` raises
    # PermissionError for unprivileged users, which the helper treats as
    # "dead/unreachable" and skips. PID we deliberately know is gone:
    # spawn a true subprocess, wait for it, then use its PID.
    p = subprocess.Popen(["true"])
    p.wait()
    dead_pid = p.pid

    events.append(
        cfg.events_file, "pipeline_start",
        name="dead-one", pid=dead_pid,
        started_at="2099-01-01T00:00:00Z",
        log="/tmp/fake.log",
    )
    alive = rollback.list_alive_pipelines_in_range(cfg, boundary)
    assert alive == []


def test_list_alive_pipelines_skips_events_before_boundary(tmp_path):
    cfg = _make_cfg(tmp_path)
    # Pre-boundary event.
    events.append(
        cfg.events_file, "pipeline_start",
        name="pre", pid=os.getpid(),
        started_at="2000-01-01T00:00:00Z",
        log="/tmp/fake.log",
    )
    # Bump boundary AFTER that event so it falls before the commit ts.
    import time as _time
    _time.sleep(1.1)
    _commit_state(tmp_path, "TB-5: post-event commit", "y\n")
    boundary = rollback.git_head(cfg)

    alive = rollback.list_alive_pipelines_in_range(cfg, boundary)
    assert alive == []
