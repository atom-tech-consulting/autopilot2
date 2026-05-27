# Empty-cycles counter must recognize `ideation_cycle_summary` as exit marker

Tags: #autopilot #empty-cycles #focus-advance #counter #bug #regression-pin

## Goal

Add `ideation_cycle_summary` to the exit-marker set in
`_ideation_empty_against_focus` (`ap2/focus_advance.py`), and register
the event type in `ap2/events.py`'s vocabulary. Closes the goal.md
`## Done when` failure mode "Ideation reliably proposes goal-aligned
next steps that substantively advance the goal (not just goal-shaped
pro-forma compliance)" — under the current counter logic, **empty
ideation cycles never trigger auto-advance** because the ideation
agent emits `ideation_cycle_summary` (not `ideation_complete`) on
the no-proposal path, and the counter only recognizes
`ideation_complete` as the cycle-end marker. Result: the
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES` threshold is structurally
unreachable; the operator must manually rotate even when ideation
has correctly reasoned its way to "nothing to propose."

Why now: 2026-05-27T11:02:09Z — first natural empty cycle after
the rewind-focus + TB-292 counter restructure landed. Agent emitted
`ideation_cycle_summary` with summary "0 proposals; focus-2
(operator-legible reporting and monitoring) marked
exhausted-needs-operator — all 3 Progress signal[s addressed]";
scrub correctly removed 1058 chars of verdict language; counter
left `focus_pointer.empty_cycles = 0`. Trace of the failure:
`ap2/focus_advance.py:_ideation_empty_against_focus` checks
`elif typ == "ideation_complete" and in_cycle:` (the only exit
predicate); the agent's actual exit-marker is `ideation_cycle_summary`
(11 historical occurrences in `events.jsonl` going back to
2026-05-10, the first one literally titled "17th consecutive
0-proposal cycle"). The two-event pattern is intentional in the
agent's prompt — `ideation_complete` summarizes proposals,
`ideation_cycle_summary` summarizes a 0-proposal cycle's reasoning
— but the counter wasn't updated when the second event entered
the vocabulary. Without this fix, the empty-cycles auto-advance
mechanism is fully non-functional under normal operation; today's
investigation arc (TB-291, TB-292, TB-293, TB-295) closed bugs
1-4 but the auto-advance still cannot fire because the agent never
emits the event the counter is looking for.

## Scope

(1) `ap2/focus_advance.py` `_ideation_empty_against_focus`: extend
the exit-marker check from `elif typ == "ideation_complete" and
in_cycle:` to `elif typ in ("ideation_complete",
"ideation_cycle_summary") and in_cycle:`. Same handler body —
increment if no proposal in the cycle, reset if any proposal
fired. The `cycle_had_proposal` flag's semantics don't change;
either exit event closes the cycle the same way.

(2) `ap2/focus_advance.py` docstrings: update the docstring at the
function header (around L60-100) to name both exit markers
explicitly. Drop any "only `ideation_complete` counts" framing
from comments / docstrings that TB-292 introduced.

(3) `ap2/events.py`: register `ideation_cycle_summary` in the event
vocabulary alongside `ideation_complete`. Document the
distinction: `ideation_complete` carries a proposal summary
(used when ≥1 proposal landed this cycle);
`ideation_cycle_summary` carries a no-proposal-reasoning summary
(used when 0 proposals landed). Both are emitted by the ideation
agent via `log_event` MCP at the end of a cycle.

(4) Update existing tests in `ap2/tests/test_empty_cycles_counter.py`
(landed in TB-292) to cover the new exit marker:
  - Cycle with `ideation_empty_board` entry + `ideation_cycle_summary`
    exit (no proposals) → contributes 1 to count.
  - Mixed sequence: `ideation_complete` cycle + 2×
    `ideation_cycle_summary` cycles → counter reaches 2 (productive
    cycle reset to 0, two empties counted).
  - Sequence reaching threshold: 3×`ideation_cycle_summary` exits
    → counter = 3 → trip.

(5) `ap2/ideation.default.md`: add a brief note in the agent's
exit-protocol section (where the prompt instructs emitting the
end-of-cycle summary event) clarifying that both event names are
valid — `ideation_complete` when proposing, `ideation_cycle_summary`
when not. (Best-effort docs alignment; the agent's existing
behavior is correct, only the prompt's documented event list
needs updating to match.)

## Design

Two-event vocabulary is intentional in the agent's behavior — keeps
the summary text clearly typed by cycle outcome. The fix is making
the counter aware of both names, not collapsing them. The
`cycle_had_proposal` flag inside the counter already separates
productive from empty within a cycle; the exit-marker check just
needs to recognize either name as "cycle closed."

Why not rename `ideation_cycle_summary` → `ideation_complete` to
unify? Three reasons against:

- Historical events.jsonl carries 11 `ideation_cycle_summary` events
  spanning 2026-05-10 → 2026-05-27. Renaming retroactively would
  require either a one-shot rewrite (fragile) or a "both old and
  new" parser anyway. Cheaper to accept the two-name vocabulary.
- The two summaries carry materially different payload shapes
  (proposal lists vs. reasoning narratives). Distinct event names
  make downstream consumers (status report digests, web UI
  rendering, audit tooling) easier to write.
- TB-292's restructure already established cycle-grouped semantics;
  the only correctness gap is the exit-marker set, which is
  one-line.

Why not also update `ideation_timeout` / `ideation_error` handling?
Those events don't close a cycle from the counter's perspective
(infrastructure failures don't count toward empty-cycles signal,
per TB-292's design). They stay outside the exit-marker set; the
`in_cycle = False` reset on those branches is correct as-is.

## Verification

- `grep -q 'ideation_cycle_summary' ap2/focus_advance.py` — counter recognizes the new exit marker.
- `grep -q 'ideation_cycle_summary' ap2/events.py` — event registered in vocabulary.
- `uv run python -c "from ap2.focus_advance import _ideation_empty_against_focus; tail = [{'type':'ideation_empty_board'},{'type':'ideation_cycle_summary'}]; assert _ideation_empty_against_focus(tail, 'x') == 1, 'empty cycle via cycle_summary should = 1'"` — one-empty-cycle invariant for the new marker.
- `uv run python -c "from ap2.focus_advance import _ideation_empty_against_focus; tail = [{'type':'ideation_empty_board'},{'type':'ideation_cycle_summary'}]*3; assert _ideation_empty_against_focus(tail, 'x') == 3, 'three empty cycles via cycle_summary should = 3'"` — threshold-reachability invariant.
- `uv run python -c "from ap2.focus_advance import _ideation_empty_against_focus; tail = [{'type':'ideation_empty_board'},{'type':'ideation_proposal_recorded'},{'type':'ideation_cycle_summary'}]; assert _ideation_empty_against_focus(tail, 'x') == 0, 'cycle with proposal + cycle_summary should reset'"` — productive-cycle reset still works with new exit marker.
- `uv run pytest -q ap2/tests/test_empty_cycles_counter.py` — counter tests pass against the extended exit-marker set.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Renaming `ideation_cycle_summary` → `ideation_complete` —
  rejected design alternative (see Design).
- Changing the agent's prompt to always emit `ideation_complete` —
  same rejection; the two-name vocabulary is intentional.
- Backfilling historical `focus_pointer.empty_cycles` values — the
  pointer field is recomputed every tick from the events tail, so
  the next tick after this fix lands will surface the correct count
  retroactively.
- Auditing other counters / event-watching helpers in the codebase
  for similar exit-marker mismatches — separate hardening pass if
  the pattern recurs.
- TB-284 scrub bug (separate TB-294, already landed). The scrub
  correctly removes verdict language regardless of which exit
  marker fires; this TB only concerns the counter's awareness.
