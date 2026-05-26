# Restructure empty-cycles counter to cycle-grouped semantics

Tags: #autopilot #empty-cycles #focus-advance #counter #bug #regression-pin

## Goal

Replace `_ideation_empty_against_focus` in `ap2/focus_advance.py` — currently
an event-walking flat-increment counter — with a cycle-grouped algorithm that
correctly accumulates "consecutive ideation cycles that exited without
recording a proposal." Closes the goal.md `## Done when` failure mode
"Ideation reliably proposes goal-aligned next steps that substantively advance
the goal (not just goal-shaped pro-forma compliance)" — the current counter
trips after roughly 1.5 truly-empty cycles (or 1 empty + 1 productive cycle
under the now-fenced queue-path desync), prematurely advancing the focus and
parking the loop on a stale "done" verdict even when ideation just produced
a substantive proposal.

Why now: 2026-05-26T08:36:05Z incident — `focus_advanced
trigger=empty_cycles_heuristic` fired at the same tick TB-290 (a real
ideation proposal closing the last named-in-goal.md attention-detector axis)
was being drained into Backlog. Tracing the counter showed the bug: one
ideation cycle emits `ideation_empty_board` (daemon-emitted entry marker
at `ideation.py:706`) AND `ideation_complete` (agent-emitted exit marker
via the `log_event` MCP tool). Both are in the counter's increment set, so
one cycle = +2; one productive cycle nets +1 (the reset only zeros between
the increments). At threshold 3, ~1.5 empty cycles trip — not the 3 the
env-knob name advertises. TB-291 landed a tool fence (removed
`operator_queue_append` from ideation's toolset) that closed the
exacerbating queue-path desync, but the underlying double-count is
structurally wrong and still active.

## Scope

(1) `ap2/focus_advance.py`: replace `_ideation_empty_against_focus`'s
event-walking flat-increment body with a cycle-grouped algorithm.
Walk the relevant events tail forward grouping into ideation cycles
bounded by `ideation_empty_board` (entry) and one of
`ideation_complete` / `ideation_timeout` / `ideation_error` (exit).
Per cycle: increment count if cycle exited via `ideation_complete`
AND no `ideation_proposal_recorded` fired within the cycle; reset
count to 0 if any proposal fired; leave count unchanged if cycle
exited via `ideation_timeout` or `ideation_error` (infrastructure
failure, not "ideation chose not to propose").

(2) Preserve the existing function name + signature so call sites in
`_maybe_advance_focus` don't need to change. Update the function's
docstring to reflect the new cycle-grouped semantics — drop the
"permissive" framing; the new counter is precise.

(3) Preserve the existing `focus_advanced to=<focus>` cutoff logic
(this fix is orthogonal to the separate operator-pointer-rewind
bug; that fence-event-emission is its own TB).

(4) Preserve the `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` env knob and its
default of 3 — the name now correctly describes the semantics
("3 consecutive empty cycles to trip"), where previously it was
approximately a misnomer.

(5) Update existing tests in `ap2/tests/test_tb226_focus_rotation.py`
that exercise the empty-cycles path. The previous fixtures used
bare `ideation_empty_board` event lists; under the new semantics,
each cycle needs entry+exit event pairs (`ideation_empty_board`
followed by `ideation_complete` for an empty cycle;
`ideation_empty_board` + `ideation_proposal_recorded` +
`ideation_complete` for a productive cycle).

(6) New regression-pin module `ap2/tests/test_empty_cycles_counter.py`
covering the policy decisions explicitly:
- Empty cycle (entry + complete) contributes 1 to count.
- Productive cycle (entry + proposal + complete) contributes 0
  (reset).
- Timeout cycle (entry + timeout) leaves count unchanged.
- Error cycle (entry + error) leaves count unchanged.
- Mixed sequence: 3 consecutive empty cycles → count = 3.
- Mixed sequence: empty, empty, productive → count = 0 (reset).
- Mixed sequence: productive, empty, empty, empty → count = 3.
- `ideation_skipped` events outside any cycle are ignored.
- Truncated cycle (events appearing after a cutoff without their
  matching entry marker) are ignored.
- Empty events tail → count = 0.

## Design

The current implementation treats events as evidence: any event in the
increment set bumps the counter, any reset event zeros it. This conflates
events with cycles — the wrong granularity for a "consecutive cycles
without proposal" semantic. The new shape groups events into cycles and
asks one question per cycle: "did the agent record a proposal during this
cycle?" Per cycle = at most one count-change (either increment or reset).
Algorithm sketch:

```
count = 0
in_cycle = False
cycle_had_proposal = False
for e in relevant_tail:
    typ = e.type
    if typ == "ideation_empty_board":
        in_cycle = True
        cycle_had_proposal = False
    elif typ == "ideation_proposal_recorded" and in_cycle:
        cycle_had_proposal = True
    elif typ == "ideation_complete" and in_cycle:
        count = 0 if cycle_had_proposal else count + 1
        in_cycle = False
    elif typ in ("ideation_timeout", "ideation_error") and in_cycle:
        in_cycle = False    # don't count infrastructure failures
return count
```

Two policy decisions worth flagging:

**Timeouts don't count.** `ideation_timeout` means the SDK call exceeded
its budget; we don't know whether the agent would have proposed. Treating
timeouts as empty would penalize transient SDK slowness — a cluster of
network issues could falsely trip focus advance. Non-counting is the
conservative choice. If timeouts become frequent enough to skew the empty
signal, the right response is fixing the SDK budget, not counting them as
empty.

**Errors don't count.** Same logic — `ideation_error` is infrastructure
failure, not "ideation reasoned and found nothing." Non-counting matches
the "don't penalize broken plumbing" principle.

Defensive shape: cycle-boundary tracking via `in_cycle` flag handles
truncated tails cleanly. If the tail starts mid-cycle (we missed the
entry marker), `in_cycle` stays False and the proposal/complete events
in that orphan cycle are ignored. If a cycle starts but its exit marker
falls off the tail (rare — would require a daemon crash mid-cycle), the
next entry marker resets the flags cleanly without spurious increments.

## Verification

- `grep -q 'in_cycle' ap2/focus_advance.py` — new cycle-grouped state introduced.
- `grep -q 'ideation_timeout' ap2/focus_advance.py` — timeout exit handled.
- `grep -q 'ideation_error' ap2/focus_advance.py` — error exit handled.
- `uv run python -c "from ap2.focus_advance import _ideation_empty_against_focus; tail = [{'type':'ideation_empty_board'},{'type':'ideation_complete'}]; assert _ideation_empty_against_focus(tail, 'x') == 1, 'one empty cycle should = 1'"` — one-empty-cycle invariant.
- `uv run python -c "from ap2.focus_advance import _ideation_empty_against_focus; tail = [{'type':'ideation_empty_board'},{'type':'ideation_proposal_recorded'},{'type':'ideation_complete'}]; assert _ideation_empty_against_focus(tail, 'x') == 0, 'one productive cycle should = 0'"` — productive-cycle reset invariant.
- `uv run python -c "from ap2.focus_advance import _ideation_empty_against_focus; tail = [{'type':'ideation_empty_board'},{'type':'ideation_timeout'}]; assert _ideation_empty_against_focus(tail, 'x') == 0, 'timeout cycle should not count'"` — timeout-non-count invariant.
- `test -f ap2/tests/test_empty_cycles_counter.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_empty_cycles_counter.py` — counter tests pass.
- `uv run pytest -q ap2/tests/test_tb226_focus_rotation.py` — existing focus-rotation tests pass against the restructured counter.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Operator pointer rewind not emitting a synthetic `focus_advanced`
  event for the cutoff logic — separate TB (bug 3). Affects manual
  recovery flows; orthogonal to this counter restructuring.
- Auto-approve gate behavior under `roadmap_complete` (TB-290 has
  been stuck for hours despite docstring claim "task dispatch is NOT
  affected") — separate TB (bug 4). May be correct-but-undocumented
  or a fourth bug.
- TB-284 scrub mechanism silent-timeout — separate TB. Independent
  surface; scrub runs after ideation cycles, not part of the counter.
- Renaming `_ideation_empty_against_focus` (the name is fine; the
  semantics are what change).
- Renaming `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (the env-knob name now
  correctly describes the new semantics).
- Tuning the default threshold of 3 (the value is fine once the
  counter actually counts cycles instead of half-cycles).
- Backfilling historical counter values (`focus_pointer.json`'s
  `empty_cycles` field gets recomputed every tick from the events
  tail; no migration needed).
