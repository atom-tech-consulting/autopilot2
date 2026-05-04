# TB-159 — Briefing validator: require Goal section to cite a goal.md focus item or Done-when bullet

Tags: `#autopilot` `#ideation` `#briefing` `#validation` `#goal-alignment`

## Goal

Close the "gap-covering without drift" failure mode goal.md's "Current focus:
ideation quality" section calls out: "Reject proposals whose value is only
'make ap2 itself nicer' unless ap2-improvement is on the project's goal path.
Bias toward the target project's outcomes, not the meta-system's polish."

Today TB-154's `_validate_briefing_structure` (ap2/tools.py) only checks that
the canonical section names are present — a `## Goal` section can sit empty
or describe pure ap2-meta-polish unconnected to any focus item, and still
pass at queue-append time. The reviewer-gate catches drift after TB-N
allocation; we want a mechanical guard that fires *before* allocation so the
operator's review queue isn't polluted with off-goal proposals.

This advances goal.md's "Current focus: ideation quality" — specifically the
"Gap-covering without drift" failure mode (goal.md lines 50-59) — by making
goal-relevance a queue-append-time precondition rather than a soft norm.

## Scope

- `ap2/tools.py::_validate_briefing_structure` — extend to also fail when the
  `## Goal` section body (text between `## Goal` and the next `##` heading)
  contains no token from a derived `goal_anchors` set.
- `ap2/init.py` — add a `GOAL_ANCHOR_HEADINGS` constant naming the headings
  the validator scans in `goal.md` to derive anchors (default:
  `("Current focus", "Done when")`); single source of truth for both the
  validator and the lint.
- `ap2/check.py::_check_briefing_structure` — add the same check as a
  warning-level lint so existing on-disk briefings surface the gap without
  blocking.
- `ap2/prompts.py` — extend the operator_queue_append docstring + ideation
  prompt rule to name the new requirement.
- New tests in `ap2/tests/test_tools.py` and `ap2/tests/test_check.py`.

Out-of-scope: changing what counts as a goal.md anchor (e.g. adding
`## Mission` or `## Constraints`); enforcing anchor relevance with an SDK
judge (presence of an anchor token is sufficient for this pass).

## Design

`goal.md`'s top-level layout is stable: `## Mission`, `## Done when`,
`## Current focus: <topic>`, `## Non-goals`, `## Constraints`. The validator
will:

1. Parse `goal.md` once per validation call (cheap; <5KB file). Walk the
   `##` headings; collect:
   - The exact title text of every heading whose text starts with one of
     `GOAL_ANCHOR_HEADINGS` (default: `Current focus`, `Done when`).
   - For each `## Done when` bullet, the first 3-6 words verbatim
     (lowercase, punctuation-stripped) as candidate anchor phrases.
2. Build a `goal_anchors: set[str]` of normalized lowercase phrases.
3. When validating a briefing, extract the `## Goal` body, lowercase it, and
   reject if no anchor phrase appears as a substring. Error message names
   the available anchors so the author can pick one.
4. Fall back to "no validation" (skip the check, don't reject) when goal.md
   is missing or all-placeholder — the existing `_validate_briefing_structure`
   pattern of failing closed only on parseable inputs.

The `## Goal` body extraction reuses the same heading-walk pattern
`_validate_briefing_structure` already does for section presence; pull the
walk into a shared `_briefing_section_body(text, heading)` helper.

`ap2/check.py`'s lint emits a `briefing_goal_off_anchor` warning with the
same body-text → anchor-phrase check, lower-severity (does not exit non-zero).

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `uv run pytest -q ap2/tests/test_tools.py -k validate_briefing_structure` — the new
  goal-anchor cases pass.
- `grep -q "GOAL_ANCHOR_HEADINGS" ap2/init.py` — constant added.
- `grep -q "goal_anchors" ap2/tools.py` — validator path wires the anchor set.
- `grep -q "goal_anchors\|GOAL_ANCHOR" ap2/check.py` — lint path uses the
  same anchor source.
- New unit test `test_validate_briefing_rejects_goal_section_without_anchor`
  in `ap2/tests/test_tools.py` exercises a briefing whose `## Goal` body
  cites no anchor phrase from a stub goal.md and asserts
  `_validate_briefing_structure` returns a non-None error string mentioning
  "goal.md" or "anchor".
- New unit test `test_validate_briefing_accepts_goal_section_with_done_when_quote`
  in `ap2/tests/test_tools.py` exercises a briefing quoting a Done-when bullet
  and asserts the validator returns None.
- New unit test `test_validate_briefing_skips_anchor_check_when_goal_md_missing`
  in `ap2/tests/test_tools.py` asserts the new check no-ops with no goal.md.
- New test in `ap2/tests/test_check.py` named
  `test_check_briefing_emits_goal_off_anchor_warning` confirms `ap2 check`
  surfaces the lint without exiting non-zero.

## Out of scope

- SDK-judged anchor *relevance* (we enforce presence of an anchor token, not
  whether the proposal genuinely advances that anchor — that's the
  reviewer-gate's job).
- Migrating existing on-disk briefings; the lint surfaces them, the operator
  decides whether to rewrite or leave.
- Adding new heading types to `GOAL_ANCHOR_HEADINGS` (Mission, Constraints,
  Non-goals) — current focus + done-when is the goal-relevance signal we
  want this cycle.
