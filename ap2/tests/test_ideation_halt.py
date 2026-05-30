"""TB-226 / TB-342 / TB-345: behavioral pinning for the ideation-halt
surface (formerly the focus_advance component, now the core
`ap2/ideation_halt.py` module).

Background: goal.md carries the design for multi-`## Current focus:`
headings as an operator-authored priority/intent surface. TB-226
shipped the parser + the runtime pointer + a rotation pointer walk.
TB-342 collapsed the rotation theatre into a single
ideation-exhaustion detector: the daemon counts consecutive empty
ideation cycles since the most recent `goal_updated` event and emits
`roadmap_complete` once when the threshold trips. TB-345 then merged
that detector out of the `focus_advance` component into the core
`ap2/ideation_halt.py` module and renamed the entry point
(`maybe_halt_on_exhaustion(cfg)`) + the two operator knobs to the
`AP2_IDEATION_HALT_*` namespace (keeping the old `AP2_FOCUS_*` names as
deprecated back-compat aliases). Multi-focus headings remain
expressive prose; the daemon does not sequence them.

Behavioral cases pinned here:

  Parser (unchanged): happy path with zero / one / three headings,
    malformed / empty / nested Progress-signals shapes, code fences.

  Env knobs (`AP2_IDEATION_HALT_EMPTY_CYCLES`,
  `AP2_IDEATION_HALT_DISABLED`): default / override / invalid-value
    parse + empty-cycles clamp to [1, 20], all via the core
    `cfg.get_core_value(...)` resolution path. Plus a back-compat pin
    that the DEPRECATED `AP2_FOCUS_AUTO_ADVANCE_DISABLED` /
    `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` aliases still resolve through to
    `core.ideation_halt_*`.

  Pointer state (slimmed schema), exhaustion detector, halt + ack
  semantics, dispatch un-gating (TB-275), resume on goal_updated —
  all unchanged in behavior, only the call site renamed.

Mirrors the shape of `test_tb225_auto_unfreeze.py` — direct unit pins
on parser + env knobs + pointer + detector, plus a board-level walk
that exercises the TB-275 un-gated dispatch.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ap2 import daemon, events, goal, ideation_halt, tools
from ap2.config import Config
from ap2.init import init_project


# Direct references to the names the briefing's `## Verification`
# bullets / coverage-drift gates expect to see in this test file.
# Loaded at module top so a refactor that removes them surfaces
# cleanly on import.
_NAMES_FOR_DRIFT_GATE = (
    ideation_halt.maybe_halt_on_exhaustion,
    ideation_halt._ideation_halt_empty_cycles_threshold,
    ideation_halt._ideation_halt_disabled,
    ideation_halt._consecutive_empty_ideation_cycles,
    goal.parse_focus_list,
    goal.read_focus_list,
    goal.load_pointer,
    goal.save_pointer,
    goal.roadmap_exhausted,
    goal.reset_pointer_on_goal_updated,
)


# Env-knob name substrings the docs-drift / coverage-drift gates scan
# for. Both the new canonical names AND the deprecated aliases are
# named here so the back-compat surface stays auditable from tests.
_ENV_KNOB_SUBSTRINGS = (
    "AP2_IDEATION_HALT_EMPTY_CYCLES",
    "AP2_IDEATION_HALT_DISABLED",
    # Deprecated back-compat aliases (TB-345).
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES",
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED",
)


# Event-type strings the coverage-drift gate expects to see in this
# test file (substring match against the test corpus).
_EVENT_TYPE_SUBSTRINGS = (
    "roadmap_complete",
    "goal_updated",
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
    """Single `## Current focus:` heading returns a one-element list."""
    text = _make_goal_md(
        "## Current focus: end-to-end automation\n\n"
        "Body of the active focus.\n\n"
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    assert foci[0].title == "end-to-end automation"
    assert "Body of the active focus." in foci[0].body
    assert foci[0].progress_signals_bullets is None
    assert foci[0].has_progress_signals() is False


def test_parse_focus_list_three_headings():
    """Three sequential `## Current focus:` headings, mixed
    Progress-signals shapes."""
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
    an empty list (NOT None)."""
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
    heading is NOT accepted by the parser."""
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
    OPTIONAL."""
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
    """`line_range` reports 1-indexed start/end of the heading + body."""
    text = (
        "line 1\n"
        "line 2\n"
        "## Current focus: alpha\n"
        "body line\n"
    )
    foci = goal.parse_focus_list(text)
    assert len(foci) == 1
    start, end = foci[0].line_range
    assert start == 3
    assert end >= start


# ===========================================================================
# Env-knob unit pins (TB-345 — read via `cfg.get_core_value(...)`)
# ===========================================================================


def test_ideation_halt_empty_cycles_default(cfg, monkeypatch):
    """`AP2_IDEATION_HALT_EMPTY_CYCLES` unset / empty → schema default 3."""
    monkeypatch.delenv("AP2_IDEATION_HALT_EMPTY_CYCLES", raising=False)
    monkeypatch.delenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", raising=False)
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 3

    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 3


def test_ideation_halt_empty_cycles_override(cfg, monkeypatch):
    """Operator can override via the canonical env var."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "5")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 5


def test_ideation_halt_empty_cycles_clamps(cfg, monkeypatch):
    """Out-of-range values clamp to [1, 20]."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "0")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 1

    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "-50")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 1

    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "999")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 20


def test_ideation_halt_empty_cycles_invalid_falls_back(cfg, monkeypatch):
    """Non-int values fall back to the default."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "garbage")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 3


def test_ideation_halt_disabled_default(cfg, monkeypatch):
    """Default unset → False (auto-halt enabled)."""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.delenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", raising=False)
    assert ideation_halt._ideation_halt_disabled(cfg) is False


def test_ideation_halt_disabled_truthy(cfg, monkeypatch):
    """`1` / `true` / `yes` / `on` all parse as True."""
    for val in ("1", "true", "TRUE", "yes", "Yes", "on", "ON"):
        monkeypatch.setenv("AP2_IDEATION_HALT_DISABLED", val)
        assert ideation_halt._ideation_halt_disabled(cfg) is True, f"failed for {val!r}"


def test_ideation_halt_disabled_falsy(cfg, monkeypatch):
    """`0` / `false` / `no` / empty all parse as False."""
    for val in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("AP2_IDEATION_HALT_DISABLED", val)
        assert ideation_halt._ideation_halt_disabled(cfg) is False, f"failed for {val!r}"


# --- Deprecated back-compat alias pins (TB-345) ----------------------------


def test_deprecated_alias_disables_halt(cfg, monkeypatch):
    """The DEPRECATED `AP2_FOCUS_AUTO_ADVANCE_DISABLED` alias still
    disables the halt — it resolves through `FLAT_TO_SECTIONED` to the
    same `core.ideation_halt_disabled` key the canonical name maps to,
    so a stale operator env keeps working for one release."""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    assert ideation_halt._ideation_halt_disabled(cfg) is True
    # And it actually short-circuits the detector (see kill-switch test
    # below for the end-to-end behavioral pin).


def test_deprecated_alias_empty_cycles_threshold(cfg, monkeypatch):
    """The DEPRECATED `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` alias still
    resolves through to `core.ideation_halt_empty_cycles`."""
    monkeypatch.delenv("AP2_IDEATION_HALT_EMPTY_CYCLES", raising=False)
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "7")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 7


def test_canonical_wins_over_deprecated_alias(cfg, monkeypatch):
    """When BOTH the canonical and the deprecated name are set, the
    canonical name wins (it precedes the alias in `FLAT_TO_SECTIONED`
    iteration order)."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "4")
    monkeypatch.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "9")
    assert ideation_halt._ideation_halt_empty_cycles_threshold(cfg) == 4


# ===========================================================================
# Pointer state file round-trip (slimmed schema)
# ===========================================================================


def test_pointer_default_emit_when_missing(cfg):
    """No pointer file on disk → default shape (slimmed schema)."""
    p = goal.load_pointer(cfg)
    assert p["empty_cycles"] == 0
    assert p["roadmap_complete_ack_idx"] is None
    assert p["roadmap_complete_emitted"] is False
    assert p["schema"] == goal.POINTER_SCHEMA_VERSION
    # The pre-TB-342 rotation pointer fields are gone.
    assert "active_index" not in p
    assert "active_title" not in p
    assert "exhausted_titles" not in p


def test_pointer_round_trip(cfg):
    """Save then load yields the same pointer (apart from the
    `updated_ts` stamp, which is overwritten on save)."""
    p = goal.load_pointer(cfg)
    p["empty_cycles"] = 2
    p["roadmap_complete_emitted"] = True
    p["roadmap_complete_ack_idx"] = 1
    goal.save_pointer(cfg, p)

    loaded = goal.load_pointer(cfg)
    assert loaded["empty_cycles"] == 2
    assert loaded["roadmap_complete_emitted"] is True
    assert loaded["roadmap_complete_ack_idx"] == 1
    assert loaded["updated_ts"]  # stamped


def test_pointer_load_tolerates_malformed_json(cfg):
    """Hand-mangled pointer file should default-emit, not crash."""
    goal.pointer_path(cfg).write_text("not json at all {")
    p = goal.load_pointer(cfg)
    assert p["empty_cycles"] == 0
    assert p["schema"] == goal.POINTER_SCHEMA_VERSION


def test_pointer_load_tolerates_missing_keys(cfg):
    """An old pointer file without newer keys (forward-compat path)
    gets defaults filled in for the missing fields. Legacy rotation
    fields in an old pointer file are silently dropped."""
    goal.pointer_path(cfg).write_text(
        '{"empty_cycles": 1, "roadmap_complete_emitted": true, '
        '"active_index": 99, "active_title": "stale", '
        '"exhausted_titles": ["stale"]}'
    )
    p = goal.load_pointer(cfg)
    assert p["empty_cycles"] == 1
    assert p["roadmap_complete_emitted"] is True
    # Missing keys → defaults.
    assert p["roadmap_complete_ack_idx"] is None
    # Legacy rotation fields are filtered out on load.
    assert "active_index" not in p
    assert "active_title" not in p
    assert "exhausted_titles" not in p


# ===========================================================================
# Exhaustion detector — empty-cycles is the sole halt signal.
# ===========================================================================


def _write_goal_with_foci(cfg: Config, *titles: str) -> None:
    """Write a goal.md with the given titles in `## Current focus:`
    headings, each carrying a bare body and NO `Progress signals:`
    block."""
    sections = "".join(
        f"## Current focus: {t}\n\nBody for {t}.\n\n" for t in titles
    )
    (cfg.project_root / "goal.md").write_text(_make_goal_md(sections))


def _write_goal_with_progress_signals(cfg: Config, title: str, bullets: list[str]) -> None:
    """Write a single-focus goal.md with an explicit
    `Progress signals:` sub-block."""
    bullet_block = "\n".join(f"- {b}" for b in bullets)
    body = (
        f"## Current focus: {title}\n\n"
        f"Body for {title}.\n\n"
        f"Progress signals:\n{bullet_block}\n\n"
    )
    (cfg.project_root / "goal.md").write_text(_make_goal_md(body))


def _emit_ideation_empty(cfg: Config) -> None:
    """Append one full empty ideation cycle so the counter can see it:
    `ideation_empty_board` (entry) + `ideation_complete` (exit) with no
    `ideation_proposal_recorded` in between (TB-292 cycle-grouped)."""
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty cycle")


def _emit_ideation_productive(cfg: Config, *, task: str = "TB-X") -> None:
    """Append one full productive ideation cycle: entry + proposal +
    exit. Resets the empty-cycles counter."""
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_proposal_recorded", task=task)
    events.append(cfg.events_file, "ideation_complete", summary=f"productive cycle ({task})")


def test_empty_cycles_threshold_emits_roadmap_complete(cfg, monkeypatch):
    """Threshold reached → `roadmap_complete` fires once with
    `trigger=empty_cycles_heuristic`, `roadmap_complete_emitted=True`,
    and `roadmap_exhausted` flips to True."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")

    # Three empty cycles → threshold tripped.
    for _ in range(3):
        _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    # Event landed exactly once.
    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1
    assert rc[0]["exhausted_count"] == 3
    assert rc[0]["trigger"] == "empty_cycles_heuristic"

    # Pointer flag flipped.
    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is True
    assert pointer["roadmap_complete_ack_idx"] is None
    # Empty-cycles forensic counter is synced.
    assert pointer["empty_cycles"] == 3

    # Halt active via the canonical predicate.
    assert goal.roadmap_exhausted(cfg) is True

    # Re-running the pass should NOT re-emit.
    ideation_halt.maybe_halt_on_exhaustion(cfg)
    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1, "roadmap_complete should not re-emit"


def test_empty_cycles_below_threshold_no_halt(cfg, monkeypatch):
    """Below-threshold count does NOT emit `roadmap_complete`."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta")

    # Two empty cycles — below the threshold.
    _emit_ideation_empty(cfg)
    _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False
    # The counter is synced for observability.
    assert pointer["empty_cycles"] == 2
    assert goal.roadmap_exhausted(cfg) is False
    # No event fired.
    tail = events.tail(cfg.events_file, 50)
    assert not [e for e in tail if e.get("type") == "roadmap_complete"]


def test_empty_cycles_resets_on_proposal(cfg, monkeypatch):
    """A productive ideation cycle resets the empty-cycles counter to
    0, even after several prior empty cycles."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta")

    for _ in range(5):
        _emit_ideation_empty(cfg)
    # A productive cycle → counter resets to 0 at this cycle's exit.
    _emit_ideation_productive(cfg, task="TB-1")
    # One more empty cycle after the reset; well below threshold now.
    _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False
    assert pointer["empty_cycles"] == 1


def test_goal_updated_resets_counter_window(cfg, monkeypatch):
    """The cutoff: `_consecutive_empty_ideation_cycles` resets at the
    most recent `goal_updated` event. Pre-edit empty cycles do not
    count against the post-edit runway, so a goal.md edit followed by
    a single empty cycle leaves the counter at 1 (not 4)."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    _write_goal_with_foci(cfg, "alpha", "beta")

    # Three empty cycles before the operator extends goal.md.
    for _ in range(3):
        _emit_ideation_empty(cfg)
    # Operator extends goal.md → the drain emits `goal_updated`.
    events.append(cfg.events_file, "goal_updated", reason="extension")
    # One post-edit empty cycle.
    _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    # Only the post-`goal_updated` cycle counts.
    assert pointer["empty_cycles"] == 1
    assert pointer["roadmap_complete_emitted"] is False
    assert goal.roadmap_exhausted(cfg) is False


def test_progress_signals_focus_halts_via_empty_cycles_only(cfg, monkeypatch):
    """TB-283 + TB-285 pin: a focus that carries `Progress signals:`
    bullets halts via the SAME empty-cycles heuristic — the bullets
    are advisory ideation-prompt context only."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "3")
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    _write_goal_with_progress_signals(
        cfg, "alpha", ["signal 1", "signal 2"],
    )

    for _ in range(3):
        _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is True

    rc = [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") == "roadmap_complete"
    ]
    assert rc
    assert rc[-1]["trigger"] == "empty_cycles_heuristic"


# ===========================================================================
# Kill-switch
# ===========================================================================


def test_halt_disabled_short_circuits(cfg, monkeypatch):
    """`AP2_IDEATION_HALT_DISABLED=1` blocks the halt even when the
    empty-cycles threshold trips. A decisions-needed bullet surfaces so
    the operator can halt manually (by editing goal.md)."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    monkeypatch.setenv("AP2_IDEATION_HALT_DISABLED", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False, (
        "kill-switch should have blocked the halt"
    )
    # No event landed.
    tail = events.tail(cfg.events_file, 50)
    assert not [e for e in tail if e.get("type") == "roadmap_complete"]

    # Decisions-needed bullet landed.
    ideation_state = (
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    assert ideation_state.exists()
    text = ideation_state.read_text()
    assert "Decisions needed from operator" in text
    assert "AP2_IDEATION_HALT_DISABLED" in text


def test_deprecated_kill_switch_alias_short_circuits(cfg, monkeypatch):
    """Back-compat: the DEPRECATED `AP2_FOCUS_AUTO_ADVANCE_DISABLED`
    alias still blocks the halt end-to-end (resolves to
    `core.ideation_halt_disabled`)."""
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False, (
        "deprecated kill-switch alias should have blocked the halt"
    )
    tail = events.tail(cfg.events_file, 50)
    assert not [e for e in tail if e.get("type") == "roadmap_complete"]


# ===========================================================================
# Roadmap-complete halt + ack semantics (TB-340)
# ===========================================================================


def test_ack_does_not_clear_roadmap_complete_gate(cfg, monkeypatch):
    """TB-340: an operator ack with the `roadmap_complete` token does
    NOT clear the gate. The gate is `roadmap_complete_emitted`; the
    ack only DISMISSES the operator nag."""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_ideation_empty(cfg)

    ideation_halt.maybe_halt_on_exhaustion(cfg)
    assert goal.roadmap_exhausted(cfg) is True

    # Operator acks via the standard ack pipeline. The drain emits an
    # `operator_ack` event with the note carrying the token.
    events.append(
        cfg.events_file,
        "operator_ack",
        note="roadmap_complete: dismissing the notice",
    )
    # TB-340: the gate is `roadmap_complete_emitted`; the ack does NOT
    # flip it. The pointer flag is still set.
    assert goal.roadmap_exhausted(cfg) is True


def test_ack_without_token_does_not_clear(cfg, monkeypatch):
    """Ack without the `roadmap_complete` token does NOT clear the
    gate. (Post-TB-340 NO ack clears the gate — the predicate is the
    pointer flag — so this is the same verdict as the token-bearing
    ack; the test is retained as a regression guard.)"""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_ideation_empty(cfg)
    ideation_halt.maybe_halt_on_exhaustion(cfg)
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
    longer carries a `goal.roadmap_exhausted(cfg)` conjunction."""
    daemon_src = Path(daemon.__file__).read_text()
    assert "and goal.roadmap_exhausted(cfg)" not in daemon_src, (
        "TB-275: the dispatch-path roadmap-exhaustion halt conjunction "
        "is back in daemon.py. roadmap_complete must gate the ideation "
        "trigger only (see `ap2/ideation.py`), never task dispatch."
    )


def test_dispatch_promotes_when_roadmap_exhausted(cfg, monkeypatch):
    """TB-275 regression pin (behavioral): when the halt is active AND
    a dispatchable Backlog task is present, the daemon
    auto-promotes/dispatches it (no halt)."""
    monkeypatch.delenv("AP2_IDEATION_HALT_DISABLED", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)
    monkeypatch.setenv("AP2_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha")
    _emit_ideation_empty(cfg)
    ideation_halt.maybe_halt_on_exhaustion(cfg)

    # Pre-condition: ideation halt active.
    assert goal.roadmap_exhausted(cfg) is True

    # Seed a dispatchable Backlog task.
    from ap2.board import Board
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-999",
        title="dispatchable Backlog task under roadmap_complete",
    )
    board.save()

    # Stub _tick internals.
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda cfg: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_async)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)
    monkeypatch.setattr(ideation_halt, "maybe_halt_on_exhaustion", lambda cfg: None)
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

    from ap2.board import Board as _Board
    board = _Board.load(cfg.tasks_file)
    loc = board.find("TB-999")
    assert loc is not None
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
    assert len(promoted) == 1


def test_ideation_trigger_gate_still_intact():
    """TB-275 sibling pin: the ideation-trigger gate in
    `_maybe_ideate` still uses the canonical predicate."""
    from ap2 import ideation as _ideation
    ideation_src = Path(_ideation.__file__).read_text()
    assert "_goal.roadmap_exhausted(cfg)" in ideation_src or \
        "goal.roadmap_exhausted(cfg)" in ideation_src, (
        "TB-275: the ideation-trigger roadmap-complete gate has been "
        "removed from `_maybe_ideate`."
    )
    assert 'reason="roadmap_complete"' in ideation_src or \
        "reason='roadmap_complete'" in ideation_src, (
        "TB-275: the `ideation_skipped reason=roadmap_complete` "
        "event shape changed in `_maybe_ideate`."
    )


# ===========================================================================
# Resume on goal_updated (TB-342 contract)
# ===========================================================================


def test_reset_pointer_on_goal_updated_clears_halt(cfg, monkeypatch):
    """`goal.reset_pointer_on_goal_updated` clears `empty_cycles`,
    `roadmap_complete_emitted`, and `roadmap_complete_ack_idx`. The
    helper is called by the `update_goal` operator-queue drain handler
    after the atomic goal.md write."""
    _write_goal_with_foci(cfg, "alpha")
    # Seed a halted pointer.
    pointer = goal.load_pointer(cfg)
    pointer["empty_cycles"] = 5
    pointer["roadmap_complete_emitted"] = True
    pointer["roadmap_complete_ack_idx"] = 1
    goal.save_pointer(cfg, pointer)

    # Operator extends.
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")
    foci = goal.read_focus_list(cfg)
    new_pointer = goal.reset_pointer_on_goal_updated(cfg, foci)
    goal.save_pointer(cfg, new_pointer)

    assert new_pointer["empty_cycles"] == 0
    assert new_pointer["roadmap_complete_emitted"] is False
    assert new_pointer["roadmap_complete_ack_idx"] is None
    # Halt clears naturally.
    assert goal.roadmap_exhausted(cfg) is False


def test_reset_pointer_on_goal_updated_is_idempotent_on_unhalted(cfg):
    """The helper is idempotent: calling it against a not-halted
    pointer is a no-op semantically (the flag was already False, the
    counter was already 0)."""
    _write_goal_with_foci(cfg, "alpha")
    # Default-emit pointer is already cleared.
    foci = goal.read_focus_list(cfg)
    new_pointer = goal.reset_pointer_on_goal_updated(cfg, foci)
    assert new_pointer["roadmap_complete_emitted"] is False
    assert new_pointer["empty_cycles"] == 0
    assert new_pointer["roadmap_complete_ack_idx"] is None


# ===========================================================================
# Smoke: the empty-focus-list / missing-goal.md paths don't crash
# ===========================================================================


def test_no_foci_no_op(cfg):
    """No `## Current focus:` headings in goal.md → detector pass is
    a no-op."""
    (cfg.project_root / "goal.md").write_text(_make_goal_md(""))
    ideation_halt.maybe_halt_on_exhaustion(cfg)
    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False
    assert goal.roadmap_exhausted(cfg) is False


def test_missing_goal_md_no_op(tmp_path: Path):
    """Missing goal.md → detector pass is a no-op."""
    init_project(tmp_path)
    (tmp_path / "goal.md").unlink()
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    ideation_halt.maybe_halt_on_exhaustion(cfg)
    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False
    assert goal.roadmap_exhausted(cfg) is False


# ===========================================================================
# Drift-gate substring anchors.
# ===========================================================================


def test_drift_gate_anchors_present():
    """All env-knob / event-type substrings this work introduced are
    referenced in this test module."""
    text = Path(__file__).read_text()
    for knob in _ENV_KNOB_SUBSTRINGS:
        assert knob in text, f"env knob {knob} missing from test file"
    for evt in _EVENT_TYPE_SUBSTRINGS:
        assert evt in text, f"event type {evt} missing from test file"
    # Spot-check the helper names too.
    assert "parse_focus_list" in text
    assert "maybe_halt_on_exhaustion" in text
    assert "roadmap_exhausted" in text
    assert "reset_pointer_on_goal_updated" in text
