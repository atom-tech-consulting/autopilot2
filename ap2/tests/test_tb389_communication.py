"""TB-389: the `communication` component — channel surface (inbound +
outbound) wrapping the channel adapters.

Pins the cleavage TB-389 introduces:

  1. The channel surface — previously split across core's removed
     `registry.channel_adapters()` (outbound) and the `inbound_poll`
     hook_point (inbound), with `mattermost` as a top-level component —
     is now owned by a single `communication` component that holds its
     channel adapters in an INTERNAL registry invisible to core.
  2. Outbound is EVENT-DRIVEN: a core call site enqueues onto the
     `ap2.notify` queue and the component's `run_outbound_tick`
     (registered on `Phase.COMMUNICATION`) delivers + marks delivered.
  3. Inbound (`poll_inbound`) walks the same internal channel registry.
  4. Mattermost is wired as a channel adapter under the communication
     component, not a top-level loop participant.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from ap2 import daemon, events, notify
from ap2.config import Config
from ap2.registry import Manifest, Phase, Registry, default_registry
from ap2.components.communication import (
    channel_adapters,
    channel_registry,
    poll_inbound,
    run_outbound_tick,
)


def _project(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _patch_mm_post(monkeypatch, *, raises: Exception | None = None):
    posts: list[dict] = []

    def _fake(channel: str, text: str, thread_id: str = "") -> str:
        posts.append({"channel": channel, "text": text, "thread_id": thread_id})
        if raises is not None:
            raise raises
        return "post-xyz"

    from ap2 import tools
    monkeypatch.setattr(tools, "_mm_post", _fake)
    return posts


def _types(cfg: Config) -> list[str]:
    return [e.get("type") for e in events.tail(cfg.events_file, 50)]


# ---------------------------------------------------------------------------
# Registry shape: communication discovered + always-on; mattermost demoted.
# ---------------------------------------------------------------------------


def test_communication_is_discovered_mattermost_is_not():
    reg = Registry.discover()
    names = {m.name for m in reg.components}
    assert "communication" in names
    assert "mattermost" not in names, (
        "TB-389: mattermost was demoted to a channel adapter under the "
        "communication component — it must NOT be a discovered top-level "
        "loop participant."
    )


def test_communication_manifest_registers_inbound_and_outbound_tickwork():
    reg = Registry.discover()
    manifest = reg.get("communication")
    assert isinstance(manifest, Manifest)
    # Always-on (mirrors attention) — channel multiplicity is no longer a
    # kernel toggle.
    assert manifest.env_flag is None
    assert manifest.default_enabled is True
    # Inbound + outbound tick-phase work.
    assert callable(manifest.hook_points.get("poll_inbound"))
    assert callable(manifest.hook_points.get("outbound_tick"))
    # Outbound delivery is a Phase.COMMUNICATION tick hook.
    phases = [p for (p, _h) in manifest.tick_hooks]
    assert Phase.COMMUNICATION in phases
    # MCP-tool handlers for the Mattermost channel are re-exported here.
    assert callable(manifest.hook_points.get("mcp_tool_reply"))
    assert callable(manifest.hook_points.get("mcp_tool_thread_read"))


def test_core_registry_has_no_channel_adapters_surface():
    assert not hasattr(default_registry(), "channel_adapters"), (
        "TB-389: `registry.channel_adapters()` must be removed from core."
    )


# ---------------------------------------------------------------------------
# Internal channel registry gating.
# ---------------------------------------------------------------------------


def test_channel_registry_gates_on_mm_channels(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    assert channel_registry(cfg) == []
    assert channel_adapters(cfg) == []

    monkeypatch.setenv("AP2_MM_CHANNELS", "ch-1")
    chans = channel_registry(cfg)
    assert [c.name for c in chans] == ["mattermost"]
    assert chans[0].inbound is not None  # inbound-capable


# ---------------------------------------------------------------------------
# Inbound poll.
# ---------------------------------------------------------------------------


def test_poll_inbound_routes_through_channel_registry(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch-1")

    sentinel = [{"id": "p1", "text": "@claude-bot hi"}]
    import ap2.components.mattermost as mm
    # `channel_registry` does `from ap2.components.mattermost import
    # check_new_messages`, which reads the package-level name — patch
    # there (not on the `impl` submodule whose binding is already
    # re-exported into the package namespace).
    monkeypatch.setattr(mm, "check_new_messages", lambda cfg: sentinel)

    assert poll_inbound(cfg) == sentinel

    # No channel configured → nothing to poll.
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    assert poll_inbound(cfg) == []


# ---------------------------------------------------------------------------
# Outbound delivery: event-driven, delivered + idempotent.
# ---------------------------------------------------------------------------


def test_outbound_tick_delivers_and_marks_delivered(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch-1")
    posts = _patch_mm_post(monkeypatch)

    notify.enqueue(cfg, "hello world", kind="auto_diagnose")
    assert len(notify.pending(cfg)) == 1

    run_outbound_tick(cfg)
    assert len(posts) == 1
    assert posts[0]["text"] == "hello world"
    assert "notification_delivered" in _types(cfg)
    # Marked delivered → no longer pending.
    assert notify.pending(cfg) == []

    # Idempotent: a second tick re-posts nothing.
    run_outbound_tick(cfg)
    assert len(posts) == 1


def test_outbound_tick_no_channel_leaves_pending(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    posts = _patch_mm_post(monkeypatch)

    notify.enqueue(cfg, "queued", kind="smoke_alert")
    run_outbound_tick(cfg)

    assert posts == []
    # Stays pending so a later-configured channel still gets it.
    assert len(notify.pending(cfg)) == 1


def test_outbound_tick_post_failure_emits_error_and_retries(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch-1")
    _patch_mm_post(monkeypatch, raises=RuntimeError("network down"))

    notify.enqueue(cfg, "boom", kind="auto_diagnose")
    run_outbound_tick(cfg)
    err = [e for e in events.tail(cfg.events_file, 50)
           if e.get("type") == "notification_error"]
    assert len(err) == 1
    assert "RuntimeError" in err[0]["error"]
    # Left pending for retry.
    assert len(notify.pending(cfg)) == 1


# ---------------------------------------------------------------------------
# Daemon wiring: _tick walks Phase.COMMUNICATION; no inbound_poll literal.
# ---------------------------------------------------------------------------


def test_tick_walks_communication_phase_with_error_wrap():
    src = inspect.getsource(daemon._tick)
    assert "Phase.COMMUNICATION" in src, (
        "TB-389: daemon._tick must walk the communication outbound phase."
    )
    # The walk is wrapped so a delivery hiccup can't abort the tick.
    assert "communication_error" in src


def test_daemon_has_no_inbound_poll_literal():
    src = inspect.getsource(daemon)
    assert "inbound_poll" not in src, (
        "TB-389: core no longer polls inbound via the one-off "
        "`inbound_poll` hook_point."
    )


def test_check_inbound_messages_routes_through_communication(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch-1")
    sentinel = [{"id": "p9", "text": "@claude-bot ping"}]
    import ap2.components.mattermost as mm
    # `channel_registry` does `from ap2.components.mattermost import
    # check_new_messages`, which reads the package-level name — patch
    # there (not on the `impl` submodule whose binding is already
    # re-exported into the package namespace).
    monkeypatch.setattr(mm, "check_new_messages", lambda cfg: sentinel)
    assert daemon._check_inbound_messages(cfg) == sentinel
