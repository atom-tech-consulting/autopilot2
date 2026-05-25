# Rename Done-when sub-block to Progress signals in goal.md format

## Goal

Rename the per-focus `Done when:` sub-block in goal.md to
`Progress signals:` to reflect its new semantics: advisory outcome
guidance for ideation prompt context, not a gating criterion the
daemon auto-advances against. Make the block optional — a focus
heading with no `Progress signals:` sub-block is valid and parses
as an empty list. Closes the goal.md `## Done when` failure mode
"Ideation reliably proposes goal-aligned next steps that
substantively advance the goal (not just goal-shaped pro-forma
compliance)" by removing the footgun where operators author
shape-shaped criteria thinking they auto-fire focus advancement —
the criteria no longer gate anything once the done-when judge is
removed (separate TB), but the old name keeps the gating
connotation alive.

Why now: this rename is the operator-facing cleanup half of the
done-when judge removal arc. Without it, operators read "Done when:"
in the goal.md template and the howto and assume the criteria still
drive the loop's exit condition. The new name makes the advisory
role explicit ("here's what good progress looks like; the loop's
exit condition is empty-cycles") and prevents the next round of
shape-shaped criteria from being authored under false pretenses.
Today's parser (`ap2/goal.py` `_DONE_WHEN_INLINE_RE` and
`_DONE_WHEN_SUBHEAD_RE` near L62-63) is a hard-coded match on the
literal "Done when" string in two regex shapes; the rename touches
a small, contained surface.

## Scope

(1) `ap2/goal.py`: rename `_DONE_WHEN_INLINE_RE` →
`_PROGRESS_SIGNALS_INLINE_RE` and `_DONE_WHEN_SUBHEAD_RE` →
`_PROGRESS_SIGNALS_SUBHEAD_RE`. Update regex bodies to match
`Progress signals:` (inline) and `### Progress signals` (subhead).
Rename `_parse_done_when_from_body` → `_parse_progress_signals_from_body`.

(2) `ap2/goal.py` `FocusItem`: rename `done_when_bullets` field →
`progress_signals_bullets`; rename `has_done_when()` method →
`has_progress_signals()`. Update all call sites in `ap2/` and
`ap2/tests/` (focus_advance.py was largely cleared by the
done-when judge removal TB; remaining call sites in goal.py
plus any ideation.py / status_report.py / tests).

(3) `goal.md` (the in-repo target file): rewrite each
`## Current focus:` heading's `Done when:` sub-block heading to
`Progress signals:`. Do NOT alter bullet content; this is a
rename, not a content edit.

(4) Confirm optionality: a `## Current focus:` heading without a
`Progress signals:` sub-block must parse as a `FocusItem` with
`progress_signals_bullets=None` (or `[]`) and
`has_progress_signals()` returning False. The current parser
already supports absence-as-None per `_parse_done_when_from_body`'s
docstring; verify the renamed function preserves that behavior
and pin in a test.

(5) Hard cut on the parser: do NOT accept the legacy `Done when:`
heading. Per the project's commit-vs-gitignore-rule norm, git
history is the rollback substrate; a backcompat shim is not
warranted for a goal.md format change.

(6) Update `ap2/init.py:BRIEFING_TEMPLATE` and any goal.md
template strings in `ap2/init.py` if they seed a `Done when:`
sub-block — rename to `Progress signals:`.

## Design

This rename is purely a vocabulary swap; semantics already changed
when the done-when judge was deleted in the prior TB. Two regex
constants + one field + one method + a handful of call sites. The
hard cut is safe because the project doesn't ship to external
consumers — every goal.md is in-tree and updated in the same
commit. The optional-block design was already implicit in the old
parser (`_parse_done_when_from_body` returns `None` when no marker
matches); naming this behavior explicitly in the renamed function's
docstring and pinning it in a test makes it a contract.

## Verification

- `! grep -rq '_DONE_WHEN' ap2/` — old regex constants fully renamed.
- `! grep -rqE 'done_when_bullets|has_done_when' ap2/` — old field/method names fully renamed.
- `! grep -qE '^Done when:|^### Done when' goal.md` — in-repo goal.md uses the new heading.
- `grep -q '_PROGRESS_SIGNALS' ap2/goal.py` — new regex constants present.
- `grep -qE 'progress_signals_bullets|has_progress_signals' ap2/goal.py` — new field/method names present.
- `grep -qE '^Progress signals:|^### Progress signals' goal.md` — in-repo goal.md uses the new heading.
- `uv run pytest -q ap2/tests/` — full test suite passes against renamed surface.

## Out of scope

- Editing the bullet content under each focus's renamed
  `Progress signals:` block (rename only; the criteria themselves
  belong to operator-authored goal.md content).
- Updating `ap2/howto.md` to describe the new name and the new
  advancement model (separate follow-up TB; one howto sweep
  covers all renames + the mechanism change together).
- Adding a backcompat shim that accepts both `Done when:` and
  `Progress signals:` (deliberate hard cut per project norms).
- Removing the empty-cycles `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
  env knob or its default of 3 (knob stays; default reassessment
  is a separate later TB if needed).
## Attempts

### 2026-05-25 — blocked
Code-side rename landed in ac4f861 (scope items 1, 2, 4, 5, 6 complete; 1997 tests pass): renamed _DONE_WHEN_*_RE→_PROGRESS_SIGNALS_*_RE regex constants, _parse_done_when_from_body→_parse_progress_signals_from_body, FocusItem.done_when_bullets→progress_signals_bullets, has_done_when()→has_progress_signals(); added a hard-cut pin (legacy `Done when:` heading no longer parses) and an optional-block contract pin; updated test fixtures + helper names + docstrings across goal.py/focus_advance.py/daemon.py/events.py/test_tb226/test_walk_away_loop/test_briefing_validators; cleaned up TB-283 leftover `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` references in howto.md (narrow dead-doc removal, broader howto sweep stays TB-286). BLOCKED on scope item (3): goal.md L213 `Done when:` → `Progress signals:` rename requires operator action — goal.md is in ap2.tools.TASK_AGENT_FENCED_PATHS so _TASK_DISALLOWED_TOOLS rejects Edit(goal.md)/Write(goal.md) and CLAUDE.md prohibits Bash workarounds. The briefing's `! grep -qE '^Done when:|^### Done when' goal.md` and `grep -qE '^Progress signals:|^### Progress signals' goal.md` verification bullets will fail until the operator applies the one-line rename via `ap2 update-goal` (parser is already wired for the new heading and will pick it up on the next focus-list read). Briefing scope (3) and the CLAUDE.md fenced-file rule are in direct conflict — flagging so the operator can either run the manual update-goal or weaken scope (3) on the briefing.
- **commit:** ac4f861
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260525T063109Z-TB-285.prompt.md`, `stream: .cc-autopilot/debug/20260525T063109Z-TB-285.stream.jsonl`, `messages: .cc-autopilot/debug/20260525T063109Z-TB-285.messages.jsonl`
