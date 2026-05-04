# TB-161 — Briefing validator: require non-empty "Why now" rationale within Goal section

Tags: `#autopilot` `#ideation` `#briefing` `#validation` `#scope-creep`

## Goal

Close the "push for progress without scope creep" failure mode goal.md's
"Current focus: ideation quality" section calls out: "every proposal needs
to pass an 'if we delete this and the goal still ships, was it useful?'
test." Today TB-154's `_validate_briefing_structure` enforces canonical
section presence but accepts any non-empty `## Goal` body — a briefing
proposing a feature whose only justification is "this would be cool" or
"it might be useful later" passes structurally and lands `@blocked:review`,
where the operator has to triage it without an explicit author-side
articulation of the delete-test.

This advances goal.md's "Current focus: ideation quality" — specifically the
"Push for progress without scope creep" failure mode (goal.md lines 61-70) —
by making the delete-test a queue-append-time precondition, forcing the
ideator to articulate the case in writing rather than hand-waving.

Pairs with TB-159 (goal-anchor citation): TB-159 ensures the proposal points
*at* a goal axis; TB-161 ensures the author has reasoned about whether the
specific work is the *right next chunk* on that axis.

## Scope

- `ap2/init.py::BRIEFING_TEMPLATE` — extend the `## Goal` template body to
  include a "Why now (delete-test):" line and prompt prose describing the
  goal.md delete-test.
- `ap2/tools.py::_validate_briefing_structure` — reuse the
  `_briefing_section_body` helper (added in TB-159 if it lands first;
  otherwise inline the heading-walk) to extract `## Goal` body. Reject when
  the body lacks a `Why now` token (case-insensitive substring match) OR
  when the `Why now` paragraph is shorter than 40 chars after stripping the
  marker. Error message: `"## Goal section must include a non-empty 'Why
  now' rationale (goal.md's delete-test)"`.
- `ap2/check.py::_check_briefing_structure` — same check as warning-level
  lint.
- `ap2/prompts.py` — extend the operator_queue_append docstring + ideation
  prompt rule to name the requirement; cite goal.md's "delete-test"
  phrasing verbatim so the agent has the exact framing.
- New tests in `ap2/tests/test_tools.py` and `ap2/tests/test_check.py`.

Out-of-scope: SDK-judged rationale *quality* (presence + minimum length is
the mechanical guard; the reviewer-gate plus the operator's `ap2 reject`
verb cover the quality dimension).

## Design

`Why now` is the simplest unambiguous marker the validator can grep for; it
mirrors goal.md's own framing and reads naturally inline:

    ## Goal

    Close the "drift into ap2-meta polish" failure mode...

    Why now: TB-154 only checks section presence, so an ap2-meta proposal
    can sit @blocked:review for hours before the operator triages — the
    queue-append-time guard is cheaper than the review round-trip.

The 40-char minimum prevents trivial passes like `Why now: yes` while
staying short enough that templates don't feel padded. Pulled out as a
named constant `WHY_NOW_MIN_CHARS` in `ap2/init.py` so tests pin it.

The marker check is *line-anchored* via regex `(?im)^\s*why now[\s:]` —
must appear at the start of a line (or after newline + whitespace) so it
isn't matched inside arbitrary prose ("the question of why now is hard…").

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `uv run pytest -q ap2/tests/test_tools.py -k why_now` — the new validator
  cases pass.
- `grep -q "Why now" ap2/init.py` — template carries the marker.
- `grep -q "WHY_NOW_MIN_CHARS\|why now" ap2/tools.py` — validator path
  references the constant or marker.
- `grep -q "why now\|delete-test" ap2/check.py` — lint path checks the
  same marker.
- `grep -q "delete-test\|Why now" ap2/prompts.py` — operator_queue_append
  docstring or ideation prompt rule names the requirement.
- `grep -q "Why now" ap2/ideation.default.md` — ideation prompt body cites
  the marker so the ideator authors briefings that satisfy the gate.
- New unit test `test_validate_briefing_rejects_goal_without_why_now`
  in `ap2/tests/test_tools.py` exercises a `## Goal` body without the
  marker and asserts the validator returns a non-None error string
  containing `"Why now"`.
- New unit test `test_validate_briefing_rejects_why_now_below_min_chars`
  in `ap2/tests/test_tools.py` exercises `Why now: yes` and asserts
  rejection.
- New unit test `test_validate_briefing_accepts_goal_with_why_now_paragraph`
  in `ap2/tests/test_tools.py` exercises a 60+ char Why-now paragraph and
  asserts the validator returns None.
- New unit test `test_validate_briefing_why_now_check_is_line_anchored`
  in `ap2/tests/test_tools.py` confirms the marker isn't matched mid-prose
  (e.g. "considered why now" inside a sentence does not satisfy the gate).
- New test in `ap2/tests/test_check.py` named
  `test_check_briefing_emits_why_now_warning` confirms `ap2 check`
  surfaces the lint without exiting non-zero.

## Out of scope

- SDK-judged rationale quality. Presence + 40-char minimum is the
  mechanical guard; deeper "is this rationale actually convincing?"
  reasoning is the reviewer-gate's job.
- Renaming the marker (e.g. "Delete test:" instead of "Why now:") — pick
  one phrasing for this cycle, iterate later if usage shows friction.
- Migrating existing on-disk briefings; the lint surfaces them, operator
  decides whether to backfill via `ap2 update TB-N --briefing-file ...`.
