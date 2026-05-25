# Update ap2/howto.md for empty-cycles advancement + Progress signals rename

## Goal

Reconcile `ap2/howto.md` with the new focus-advancement model after the
done-when judge removal, ideation_state.md scrub, and `Done when:` →
`Progress signals:` rename land. Closes the goal.md `## Done when`
failure mode "Ideation reliably proposes goal-aligned next steps that
substantively advance the goal (not just goal-shaped pro-forma
compliance)" by removing operator-facing docs that still describe the
deleted judge path, so future operators don't author shape-shaped
criteria expecting them to drive advancement. Operator workflow now
reads cleanly: extend a focus via `ap2 update-goal`, retire via
empty-cycles or `ap2 ack roadmap_complete` — no separate advance
command, no judge to tune.

Why now: this TB is the docs half of the advancement-mechanism rework.
Without it, the howto and the code disagree — the howto describes the
done-when judge as the primary advancement signal (with empty-cycles
as fallback) while the code uses empty-cycles unconditionally. The
divergence is a trap for the next operator who reads howto.md to
understand how the loop decides a focus is done. Standalone TB because
the docs sweep depends on all three preceding changes (judge removal,
scrub pass, rename) being landed and stable.

## Scope

(1) `ap2/howto.md`: rewrite the section describing focus advancement
to name empty-cycles as the sole signal (consecutive ideation
cycles producing zero proposals; threshold via
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`, default 3). Remove all references
to the done-when judge, `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT`, and the
"two paths" framing.

(2) `ap2/howto.md`: rename every `Done when:` mention to
`Progress signals:` in the focus-authoring guidance. Describe the
block as optional and advisory — useful ideation prompt context
describing what good progress looks like, never a gating signal.

(3) `ap2/howto.md`: document the post-ideation scrub pass briefly
under the ideation section (one paragraph): scrub removes
self-confirming exhaustion language from ideation_state.md so each
cycle reasons fresh; fail-open on LLM errors; model via
`AP2_IDEATION_SCRUB_MODEL`.

(4) `ap2/howto.md`: document operator advancement workflow —
`ap2 update-goal` to add/edit foci, `ap2 ack roadmap_complete` to
dismiss the parked-ideation notice when all foci exhaust, the
kill-switch `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` for full-manual
mode. No new commands; existing operator surface covers the
workflow.

(5) Audit `ap2/howto.md` for any other stale references to the
judge path (search for `judge`, `done_when`, `done-when`,
`shape-shaped`) and rewrite or delete as appropriate.

(6) Per the project's howto-structural-decoupling norm, worked
examples in howto.md must teach shape only — they must NOT quote
live `goal.md` content. Audit the rewritten sections to ensure
this norm holds.

## Design

This TB is purely an operator-doc rewrite — no code changes, no new
tests beyond the structural-decoupling check that already covers
howto.md. The rewrite has to land AFTER the three mechanism /
rename TBs so it reflects the final state, not an intermediate.
The shape: one section (focus advancement) gets rewritten end-to-end
rather than spot-edited, since the framing changed enough that
piecemeal edits would leave the section incoherent.

## Verification

- `! grep -qiE 'done.when judge|done_when_judge|AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT' ap2/howto.md` — judge references removed.
- `! grep -q 'Done when:' ap2/howto.md` — old block name fully renamed in docs.
- `grep -q 'Progress signals' ap2/howto.md` — new block name present in docs.
- `grep -qi 'empty.cycles' ap2/howto.md` — advancement model documented.
- `grep -q 'AP2_FOCUS_ADVANCE_EMPTY_CYCLES' ap2/howto.md` — threshold knob documented.
- `grep -qE 'AP2_IDEATION_SCRUB_MODEL|ideation.state.scrub' ap2/howto.md` — scrub pass documented.
- `uv run pytest -q ap2/tests/` — full suite passes (existing howto structural-decoupling tests cover the rewrite).

## Out of scope

- Any code changes (those land in the preceding three TBs).
- Generating a separate "migration guide" doc for the rename (the
  rename is a hard cut per the rename TB's design; the howto IS
  the new source of truth).
- Updating `ap2/ideation.default.md` (separate follow-up if the
  default ideation prompt also names the old block — its own
  surface, handled separately to keep this TB scoped to operator
  docs).
- Cross-linking to the `investigation-focus-done-when-premature.md`
  scratch doc (operator scratch is gitignored per project norms;
  the howto stands on its own).
