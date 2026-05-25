"""TB-226: behavioral pinning for the axis-4 focus-rotation surface.

Background (briefing's Why-now): goal.md L115-138 has carried the
design for multi-`## Current focus:` heading sequencing since the
end-to-end-automation pivot, but zero implementation. Without focus
rotation, walk-away time is bounded by the topmost focus's natural
exhaustion point — when one focus's gaps are addressed, ideation has
nothing valuable to propose until the operator manually rotates
goal.md, forcing intervention at exactly the moment the loop should
be most productive. Axis 4 closes the gap.

Behavioral cases pinned here:

  Parser:
    - happy path with zero / one / three `## Current focus:` headings
    - malformed `Progress signals:` sub-block (heading present, no
      bullets)
    - embedded code fences ` ``` ... ``` ` don't confuse bullet
      collection
    - nested `### Progress signals` sub-heading variant
    - non-string / empty input returns []
    - absent `Progress signals:` sub-block parses with
      `progress_signals_bullets=None` (the block is OPTIONAL per the
      TB-285 contract)

  Env knobs (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`,
  `AP2_FOCUS_AUTO_ADVANCE_DISABLED`):
    - default / override / invalid-value parse for each knob
    - empty-cycles clamp to [1, 20]

  Pointer state:
    - load round-trip after save
    - load of missing file returns the default-emit shape
    - load tolerates malformed JSON without crashing

  Advance heuristic (TB-283: empty-cycles is the sole signal):
    - empty-cycles fires when threshold reached
    - empty-cycles counter resets on `ideation_proposal_recorded`
    - a focus carrying `Progress signals:` bullets advances via the
      SAME empty-cycles signal (no separate LLM-judge path; the
      bullets are advisory ideation-prompt context only)
    - `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` short-circuits even
      when criteria are met

  Halt + ack:
    - all-foci-exhausted → `roadmap_complete` event + decisions-
      needed bullet + `goal.roadmap_exhausted()` True
    - operator_ack with `roadmap_complete` token in note clears
      the halt → `goal.roadmap_exhausted()` False

  Dispatch under roadmap_complete (TB-275):
    - the dispatch-path roadmap-exhaustion halt is REMOVED — a
      dispatchable Backlog task auto-promotes/dispatches even when
      `goal.roadmap_exhausted` is True (regression pin against the
      2026-05-20 TB-273/TB-274 freeze)
    - the ideation-trigger gate in `_maybe_ideate` is unchanged —
      `roadmap_complete` still parks ideation, just not dispatch

Mirrors the shape of `test_tb223_auto_approve.py` /
`test_tb224_token_caps.py` / `test_tb225_auto_unfreeze.py` —
direct unit pins on parser + env knobs + pointer + halt scan,
plus a board-level walk that exercises the TB-275 un-gated
dispatch.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ap2 import daemon, events, goal, tools
from ap2.config import Config
from ap2.init import init_project


# Direct references to the names the briefing's `## Verification`
# bullets / coverage-drift gates expect to see in this test file.
# Loaded at module top so a refactor that removes them surfaces
# cleanly on import. TB-283: the LLM-judge-effort knob was dropped
# from goal.py along with the LLM-judge advance path; the empty-
# cycles heuristic is now the sole signal.
_NAMES_FOR_DRIFT_GATE = (
    daemon._maybe_advance_focus,
    goal.parse_focus_list,
    goal.read_focus_list,
    goal.advance_empty_cycles_threshold,
    goal.auto_advance_disabled,
    goal.load_pointer,
    goal.save_pointer,
    goal.roadmap_exhausted,
)


# Env-knob name substrings the docs-drift / coverage-drift gates scan
# for (they assert each `AP2_*` env knob appears somewhere under
# `ap2/tests/`).  Keeping them grouped here makes the substring
# coverage obvious and self-documenting. TB-283: the prior
# judge-effort env knob was retired along with the LLM-judge advance
# path; the empty-cycles heuristic is the sole signal.
_ENV_KNOB_SUBSTRINGS = (
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES",
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED",
)


# Event-type strings the coverage-drift gate expects to see in this
# test file (substring match against the test corpus).
_EVENT_TYPE_SUBSTRINGS = (
    "focus_advanced",
    "roadmap_complete",
)


# ===========================================================================
# Fixtures
# ===========================================================================


_GOAL_MD_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away.\n\n"
    "{focus_section}"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


def _make_goal_md(focus_section: str) -> str:
    return _GOAL_MD_TEMPLATE.format(focus_section=focus_section)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Project root with the standard ap2 init layout + a stub goal.md.

    Tests that need a specific focus shape rewrite `goal.md` directly
    before invoking the unit under test.
    """
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(
        _make_goal_md("## Current focus: bootstrap\n\nBootstrap body.\n\n")
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


# ===========================================================================
# Parser unit pins
# ===========================================================================


def test_parse_focus_list_zero_headings():
    """Goal.md with no `## Current focus:` heading returns []. Pre-pivot
    fixtures and brand-new init scaffolds both hit this branch."""
    text = _make_goal_md("")
    assert goal.parse_focus_list(text) == []


def test_parse_focus_list_one_heading():
    """Single `## Current focus:` heading returns a one-element list.
    Pins today's most common shape (goal.md before the multi-focus
    rollout)."""
    text = _make_goal_md(
        "## Current focus: end-to-end automation\n\n"
        "Body of the active focus.\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    assert foci[0].title == "end-to-end automation"
    assert "Body of the active focus." in foci[0].body
    # TB-285: a `## Current focus:` heading with no `Progress signals:`
    # sub-block parses cleanly — the block is OPTIONAL.
    assert foci[0].progress_signals_bullets is None
    assert foci[0].has_progress_signals() is False


def test_parse_focus_list_three_headings():
    """Three sequential `## Current focus:` headings, mixed
    Progress-signals shapes. Pins the multi-focus walk."""
    text = _make_goal_md(
        "## Current focus: alpha\n\n"
        "Alpha body.\n\n"
        "Progress signals:\n"
        "- alpha bullet 1\n"
        "- alpha bullet 2\n\n"
        "## Current focus: beta\n\n"
        "Beta body with no Progress-signals block.\n\n"
        "## Current focus: gamma\n\n"
        "Gamma body.\n\n"
        "### Progress signals\n\n"
        "- gamma bullet 1\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert [f.title for f in foci] == ["alpha", "beta", "gamma"]
    assert foci[0].progress_signals_bullets == ["alpha bullet 1", "alpha bullet 2"]
    assert foci[1].progress_signals_bullets is None
    assert foci[1].has_progress_signals() is False
    assert foci[2].progress_signals_bullets == ["gamma bullet 1"]


def test_parse_focus_list_empty_progress_signals_block():
    """A `Progress signals:` heading with no following bullets returns
    an empty list (NOT None). The parser distinguishes "no block" from
    "empty block".
    """
    text = _make_goal_md(
        "## Current focus: alpha\n\n"
        "Alpha body.\n\n"
        "Progress signals:\n\n"
        "Some trailing prose.\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    assert foci[0].has_progress_signals() is True
    assert foci[0].progress_signals_bullets == []


def test_parse_focus_list_code_fence_skipped():
    """Bullets inside fenced ``` ... ``` code blocks don't get
    mistakenly collected as Progress-signals bullets."""
    text = _make_goal_md(
        "## Current focus: alpha\n\n"
        "Body with a code sample:\n\n"
        "```\n"
        "- this is shell output, not a Progress-signals bullet\n"
        "- neither is this\n"
        "```\n\n"
        "Progress signals:\n"
        "- real progress-signals bullet\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert foci[0].progress_signals_bullets == ["real progress-signals bullet"]


def test_parse_focus_list_legacy_done_when_not_accepted():
    """TB-285 hard cut: the legacy `Done when:` / `### Done when`
    heading is NOT accepted by the parser. A focus body that uses the
    old heading parses with `progress_signals_bullets=None` — the same
    shape as a focus with no sub-block at all.

    Per project norm (git history is the rollback substrate), no
    backcompat shim. The in-tree goal.md is updated in the same arc;
    external consumers do not exist."""
    text = _make_goal_md(
        "## Current focus: alpha\n\n"
        "Alpha body.\n\n"
        "Done when:\n"
        "- legacy bullet — must NOT be collected\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    assert foci[0].progress_signals_bullets is None
    assert foci[0].has_progress_signals() is False


def test_parse_focus_list_progress_signals_block_optional():
    """TB-285 contract pin: the `Progress signals:` sub-block is
    OPTIONAL. A focus heading with no sub-block parses with
    `progress_signals_bullets=None` and `has_progress_signals()`
    returning False — distinct from an empty-but-present block."""
    text = _make_goal_md(
        "## Current focus: alpha\n\n"
        "Just a body paragraph, no Progress-signals sub-block.\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    assert foci[0].progress_signals_bullets is None
    assert foci[0].has_progress_signals() is False


def test_parse_focus_list_handles_non_string():
    """Non-string / empty input returns []."""
    assert goal.parse_focus_list("") == []
    assert goal.parse_focus_list(None) == []  # type: ignore[arg-type]


def test_parse_focus_list_line_range():
    """`line_range` reports 1-indexed start/end of the heading + body
    in the source text. Useful for operator-facing diagnostics."""
    text = (
        "line 1\n"      # 1
        "line 2\n"      # 2
        "## Current focus: alpha\n"   # 3
        "body line\n"    # 4
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    start, end = foci[0].line_range
    assert start == 3
    assert end >= start


# ===========================================================================
# Env-knob unit pins
# ===========================================================================


def test_advance_empty_cycles_default(monkeypatch):
    """`AP2_FOCUS_ADVANCE_EMPTY_CYCLES` unset / empty → default 3."""
    monkeypatch.delenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", raising=False)
    assert goal.advance_empty_cycles_threshold() == 3

    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "")
    assert goal.advance_empty_cycles_threshold() == 3


def test_advance_empty_cycles_override(monkeypatch):
    """Operator can override via env var. Pins the override shape."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "5")
    assert goal.advance_empty_cycles_threshold() == 5


def test_advance_empty_cycles_clamps(monkeypatch):
    """Out-of-range values clamp to [1, 20]. Pins the safety floor."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "0")
    assert goal.advance_empty_cycles_threshold() == 1

    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "-50")
    assert goal.advance_empty_cycles_threshold() == 1

    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "999")
    assert goal.advance_empty_cycles_threshold() == 20


def test_advance_empty_cycles_invalid_falls_back(monkeypatch):
    """Non-int values fall back to the default."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "garbage")
    assert goal.advance_empty_cycles_threshold() == 3


def test_auto_advance_disabled_default(monkeypatch):
    """Default unset → False (auto-advance enabled)."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    assert goal.auto_advance_disabled() is False


def test_auto_advance_disabled_truthy(monkeypatch):
    """`1` / `true` / `yes` / `on` all parse as True."""
    for val in ("1", "true", "TRUE", "yes", "Yes", "on", "ON"):
        monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", val)
        assert goal.auto_advance_disabled() is True, f"failed for {val!r}"


def test_auto_advance_disabled_falsy(monkeypatch):
    """`0` / `false` / `no` / empty all parse as False."""
    for val in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", val)
        assert goal.auto_advance_disabled() is False, f"failed for {val!r}"


# TB-283: the prior judge-effort env-knob tests were retired along
# with the knob itself (deleted from `ap2/goal.py`). The LLM-judge
# advance path is gone; the empty-cycles heuristic is the sole signal.


# ===========================================================================
# Pointer state file round-trip
# ===========================================================================


def test_pointer_default_emit_when_missing(cfg):
    """No pointer file on disk → default shape at index 0."""
    p = goal.load_pointer(cfg)
    assert p["active_index"] == 0
    assert p["active_title"] == ""
    assert p["empty_cycles"] == 0
    assert p["exhausted_titles"] == []
    assert p["roadmap_complete_ack_idx"] is None
    assert p["roadmap_complete_emitted"] is False
    assert p["schema"] == goal.POINTER_SCHEMA_VERSION


def test_pointer_round_trip(cfg):
    """Save then load yields the same pointer (apart from the
    `updated_ts` stamp, which is overwritten on save)."""
    p = goal.load_pointer(cfg)
    p["active_index"] = 2
    p["active_title"] = "gamma"
    p["empty_cycles"] = 1
    p["exhausted_titles"] = ["alpha", "beta"]
    goal.save_pointer(cfg, p)

    loaded = goal.load_pointer(cfg)
    assert loaded["active_index"] == 2
    assert loaded["active_title"] == "gamma"
    assert loaded["empty_cycles"] == 1
    assert loaded["exhausted_titles"] == ["alpha", "beta"]
    assert loaded["updated_ts"]  # stamped


def test_pointer_load_tolerates_malformed_json(cfg):
    """Hand-mangled pointer file should default-emit, not crash."""
    goal.pointer_path(cfg).write_text("not json at all {")
    p = goal.load_pointer(cfg)
    assert p["active_index"] == 0
    assert p["schema"] == goal.POINTER_SCHEMA_VERSION


def test_pointer_load_tolerates_missing_keys(cfg):
    """An old pointer file without newer keys (forward-compat path)
    gets defaults filled in for the missing fields."""
    goal.pointer_path(cfg).write_text(
        '{"active_index": 1, "active_title": "beta"}'
    )
    p = goal.load_pointer(cfg)
    assert p["active_index"] == 1
    assert p["active_title"] == "beta"
    # Missing keys → defaults.
    assert p["empty_cycles"] == 0
    assert p["exhausted_titles"] == []
    assert p["roadmap_complete_emitted"] is False


# ===========================================================================
# Advance pass — heuristic fallback
# ===========================================================================


def _write_goal_with_foci(cfg: Config, *titles: str) -> None:
    """Write a goal.md with the given titles in `## Current focus:`
    headings, each carrying a bare body and NO `Progress signals:`
    block (the empty-cycles heuristic fires regardless of the
    sub-block's presence post-TB-283)."""
    sections = "".join(
        f"## Current focus: {t}\n\nBody for {t}.\n\n" for t in titles
    )
    (cfg.project_root / "goal.md").write_text(_make_goal_md(sections))


def _write_goal_with_progress_signals(cfg: Config, title: str, bullets: list[str]) -> None:
    """Write a single-focus goal.md with an explicit
    `Progress signals:` sub-block (advisory ideation-prompt context;
    does NOT gate the advance pass post-TB-283)."""
    bullet_block = "\n".join(f"- {b}" for b in bullets)
    body = (
        f"## Current focus: {title}\n\n"
        f"Body for {title}.\n\n"
        f"Progress signals:\n{bullet_block}\n\n"
    )
    (cfg.project_root / "goal.md").write_text(_make_goal_md(body))


def _emit_ideation_empty(cfg: Config) -> None:
    """Append an `ideation_empty_board` event so the heuristic counter
    can see it."""
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)


def test_empty_cycles_heuristic_advance(cfg, monkeypatch):
    """Active focus with no Done-when block: advance fires after N
    consecutive `ideation_empty_board` events with no proposals
    against the focus."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")

    # Three ideation-empty cycles → threshold reached.
    for _ in range(3):
        _emit_ideation_empty(cfg)

    # Run the advance pass.
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 1, f"pointer didn't advance: {pointer}"
    assert pointer["active_title"] == "beta"
    assert "alpha" in pointer["exhausted_titles"]

    # `focus_advanced` event emitted with the right shape.
    tail = events.tail(cfg.events_file, 50)
    advanced = [e for e in tail if e.get("type") == "focus_advanced"]
    assert advanced, "no focus_advanced event"
    e = advanced[-1]
    assert e["from"] == "alpha"
    assert e["to"] == "beta"
    assert e["trigger"] == "empty_cycles_heuristic"
    assert e["new_index"] == 1
    assert e["total_foci"] == 3


def test_empty_cycles_heuristic_below_threshold(cfg, monkeypatch):
    """Below-threshold empty-cycle count does NOT advance."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta")

    # Only two empty cycles — below the threshold.
    _emit_ideation_empty(cfg)
    _emit_ideation_empty(cfg)

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0
    # The counter is synced for observability.
    assert pointer["empty_cycles"] == 2


def test_empty_cycles_resets_on_proposal(cfg, monkeypatch):
    """`ideation_proposal_recorded` resets the empty-cycles counter:
    even with 5 prior empty cycles, a single recorded proposal pushes
    the count back to 0 until new empty cycles accumulate."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta")

    for _ in range(5):
        _emit_ideation_empty(cfg)
    # A proposal landed → counter resets.
    events.append(cfg.events_file, "ideation_proposal_recorded", task="TB-1")
    # One more empty cycle after the reset; well below threshold now.
    _emit_ideation_empty(cfg)

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0, "should not have advanced"
    assert pointer["empty_cycles"] == 1


# ===========================================================================
# TB-283: the LLM-judge advance path is gone. Empty-cycles is the sole
# advance signal (see `ap2/focus_advance.py`). Even when a focus carries
# explicit `Progress signals:` bullets (TB-285 rename of the prior
# `Done when:` sub-block), the daemon advances solely off the
# heuristic — the bullets remain in goal.md as ideation-prompt context
# but no longer gate the pointer. The judge-path tests were removed
# along with the judge itself.
# ===========================================================================


def test_progress_signals_focus_advances_via_empty_cycles_only(cfg, monkeypatch):
    """TB-283 + TB-285 pin: a focus that carries `Progress signals:`
    bullets advances via the SAME empty-cycles heuristic as any other
    focus. The bullets are advisory ideation-prompt context; they no
    longer trip a separate LLM-judge advance path."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    (cfg.project_root / "goal.md").write_text(
        _make_goal_md(
            "## Current focus: alpha\n\n"
            "Body.\n\n"
            "Progress signals:\n"
            "- signal 1\n"
            "- signal 2\n\n"
            "## Current focus: beta\n\n"
            "Beta body.\n\n"
        )
    )

    # Three empty ideation cycles against alpha → advance via the
    # heuristic, regardless of the `Progress signals:` block's presence.
    for _ in range(3):
        _emit_ideation_empty(cfg)

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 1
    assert pointer["active_title"] == "beta"

    advanced = [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "focus_advanced"
    ]
    assert advanced
    # Sole trigger value the daemon emits post-TB-283.
    assert advanced[-1]["trigger"] == "empty_cycles_heuristic"


# ===========================================================================
# Kill-switch
# ===========================================================================


def test_auto_advance_disabled_short_circuits(cfg, monkeypatch):
    """`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` blocks advance even when
    the empty-cycles threshold trips. A decisions-needed bullet
    surfaces so the operator advances manually."""
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "1")
    monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    _emit_ideation_empty(cfg)

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0, "kill-switch should have blocked"

    # Decisions-needed bullet landed.
    ideation_state = (
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    assert ideation_state.exists()
    text = ideation_state.read_text()
    assert "Decisions needed from operator" in text
    assert "AP2_FOCUS_AUTO_ADVANCE_DISABLED" in text


# ===========================================================================
# Roadmap-complete halt + ack
# ===========================================================================


def test_roadmap_complete_event_on_exhaustion(cfg, monkeypatch):
    """When the pointer crosses the last focus, the next advance
    pass emits `roadmap_complete` once + a decisions-needed bullet,
    and `goal.roadmap_exhausted` returns True."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    # Pre-position pointer past the single focus.
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)

    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    # Event landed.
    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1
    assert rc[0]["exhausted_count"] == 1
    assert rc[0]["trigger"] == "pointer_past_last"

    # Decisions-needed bullet appended.
    ideation_state = (
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    assert ideation_state.exists()
    assert "Roadmap complete" in ideation_state.read_text()

    # Halt active.
    assert goal.roadmap_exhausted(cfg) is True

    # Re-running the advance pass should NOT re-emit the event.
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))
    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, "roadmap_complete should not re-emit"


def test_ack_clears_roadmap_complete_halt(cfg, monkeypatch):
    """Operator ack with `roadmap_complete` token in the note clears
    the halt. `goal.roadmap_exhausted` returns False after the ack."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)

    # Emit the roadmap_complete event (would normally come from
    # `_maybe_advance_focus`).
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))
    assert goal.roadmap_exhausted(cfg) is True

    # Operator acks via the standard ack pipeline. The drain side
    # emits an `operator_ack` event with the note carrying the token.
    events.append(
        cfg.events_file,
        "operator_ack",
        note="roadmap_complete: extended the roadmap with axis 5",
    )
    assert goal.roadmap_exhausted(cfg) is False


def test_ack_without_token_does_not_clear(cfg, monkeypatch):
    """Ack without the `roadmap_complete` token does NOT clear the
    halt — the daemon scans for the specific token only (mirrors
    `_auto_approve_paused`'s `auto_approve_unfreeze` shape)."""
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))
    assert goal.roadmap_exhausted(cfg) is True

    events.append(
        cfg.events_file,
        "operator_ack",
        note="some unrelated decision",
    )
    assert goal.roadmap_exhausted(cfg) is True


# ===========================================================================
# Dispatch-path: roadmap_complete is NOT a dispatch gate (TB-275)
# ===========================================================================


def test_dispatch_path_no_roadmap_halt_in_source():
    """TB-275 regression pin (source-level): the dispatch path no
    longer carries a `goal.roadmap_exhausted(cfg)` conjunction.

    The TB-226 dispatch halt that used to live next to
    `board.next_dispatchable("Backlog")` froze operator-added
    (`ap2 add`) and operator-approved (`ap2 approve`) Backlog tasks
    behind a roadmap-state gate that has nothing to do with whether
    the task is dispatchable — manufacturing an intervention for
    work the operator had already greenlit. The halt is removed; the
    only roadmap-state gate that remains is the ideation-trigger gate
    in `_maybe_ideate`. Pinning the literal conjunction's absence in
    `daemon.py` ensures a re-introduction surfaces immediately.
    """
    daemon_src = Path(daemon.__file__).read_text()
    assert "and goal.roadmap_exhausted(cfg)" not in daemon_src, (
        "TB-275: the dispatch-path roadmap-exhaustion halt conjunction "
        "is back in daemon.py. roadmap_complete must gate the ideation "
        "trigger only (see `ap2/ideation.py`), never task dispatch."
    )


def test_dispatch_promotes_when_roadmap_exhausted(cfg, monkeypatch):
    """TB-275 regression pin (behavioral): when the roadmap is
    exhausted AND a dispatchable Backlog task is present, the daemon
    auto-promotes/dispatches it (no halt).

    The TB-226 dispatch halt that previously short-circuited
    `backlog = None` whenever `goal.roadmap_exhausted` returned True
    is gone. This test would have failed pre-fix: with the gate live,
    `move_to_ready` never fired and the seeded Backlog task stayed in
    Backlog. Post-fix the task auto-promotes to Ready (then onward to
    Active via `run_task`) on the same tick, regardless of
    roadmap-complete state. This exact freeze hit production on
    2026-05-20 with TB-273 (operator-approved) and TB-274
    (operator-added) sitting frozen in Backlog for hours behind a
    roadmap_complete that fired at 12:20.
    """
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    goal.save_pointer(cfg, pointer)
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))

    # Pre-condition: roadmap is exhausted (ideation trigger gate
    # would skip on the next ideation cycle — but THIS test exercises
    # the dispatch path, which must remain un-gated).
    assert goal.roadmap_exhausted(cfg) is True

    # Seed a dispatchable Backlog task — same shape `ap2 add` /
    # operator-approved `ap2 approve` produces (no `@blocked:review`,
    # no auto_approved provenance — pure operator-originated work).
    from ap2.board import Board
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-999",
        title="dispatchable Backlog task under roadmap_complete",
    )
    board.save()

    # Stub _tick internals: bypass operator-queue drain, pipeline
    # sweep, focus-advance (already exercised above), ideation, cron,
    # and `run_task` itself. We're pinning the auto-promote step
    # only — that `move_to_ready` fires under a halted roadmap.
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda cfg: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_async)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)
    monkeypatch.setattr(daemon, "_maybe_advance_focus", _noop_async)
    from ap2 import ideation as _ideation
    monkeypatch.setattr(_ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(_ideation, "force_ideate", _noop_async)
    monkeypatch.setattr(daemon, "load_jobs", lambda path: [])
    monkeypatch.setattr(daemon, "run_task", _noop_async)

    class _NoopSDK:
        class ClaudeAgentOptions:
            def __init__(self, **kw):
                self.kw = kw

    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    # Post-condition: TB-999 was auto-promoted to Ready (or already
    # moved onward — the assertion accepts both since `run_task` is
    # stubbed and won't move it back).
    from ap2.board import Board as _Board
    board = _Board.load(cfg.tasks_file)
    loc = board.find("TB-999")
    assert loc is not None, "TB-999 vanished from the board"
    assert loc[0] in ("Ready", "Active"), (
        f"TB-275 regression: a dispatchable Backlog task must auto-"
        f"promote even when `goal.roadmap_exhausted` is True; "
        f"TB-999 is still in section={loc[0]!r}"
    )

    # And the `backlog_auto_promoted` audit event landed.
    tail = events.tail(cfg.events_file, 200)
    promoted = [
        e for e in tail
        if e.get("type") == "backlog_auto_promoted"
        and e.get("task") == "TB-999"
    ]
    assert len(promoted) == 1, (
        f"TB-275 regression: `backlog_auto_promoted` must fire for "
        f"TB-999 under a roadmap_complete halt; events: "
        f"{[e['type'] for e in tail]}"
    )


def test_ideation_trigger_gate_still_intact(cfg, monkeypatch):
    """TB-275 sibling pin: removing the dispatch halt must NOT
    accidentally remove the ideation-trigger gate. `_maybe_ideate`
    still emits `ideation_skipped reason=roadmap_complete` when
    `goal.roadmap_exhausted` is True.

    This is a structural cross-check: the fix removes ONE gate
    (dispatch) and KEEPS the other (ideation). The TB-246 test
    module (`test_tb246_ideation_roadmap_complete_gate.py`)
    exercises the gate end-to-end; here we just pin that the
    canonical predicate still appears in `_maybe_ideate` so a future
    refactor doesn't silently strip both gates.
    """
    from ap2 import ideation as _ideation
    ideation_src = Path(_ideation.__file__).read_text()
    assert "_goal.roadmap_exhausted(cfg)" in ideation_src or \
        "goal.roadmap_exhausted(cfg)" in ideation_src, (
        "TB-275: the ideation-trigger roadmap-complete gate has been "
        "removed from `_maybe_ideate`. The fix removes ONLY the "
        "dispatch halt; the ideation gate is the correct (sole) "
        "mechanism for `stop proposing when the roadmap is exhausted`."
    )
    # And the skip event's reason string is still `roadmap_complete`
    # — a refactor that renames the reason would break operator
    # tooling that greps `events.jsonl`.
    assert 'reason="roadmap_complete"' in ideation_src or \
        "reason='roadmap_complete'" in ideation_src, (
        "TB-275: the `ideation_skipped reason=roadmap_complete` "
        "event shape changed in `_maybe_ideate`."
    )


# ===========================================================================
# Roadmap extension flow — resume after operator extends the foci list
# ===========================================================================


def test_pointer_resets_on_roadmap_extension(cfg, monkeypatch):
    """When the operator extends the roadmap (adds new
    `## Current focus:` headings via `ap2 update-goal`), the
    `reset_pointer_on_roadmap_extension` helper snaps the
    `active_index` to the first newly-added focus."""
    _write_goal_with_foci(cfg, "alpha")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1  # past the single focus
    pointer["exhausted_titles"] = ["alpha"]
    pointer["roadmap_complete_emitted"] = True
    goal.save_pointer(cfg, pointer)

    # Operator extends.
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")
    foci = goal.read_focus_list(cfg)
    new_pointer = goal.reset_pointer_on_roadmap_extension(cfg, foci)
    assert new_pointer["active_index"] == 1
    assert new_pointer["active_title"] == "beta"
    assert new_pointer["empty_cycles"] == 0
    assert new_pointer["roadmap_complete_emitted"] is False


# ===========================================================================
# Smoke: the empty-focus-list / missing-goal.md paths don't crash
# ===========================================================================


def test_advance_no_foci_no_op(cfg):
    """No `## Current focus:` headings in goal.md → advance pass is
    a no-op. Pre-pivot fixtures hit this branch."""
    (cfg.project_root / "goal.md").write_text(_make_goal_md(""))
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))
    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0
    assert goal.roadmap_exhausted(cfg) is False


def test_advance_missing_goal_md_no_op(tmp_path: Path):
    """Missing goal.md → advance pass is a no-op (fresh project
    before `ap2 init` writes a goal.md template)."""
    init_project(tmp_path)
    (tmp_path / "goal.md").unlink()
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    asyncio.run(daemon._maybe_advance_focus(cfg, sdk=None))
    pointer = goal.load_pointer(cfg)
    assert pointer["active_index"] == 0
    assert goal.roadmap_exhausted(cfg) is False


# ===========================================================================
# Drift-gate substring anchors — ensure the env knobs / event types /
# helper names this work introduced all carry a substring reference
# in this test file. Mirrors the `_NAMES_FOR_DRIFT_GATE` /
# `_ENV_KNOB_SUBSTRINGS` / `_EVENT_TYPE_SUBSTRINGS` declarations at
# the top of the module.
# ===========================================================================


def test_drift_gate_anchors_present():
    """All env-knob / event-type substrings this work introduced are
    referenced in this test module. Pure self-check; mirrors the
    coverage-drift gate's substring-presence check at the test-corpus
    level."""
    text = Path(__file__).read_text()
    for knob in _ENV_KNOB_SUBSTRINGS:
        assert knob in text, f"env knob {knob} missing from test file"
    for evt in _EVENT_TYPE_SUBSTRINGS:
        assert evt in text, f"event type {evt} missing from test file"
    # Spot-check the helper names too.
    assert "parse_focus_list" in text
    assert "_maybe_advance_focus" in text
    assert "roadmap_exhausted" in text
