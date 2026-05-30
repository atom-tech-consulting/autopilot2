"""TB-292 / TB-342: regression-pin for `_ideation_empty_against_focus`'s
cycle-grouped semantics. TB-300 extended the exit-marker set from
`ideation_complete` alone to also include `ideation_cycle_summary`;
TB-342 changed the reset cutoff from `focus_advanced to=<focus_title>`
(the now-deleted multi-focus rotation event) to `goal_updated` (the
operator-edits-goal.md signal).

The pre-TB-292 implementation walked events as flat evidence: any
event in the increment set bumped the counter, any reset event zeroed
it. That conflated events with cycles — one ideation cycle emits BOTH
`ideation_empty_board` (daemon-emitted entry marker at
`ideation._run_ideation`) AND `ideation_complete` (agent-emitted exit
via the `log_event` MCP tool), so one cycle bumped the count by 2 and
one productive cycle netted +1 (the reset only zeroed between the two
increments). At threshold 3, ~1.5 truly-empty cycles tripped the focus
advance — half the cadence the env-knob name advertised.

The TB-292 implementation groups events into cycles bounded by entry
(`ideation_empty_board`) and exit (`ideation_complete` /
`ideation_cycle_summary` / `_timeout` / `_error`) markers. Per cycle =
at most one count-change:

  - Empty cycle (entry + complete/cycle_summary, no proposal)
    → count += 1.
  - Productive cycle (entry + proposal_recorded + complete/cycle_summary)
    → count = 0.
  - Timeout cycle (entry + timeout) → count unchanged.
  - Error cycle (entry + error) → count unchanged.

TB-300 specifically pins `ideation_cycle_summary` as a valid exit
marker: the agent's two-event vocabulary is intentional —
`ideation_complete` for cycles that produced proposals,
`ideation_cycle_summary` for cycles that produced none — and under
the pre-TB-300 single-name predicate the auto-advance threshold was
structurally unreachable because every natural 0-proposal cycle
arrived through the `ideation_cycle_summary` branch the counter
ignored.

TB-342 reset cutoff change: the multi-focus rotation pointer walk was
collapsed into a single ideation-exhaustion detector, so the counter
no longer scopes to a focus title (the pointer doesn't walk). The
natural reset signal for the global empty-cycles run is now
`goal_updated` (operator extended/edited goal.md via
`ap2 update-goal`); the `focus_advanced` cutoff went away with the
rotation. Pre-edit empty cycles do not count against the post-edit
runway.

These tests pin the policy decisions explicitly so a future refactor
can't silently regress to the old double-count shape — or, just as
importantly, can't accidentally start counting timeouts/errors as
empty (which would let transient SDK slowness falsely trip the halt)
or drop `ideation_cycle_summary` from the exit-marker set (which
would re-introduce the TB-300 unreachable-threshold bug) or revert
the TB-342 cutoff back to a focus-scoped one.

This module is a pure unit test against
`ap2.ideation_halt._consecutive_empty_ideation_cycles` — no
fixtures, no disk I/O, just constructed event-tail dicts. Behavioral
coverage of the same surface against the live daemon harness lives in
`test_tb226_focus_rotation.py`.
"""
from __future__ import annotations

from ap2.ideation_halt import _consecutive_empty_ideation_cycles


# ===========================================================================
# Helpers — build event-tail dicts in the shape `events.tail()` returns
# ===========================================================================


def _evt(type_: str, **fields) -> dict:
    """Minimal event dict for tail-construction. `events.tail()` returns
    dicts with at least `ts` and `type`; the counter only reads `type`
    (and `goal_updated` is the cutoff anchor), so the rest is optional
    padding to make assertions read naturally."""
    return {"type": type_, **fields}


def _empty_cycle() -> list[dict]:
    """One empty ideation cycle: entry + `ideation_complete` exit, no
    proposal. This is the canonical pre-TB-300 empty-cycle shape — the
    agent CAN emit `ideation_complete` on a 0-proposal cycle when its
    summary still describes proposals it ALMOST emitted (rare in
    production today; the `_empty_cycle_via_summary` shape below is
    the natural emission)."""
    return [_evt("ideation_empty_board"), _evt("ideation_complete")]


def _empty_cycle_via_summary() -> list[dict]:
    """One empty ideation cycle: entry + `ideation_cycle_summary` exit,
    no proposal. TB-300: this is the natural 0-proposal emission
    shape."""
    return [_evt("ideation_empty_board"), _evt("ideation_cycle_summary")]


def _productive_cycle(task: str = "TB-1") -> list[dict]:
    """One productive cycle: entry + proposal + `ideation_complete`
    exit."""
    return [
        _evt("ideation_empty_board"),
        _evt("ideation_proposal_recorded", task=task),
        _evt("ideation_complete"),
    ]


def _productive_cycle_via_summary(task: str = "TB-1") -> list[dict]:
    """One productive cycle but exiting via `ideation_cycle_summary`."""
    return [
        _evt("ideation_empty_board"),
        _evt("ideation_proposal_recorded", task=task),
        _evt("ideation_cycle_summary"),
    ]


def _timeout_cycle() -> list[dict]:
    """One timeout cycle: entry + timeout exit (no proposal, no complete)."""
    return [_evt("ideation_empty_board"), _evt("ideation_timeout")]


def _error_cycle() -> list[dict]:
    """One error cycle: entry + error exit (no proposal, no complete)."""
    return [_evt("ideation_empty_board"), _evt("ideation_error")]


# ===========================================================================
# Per-cycle accounting — one cycle at a time
# ===========================================================================


def test_empty_cycle_contributes_one():
    """An empty cycle (entry + complete) bumps the counter by 1."""
    assert _consecutive_empty_ideation_cycles(_empty_cycle()) == 1


def test_productive_cycle_resets_to_zero():
    """A productive cycle (entry + proposal + complete) resets the
    counter to 0."""
    assert _consecutive_empty_ideation_cycles(_productive_cycle()) == 0


def test_timeout_cycle_does_not_count():
    """A timeout cycle leaves the count unchanged. Infrastructure
    failure is not 'ideation chose not to propose' — treating it as
    empty would let transient SDK slowness falsely trip the halt."""
    assert _consecutive_empty_ideation_cycles(_timeout_cycle()) == 0


def test_error_cycle_does_not_count():
    """Same logic as timeout: an error cycle is infrastructure
    failure, not 'ideation reasoned and found nothing.'"""
    assert _consecutive_empty_ideation_cycles(_error_cycle()) == 0


# ===========================================================================
# TB-300: `ideation_cycle_summary` as exit marker
# ===========================================================================


def test_empty_cycle_via_cycle_summary_contributes_one():
    """An empty cycle that exits via `ideation_cycle_summary` (the
    agent's natural 0-proposal emission shape) bumps the counter by 1.
    Pins the TB-300 invariant — the pre-TB-300 single-name predicate
    silently dropped this shape on the floor, leaving the
    auto-halt threshold structurally unreachable."""
    assert _consecutive_empty_ideation_cycles(_empty_cycle_via_summary()) == 1


def test_three_empty_cycles_via_cycle_summary_reach_threshold():
    """Three consecutive empty cycles each exited via
    `ideation_cycle_summary` → count = 3."""
    tail = (
        _empty_cycle_via_summary()
        + _empty_cycle_via_summary()
        + _empty_cycle_via_summary()
    )
    assert _consecutive_empty_ideation_cycles(tail) == 3


def test_productive_cycle_via_cycle_summary_resets():
    """A productive cycle that exits via `ideation_cycle_summary` (a
    proposal landed AND the summary name fired — the rare-but-valid
    cross-shape; the counter must still reset to 0)."""
    assert _consecutive_empty_ideation_cycles(_productive_cycle_via_summary()) == 0


def test_mixed_complete_then_cycle_summary_empties_count():
    """Mixed sequence: one productive cycle (exits via
    `ideation_complete`) followed by two empty cycles (exit via
    `ideation_cycle_summary`)."""
    tail = (
        _productive_cycle()
        + _empty_cycle_via_summary()
        + _empty_cycle_via_summary()
    )
    assert _consecutive_empty_ideation_cycles(tail) == 2


def test_interleaved_exit_marker_names_count_uniformly():
    """Cycles exiting via either `ideation_complete` OR
    `ideation_cycle_summary` all count toward the same counter."""
    tail = (
        _empty_cycle()
        + _empty_cycle_via_summary()
        + _empty_cycle()
        + _empty_cycle_via_summary()
    )
    assert _consecutive_empty_ideation_cycles(tail) == 4


# ===========================================================================
# Multi-cycle sequences — pin the algorithm's accumulation behavior
# ===========================================================================


def test_three_consecutive_empty_cycles_count_to_three():
    """Three consecutive empty cycles → count = 3."""
    tail = _empty_cycle() + _empty_cycle() + _empty_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 3


def test_empty_empty_productive_resets():
    """Two empties followed by a productive cycle → count = 0."""
    tail = _empty_cycle() + _empty_cycle() + _productive_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 0


def test_productive_then_three_empties_count_to_three():
    """Productive cycle, then three consecutive empties → count = 3."""
    tail = (
        _productive_cycle()
        + _empty_cycle()
        + _empty_cycle()
        + _empty_cycle()
    )
    assert _consecutive_empty_ideation_cycles(tail) == 3


def test_timeouts_interleaved_do_not_perturb():
    """A timeout cycle between two empty cycles does not reset and
    does not increment — count remains 2."""
    tail = _empty_cycle() + _timeout_cycle() + _empty_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 2


def test_errors_interleaved_do_not_perturb():
    """Same shape as the timeout interleave but with an error cycle."""
    tail = _empty_cycle() + _error_cycle() + _empty_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 2


# ===========================================================================
# Boundary + defensive shapes
# ===========================================================================


def test_empty_tail_returns_zero():
    """No events at all → count = 0."""
    assert _consecutive_empty_ideation_cycles([]) == 0


def test_ideation_skipped_outside_any_cycle_ignored():
    """`ideation_skipped` events (cooldown / disabled / no-slots /
    roadmap_complete) fall OUTSIDE the entry/exit cycle markers and
    must not perturb the counter."""
    tail = [
        _evt("ideation_skipped", reason="cooldown"),
        _evt("ideation_skipped", reason="roadmap_complete"),
    ] + _empty_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 1


def test_truncated_cycle_without_entry_marker_ignored():
    """An exit marker (or proposal) without a preceding entry marker
    is ignored — `in_cycle` stays False until a fresh
    `ideation_empty_board` opens a new cycle."""
    tail = [
        _evt("goal_updated"),
        _evt("ideation_complete"),  # orphan — no preceding entry
        _evt("ideation_proposal_recorded", task="TB-X"),  # orphan
    ] + _empty_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 1


# ===========================================================================
# TB-342: `goal_updated` cutoff
# ===========================================================================


def test_goal_updated_cutoff_excludes_pre_edit_events():
    """Events older than the most recent `goal_updated` are excluded
    from the count. A string of empties accumulated against the
    pre-edit goal must not count against the post-edit runway."""
    tail = (
        _empty_cycle()  # before the edit — should be ignored
        + _empty_cycle()
        + _empty_cycle()
        + [_evt("goal_updated", reason="extension")]
        + _empty_cycle()  # only this one counts
    )
    assert _consecutive_empty_ideation_cycles(tail) == 1


def test_most_recent_goal_updated_is_the_cutoff():
    """When multiple `goal_updated` events appear in the tail, only
    the most recent one anchors the cutoff."""
    tail = (
        _empty_cycle()
        + [_evt("goal_updated", reason="first edit")]
        + _empty_cycle()  # between edits — ignored
        + _empty_cycle()
        + [_evt("goal_updated", reason="second edit")]
        + _empty_cycle()  # only this one counts
        + _empty_cycle()
    )
    assert _consecutive_empty_ideation_cycles(tail) == 2


def test_no_goal_updated_event_walks_whole_tail():
    """Absence of `goal_updated` in the tail means the counter walks
    every event (the initial pre-edit runway from a fresh project
    where goal.md was written by `init_project`, not updated through
    the queue)."""
    tail = _empty_cycle() + _empty_cycle()
    assert _consecutive_empty_ideation_cycles(tail) == 2
