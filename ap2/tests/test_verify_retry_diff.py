"""Unit tests for TB-127: per-task verifier picks the task's commit, not HEAD.

On retry of a task whose first attempt already committed an implementation,
HEAD is a daemon `state:` bookkeeping commit (TASKS.md +
retry_state.json + briefing Attempts) — not the agent's source-code commit.
`git show HEAD` against that gives the prose-bullet judge only board moves
and lets it hallucinate "no new tests added" / "diff contains no changes
to file X". `_find_task_commit` walks the log for a commit subject prefixed
with `<task_id>:` so the judge sees the real implementation diff regardless
of whether the agent's commit is at HEAD or buried under daemon state
commits.

These tests pin the helpers directly (no SDK). The e2e wiring through
`verify_task` is exercised in `tests/e2e/test_verify_per_task.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ap2.verify import _find_task_commit, _git_show_for_task, _git_show_head


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


def test_find_task_commit_returns_none_outside_git_repo(tmp_path: Path):
    """Skip path: no `.git` dir → None (mirrors _git_show_head's contract)."""
    assert _find_task_commit(tmp_path, "TB-5") is None


def test_find_task_commit_returns_none_when_no_matching_subject(tmp_path: Path):
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-m", "unrelated: chore"], tmp_path)

    assert _find_task_commit(tmp_path, "TB-5") is None


def test_find_task_commit_finds_head_commit_when_subject_matches(tmp_path: Path):
    """Happy path: agent just committed, HEAD's subject names the task."""
    _git_init(tmp_path)
    (tmp_path / "foo.py").write_text("def foo(): pass\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: add foo"], tmp_path)

    head_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    found = _find_task_commit(tmp_path, "TB-5")
    assert found == head_sha


def test_find_task_commit_walks_past_state_commits_to_implementation(tmp_path: Path):
    """The retry case (TB-127): TB-5's implementation commit is N commits
    back; HEAD is a `state:` bookkeeping commit. The walker must skip the
    state commits and return the implementation SHA so the prose judge
    sees the real diff.
    """
    _git_init(tmp_path)

    # Implementation commit (first attempt).
    (tmp_path / "foo.py").write_text("def foo(): return 42\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement foo with tests"], tmp_path)
    impl_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    # Several daemon state commits on top — these are what HEAD currently
    # points at when a retry runs.
    for _ in range(3):
        (tmp_path / "TASKS.md").write_text("# Tasks\n")
        _git(["add", "TASKS.md"], tmp_path)
        _git(
            ["commit", "--allow-empty", "-m", "state: TB-5 → Backlog"],
            tmp_path,
        )

    head_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert head_sha != impl_sha  # paranoia

    found = _find_task_commit(tmp_path, "TB-5")
    assert found == impl_sha


def test_find_task_commit_strict_token_match_avoids_substring_collision(
    tmp_path: Path,
):
    """`TB-12` must not match a subject for `TB-127: ...` — the matcher
    splits on `[:\\s]` and compares the first token exactly. Mirrors the
    same convention used by `daemon._infer_result_from_head` (TB-74).
    """
    _git_init(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n")
    _git(["add", "x.py"], tmp_path)
    _git(["commit", "-m", "TB-127: bigger task that mentions TB-12"], tmp_path)

    assert _find_task_commit(tmp_path, "TB-12") is None
    assert _find_task_commit(tmp_path, "TB-127") is not None


def test_find_task_commit_picks_most_recent_when_multiple_match(tmp_path: Path):
    """If a task somehow committed twice (extension commit on retry), the
    most recent commit is returned — that's the diff the judge wants."""
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("a = 1\n")
    _git(["add", "a.py"], tmp_path)
    _git(["commit", "-m", "TB-5: first attempt"], tmp_path)

    (tmp_path / "b.py").write_text("b = 2\n")
    _git(["add", "b.py"], tmp_path)
    _git(["commit", "-m", "TB-5: gap-fill on retry"], tmp_path)
    second_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    found = _find_task_commit(tmp_path, "TB-5")
    assert found == second_sha


def test_git_show_for_task_returns_implementation_diff_under_state_commits(
    tmp_path: Path,
):
    """Integration of the two helpers: when the implementation is buried
    under state commits, `_git_show_for_task` returns the implementation
    diff (containing `foo.py`), not the bookkeeping diff (containing only
    TASKS.md). This is the exact substitution that fixes TB-127.
    """
    _git_init(tmp_path)
    (tmp_path / "foo.py").write_text("def foo(): return 42\n")
    _git(["add", "foo.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement foo"], tmp_path)

    (tmp_path / "TASKS.md").write_text("# Tasks\n")
    _git(["add", "TASKS.md"], tmp_path)
    _git(["commit", "-m", "state: TB-5 → Backlog"], tmp_path)

    # Sanity: HEAD's diff is just TASKS.md.
    head_diff = _git_show_head(tmp_path)
    assert "TASKS.md" in head_diff
    assert "foo.py" not in head_diff

    # Task-resolving diff: contains the implementation file.
    task_diff = _git_show_for_task(tmp_path, "TB-5")
    assert "foo.py" in task_diff
    assert "def foo()" in task_diff


def test_git_show_for_task_falls_back_to_head_when_no_match(tmp_path: Path):
    """When no commit subject matches the task id, fall back to HEAD —
    preserves legacy behavior for tasks whose first attempt never
    committed (the prose judge will then see whatever HEAD has).
    """
    _git_init(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n")
    _git(["add", "x.py"], tmp_path)
    _git(["commit", "-m", "unrelated commit"], tmp_path)

    head_diff = _git_show_head(tmp_path)
    task_diff = _git_show_for_task(tmp_path, "TB-99")
    assert task_diff == head_diff


def test_git_show_for_task_uses_head_when_task_id_is_none(tmp_path: Path):
    """Backward-compat: callers that don't pass `task_id` keep getting HEAD."""
    _git_init(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n")
    _git(["add", "x.py"], tmp_path)
    _git(["commit", "-m", "TB-5: implement x"], tmp_path)

    assert _git_show_for_task(tmp_path, None) == _git_show_head(tmp_path)
