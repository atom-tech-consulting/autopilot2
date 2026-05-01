"""Smoke tests for ap2.prompts: the load-bearing parts of the task prompt
must not silently drift. Each phrase pinned here corresponds to a daemon-side
invariant — change them only when the corresponding daemon code also changes.
"""
from __future__ import annotations

from pathlib import Path

from ap2.board import Task
from ap2.config import Config
from ap2.prompts import (
    build_control_prompt,
    build_mattermost_prompt,
    build_task_prompt,
)


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


def test_prompt_advertises_cron_propose_for_recurring_proposals(tmp_path):
    """TB-123: the `cron=` arg was lifted off `report_result` and into a
    dedicated `cron_propose` MCP tool. The prompt footer must surface the
    new tool by name (so the agent can discover it) and must NOT
    instruct the agent to pass cron via `report_result`'s args (which
    would silently fail since the field is gone from the schema).
    """
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    # New tool surfaced by name with all four arg fields.
    assert "cron_propose" in p
    assert "rationale" in p
    assert "schedule" in p
    # Drop-pin: the obsolete `cron=` arg phrasing in `report_result` is
    # gone — the JSON-list-in-string contract no longer exists.
    assert "cron='[" not in p
    assert '"action": "add"' not in p


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


def test_mattermost_prompt_restriction_note_mentions_concurrent_task(tmp_path):
    """TB-122 + TB-142: when a task is in flight, the prompt explicitly
    explains that cron_edit, ideation_state_write, and (TB-142) board_edit
    are off-limits, and tells the agent to route board mutations through
    `operator_queue_append` instead. The same prompt must spell out the
    operator-still-available actions (queue add/approve/delete/backlog/
    unfreeze, daemon_control, operator_log_append) so the handler doesn't
    refuse work it CAN do."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot pause",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg, task_in_flight=True)
    # Pinned: agent knows why the toolset is narrower.
    assert "task agent is currently running" in p
    assert "cron_edit" in p
    assert "ideation_state_write" in p
    # Pinned (TB-142): board_edit is named as off-limits, and the
    # queue-routing equivalent is named so the handler can still mutate.
    assert "board_edit" in p
    assert "operator_queue_append" in p
    # Pinned: agent knows pause takes effect on the next tick.
    assert "next" in p.lower() and "tick" in p.lower()
    # Pinned: TB-121 cross-ref — `approve` must remain discoverable from
    # the restricted prompt (it's now a queue op, not a board_edit action).
    assert "approve" in p
    # Pinned: operator_log_append remains available so "ack:" still works.
    assert "operator_log_append" in p

    # Idle prompt does NOT contain the concurrent-task header.
    p_idle = build_mattermost_prompt(cfg, msg)
    assert "task agent is currently running" not in p_idle


def test_mattermost_prompt_restricted_routes_board_ops_through_queue(tmp_path):
    """TB-142 (load-bearing): the restricted-mode "Your job" rubric must
    direct the agent at `operator_queue_append` for board mutations and
    explicitly steer it AWAY from `board_edit` (which is filtered out of
    MM_HANDLER_TOOLS_RESTRICTED). Pin both the routing instruction and
    the rationale (drain happens between tick stages, so the running
    task's snapshot window never sees the mutation).
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot approve TB-9",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg, task_in_flight=True)
    # The "Your job" rubric routes board mutations through the queue.
    assert "operator_queue_append" in p
    # Explicit "NOT board_edit" guidance (so the agent doesn't fall back
    # to `board_edit` if it remembers seeing it elsewhere).
    assert "NOT `board_edit`" in p or "not `board_edit`" in p.lower()
    # The TB-142 rationale ties the routing to the in-flight snapshot
    # window — agents who understand WHY are less likely to drift.
    assert "snapshot" in p.lower() or "TB-110" in p

    # Idle prompt routes through `board_edit` directly (FULL toolset still
    # has it; queue-routing is only required when restricted).
    p_idle = build_mattermost_prompt(cfg, msg)
    assert "board_edit" in p_idle
    assert "approve" in p_idle


# ---------------------------------------------------------------------------
# TB-128: control prompts must inject a fresh "right now" snapshot, and the
# status-report job must get an explicit timestamp / freshness contract.

def test_control_prompt_injects_current_state_block(tmp_path):
    """Every control-agent prompt (cron + ideation) must carry a
    `## Current state` block with a freshly computed UTC `now:` timestamp,
    board counts, and recent commits. This is the deterministic anchor the
    status-report cron uses for its headline (TB-128) — no more re-rendering
    text from a prior context.
    """
    cfg = _cfg(tmp_path)
    p = build_control_prompt(cfg, "status-report", "post a status report")
    assert "## Current state" in p
    assert "rendered just before this prompt was sent" in p
    # Headline timestamp is a real ISO-Z string formatted right now.
    import re

    assert re.search(
        r"now: 20\d\d-[01]\d-[0-3]\dT[0-2]\d:[0-5]\d:[0-5]\dZ", p
    ), "expected a current ISO-Z `now:` timestamp in the snapshot block"
    # Board counts use the same A/R/B/P/C/F shape as `ap2 status`.
    assert "(Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)" in p


def test_control_prompt_status_report_pins_freshness_contract(tmp_path):
    """For the `status-report` job specifically, the prompt must spell out
    the load-bearing rules so the agent can't drift back to copying a stale
    timestamp from prior turns: (1) headline timestamp = the snapshot's
    `now:` value verbatim, (2) re-read events.jsonl + TASKS.md fresh,
    (3) skip the post if nothing has changed since the last status_report.
    """
    cfg = _cfg(tmp_path)
    p = build_control_prompt(cfg, "status-report", "post a status report")
    # (1) Use the snapshot timestamp verbatim — no copying from elsewhere.
    assert "Status-report contract" in p
    assert "literal `now:` value" in p
    assert "Do NOT reuse a timestamp from" in p
    # (2) Fresh reads of the canonical state files.
    assert ".cc-autopilot/events.jsonl" in p
    assert "TASKS.md" in p
    # (3) Skip-when-idle directive (defense in depth — daemon also gates).
    assert "Skip the Mattermost post entirely" in p
    assert "no activity since" in p


def test_control_prompt_non_status_jobs_skip_status_report_contract(tmp_path):
    """The status-report contract is keyed on job name. Other control jobs
    (e.g. ideation) must not get the status-report-specific addendum
    appended to their prompt — it's noise for them and would confuse
    ideation's own freshness model.
    """
    cfg = _cfg(tmp_path)
    p_status = build_control_prompt(cfg, "status-report", "x")
    p_other = build_control_prompt(cfg, "ideation", "x")
    assert "Status-report contract" in p_status
    assert "Status-report contract" not in p_other
    # The shared `## Current state` block IS in both — it's harmless context.
    assert "## Current state" in p_other
