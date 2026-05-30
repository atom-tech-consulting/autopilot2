"""TB-246: `_maybe_ideate` roadmap-complete skip gate.

Pins the new axis-4 ideation gate (sibling to the TB-174
focus-exhausted gate). When `goal.roadmap_exhausted(cfg)` returns
True (the focus-list pointer has advanced past the last `## Current
focus:` heading AND the operator has not yet acked the
`roadmap_complete` halt), `_maybe_ideate` skips the SDK call, emits
`ideation_skipped reason=roadmap_complete`, and calls `mark_run` so
the cooldown clock advances. `force_ideate` bypasses the gate so
the operator's recovery path (`ap2 ack roadmap_complete && ap2
update-goal && ap2 ideate --force`) still works.

Mirrors `test_maybe_ideate_skips_when_all_focus_exhausted` /
`test_maybe_ideate_runs_when_any_focus_in_progress` /
`test_force_ideate_bypasses_focus_exhausted_gate` from TB-174
(`test_ideation_trigger.py:733+`) — the two gates are intentionally
parallel.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ap2 import events
from ap2.board import Board
from ap2.config import Config
from ap2.cron import load_state, save_state
from ap2.ideation import (
    IDEATION_NAME,
    _maybe_ideate,
    force_ideate,
)


# ---------------------------------------------------------------------------
# Fixture helpers — local copies of `test_ideation_trigger.py`'s
# `_stub_run_control_agent` / `_make_project` so this module is
# self-contained and a future refactor of the TB-174 module doesn't
# silently neutralize the TB-246 pins.


def _stub_run_control_agent(monkeypatch):
    """Replace `daemon._run_control_agent` with a no-op recorder.

    Mirrors the TB-160 module's stub so the gate tests can detect
    whether the SDK would have been invoked without paying the real
    SDK cost.
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

    from ap2 import daemon as _daemon
    monkeypatch.setattr(_daemon, "_run_control_agent", fake)
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", fake_snapshot)
    monkeypatch.setattr(_daemon, "_changed_state_paths", fake_changed)
    monkeypatch.setattr(_daemon, "_commit_state_files", fake_commit)
    return calls


def _make_project(
    tmp_path: Path,
    monkeypatch,
    *,
    sections: dict[str, list[tuple[str, str]]],
) -> Config:
    """Build a Config + populated TASKS.md under `tmp_path`."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-100\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("Test ideation prompt body.\n")

    board = Board.load(cfg.tasks_file)
    for section, tasks in sections.items():
        for tid, title in tasks:
            board.add(section, task_id=tid, title=title)
    board.save()

    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "0")
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    save_state(cfg.cron_state_file, {IDEATION_NAME: time.time() - 10000})
    return cfg


def _write_goal_with_focus(cfg: Config, foci: list[str]) -> None:
    """Write a minimal goal.md under `cfg.project_root` populated with
    one `## Current focus:` heading per entry in `foci`. Each focus
    gets a tiny body so `parse_focus_list` returns one FocusItem per
    heading.
    """
    body = ["# Goal\n", "\n", "## Mission\n\n- placeholder.\n", "\n"]
    for title in foci:
        body.append(f"## Current focus: {title}\n\n- one focus body bullet.\n\n")
    (cfg.project_root / "goal.md").write_text("".join(body))


def _set_pointer_past_last_focus(cfg: Config, foci_count: int) -> None:
    """Write `focus_pointer.json` with `roadmap_complete_emitted=True`
    so `goal.roadmap_exhausted(cfg)` returns True (the halt flag set
    by the TB-342-collapsed detector). `foci_count` is retained for
    call-site parity with the pre-TB-342 helper signature; the new
    schema has no per-focus pointer fields."""
    import json
    payload = {
        "schema": 1,
        "empty_cycles": 0,
        "roadmap_complete_ack_idx": None,
        "roadmap_complete_emitted": True,
        "updated_ts": "2026-05-17T00:00:00Z",
    }
    pointer_path = cfg.project_root / ".cc-autopilot" / "focus_pointer.json"
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Gate behavior.


def test_maybe_ideate_skips_when_roadmap_exhausted(tmp_path, monkeypatch):
    """`goal.roadmap_exhausted(cfg)` True → `_maybe_ideate` skips the
    SDK call, emits `ideation_skipped reason=roadmap_complete`, and
    calls `mark_run` so the cooldown clock advances.

    Pins all four briefing-specified behaviors:
      (a) the SDK is NOT invoked (capture spy stays empty);
      (b) `ideation_skipped` lands in events.jsonl with
          `reason=roadmap_complete`;
      (c) `mark_run` advances the cooldown timestamp (so a 30s
          daemon tick doesn't keep re-evaluating the gate every loop);
      (d) the historical entry-marker `ideation_empty_board` is
          ABSENT — this is a skip, not an SDK invocation.
    """
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first")]},
    )
    _write_goal_with_focus(cfg, ["axis 1: foo", "axis 2: bar"])
    _set_pointer_past_last_focus(cfg, foci_count=2)
    # Anchor the cooldown timestamp at a stale value so the post-call
    # check can prove `mark_run` advanced it.
    save_state(cfg.cron_state_file, {IDEATION_NAME: 0.0})
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    # (a) SDK not invoked.
    assert calls == [], "ideation should have skipped — roadmap exhausted"

    # (b) Skip event landed with the briefing-pinned shape.
    evts = events.tail(cfg.events_file, 20)
    skips = [e for e in evts if e["type"] == "ideation_skipped"]
    assert len(skips) == 1, (
        f"expected exactly one ideation_skipped event; got: "
        f"{[e['type'] for e in evts]}"
    )
    assert skips[0]["reason"] == "roadmap_complete", (
        f"reason must be `roadmap_complete`; got {skips[0]!r}"
    )

    # (c) Cooldown advanced.
    state = load_state(cfg.cron_state_file)
    assert state[IDEATION_NAME] > time.time() - 5, (
        "_maybe_ideate must call mark_run on the roadmap-complete skip "
        "path so the cooldown clock advances"
    )

    # (d) The historical entry-marker is absent.
    assert "ideation_empty_board" not in [e["type"] for e in evts]


def test_maybe_ideate_runs_when_roadmap_not_exhausted(tmp_path, monkeypatch):
    """`goal.roadmap_exhausted(cfg)` False (pointer still inside the
    focus list) does NOT trip the gate — the natural path still
    fires. Regression pin against over-application: this gate must
    only fire on the formal axis-4 halt state, never on a
    mid-roadmap project.
    """
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first")]},
    )
    _write_goal_with_focus(cfg, ["axis 1: foo", "axis 2: bar"])
    # Default pointer (file absent) has `roadmap_complete_emitted=False`,
    # so `goal.roadmap_exhausted(cfg)` returns False naturally — the
    # ideation gate doesn't trip.
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, (
        "ideation should have fired — pointer is still inside the focus "
        "list (roadmap NOT exhausted)"
    )
    # And no `ideation_skipped reason=roadmap_complete` event lands —
    # the gate did not trip.
    evts = events.tail(cfg.events_file, 20)
    skips = [
        e for e in evts
        if e["type"] == "ideation_skipped" and e.get("reason") == "roadmap_complete"
    ]
    assert skips == [], (
        f"roadmap-complete skip must NOT fire when the pointer is inside "
        f"the focus list; events: {[e['type'] for e in evts]}"
    )


def test_force_ideate_bypasses_roadmap_complete_gate(tmp_path, monkeypatch):
    """`force_ideate` invokes the SDK even when
    `goal.roadmap_exhausted(cfg)` is True. The forced path is the
    operator's recovery override — typically used after `ap2 ack
    roadmap_complete && ap2 update-goal` so the fresh focus has
    somewhere to land its first proposals, BEFORE the next tick has
    advanced the pointer.

    Sanity-checks both halves: (a) `_maybe_ideate` would have
    skipped on this fixture (precondition); (b) `force_ideate`
    invokes the SDK once anyway.
    """
    cfg = _make_project(tmp_path, monkeypatch, sections={})
    _write_goal_with_focus(cfg, ["only axis"])
    _set_pointer_past_last_focus(cfg, foci_count=1)
    save_state(cfg.cron_state_file, {IDEATION_NAME: time.time() - 10000})

    # (a) Precondition: `_maybe_ideate` skips on this fixture.
    natural_calls = _stub_run_control_agent(monkeypatch)
    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))
    assert natural_calls == [], (
        "_maybe_ideate sanity check — roadmap-complete gate must skip "
        "before we test the forced override"
    )

    # (b) `force_ideate` runs the helper anyway — the forced path
    # bypasses the roadmap-complete gate.
    asyncio.run(force_ideate(cfg, sdk=None, mcp_server=None))
    assert len(natural_calls) == 1, (
        "force_ideate must invoke the control agent unconditionally — "
        "the roadmap-complete gate is bypassed on the forced path"
    )


def test_skip_event_precedes_mark_run(tmp_path, monkeypatch):
    """Ordering pin: the `ideation_skipped reason=roadmap_complete`
    event lands in events.jsonl BEFORE `mark_run` bumps the
    cron-state timestamp. A reader inspecting `events.jsonl` after
    the skip should always see the event accompanied by a fresh
    cron-state file, never the inverse (cron-state advanced but
    event missing). Captured by patching `mark_run` and asserting
    the event is already present at patch-call time.
    """
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first")]},
    )
    _write_goal_with_focus(cfg, ["axis a"])
    _set_pointer_past_last_focus(cfg, foci_count=1)
    _stub_run_control_agent(monkeypatch)

    seen_event_at_mark_run: dict[str, bool] = {"value": False}

    import ap2.ideation as ideation_mod
    real_mark_run = ideation_mod.mark_run

    def spy_mark_run(state_path, name):
        evts = events.tail(cfg.events_file, 20)
        seen_event_at_mark_run["value"] = any(
            e.get("type") == "ideation_skipped"
            and e.get("reason") == "roadmap_complete"
            for e in evts
        )
        return real_mark_run(state_path, name)

    monkeypatch.setattr(ideation_mod, "mark_run", spy_mark_run)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert seen_event_at_mark_run["value"], (
        "ordering invariant — `ideation_skipped reason=roadmap_complete` "
        "must be appended to events.jsonl BEFORE `mark_run` advances the "
        "cron-state timestamp so an external reader never sees a fresh "
        "cron-state without the matching skip event."
    )
