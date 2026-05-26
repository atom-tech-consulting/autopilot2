"""TB-292: regression-pin for `_ideation_empty_against_focus`'s
cycle-grouped semantics.

The pre-TB-292 implementation walked events as flat evidence: any
event in the increment set bumped the counter, any reset event zeroed
it. That conflated events with cycles — one ideation cycle emits BOTH
`ideation_empty_board` (daemon-emitted entry marker at
`ideation._run_ideation`) AND `ideation_complete` (agent-emitted exit
via the `log_event` MCP tool), so one cycle bumped the count by 2 and
one productive cycle netted +1 (the reset only zeroed between the two
increments). At threshold 3, ~1.5 truly-empty cycles tripped the focus
advance — half the cadence the env-knob name advertised.

The new implementation groups events into cycles bounded by entry
(`ideation_empty_board`) and exit (`ideation_complete` / `_timeout` /
`_error`) markers. Per cycle = at most one count-change:

  - Empty cycle (entry + complete, no proposal) → count += 1.
  - Productive cycle (entry + proposal_recorded + complete) → count = 0.
  - Timeout cycle (entry + timeout) → count unchanged.
  - Error cycle (entry + error) → count unchanged.

These tests pin the policy decisions explicitly so a future refactor
can't silently regress to the old double-count shape — or, just as
importantly, can't accidentally start counting timeouts/errors as
empty (which would let transient SDK slowness falsely trip focus
advance).

This module is a pure unit test against
`ap2.focus_advance._ideation_empty_against_focus` — no fixtures, no
disk I/O, just constructed event-tail dicts. Behavioral coverage of
the same surface against the live daemon harness lives in
`test_tb226_focus_rotation.py`.
"""
from __future__ import annotations

from ap2.focus_advance import _ideation_empty_against_focus


# ===========================================================================
# Helpers — build event-tail dicts in the shape `events.tail()` returns
# ===========================================================================


def _evt(type_: str, **fields) -> dict:
    """Minimal event dict for tail-construction. `events.tail()` returns
    dicts with at least `ts` and `type`; the counter only reads `type`
    + (`to` for the `focus_advanced` cutoff scan), so the rest is
    optional padding to make assertions read naturally."""
    return {"type": type_, **fields}


def _empty_cycle() -> list[dict]:
    """One empty ideation cycle: entry + exit, no proposal."""
    return [_evt("ideation_empty_board"), _evt("ideation_complete")]


def _productive_cycle(task: str = "TB-1") -> list[dict]:
    """One productive cycle: entry + proposal + exit."""
    return [
        _evt("ideation_empty_board"),
        _evt("ideation_proposal_recorded", task=task),
        _evt("ideation_complete"),
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
    tail = _empty_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 1


def test_productive_cycle_resets_to_zero():
    """A productive cycle (entry + proposal + complete) resets the
    counter to 0 — no carryover from earlier empties (this test
    isolates the per-cycle reset; mixed sequences are exercised below)."""
    tail = _productive_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 0


def test_timeout_cycle_does_not_count():
    """A timeout cycle leaves the count unchanged (here: starts at 0
    and stays at 0). Infrastructure failure is not 'ideation chose
    not to propose' — treating it as empty would let transient SDK
    slowness falsely trip focus advance."""
    tail = _timeout_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 0


def test_error_cycle_does_not_count():
    """Same logic as timeout: an error cycle is infrastructure
    failure, not 'ideation reasoned and found nothing.' Don't count."""
    tail = _error_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 0


# ===========================================================================
# Multi-cycle sequences — pin the algorithm's accumulation behavior
# ===========================================================================


def test_three_consecutive_empty_cycles_count_to_three():
    """Three consecutive empty cycles → count = 3. This is the
    canonical threshold-3 trip path; the new counter cleanly matches
    what `AP2_FOCUS_ADVANCE_EMPTY_CYCLES=3` advertises."""
    tail = _empty_cycle() + _empty_cycle() + _empty_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 3


def test_empty_empty_productive_resets():
    """Two empties followed by a productive cycle → count = 0 (reset
    by the productive cycle's complete event with a proposal landed
    inside the cycle)."""
    tail = _empty_cycle() + _empty_cycle() + _productive_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 0


def test_productive_then_three_empties_count_to_three():
    """Productive cycle, then three consecutive empties → count = 3.
    The productive cycle resets at its exit; the three empties
    accumulate fresh on top of the zero baseline."""
    tail = (
        _productive_cycle()
        + _empty_cycle()
        + _empty_cycle()
        + _empty_cycle()
    )
    assert _ideation_empty_against_focus(tail, "alpha") == 3


def test_timeouts_interleaved_do_not_perturb():
    """A timeout cycle between two empty cycles does not reset and
    does not increment — count remains 2 (from the two empties).
    Pins the 'infrastructure failures are invisible to the counter'
    invariant; a string of SDK timeouts shouldn't disturb whatever
    empty-cycle streak is in flight."""
    tail = _empty_cycle() + _timeout_cycle() + _empty_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 2


def test_errors_interleaved_do_not_perturb():
    """Same shape as the timeout interleave but with an error cycle —
    same expected outcome."""
    tail = _empty_cycle() + _error_cycle() + _empty_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 2


# ===========================================================================
# Boundary + defensive shapes
# ===========================================================================


def test_empty_tail_returns_zero():
    """No events at all → count = 0."""
    assert _ideation_empty_against_focus([], "alpha") == 0


def test_ideation_skipped_outside_any_cycle_ignored():
    """`ideation_skipped` events (cooldown / disabled / no-slots /
    roadmap_complete) fall OUTSIDE the entry/exit cycle markers and
    must not perturb the counter. Pins the 'orphan events are
    ignored' invariant — important because `ideation_skipped` fires
    every tick when the gate's parked, and a naive counter that
    treated it as an empty would saturate immediately."""
    tail = [
        _evt("ideation_skipped", reason="cooldown"),
        _evt("ideation_skipped", reason="roadmap_complete"),
    ] + _empty_cycle()
    # Only the one full empty cycle should count.
    assert _ideation_empty_against_focus(tail, "alpha") == 1


def test_truncated_cycle_without_entry_marker_ignored():
    """A cycle's exit marker appearing AFTER the cutoff but its
    matching entry marker falling off the tail (or before the cutoff)
    is ignored — `in_cycle` stays False until a fresh
    `ideation_empty_board` opens a new cycle.

    Constructed shape: a `focus_advanced to=alpha` cutoff, then a
    bare `ideation_complete` (orphan exit, no entry), then one full
    empty cycle. Only the full cycle counts."""
    tail = [
        _evt("focus_advanced", **{"from": "prior", "to": "alpha"}),
        _evt("ideation_complete"),  # orphan — no preceding entry
        _evt("ideation_proposal_recorded", task="TB-X"),  # orphan
    ] + _empty_cycle()
    assert _ideation_empty_against_focus(tail, "alpha") == 1


def test_focus_advanced_cutoff_excludes_prior_focus_events():
    """Events older than the most recent `focus_advanced to=<focus>`
    are excluded from the count. A string of empties accumulated
    against the PRIOR focus must not count against the freshly-
    advanced focus's freshness."""
    tail = (
        _empty_cycle()  # against the prior focus — should be ignored
        + _empty_cycle()
        + _empty_cycle()
        + [_evt("focus_advanced", **{"from": "prior", "to": "alpha"})]
        + _empty_cycle()  # only this one counts
    )
    assert _ideation_empty_against_focus(tail, "alpha") == 1


def test_cutoff_only_matches_target_focus_title():
    """A `focus_advanced` event whose `to` field doesn't match the
    target focus title is NOT a cutoff for that target. Prior empties
    against `alpha` should still count toward `alpha`'s freshness
    even though a separate advance to `beta` happened in between
    (this would only matter if the daemon ever advanced through
    multiple foci in a tail window, but pin the invariant defensively)."""
    tail = (
        _empty_cycle()
        + [_evt("focus_advanced", **{"from": "alpha", "to": "beta"})]
        + _empty_cycle()
    )
    # The cutoff scan looks for `to=alpha`; nothing matched, so the
    # cutoff stays at -1 and all events count.
    assert _ideation_empty_against_focus(tail, "alpha") == 2


def test_double_entry_marker_opens_fresh_cycle():
    """Two `ideation_empty_board` events in a row (rare — would
    require a daemon crash mid-cycle skipping the exit marker) reset
    the in-flight cycle state cleanly. The second entry opens a
    fresh cycle; the first cycle's accumulated `cycle_had_proposal`
    flag (if any) is dropped."""
    tail = [
        _evt("ideation_empty_board"),
        _evt("ideation_proposal_recorded", task="TB-1"),  # belongs to cycle 1
        _evt("ideation_empty_board"),  # cycle 2 starts; cycle 1 dropped
        _evt("ideation_complete"),  # closes cycle 2 → empty, count += 1
    ]
    assert _ideation_empty_against_focus(tail, "alpha") == 1
