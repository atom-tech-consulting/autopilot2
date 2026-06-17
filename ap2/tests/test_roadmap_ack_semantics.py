"""TB-340 (+ TB-342 collapse): dismiss-the-notice vs resume-ideation
semantics for the ideation-exhaustion halt.

Pins the corrected contract:

  - `goal.roadmap_exhausted` is a PURE function of the pointer
    (`roadmap_complete_emitted` flag post-TB-342, gated by an
    `len(foci) == 0` guard). No events-scan, no
    `roadmap_complete_ack_idx` read — an
    `operator_ack[roadmap_complete]` in the events tail does NOT clear
    the gate. (Pre-TB-340 the ack flipped the gate to False, wrongly
    un-parking ideation against an already-exhausted goal; TB-342 then
    collapsed the multi-focus rotation pointer walk into a single
    exhaustion detector, so the predicate became the flag the detector
    sets rather than an index comparison.)

  - `goal.roadmap_complete_notice_dismissed` is the ONLY consumer of
    the dismissal marker. It gates SURFACING (the operator nag), never
    PARKING. It returns True only after an ack for the CURRENT
    exhaustion episode, and re-arms (False) on the next fresh
    `roadmap_complete` emit because `focus_advance` clears the marker
    at emit time.

  - The exact 2026-05-29 stale-state bug: a dismissal marker left by a
    PRIOR extend→re-exhaust episode at the SAME foci count must NOT
    suppress a fresh episode's nag. The reset-on-emit makes the single
    forensic field authoritative (one writer clears, one writer sets,
    one reader).

  - Resuming ideation is editing goal.md (TB-342 collapsed the
    pre-existing `rewind-focus` recovery verb away with the rotation
    pointer walk). A `goal_updated` event triggers
    `reset_pointer_on_goal_updated`, which clears
    `roadmap_complete_emitted` so `roadmap_exhausted` returns False
    naturally — no ack involved.

Sibling to `test_tb226_focus_rotation.py` (which pins the exhaustion
detector + the now-flipped ack expectation) and
`test_tb242_status_active_focus_surface.py` (which pins the
surfacing-vs-state split on the `ap2 status` / web surfaces).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import events, goal, ideation_halt, operator_queue
from ap2.config import Config
from ap2.init import init_project


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


def _write_goal_with_foci(cfg: Config, *titles: str) -> None:
    """Write a goal.md with the given titles as `## Current focus:`
    headings (bare bodies, no `Progress signals:` sub-block)."""
    sections = "".join(
        f"## Current focus: {t}\n\nBody for {t}.\n\n" for t in titles
    )
    (cfg.project_root / "goal.md").write_text(
        _GOAL_MD_TEMPLATE.format(focus_section=sections)
    )


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Project root with the standard ap2 init layout."""
    # TB-413: sectioned knob name (`core.ideation_halt_disabled`) — the flat
    # `AP2_IDEATION_HALT_DISABLED` override path was removed.
    monkeypatch.delenv("AP2_CORE_IDEATION_HALT_DISABLED", raising=False)
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _exhaust(cfg: Config, *titles: str, monkeypatch=None) -> None:
    """Write a goal.md with `titles`, accumulate enough empty ideation
    cycles to trip the threshold, and run the detector pass so
    `roadmap_complete` emits (which also clears the dismissal marker
    per TB-340). TB-342: the helper no longer pre-positions an
    `active_index` past the last focus — the rotation pointer walk is
    gone, so the halt fires from the empty-cycles detector directly."""
    _write_goal_with_foci(cfg, *titles)
    if monkeypatch is not None:
        monkeypatch.setenv("AP2_CORE_IDEATION_HALT_EMPTY_CYCLES", "1")
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty")
    ideation_halt.maybe_halt_on_exhaustion(cfg)


def _ack_roadmap_complete(cfg: Config) -> None:
    """Apply an `ap2 ack roadmap_complete` via the real drain handler,
    which sets the pointer's dismissal marker AND emits an
    `operator_ack` event carrying the token."""
    operator_queue._apply_operator_ack(
        cfg, {"note": "roadmap_complete — dismissing the notice"}
    )


# ===========================================================================
# (1) The gate is a pure pointer predicate — ack does NOT clear it.
# ===========================================================================


def test_roadmap_exhausted_pure_pointer_predicate(cfg, monkeypatch):
    """`roadmap_exhausted` returns True when
    `roadmap_complete_emitted` is set (TB-342 collapsed gate)
    regardless of any `operator_ack[roadmap_complete]` in the events
    tail (the ack does NOT clear the gate)."""
    _exhaust(cfg, "alpha", "beta", monkeypatch=monkeypatch)
    assert goal.roadmap_exhausted(cfg) is True

    # An ack lands AFTER the most recent roadmap_complete with the
    # token in its note — pre-TB-340 this flipped the gate to False.
    _ack_roadmap_complete(cfg)
    assert goal.roadmap_exhausted(cfg) is True, (
        "the ack must NOT clear the gate — `roadmap_exhausted` is the "
        "`roadmap_complete_emitted` flag and the detector has set it"
    )


def test_roadmap_exhausted_ignores_ack_idx_field(cfg):
    """Even a pointer whose `roadmap_complete_ack_idx` already equals
    the foci count (a stale dismissal marker) does NOT make
    `roadmap_exhausted` return False — the forensic field is read ONLY
    by `roadmap_complete_notice_dismissed`, never by the gate."""
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = True
    pointer["roadmap_complete_ack_idx"] = 2  # marker == foci count
    goal.save_pointer(cfg, pointer)
    assert goal.roadmap_exhausted(cfg) is True


def test_roadmap_exhausted_total_zero_guard(cfg):
    """`total == 0` (no `## Current focus:` headings) → False; there's
    nothing to exhaust."""
    _write_goal_with_foci(cfg)  # no foci
    assert goal.roadmap_exhausted(cfg) is False


# ===========================================================================
# (2) The 2026-05-29 stale-state regression pin: a fresh emit re-arms.
# ===========================================================================


def test_fresh_emit_clears_stale_dismissal_marker(cfg, monkeypatch):
    """After a fresh `roadmap_complete` emit, a dismissal marker set by
    a PRIOR episode at the SAME foci count does NOT suppress the nag.

    Reproduces the exact 2026-05-29 bug: a stale `roadmap_complete_ack_idx`
    (left by a prior extend→re-exhaust episode at the same focus count)
    used to defeat the cheap-skip / suppress the nag with no operator
    action. TB-340 clears the marker at emit time so each episode
    re-nags exactly once.
    """
    monkeypatch.setenv("AP2_CORE_IDEATION_HALT_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    # Stale dismissal from a PRIOR episode at the same foci count (2),
    # and `roadmap_complete_emitted=False` so the detector pass treats
    # this as a FRESH exhaustion episode and emits.
    pointer["roadmap_complete_ack_idx"] = 2
    pointer["roadmap_complete_emitted"] = False
    goal.save_pointer(cfg, pointer)
    # Seed an empty cycle so the threshold (1) trips.
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty")

    ideation_halt.maybe_halt_on_exhaustion(cfg)

    # The fresh emit cleared the stale marker → the nag is re-armed.
    refreshed = goal.load_pointer(cfg)
    assert refreshed["roadmap_complete_ack_idx"] is None, (
        "the fresh `roadmap_complete` emit must reset the dismissal "
        "marker to None so the episode re-nags"
    )
    assert goal.roadmap_complete_notice_dismissed(cfg) is False, (
        "a stale dismissal from a prior episode must NOT suppress the "
        "fresh episode's nag (the 2026-05-29 stale-state bug)"
    )
    # The event did fire (the episode is freshly emitted).
    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1


# ===========================================================================
# (3) Dismissal lifecycle: per-episode, re-arms on the next emit.
# ===========================================================================


def test_notice_dismissed_lifecycle(cfg, monkeypatch):
    """`roadmap_complete_notice_dismissed` returns True only after an
    ack for the CURRENT episode, and False again after the next fresh
    `roadmap_complete` emit."""
    # Episode 1: exhaust → not dismissed yet.
    _exhaust(cfg, "alpha", "beta", monkeypatch=monkeypatch)
    assert goal.roadmap_complete_notice_dismissed(cfg) is False

    # Operator dismisses THIS episode.
    _ack_roadmap_complete(cfg)
    assert goal.roadmap_complete_notice_dismissed(cfg) is True, (
        "after an ack for the current episode the notice is dismissed"
    )
    # Gate is unaffected — ideation stays parked.
    assert goal.roadmap_exhausted(cfg) is True

    # Operator extends goal.md (resume); the `update_goal` drain
    # handler calls `reset_pointer_on_goal_updated` which clears the
    # halt. Simulate the reset directly here.
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")
    foci = goal.read_focus_list(cfg)
    resumed = goal.reset_pointer_on_goal_updated(cfg, foci)
    goal.save_pointer(cfg, resumed)
    # Resume cleared the gate.
    assert goal.roadmap_exhausted(cfg) is False
    assert goal.roadmap_complete_notice_dismissed(cfg) is False

    # Re-exhaust: emit `goal_updated` so the empty-cycles counter's
    # cutoff resets onto the post-edit runway, then seed an empty
    # cycle and let the detector re-emit.
    events.append(cfg.events_file, "goal_updated", reason="extension")
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty")
    ideation_halt.maybe_halt_on_exhaustion(cfg)

    # Fresh episode → notice NOT dismissed even though episode 1 was.
    assert goal.roadmap_exhausted(cfg) is True
    assert goal.roadmap_complete_notice_dismissed(cfg) is False, (
        "the fresh episode re-arms the nag — the prior episode's "
        "dismissal does not carry over"
    )


def test_notice_dismissed_requires_matching_foci_count(cfg):
    """`roadmap_complete_notice_dismissed` is False when the marker
    doesn't match the CURRENT foci count (e.g. the roadmap grew but the
    marker reflects an older, smaller count)."""
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = True
    pointer["roadmap_complete_ack_idx"] = 2  # stale: only 2 at ack time
    goal.save_pointer(cfg, pointer)
    assert goal.roadmap_exhausted(cfg) is True
    assert goal.roadmap_complete_notice_dismissed(cfg) is False


def test_notice_dismissed_false_when_not_exhausted(cfg):
    """`roadmap_complete_notice_dismissed` is False whenever the
    roadmap is NOT exhausted, regardless of the marker — dismissal is
    meaningless when there's no halt to dismiss."""
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    pointer["roadmap_complete_emitted"] = False  # not halted
    pointer["roadmap_complete_ack_idx"] = 2
    goal.save_pointer(cfg, pointer)
    assert goal.roadmap_exhausted(cfg) is False
    assert goal.roadmap_complete_notice_dismissed(cfg) is False


# ===========================================================================
# (4) Resume is a pointer move — no ack involved.
# ===========================================================================


def test_update_goal_pointer_reset_clears_gate_without_ack(cfg, monkeypatch):
    """A simulated `update-goal` reset
    (`reset_pointer_on_goal_updated`) makes `roadmap_exhausted` return
    False with NO ack. TB-342: editing goal.md via `ap2 update-goal`
    is the sole resume signal post-collapse — the pre-TB-342
    `rewind-focus` pointer move went away with the rotation theatre.
    """
    _exhaust(cfg, "alpha", "beta", monkeypatch=monkeypatch)
    assert goal.roadmap_exhausted(cfg) is True

    # Operator extends goal.md; the helper resets the halt flag.
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")
    foci = goal.read_focus_list(cfg)
    resumed = goal.reset_pointer_on_goal_updated(cfg, foci)
    goal.save_pointer(cfg, resumed)
    assert goal.roadmap_exhausted(cfg) is False, (
        "extending goal.md must clear the gate without any ack"
    )
