"""TB-227: behavioral pinning for the auto-approve / auto-unfreeze loop
state surface (`ap2 status` text + JSON; web home page Automation card).

The aggregator is `ap2.automation_status.collect_auto_approve_state` —
pure-function, events.jsonl-tail-only. Three test arcs:

  (1) Helper contract: keys present + correct type regardless of
      knob-state; happy / paused-on-each-of-the-four-reasons / 24h
      counter aggregation / window-resume idx scoping / unfreeze idx
      scoping.

  (2) CLI rendering: text-mode emits the `auto-approve:` line when the
      knob is on OR a 24h counter is non-zero; omitted otherwise.
      Paused-mode shows the ack verb. `--json` always carries the
      full `auto_approve` dict.

  (3) Web home: `_render_automation_card` returns "" when knob off +
      all 24h counters zero; returns a card with the `is-paused` class
      and the ack verb when paused; returns `is-healthy` when knob
      on + no halt. Sparkline `<svg>` lands when buckets are non-zero.

A future refactor that breaks the JSON shape, softens the omit-on-empty
rendering rule, or drops the ack verb from the paused-mode CLI / web
output trips a focused subset of these tests with a diff-shaped error.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Per-test cfg over a fresh project root with AP2_* env stripped
    BEFORE `Config.load`.

    TB-332: post-cross-package migration, `automation_status` helpers
    read auto_approve knobs via `cfg.get_component_value(...)`, which
    snapshots `cfg.components_config` at load time from the env-
    override layer (`apply_env_overrides`). The dev shell may export
    `AP2_AUTO_APPROVE=1` (the parent process the test inherits from),
    which would otherwise paint `enabled=True` onto the cfg snapshot
    before a test body's `monkeypatch.delenv("AP2_AUTO_APPROVE")` runs.
    Stripping AP2_* env in the fixture (BEFORE `Config.load`) pins
    each test's cfg to a clean baseline; test bodies that want a knob
    truthy use `monkeypatch.setenv(...)` AFTER the fixture lands and
    the cfg-read path picks up the env via the call-time precedence
    in `Config.get_component_value`. Mirrors the TB-326 / TB-327 /
    TB-328 / TB-330 cluster-pilot fixture shape.
    """
    import os

    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _ts_offset(now: _dt.datetime, *, hours_ago: float) -> str:
    """Render a `ts` string `hours_ago` before `now` in the canonical
    ap2 event format (`%Y-%m-%dT%H:%M:%SZ`, UTC `Z` suffix). Used to
    pin per-event placement in the 24h window."""
    when = now - _dt.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(cfg: Config, type_: str, **fields) -> None:
    """`events.append` always stamps `ts=now()`; tests that need a
    specific `ts` rewrite the line in place. This helper is a thin
    delegator so the rewrite-in-place flow stays consistent."""
    events.append(cfg.events_file, type_, **fields)


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    """Replace the `ts` field on the most recent line in events.jsonl.

    The aggregator's 24h counters read `ts` to bucket events; appending
    via the public `events.append` always stamps `now()`, so tests that
    need an event "in the past" rewrite the line after the append.
    Keeping the rewrite tightly scoped (last line only) prevents
    accidental corruption of older events.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


# ===========================================================================
# (1) Helper contract.
# ===========================================================================


def test_collect_state_shape_when_knob_off_and_no_activity(cfg: Config, monkeypatch):
    """Default state — knob unset, no events.jsonl activity. Every key
    in the contract is present with the documented type. Pin against a
    refactor that drops a key or returns `None` where an int is
    documented."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)

    state = automation_status.collect_auto_approve_state(cfg)
    # Every key in the contract must be present. TB-232 added the
    # `dry_run_enabled` + `would_auto_approve_count_24h` pair as the
    # monitor-only on-ramp surface; the two land in the same dict so
    # CLI / web / JSON renderings consume one source of truth.
    # TB-238 added the parallel `auto_unfreeze_dry_run_enabled` +
    # `would_auto_unfreeze_count_24h` pair on the axis-2 side so the
    # status-report digest can render both dry-runs as one block.
    # TB-243 added `validator_judge_fail_count_24h` +
    # `validator_judge_timeout_count_24h` to surface the TB-235
    # dependency-coherence judge's fail-open audit counts on
    # `ap2 status` + the web home Automation card.
    expected_keys = {
        "auto_approve_enabled", "auto_approve_paused",
        "consecutive_freezes", "freeze_threshold",
        "per_task_token_cap", "window_token_cap",
        "window_tokens_used",
        "auto_approved_count_24h",
        "auto_unfreeze_applied_count_24h",
        "auto_unfreeze_skipped_count_24h",
        "pause_reason",
        "dry_run_enabled",
        "would_auto_approve_count_24h",
        "auto_unfreeze_dry_run_enabled",
        "would_auto_unfreeze_count_24h",
        "validator_judge_fail_count_24h",
        "validator_judge_timeout_count_24h",
    }
    assert set(state.keys()) == expected_keys

    # Types per the contract.
    assert state["auto_approve_enabled"] is False
    assert state["auto_approve_paused"] is False
    assert state["consecutive_freezes"] == 0
    assert state["freeze_threshold"] == 3  # default per ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT
    assert state["per_task_token_cap"] is None
    assert state["window_token_cap"] is None
    assert state["window_tokens_used"] == 0
    assert state["auto_approved_count_24h"] == 0
    assert state["auto_unfreeze_applied_count_24h"] == 0
    assert state["auto_unfreeze_skipped_count_24h"] == 0
    assert state["pause_reason"] is None
    assert state["dry_run_enabled"] is False
    assert state["would_auto_approve_count_24h"] == 0
    assert state["auto_unfreeze_dry_run_enabled"] is False
    assert state["would_auto_unfreeze_count_24h"] == 0
    assert state["validator_judge_fail_count_24h"] == 0
    assert state["validator_judge_timeout_count_24h"] == 0


# ===========================================================================
# TB-238: auto-unfreeze dry-run sibling keys on the collector surface.
# ===========================================================================


def test_collect_state_auto_unfreeze_dry_run_flag(cfg: Config, monkeypatch):
    """`AP2_AUTO_UNFREEZE_DRY_RUN=1` → the aggregator's public dict
    has `auto_unfreeze_dry_run_enabled=True`. Operator-facing CLI /
    web / JSON / status-report digest surfaces consume this key to
    render a "dry-run window" sub-block confirming the loop is in
    monitor mode at a glance.

    Mirror of `test_dry_run_flag_in_collect_auto_approve_state` (TB-232)
    on the axis-2 side; pinned in TB-227's contract file so the
    expected-keys set in `test_collect_state_shape_when_knob_off_and_
    no_activity` and the per-knob behavior pin live alongside the
    rest of the collector contract.
    """
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", raising=False)
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_unfreeze_dry_run_enabled"] is False

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", "1")
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_unfreeze_dry_run_enabled"] is True


def test_collect_state_would_auto_unfreeze_24h_counter(
    cfg: Config, monkeypatch,
):
    """Two seeded `would_auto_unfreeze` events surface as a 24h
    counter value of 2 in the aggregator's public dict — parallel to
    the TB-232 `would_auto_approve_count_24h` counter and the TB-227
    `auto_unfreeze_applied_count_24h` real-mode counter. Pins the
    aggregator's tail-scan symmetry across both event streams.

    Sanity-checks that an empty tail (no `would_auto_unfreeze` events
    seeded) yields zero so the counter doesn't false-positive on
    other event types.
    """
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")

    # Baseline: no events → counter at 0.
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["would_auto_unfreeze_count_24h"] == 0

    events.append(
        cfg.events_file, "would_auto_unfreeze",
        task="TB-600", shape="blocked_review_typo",
        **{"from": "x", "to": "y", "file": "f.md", "line": 1, "dry_run": True},
    )
    events.append(
        cfg.events_file, "would_auto_unfreeze",
        task="TB-601", shape="blocked_review_typo",
        **{"from": "x", "to": "y", "file": "f.md", "line": 2, "dry_run": True},
    )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["would_auto_unfreeze_count_24h"] == 2, state
    # Sanity: real-mode auto-unfreeze counter is untouched (no
    # `auto_unfreeze_applied` events were seeded).
    assert state["auto_unfreeze_applied_count_24h"] == 0


def test_is_auto_unfreeze_dry_run_helper_directly(monkeypatch):
    """Direct unit pin on the env-knob parser so a future refactor
    that changes the truthy-set surfaces clearly here instead of
    cascading through `collect_auto_approve_state`. Mirrors the
    shape of `test_is_auto_approve_dry_run_helper_directly` in
    `test_tb232_auto_approve_dry_run.py`."""
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    assert automation_status._is_auto_unfreeze_dry_run() is False

    for truthy in ("1", "true", "yes"):
        monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", truthy)
        assert automation_status._is_auto_unfreeze_dry_run() is True, truthy

    for falsy in ("0", "false", "no", "", " "):
        monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", falsy)
        assert automation_status._is_auto_unfreeze_dry_run() is False, falsy


def test_collect_state_knob_on_no_halt(cfg: Config, monkeypatch):
    """Knob truthy → `auto_approve_enabled=True`, no halt-class event
    → `auto_approve_paused=False`, `pause_reason=None`."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_enabled"] is True
    assert state["auto_approve_paused"] is False
    assert state["pause_reason"] is None


def test_collect_state_freeze_threshold_env_override(cfg: Config, monkeypatch):
    """`AP2_AUTO_APPROVE_FREEZE_THRESHOLD=5` surfaces as 5, not the
    default 3. Operator-configured caps must be reflected in the
    surface."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "5")
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["freeze_threshold"] == 5


def test_collect_state_token_caps_surfaced(cfg: Config, monkeypatch):
    """Both token-cap knobs surface as their int value when set;
    `None` when unset. Operators tuning their budget must see what
    the daemon's check uses."""
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "150000")
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000000")

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["per_task_token_cap"] == 150000
    assert state["window_token_cap"] == 1000000


def test_collect_state_paused_on_consecutive_freezes(cfg: Config, monkeypatch):
    """An `auto_approve_paused` event past the last
    `auto_approve_unfreeze` ack → `pause_reason="consecutive_freezes"`,
    `auto_approve_paused=True`."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    _append_event(
        cfg, "auto_approve_paused",
        task="TB-100", threshold=3, reason="seeded test pause",
    )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_paused"] is True
    assert state["pause_reason"] == "consecutive_freezes"


def test_collect_state_paused_on_per_task_cap(cfg: Config, monkeypatch):
    """An `auto_approve_halted` event with `reason=per_task_cap` →
    `pause_reason="per_task_token_cap_exceeded"` (renamed per the
    operator-facing vocabulary)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    _append_event(
        cfg, "auto_approve_halted",
        task="TB-101", reason="per_task_cap", used=200_000, cap=150_000,
    )
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_paused"] is True
    assert state["pause_reason"] == "per_task_token_cap_exceeded"


def test_collect_state_paused_on_window_cap(cfg: Config, monkeypatch):
    """An `auto_approve_halted` event with `reason=window_cap` →
    `pause_reason="window_token_cap_exceeded"`."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    _append_event(
        cfg, "auto_approve_halted",
        task="TB-102", reason="window_cap",
        used=1_200_000, cap=1_000_000, window_used=1_200_000,
    )
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_paused"] is True
    assert state["pause_reason"] == "window_token_cap_exceeded"


def test_collect_state_paused_on_task_error(cfg: Config, monkeypatch):
    """An `auto_approve_halted` event with `reason=task_error` →
    `pause_reason="task_error"`."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    _append_event(
        cfg, "auto_approve_halted",
        task="TB-103", reason="task_error",
        error_excerpt="SDK timeout at initialize",
    )
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_paused"] is True
    assert state["pause_reason"] == "task_error"


def test_collect_state_unfreeze_ack_clears_freeze_pause(cfg: Config, monkeypatch):
    """An `operator_ack` with note containing `auto_approve_unfreeze`
    AFTER an `auto_approve_paused` event clears that pause from the
    surface. Pins the same ack-idx-scoping the daemon uses so the
    surface and the live state stay aligned.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    _append_event(
        cfg, "auto_approve_paused",
        task="TB-104", threshold=3, reason="seeded",
    )
    _append_event(
        cfg, "operator_ack",
        verb="auto_approve_unfreeze",
        note="ack: auto_approve_unfreeze — restart after fix",
    )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_paused"] is False
    assert state["pause_reason"] is None


def test_collect_state_window_resume_ack_clears_halt_pause(
    cfg: Config, monkeypatch,
):
    """An `operator_ack` with note containing `auto_approve_window_resume`
    AFTER an `auto_approve_halted` event clears the halt from the
    surface."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    _append_event(
        cfg, "auto_approve_halted",
        task="TB-105", reason="window_cap",
        used=1_200_000, cap=1_000_000,
    )
    _append_event(
        cfg, "operator_ack",
        verb="auto_approve_window_resume",
        note="ack: auto_approve_window_resume — budget reset",
    )

    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_paused"] is False
    assert state["pause_reason"] is None


def test_collect_state_consecutive_freezes_counts_trailing_streak(
    cfg: Config, monkeypatch,
):
    """`consecutive_freezes` is the count of trailing failure-status
    `task_complete` events since the last unfreeze ack. A non-failure
    completion resets the streak to 0."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    # Streak: 2 failures, then a pass, then 3 failures → streak = 3.
    for tid, status in (
        ("TB-200", "verification_failed"),
        ("TB-201", "verification_failed"),
        ("TB-202", "complete"),
        ("TB-203", "verification_failed"),
        ("TB-204", "failed"),
        ("TB-205", "error"),
    ):
        _append_event(
            cfg, "task_complete",
            task=tid, status=status, commit="", summary="seeded",
        )
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["consecutive_freezes"] == 3


def test_collect_state_24h_counter_aggregation(cfg: Config, monkeypatch):
    """`auto_approved_count_24h` counts within the rolling window;
    events older than `window_s` drop. Pins the timestamp-based
    bucketing.
    """
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    now = _dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # 2 inside the 24h window, 1 outside (26h ago).
    _append_event(cfg, "auto_approved", task="TB-300", knob="1")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=1))
    _append_event(cfg, "auto_approved", task="TB-301", knob="1")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=12))
    _append_event(cfg, "auto_approved", task="TB-302", knob="1")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=26))

    state = automation_status.collect_auto_approve_state(cfg, now=now)
    assert state["auto_approved_count_24h"] == 2


def test_collect_state_window_tokens_used_sums_auto_approved_only(
    cfg: Config, monkeypatch,
):
    """`window_tokens_used` sums input+output tokens across
    `task_run_usage` events for AUTO-APPROVED tasks within the rolling
    window. Operator-approved tasks (no `auto_approved` event) are
    excluded — auto-promote budget is the bucket being tracked."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    now = _dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # TB-400 was auto-approved; its usage counts toward the window.
    _append_event(cfg, "auto_approved", task="TB-400", knob="1")
    _append_event(
        cfg, "task_run_usage",
        task="TB-400", status="complete",
        usage={"input_tokens": 1000, "output_tokens": 500,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=1))

    # TB-401 was NOT auto-approved (operator approved). Even within
    # the window, its usage MUST NOT count.
    _append_event(
        cfg, "task_run_usage",
        task="TB-401", status="complete",
        usage={"input_tokens": 5000, "output_tokens": 5000,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=2))

    # TB-402 was auto-approved but its usage is 26h old → outside
    # window.
    _append_event(cfg, "auto_approved", task="TB-402", knob="1")
    _append_event(
        cfg, "task_run_usage",
        task="TB-402", status="complete",
        usage={"input_tokens": 100, "output_tokens": 100,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=26))

    state = automation_status.collect_auto_approve_state(cfg, now=now)
    # Only TB-400's 1500 tokens count.
    assert state["window_tokens_used"] == 1500


def test_collect_state_window_resume_idx_scopes_window_tokens(
    cfg: Config, monkeypatch,
):
    """`window_tokens_used` resets at the most recent
    `auto_approve_window_resume` operator_ack — `task_run_usage`
    events BEFORE the ack don't count toward the post-ack budget."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    now = _dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # 1000 tokens BEFORE the resume ack — must NOT count.
    _append_event(cfg, "auto_approved", task="TB-500", knob="1")
    _append_event(
        cfg, "task_run_usage",
        task="TB-500", status="complete",
        usage={"input_tokens": 600, "output_tokens": 400,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=5))
    _append_event(
        cfg, "operator_ack",
        verb="auto_approve_window_resume",
        note="ack: auto_approve_window_resume — budget reset",
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=4))

    # 700 tokens AFTER the resume ack — counts.
    _append_event(cfg, "auto_approved", task="TB-501", knob="1")
    _append_event(
        cfg, "task_run_usage",
        task="TB-501", status="complete",
        usage={"input_tokens": 400, "output_tokens": 300,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=1))

    state = automation_status.collect_auto_approve_state(cfg, now=now)
    assert state["window_tokens_used"] == 700


# ===========================================================================
# (2) CLI rendering.
# ===========================================================================


def test_cli_status_omits_auto_approve_line_when_off_and_silent(
    cfg: Config, capsys, monkeypatch,
):
    """Knob off AND all 24h counters zero → the `auto-approve:` text
    line is OMITTED entirely (mirrors TB-189's classifications
    omit-on-empty pattern). Fresh / pre-opt-in projects don't grow a
    perpetual zero-line.
    """
    from ap2.cli import cmd_status

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" not in out


def test_cli_status_renders_healthy_line_when_knob_on(
    cfg: Config, capsys, monkeypatch,
):
    """Knob on, no halt → `auto-approve: enabled (...)` line with the
    24h counters. No `PAUSED` token."""
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" in out
    assert "enabled" in out
    assert "PAUSED" not in out


def test_cli_status_renders_paused_line_with_ack_verb(
    cfg: Config, capsys, monkeypatch,
):
    """Knob on + halt active → `auto-approve: PAUSED (reason=...)` line
    that names the ack verb so the operator's action is one readable
    nudge away (mirrors TB-151's pending-review line shape)."""
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-600", reason="window_cap", used=1_200_000, cap=1_000_000,
    )
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve: PAUSED" in out
    assert "window_token_cap_exceeded" in out
    # Ack verb shown literally so the operator can copy-paste it.
    assert "ap2 ack auto_approve_window_resume" in out


def test_cli_status_renders_line_when_knob_off_but_counters_nonzero(
    cfg: Config, capsys, monkeypatch,
):
    """Knob off but a 24h counter is non-zero (e.g. an `auto_approved`
    event still in the window from a prior on-cycle) → the line is
    rendered so operators can see recent automation activity even
    after disabling the knob."""
    from ap2.cli import cmd_status

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    events.append(cfg.events_file, "auto_approved", task="TB-700", knob="1")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" in out


def test_cli_status_json_carries_full_auto_approve_dict(
    cfg: Config, capsys, monkeypatch,
):
    """`ap2 status --json` always includes the `auto_approve` key
    carrying the full helper dict, regardless of knob-state. Pins the
    machine-consumer contract."""
    from ap2.cli import cmd_status

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    # TB-280 hermeticity fix: the per-task / window token-cap knobs
    # leak from the operator's shell into pytest (the operator runs
    # daemons with `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=100000000`), so
    # the `is None` assertions below would otherwise fail purely on
    # env pollution. Same shape as the existing `AP2_AUTO_APPROVE`
    # delenv — pins the default-state contract.
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert "auto_approve" in payload
    a = payload["auto_approve"]
    # Key + type contract (matches `collect_auto_approve_state` docstring).
    assert isinstance(a["auto_approve_enabled"], bool)
    assert isinstance(a["auto_approve_paused"], bool)
    assert isinstance(a["consecutive_freezes"], int)
    assert isinstance(a["freeze_threshold"], int)
    assert isinstance(a["window_tokens_used"], int)
    assert isinstance(a["auto_approved_count_24h"], int)
    assert isinstance(a["auto_unfreeze_applied_count_24h"], int)
    assert isinstance(a["auto_unfreeze_skipped_count_24h"], int)
    # `pause_reason` is `None` when not paused.
    assert a["pause_reason"] is None
    # `per_task_token_cap` / `window_token_cap` default to None.
    assert a["per_task_token_cap"] is None
    assert a["window_token_cap"] is None


def test_cli_status_json_carries_pause_reason_when_paused(
    cfg: Config, capsys, monkeypatch,
):
    """When a halt-class event is active, `--json` carries the
    discriminated `pause_reason` token verbatim — machine consumers
    don't have to re-derive it from the event tail."""
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-800", reason="task_error",
        error_excerpt="SDK timeout",
    )

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    a = payload["auto_approve"]
    assert a["auto_approve_paused"] is True
    assert a["pause_reason"] == "task_error"


# ===========================================================================
# (3) Web home: Automation card.
# ===========================================================================


def test_web_home_omits_automation_card_when_off_and_silent(
    cfg: Config, monkeypatch,
):
    """Pre-opt-in project (knob off + no 24h activity) → the Automation
    card is OMITTED entirely from the home page so fresh projects
    don't grow a perpetual `auto-approve: off` card. Server-side
    omission (not CSS-hidden), per the same pattern as
    `_render_pending_queue` / `_render_operator_decisions`."""
    from ap2 import web

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    html = web._render_home(cfg)
    # The `<div class="automation-status` marker (with the open `class=`
    # attribute prefix) only appears when the renderer emits the card.
    # The CSS stylesheet at the top of the layout also mentions the
    # class name (as a selector); we strictly look for the element form.
    assert '<div class="automation-status' not in html


def test_web_home_renders_healthy_card_when_knob_on(cfg: Config, monkeypatch):
    """Knob on, no halt → the Automation card lands with the
    `is-healthy` class and the `Auto-approve` header. Pins the
    visual-class branch for the happy path."""
    from ap2 import web

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    html = web._render_home(cfg)
    assert '<div class="automation-status is-healthy"' in html
    assert "Auto-approve" in html
    # Healthy state doesn't render the PAUSED tint on the element.
    assert '<div class="automation-status is-paused"' not in html


def test_web_home_renders_paused_card_with_red_border(cfg: Config, monkeypatch):
    """When a halt is active, the Automation card carries the
    `is-paused` class (red border per the TB-148 status palette). Class
    name match is the pin so a future palette refactor that drops the
    class trips here. The ack verb is rendered literally so the
    operator can copy-paste."""
    from ap2 import web

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    events.append(
        cfg.events_file, "auto_approve_halted",
        task="TB-900", reason="window_cap", used=1_200_000, cap=1_000_000,
    )
    html = web._render_home(cfg)
    assert '<div class="automation-status is-paused"' in html
    assert "ap2 ack auto_approve_window_resume" in html


def test_web_home_card_renders_when_counters_nonzero_even_if_knob_off(
    cfg: Config, monkeypatch,
):
    """Knob off, but a 24h `auto_approved` event landed → the card
    surfaces so an operator who disabled the knob still sees recent
    activity in the window."""
    from ap2 import web

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    events.append(cfg.events_file, "auto_approved", task="TB-1000", knob="1")
    html = web._render_home(cfg)
    assert '<div class="automation-status' in html


def test_web_home_card_links_to_events_for_drilldown(cfg: Config, monkeypatch):
    """Each counter row links to `/events?type=<name>` so the operator
    can drill into individual auto-approved / auto-unfrozen events
    without leaving the home page."""
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    events.append(cfg.events_file, "auto_approved", task="TB-1100", knob="1")
    events.append(cfg.events_file, "auto_unfreeze_applied", task="TB-1101", shape="x")
    html = web._render_home(cfg)
    assert '/events?type=auto_approved' in html
    assert '/events?type=auto_unfreeze_applied' in html


def test_web_home_card_renders_sparkline_when_counters_nonzero(
    cfg: Config, monkeypatch,
):
    """Non-zero hourly buckets emit a `<polyline>` sparkline so the
    operator sees the 24h trend at a glance. Empty buckets short-circuit
    to no sparkline (already pinned implicitly by the omit-on-empty card)."""
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    events.append(cfg.events_file, "auto_approved", task="TB-1200", knob="1")
    html = web._render_home(cfg)
    assert "as-sparkline" in html
    assert "<polyline" in html


# ===========================================================================
# Verification: helper-symbol structural pin (briefing's grep verifiers).
# ===========================================================================


def test_helper_module_exports_expected_symbol():
    """The briefing's `grep -nE "^def collect_auto_approve_state" ap2/automation_status.py`
    verifier must match: the top-level helper symbol lands at module
    level (not nested inside a class)."""
    from ap2 import automation_status as _mod
    assert hasattr(_mod, "collect_auto_approve_state")
    assert callable(_mod.collect_auto_approve_state)
