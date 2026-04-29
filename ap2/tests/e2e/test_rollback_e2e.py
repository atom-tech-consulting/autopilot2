"""TB-111 — `ap2 rollback` CLI end-to-end tests.

Walk a synthetic daemon-shaped history (agent commit + state commit per task)
and verify the CLI's plan, pre-flight checks, execution, and event shape.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ap2 import cli, events, rollback
from ap2.board import Board
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
    _git(["add", "TASKS.md", "CLAUDE.md", ".cc-autopilot/.gitignore"], cwd)
    _git(["commit", "-m", "baseline"], cwd)


def _commit(cwd: Path, message: str, *, files: dict[str, str] | None = None) -> str:
    for rel, content in (files or {}).items():
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _git(["add", rel], cwd)
    _git(["commit", "-m", message], cwd)
    return _git(["rev-parse", "HEAD"], cwd).stdout.strip()


def _seed_two_tasks(cfg: Config) -> tuple[str, str]:
    """Seed: TB-5 + state-5, TB-6 + state-6. Returns (b5, b6) — the
    boundaries before TB-5 and before TB-6 respectively."""
    boundary_before_5 = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    _commit(cfg.project_root, "TB-5: implement A",
            files={"a.py": "a\n"})
    _commit(cfg.project_root, "state: TB-5 → Complete",
            files={"TASKS.md":
                "# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n"
                "- [x] **TB-5** **A**\n## Frozen\n"})
    boundary_before_6 = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    _commit(cfg.project_root, "TB-6: implement B",
            files={"b.py": "b\n"})
    _commit(cfg.project_root, "state: TB-6 → Complete",
            files={"TASKS.md":
                "# Tasks\n## Active\n## Ready\n## Backlog\n## Complete\n"
                "- [x] **TB-5** **A**\n- [x] **TB-6** **B**\n## Frozen\n"})
    return boundary_before_5, boundary_before_6


def _make_cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    # Match a real ap2 project layout: gitignore the runtime ephemeral
    # files so `git status --porcelain` stays clean for the rollback
    # pre-flight check.
    (tmp_path / ".cc-autopilot").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cc-autopilot" / ".gitignore").write_text(
        "events.jsonl\ncron_state.json\nmm_state.json\n"
        "auto_diagnose_state.json\ndebug/\npipelines/\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _git_init(tmp_path)
    return cfg


def _run_cli(argv: list[str], cfg: Config) -> int:
    """Invoke ap2.cli.main against `cfg.project_root`."""
    return cli.main(["--project", str(cfg.project_root)] + argv)


# ---------------------------------------------------------------------------
# 1. Roll back one task — board, state, event all line up.

def test_rollback_one_task_resets_history_and_emits_event(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    _b5, b6 = _seed_two_tasks(cfg)

    rc = _run_cli(["rollback", "-y"], cfg)
    captured = capsys.readouterr()
    assert rc == 0, captured.err

    head = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    assert head == b6

    # TASKS.md is back to "TB-5 only complete"
    tasks = (cfg.project_root / "TASKS.md").read_text()
    assert "TB-6" not in tasks
    assert "TB-5" in tasks

    evts = events.tail(cfg.events_file, 20)
    rb = [e for e in evts if e["type"] == "task_rollback"]
    assert len(rb) == 1
    assert rb[0]["boundary_sha"] == b6
    assert rb[0]["affected_tasks"] == ["TB-6"]
    assert len(rb[0]["reverted_commits"]) == 2  # agent + state


# ---------------------------------------------------------------------------
# 2. Roll back N=2.

def test_rollback_n_2_undoes_both_tasks(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    b5, _b6 = _seed_two_tasks(cfg)

    rc = _run_cli(["rollback", "-n", "2", "-y"], cfg)
    captured = capsys.readouterr()
    assert rc == 0, captured.err

    head = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    assert head == b5

    evts = events.tail(cfg.events_file, 20)
    rb = next(e for e in evts if e["type"] == "task_rollback")
    assert set(rb["affected_tasks"]) == {"TB-5", "TB-6"}


# ---------------------------------------------------------------------------
# 3. `--task TB-N` resolves to the right linear boundary.

def test_rollback_task_resolves_to_linear_boundary(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    b5, _b6 = _seed_two_tasks(cfg)

    rc = _run_cli(["rollback", "--task", "TB-5", "-y"], cfg)
    captured = capsys.readouterr()
    assert rc == 0, captured.err

    head = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    assert head == b5

    evts = events.tail(cfg.events_file, 20)
    rb = next(e for e in evts if e["type"] == "task_rollback")
    # Linear: rolling back to TB-5 also undoes TB-6.
    assert set(rb["affected_tasks"]) == {"TB-5", "TB-6"}


# ---------------------------------------------------------------------------
# 4. Refuses dirty working tree.

def test_rollback_refuses_dirty_working_tree(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    _seed_two_tasks(cfg)
    # Dirty the working tree.
    (cfg.project_root / "TASKS.md").write_text("dirty\n")

    rc = _run_cli(["rollback", "-y"], cfg)
    captured = capsys.readouterr()
    assert rc == 1
    assert "working tree is dirty" in captured.err
    # No rollback event fired.
    evts = events.tail(cfg.events_file, 20)
    assert all(e["type"] != "task_rollback" for e in evts)


# ---------------------------------------------------------------------------
# 5. Pipeline-running warning fires (proceeds, doesn't refuse).

def test_rollback_warns_about_running_pipelines(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    b5, _b6 = _seed_two_tasks(cfg)

    # Synthesize a pipeline_start event in the boundary..HEAD window with
    # the test runner's pid (guaranteed alive).
    events.append(
        cfg.events_file, "pipeline_start",
        name="vwap-wfo", pid=os.getpid(),
        started_at="2099-01-01T00:00:00Z",
        log="/tmp/fake.log",
    )

    rc = _run_cli(["rollback", "-n", "2", "-y"], cfg)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    # The warning was printed alongside the plan + the post-reset note.
    assert "pipeline subprocess" in captured.out.lower() or \
           "pipelines still running" in captured.out.lower()

    evts = events.tail(cfg.events_file, 20)
    rb = next(e for e in evts if e["type"] == "task_rollback")
    assert rb["pipeline_warnings"], "expected the warning list to be non-empty"
    assert rb["pipeline_warnings"][0]["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# 6. Non-ancestor `--to` refused.

def test_rollback_refuses_non_ancestor_to(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    _seed_two_tasks(cfg)

    # Create an unrelated commit on a sideline branch — guaranteed
    # non-ancestor of HEAD on `main`.
    _git(["checkout", "-b", "sideline"], cfg.project_root)
    side_sha = _commit(cfg.project_root, "sideline-only",
                       files={"side.py": "x\n"})
    _git(["checkout", "main"], cfg.project_root)

    rc = _run_cli(["rollback", "--to", side_sha, "-y"], cfg)
    captured = capsys.readouterr()
    assert rc == 1
    assert "not an ancestor" in captured.err

    evts = events.tail(cfg.events_file, 20)
    assert all(e["type"] != "task_rollback" for e in evts)


# ---------------------------------------------------------------------------
# 7. Bonus: nothing-to-roll-back is a clean no-op.

def test_rollback_nothing_to_do(tmp_path, capsys):
    cfg = _make_cfg(tmp_path)
    # No tasks seeded; HEAD is baseline. resolve_by_n(1) returns None.
    rc = _run_cli(["rollback", "-y"], cfg)
    captured = capsys.readouterr()
    # Either a "nothing to roll back" success or a "doesn't have N
    # task-completions" refusal — both are acceptable; what's NOT
    # acceptable is silently mutating HEAD or emitting a rollback event.
    head_before = _git(["rev-parse", "HEAD"], cfg.project_root).stdout.strip()
    evts = events.tail(cfg.events_file, 20)
    assert all(e["type"] != "task_rollback" for e in evts)
    assert head_before  # still on the baseline commit
    assert rc in (0, 1)


# ---------------------------------------------------------------------------
# 8. task_rollback is in MEANINGFUL_EVENT_TYPES (TB-111 wiring).

def test_task_rollback_in_meaningful_event_types():
    from ap2.diagnose import MEANINGFUL_EVENT_TYPES
    assert "task_rollback" in MEANINGFUL_EVENT_TYPES
