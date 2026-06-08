"""TB-223: behavioral pinning for the opt-in `AP2_AUTO_APPROVE` mode.

`AP2_AUTO_APPROVE` is the master switch on the upcoming
**Current focus: end-to-end automation** axis 1 (manual-approval
bottleneck). The trio of knobs forms a layered safety model:

  - `AP2_AUTO_APPROVE` (master switch, default unset) — when truthy,
    ideation-authored `add_backlog` rows omit the `@blocked:review`
    codespan so the daemon's next-tick auto-promote dispatches the
    task without operator approval.
  - `AP2_AUTO_APPROVE_GATE_TAGS` (default `#breaking-change,#high-risk`)
    — per-shape opt-out. A proposed task carrying any gate-tag retains
    `@blocked:review` even in auto-approve mode.
  - `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` (default 3) — systemic-regression
    circuit-breaker. N consecutive `task_complete` failures landing in
    `retry_exhausted` halts auto-promote of auto-approved tasks until
    the operator emits `ap2 ack auto_approve_unfreeze`.

Five behavioral pinning cases (briefing's `## Verification` enumerates
the contract):

  (a) Unset knob → `@blocked:review` preserved on the proposed row.
  (b) Set knob → `@blocked:review` stripped on the proposed row AND
      the audit-trail `auto_approved` event fires.
  (c) Gate-tag-matching task → `@blocked:review` retained even when
      the master switch is on.
  (d) Cumulative-regression threshold N consecutive failures →
      `_auto_approve_paused` returns True; the `_tick` auto-promote
      step emits `auto_approve_paused` and skips moving the
      auto-approved Backlog task to Ready.
  (e) Operator ack via `ap2 ack auto_approve_unfreeze` → resets the
      failure counter; `_auto_approve_paused` returns False on the
      next tick; the gated task is promoted.

The test shape mirrors `test_ideation_proposals.py` (real `do_board_edit`
seam for the row-composition pin) and `test_tb211_event_types.py`
(real `_tick` end-to-end with stubbed internals for the daemon pin).
A future refactor that flips the env-knob parse default, drops the
gate-tag intersection, breaks the `auto_approved` event payload, or
softens the freeze-threshold semantics trips a focused subset of
these tests with a precise diff-shaped error.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import daemon, events, ideation, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# Minimal goal.md whose `## Current focus` heading + `## Done when` bullets
# expose anchors the briefing validator (`_validate_briefing_structure`
# step 4 / 5) can match against. Mirrors `_GOAL_MD` in
# `test_ideation_proposals.py` so the structural gate sees real anchors.
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
# verbatim AND carries a TB-164-shaped `Why now:` rationale, so the
# `_validate_briefing_structure` gate passes cleanly. Used by every
# `add_backlog` call below.
_BRIEFING = (
    "# An auto-approve test briefing\n\n"
    "## Goal\n\n"
    "Closes the end-to-end automation gap that the current focus "
    "names (cites the `## Current focus: end-to-end automation` "
    "heading).\n\n"
    "Why now: closes the manual-approval bottleneck — without this "
    "the walk-away promise breaks under typical proposal load.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout and a real goal.md
    so the briefing-structural gate has anchors to match. `init_project`
    seeds the placeholder goal.md; we overwrite it with `_GOAL_MD`."""
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _seed_completions(
    cfg: Config, *, task_ids: list[str], status: str,
    end_in_retry_exhausted: bool = True,
) -> None:
    """Append a sequence of `task_complete` events (one per id) to
    `events.jsonl`, optionally followed by a `retry_exhausted` event
    for the last task. Used by the circuit-breaker tests to seed the
    failure window."""
    for tid in task_ids:
        events.append(
            cfg.events_file, "task_complete",
            task=tid, status=status, commit="", summary="seeded",
        )
    if end_in_retry_exhausted and task_ids:
        events.append(
            cfg.events_file, "retry_exhausted",
            task=task_ids[-1], attempts=3, last_status=status,
        )


# ===========================================================================
# (a) Unset knob → @blocked:review preserved on the proposed row.
# ===========================================================================


def test_unset_knob_preserves_blocked_review_codespan(cfg: Config, monkeypatch):
    """Default behavior (AP2_AUTO_APPROVE unset): an ideation-shaped
    `add_backlog` call with `blocked_on="review"` produces a TASKS.md
    row carrying the `@blocked:review` codespan. No `auto_approved`
    event fires. Pin against a refactor that silently flips the
    default toward auto-approve."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "ideation proposes a feature",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" in line, (
        f"expected `@blocked:review` codespan on the Backlog row; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "AP2_AUTO_APPROVE unset must NOT emit auto_approved events"
    )


# ===========================================================================
# (b) Set knob → @blocked:review stripped; auto_approved event fires.
# ===========================================================================


def test_set_knob_strips_blocked_review_and_emits_audit_event(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE=1` → the proposed row lands in Backlog WITHOUT
    the `@blocked:review` codespan, and an `auto_approved` event fires
    with `task=TB-N` and `knob=` capturing the env value at proposal
    time. The next-tick auto-promote will pick the task up immediately
    because no review gate remains."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "auto-approved feature",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    assert loc is not None
    section, idx = loc
    line = board.sections[section][idx]
    assert "@blocked:review" not in line, (
        "AP2_AUTO_APPROVE=1 must strip `@blocked:review` from the row; "
        f"got: {line!r}"
    )
    # Stripping `review` from a `blocked_on` that contained only that
    # token should leave no `@blocked:` codespan at all (`_approve_review_token`-shape
    # behavior — clean line).
    assert "@blocked:" not in line, (
        "review-only blocker stripped → no leftover empty `@blocked:` codespan; "
        f"got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    auto_evts = [e for e in evts if e.get("type") == "auto_approved"]
    assert len(auto_evts) == 1, evts
    assert auto_evts[0]["task"] == tb_id
    assert auto_evts[0]["knob"] == "1"


def test_set_knob_with_mixed_blockers_only_strips_review(
    cfg: Config, monkeypatch,
):
    """When `blocked_on="review,TB-5"`, AP2_AUTO_APPROVE strips ONLY
    the `review` token — the `TB-5` dependency stays on the row.
    Pins the surgical-strip behavior so a future refactor that
    no-op'd the whole codespan trips here."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "true")

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "auto-approved with sibling dep",
            "blocked_on": "review,TB-5",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    line = board.sections[loc[0]][loc[1]]
    assert "review" not in line.lower().split("@blocked:", 1)[-1].split("`")[0], (
        f"`review` should be stripped from @blocked: csv; got: {line!r}"
    )
    assert "TB-5" in line, (
        f"sibling TB-5 dep must survive the strip; got: {line!r}"
    )


# ===========================================================================
# (c) Gate-tag-matching task → @blocked:review retained even with knob ON.
# ===========================================================================


def test_gate_tag_match_retains_blocked_review_even_when_knob_on(
    cfg: Config, monkeypatch,
):
    """With `AP2_AUTO_APPROVE=1` AND the proposed task carrying
    `#breaking-change` (a default gate-tag), the row retains its
    `@blocked:review` codespan. Operator's escape hatch for elevated-
    risk categories still works under auto-approve."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "yes")
    # Don't set AP2_AUTO_APPROVE_GATE_TAGS — default is
    # `#breaking-change,#high-risk` which the helper picks up.
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)

    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "risky migration",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot", "#breaking-change"],
        },
    )
    body = _unwrap(res)
    tb_id = body["task_id"]

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_id)
    line = board.sections[loc[0]][loc[1]]
    assert "@blocked:review" in line, (
        "gate-tag `#breaking-change` must preserve `@blocked:review` "
        f"even when AP2_AUTO_APPROVE is on; got: {line!r}"
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e.get("type") == "auto_approved"] == [], (
        "gate-tag match must NOT emit auto_approved (the row still "
        "carries `@blocked:review`)"
    )


def test_custom_gate_tag_list_overrides_default(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_GATE_TAGS` overrides the default set. With
    `#schema-migration` as the custom gate-tag, a task carrying
    `#breaking-change` (NOT in the custom list) gets auto-approved,
    and a task carrying `#schema-migration` retains review."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_GATE_TAGS", "#schema-migration")

    # Task carrying old-default but NOT in custom list → auto-approved.
    res1 = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "non-schema migration",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#breaking-change"],
        },
    )
    tb1 = _unwrap(res1)["task_id"]

    # Task carrying the custom gate-tag → review retained.
    res2 = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "actual schema migration",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#schema-migration"],
        },
    )
    tb2 = _unwrap(res2)["task_id"]

    board = Board.load(cfg.tasks_file)
    line1 = board.sections[board.find(tb1)[0]][board.find(tb1)[1]]
    line2 = board.sections[board.find(tb2)[0]][board.find(tb2)[1]]
    assert "@blocked:review" not in line1, line1
    assert "@blocked:review" in line2, line2


def test_should_auto_approve_helper_directly(monkeypatch):
    """Direct unit pin on `ideation.should_auto_approve(tags)` so a
    refactor that changes the helper signature surfaces clearly
    instead of cascading through the `do_board_edit` integration."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    assert ideation.should_auto_approve(["#anything"]) is False
    assert ideation.should_auto_approve(None) is False

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)
    # Default gate-tags exclude #autopilot → auto-approve.
    assert ideation.should_auto_approve(["#autopilot"]) is True
    # Default gate-tags include #breaking-change → retain review.
    assert ideation.should_auto_approve(["#breaking-change"]) is False
    # No tags at all → auto-approve (operator opted in, no gate hit).
    assert ideation.should_auto_approve([]) is True
    assert ideation.should_auto_approve(None) is True
    # Bare tag without `#` prefix still matches the gate.
    assert ideation.should_auto_approve(["breaking-change"]) is False


# ===========================================================================
# (d) Cumulative-regression threshold — _auto_approve_paused gates the
#     auto-promote step in `_tick`.
# ===========================================================================


def test_freeze_threshold_default_is_three(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_FREEZE_THRESHOLD` unset → defaults to 3
    (`ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT`). Direct pin
    against the parser default flipping silently.

    TB-326 (axis-5): the helper now takes a `cfg` argument and routes
    the env lookup through `Config.get_component_value`'s reverse-
    `FLAT_TO_SECTIONED` fallback. The flat env name still wins via the
    back-compat shim, so this parser pin still exercises the env
    parser shape end-to-end (default-on-empty, int-cast, default-on-
    error).
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)
    assert daemon._auto_approve_freeze_threshold(cfg) == 3
    assert ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT == 3

    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "5")
    assert daemon._auto_approve_freeze_threshold(cfg) == 5

    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "not-a-number")
    assert daemon._auto_approve_freeze_threshold(cfg) == 3


def test_auto_approve_paused_fires_after_threshold_failures(
    cfg: Config, monkeypatch,
):
    """When N=3 consecutive `task_complete` events have status in the
    failure set AND end in `retry_exhausted`, `_auto_approve_paused`
    returns True. With fewer than N failures, returns False."""
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "3")

    # Fewer than threshold → not paused.
    _seed_completions(
        cfg, task_ids=["TB-100", "TB-101"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )
    assert daemon._auto_approve_paused(cfg) is False

    # Add the third failure → paused.
    _seed_completions(
        cfg, task_ids=["TB-102"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )
    assert daemon._auto_approve_paused(cfg) is True


def test_auto_approve_paused_ignores_successes(cfg: Config, monkeypatch):
    """A successful `task_complete` interleaved in the recent window
    breaks the consecutive-failure chain — `_auto_approve_paused`
    returns False even if there are >= N total failures."""
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "3")

    # 2 fails + 1 success + 2 fails: only the last 2 fails are
    # "consecutive" in the window (the success splits the streak).
    _seed_completions(
        cfg, task_ids=["TB-100", "TB-101"],
        status="verification_failed",
        end_in_retry_exhausted=False,
    )
    events.append(cfg.events_file, "task_complete",
                  task="TB-102", status="complete", commit="abc",
                  summary="shipped")
    _seed_completions(
        cfg, task_ids=["TB-103", "TB-104"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )

    assert daemon._auto_approve_paused(cfg) is False


def test_auto_approve_paused_disabled_by_zero_threshold(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE_FREEZE_THRESHOLD=0` → the circuit-breaker is
    disabled outright. `_auto_approve_paused` short-circuits to False
    regardless of failure window. Operator's explicit-trust escape
    hatch."""
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    _seed_completions(
        cfg, task_ids=["TB-100", "TB-101", "TB-102", "TB-103"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )
    assert daemon._auto_approve_paused(cfg) is False


def _stub_tick_quiet(monkeypatch) -> None:
    """Stub every `_tick` internal except the auto-promote step so we
    can exercise the freeze path end-to-end. Mirrors the helper of the
    same name in `test_tb211_event_types.py`."""
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda cfg: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    async def _noop_sweep(cfg, sdk):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_sweep)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    monkeypatch.setattr(ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(ideation, "force_ideate", _noop_async)
    # TB-381: the cron stage is now the `Phase.CRON_DISPATCH` walk into the
    # cron scheduler component; neutralize it by stubbing the component's
    # `load_jobs` (string target avoids importing the impl module here).
    monkeypatch.setattr("ap2.components.cron.impl.load_jobs", lambda path: [])

    # Critical: stub `run_task` so the auto-promote → dispatch chain
    # doesn't actually invoke a task agent.
    async def _noop_run_task(cfg, sdk, mcp_server, task):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "run_task", _noop_run_task)


class _NoopSDK:
    """SDK stub matching `test_tb211_event_types._NoopSDK`."""

    def __init__(self) -> None:
        self.called = False

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


def test_tick_halts_auto_promote_when_freeze_active(
    cfg: Config, monkeypatch,
):
    """End-to-end pin: with the freeze threshold tripped AND an
    auto-approved task waiting in Backlog, `_tick` emits
    `auto_approve_paused` and does NOT promote the task to Ready.
    A subsequent `ap2 ack auto_approve_unfreeze` is exercised in the
    next test (case e)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "3")

    # 1. Land an auto-approved Backlog task (the do_board_edit path
    #    we pinned in case (b); this also emits the `auto_approved`
    #    event the `_was_auto_approved` lookup needs).
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "should be paused",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    tb_paused = _unwrap(res)["task_id"]

    # 2. Seed three consecutive retry_exhausted-ended failures so the
    #    circuit-breaker trips.
    _seed_completions(
        cfg, task_ids=["TB-901", "TB-902", "TB-903"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )
    assert daemon._auto_approve_paused(cfg) is True
    assert daemon._was_auto_approved(cfg, tb_paused) is True

    # 3. Drive `_tick`. The auto-promote step should observe the
    #    freeze, emit `auto_approve_paused`, and skip the
    #    move_to_ready.
    _stub_tick_quiet(monkeypatch)
    sdk = _NoopSDK()
    asyncio.run(daemon._tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 200)
    paused = [e for e in evts if e.get("type") == "auto_approve_paused"]
    assert len(paused) >= 1, evts
    assert paused[-1]["task"] == tb_paused
    assert paused[-1]["threshold"] == 3

    # The task must NOT have moved to Ready.
    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_paused)
    assert loc is not None and loc[0] == "Backlog", (
        f"auto-approved task must remain in Backlog while freeze active; "
        f"got section={loc[0] if loc else 'missing'}"
    )

    # And `backlog_auto_promoted` did NOT fire for it.
    promoted = [
        e for e in evts
        if e.get("type") == "backlog_auto_promoted" and e.get("task") == tb_paused
    ]
    assert promoted == [], (
        "the auto-approved task must not be promoted while freeze active"
    )


# ===========================================================================
# (e) Operator ack via `ap2 ack auto_approve_unfreeze` resumes auto-promote.
# ===========================================================================


def test_operator_ack_unfreeze_resumes_auto_promote(cfg: Config, monkeypatch):
    """After three consecutive `retry_exhausted`-ended failures (freeze
    active) AND an `operator_ack` whose `note` carries the
    `auto_approve_unfreeze` token, `_auto_approve_paused` returns
    False. Driving `_tick` then promotes the previously-paused
    auto-approved Backlog task to Ready."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "3")

    # Land the auto-approved task.
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "should be promoted after unfreeze",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    tb_paused = _unwrap(res)["task_id"]

    # Trip the circuit-breaker.
    _seed_completions(
        cfg, task_ids=["TB-901", "TB-902", "TB-903"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )
    assert daemon._auto_approve_paused(cfg) is True

    # Operator emits the unfreeze ack — uses the existing
    # `operator_ack` event (TB-106 / TB-201). The drain-side helper
    # `_apply_operator_ack` does the write + emit; we call it
    # directly here to avoid threading the full queue-drain plumbing
    # into the test (the queue → drain → emit chain is pinned
    # separately by `test_operator_queue.py`).
    tools._apply_operator_ack(
        cfg, {"note": "auto_approve_unfreeze: cleared, root cause was env"},
    )
    assert daemon._auto_approve_paused(cfg) is False, (
        "operator_ack with `auto_approve_unfreeze` token must reset "
        "the failure counter"
    )

    # Drive `_tick`. The auto-promote step should now move the task
    # to Ready.
    _stub_tick_quiet(monkeypatch)
    sdk = _NoopSDK()
    asyncio.run(daemon._tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_paused)
    assert loc is not None
    assert loc[0] == "Ready", (
        f"after unfreeze, the auto-approved task must promote to Ready; "
        f"got section={loc[0]}"
    )

    evts = events.tail(cfg.events_file, 200)
    promoted = [
        e for e in evts
        if e.get("type") == "backlog_auto_promoted" and e.get("task") == tb_paused
    ]
    assert len(promoted) == 1, evts


def test_operator_approved_task_dispatches_even_when_freeze_active(
    cfg: Config, monkeypatch,
):
    """The freeze pauses ONLY auto-approved tasks. An
    operator-approved task (`ideation_approved` event, not
    `auto_approved`) continues to dispatch normally even while the
    circuit-breaker is active. Pins the "targeted, not blanket"
    behavior from the briefing's Scope (3)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "3")

    # Land a task that is NOT auto-approved (carries a gate-tag so
    # the review codespan stays on the row).
    monkeypatch.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "operator-approved task",
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#breaking-change"],
        },
    )
    tb_op = _unwrap(res)["task_id"]

    # Operator approves via the synchronous approve path (the same
    # plumbing `ap2 approve TB-N` invokes via the operator queue,
    # but we use the direct seam for test cleanliness).
    approve_res = tools.do_board_edit(
        cfg, {"action": "approve", "task_id": tb_op},
    )
    _unwrap(approve_res)
    assert daemon._was_auto_approved(cfg, tb_op) is False, (
        "operator-approved task must NOT be in the auto-approved bucket "
        "for the freeze check"
    )

    # Trip the circuit-breaker.
    _seed_completions(
        cfg, task_ids=["TB-901", "TB-902", "TB-903"],
        status="verification_failed",
        end_in_retry_exhausted=True,
    )
    assert daemon._auto_approve_paused(cfg) is True

    # Drive `_tick`. The operator-approved task should promote because
    # the freeze is targeted at auto-approved tasks only.
    _stub_tick_quiet(monkeypatch)
    sdk = _NoopSDK()
    asyncio.run(daemon._tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    loc = board.find(tb_op)
    assert loc is not None
    assert loc[0] == "Ready", (
        f"operator-approved task must promote even while freeze active; "
        f"got section={loc[0]}"
    )
