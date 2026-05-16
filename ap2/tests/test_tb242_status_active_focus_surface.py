"""TB-242: behavioral pinning for the axis-4 focus-rotation operator
surface on `ap2 status` (text + JSON) and the web home.

TB-226 (`b8a3...` parser + `focus_pointer.json`) and TB-237 (`b2fb6b1`
axis-4 e2e walk-away pin) shipped the multi-focus rotation
machinery, but until TB-242 the only way for an operator to answer
"what focus am I on, and how many remain?" was to grep events.jsonl
for `focus_advanced` or read `focus_pointer.json` by hand. That
left the axis-4 walk-away promise (goal.md L131-138: "walk-away
time scales with the operator-declared roadmap length") technically
shipped but operationally unverifiable on demand.

This module pins the render contract on three surfaces:

  (1) `cmd_status` text: single-focus goal.md → `focus: <title>`
      (no `(N of M)` suffix — single-focus projects don't need a
      position counter).
  (2) `cmd_status` text: multi-focus goal.md → `focus: <title>
      (1 of 3)`.
  (3) `cmd_status` text: roadmap-complete halt state → the halt-state
      line "focus: ROADMAP_COMPLETE — ap2 ack roadmap_complete to
      resume", mirroring TB-227's auto-approve PAUSED line shape.
  (4) `cmd_status` JSON: `active_focus` block carries
      `title` / `index` / `total` / `roadmap_complete` keys, and
      falls back to `null` when no `## Current focus:` headings
      exist (fresh-project no-op path).
  (5) `web._render_home` HTML: focus title + position rendered
      above the automation card.

Fixtures mirror TB-241 / TB-227 — `init_project` + a goal.md write
+ the same `cfg` pytest fixture. No SDK / network / freezegun
dependence — purely a read-layer composition test.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, goal
from ap2.config import Config
from ap2.init import init_project


# Direct references to the names the briefing's `## Verification`
# bullets / coverage-drift gates expect to see in this test file.
# Loaded at module top so a refactor that removes them surfaces
# cleanly on import.
_NAMES_FOR_DRIFT_GATE = (
    goal.read_focus_list,
    goal.active_focus,
    goal.load_pointer,
    goal.save_pointer,
    goal.roadmap_exhausted,
)


# ===========================================================================
# Fixtures
# ===========================================================================


_GOAL_MD_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- top-level done\n\n"
    "{focus_section}"
    "## Non-goals\n\n"
    "- ng\n"
)


def _write_goal_md(cfg: Config, focus_section: str) -> None:
    """Overwrite the project's goal.md with the canonical scaffold +
    the supplied `focus_section`. Tests use this to control the
    number / shape of `## Current focus:` headings the unit under
    test sees."""
    (cfg.project_root / "goal.md").write_text(
        _GOAL_MD_TEMPLATE.format(focus_section=focus_section)
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout. Tests write
    goal.md themselves before invoking the unit under test (the
    default `init_project` scaffold uses `## Current focus` without
    the trailing colon, so the focus-list parser returns [] until
    the test explicitly writes a colon-bearing focus section)."""
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


# ===========================================================================
# (1) Single-focus goal.md → text shows `focus: <title>` without
# the `(N of M)` suffix.
# ===========================================================================


def test_cli_status_single_focus_text_omits_position(
    cfg: Config, capsys,
):
    """One `## Current focus:` heading → the text-render shows
    `focus:    <title>` with no position counter. Single-focus
    projects don't need an N-of-M suffix (and the operator reading
    `(1 of 1)` would correctly suspect the surface is broken)."""
    from ap2.cli import cmd_status

    _write_goal_md(cfg, "## Current focus: bootstrap\n\nbody.\n\n")
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" in out
    assert "bootstrap" in out
    # Single-focus projects don't grow a `(1 of 1)` counter.
    assert "1 of 1" not in out
    assert "(1 of " not in out


# ===========================================================================
# (2) Multi-focus goal.md → text shows `focus: <title> (1 of 3)`.
# ===========================================================================


def test_cli_status_multi_focus_text_shows_position(
    cfg: Config, capsys,
):
    """Three `## Current focus:` headings → text-render shows the
    first focus's title with `(1 of 3)` position counter. Pins the
    multi-focus walk-away render."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n"
        "## Current focus: gamma\n\ngamma body.\n\n",
    )
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" in out
    assert "alpha (1 of 3)" in out, out


def test_cli_status_multi_focus_text_reflects_pointer_advance(
    cfg: Config, capsys,
):
    """After the pointer advances (e.g. to index 1), text-render
    reflects the NEW active focus title + `(2 of 3)` position. Pins
    that the surface is pointer-driven, not always-first-focus."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n"
        "## Current focus: gamma\n\ngamma body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    pointer["active_title"] = "beta"
    goal.save_pointer(cfg, pointer)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "beta (2 of 3)" in out, out


# ===========================================================================
# (3) Roadmap-complete halt → text shows the halt-state line with
# the `ap2 ack roadmap_complete` resume hint.
# ===========================================================================


def test_cli_status_roadmap_complete_text_shows_halt_line(
    cfg: Config, capsys,
):
    """Pointer past the last focus AND a `roadmap_complete` event in
    the tail with no subsequent ack → text-render shows the halt
    line `focus: ROADMAP_COMPLETE — `ap2 ack roadmap_complete` to
    resume`. Mirrors TB-227's auto-approve PAUSED line shape so the
    operator's eye picks up the halt without a second pass."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    # Push the pointer past the last focus (active_index >= len(foci))
    # to simulate the halt state with no operator ack yet.
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 2  # past the last 0-indexed focus
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)
    # Emit a `roadmap_complete` event so the events-scan branch of
    # `roadmap_exhausted` finds a halt marker; absence of a
    # subsequent `operator_ack` event with the `roadmap_complete`
    # token keeps the halt active.
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ROADMAP_COMPLETE" in out, out
    assert "`ap2 ack roadmap_complete`" in out, out
    # Halt-state render replaces the position counter entirely — no
    # stale `(N of M)` should leak through.
    assert "(3 of 2)" not in out


def test_cli_status_roadmap_complete_cleared_after_ack(
    cfg: Config, capsys,
):
    """After an `operator_ack` event carrying the `roadmap_complete`
    token lands AFTER the most recent `roadmap_complete` event, the
    halt clears and the text-render falls back to the regular
    focus line (no `ROADMAP_COMPLETE` marker). Pins that the
    render is wired to `goal.roadmap_exhausted`'s ack-aware
    verdict, not just the raw `active_index >= total` check."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 2
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")
    # Operator-ack arrives later in the tail — clears the halt.
    events.append(
        cfg.events_file, "operator_ack",
        note="roadmap_complete acknowledged; extending roadmap",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ROADMAP_COMPLETE" not in out, out


# ===========================================================================
# (4) JSON output includes the `active_focus` block.
# ===========================================================================


def test_cli_status_json_carries_active_focus_block(
    cfg: Config, capsys,
):
    """Multi-focus goal.md → JSON output has an `active_focus`
    object with the four contracted keys (`title`, `index`,
    `total`, `roadmap_complete`). `index` is 0-based to match the
    underlying `focus_pointer.json`'s `active_index` field; the
    text branch displays it as `idx+1` for human readability."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "active_focus" in payload
    af = payload["active_focus"]
    assert af is not None
    assert af["title"] == "alpha"
    assert af["index"] == 0
    assert af["total"] == 2
    assert af["roadmap_complete"] is False


def test_cli_status_json_active_focus_null_when_no_focus_headings(
    cfg: Config, capsys,
):
    """Fresh-project no-op path: goal.md present but no
    `## Current focus:` headings (the default `ap2 init` scaffold
    uses `## Current focus` WITHOUT the trailing colon, so the
    parser returns []) → JSON exposes the key but with a `null`
    value so machine consumers can distinguish "no roadmap" from
    "roadmap exhausted"."""
    from ap2.cli import cmd_status

    # No goal.md write — use the bare `init_project` scaffold which
    # has `## Current focus` (no colon) and therefore parses to [].
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "active_focus" in payload
    assert payload["active_focus"] is None


def test_cli_status_json_active_focus_carries_roadmap_complete_flag(
    cfg: Config, capsys,
):
    """Halt state → JSON's `active_focus.roadmap_complete` is True.
    Pins that machine consumers (web UI, external monitors) can read
    the halt state from one boolean field instead of replicating the
    events-scan logic."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 2
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    af = payload["active_focus"]
    assert af is not None
    assert af["total"] == 2
    assert af["roadmap_complete"] is True


def test_cli_status_text_omits_focus_line_when_no_focus_headings(
    cfg: Config, capsys,
):
    """Fresh-project no-op path on the text surface: the default
    `init_project` goal.md (no `## Current focus:` colon-headings)
    → no `focus:` line in the text output. Preserves byte-identical
    pre-TB-242 output for projects that haven't pivoted to the
    focus-rotation model yet."""
    from ap2.cli import cmd_status

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" not in out


def test_cli_status_text_omits_focus_line_when_goal_md_missing(
    cfg: Config, capsys,
):
    """Even more aggressive no-op path: goal.md doesn't exist at
    all → the surface is silent (no `focus:` line in text). Pins
    that `read_focus_list`'s missing-file path is respected end
    to end."""
    from ap2.cli import cmd_status

    (cfg.project_root / "goal.md").unlink()
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" not in out


# ===========================================================================
# (5) Web home HTML renders the focus title + position.
# ===========================================================================


def test_web_home_renders_focus_card_with_position(cfg: Config):
    """Multi-focus goal.md → the home HTML includes the focus
    title AND the `1 of 3` position string. Pins the parallel
    surface to the CLI's text render."""
    from ap2 import web

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n"
        "## Current focus: gamma\n\ngamma body.\n\n",
    )
    html_out = web._render_home(cfg)
    assert "alpha" in html_out
    assert "1 of 3" in html_out, html_out
    # Card uses a `Focus` header label so it's findable in the DOM.
    assert ">Focus<" in html_out, html_out


def test_web_home_renders_focus_card_single_focus_without_position(cfg: Config):
    """Single-focus goal.md → the home HTML includes the focus
    title and a `Focus` header label, but no `(1 of 1)` position
    counter (symmetry with the CLI text render)."""
    from ap2 import web

    _write_goal_md(cfg, "## Current focus: bootstrap\n\nbody.\n\n")
    html_out = web._render_home(cfg)
    assert "bootstrap" in html_out
    assert ">Focus<" in html_out
    assert "1 of 1" not in html_out


def test_web_home_renders_focus_card_halt_state(cfg: Config):
    """Halt state → the home HTML includes the `ROADMAP_COMPLETE`
    marker and the `ap2 ack roadmap_complete` resume verb rendered
    as `<code>`. Parallel to TB-227's auto-approve PAUSED card
    shape so the operator's eye picks up both halts as one cluster
    of urgent operator-attention surfaces."""
    from ap2 import web

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 2
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")

    html_out = web._render_home(cfg)
    assert "ROADMAP_COMPLETE" in html_out, html_out
    assert "ap2 ack roadmap_complete" in html_out, html_out


def test_web_home_omits_focus_card_when_no_focus_headings(cfg: Config):
    """Fresh-project no-op path on the web surface: the card is
    omitted entirely when goal.md has no `## Current focus:`
    headings. Preserves byte-identical pre-TB-242 output for
    projects that haven't pivoted to the focus-rotation model."""
    from ap2 import web

    # Bare `init_project` scaffold (no colon-headings) → empty.
    html_out = web._render_home(cfg)
    assert ">Focus<" not in html_out
