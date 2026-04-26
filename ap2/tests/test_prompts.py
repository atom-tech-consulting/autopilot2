"""Smoke tests for ap2.prompts: the load-bearing parts of the task prompt
must not silently drift. Each phrase pinned here corresponds to a daemon-side
invariant — change them only when the corresponding daemon code also changes.
"""
from __future__ import annotations

from pathlib import Path

from ap2.board import Task
from ap2.config import Config
from ap2.prompts import build_mattermost_prompt, build_task_prompt


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


def test_mattermost_prompt_pins_explicit_thread_id(tmp_path):
    """The handler agent must reply in the user's thread, not in some thread_id
    it picks up from the recent-events block (which often contains an unrelated
    cron status-report thread). The fix wires the literal thread_id into the
    prompt as the value to pass to `mattermost_reply`.
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "stoch",
        "user": "li.zhang",
        "text": "@claude-bot status?",
        "thread_id": "",  # top-level message
    }
    p = build_mattermost_prompt(cfg, msg)
    assert 'channel: "ch-abc"' in p
    assert 'thread_id: ""' in p
    assert "do NOT pull" in p

    # Threaded reply: the handler should use the thread root.
    msg_threaded = dict(msg, thread_id="root-xyz")
    p2 = build_mattermost_prompt(cfg, msg_threaded)
    assert 'thread_id: "root-xyz"' in p2
