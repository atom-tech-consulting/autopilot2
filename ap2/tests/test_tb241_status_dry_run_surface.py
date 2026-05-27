"""TB-241: behavioral pinning for the dry-run readiness signal on the
operator's primary on-demand return surfaces (`ap2 status` text + the
web home Automation card).

TB-238 (`d861d83`) extended `automation_status.collect_auto_approve_state`
with four dry-run readiness fields (`auto_approve_dry_run_enabled` /
`would_auto_approve_count_24h` / `auto_unfreeze_dry_run_enabled` /
`would_auto_unfreeze_count_24h`) AND added a `*Dry-run window:*` digest
sub-block to the status-report cron, but `ap2/cli.py:cmd_status` and
`ap2/web.py:_render_automation_card` still rendered only the
real-mode TB-227 counters. An operator flipping `AP2_AUTO_APPROVE_DRY_RUN=1`
and running `ap2 status` saw a byte-identical auto-approve summary —
zero evidence the knob changed anything until the next status-report
cron tick.

Four behavioral cases pinned here:

  (1) `cmd_status` text-render emits a `dry-run: would-approve N (24h) | would-unfreeze M (24h)`
      line immediately below the existing `auto-approve:` line when EITHER
      dry-run knob is on.
  (2) `cmd_status` text-render OMITS the `dry-run:` line entirely when
      both dry-run knobs are off (preserves the TB-227 default-off output).
  (3) `cmd_status` block-visibility heuristic counts dry-run 24h activity
      so the auto-approve block surfaces even when both knobs are off but
      `would_auto_approve` / `would_auto_unfreeze` events landed in the
      window (an operator who flipped both knobs off but ran their
      dry-run validation earlier in the window still sees the readiness
      trail).
  (4) `_render_automation_card` HTML carries a `would-approved (24h)` row
      AND the `[dry-run]` badge when the auto-approve dry-run knob is on
      (parallel pin for the auto-unfreeze axis covered by case 1 + a
      direct symmetry assertion in test 5).

Fixtures mirror TB-227 / TB-228 — `init_project` + `events.append` +
the same `cfg` pytest fixture. No SDK / network / freezegun dependence.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


# ---------------------------------------------------------------------------
# (1) cli text render shows the dry-run line when EITHER knob is on.
# ---------------------------------------------------------------------------


def test_cli_status_renders_dry_run_line_when_auto_approve_dry_run_on(
    cfg: Config, capsys, monkeypatch,
):
    """`AP2_AUTO_APPROVE_DRY_RUN=1` → `cmd_status` text output includes
    a `dry-run: would-approve N (24h) | would-unfreeze M (24h)` line
    immediately below the existing `auto-approve:` line.

    Fixture seeds one `would_auto_approve` event so the counter renders
    as `1`. The auto-unfreeze side carries `0` (knob off) but is still
    rendered in the same line — operators reading the readiness
    signal want both axes visible at a glance even when only one is
    in monitor mode.
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-1500", knob="1", dry_run=True,
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" in out
    assert "dry-run: would-approve 1 (24h) | would-unfreeze 0 (24h)" in out, out


def test_cli_status_renders_dry_run_line_when_auto_unfreeze_dry_run_on(
    cfg: Config, capsys, monkeypatch,
):
    """Symmetric pin on the axis-2 side: `AP2_AUTO_UNFREEZE_DRY_RUN=1`
    (with auto-approve dry-run off) still produces the same single
    `dry-run:` line, with the would-unfreeze count populated from a
    seeded event. Pins that the line shape is one block per surface,
    not one block per knob.
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")

    events.append(
        cfg.events_file, "would_auto_unfreeze",
        task="TB-1501", shape="blocked_review_typo",
        **{"from": "x", "to": "y", "file": "f.md", "line": 1, "dry_run": True},
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run: would-approve 0 (24h) | would-unfreeze 1 (24h)" in out, out


# ---------------------------------------------------------------------------
# (2) cli text render OMITS the dry-run line when BOTH knobs are off.
# ---------------------------------------------------------------------------


def test_cli_status_omits_dry_run_line_when_both_knobs_off(
    cfg: Config, capsys, monkeypatch,
):
    """Default-off byte-identical regression pin: neither dry-run knob
    set → `cmd_status` text output MUST NOT include a `dry-run:` line.
    Pins the omit-on-empty rule so the TB-227 default-off auto-approve
    block stays untouched by TB-241.

    Knob `AP2_AUTO_APPROVE=1` is still on so the auto-approve block
    itself renders (otherwise the omit would be vacuous — no block,
    no dry-run line either).
    """
    from ap2.cli import cmd_status

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-approve:" in out  # parent block still renders
    assert "dry-run:" not in out, out
    assert "would-approve" not in out
    assert "would-unfreeze" not in out


# ---------------------------------------------------------------------------
# (3) Block-visibility heuristic counts dry-run 24h activity so the
# auto-approve block surfaces even when both knobs are OFF but `would_*`
# events landed in the window.
# ---------------------------------------------------------------------------


def test_cli_status_renders_block_when_dry_run_24h_activity_nonzero_and_knobs_off(
    cfg: Config, capsys, monkeypatch,
):
    """Knobs all off (no `AP2_AUTO_APPROVE`, no dry-run knobs) but a
    `would_auto_approve` event landed in the 24h window → the
    `auto-approve:` block surfaces so the operator still sees the
    real-mode summary (with the dry-run line OMITTED because both
    dry-run knobs are off — the events are historical from a prior
    on-cycle).

    Pre-TB-241 the heuristic counted only `auto_approved_count_24h +
    auto_unfreeze_applied_count_24h + auto_unfreeze_skipped_count_24h`,
    so a dry-run-only window made the block disappear once the operator
    flipped the knobs off — losing the audit trail. TB-241's heuristic
    extension keeps the block visible.
    """
    from ap2.cli import cmd_status

    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-1502", knob="1", dry_run=True,
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # Parent block renders even though the legacy real-mode counters
    # are all zero.
    assert "auto-approve:" in out, out
    # But the dry-run line itself is omitted because both knobs are off
    # (the events are historical residue, not a live monitor session).
    assert "dry-run:" not in out


# ---------------------------------------------------------------------------
# (4) Web `_render_automation_card` HTML contains the `would-approved`
# row + `[dry-run]` badge when the auto-approve dry-run knob is on.
# ---------------------------------------------------------------------------


def test_web_automation_card_renders_would_approved_row_and_badge(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_APPROVE_DRY_RUN=1` → the Automation card's HTML
    includes:
      - a `would-approved (24h)` row linking to
        `/events?type=would_auto_approve` (parallel shape to the
        existing `auto-approved (24h)` row), AND
      - a `[dry-run]` badge next to the card header so the operator
        sees at a glance that the loop is in monitor mode.

    Auto-unfreeze knob is off in this fixture so the parallel
    `would-unfrozen` row is suppressed — pins the per-axis gating of
    the rows (each row appears only when its knob is on, mirroring
    the TB-238 digest sub-block's on-axis suppression rule).
    """
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    events.append(
        cfg.events_file, "would_auto_approve",
        task="TB-1600", knob="1", dry_run=True,
    )

    html = web._render_home(cfg)
    assert "would-approved (24h)" in html, html
    assert '/events?type=would_auto_approve' in html
    assert "[dry-run]" in html
    # Per-axis suppression: auto-unfreeze knob off → would-unfrozen
    # row is omitted (operator sees only the axis they opted in to).
    assert "would-unfrozen (24h)" not in html


def test_web_automation_card_renders_would_unfrozen_row_when_au_dry_run_on(
    cfg: Config, monkeypatch,
):
    """Symmetric pin on the axis-2 side: `AP2_AUTO_UNFREEZE_DRY_RUN=1`
    → the card includes a `would-unfrozen (24h)` row linking to
    `/events?type=would_auto_unfreeze` plus the `[dry-run]` badge. The
    `would-approved` row is suppressed (auto-approve dry-run off).

    Confirms the badge is gated on `(aa_dry_run or au_dry_run)` rather
    than just the auto-approve side — a refactor that hard-codes the
    badge to the auto-approve knob trips here.
    """
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")

    events.append(
        cfg.events_file, "would_auto_unfreeze",
        task="TB-1601", shape="blocked_review_typo",
        **{"from": "x", "to": "y", "file": "f.md", "line": 1, "dry_run": True},
    )

    html = web._render_home(cfg)
    assert "would-unfrozen (24h)" in html, html
    assert '/events?type=would_auto_unfreeze' in html
    assert "[dry-run]" in html
    # Per-axis suppression on the other side.
    assert "would-approved (24h)" not in html


def test_web_automation_card_omits_dry_run_artifacts_when_both_knobs_off(
    cfg: Config, monkeypatch,
):
    """Default-off byte-identical regression pin for the web card:
    neither dry-run knob set → the card's HTML MUST NOT include the
    `[dry-run]` badge, the `would-approved (24h)` row, or the
    `would-unfrozen (24h)` row. Preserves TB-227's default-on output
    so a pre-opt-in operator's card stays untouched by TB-241.
    """
    from ap2 import web

    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)

    html = web._render_home(cfg)
    # Parent card still renders (knob on).
    assert '<div class="automation-status' in html
    assert "[dry-run]" not in html
    assert "would-approved" not in html
    assert "would-unfrozen" not in html
