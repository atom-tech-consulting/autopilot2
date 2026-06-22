"""Tests for the `## Authoring goal.md` section (TB-200, TB-206).

TB-403 carved the `## Authoring goal.md` section into the auto-triggered
`skills/ap2-ideation-goals/SKILL.md` (operator-facing goal/focus authoring +
the retrospective-audit workflow); TB-406 then retired the legacy operator
manual entirely. These gates therefore read the goal-authoring reference
from `IDEATION_GOALS_SKILL` now, not `HOWTO_PATH` — the same retarget shape
the env-knob / config-key / CLI-verb gates took onto their carve skills in
`ap2/tests/test_docs_drift.py`.

Anti-drift gates after TB-206's structural decoupling:

1. Heading + all five subsections + both validator TB-N references are present
   in the ap2-ideation-goals skill. These are stable structural anchors; a
   reader who greps for them lands in a valid section.
2. The Current-focus worked example's `## Current focus: <theme>` heading is
   itself usable as a TB-161 anchor: a synthetic briefing whose `## Goal` body
   cites that heading text passes `_validate_briefing_structure` against a
   synthetic `goal.md` carrying only that heading. This pins the contract the
   docs sell — if a reader follows the example, the result actually clears
   the queue-append gate — WITHOUT coupling to this repo's live `goal.md`
   content (the coupling that cascaded operator focus rotations into
   project-wide pytest failures pre-TB-206).
"""
from __future__ import annotations

from pathlib import Path

from ap2.tools import _validate_briefing_structure


REPO_ROOT = Path(__file__).resolve().parents[2]
# TB-403: goal-authoring reference now lives in this skill.
IDEATION_GOALS_SKILL = REPO_ROOT / "ap2" / "skills" / "ap2-ideation-goals" / "SKILL.md"


def _current_focus_worked_example_heading(skill_text: str) -> str:
    """Return the blockquoted `## Current focus: <theme>` line from the
    skill's `### Current focus` worked-example block.

    The Current-focus worked example is the one block whose blockquoted
    content must carry a real `##`-level heading (the whole point of the
    block is to demonstrate the heading shape). We pull that line so the
    anchor-validator test can build its synthetic fixture from it, keeping
    the test in sync with whatever fictional theme the skill names without
    hard-coding the string in two places.
    """
    start_marker = "### Current focus"
    start = skill_text.find(start_marker)
    assert start != -1, (
        "missing `### Current focus` heading in skills/ap2-ideation-goals/SKILL.md"
    )
    rest = skill_text[start + len(start_marker):]
    # Stop at the next `###`-level (sibling subsection) or `##`-level heading.
    next_h3 = rest.find("\n### ")
    next_h2 = rest.find("\n## ")
    stop = min(x for x in (next_h3, next_h2) if x != -1) if (
        next_h3 != -1 or next_h2 != -1
    ) else len(rest)
    section = rest[:stop]
    for raw in section.splitlines():
        line = raw.lstrip()
        if not line.startswith(">"):
            continue
        body = line[1:]
        if body.startswith(" "):
            body = body[1:]
        if body.startswith("## Current focus:"):
            return body.rstrip()
    raise AssertionError(
        "no `## Current focus:` line in `### Current focus` worked-example "
        "blockquote of skills/ap2-ideation-goals/SKILL.md"
    )


def test_authoring_section_present():
    """Heading + all five subsections + both validator TB-Ns are referenced."""
    text = IDEATION_GOALS_SKILL.read_text()
    assert "## Authoring goal.md" in text
    for sub in (
        "### Mission",
        "### Done when",
        "### Current focus",
        "### Non-goals",
        "### Constraints",
    ):
        assert sub in text, f"missing subsection {sub!r}"
    # Both validators referenced by TB-N so a reader can grep back to code.
    assert "TB-161" in text
    assert "TB-164" in text


def test_worked_example_current_focus_satisfies_anchor_validator(tmp_path):
    """The Current-focus worked example MUST itself be usable as a TB-161
    anchor: a synthetic briefing whose `## Goal` body cites the worked-
    example's Current-focus heading text passes `_validate_briefing_structure`.

    Self-contained: builds a tmp `goal.md` carrying ONLY the skill's quoted
    Current-focus heading (so the validator can mine an anchor from it) and
    a synthetic briefing whose `## Goal` body cites that heading. Decoupled
    from this repo's live `goal.md` per TB-206 — the prior coupling fired
    on every operator focus rotation and cascaded into project-wide pytest
    failures despite the rotating content being legitimate.
    """
    heading_line = _current_focus_worked_example_heading(IDEATION_GOALS_SKILL.read_text())
    # e.g. "## Current focus: webhook reliability" → "Current focus: webhook reliability"
    assert heading_line.startswith("## Current focus:"), (
        f"expected `## Current focus:` line; got {heading_line!r}"
    )
    heading_title = heading_line[len("## "):].strip()

    tmp_goal = tmp_path / "goal.md"
    tmp_goal.write_text(
        "# Project Goals\n\n"
        "## Mission\n\nFictional project for docs-test fixture.\n\n"
        f"{heading_line}\n\nNarrative naming the theme.\n"
    )

    briefing = (
        "# TB-TEST: synthetic anchor test\n\n"
        "## Goal\n\n"
        f"Extend test coverage for the `{heading_title}` theme "
        "so future refactors land with a confident regression net in "
        "place.\n\n"
        "Why now: closes the failure mode where a behavior tweak silently "
        "regresses because no test pins the invariant.\n\n"
        "## Scope\n\n"
        "- ap2/tests/\n\n"
        "## Design\n\n"
        "Add regression-pin tests for one underspecified surface.\n\n"
        "## Verification\n\n"
        "- `uv run pytest -q` — full suite passes\n\n"
        "## Out of scope\n\n"
        "- Anything else.\n"
    )
    err = _validate_briefing_structure(briefing, goal_md_path=tmp_goal)
    assert err is None, f"synthetic briefing rejected: {err!r}"
