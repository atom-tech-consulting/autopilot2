"""TB-242 / TB-342: behavioral pinning for the axis-4 focus surface on
`ap2 status` (text + JSON) and the web home.

TB-226 (`b8a3...` parser + `focus_pointer.json`) and TB-237 (axis-4
e2e walk-away pin) shipped the multi-focus rotation machinery; TB-242
added pull-surface visibility on `ap2 status` / web home so the
operator could answer "what focus am I on, and how many remain?" on
demand. TB-342 then collapsed the multi-focus rotation pointer walk
into a single ideation-exhaustion detector (ideation never actually
scoped itself to the active focus, so the walk changed nothing about
what got proposed). The pull-surface contract this module pins moved
with the collapse:

  (1) `cmd_status` text: single-focus goal.md → `focus: <title>` (no
      position counter — TB-342 dropped `(N of M)` since the daemon no
      longer sequences foci).
  (2) `cmd_status` text: multi-focus goal.md → `focus: <title1>,
      <title2>, <title3>` (priority-ordered prose list, no position
      counter post-TB-342).
  (3) `cmd_status` text: parked-ideation state → the halt-state line
      "focus: parked — ideation exhausted; extend goal.md to resume,
      or `ap2 ack roadmap_complete` to dismiss this notice".
  (4) `cmd_status` JSON: `active_focus` block carries
      `titles` / `roadmap_complete` keys, and falls back to `null`
      when no `## Current focus:` headings exist (fresh-project no-op
      path).
  (5) `web._render_home` HTML: focus titles + halt state rendered
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
# TB-342: `goal.active_focus` retired with the rotation pointer walk.
_NAMES_FOR_DRIFT_GATE = (
    goal.read_focus_list,
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
    the supplied `focus_section`."""
    (cfg.project_root / "goal.md").write_text(
        _GOAL_MD_TEMPLATE.format(focus_section=focus_section)
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout."""
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


# ===========================================================================
# (1) Single-focus goal.md → text shows `focus: <title>`.
# ===========================================================================


def test_cli_status_single_focus_text(cfg: Config, capsys):
    """One `## Current focus:` heading → the text-render shows
    `focus:    <title>` with no position counter (TB-342 dropped the
    `(N of M)` display)."""
    from ap2.cli import cmd_status

    _write_goal_md(cfg, "## Current focus: bootstrap\n\nbody.\n\n")
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" in out
    assert "bootstrap" in out
    # TB-342: no position counter on any focus surface.
    assert "1 of 1" not in out
    assert "(1 of " not in out


# ===========================================================================
# (2) Multi-focus goal.md → text shows comma-separated priority list.
# ===========================================================================


def test_cli_status_multi_focus_text_shows_all_titles(cfg: Config, capsys):
    """Three `## Current focus:` headings → text-render shows all
    three titles in priority order, comma-separated. TB-342: the
    daemon no longer sequences foci; the list is the operator's
    intent."""
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
    assert "alpha, beta, gamma" in out, out
    # No position counter.
    assert "(1 of 3)" not in out
    assert "(2 of 3)" not in out


# ===========================================================================
# (3) Roadmap-complete halt → text shows the halt-state line with the
# resume/dismiss hint.
# ===========================================================================


def test_cli_status_roadmap_complete_text_shows_halt_line(
    cfg: Config, capsys,
):
    """`roadmap_complete_emitted=True` → text-render shows the
    `focus: parked — ideation exhausted` state line WITH the
    two-verb resume/dismiss nag (TB-340 / TB-342: ack dismisses, edit
    goal.md to resume — the pre-TB-342 `ap2 rewind-focus` verb went
    away with the rotation pointer walk)."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "parked" in out, out
    assert "ideation exhausted" in out, out
    # Nag NOT dismissed → the resume/dismiss hint is present.
    assert "ap2 update-goal" in out, out
    assert "ap2 ack roadmap_complete" in out, out
    # TB-342: rewind-focus is gone.
    assert "rewind-focus" not in out, out


def test_cli_status_roadmap_complete_nag_suppressed_after_dismiss(
    cfg: Config, capsys,
):
    """TB-340 surfacing-vs-state split: after the operator DISMISSES
    the notice, the text-render STILL shows the parked state line but
    suppresses the actionable resume/dismiss hint."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = True
    # Simulate the ack drain handler having dismissed THIS episode.
    pointer["roadmap_complete_ack_idx"] = 2
    goal.save_pointer(cfg, pointer)
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # State line preserved: still parked.
    assert "parked" in out, out
    assert "notice dismissed" in out, out
    # Actionable nag suppressed.
    assert "extend" not in out or "to resume" not in out, out


# ===========================================================================
# (4) JSON output includes the `active_focus` block.
# ===========================================================================


def test_cli_status_json_carries_active_focus_block(cfg: Config, capsys):
    """Multi-focus goal.md → JSON output has an `active_focus` object
    with the TB-342 contracted keys (`titles`, `roadmap_complete`).
    The pre-TB-342 `title` / `index` / `total` fields went away with
    the rotation pointer walk."""
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
    assert af["titles"] == ["alpha", "beta"]
    assert af["roadmap_complete"] is False


def test_cli_status_json_active_focus_null_when_no_focus_headings(
    cfg: Config, capsys,
):
    """Fresh-project no-op path: goal.md present but no
    `## Current focus:` headings → JSON exposes the key but with a
    `null` value."""
    from ap2.cli import cmd_status

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "active_focus" in payload
    assert payload["active_focus"] is None


def test_cli_status_json_active_focus_carries_roadmap_complete_flag(
    cfg: Config, capsys,
):
    """Halt state → JSON's `active_focus.roadmap_complete` is True."""
    from ap2.cli import cmd_status

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    af = payload["active_focus"]
    assert af is not None
    assert af["titles"] == ["alpha", "beta"]
    assert af["roadmap_complete"] is True


def test_cli_status_text_omits_focus_line_when_no_focus_headings(
    cfg: Config, capsys,
):
    """Fresh-project no-op path on the text surface: no
    `## Current focus:` heading → no `focus:` line."""
    from ap2.cli import cmd_status

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" not in out


def test_cli_status_text_omits_focus_line_when_goal_md_missing(
    cfg: Config, capsys,
):
    """goal.md doesn't exist → no `focus:` line."""
    from ap2.cli import cmd_status

    (cfg.project_root / "goal.md").unlink()
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "focus:" not in out


# ===========================================================================
# (5) Web home HTML renders the focus titles + halt state.
# ===========================================================================


def test_web_home_renders_focus_card_with_titles(cfg: Config):
    """Multi-focus goal.md → the home HTML includes all focus titles
    in a comma-separated priority list. TB-342: no position counter."""
    from ap2 import web

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n"
        "## Current focus: gamma\n\ngamma body.\n\n",
    )
    html_out = web._render_home(cfg)
    assert "alpha" in html_out
    assert "beta" in html_out
    assert "gamma" in html_out
    # Card uses a `Focus` header label so it's findable in the DOM.
    assert ">Focus<" in html_out, html_out
    # No `(N of M)` post-TB-342.
    assert "1 of 3" not in html_out
    assert "2 of 3" not in html_out


def test_web_home_renders_focus_card_single_focus(cfg: Config):
    """Single-focus goal.md → the home HTML includes the focus title
    and a `Focus` header label."""
    from ap2 import web

    _write_goal_md(cfg, "## Current focus: bootstrap\n\nbody.\n\n")
    html_out = web._render_home(cfg)
    assert "bootstrap" in html_out
    assert ">Focus<" in html_out
    assert "1 of 1" not in html_out


def test_web_home_renders_focus_card_halt_state(cfg: Config):
    """Halt state → the home HTML includes a `parked` marker and the
    `ap2 ack roadmap_complete` resume verb rendered as `<code>`."""
    from ap2 import web

    _write_goal_md(
        cfg,
        "## Current focus: alpha\n\nalpha body.\n\n"
        "## Current focus: beta\n\nbeta body.\n\n",
    )
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)
    events.append(cfg.events_file, "roadmap_complete", reason="exhausted")

    html_out = web._render_home(cfg)
    assert "parked" in html_out, html_out
    assert "ap2 ack roadmap_complete" in html_out, html_out


def test_web_home_omits_focus_card_when_no_focus_headings(cfg: Config):
    """Fresh-project no-op path on the web surface: the card is
    omitted entirely when goal.md has no `## Current focus:`
    headings."""
    from ap2 import web

    html_out = web._render_home(cfg)
    assert ">Focus<" not in html_out
