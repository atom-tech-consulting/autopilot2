"""TB-297: regression-pin for the opt-in immediate-Mattermost-push
on `attention_raised` emission.

Closes the TB-282 Out-of-scope axis the briefing's L119-122 named:
the post-trip `auto_approve_paused` and pre-trip
`cost_cap_approach` conditions are time-sensitive — waiting for
the next status-report cron defeats the "proactively
surfaced" claim. Operators opt into immediate-push once they've
sampled their detector cadence.

This module pins the briefing's six load-bearing arcs:

  (1) Push opt-out by default — knob unset / falsy →
      `_mm_post` is NOT called, no `attention_pushed` event lands.
  (2) Knob on + destination set → exactly one `_mm_post` call per
      fresh condition, post text is the documented one-line shape
      (`[<project_name>] ⚠ <summary>`).
  (3) Knob on + `AP2_MM_CHANNELS` unset → one
      `attention_push_no_destination` event sticks, subsequent
      fires do NOT re-emit (per-state-file flag mirrors the
      watchdog's `warned_no_destination` pattern).
  (4) Knob on + `_mm_post` raises → `attention_push_error` event
      emitted, the rest of the candidate iteration continues.
  (5) Debounce reuse — a condition that just fired produces an
      `attention_raised` but NO second push within
      `AP2_ATTENTION_DEBOUNCE_S` (push runs only when a fresh
      `attention_raised` appends, which already honors the
      detector debounce).
  (6) Project-name prefix present in the post text — monkeypatch
      `cfg.project_name` and assert the post text reflects the
      override.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path

import pytest

from ap2 import events
from ap2.components.attention import (
    AttentionCondition,
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_TASK_STUCK_THRESHOLD_S,
)
from ap2.board import Board
from ap2.config import (
    Config,
    DEFAULT_ATTENTION_IMMEDIATE_PUSH,
)
from ap2.init import init_project


# ---------------------------------------------------------------------------
# Shared fixtures.


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Clean project scaffold with the TB-297 push knob unset so the
    conservative default is the contract under test. Detector knobs
    cleared too so each arc controls its own threshold/debounce
    semantics explicitly."""
    monkeypatch.delenv("AP2_ATTENTION_IMMEDIATE_PUSH", raising=False)
    monkeypatch.delenv("AP2_TASK_STUCK_THRESHOLD_S", raising=False)
    monkeypatch.delenv("AP2_ATTENTION_DEBOUNCE_S", raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _ts_seconds_ago(now: _dt.datetime, *, seconds_ago: float) -> str:
    when = now - _dt.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


def _seed_active_task(cfg: Config, task_id: str, title: str) -> None:
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id=task_id, title=title)
    board.save()


def _stage_stuck_task(cfg: Config, task_id: str, title: str) -> None:
    """Seed a stuck task whose `task_start` is well past the default
    threshold so `_maybe_emit_attention_events` will fire a fresh
    `attention_raised` on the next call."""
    _seed_active_task(cfg, task_id, title)
    events.append(cfg.events_file, "task_start", task=task_id, title=title)
    past_ts = (
        _dt.datetime.now(_dt.timezone.utc)
        - _dt.timedelta(seconds=DEFAULT_TASK_STUCK_THRESHOLD_S + 60)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_last_event_ts(cfg, past_ts)


def _tail_events_of_type(cfg: Config, type_: str) -> list[dict]:
    tail = events.tail(cfg.events_file, 200)
    return [e for e in tail if e.get("type") == type_]


# ---------------------------------------------------------------------------
# Arc 0: default value + hot-reload listing.


def test_default_attention_immediate_push_is_false():
    """Source-level pin: the conservative-default constant is False.
    Operators must explicitly opt in to immediate push (goal.md
    Non-goals L253-256: "any other operator-in-the-loop relaxation
    are OPT-IN env knobs with conservative defaults")."""
    assert DEFAULT_ATTENTION_IMMEDIATE_PUSH is False


def test_attention_immediate_push_knob_is_hot_reloadable():
    """`AP2_ATTENTION_IMMEDIATE_PUSH` lands in `HOT_RELOADABLE_KNOBS`
    so an operator can flip the knob via `.cc-autopilot/env` without
    a daemon restart — mirrors the sibling attention detector-
    sensitivity knobs."""
    from ap2.env_reload import HOT_RELOADABLE_KNOBS

    assert "AP2_ATTENTION_IMMEDIATE_PUSH" in HOT_RELOADABLE_KNOBS


# ---------------------------------------------------------------------------
# Arc 1: push opt-out by default (knob unset → no post, no event).


def test_push_opt_out_by_default(cfg: Config, monkeypatch):
    """Knob unset (default) → `_mm_post` is NOT called and no
    `attention_pushed` event lands. Pins the goal.md Non-goals
    L253-256 "conservative-default opt-in" contract."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import tools, daemon

    monkeypatch.delenv("AP2_ATTENTION_IMMEDIATE_PUSH", raising=False)
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    posts: list[tuple[str, str]] = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "post-id"

    monkeypatch.setattr(tools, "_mm_post", _fake_mm_post)
    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    _stage_stuck_task(cfg, "TB-1001", "Stuck without push")
    _maybe_emit_attention_events(cfg)

    # `attention_raised` event DID fire (the upstream contract).
    raised = _tail_events_of_type(cfg, "attention_raised")
    assert len(raised) == 1, raised
    # Push did NOT happen.
    assert posts == [], posts
    assert _tail_events_of_type(cfg, "attention_pushed") == []
    assert _tail_events_of_type(cfg, "attention_push_error") == []
    assert _tail_events_of_type(cfg, "attention_push_no_destination") == []


def test_push_opt_out_for_falsy_value(cfg: Config, monkeypatch):
    """Explicit falsy values (`AP2_ATTENTION_IMMEDIATE_PUSH=0` /
    `false` / `no` / `off`) also skip the push — operator typo
    `0` doesn't accidentally enable it."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import tools, daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "0")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    posts: list = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    _stage_stuck_task(cfg, "TB-1002", "Stuck with knob=0")
    _maybe_emit_attention_events(cfg)

    assert posts == []
    assert _tail_events_of_type(cfg, "attention_pushed") == []


# ---------------------------------------------------------------------------
# Arc 2: knob on + destination set → one `_mm_post` per fresh
# condition with the documented one-line shape.


def test_push_on_calls_mm_post_with_documented_shape(cfg: Config, monkeypatch):
    """Knob on + destination set → exactly one `_mm_post` call per
    fresh condition; post text is `[<project_name>] ⚠ <summary>`
    and an `attention_pushed` event lands with the documented
    payload (`attention_type`, `key`, `channel`, `post_id`,
    `summary`)."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    posts: list[tuple[str, str]] = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "synthetic-post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    _stage_stuck_task(cfg, "TB-1003", "Stuck with push on")
    _maybe_emit_attention_events(cfg)

    # Exactly one Mattermost post landed for the one fresh condition.
    assert len(posts) == 1, posts
    channel, text = posts[0]
    assert channel == "test-channel-id"
    # Project-name prefix + glyph + summary shape.
    assert text.startswith(f"[{cfg.project_name}] ⚠ "), text
    assert "TB-1003" in text

    # `attention_pushed` event lands with the documented payload.
    pushed = _tail_events_of_type(cfg, "attention_pushed")
    assert len(pushed) == 1, pushed
    evt = pushed[0]
    assert evt["attention_type"] == "task_stuck"
    assert evt["key"] == "task_stuck:TB-1003"
    assert evt["channel"] == "test-channel-id"
    assert evt["post_id"] == "synthetic-post-id"
    assert "TB-1003" in evt["summary"]


def test_push_text_includes_project_name_prefix(cfg: Config, monkeypatch):
    """The project-name prefix in the post text is sourced from
    `cfg.project_name` at call-time — monkeypatching it after Config
    load changes the post text on the next push. Pins the helper-
    reuse contract from the briefing's Design block (avoid
    duplicating the project-name helper the status-report cron
    already uses)."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    posts: list[tuple[str, str]] = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    # Rewrite the project-name to a sentinel value.
    cfg.project_name = "alpha-renamed-project"

    _stage_stuck_task(cfg, "TB-1004", "Project-rename push")
    _maybe_emit_attention_events(cfg)

    assert len(posts) == 1, posts
    _, text = posts[0]
    assert text.startswith("[alpha-renamed-project] ⚠ "), text


# ---------------------------------------------------------------------------
# Arc 3: knob on + AP2_MM_CHANNELS unset → one
# `attention_push_no_destination` then sticky-suppress.


def test_no_destination_emits_sticky_warning_then_suppresses(
    cfg: Config, monkeypatch,
):
    """Knob on + `AP2_MM_CHANNELS` unset → one
    `attention_push_no_destination` event lands per state-file
    lifetime, subsequent push attempts within the same daemon
    process do NOT re-emit. Mirrors the watchdog's
    `warned_no_destination` pattern.

    Two separate conditions fire across two ticks; only the first
    produces the no-destination audit event. The second condition's
    debounce window blocks a fresh `attention_raised`, but the
    sticky-flag is the load-bearing pin here — even if it fired,
    the second push attempt would still find no-destination and
    the sticky flag would prevent a second audit.
    """
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    posts: list = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    # First stuck task on tick 1.
    _stage_stuck_task(cfg, "TB-1010", "First stuck no-dest")
    _maybe_emit_attention_events(cfg)

    # Second distinct stuck task on tick 2 — distinct (type, key)
    # so debounce does NOT suppress the second `attention_raised`.
    _stage_stuck_task(cfg, "TB-1011", "Second stuck no-dest")
    _maybe_emit_attention_events(cfg)

    # No `_mm_post` should ever have been called.
    assert posts == [], posts

    # Exactly one no-destination audit event should have landed
    # across the two pushes — sticky-flag suppresses the second.
    nodest = _tail_events_of_type(cfg, "attention_push_no_destination")
    assert len(nodest) == 1, nodest
    evt = nodest[0]
    assert evt["reason"] == "AP2_MM_CHANNELS unset"
    # The audit references one of the two attempting conditions
    # (the FIRST — the second pushes against the sticky flag).
    assert evt["attention_type"] == "task_stuck"
    assert evt["key"] == "task_stuck:TB-1010"


def test_no_destination_state_file_persists_flag(cfg: Config, monkeypatch):
    """The sticky-warn flag persists in
    `.cc-autopilot/attention_push_state.json` so the daemon
    restart-cycle keeps the warning suppressed until destination
    returns (and the next successful push resets the flag)."""
    from ap2.daemon import _maybe_emit_attention_events, _attention_push_state_path
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    def _unused_mm_post(*a, **k):
        raise AssertionError("must not call _mm_post without destination")

    monkeypatch.setattr(daemon.tools, "_mm_post", _unused_mm_post)

    _stage_stuck_task(cfg, "TB-1020", "State-file persist test")
    _maybe_emit_attention_events(cfg)

    state_path = _attention_push_state_path(cfg)
    assert state_path.exists(), state_path
    state = _json.loads(state_path.read_text())
    assert state.get("warned_no_destination") is True, state


def test_no_destination_flag_resets_after_successful_push(
    cfg: Config, monkeypatch,
):
    """After the destination returns (operator sets `AP2_MM_CHANNELS`)
    and a push succeeds, the sticky `warned_no_destination` flag
    resets to false so a future env-config gap re-warns. Mirrors
    the watchdog's `state["warned_no_destination"] = False`
    post-success reset."""
    from ap2.daemon import (
        _maybe_emit_attention_events,
        _attention_push_state_path,
        _load_attention_push_state,
        _save_attention_push_state,
    )
    from ap2 import daemon

    # Pre-populate the sticky flag as if a prior no-destination warn
    # had landed.
    _save_attention_push_state(cfg, {"warned_no_destination": True})

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "now-configured-channel")

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        return "post-after-fix"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    _stage_stuck_task(cfg, "TB-1021", "Destination-back-online")
    _maybe_emit_attention_events(cfg)

    state = _load_attention_push_state(cfg)
    assert state.get("warned_no_destination") is False, state


# ---------------------------------------------------------------------------
# Arc 4: knob on + `_mm_post` raises → `attention_push_error` event
# emitted, candidate iteration continues.


def test_mm_post_failure_emits_attention_push_error(cfg: Config, monkeypatch):
    """Knob on + `_mm_post` raises → `attention_push_error` lands
    with the documented payload (`channel`, `attention_type`, `key`,
    `error`). The error is `<ExceptionType>: <message>` shape per
    the watchdog's post-error pattern."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    def _raising_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        raise RuntimeError("synthetic mattermost failure")

    monkeypatch.setattr(daemon.tools, "_mm_post", _raising_mm_post)

    _stage_stuck_task(cfg, "TB-1030", "Push-failure test")
    _maybe_emit_attention_events(cfg)

    errors = _tail_events_of_type(cfg, "attention_push_error")
    assert len(errors) == 1, errors
    evt = errors[0]
    assert evt["channel"] == "test-channel-id"
    assert evt["attention_type"] == "task_stuck"
    assert evt["key"] == "task_stuck:TB-1030"
    assert "RuntimeError" in evt["error"], evt["error"]
    assert "synthetic mattermost failure" in evt["error"], evt["error"]
    # No `attention_pushed` event on the failure path.
    assert _tail_events_of_type(cfg, "attention_pushed") == []


def test_mm_post_failure_does_not_abort_iteration(cfg: Config, monkeypatch):
    """A `_mm_post` failure on the first candidate must not block
    the second candidate's push attempt — the inner per-candidate
    `try/except` keeps the loop alive. Pin the iteration-continues
    contract."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    calls: list[str] = []

    def _selective_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        calls.append(text)
        if "TB-1040" in text:
            raise RuntimeError("first push fails")
        return "second-post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _selective_mm_post)

    _stage_stuck_task(cfg, "TB-1040", "First push (fails)")
    _stage_stuck_task(cfg, "TB-1041", "Second push (succeeds)")
    _maybe_emit_attention_events(cfg)

    # Both pushes were attempted (calls captured for both).
    assert len(calls) == 2, calls

    # First fired error; second fired success.
    errors = _tail_events_of_type(cfg, "attention_push_error")
    pushed = _tail_events_of_type(cfg, "attention_pushed")
    assert len(errors) == 1, errors
    assert errors[0]["key"] == "task_stuck:TB-1040"
    assert len(pushed) == 1, pushed
    assert pushed[0]["key"] == "task_stuck:TB-1041"


# ---------------------------------------------------------------------------
# Arc 5: debounce reuse — a condition that just fired produces no
# second push within `AP2_ATTENTION_DEBOUNCE_S`.


def test_push_debounce_piggybacks_on_attention_raised_debounce(
    cfg: Config, monkeypatch,
):
    """The push runs only AFTER a fresh `attention_raised` appends,
    so the existing `AP2_ATTENTION_DEBOUNCE_S` per-(type, key)
    window structurally suppresses repeat pushes — no new state
    file needed for push-debounce bookkeeping. Pin the structural
    debounce reuse contract from the briefing's Scope item (3).

    Two calls to `_maybe_emit_attention_events` for the same stuck
    task: the second call's `attention_raised` is suppressed by
    the existing TB-282 debounce, and because the push runs only
    on fresh emissions, there is NO second push either."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    posts: list = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    _stage_stuck_task(cfg, "TB-1050", "Debounce-reuse test")
    # Tick 1: fresh fire — one push lands.
    _maybe_emit_attention_events(cfg)
    # Tick 2: same condition still active, but the prior
    # `attention_raised` is well inside the default 6h debounce
    # window — no fresh emit, no fresh push.
    _maybe_emit_attention_events(cfg)

    # Exactly one `attention_raised` and one `attention_pushed`
    # event across both ticks.
    raised = _tail_events_of_type(cfg, "attention_raised")
    pushed = _tail_events_of_type(cfg, "attention_pushed")
    assert len(raised) == 1, raised
    assert len(pushed) == 1, pushed
    # Exactly one Mattermost post call captured.
    assert len(posts) == 1, posts


def test_push_refire_past_debounce_window(cfg: Config, monkeypatch):
    """A condition whose prior `attention_raised` is OUTSIDE the
    debounce window → next call produces a fresh `attention_raised`
    AND a fresh `attention_pushed`. Pin the boundary so a refactor
    that decouples push from the structural debounce surfaces
    here."""
    from ap2.daemon import _maybe_emit_attention_events
    from ap2 import daemon

    monkeypatch.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")

    posts: list = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append((channel, text))
        return "post-id"

    monkeypatch.setattr(daemon.tools, "_mm_post", _fake_mm_post)

    _stage_stuck_task(cfg, "TB-1051", "Refire-past-window test")

    # Stash a prior `attention_raised` event well past the default
    # 6h debounce window (7h ago).
    events.append(
        cfg.events_file, "attention_raised",
        attention_type="task_stuck", key="task_stuck:TB-1051",
        summary="prior", task="TB-1051",
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    prior_ts = (
        now - _dt.timedelta(seconds=DEFAULT_ATTENTION_DEBOUNCE_S + 3600)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_last_event_ts(cfg, prior_ts)

    _maybe_emit_attention_events(cfg)

    # Fresh `attention_raised` past the debounce → fresh push.
    raised = _tail_events_of_type(cfg, "attention_raised")
    pushed = _tail_events_of_type(cfg, "attention_pushed")
    # Two `attention_raised` (the prior + the fresh fire).
    assert len(raised) == 2, raised
    # One fresh push.
    assert len(pushed) == 1, pushed
    assert len(posts) == 1, posts


# ---------------------------------------------------------------------------
# Arc 6: status-report skip-gate listing + event registry coverage.


def test_attention_pushed_in_status_report_interesting_types():
    """`attention_pushed` lands in
    `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` so a fresh push
    un-skips the dedup/idle gate — the next routine status-report
    acknowledges the immediate-push happened. Parallel to TB-282's
    `attention_raised` entry."""
    from ap2.status_report import _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES

    assert (
        "attention_pushed"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    ), _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    # Regression-pin against an edit that overwrote the frozenset
    # instead of extending it — prior allowlist members survive.
    assert (
        "attention_raised"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )
    assert (
        "auto_approve_paused"
        in _STATUS_REPORT_AUTOMATION_INTERESTING_TYPES
    )


def test_push_event_types_documented_in_events_module():
    """Source-level pin: the three TB-297 event types appear in
    `ap2/events.py`'s docstring registry. The docs-drift gate
    enforces every emitted type is mentioned in howto.md; this
    pin asserts the on-module registry-comment surface is also
    populated for readers grepping the event catalog."""
    events_src = (Path(__file__).resolve().parent.parent / "events.py").read_text()
    assert "attention_pushed" in events_src
    assert "attention_push_error" in events_src
    assert "attention_push_no_destination" in events_src


# ---------------------------------------------------------------------------
# Briefing-verification structural pins (mirror the briefing's
# `## Verification` greps in test form so a refactor that violates
# the structural pins surfaces here as a clean test failure).


def test_briefing_verification_greps_match():
    """Mirror the briefing's `## Verification` greps in test form.

    TB-315 (axis 5): `_maybe_push_attention` + its `attention_pushed`
    audit-event emission moved from `ap2/daemon.py` into
    `ap2/components/attention/__init__.py` as part of the subpackage
    migration. The daemon retains a module-level alias
    `_maybe_push_attention = _attention_manifest.hook_points[…]` (so
    the alias name still appears in daemon.py source and the test path
    `from ap2.daemon import _maybe_push_attention` still resolves), but
    the `attention_pushed` literal lives in the subpackage now —
    redirect that grep to the new location.
    """
    repo_root = Path(__file__).resolve().parent.parent
    config_src = (repo_root / "config.py").read_text()
    env_reload_src = (repo_root / "env_reload.py").read_text()
    daemon_src = (repo_root / "daemon.py").read_text()
    attention_src = (
        repo_root / "components" / "attention" / "__init__.py"
    ).read_text()
    events_src = (repo_root / "events.py").read_text()
    status_report_src = (repo_root / "status_report.py").read_text()

    # `grep -Eq "AP2_ATTENTION_IMMEDIATE_PUSH" ap2/config.py`
    assert "AP2_ATTENTION_IMMEDIATE_PUSH" in config_src
    # `grep -q "AP2_ATTENTION_IMMEDIATE_PUSH" ap2/env_reload.py`
    assert "AP2_ATTENTION_IMMEDIATE_PUSH" in env_reload_src
    # `grep -Eq "_maybe_push_attention" ap2/daemon.py` (the alias
    # block still names it; the body lives in the subpackage).
    assert "_maybe_push_attention" in daemon_src
    # `grep -Eq "_maybe_push_attention|attention_pushed"
    # ap2/components/attention/__init__.py` (the relocated home of the
    # helper + the `attention_pushed` audit-event emission).
    assert "_maybe_push_attention" in attention_src
    assert "attention_pushed" in attention_src
    # `grep -q "attention_pushed" ap2/events.py`
    assert "attention_pushed" in events_src
    # `grep -q "attention_pushed" ap2/status_report.py`
    assert "attention_pushed" in status_report_src
