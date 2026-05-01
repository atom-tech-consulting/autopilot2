"""TB-122: unit tests for concurrent MM handler with restricted toolset.

Covers:
  (a) MM_HANDLER_TOOLS_RESTRICTED drops exactly cron_edit + ideation_state_write.
  (b) MM_HANDLER_TOOLS_RESTRICTED keeps the operator-facing tools.
  (c) build_mattermost_prompt includes the restriction note when task_in_flight=True.
  (d) build_mattermost_prompt has no restriction note when task_in_flight=False.
  (e) handle_message passes MM_HANDLER_TOOLS_RESTRICTED to SDK while a task is active.
  (f) handle_message passes MM_HANDLER_TOOLS_FULL to SDK when the board is idle.
  (g) AP2_MM_TICK_S overrides the default mm_tick_interval_s (10s).
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
    MM_HANDLER_TOOLS_FULL,
    MM_HANDLER_TOOLS_RESTRICTED,
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


def test_mm_handler_tools_full_equals_control_agent_tools():
    """MM_HANDLER_TOOLS_FULL is the same list as CONTROL_AGENT_TOOLS (no-change baseline)."""
    assert MM_HANDLER_TOOLS_FULL is CONTROL_AGENT_TOOLS or MM_HANDLER_TOOLS_FULL == CONTROL_AGENT_TOOLS


def test_restricted_drops_cron_edit():
    """MM_HANDLER_TOOLS_RESTRICTED must NOT include cron_edit."""
    assert "mcp__autopilot__cron_edit" not in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_drops_ideation_state_write():
    """MM_HANDLER_TOOLS_RESTRICTED must NOT include ideation_state_write."""
    assert "mcp__autopilot__ideation_state_write" not in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_drops_board_edit():
    """TB-142: direct `board_edit` is the second false-positive surface for
    TB-110's state-violation check. Operator chat commands that previously
    routed through `board_edit` (`@claude-bot freeze/approve/...`) now go
    through `operator_queue_append`; the daemon drains queued ops between
    tick stages, so the in-flight task's snapshot window is never racing a
    direct TASKS.md mutation. RESTRICTED must NOT contain `board_edit`.
    """
    assert "mcp__autopilot__board_edit" not in MM_HANDLER_TOOLS_RESTRICTED


def test_full_keeps_board_edit():
    """Idle handler runs (no Active tasks) keep direct `board_edit` — there's
    no in-flight task to violate against, so the queue-routing dance isn't
    needed. TB-142: only the RESTRICTED set drops `board_edit`."""
    assert "mcp__autopilot__board_edit" in MM_HANDLER_TOOLS_FULL


def test_restricted_keeps_daemon_control():
    """pause/resume mid-task is the primary use-case; daemon_control must stay."""
    assert "mcp__autopilot__daemon_control" in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_keeps_mattermost_reply():
    """Handler must still be able to reply."""
    assert "mcp__autopilot__mattermost_reply" in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_keeps_log_event():
    assert "mcp__autopilot__log_event" in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_keeps_operator_log_append():
    """TB-122: operator_log_append is the operator's veto channel mid-task."""
    assert "mcp__autopilot__operator_log_append" in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_keeps_read_glob_grep():
    """Read-only filesystem access must always be available."""
    for tool in ("Read", "Glob", "Grep"):
        assert tool in MM_HANDLER_TOOLS_RESTRICTED, f"{tool} missing from RESTRICTED"


def test_restricted_keeps_git_log_grep():
    assert "mcp__autopilot__git_log_grep" in MM_HANDLER_TOOLS_RESTRICTED


def test_restricted_is_strict_subset_of_full():
    """RESTRICTED ⊂ FULL — nothing new is added, some are dropped."""
    full_set = set(MM_HANDLER_TOOLS_FULL)
    restricted_set = set(MM_HANDLER_TOOLS_RESTRICTED)
    assert restricted_set < full_set, "RESTRICTED must be a strict subset of FULL"


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


def test_prompt_includes_restriction_note_when_in_flight(tmp_path):
    """Prompt must tell the agent why its toolset is narrower."""
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg(), task_in_flight=True)
    assert "task agent is currently running" in p
    assert "cron_edit" in p
    assert "ideation_state_write" in p
    assert "restricted toolset" in p or "Restricted toolset" in p


def test_prompt_no_restriction_note_when_idle(tmp_path):
    """Default prompt (no in-flight task) must NOT inject the restriction note."""
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg())
    assert "task agent is currently running" not in p
    assert "restricted toolset" not in p and "Restricted toolset" not in p


def test_prompt_restriction_note_mentions_queue_routing_and_daemon_control(tmp_path):
    """TB-142: the restriction note must (a) name `board_edit` as off-limits
    so the agent doesn't try it, (b) point to `operator_queue_append` as
    the queue-routed equivalent, and (c) keep `daemon_control` advertised
    so pause/resume mid-task still works.
    """
    cfg = _cfg(tmp_path)
    p = build_mattermost_prompt(cfg, _base_msg(), task_in_flight=True)
    # board_edit is mentioned (as disabled), and queue-routing is named.
    assert "board_edit" in p
    assert "operator_queue_append" in p
    assert "daemon_control" in p


def test_prompt_mentions_approve_action(tmp_path):
    """TB-121 cross-ref: prompt should mention 'approve' as a valid board_edit action."""
    cfg = _cfg(tmp_path)
    # Should appear in both modes (it's in the "Your job" section).
    p = build_mattermost_prompt(cfg, _base_msg())
    assert "approve" in p


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


def test_handle_message_uses_restricted_toolset_when_task_active(tmp_path):
    """handle_message must pass MM_HANDLER_TOOLS_RESTRICTED when Active task exists."""
    cfg = _cfg(tmp_path)
    _add_active_task(cfg)

    sdk = _CapturingSDK()
    msg = _base_msg()

    from ap2.daemon import handle_message
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=msg))

    assert sdk.captured_tools, "SDK.query was never called"
    assert sdk.captured_tools[0] == MM_HANDLER_TOOLS_RESTRICTED


def test_handle_message_uses_full_toolset_when_idle(tmp_path):
    """handle_message must pass MM_HANDLER_TOOLS_FULL when board is idle."""
    cfg = _cfg(tmp_path)

    sdk = _CapturingSDK()
    msg = _base_msg()

    from ap2.daemon import handle_message
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=msg))

    assert sdk.captured_tools, "SDK.query was never called"
    assert sdk.captured_tools[0] == MM_HANDLER_TOOLS_FULL


def test_handle_message_emits_toolset_field_in_event(tmp_path):
    """`mattermost` event records which toolset shape was selected (audit trail)."""
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


def test_handle_message_toolset_full_emitted_on_idle(tmp_path):
    from ap2 import events as _events
    from ap2.daemon import handle_message

    cfg = _cfg(tmp_path)
    cfg.ensure_dirs()

    sdk = _CapturingSDK()
    asyncio.run(handle_message(cfg, sdk, mcp_server=None, msg=_base_msg()))

    evts = _events.tail(cfg.events_file, 10)
    mm = [e for e in evts if e["type"] == "mattermost"]
    assert mm and mm[-1].get("toolset") == "full"


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
