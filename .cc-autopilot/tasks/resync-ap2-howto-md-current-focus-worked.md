# TB-206 — Resync `ap2/howto.md` Current-focus worked-example with post-pivot `goal.md`; decouple example from focus rotation

Tags: `#autopilot` `#docs` `#code-quality` `#operator-surface` `#fix-briefing` `#regression-pin`

## Goal

Close the regression-cascade goal.md's `Current focus: code quality` focus's (2) **Operator-facing documentation** axis (goal.md L65-72) opened when the operator rotated the Current focus on 2026-05-12T17:02Z: `ap2/howto.md` L149-156's `### Current focus` worked-example block (added by TB-200) quotes `Current focus: ideation quality signal collection` verbatim, but `ap2/tests/test_docs.py::test_worked_example_quotes_appear_verbatim_in_goal_md` enforces verbatim presence of every blockquoted line in `goal.md` — and that heading no longer exists after the pivot. The project-wide pytest gate (`uv run pytest -q ap2/tests/`) now exits 1 on two `test_docs.py` failures, which means every newly-dispatched task automatically `verification_fails` its project-wide bullet regardless of its own work.

Why now: this is an active operational failure on the new Current focus: code quality theme. TB-203 (commit `1ed8a03`, 19:18:57Z) and TB-204 (commit `ecd5b2f`, 19:49:50Z) both committed scope-correct work — TB-203's new `test_docs_drift.py` (4 tests, all pass), TB-204's new `_briefing_fixtures.py` + ~30 inline-briefing migrations across 13 test files — but both went `verification_failed` and returned to Backlog because the project-wide gate failed on the pre-existing test_docs.py breaks. TB-204's commit message explicitly names the diagnosis: "Two pre-existing test_docs.py failures (unrelated to TB-204) reflect the operator's 2026-05-12 17:02Z goal.md pivot from 'ideation quality signal collection' to 'code quality' and persist on a clean checkout without these changes." TB-205 is approved and queued next — same gate, same failure. Without this fix, three operator-approved code-quality proposals cascade to retry-exhausted → Frozen before any of their work lands in Complete. Beyond the immediate unblock, the structural coupling (verbatim quote of the *currently-rotating* `## Current focus` heading) means the same failure mode recurs on every future operator focus rotation — operators cannot rotate focus via `ap2 update-goal` without simultaneously editing howto.md, an unstated coupling that violates goal.md L70-72's "operator who can't understand a surface from its documented description" in the *opposite* direction (the documented description silently breaks the daemon).

## Scope

(1) Update `ap2/howto.md`'s `### Current focus` subsection inside `## Authoring goal.md` (lines ~131-156): replace the verbatim quote of the pre-pivot heading at L151 (`> ## Current focus: ideation quality signal collection`) and the trailing explanatory paragraph at L153-156 (which names the substring `Current focus: ideation quality signal collection`) with focus-rotation-resilient content. Use exactly one of:

  - **Shape A (re-sync today, keep verbatim coupling)**: rewrite the blockquote as `> ## Current focus: code quality` (the post-pivot heading verbatim) and update the explanatory paragraph to name `Current focus: code quality` as the substring. Add a one-sentence inline note that the worked example must be re-synced via `ap2 update-goal` whenever the operator rotates the focus heading.

  - **Shape B (recommended — decouple example from focus rotation)**: rewrite the blockquote as `> ## Current focus: <theme name>` (literal placeholder), and rephrase the explanatory paragraph to discuss heading-title-as-anchor mechanically without naming any specific focus. The substring-citation example can read `Current focus: <theme name>` (placeholder) — the test will skip the placeholder line for the verbatim-in-goal-md check.

(2) Update `ap2/tests/test_docs.py` to match the chosen shape:
  - If Shape A: update `test_worked_example_current_focus_satisfies_anchor_validator`'s synthetic briefing to cite `Current focus: code quality` as its goal-anchor (instead of the pre-pivot string). Leave `test_worked_example_quotes_appear_verbatim_in_goal_md` strict.
  - If Shape B: add a tiny `_is_placeholder_line(line: str) -> bool` helper that returns True for any blockquote line containing `<theme name>` (or `<...>`-shape angle-bracket placeholder); have `test_worked_example_quotes_appear_verbatim_in_goal_md` skip such lines from the verbatim-match. AND change `test_worked_example_current_focus_satisfies_anchor_validator` to read the first `## Current focus:` heading from `goal.md` programmatically and use that title text as the anchor in its synthetic briefing — so the test follows operator focus rotations without code changes.

(3) Don't touch `ap2/init.py`'s `GOAL_TEMPLATE` — the placeholder template already uses focus-rotation-resilient stubs (per TB-199).

(4) Don't add an `ap2 check` warning for howto.md/goal.md drift or an `ap2 update-goal` post-pytest hook — TB-203's `test_docs_drift.py` already gates the surface-name catalog and these two `test_docs.py` tests gate the worked-example quotes. The full pytest gate carries the signal; no parallel surface.

(5) Do not modify the Mission / Done-when / Non-goals / Constraints worked-example blocks (L93-100, L121-130, L173-183, L202-207). Those quote stable goal.md content that doesn't rotate per-cycle; their verbatim coupling is intentional anti-drift and still pays its rent.

## Design

Shape B is the goal-aligned recommendation: it removes a structural coupling that operators didn't know existed and that will recur on every focus rotation. The cost is ~10-15 lines of test refactor (parse the first `## Current focus:` heading from goal.md, expose its title for the anchor test). Shape A is the cheap-and-narrow path — fixes today's break in <5 lines but leaves the same failure mode armed for the next operator pivot.

Heading-extraction helper for Shape B:

```python
import re

def _first_current_focus_title(goal_text: str) -> str | None:
    """Return the title text of the first `## Current focus:` heading,
    or None if no such heading exists. Example: returns 'code quality'
    for '## Current focus: code quality'."""
    m = re.search(r"^## Current focus:\s*(.+?)\s*$", goal_text, re.MULTILINE)
    return m.group(1) if m else None
```

For the verbatim-match test (Shape B), a small allow-list:

```python
def _is_placeholder_line(line: str) -> bool:
    return "<" in line and ">" in line and any(
        marker in line for marker in ("<theme name>", "<...>", "<placeholder>")
    )
```

The anti-drift gate's purpose for the four stable worked-example blocks (Mission / Done-when / Non-goals / Constraints) remains unchanged — those don't rotate per-cycle, so verbatim coupling there still catches real docs drift. Only the Current-focus worked example pays the rotation tax, and only that block uses the placeholder shape.

Operator-visibility consideration (Shape B): the placeholder `<theme name>` reads as a template hint, which is the correct semantic — operators authoring goal.md replace it with their actual theme, and the surrounding prose (heading-title-as-anchor mechanics) is what carries the learning value. The Mission / Done-when / Non-goals / Constraints blocks STILL quote real goal.md content (so readers see a filled-in model), which preserves the "operators have a real filled-in model" rationale TB-200's header paragraph (L75-79) names.

## Verification

- `uv run pytest -q ap2/tests/test_docs.py` — every `test_docs.py` test passes (exit 0); specifically `test_worked_example_quotes_appear_verbatim_in_goal_md` and `test_worked_example_current_focus_satisfies_anchor_validator` no longer fail.
- `uv run pytest -q ap2/tests/` — full regression suite green (exit 0); the project-wide gate that TB-203/TB-204/TB-205 trip on is unblocked.
- `! grep -nE "Current focus: ideation quality signal collection" ap2/howto.md` — exit 0 (zero matches; the stale pre-pivot focus quote is removed from howto.md). Idiom inverts the exit so the verifier sees pass-on-zero-matches per the TB-187 / TB-191 pattern (`ap2/verify.py:266` treats non-zero exit as fail).
- `grep -nE "## Current focus:" ap2/howto.md` — exit 0 (the worked-example block still exists with some `## Current focus:` heading shape — either the post-pivot literal or the `<theme name>` placeholder).
- Prose: the choice of Shape A vs Shape B is consistent across `ap2/howto.md`'s `### Current focus` subsection AND `ap2/tests/test_docs.py`'s two failing tests — i.e., if howto.md uses the `<theme name>` placeholder (Shape B), the test must skip placeholder lines from verbatim-match AND read the anchor from goal.md programmatically; if howto.md uses the post-pivot `code quality` heading verbatim (Shape A), the test must use `Current focus: code quality` in its synthetic briefing. Judge confirms via `Read` of both files.
- Prose: the Mission / Done-when / Non-goals / Constraints worked-example blocks in `ap2/howto.md` (L93-100, L121-130, L173-183, L202-207) are unchanged from the TB-200-shipped content — only the Current-focus block (L149-156) is rewritten. Judge confirms via `git diff` against commit `7d7c142`.

## Out of scope

- Auto-generating any part of `ap2/howto.md` from `goal.md` (paraphrased-docs failure mode per goal.md L70-72; the worked-example pattern itself stays human-authored, only the *coupling* with goal.md's specific focus title gets decoupled).
- Hardening the Mission / Done-when / Non-goal / Constraints worked-example blocks against future drift (those don't rotate per cycle; today's verbatim coupling there is the intentional anti-drift signal goal.md L70-72 names).
- Adding an `ap2 update-goal` post-step that runs pytest or an `ap2 check` warning that mirrors the test (parallel surface; the existing pytest gate is the authority).
- Unfreezing / retrying TB-203 or TB-204 directly — that's the operator's call after this lands. If either has retry budget remaining when TB-206 lands, they'll naturally re-dispatch and pass; if they've already retry-exhausted to Frozen, the operator runs `ap2 unfreeze TB-203` / `ap2 unfreeze TB-204` once TB-206 commits.
- Generalizing the placeholder-skip helper for any worked-example block beyond `### Current focus` (only Current-focus rotates; the others stay verbatim-coupled by design).
