"""TB-122 / TB-145: unit tests for the concurrent MM handler toolset.

Originally TB-122 introduced a FULL/RESTRICTED toggle keyed on whether a
task agent was in flight. TB-145 collapsed that into a single
unconditional `MM_HANDLER_TOOLS` set (the previous RESTRICTED shape),
because the snapshot check was a TOCTOU race against the daemon's main
tick loop. This file now covers:

  (a) `MM_HANDLER_TOOLS` drops `cron_edit`, `ideation_state_write`, and
      `board_edit` (the three tools that were the in-flight rationale).
  (b) `MM_HANDLER_TOOLS` keeps the operator-facing tools (queue,
      daemon_control, mattermost_reply, operator_log_append, log_event,
      reads).
  (c) `build_mattermost_prompt` includes the always-on restriction note
      explaining the fixed toolset.
  (d) `build_mattermost_prompt` does NOT mention conditional / state-
      dependent toolset switching (no "when a task is active" / "when
      the board is idle" language — TB-145 invariant).
  (e) `handle_message` passes `MM_HANDLER_TOOLS` to the SDK regardless
      of whether the board has Active tasks (no board snapshot at
      handler-spawn time — TB-145 race fix).
  (f) `AP2_MM_TICK_S` overrides the default `mm_tick_interval_s` (10s).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ap2.board import Board
from ap2.config import Config
from ap2.prompts import build_mattermost_prompt
from ap2.tools import (
    CONTROL_AGENT_TOOLS,
    MM_HANDLER_TOOLS,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    return Config.load(tmp_path)


def _add_active_task(cfg: Config, task_id: str = "TB-1", title: str = "In-flight") -> None:
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id=task_id, title=title)
    board.save()


# ── toolset constant shape ────────────────────────────────────────────────────


def test_mm_handler_tools_drops_cron_edit():
    """TB-145: `cron_edit` must NOT be in `MM_HANDLER_TOOLS` (would race
    the daemon's tick / cron-fire window)."""
    assert "mcp__autopilot__cron_edit" not in MM_HANDLER_TOOLS


def test_mm_handler_tools_drops_ideation_state_write():
    """TB-145: `ideation_state_write` must NOT be in `MM_HANDLER_TOOLS`
    (would rewrite the per-cycle assessment ideation was acting on)."""
    assert "mcp__autopilot__ideation_state_write" not in MM_HANDLER_TOOLS


def test_mm_handler_tools_drops_board_edit():
    """TB-142 / TB-145: direct `board_edit` is the second false-positive
    surface for TB-110's state-violation check. Operator chat commands
    that previously routed through `board_edit` (`@claude-bot freeze /
    approve / ...`) now go through `operator_queue_append`; the daemon
    drains queued ops between tick stages, so an in-flight task's
    snapshot window is never racing a direct TASKS.md mutation.
    `MM_HANDLER_TOOLS` must NOT contain `board_edit`."""
    assert "mcp__autopilot__board_edit" not in MM_HANDLER_TOOLS


def test_mm_handler_tools_keeps_daemon_control():
    """pause/resume mid-task is the primary use-case; daemon_control must stay."""
    assert "mcp__autopilot__daemon_control" in MM_HANDLER_TOOLS


def test_mm_handler_tools_keeps_mattermost_reply():
    """Handler must still be able to reply."""
    assert "mcp__autopilot__mattermost_reply" in MM_HANDLER_TOOLS


def test_mm_handler_tools_keeps_log_event():
    assert "mcp__autopilot__log_event" in MM_HANDLER_TOOLS


def test_mm_handler_tools_keeps_operator_log_append():
    """TB-122: operator_log_append is the operator's veto channel mid-task."""
    assert "mcp__autopilot__operator_log_append" in MM_HANDLER_TOOLS


def test_mm_handler_tools_keeps_read_glob_grep():
    """Read-only filesystem access must always be available."""
    for tool in ("Read", "Glob", "Grep"):
        assert tool in MM_HANDLER_TOOLS, f"{tool} missing from MM_HANDLER_TOOLS"


def test_mm_handler_tools_keeps_git_log_grep():
    assert "mcp__autopilot__git_log_grep" in MM_HANDLER_TOOLS


def test_mm_handler_tools_is_strict_subset_of_control_agent_tools():
    """MM_HANDLER_TOOLS ⊂ CONTROL_AGENT_TOOLS — nothing new is added,
    and post-TB-146 the only tools dropped from CONTROL_AGENT_TOOLS by
    the handler filter are `ideation_state_write` and `board_edit`.
    (`cron_edit` is filtered defensively but TB-146 already removed it
    from `CONTROL_AGENT_TOOLS`, so it doesn't show up in this diff.)"""
    full_set = set(CONTROL_AGENT_TOOLS)
    mm_set = set(MM_HANDLER_TOOLS)
    assert mm_set < full_set, "MM_HANDLER_TOOLS must be a strict subset of CONTROL_AGENT_TOOLS"
    assert full_set - mm_set == {
        "mcp__autopilot__ideation_state_write",
        "mcp__autopilot__board_edit",
    }
    # TB-146: `cron_edit` is absent from BOTH sets — operator-CLI-only.
    assert "mcp__autopilot__cron_edit" not in full_set
    assert "mcp__autopilot__cron_edit" not in mm_set


# ── build_mattermost_prompt ───────────────────────────────────────────────────


def _base_msg():
    return {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot status",
        "thread_id": "",
    }


def test_prompt_includes_unconditional_restriction_note(tmp_path):
    """TB-145: the prompt must always carry the restriction note (no
    longer gated on task_in_flight). The agent needs to know its
    toolset is narrowed and why."""
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg())
    assert "cron_edit" in p
    assert "ideation_state_write" in p
    assert "restricted toolset" in p or "Restricted toolset" in p


def test_prompt_restriction_note_mentions_queue_routing_and_daemon_control(tmp_path):
    """TB-142 / TB-145: the restriction note must (a) name `board_edit` as
    off-limits so the agent doesn't try it, (b) point to
    `operator_queue_append` as the queue-routed equivalent, and (c) keep
    `daemon_control` advertised so pause/resume mid-task still works.
    """
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg())
    assert "board_edit" in p
    assert "operator_queue_append" in p
    assert "daemon_control" in p


def test_prompt_mentions_approve_action(tmp_path):
    """TB-121 cross-ref: the prompt should mention 'approve' as a valid
    queue op so operators can approve ideation-proposed tasks via chat."""
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg())
    assert "approve" in p


def test_prompt_does_not_mention_conditional_toolset_switching(tmp_path):
    """TB-145 invariant: the prompt must NOT carry any "when a task is
    active" / "when the board is idle" / "your toolset varies" language.
    The handler always runs with the same fixed toolset and the prompt
    should reflect that — drift back into a conditional shape would be
    a structural regression."""
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg())
    lower = p.lower()
    forbidden = [
        "when a task is active",
        "when the board is idle",
        "task currently in flight",
        "toolset varies",
        "task agent is currently running",
        "your toolset is restricted: `cron_edit`",  # the old in-flight prefix phrasing
        "they'll be available again once the daemon is idle",
        "depending on board state",
        "depends on board state",
    ]
    for phrase in forbidden:
        assert phrase.lower() not in lower, (
            f"prompt mentions conditional toolset switching: {phrase!r}"
        )


# ── handle_message toolset selection ─────────────────────────────────────────


class _CapturingSDK:
    """Minimal fake SDK that records the allowed_tools passed to query()."""

    captured_tools: list[list[str]]

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def __init__(self):
        self.captured_tools = []

    def query(self, *, prompt, options):
        self.captured_tools.append(options.kw.get("allowed_tools", []))
        return self._empty()

    @staticmethod
    async def _empty():
        return
        yield  # make it an async generator


def test_handle_message_uses_mm_handler_tools_when_task_active(tmp_path):
    """TB-145: handle_message must pass MM_HANDLER_TOOLS regardless of
    whether the board has an Active task. The previous TB-122 design
    snapshot-checked the board and switched to RESTRICTED only when an
    Active task was present; that snapshot was a TOCTOU race, so the
    selection is now unconditional."""
    cfg = _cfg(tmp_path)
    _add_active_task(cfg)

    sdk = _CapturingSDK()
    msg = _base_msg()

    from ap2.daemon import handle_message
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=msg))

    assert sdk.captured_tools, "SDK.query was never called"
    assert sdk.captured_tools[0] == MM_HANDLER_TOOLS


def test_handle_message_uses_mm_handler_tools_when_idle(tmp_path):
    """TB-145: same fixed toolset on an idle board — no snapshot-based
    selection, so the toolset is identical to the active-task case."""
    cfg = _cfg(tmp_path)

    sdk = _CapturingSDK()
    msg = _base_msg()

    from ap2.daemon import handle_message
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=msg))

    assert sdk.captured_tools, "SDK.query was never called"
    assert sdk.captured_tools[0] == MM_HANDLER_TOOLS


def test_handle_message_does_not_consult_board_for_toolset_selection(tmp_path):
    """TB-145 (load-bearing): the handler must produce the exact same
    `allowed_tools` regardless of board state. This is the single
    behavioral pin against re-introducing the FULL/RESTRICTED toggle.
    Same fixture run twice — once idle, once with an Active task —
    must yield byte-identical toolsets."""
    cfg = _cfg(tmp_path)

    sdk_idle = _CapturingSDK()
    from ap2.daemon import handle_message
    asyncio.run(handle_message(cfg, sdk_idle, mcp_server=None, msg=_base_msg()))

    _add_active_task(cfg)
    sdk_active = _CapturingSDK()
    asyncio.run(handle_message(cfg, sdk_active, mcp_server=None, msg=_base_msg()))

    assert sdk_idle.captured_tools and sdk_active.captured_tools
    assert sdk_idle.captured_tools[0] == sdk_active.captured_tools[0], (
        "TB-145 violation: handle_message produced different toolsets for "
        "idle vs active boards. Toolset selection must be unconditional."
    )


def test_handle_message_emits_toolset_field_in_event(tmp_path):
    """`mattermost` event records which toolset shape was used (audit
    trail). TB-145: always "restricted" now (kept for downstream
    compat — events.jsonl readers may still filter on it)."""
    from ap2 import events as _events
    from ap2.daemon import handle_message

    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()
    _add_active_task(cfg)

    sdk = _CapturingSDK()
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_base_msg()))

    evts = _events.tail(cfg.events_file, 10)
    mm = [e for e in evts if e["type"] == "mattermost"]
    assert mm, "no mattermost event was emitted"
    assert mm[-1].get("toolset") == "restricted"


def test_handle_message_emits_toolset_restricted_on_idle_too(tmp_path):
    """TB-145: idle-board handler runs also emit `toolset=restricted` —
    the handler no longer has a "full" mode."""
    from ap2 import events as _events
    from ap2.daemon import handle_message

    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()

    sdk = _CapturingSDK()
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_base_msg()))

    evts = _events.tail(cfg.events_file, 10)
    mm = [e for e in evts if e["type"] == "mattermost"]
    assert mm and mm[-1].get("toolset") == "restricted"


# ── config ────────────────────────────────────────────────────────────────────


def test_config_has_mm_tick_interval_s(tmp_path):
    """Config must expose mm_tick_interval_s for the _mm_loop sleep."""
    cfg = _cfg(tmp_path)
    assert hasattr(cfg, "mm_tick_interval_s")
    assert cfg.mm_tick_interval_s == 10  # default


def test_config_mm_tick_interval_overrideable(tmp_path, monkeypatch):
    """AP2_MM_TICK_S env var must override the default."""
    monkeypatch.setenv("AP2_MM_TICK_S", "5")
    cfg = Config.load(tmp_path)
    assert cfg.mm_tick_interval_s == 5
