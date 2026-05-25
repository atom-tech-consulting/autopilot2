# Make empty-cycles the sole focus-advance signal; delete done-when judge

## Goal

Stop `_judge_done_when` from prematurely auto-advancing a focus on
diff-reading verdicts against shape-shaped Done-when bullets. Collapse
to the existing empty-cycles heuristic as the universal signal: a focus
advances after `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3) consecutive
ideation cycles produce zero proposals against it. Closes the goal.md
`## Done when` failure mode "Ideation reliably proposes goal-aligned
next steps that substantively advance the goal (not just goal-shaped
pro-forma compliance)" — the judge currently rules a focus done after
~3 tasks satisfy shape-shaped criteria by their commit diffs, collapsing
multi-week foci into 3-task cycles. The empty-cycles signal is
behavioral (ideation tried and produced nothing), not a separate LLM
verdict that can be gamed by easy-to-satisfy criteria.

Why now: 2026-05-23 incident — `focus_advanced from="operator-legible
reporting and monitoring" trigger=done_when_judge` fired at 03:06:06Z
after TB-280/281/282 each commit-satisfied one shape-shaped Done-when
bullet, parking the daemon at ROADMAP_COMPLETE. The judge declared
"done" by reading commit diffs of code the running daemon (pid 90751,
started 21:42Z 2026-05-22) had never executed — structurally unable
to verify operator-legibility in practice. If this is steady-state
behavior, walk-away time shrinks instead of growing. The empty-cycles
fallback path already exists in `ap2/focus_advance.py` (the `else`
branch of `_maybe_advance_focus` around L212-227); collapsing to it
is the smallest change that removes the bad signal.

## Scope

(1) `ap2/focus_advance.py` `_maybe_advance_focus`: delete the
`if active.has_done_when() and active.done_when_bullets:` branch that
invokes `_judge_done_when` (currently L193-211). Make the empty-cycles
path (currently the `else` branch L212-227) the unconditional path —
runs for every focus regardless of Done-when bullet presence.

(2) `ap2/focus_advance.py`: delete `_judge_done_when` (the ~100-line
async function at L286 and below) entirely, including its docstring
and prompt-building block.

(3) `ap2/daemon.py`: drop the `_judge_done_when` import + re-export
(L1714-1723 region). Re-exports of `_maybe_advance_focus` and
`_ideation_empty_against_focus` stay so existing test seams remain.

(4) `ap2/goal.py`: delete `done_when_judge_effort()` (~L423-440) and
any reference to the `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` env knob.

(5) `ap2/events.py`: the `focus_advanced trigger` doc-comment
(~L126) currently lists `done_when_judge / empty_cycles_heuristic`.
Drop `done_when_judge` from the documented set so only
`empty_cycles_heuristic` (and `pointer_past_last` for the
roadmap-complete edge) remain.

(6) Update `ap2/tests/test_tb226_focus_rotation.py`: remove tests
that monkey-patch `daemon._judge_done_when` or that exercise the
`done_when_judge` trigger path. Tests covering the empty-cycles
path stay and become the sole advance-path tests.

## Design

Two paths already coexist (`focus_advance.py` L193 branches on
`has_done_when()`); this collapses to one. The empty-cycles signal
is grounded in observable ideation behavior — each ideation cycle
either records a proposal or doesn't, no separate verdict needed.
Risk: a focus with legitimate Done-when bullets and slow ideation
could advance on three consecutive 0-proposal ticks; mitigated by
the consecutive-counter design (one fresh `ideation_proposal_recorded`
resets) and the default threshold of 3. Operator escape if the
heuristic misfires: `ap2 update-goal` to re-extend, or the existing
`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` kill-switch as full-stop.

The `Done when:` sub-block in goal.md focus headings is not deleted
by this task — it remains as operator-authored advisory text (useful
ideation prompt context describing the shape of substantive progress).
A follow-up rename TB converts it to `Progress signals:` to remove
the gate-y connotation now that no code path reads it as a gating
criterion.

## Verification

- `! grep -q '_judge_done_when' ap2/focus_advance.py` — judge function removed from focus_advance.
- `! grep -q '_judge_done_when' ap2/daemon.py` — judge re-export removed from daemon.
- `! grep -q 'AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT' ap2/goal.py` — env knob no longer referenced.
- `! grep -q 'done_when_judge' ap2/events.py` — trigger value no longer documented.
- `grep -q 'empty_cycles_heuristic' ap2/focus_advance.py` — heuristic path still wired.
- `grep -q 'AP2_FOCUS_ADVANCE_EMPTY_CYCLES' ap2/goal.py` — empty-cycles threshold env knob preserved.
- `uv run pytest -q ap2/tests/test_tb226_focus_rotation.py` — focus-rotation tests pass against the single-path advance.
- `uv run pytest -q` — full suite passes.

## Out of scope

- The scrub pass on `ideation_state.md` to remove self-confirming
  exhaustion language (separate follow-up TB).
- Renaming `Done when:` → `Progress signals` in `goal.md` format
  and parser (separate follow-up TB; depends on this one landing
  first).
- Updating `ap2/howto.md` for the new advancement model (separate
  follow-up TB that runs after the mechanism + rename land).
- Tuning the default value of `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
  (current default 3; reassess only if 3 turns out to be too
  eager after live observation).
- Deleting the latent `focus_exhausted` skip predicate in
  `ap2/ideation.py` (depends on the scrub-pass TB removing the
  cached exhausted statuses the predicate misreads; folds into
  the scrub TB's scope).
