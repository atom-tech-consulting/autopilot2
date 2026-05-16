"""TB-243: behavioral pinning for the validator-judge fail-open surface
on `ap2 status` (text + JSON) and the web home Automation card.

TB-235 (`tools._validate_briefing_structure` check #7) added a Haiku-4.5
judge that rejects briefings naming an implicit hard predecessor not
declared in `@blocked:TB-N`. The judge fails open on SDK / parse
errors — refusing to gate `ap2 add` / `ap2 update` on a transient
Anthropic API hiccup is the load-bearing trade-off — and each skipped
call lands as a `validator_judge_fail` or `validator_judge_timeout`
event. Until TB-243 nothing rendered those events on the operator's
on-demand pull surfaces, leaving the auto-approve safety claim
(goal.md L82-85: "upstream gates already make this safe in practice")
silently-degradable: an operator with `AP2_AUTO_APPROVE=1` whose
judge had been quietly timing out for 10 consecutive briefings had
to wait for the next status-report cron tick (~2h cadence) to find
out.

This module pins five behavioral cases across three surfaces:

  (1) `collect_auto_approve_state` carries the two new keys with the
      correct zero-default + 24h-window filter (events outside the
      window are excluded).
  (2) `cmd_status` text-render omits the `validator-judge:` line when
      both counts are zero; emits it (with the `[noisy]` suffix at the
      threshold) when either is non-zero.
  (3) `cmd_status` `--json` carries the nested
      `auto_approve.validator_judge.{fail_count_24h, timeout_count_24h}`
      object — always present (zeros when no events).
  (4) `_render_automation_card` HTML omits the "Validator judge (24h)"
      row when both counts are zero; emits it (with the `as-vj-noisy`
      warn-tint class at the threshold) when either is non-zero.
  (5) `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` parser honors the operator
      override (with the same default-on-malformed semantics as the
      TB-224 / TB-234 token-cap knobs).

Fixtures mirror TB-227 / TB-241 — `init_project` + `events.append` +
the same `cfg` pytest fixture. No SDK / network / freezegun dependence.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import automation_status, events
from ap2.config import Config
from ap2.init import init_project


# ===========================================================================
# Fixtures + helpers (mirror TB-227's helper trio).
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _ts_offset(now: _dt.datetime, *, hours_ago: float) -> str:
    when = now - _dt.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    """Replace the `ts` field on the most recent events.jsonl line.

    Mirrors `tests/test_tb227_automation_status.py::_rewrite_last_event_ts`
    so the 24h-window edge cases are reachable without a freezegun
    dependency. Public `events.append` always stamps `now()`; tests
    that need an event "in the past" rewrite the line after append.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


# ===========================================================================
# (1) Collector contract — zero-default + 24h-window filter.
# ===========================================================================


def test_collector_zero_default_when_no_events(cfg: Config, monkeypatch):
    """Default state — no `validator_judge_*` events in the tail. Both
    new keys are present with int(0) values regardless of TB-235 knob
    state. Pin against a refactor that drops a key, returns `None`,
    or leaks an undeclared type."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    state = automation_status.collect_auto_approve_state(cfg)
    assert "validator_judge_fail_count_24h" in state
    assert "validator_judge_timeout_count_24h" in state
    assert state["validator_judge_fail_count_24h"] == 0
    assert state["validator_judge_timeout_count_24h"] == 0
    assert isinstance(state["validator_judge_fail_count_24h"], int)
    assert isinstance(state["validator_judge_timeout_count_24h"], int)


def test_collector_24h_window_excludes_old_events(cfg: Config, monkeypatch):
    """Events older than `window_s` MUST NOT count toward the 24h
    aggregates — same filter as `auto_approved_count_24h`.

    Seeds three of each type, two inside the window (1h / 12h ago) and
    one outside (26h ago); the aggregator must return `2` per key.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    now = _dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=_dt.timezone.utc)

    # validator_judge_fail: 2 inside window, 1 outside.
    events.append(cfg.events_file, "validator_judge_fail", error="boom")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=1))
    events.append(cfg.events_file, "validator_judge_fail", error="boom2")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=12))
    events.append(cfg.events_file, "validator_judge_fail", error="old")
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=26))

    # validator_judge_timeout: 2 inside, 1 outside.
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15, error="TimeoutError()",
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=2))
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15, error="TimeoutError()",
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=20))
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15, error="old",
    )
    _rewrite_last_event_ts(cfg, _ts_offset(now, hours_ago=30))

    state = automation_status.collect_auto_approve_state(cfg, now=now)
    assert state["validator_judge_fail_count_24h"] == 2
    assert state["validator_judge_timeout_count_24h"] == 2


# ===========================================================================
# (2) CLI text-render — omit-on-empty + render-on-nonzero + [noisy] suffix.
# ===========================================================================


def test_cli_status_omits_validator_judge_line_when_both_counts_zero(
    cfg: Config, capsys, monkeypatch,
):
    """No `validator_judge_*` events in the tail → `cmd_status` text
    output MUST NOT include a `validator-judge:` sub-line. Pins the
    omit-on-empty rule so the TB-227 default-healthy block stays
    compact (mirrors TB-241's dry-run line behavior).

    Knob `AP2_AUTO_APPROVE=1` is still on so the auto-approve block
    itself renders (otherwise the omit would be vacuous — no block,
    no validator-judge line either).
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" in out  # parent block still renders
    assert "validator-judge:" not in out, out


def test_cli_status_renders_validator_judge_line_when_fail_nonzero(
    cfg: Config, capsys, monkeypatch,
):
    """One `validator_judge_fail` event in the tail → `cmd_status` text
    output includes a `validator-judge: 1 fail | 0 timeout (24h)`
    sub-line. The `[noisy]` suffix is absent because `(1 + 0) < 5`
    (default threshold).
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    events.append(
        cfg.events_file, "validator_judge_fail",
        error="non-dict judge response",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "validator-judge: 1 fail | 0 timeout (24h)" in out, out
    assert "[noisy]" not in out, out


def test_cli_status_appends_noisy_suffix_at_threshold(
    cfg: Config, capsys, monkeypatch,
):
    """When `(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`
    (default 5), the validator-judge sub-line gets a ` [noisy]`
    suffix. Pins the threshold-gating at the rendering layer (the
    collector emits raw counts; the suffix is a render-layer concern).

    Seeds 3 fails + 2 timeouts = 5 to hit the default threshold
    boundary; the inequality is `>=`, so 5 trips.
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    for _ in range(3):
        events.append(cfg.events_file, "validator_judge_fail", error="x")
    for _ in range(2):
        events.append(
            cfg.events_file, "validator_judge_timeout",
            timeout_s=15, error="x",
        )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "validator-judge: 3 fail | 2 timeout (24h) [noisy]" in out, out


# ===========================================================================
# (3) CLI JSON branch — nested object always present (zeros when no events).
# ===========================================================================


def test_cli_status_json_always_present_zero_object(
    cfg: Config, capsys, monkeypatch,
):
    """`--json` carries the nested `auto_approve.validator_judge` object
    even when both counts are zero — machine consumers want a stable
    shape regardless of TB-235 activity.

    Pins the contract:
      out["auto_approve"]["validator_judge"]["fail_count_24h"] == 0
      out["auto_approve"]["validator_judge"]["timeout_count_24h"] == 0
    """
    from ap2.cli import cmd_status

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    aa = out["auto_approve"]
    assert "validator_judge" in aa
    assert aa["validator_judge"] == {
        "fail_count_24h": 0,
        "timeout_count_24h": 0,
    }


def test_cli_status_json_nested_object_carries_counts(
    cfg: Config, capsys, monkeypatch,
):
    """`--json` carries the actual counts in the nested object — pins
    the wiring from collector → JSON layer (not just the zero-default
    case)."""
    from ap2.cli import cmd_status

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    events.append(cfg.events_file, "validator_judge_fail", error="x")
    events.append(cfg.events_file, "validator_judge_fail", error="y")
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15, error="t",
    )

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["auto_approve"]["validator_judge"] == {
        "fail_count_24h": 2,
        "timeout_count_24h": 1,
    }


# ===========================================================================
# (4) Web `_render_automation_card` HTML omit-on-empty + render-on-nonzero
# + warn-tint class at threshold.
# ===========================================================================


def test_web_automation_card_omits_validator_judge_row_when_both_zero(
    cfg: Config, monkeypatch,
):
    """Both counts zero → `_render_automation_card` HTML MUST NOT
    include a "Validator judge (24h)" row. Pins the default-healthy
    card stays untouched by TB-243 when the gate is quiet.
    """
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    card = web._render_automation_card(cfg)
    assert "Validator judge (24h)" not in card, card
    # And the warn-tint class is absent too.
    assert "as-vj-noisy" not in card


def test_web_automation_card_renders_validator_judge_row_when_nonzero(
    cfg: Config, monkeypatch,
):
    """Non-zero counts → the card includes a "Validator judge (24h)"
    row linking to `/events?type=validator_judge_fail` and
    `/events?type=validator_judge_timeout`. The `as-vj-noisy` warn-tint
    class is absent (sum < threshold default 5).
    """
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    events.append(cfg.events_file, "validator_judge_fail", error="x")
    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15, error="t",
    )

    card = web._render_automation_card(cfg)
    assert "Validator judge (24h)" in card, card
    assert "/events?type=validator_judge_fail" in card
    assert "/events?type=validator_judge_timeout" in card
    assert "1 fail" in card
    assert "1 timeout" in card
    # 1 + 1 = 2 < 5 (default threshold) → no warn-tint, no [noisy].
    assert "as-vj-noisy" not in card
    assert "[noisy]" not in card


def test_web_automation_card_warn_tint_at_threshold(
    cfg: Config, monkeypatch,
):
    """When `(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`
    (default 5), the row gets the `as-vj-noisy` class AND the
    `[noisy]` text suffix. Pins both the CSS-hook and the
    operator-readable annotation."""
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    for _ in range(5):
        events.append(cfg.events_file, "validator_judge_fail", error="x")

    card = web._render_automation_card(cfg)
    assert "Validator judge (24h)" in card, card
    assert "as-vj-noisy" in card, card
    assert "[noisy]" in card, card


def test_web_automation_card_renders_when_only_validator_judge_active(
    cfg: Config, monkeypatch,
):
    """Auto-approve knob OFF + no other 24h activity, but a
    `validator_judge_fail` event landed → the card surfaces anyway
    (the silent-degradation hazard is the WHOLE reason for surfacing
    these counts; gating on auto-approve would defeat it).
    """
    from ap2 import web

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    events.append(cfg.events_file, "validator_judge_fail", error="x")

    card = web._render_automation_card(cfg)
    # Card body is present.
    assert "automation-status" in card, card
    assert "Validator judge (24h)" in card


# ===========================================================================
# (5) Threshold knob parser — operator override + default-on-malformed.
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 5),          # unset → default
        ("", 5),            # empty → default
        ("   ", 5),         # whitespace-only → default
        ("not-an-int", 5),  # malformed → default
        ("0", 5),           # zero → default (positive-only)
        ("-3", 5),          # negative → default
        ("1", 1),           # operator override, low
        ("10", 10),         # operator override, high
        ("99", 99),         # operator override, very high
    ],
)
def test_validator_judge_noisy_threshold_parse(monkeypatch, raw, expected):
    """Parser honors operator override; default-on-malformed mirrors
    the TB-224 / TB-234 token-cap helpers so the env-knob vocabulary
    is consistent across the codebase."""
    if raw is None:
        monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raw)
    assert automation_status.validator_judge_noisy_threshold() == expected


def test_cli_status_threshold_override_lowers_noisy_trip_point(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD=2` → 2 fails alone trips
    `[noisy]` (default threshold is 5). Pins the operator-override
    path end-to-end through the text rendering layer.
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "2")

    events.append(cfg.events_file, "validator_judge_fail", error="x")
    events.append(cfg.events_file, "validator_judge_fail", error="y")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "validator-judge: 2 fail | 0 timeout (24h) [noisy]" in out, out
