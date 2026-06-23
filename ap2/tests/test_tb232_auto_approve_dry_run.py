"""TB-232: behavioral pinning for the monitor-only auto-approve dry-run
mode (`AP2_AUTO_APPROVE_DRY_RUN=1`).

Monitor-only on-ramp for the `AP2_AUTO_APPROVE` master switch shipped
in TB-223. The dry-run knob closes the binary-cliff on-ramp gap (zero
`auto_approved` events landed in events.jsonl in the day after TB-223
shipped — the operator's cost of trying the feature was "trust the
gating from minute one" with no prior observation). With both knobs
on:

  - The auto-approve gate chain (tags + freeze-threshold + token caps)
    still evaluates exactly as it would in real mode.
  - The WRITE step changes: instead of stripping `@blocked:review` and
    emitting `auto_approved`, the daemon emits a `would_auto_approve`
    audit event with `dry_run=True` AND leaves the codespan intact so
    the task still requires `ap2 approve`.
  - The operator observes the `would_auto_approve` event stream + the
    `would_auto_approve_count_24h` counter on `ap2 status` for ≥24h,
    then unsets the dry-run knob to engage real dispatch.

Five behavioral pinning cases (briefing's `## Verification` enumerates
the contract):

  (1) `test_would_auto_approve_event_fires_when_dry_run_set` —
      end-to-end through `do_board_edit`: with both knobs on, the
      `would_auto_approve` event fires with `dry_run=True`, no
      `auto_approved` event fires, the task line still carries
      `@blocked:review`.
  (2) `test_blocked_review_codespan_preserved_in_dry_run_mode` —
      explicit board re-parse pin on the codespan-preservation
      semantics so a future regression that strips review even in
      dry-run mode trips here with a precise diff.
  (3) `test_real_auto_approve_unaffected_when_dry_run_unset` —
      with `AP2_AUTO_APPROVE=1` only, the existing TB-223 behavior
      holds: review stripped + `auto_approved` event emitted. Pins
      the no-regression guarantee on the non-dry-run path.
  (4) `test_dry_run_flag_in_collect_auto_approve_state` — the
      `_is_auto_approve_dry_run` helper surfaces through the public
      `collect_auto_approve_state` dict as `dry_run_enabled=True`.
  (5) `test_would_auto_approve_counter_in_collect_state` — two
      seeded `would_auto_approve` events surface as
      `would_auto_approve_count_24h=2` in the public dict.

The test shape mirrors `test_tb223_auto_approve.py` (real
`do_board_edit` seam for the row-composition pin) and
`test_tb227_automation_status.py` (events.jsonl-tail aggregator pin).
A future refactor that flips the dry-run check direction, drops the
`dry_run=True` discriminator field, or wires the dry-run knob to a
different gate-site (e.g. accidentally moves the strip into the
daemon's auto-promote step instead of the proposal-time gate) trips
a focused subset of these tests with a diff-shaped error.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import automation_status, events, tools
from ap2.board import Board
from ap2.components.auto_approve import run_auto_approve_pass
from ap2.config import Config
from ap2.init import init_project


# Minimal goal.md whose `## Current focus` heading + `## Done when`
# bullets expose anchors the briefing structural validator can match
# against. Mirrors `_GOAL_MD` in `test_tb223_auto_approve.py`.
_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


# A briefing whose `## Goal` body cites the `## Current focus` heading
# verbatim AND carries a TB-164-shaped `Why now:` rationale so the
# `_validate_briefing_structure` gate passes cleanly.
_BRIEFING = (
    "# A dry-run test briefing\n\n"
    "## Goal\n\n"
    "Closes the end-to-end automation gap that the current focus "
    "names (cites the `## Current focus: end-to-end automation` "
    "heading).\n\n"
    "Why now: closes the binary-cliff on-ramp gap — without this "
    "operators face a trust-from-minute-one moment that blocks "
    "walk-away adoption under typical proposal load.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout and a real
    goal.md so the briefing-structural gate has anchors to match.
    `init_project` seeds the placeholder goal.md; we overwrite it
    with `_GOAL_MD` so the `Current focus` heading matches the
    briefing body's citation."""
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


# ===========================================================================
# (1) `would_auto_approve` event fires when dry-run set; review preserved.
# ===========================================================================


def test_would_auto_approve_event_fires_when_dry_run_set(
    cfg: Config, monkeypatch,
):
    """Default-on (TB-430) with `AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN=1`
    set, an ideation-shaped `add_backlog` call (a) emits a
    `would_auto_approve` event with `dry_run=True`, (b) does NOT emit
    an `auto_approved` event, (c) leaves the row's `@blocked:review`
    codespan intact (operator-manual `ap2 approve` still required)."""
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", raising=False)
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "1")
    # Don't set AP2_AUTO_APPROVE_GATE_TAGS — default is
    # `#breaking-change,#high-risk` which the `#autopilot` tag below
    # doesn't intersect, so the tags gate passes cleanly.
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "ideation proposes a feature in dry-run",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    # TB-383: `board_edit` is policy-free — run the loop pass (the
    # daemon's PRE_DISPATCH step) to drive the dry-run evaluation.
    run_auto_approve_pass(cfg)

    # (a) `would_auto_approve` event fires with the right shape.
    evts = events.tail(cfg.events_file, 50)
    would_evts = [e for e in evts if e.get("type") == "would_auto_approve"]
    assert len(would_evts) == 1, (
        f"expected exactly one `would_auto_approve` event; got: {evts}"
    )
    assert would_evts[0]["task"] == tb_id
    # TB-430: knob = suppress-key (`disabled`) raw value, "" default-on.
    assert would_evts[0]["knob"] == ""
    assert would_evts[0]["dry_run"] is True, (
        f"`dry_run=True` discriminator field required so downstream "
        f"consumers can distinguish simulated decisions from real "
        f"ones; got: {would_evts[0]!r}"
    )

    # (b) No `auto_approved` event fires — that's the whole point of
    #     dry-run mode.
    auto_evts = [e for e in evts if e.get("type") == "auto_approved"]
    assert auto_evts == [], (
        f"dry-run mode must NOT emit `auto_approved` events; got: "
        f"{auto_evts!r}"
    )

    # (c) The row still carries `@blocked:review`.
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"dry-run mode must preserve `@blocked:review` so the task "
        f"still requires operator `ap2 approve`; got: {line!r}"
    )


# ===========================================================================
# (2) Explicit board-reparse pin on codespan preservation.
# ===========================================================================


def test_blocked_review_codespan_preserved_in_dry_run_mode(
    cfg: Config, monkeypatch,
):
    """Defensive pin that re-parses TASKS.md and asserts the
    `@blocked:review` codespan survives the dry-run round-trip. A
    future regression that accidentally strips review in dry-run
    mode (e.g. by reordering the dry-run check inside the gate or
    flipping its truthiness sense) trips here with a precise
    diff-shaped error.

    TB-427: arm via the sectioned `AP2_COMPONENTS_AUTO_APPROVE_*`
    knobs (matching the rest of this file's cases). The flat
    `AP2_AUTO_APPROVE` / `AP2_AUTO_APPROVE_DRY_RUN` names this case used
    pre-TB-427 are config-tunables outside `ENV_PERMITTED_KEYS`, so the
    config-aware gate ignores them — the case had been passing only
    because auto-approve never actually armed (disabled, not dry-run),
    which masked the codespan-preservation behavior it claims to pin.
    With enablement now resolving from the sectioned knob it genuinely
    arms, dry-runs, and preserves the codespan."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "true")
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "dry-run preserves codespan",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    tb_id = _unwrap(res)["task_id"]

    # TB-383: drive the loop pass; dry-run must preserve the codespan.
    run_auto_approve_pass(cfg)

    # Re-read TASKS.md from disk to catch a regression where the
    # in-memory row reads correctly but the persisted line dropped
    # the codespan.
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"`@blocked:review` codespan must survive a board re-parse in "
        f"dry-run mode; got: {line!r}"
    )
    # Stronger pin: the task should also be in Backlog (not Ready),
    # because the @blocked:review codespan blocks the next-dispatchable
    # promotion path.
    assert section == "Backlog", (
        f"dry-run task should stay in Backlog (blocked by `review`); "
        f"got section={section}"
    )


# ===========================================================================
# (3) No-regression on the non-dry-run path.
# ===========================================================================


def test_real_auto_approve_unaffected_when_dry_run_unset(
    cfg: Config, monkeypatch,
):
    """Default-on (TB-430) with dry-run unset, the existing TB-223
    behavior holds: review token stripped + `auto_approved` event
    emitted + no `would_auto_approve` event. Pins the no-regression
    guarantee on the non-dry-run path — confirms the dry-run check sits
    behind a real boolean, not a flag that always suppresses the real
    path."""
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "real auto-approve no-regression",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    tb_id = _unwrap(res)["task_id"]

    # TB-383: loop pass performs the real strip (dry-run unset).
    run_auto_approve_pass(cfg)

    # Row's `@blocked:review` is stripped (TB-223 behavior).
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    line = board.sections[loc[0]][loc[1]]
    assert "@blocked:review" not in line, (
        f"non-dry-run default-on auto-approve must strip `@blocked:review` "
        f"(TB-223 behavior); got: {line!r}"
    )

    # `auto_approved` event fires (TB-223 audit-trail behavior).
    evts = events.tail(cfg.events_file, 50)
    auto_evts = [e for e in evts if e.get("type") == "auto_approved"]
    assert len(auto_evts) == 1, evts
    assert auto_evts[0]["task"] == tb_id
    # TB-430: knob = suppress-key (`disabled`) raw value, "" default-on.
    assert auto_evts[0]["knob"] == ""

    # And `would_auto_approve` does NOT fire — the dry-run path is
    # mutually exclusive with the real path on a given proposal.
    would_evts = [e for e in evts if e.get("type") == "would_auto_approve"]
    assert would_evts == [], (
        f"non-dry-run path must NOT emit `would_auto_approve`; got: "
        f"{would_evts!r}"
    )


def test_falsy_dry_run_values_treated_as_unset(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_DRY_RUN` non-truthy values (`"0"`, `"false"`,
    empty string) fall back to the real-auto-approve path. Pins the
    permissive-parse boundary: only `"1"`/`"true"`/`"yes"` engage
    dry-run, matching the rest of ap2's boolean env-knob convention.
    A future refactor that flipped to "any non-empty value =
    dry-run" would trip here."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "0")
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "dry-run falsy is unset",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    tb_id = _unwrap(res)["task_id"]

    # TB-383: loop pass strips (falsy dry-run → real path).
    run_auto_approve_pass(cfg)

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    line = board.sections[loc[0]][loc[1]]
    assert "@blocked:review" not in line, (
        f"falsy DRY_RUN value (`0`) must fall back to real "
        f"auto-approve and strip review; got: {line!r}"
    )
    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"], evts
    assert [e for e in evts if e.get("type") == "would_auto_approve"] == []


# ===========================================================================
# (4) `dry_run_enabled` surfaces in collect_auto_approve_state.
# ===========================================================================


def test_dry_run_flag_in_collect_auto_approve_state(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_DRY_RUN=1` → the aggregator's public dict
    has `dry_run_enabled=True`. Operator-facing CLI / web / JSON
    surfaces consume this key to render a "dry-run" badge so the
    operator can confirm at a glance the loop is in monitor mode."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "1")

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["dry_run_enabled"] is True

    # Unset → False (default).
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", raising=False)
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["dry_run_enabled"] is False


# ===========================================================================
# (5) `would_auto_approve_count_24h` counter aggregation.
# ===========================================================================


def test_would_auto_approve_counter_in_collect_state(cfg: Config, monkeypatch):
    """Two seeded `would_auto_approve` events surface as a 24h
    counter value of 2 in the aggregator's public dict — parallel to
    the existing `auto_approved_count_24h` counter (TB-227). Pins
    the aggregator's tail-scan symmetry across both event streams."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-500", knob="1", dry_run=True,
    )
    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-501", knob="1", dry_run=True,
    )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["would_auto_approve_count_24h"] == 2, state

    # Sanity: the real-mode counter is untouched (no `auto_approved`
    # events were seeded).
    assert state["auto_approved_count_24h"] == 0


# ===========================================================================
# Direct helper pin: `_is_auto_approve_dry_run` permissive-parse boundary.
# ===========================================================================


def test_is_auto_approve_dry_run_helper_directly(monkeypatch):
    """Direct unit pin on the env-knob parser so a future refactor
    that changes the truthy-set surfaces clearly here instead of
    cascading through the `do_board_edit` integration. Matches the
    shape of `test_should_auto_approve_helper_directly` in
    `test_tb223_auto_approve.py`."""
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    assert automation_status._is_auto_approve_dry_run() is False

    for truthy in ("1", "true", "yes"):
        monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", truthy)
        assert automation_status._is_auto_approve_dry_run() is True, truthy

    for falsy in ("0", "false", "no", "", " "):
        monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", falsy)
        assert automation_status._is_auto_approve_dry_run() is False, falsy
