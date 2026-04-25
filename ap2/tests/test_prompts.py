"""Smoke tests for ap2.prompts: the load-bearing parts of the task prompt
must not silently drift. Each phrase pinned here corresponds to a daemon-side
invariant — change them only when the corresponding daemon code also changes.
"""
from __future__ import annotations

from pathlib import Path

from ap2.board import Task
from ap2.config import Config
from ap2.prompts import build_task_prompt


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text("# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n")
    return Config.load(tmp_path)


def test_prompt_pins_commit_subject_convention(tmp_path):
    """`<TASK_ID>:` prefix is what the daemon's commit-fallback (TB-65) parses."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "STARTS WITH the task ID" in p
    assert "load-bearing" in p


def test_prompt_pins_pre_run_history_check(tmp_path):
    """Retry agents must check `git log --grep` before redoing prior work."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "Before you start: check for prior work" in p
    assert 'git log --grep="<TASK_ID>"' in p


def test_prompt_warns_against_naive_complete_from_commit_existence(tmp_path):
    """Agent must verify completeness, not just trust subject — the daemon's
    fallback trusts naively, the agent should not."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "DO NOT declare status=complete based on commit existence alone" in p


def test_prompt_pins_state_file_fence(tmp_path):
    """Daemon (not agent) owns TASKS.md / progress.md / events.jsonl."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "What the daemon handles (do NOT touch)" in p
    for f in ("TASKS.md", "progress.md", "events.jsonl"):
        assert f in p
