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
from ap2.cron import load_state, save_state
from ap2.ideation import (
    IDEATION_NAME,
    IDEATION_TRIGGER_TASK_COUNT_DEFAULT,
    _maybe_ideate,
    _trigger_task_count,
    force_ideate,
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


# ---------------------------------------------------------------------------
# TB-159: `force_ideate` — manual operator trigger that bypasses the
# natural empty-board / cooldown / `AP2_IDEATION_DISABLED` gates.


def test_force_ideate_bypasses_disable_cooldown_and_backlog_gates(
    tmp_path, monkeypatch
):
    """force_ideate fires even when ALL three of the natural gates would
    have skipped:
      - `AP2_IDEATION_DISABLED=1` is set
      - cooldown is unmet (a `mark_run` happened recently)
      - Backlog has more items than the trigger threshold

    The Active hard gate is enforced at queue-append time, NOT inside
    `force_ideate` itself — once the daemon dispatches the helper, it
    runs unconditionally. The helper still calls `mark_run` after the
    run so the next natural cooldown clock resets (back-to-back forced
    fires don't lap the next cron-driven natural fire)."""
    # Build a project with a Backlog that would otherwise skip the
    # natural threshold (10 items >= default 3).
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Backlog": [
                (f"TB-{i}", f"existing backlog {i}") for i in range(1, 11)
            ],
        },
    )

    # Set ALL natural-skip conditions:
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    # Cooldown is unmet — last fire was 1s ago.
    save_state(cfg.cron_state_file, {IDEATION_NAME: time.time() - 1})

    # Sanity: `_maybe_ideate` would have skipped here.
    natural_calls = _stub_run_control_agent(monkeypatch)
    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))
    assert natural_calls == [], (
        "_maybe_ideate must skip with disabled=1 / cooldown unmet / "
        "10 Backlog items — sanity check on the precondition"
    )

    # And now force_ideate runs the helper anyway.
    asyncio.run(force_ideate(cfg, sdk=None, mcp_server=None))
    assert len(natural_calls) == 1, (
        "force_ideate must invoke the control agent unconditionally — "
        "disabled / cooldown / backlog gates are bypassed"
    )

    # Cooldown clock was bumped — the helper called `mark_run` after the
    # run so the next natural fire still has to wait
    # AP2_IDEATION_COOLDOWN_S. Without this, repeated `ap2 ideate` calls
    # could leave the natural cooldown stale and double-fire on the next
    # cron tick.
    state = load_state(cfg.cron_state_file)
    assert state[IDEATION_NAME] > time.time() - 5, (
        "force_ideate must call mark_run after the run so the natural "
        "cooldown clock advances"
    )


# ---------------------------------------------------------------------------
# TB-168: ideation's `_run_ideation` calls `build_control_prompt` with
# `include_board=False, include_commits=False` so the rendered prompt has
# only `now:` under the snapshot header. Pin the captured prompt shape via
# the same `_stub_run_control_agent` harness the trigger-gate tests use.


def test_run_ideation_prompt_omits_board_counts_and_recent_commits(
    tmp_path, monkeypatch
):
    """Integration-flavored: drive `_maybe_ideate` end-to-end with the
    standard stub harness and assert the captured prompt's snapshot
    block matches the TB-168 trimmed shape:
      (a) `now:` line is present (load-bearing — ideation has no other
          clock for `_Last updated:` in `ideation_state.md`).
      (b) `board:` line is absent (ideation re-derives counts from
          TASKS.md per its read-order; the pre-flight snapshot is
          redundant).
      (c) `recent commits (HEAD~10):` heading is absent — and no commit
          short-sha lines appear inside the snapshot block (~60% of the
          original 10 lines are `state:` daemon meta-commits with no
          signal; the remaining feature lines are subsumed by
          `progress.md`).
    """
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first"), ("TB-2", "second")]},
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, "ideation should have fired"
    prompt = calls[0]["prompt"]

    # (a) Snapshot header + `now:` line are intact.
    assert (
        "## Current state (rendered just before this prompt was sent)"
        in prompt
    )
    import re

    assert re.search(
        r"- now: 20\d\d-[01]\d-[0-3]\dT[0-2]\d:[0-5]\d:[0-5]\dZ", prompt
    ), "trimmed snapshot must still carry a `now:` line for ideation"

    # (b) No `board:` snapshot line. The `(Active/Ready/Backlog/...)`
    # legend is the strong negative pin — it lived only on the `board:`
    # line, so its absence proves the line is gone (not just its label).
    assert "- board:" not in prompt
    assert (
        "(Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)"
        not in prompt
    )

    # (c) The recent-commits sub-block is gone. Search inside the
    # snapshot block specifically — TB-N references in the `## Recent
    # events` tail aren't load-bearing here. The snapshot ends at the
    # blank line before `## Control job:`.
    snapshot_start = prompt.find(
        "## Current state (rendered just before this prompt was sent)"
    )
    snapshot_end = prompt.find("## Control job:", snapshot_start)
    assert snapshot_start != -1 and snapshot_end != -1
    snapshot = prompt[snapshot_start:snapshot_end]
    assert "recent commits" not in snapshot.lower()
    # Commit lines render as `^  [0-9a-f]{7,40} <subject>`. Even when
    # the test fixture's tmp_path has no `.git` (so the original code
    # would have rendered "(git log unavailable)"), the heading line
    # itself was the load-bearing thing — pin its absence directly,
    # then double-check no commit-shaped lines slipped through.
    for line in snapshot.splitlines():
        assert not re.match(r"^  [0-9a-f]{7,40} ", line), (
            f"snapshot still contains a commit-shaped line: {line!r}"
        )


def test_force_ideate_emits_ideation_empty_board_event(tmp_path, monkeypatch):
    """The shared `_run_ideation` helper still emits
    `ideation_empty_board` as the entry marker (the historical event
    name). Forced runs are distinguished from natural ones by the
    `ideation_forced` event the operator-queue drain emits — NOT by the
    entry marker. Pin both behaviors here so a future rename is a
    deliberate decision, not an accident."""
    cfg = _make_project(tmp_path, monkeypatch, sections={})
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    _stub_run_control_agent(monkeypatch)

    asyncio.run(force_ideate(cfg, sdk=None, mcp_server=None))

    kinds = [e["type"] for e in events.tail(cfg.events_file, 20)]
    assert "ideation_empty_board" in kinds


# ---------------------------------------------------------------------------
# TB-169: `_run_ideation` calls `build_control_prompt` with
# `include_types=IDEATION_RELEVANT_EVENT_TYPES` so the rendered `## Recent
# events` tail filters out observability/plumbing noise. Pin the captured
# prompt's events-block contents — at least one allowlisted kind survives,
# noise kinds (`judge_call` is the load-bearing example from the briefing)
# do NOT appear inside the events block.


def test_run_ideation_prompt_filters_events_block_to_relevant_kinds(
    tmp_path, monkeypatch
):
    """End-to-end: drive `_maybe_ideate` with the standard stub harness
    and a fixture seeding both relevant (`task_complete`) and noise
    (`judge_call`) events. The captured prompt's `## Recent events`
    block must contain `task_complete` and NOT `judge_call`.

    This is the load-bearing TB-169 integration check — it's what the
    daemon will actually do per ideation cycle. The unit-level filter
    behavior is covered in `test_prompts.py`; this test pins the
    wiring."""
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first"), ("TB-2", "second")]},
    )
    # Seed events.jsonl with a mix of relevant + noise kinds. The
    # `_make_project` flow doesn't write events, so the file starts
    # empty here (modulo whatever `_maybe_ideate` itself appends post-
    # call — which lands AFTER the prompt is built and so isn't in the
    # captured tail).
    events.append(cfg.events_file, "task_complete", task="TB-99",
                  status="complete", commit="abc1234")
    events.append(cfg.events_file, "judge_call", task="TB-99",
                  bullet_idx=0, verdict="pass")
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(cfg.events_file, "verification_failed", task="TB-98")

    calls = _stub_run_control_agent(monkeypatch)
    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, "ideation should have fired (count=2 < default 3)"
    prompt = calls[0]["prompt"]

    # Anchor on the events-block heading — TB-N references can appear
    # elsewhere in the prompt (e.g. inside a future board snapshot or
    # operator-rejection block), but the TB-169 filter only governs the
    # `## Recent events` tail.
    events_start = prompt.find("## Recent events")
    assert events_start != -1, "events block heading missing from prompt"
    events_block = prompt[events_start:]

    # Positive: at least one allowlisted kind survives the filter.
    # `task_complete` is the briefing's load-bearing example.
    assert " task_complete " in events_block, (
        "expected `task_complete` line in filtered events block"
    )
    # And another lifecycle kind from the same allowlist.
    assert " verification_failed " in events_block

    # Negative: zero `judge_call` lines — this is the briefing's
    # primary concrete failure mode (full token-usage payloads at ~2KB
    # each blowing the 6KB budget).
    assert " judge_call " not in events_block, (
        "noise type `judge_call` should have been filtered out of the "
        "ideation prompt's events block"
    )
    # And `cron_complete` (cron-lifecycle plumbing — explicit briefing
    # callout).
    assert " cron_complete " not in events_block


# ---------------------------------------------------------------------------
# TB-183: pre-computed proposal-slot count flows into the prompt's
# `## Current state` snapshot block via `state_extras` (TB-151), so the
# prompt body's "propose at most N" instruction reads N from a single
# source of truth instead of the pre-TB-183 hardcoded magic-3 (which
# drifted out of sync with `AP2_IDEATION_TRIGGER_TASK_COUNT` once
# operators bumped the env knob).


def test_slot_count_injected_into_state_extras(tmp_path, monkeypatch):
    """`_maybe_ideate` with `AP2_IDEATION_TRIGGER_TASK_COUNT=5` and a board
    of 2 Ready + 1 Backlog (workable=3) computes `slots = 5 - 3 = 2` and
    passes the line `- proposal slots this cycle: 2` into the prompt's
    `## Current state` snapshot block.

    Pins both halves: (a) the captured prompt's snapshot block contains
    the exact slot-count line; (b) the prompt body references the
    snapshot value (`proposal slots this cycle` / "at most N") instead
    of the pre-TB-183 hardcoded `fewer than 3`.
    """
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Ready": [("TB-1", "ready 1"), ("TB-2", "ready 2")],
            "Backlog": [("TB-3", "backlog 1")],
        },
    )
    # Use the load-bearing default prompt body so the assertion on
    # "proposal slots this cycle" / "at most N" / no hardcoded 3 lands
    # against the real prompt — the override would mask a regression.
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    if override.exists():
        override.unlink()
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, "ideation should have fired (workable=3 < threshold=5)"
    prompt = calls[0]["prompt"]

    # (a) The snapshot block carries the exact slot-count line. Anchor
    # the search inside the `## Current state` block (the snapshot ends
    # at `## Control job:`) so a future re-export of the same line
    # somewhere else doesn't accidentally satisfy this assertion.
    snapshot_start = prompt.find(
        "## Current state (rendered just before this prompt was sent)"
    )
    snapshot_end = prompt.find("## Control job:", snapshot_start)
    assert snapshot_start != -1 and snapshot_end != -1
    snapshot = prompt[snapshot_start:snapshot_end]
    assert "- proposal slots this cycle: 2" in snapshot, (
        f"expected slot-count line in snapshot block; snapshot:\n{snapshot}"
    )

    # (b) The prompt body references the snapshot value, not a hardcoded
    # number. Both pieces of evidence must hold:
    #   - The "propose at most N" / "proposal slots this cycle" framing
    #     is present somewhere in the prompt body.
    #   - The pre-TB-183 hardcoded "fewer than 3 workable" is gone.
    assert "proposal slots this cycle" in prompt, (
        "prompt body must reference the slot-count line by name"
    )
    assert "fewer than 3 workable" not in prompt, (
        "pre-TB-183 hardcoded magic-3 must be gone from the prompt body"
    )


def test_slots_zero_skips_with_event_and_marks_run(tmp_path, monkeypatch):
    """Early-skip path: workable=5 with threshold=5 → slots=0 → SDK NOT
    invoked, `ideation_skipped_no_slots` event lands, `mark_run` advances
    the cooldown so a wedged-at-threshold board doesn't hammer the gate
    every tick.

    Pins all three behaviors the briefing calls out:
      (a) the SDK is NOT invoked (capture spy stays empty);
      (b) an `ideation_skipped_no_slots` event lands in events.jsonl,
          carrying both `queued` and `threshold` so an operator
          inspecting events sees why the skip fired;
      (c) `mark_run` is called so the cooldown clock advances normally.
    """
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Ready": [("TB-1", "r1"), ("TB-2", "r2")],
            "Backlog": [
                ("TB-3", "b1"), ("TB-4", "b2"), ("TB-5", "b3"),
            ],
        },
    )
    # Anchor the cooldown timestamp at a known stale value so the
    # post-call check can prove `mark_run` advanced it.
    save_state(cfg.cron_state_file, {IDEATION_NAME: 0.0})
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    # (a) SDK never invoked.
    assert calls == [], "ideation should have skipped — slots=0"

    # (b) The skip event landed with queue + threshold context.
    evts = events.tail(cfg.events_file, 20)
    skips = [e for e in evts if e["type"] == "ideation_skipped_no_slots"]
    assert len(skips) == 1, (
        f"expected exactly one ideation_skipped_no_slots event; got: "
        f"{[e['type'] for e in evts]}"
    )
    assert skips[0]["queued"] == 5
    assert skips[0]["threshold"] == 5
    # And the historical entry-marker event is absent — this is a skip,
    # not an SDK invocation.
    assert "ideation_empty_board" not in [e["type"] for e in evts]

    # (c) Cooldown advanced. The fixture seeded the timestamp to 0.0;
    # `mark_run` overwrites with `time.time()` which is well into the
    # 21st-century epoch range.
    state = load_state(cfg.cron_state_file)
    assert state[IDEATION_NAME] > time.time() - 5, (
        "_maybe_ideate must call mark_run on the slots=0 skip path so "
        "the cooldown clock advances"
    )


def test_slots_clamp_prevents_negative_count(tmp_path, monkeypatch):
    """No-double-decrement edge case: workable=6 with threshold=5 →
    `max(0, 5 - 6) = 0`, NOT a negative slot count. Same skip-with-event
    behavior as the slots=0-at-threshold case. Pins the clamp.

    This covers the scenario where the board is over-threshold (e.g.
    operator-added tasks beyond the configured budget) — the slot math
    must never inject a negative integer into the prompt or wrap into
    a giant unsigned value.
    """
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={
            "Ready": [("TB-1", "r1"), ("TB-2", "r2"), ("TB-3", "r3")],
            "Backlog": [
                ("TB-4", "b1"), ("TB-5", "b2"), ("TB-6", "b3"),
            ],
        },
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert calls == [], "ideation should have skipped — slots clamp to 0"
    evts = events.tail(cfg.events_file, 20)
    skips = [e for e in evts if e["type"] == "ideation_skipped_no_slots"]
    assert len(skips) == 1
    # The event records the raw queued count (6), not the clamped slot
    # value — operators inspecting events.jsonl can see the over-threshold
    # state directly.
    assert skips[0]["queued"] == 6
    assert skips[0]["threshold"] == 5


def test_slot_count_default_threshold_full_budget(tmp_path, monkeypatch):
    """Backwards-compat: with `AP2_IDEATION_TRIGGER_TASK_COUNT` unset
    (default=3) and workable=0, slots=3 — i.e. the default behavior
    matches today's pre-TB-183 hardcoded prompt instruction ("propose
    new tasks ONLY if Backlog has fewer than 3 workable items" → up
    to 3 proposals when Backlog is empty).

    Pins that bumping the env knob is the ONLY way to change the
    proposal-slot budget — there's no remaining hardcoded magic-3
    that would break parity with TB-160's env-knob default.
    """
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(tmp_path, monkeypatch, sections={})
    # Use the load-bearing default prompt so the slot line lands as it
    # would in production.
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    if override.exists():
        override.unlink()
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, "ideation should have fired (workable=0 < default 3)"
    prompt = calls[0]["prompt"]
    snapshot_start = prompt.find(
        "## Current state (rendered just before this prompt was sent)"
    )
    snapshot_end = prompt.find("## Control job:", snapshot_start)
    snapshot = prompt[snapshot_start:snapshot_end]
    assert "- proposal slots this cycle: 3" in snapshot, (
        f"default-threshold path must inject slot-count=3 (the historical "
        f"hardcoded value); snapshot:\n{snapshot}"
    )


def test_ideation_default_md_has_no_hardcoded_fewer_than_workable(tmp_path):
    """Defense-in-depth grep against the load-bearing default prompt:
    the pre-TB-183 phrase `fewer than <N> workable` must not survive
    anywhere in `ap2/ideation.default.md`. The slot count is now read
    from the snapshot block at the top of the prompt — any `fewer
    than N workable` reading would be a hardcoded magic number that
    the env knob can't reach."""
    import re as _re

    from ap2.ideation import _DEFAULT_PROMPT_PATH

    text = _DEFAULT_PROMPT_PATH.read_text()
    matches = _re.findall(r"fewer than \d+ workable", text)
    assert not matches, (
        f"ap2/ideation.default.md still has hardcoded `fewer than N "
        f"workable` phrases: {matches}. The slot count must flow from "
        f"the `## Current state` snapshot block, not a magic number "
        f"baked into the prompt body."
    )
    # Positive: the prompt body references the slot-count line by name.
    assert "proposal slots this cycle" in text, (
        "ap2/ideation.default.md must reference the `proposal slots "
        "this cycle` snapshot line by name so the agent reads its "
        "per-cycle proposal budget from a single source of truth."
    )


# ---------------------------------------------------------------------------
# TB-174: focus-exhausted self-skip gate. When the prior cycle's
# `ideation_state.md` self-reports `Status: exhausted-needs-operator`
# for every focus item under `## Current focus assessment`, the natural
# ideation path skips the SDK call (emits `ideation_skipped
# reason=focus_exhausted` and advances the cooldown). Forced runs
# (`force_ideate`, TB-159) bypass the gate so the operator can override
# after refreshing goal.md.


def _write_ideation_state_focus(cfg: Config, statuses: list[tuple[str, str]]) -> None:
    """Write a `.cc-autopilot/ideation_state.md` with `## Current focus
    assessment` populated from `statuses` (list of `(title, status)`
    tuples). Used by the gate-behavior tests below."""
    body = ["# Ideation State\n", "\n", "## Current focus assessment\n", "\n"]
    for title, status in statuses:
        body.append(f"- **{title}**\n")
        body.append(f"  - Status: `{status}`\n")
    body.append("\n## Open questions for operator\n\n- placeholder\n")
    state_file = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("".join(body))


def test_maybe_ideate_skips_when_all_focus_exhausted(tmp_path, monkeypatch):
    """All focus items self-report `exhausted-needs-operator` →
    `_maybe_ideate` skips the SDK call, emits `ideation_skipped
    reason=focus_exhausted` with `focus_count=N`, and calls `mark_run`
    so the cooldown clock advances (a daemon tick every 30s doesn't
    keep re-evaluating the gate).

    Pins all four behaviors:
      (a) the SDK is NOT invoked (capture spy stays empty);
      (b) `ideation_skipped` lands in events.jsonl with
          `reason=focus_exhausted` and `focus_count` populated;
      (c) `mark_run` advances the cooldown timestamp;
      (d) the historical entry-marker `ideation_empty_board` is
          ABSENT — this is a skip, not an SDK invocation.
    """
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first")]},
    )
    _write_ideation_state_focus(
        cfg,
        statuses=[
            ("First focus", "exhausted-needs-operator"),
            ("Second focus", "exhausted-needs-operator"),
        ],
    )
    # Anchor the cooldown timestamp at a stale value so the post-call
    # check can prove `mark_run` advanced it.
    save_state(cfg.cron_state_file, {IDEATION_NAME: 0.0})
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    # (a) SDK not invoked.
    assert calls == [], "ideation should have skipped — focus exhausted"

    # (b) Skip event landed with the briefing-pinned shape.
    evts = events.tail(cfg.events_file, 20)
    skips = [e for e in evts if e["type"] == "ideation_skipped"]
    assert len(skips) == 1, (
        f"expected exactly one ideation_skipped event; got: "
        f"{[e['type'] for e in evts]}"
    )
    assert skips[0]["reason"] == "focus_exhausted"
    assert skips[0]["focus_count"] == 2

    # (c) Cooldown advanced.
    state = load_state(cfg.cron_state_file)
    assert state[IDEATION_NAME] > time.time() - 5, (
        "_maybe_ideate must call mark_run on the focus-exhausted skip "
        "path so the cooldown clock advances"
    )

    # (d) The historical entry-marker is absent.
    assert "ideation_empty_board" not in [e["type"] for e in evts]


def test_maybe_ideate_runs_when_any_focus_in_progress(tmp_path, monkeypatch):
    """Mixed focus statuses (one `in-progress`, one
    `exhausted-needs-operator`) does NOT trip the gate — the natural
    path still fires. This is the load-bearing partial-exhaustion
    case: even one focus item with remaining work keeps the cron
    alive."""
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    cfg = _make_project(
        tmp_path,
        monkeypatch,
        sections={"Backlog": [("TB-1", "first")]},
    )
    _write_ideation_state_focus(
        cfg,
        statuses=[
            ("Active focus", "in-progress"),
            ("Done focus", "exhausted-needs-operator"),
        ],
    )
    calls = _stub_run_control_agent(monkeypatch)

    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))

    assert len(calls) == 1, (
        "ideation should have fired — at least one focus item is still "
        "in-progress"
    )
    # And no `ideation_skipped reason=focus_exhausted` event lands —
    # the gate did not trip.
    evts = events.tail(cfg.events_file, 20)
    skips = [
        e for e in evts
        if e["type"] == "ideation_skipped" and e.get("reason") == "focus_exhausted"
    ]
    assert skips == [], (
        f"focus-exhausted skip must NOT fire on a mixed-status board; "
        f"events: {[e['type'] for e in evts]}"
    )


def test_force_ideate_bypasses_focus_exhausted_gate(tmp_path, monkeypatch):
    """`force_ideate` invokes the SDK even when every focus item is
    `exhausted-needs-operator`. The forced path is the operator's
    override — typically used after refreshing goal.md so the fresh
    focus has somewhere to land its first proposals.

    Sanity-checks both halves: (a) `_maybe_ideate` would have skipped
    on this fixture (precondition); (b) `force_ideate` invokes the
    SDK once anyway."""
    cfg = _make_project(tmp_path, monkeypatch, sections={})
    _write_ideation_state_focus(
        cfg,
        statuses=[
            ("Only focus", "exhausted-needs-operator"),
        ],
    )
    save_state(cfg.cron_state_file, {IDEATION_NAME: time.time() - 10000})

    # (a) Precondition: `_maybe_ideate` skips on this fixture.
    natural_calls = _stub_run_control_agent(monkeypatch)
    asyncio.run(_maybe_ideate(cfg, sdk=None, mcp_server=None))
    assert natural_calls == [], (
        "_maybe_ideate sanity check — focus-exhausted gate must skip "
        "before we test the forced override"
    )

    # (b) `force_ideate` runs the helper anyway — the forced path
    # bypasses the focus-exhausted gate.
    asyncio.run(force_ideate(cfg, sdk=None, mcp_server=None))
    assert len(natural_calls) == 1, (
        "force_ideate must invoke the control agent unconditionally — "
        "the focus-exhausted gate is bypassed on the forced path"
    )
