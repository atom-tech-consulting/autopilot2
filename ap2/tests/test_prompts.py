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


def test_prompt_pins_pipeline_task_start_guidance(tmp_path):
    """TB-114: task agent header must steer agents to `pipeline_task_start`
    for any work expected to take more than ~5 min wall-clock —
    independent of whether the briefing has a `## Pipeline launch` section
    (that two-shape pattern was retired). The agent self-classifies; on
    pipeline pivot the daemon parks the task in `Pipeline Pending` and
    re-runs verification once subprocesses die.
    """
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    # Tool named explicitly — agents see the symbol they need to call.
    assert "pipeline_task_start" in p
    # Strong MUST phrasing — weaker words let agents rationalize past it.
    assert "MUST" in p
    # Self-classification trigger (cost-aware, not briefing-shape-aware).
    assert "5 minutes" in p or "~5 minutes" in p
    # The Pipeline Pending parking is the load-bearing post-dispatch fact.
    assert "Pipeline Pending" in p
    # Make sure the agent doesn't double-do the work inline + via pipeline.
    assert "Do NOT ALSO" in p or "Do NOT also" in p


def test_prompt_pins_state_file_fence(tmp_path):
    """Daemon (not agent) owns TASKS.md / progress.md / events.jsonl /
    CLAUDE.md / ideation_state.md / cron.yaml; operator owns goal.md.

    Every fenced path must appear in the prompt header. Permission-level
    enforcement (run_task's disallowed_tools) is the second line of defense;
    the prompt fence is the first, and a missing entry would silently let
    the agent edit a fenced file that the SDK guard *does* still block.
    """
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "do NOT touch" in p
    for f in TASK_AGENT_FENCED_PATHS:
        assert f in p, f"fenced path {f!r} missing from task prompt header"


def test_task_disallowed_tools_covers_every_fenced_path():
    """Every TASK_AGENT_FENCED_PATHS entry must produce both an `Edit(<path>)`
    and a `Write(<path>)` block in the disallowed_tools list — that's the
    SDK-level enforcement layer behind the prompt fence."""
    from ap2.daemon import _task_disallowed_tools
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    blocks = _task_disallowed_tools()
    # Always-on Bash blocks survive
    assert "Bash(git push*)" in blocks
    assert "Bash(rm -rf *)" in blocks
    # Every fenced path appears as both Edit and Write
    for path in TASK_AGENT_FENCED_PATHS:
        assert f"Edit({path})" in blocks, f"Edit({path}) missing from disallowed_tools"
        assert f"Write({path})" in blocks, f"Write({path}) missing from disallowed_tools"


def test_task_fenced_paths_includes_goal_md():
    """goal.md is operator-curated; if a task can rewrite it, ideation
    rewrites its own constraints (the TB-144 feedback-loop case in stoch)."""
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    assert "goal.md" in TASK_AGENT_FENCED_PATHS


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
