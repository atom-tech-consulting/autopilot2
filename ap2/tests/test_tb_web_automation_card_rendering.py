"""TB-256: behavioral pinning for the `_render_automation_card` body
text branches on the web home (`http://127.0.0.1:8730/`).

Symmetric mirror of TB-250's `test_tb_status_render.py` for the CLI
surface. Pre-TB-256 the web renderer fell through to printing
`enabled — circuit healthy` whenever the outer
`if not enabled and counters_total == 0: return ""` guard cleared —
which meant a `validator_judge_fail` event in the 24h window made the
card claim auto-approve was on, even with `AP2_AUTO_APPROVE` unset.
JSON path (`auto_approve.auto_approve_enabled`) stayed correct
because the aggregator never branched off `counters_total`; the bug
was local to one branch's text. An operator observed `ap2 status`
text was corrected by TB-250 but the web home still rendered the
stale "enabled" text — same regression-pin, sibling surface.

TB-256 splits the body render into three explicit state branches
(one of which is the existing block-suppress case):

  (A)        knob ON  + healthy        → "enabled — circuit healthy"
                                          (klass=is-healthy)
  (A-paused) knob ON  + paused         → "PAUSED (reason=...; ...)"
                                          (klass=is-paused)
  (B)        knob OFF + has activity   → "disabled (validator-judge
                                          24h: N fail, M timeout)"
                                          (klass=is-disabled-but-active)
                                          ← NEW
  (C)        knob OFF + no activity    → "" (suppressed; existing
                                          TB-227 behavior at L1642)

Four tests below cover one branch each. Fixture style mirrors
TB-227 (`test_tb227_automation_status.py`) — `init_project` +
`events.append`; no SDK / network / freezegun dependence.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import events, web
from ap2.config import Config
from ap2.init import init_project


# ===========================================================================
# Fixtures + helpers — same shape as TB-227 / TB-250.
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Per-test cfg with AP2_* env stripped BEFORE `Config.load` (TB-332
    cross-package migration). Mirrors the TB-227 / TB-326 fixture
    shape so the cfg snapshot doesn't inherit a parent-shell
    `AP2_AUTO_APPROVE=1` painted at `apply_env_overrides` time.
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
    parent shell that left one of them set. Mirrors TB-250's
    test-module helper."""
    for name in (
        "AP2_AUTO_APPROVE",
        "AP2_AUTO_APPROVE_DRY_RUN",
        "AP2_AUTO_UNFREEZE_DRY_RUN",
        "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)


# ===========================================================================
# (A) Knob ON, healthy → "enabled — circuit healthy"
# ===========================================================================


def test_web_card_renders_enabled_when_auto_approve_on(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE=1`, no pause / halt events → card body is the
    TB-227 healthy text (`enabled — circuit healthy`) inside an
    `is-healthy` element. Pins the State-A branch unchanged by TB-256.
    """
    _clear_auto_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    rendered = web._render_automation_card(cfg)
    assert "enabled — circuit healthy" in rendered, rendered
    # The is-healthy element class confirms the State-A branch fired
    # (State-B uses `is-disabled-but-active`; State-A-paused uses
    # `is-paused`).
    assert 'class="automation-status is-healthy"' in rendered, rendered
    # The bug we're pinning AGAINST in the symmetric direction: ensure
    # the State-B disabled text didn't leak into a card whose knob is
    # genuinely on.
    assert "disabled" not in rendered, rendered


# ===========================================================================
# (B) Knob OFF, validator-judge activity present → "disabled (...)"
# ===========================================================================


def test_web_card_renders_disabled_when_off_but_validator_activity_present(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE` unset + 4 `validator_judge_fail` events in
    the 24h window → card body is `disabled (validator-judge 24h: 4
    fail, 0 timeout)` inside an `is-disabled-but-active` element.
    Pre-TB-256 this branch printed `enabled — circuit healthy`,
    falsely claiming the knob was on. The regression-pin for the
    exact bug observed by the operator (2026-05-18) on the web
    home after TB-250 fixed the CLI mirror.
    """
    _clear_auto_env(monkeypatch)

    # Seed four fails to make the count distinguishable from 0.
    for i in range(4):
        events.append(
            cfg.events_file, "validator_judge_fail", error=f"boom-{i}",
        )

    rendered = web._render_automation_card(cfg)
    # State-B body text + the count (4) must surface somewhere in the
    # body — operators triaging a noisy judge need the magnitude at a
    # glance, not just the "disabled" label.
    assert "disabled" in rendered, rendered
    assert "4" in rendered, rendered
    # The bug we're pinning AGAINST: the false-positive "enabled" label.
    assert "enabled — circuit healthy" not in rendered, rendered
    # The new klass name surfaces so the CSS rule can style the State-B
    # card distinctly from the green is-healthy state.
    assert "is-disabled-but-active" in rendered, rendered


# ===========================================================================
# (C) Knob OFF, no activity → card suppressed entirely
# ===========================================================================


def test_web_card_suppressed_when_off_and_no_activity(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE` unset + events.jsonl empty → `_render_automation_card`
    returns the empty string. Pins the TB-227 default-off behavior at
    L1642: fresh / pre-opt-in projects don't grow a perpetual
    auto-approve card. Pre-TB-256 this was already correct (the
    block was suppressed); pinning it explicitly so the State-B
    branch's introduction doesn't accidentally leak into the
    no-activity case (e.g. a refactor that forgot the outer-`if`
    guard).
    """
    _clear_auto_env(monkeypatch)

    rendered = web._render_automation_card(cfg)
    assert rendered == "", rendered


# ===========================================================================
# (A-paused) Knob ON + paused → "PAUSED (...)" with ack verb
# ===========================================================================


def test_web_card_paused_state(cfg: Config, monkeypatch):
    """An `auto_approve_paused` event with `AP2_AUTO_APPROVE=1` → card
    is rendered in the `is-paused` element with `PAUSED` in the
    header. Pins that the State-A split into (healthy / paused) is
    preserved by TB-256 — the paused branch fires regardless of
    `enabled` (its outer guard is `state["auto_approve_paused"]`),
    NOT in the new State-B arm (which fires only when the knob is
    OFF + not paused).
    """
    _clear_auto_env(monkeypatch)
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")

    # Seed a pause event via the canonical TB-223 chain — same fixture
    # shape used by test_tb227_automation_status::test_web_home_renders_paused_card_with_red_border.
    events.append(
        cfg.events_file, "auto_approve_paused",
        task="TB-900", threshold=3, reason="seeded test pause",
    )

    rendered = web._render_automation_card(cfg)
    assert "PAUSED" in rendered, rendered
    assert 'class="automation-status is-paused"' in rendered, rendered
    # The paused branch wins over both the healthy AND disabled-with-
    # activity branches — only one body ever renders.
    assert "enabled — circuit healthy" not in rendered, rendered
    assert "is-disabled-but-active" not in rendered, rendered
