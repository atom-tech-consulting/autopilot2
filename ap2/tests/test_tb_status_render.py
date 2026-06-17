"""TB-250: behavioral pinning for the `ap2 status` text rendering branches
that drive the `auto-approve:` top-line.

Background: TB-243 added `validator_judge_fail_count_24h` and
`validator_judge_timeout_count_24h` to the `_has_24h_activity` aggregator
in `ap2.cli.cmd_status` so a noisy fail-open gate would surface the
auto-approve block even with `AP2_AUTO_APPROVE=0`. That part is correct.
But the original two-branch render (`if paused: ... else: enabled`)
fell through to printing `auto-approve: enabled (24h: ...)` whenever the
outer `if a["auto_approve_enabled"] or _has_24h_activity:` matched —
i.e. it claimed the knob was on whenever validator-judge events alone
tripped activity. JSON (`auto_approve.auto_approve_enabled`) stayed
correct because the collector never branched off `_has_24h_activity`;
the bug was local to one branch's text. An operator (li@atomtech, ~2026-05-17)
observed `ap2 status` print `auto-approve: enabled (24h: 0 approved, 0
auto-unfrozen)` with no `AP2_AUTO_*` env vars set, triggering a
false-alarm investigation — the exact operator-trust erosion the
observability surfaces are meant to PREVENT.

TB-250 splits the auto-approve top-line into three explicit state
branches (one of which is the existing block-suppress case):

  (A)        knob ON  + healthy        → `auto-approve: enabled (24h: ...)`
  (A-paused) knob ON  + paused         → `auto-approve: PAUSED (...)`
  (B)        knob OFF + has activity   → `auto-approve: disabled (
                                          validator-judge 24h: N fail,
                                          M timeout)`  ← NEW
  (C)        knob OFF + no activity    → suppressed (outer `if` evaluates
                                          false; existing TB-227 behavior)

A test for each branch + a JSON regression-pin that proves the bug never
escaped the text-render layer. Fixtures mirror TB-227 / TB-243 —
`init_project` + `events.append`; no SDK / network / freezegun
dependence.
"""
from __future__ import annotations

import json as _json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events
from ap2.config import Config
from ap2.init import init_project


# ===========================================================================
# Fixtures + helpers — same shape as TB-227 / TB-243.
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Per-test cfg with AP2_* env stripped BEFORE `Config.load` (TB-332
    cross-package migration). The cfg-read path
    (`Config.get_component_value`) snapshots the env layer onto
    `cfg.components_config` at load time; pre-stripping prevents a
    parent-shell `AP2_AUTO_APPROVE=1` from painting the cfg snapshot
    before a test body's `_clear_auto_env(monkeypatch)` runs.
    """
    import os

    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _clear_auto_env(monkeypatch) -> None:
    """Strip every `AP2_AUTO_*` env knob the test could inherit so the
    `auto-approve disabled / no activity` branch is reachable from a
    parent shell that left one of them set. Mirrors TB-243 test
    fixture's monkeypatch.delenv calls."""
    for name in (
        "AP2_AUTO_APPROVE",
        "AP2_AUTO_APPROVE_DRY_RUN",
        "AP2_AUTO_UNFREEZE_DRY_RUN",
        "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)


# ===========================================================================
# (A) Knob ON, healthy → "auto-approve: enabled (24h: ...)"
# ===========================================================================


def test_status_text_auto_approve_enabled_renders_enabled(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_AUTO_APPROVE=1`, no pause / halt events → text-render top
    line is the TB-227 healthy text (`auto-approve: enabled (24h: 0
    approved, 0 auto-unfrozen)`). Pins the State-A branch unchanged by
    TB-250 — operators with the knob on must keep seeing the same
    label they've trained their eyes on for the past dozen TBs."""
    from ap2.cli import cmd_status

    _clear_auto_env(monkeypatch)
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve: enabled" in out, out
    # Defensive: ensure the State-B "disabled" text didn't accidentally
    # appear (a refactor that swapped the two branches would trip here).
    assert "auto-approve: disabled" not in out, out
    assert "auto-approve: PAUSED" not in out, out


# ===========================================================================
# (B) Knob OFF, validator-judge activity present → "auto-approve: disabled (...)"
# ===========================================================================


def test_status_text_auto_approve_disabled_with_validator_failures_renders_disabled(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_AUTO_APPROVE` unset + `validator_judge_fail` events in the
    24h window → text-render top line is `auto-approve: disabled
    (validator-judge 24h: N fail, M timeout)`. Pre-TB-250 this branch
    printed `auto-approve: enabled (24h: 0 approved, 0 auto-unfrozen)`,
    falsely claiming the knob was on. The regression-pin for the
    exact bug observed in the wild (~2026-05-17).
    """
    from ap2.cli import cmd_status

    _clear_auto_env(monkeypatch)

    # Seed two fails to make the count distinguishable from 0.
    events.append(cfg.events_file, "validator_judge_fail", error="boom-a")
    events.append(cfg.events_file, "validator_judge_fail", error="boom-b")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # State-B top-line text.
    assert "auto-approve: disabled" in out, out
    assert "validator-judge 24h: 2 fail" in out, out
    # The bug we're pinning AGAINST: the false-positive "enabled" label.
    assert "auto-approve: enabled" not in out, out
    # And PAUSED must not appear either — paused requires the knob to
    # have been on at some point and the daemon to have logged a halt;
    # neither is present in this scenario.
    assert "auto-approve: PAUSED" not in out, out


def test_status_text_auto_approve_disabled_with_validator_timeout_renders_disabled(
    cfg: Config, capsys, monkeypatch,
):
    """Sibling of the fail-counts test: `validator_judge_timeout` events
    alone also justify rendering the State-B block (the `_has_24h_activity`
    aggregator counts both flavors). Counts surface separately in the
    label so an operator can tell a flaky API (mostly timeouts) from a
    model / parse regression (mostly fails) at the top line."""
    from ap2.cli import cmd_status

    _clear_auto_env(monkeypatch)

    events.append(
        cfg.events_file, "validator_judge_timeout",
        timeout_s=15, error="TimeoutError()",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve: disabled" in out, out
    assert "validator-judge 24h: 0 fail, 1 timeout" in out, out
    assert "auto-approve: enabled" not in out, out


# ===========================================================================
# (C) Knob OFF, no activity → block suppressed entirely
# ===========================================================================


def test_status_text_auto_approve_disabled_no_activity_suppresses_block(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_AUTO_APPROVE` unset + events.jsonl empty → no
    `auto-approve:` line at all. Pins the TB-227 default-off behavior:
    fresh / pre-opt-in projects don't grow a perpetual zero-line.
    Pre-TB-250 this was already correct (the block was suppressed);
    pinning it explicitly so the State-B branch's introduction doesn't
    accidentally leak into the no-activity case (e.g. a refactor that
    forgot the outer-`if` guard)."""
    from ap2.cli import cmd_status

    _clear_auto_env(monkeypatch)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" not in out, out
    # Sanity: the block-suppress condition is "no validator-judge sub-line
    # either" — keep the assertion explicit so a regression that tries
    # to render the sub-line independently of the parent block fails
    # loudly here.
    assert "validator-judge:" not in out, out


# ===========================================================================
# (A-paused) Knob ON + paused → "auto-approve: PAUSED (...)"
# ===========================================================================


def test_status_text_auto_approve_paused_renders_paused(
    cfg: Config, capsys, monkeypatch,
):
    """An `auto_approve_paused` event with `AP2_AUTO_APPROVE=1` →
    text-render top line is the TB-227 PAUSED text with the ack-verb
    nudge. Pins that the State-A split into (healthy / paused) is
    preserved by TB-250 — the paused branch sits inside the
    `auto_approve_enabled` arm, NOT in the new State-B arm (which
    fires only when the knob is OFF)."""
    from ap2.cli import cmd_status

    _clear_auto_env(monkeypatch)
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")

    # Seed a pause event via the canonical TB-223 chain — same fixture
    # shape used by test_tb227_automation_status::test_collect_state_paused_on_consecutive_freezes.
    events.append(
        cfg.events_file, "auto_approve_paused",
        task="TB-100", threshold=3, reason="seeded test pause",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve: PAUSED" in out, out
    # The "enabled" and "disabled" branches MUST NOT also render — only
    # one auto-approve top-line ever.
    assert "auto-approve: enabled" not in out, out
    assert "auto-approve: disabled" not in out, out


# ===========================================================================
# JSON regression-pin — the bug never affected --json output.
# ===========================================================================


def test_status_json_auto_approve_enabled_unchanged_under_disabled(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_AUTO_APPROVE` unset + validator-judge fail events present
    → `--json` reports `auto_approve.auto_approve_enabled: false`.
    Pre-TB-250 the JSON path was already correct (the collector
    `automation_status.collect_auto_approve_state` reads the env knob
    directly, never branches off `_has_24h_activity`). Pinning the
    invariant so a future refactor that tries to "harmonize" the
    text and JSON renderings doesn't accidentally regress the JSON
    contract to match the buggy text behavior.
    """
    from ap2.cli import cmd_status

    _clear_auto_env(monkeypatch)

    events.append(cfg.events_file, "validator_judge_fail", error="x")

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    # Top-level `auto_approve` block exists, and the `auto_approve_enabled`
    # flag inside it is `False` despite the validator-judge activity.
    assert out["auto_approve"]["auto_approve_enabled"] is False
    # And the validator-judge counts still surface in the nested object
    # (TB-243 contract — independent of the env knob).
    assert out["auto_approve"]["validator_judge"]["fail_count_24h"] == 1
    assert out["auto_approve"]["validator_judge"]["timeout_count_24h"] == 0
