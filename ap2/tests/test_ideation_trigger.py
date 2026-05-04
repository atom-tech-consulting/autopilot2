"""TB-160: AP2_IDEATION_TRIGGER_TASK_COUNT trigger-threshold gate.

Pins the new env knob (`AP2_IDEATION_TRIGGER_TASK_COUNT`, default 3) and
its semantics:

- The threshold compares against the **Ready+Backlog count** only;
  Pipeline Pending and Frozen don't count.
- Active is a HARD gate independent of the threshold — non-empty Active
  always skips, regardless of count.
- `>=` semantics (skip when count >= threshold; fire when below).
- Invalid env values (non-int / non-positive / empty) fall back to the
  module default silently — same permissive style as `_cooldown_s`.

These tests sidestep the e2e SDK harness and call `_maybe_ideate`
directly with a stub SDK / mcp_server, asserting on board mutations and
events to determine whether the gate fired.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from ap2 import events
from ap2 import ideation
from ap2.board import Board
from ap2.config import Config
from ap2.cron import save_state
from ap2.ideation import (
    IDEATION_TRIGGER_TASK_COUNT_DEFAULT,
    _maybe_ideate,
    _trigger_task_count,
)


# ---------------------------------------------------------------------------
# Module constant + env-knob parsing pins.


def test_default_trigger_count_is_three():
    """The module default is 3 — matches the prompt's `fewer than 3 workable
    items` cap."""
    assert IDEATION_TRIGGER_TASK_COUNT_DEFAULT == 3


def test_trigger_task_count_unset_returns_default(monkeypatch):
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    assert _trigger_task_count() == IDEATION_TRIGGER_TASK_COUNT_DEFAULT


def test_trigger_task_count_int_overrides_default(monkeypatch):
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    assert _trigger_task_count() == 5


def test_trigger_task_count_invalid_falls_back_to_default(monkeypatch):
    """Non-int / non-positive / empty values fall back to the default —
    same permissive style as `_cooldown_s`."""
    for bad in ("abc", "-1", "0", "", "  ", "3.14", "1e3"):
        monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", bad)
        assert _trigger_task_count() == IDEATION_TRIGGER_TASK_COUNT_DEFAULT, (
            f"value {bad!r} should fall back to default"
        )


# ---------------------------------------------------------------------------
# Gate behavior — exercise `_maybe_ideate` end-to-end with stubs.


def _stub_run_control_agent(monkeypatch):
    """Replace `daemon._run_control_agent` with a no-op recorder.

    Returns a list that gets a single sentinel appended each time the
    control agent would have been invoked. Tests assert on len(calls).
    """
    calls: list[dict] = []

    async def fake(cfg, sdk, mcp_server, *, label, prompt, allowed_tools, max_turns):
        calls.append({"label": label, "prompt": prompt})
        return (False, None, "", Path("/tmp/fake-prompt-dump"))

    def fake_snapshot(cfg):
        return {}

    def fake_changed(pre, post):
        return []

    def fake_commit(*args, **kwargs):
        pass

    # Lazy-import inside the fixture so we monkeypatch the same `daemon`
    # module ideation imports lazily.
    from ap2 import daemon as _daemon
    monkeypatch.setattr(_daemon, "_run_control_agent", fake)
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", fake_snapshot)
    monkeypatch.setattr(_daemon, "_changed_state_paths", fake_changed)
    monkeypatch.setattr(_daemon, "_commit_state_files", fake_commit)
    return calls


def _make_project(tmp_path: Path, monkeypatch, *, sections: dict[str, list[tuple[str, str]]]) -> Config:
    """Build a Config + populated TASKS.md under `tmp_path`."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-100\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    # Project ideation prompt override — keeps the prompt small + avoids
    # depending on the load-bearing default prompt's content.
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("Test ideation prompt body.\n")

    board = Board.load(cfg.tasks_file)
    for section, tasks in sections.items():
        for tid, title in tasks:
            board.add(section, task_id=tid, title=title)
    board.save()

    # Set the cooldown to 0 so it never short-circuits these tests.
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "0")
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    # Ensure the cooldown timestamp is well in the past.
    save_state(cfg.cron_state_file, {"ideation": time.time() - 10000})
    return cfg


def test_default_threshold_fires_when_two_backlog_tasks(tmp_path, monkeypatch):
    """Default threshold 3, Backlog has 2 (Active/Ready empty) → ideation fires.

    The historical behavior was "skip if any Backlog item exists" — this
    test verifies the new behavior under the default knob."""
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first"), ("TB-2", "second")]},
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, "ideation should have fired (count=2 < default 3)"
    kinds = [e["type"] for e in events.tail(cfg.events_file, 20)]
    assert "ideation_empty_board" in kinds


def test_threshold_one_skips_with_two_backlog_tasks(tmp_path, monkeypatch):
    """Threshold=1 reproduces the legacy "fire only when fully empty" behavior:
    2 Backlog items → skip."""
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "1")
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first"), ("TB-2", "second")]},
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert calls == [], "ideation should have skipped (count=2 >= threshold 1)"
    kinds = [e["type"] for e in events.tail(cfg.events_file, 20)]
    assert "ideation_empty_board" not in kinds


def test_active_is_hard_gate_independent_of_threshold(tmp_path, monkeypatch):
    """Active=non-empty + Ready/Backlog empty → skip even though count (0) <
    threshold (default 3). Pins the SDK-contention safety gate."""
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Active": [("TB-1", "in flight")]},
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert calls == [], "ideation should have skipped (Active hard-gate)"
    kinds = [e["type"] for e in events.tail(cfg.events_file, 20)]
    assert "ideation_empty_board" not in kinds


def test_threshold_boundary_skip_at_exact_count(tmp_path, monkeypatch):
    """`>=` semantics: count == threshold causes skip, NOT fire."""
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "3")
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Ready": [("TB-1", "ready 1")],
            "Backlog": [("TB-2", "backlog 1"), ("TB-3", "backlog 2")],
        },
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert calls == [], "ideation should have skipped at exact count == threshold"


def test_threshold_boundary_fire_below_count(tmp_path, monkeypatch):
    """Threshold-minus-one items causes ideation to fire."""
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "3")
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Ready": [("TB-1", "ready 1")],
            "Backlog": [("TB-2", "backlog 1")],
        },
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, "ideation should have fired (count=2 < threshold 3)"


def test_threshold_counts_ready_plus_backlog_not_pipeline_or_frozen(tmp_path, monkeypatch):
    """Pipeline Pending and Frozen don't count toward the threshold.

    Board: 1 Pipeline Pending + 1 Frozen + 0 Ready + 0 Backlog → count=0,
    below default threshold 3 → ideation fires."""
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Pipeline Pending": [("TB-1", "pipeline running")],
            "Frozen": [("TB-2", "stuck")],
        },
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, (
        "ideation should fire — Pipeline Pending + Frozen do not count "
        "toward the Ready+Backlog threshold"
    )
