"""Tests for the `## Authoring goal.md` section in `ap2/howto.md` (TB-200).

Two anti-drift gates:

1. Every quoted blockquote line in the Authoring-section's worked-example
   blocks still appears verbatim in this repo's `goal.md`. Catches silent
   drift the day someone edits goal.md without re-checking the howto.
2. A synthetic briefing whose `## Goal` body cites the Current-focus
   heading from the worked example passes `_validate_briefing_structure`.
   Pins the contract the docs sell: if a reader follows the example, the
   result actually clears the queue-append gate.
"""
from __future__ import annotations

from pathlib import Path

from ap2.tools import _validate_briefing_structure


REPO_ROOT = Path(__file__).resolve().parents[2]
HOWTO_PATH = REPO_ROOT / "ap2" / "howto.md"
GOAL_PATH = REPO_ROOT / "goal.md"


def _authoring_section(text: str) -> str:
    """Slice howto.md to the `## Authoring goal.md` section body."""
    start_marker = "## Authoring goal.md"
    start = text.find(start_marker)
    assert start != -1, "missing `## Authoring goal.md` heading in howto.md"
    rest = text[start + len(start_marker):]
    # Stop at the next `##`-level heading (any sibling section).
    next_h2 = rest.find("\n## ")
    return rest[:next_h2] if next_h2 != -1 else rest


def _blockquote_lines(section: str) -> list[str]:
    """Every `>`-prefixed line in the section with the marker stripped.

    Standard blockquote shape: `>` + one space + body. We strip exactly
    that so nested indentation inside the quoted bullet (the 2-space
    continuation indent of a markdown bullet) survives intact and matches
    goal.md's actual leading whitespace verbatim.
    """
    out: list[str] = []
    for raw in section.splitlines():
        line = raw.lstrip()
        if not line.startswith(">"):
            continue
        body = line[1:]
        # Drop at most one separator space so `>   continuation` → `  continuation`.
        if body.startswith(" "):
            body = body[1:]
        out.append(body)
    return out


def test_authoring_section_present():
    """Heading + all five subsections + both validator TB-Ns are referenced."""
    text = HOWTO_PATH.read_text()
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


def test_worked_example_quotes_appear_verbatim_in_goal_md():
    """Anti-drift gate: every quoted line in the worked-example blocks
    still appears verbatim in this repo's `goal.md`. If someone edits
    goal.md (or rewords a quote in howto.md) and forgets to keep them in
    sync, this test fails loudly rather than letting the example silently
    drift into fiction.
    """
    section = _authoring_section(HOWTO_PATH.read_text())
    goal_text = GOAL_PATH.read_text()
    quoted = _blockquote_lines(section)
    assert quoted, "expected at least one blockquoted worked-example line"
    for line in quoted:
        if not line.strip():
            # Blank `>` lines preserve paragraph breaks; not searchable text.
            continue
        assert line in goal_text, (
            f"worked-example quote not found verbatim in goal.md: {line!r}"
        )


def test_worked_example_current_focus_satisfies_anchor_validator():
    """The Current-focus worked example MUST itself be usable as a TB-161
    anchor: a synthetic briefing whose `## Goal` body cites the worked-
    example's Current-focus heading text passes `_validate_briefing_structure`.
    Pins the contract the docs sell — if a reader follows the example, the
    result actually clears the queue-append gate (both the TB-161 goal-
    anchor check and the TB-164 Why-now check fire here unchanged).
    """
    briefing = (
        "# TB-TEST: synthetic anchor test\n\n"
        "## Goal\n\n"
        "Extend ideation's signal collection for the `Current focus: "
        "ideation quality signal collection` theme so future cycles "
        "tune against evidence rather than intuition.\n\n"
        "Why now: closes the volume-bottleneck failure mode where "
        "ideation can't ground proposals in operator-decision history.\n\n"
        "## Scope\n\n"
        "- ap2/ideation.py\n\n"
        "## Design\n\n"
        "Capture proposal outcomes per cycle.\n\n"
        "## Verification\n\n"
        "- `uv run pytest -q` — full suite passes\n\n"
        "## Out of scope\n\n"
        "- Anything else.\n"
    )
    err = _validate_briefing_structure(briefing, goal_md_path=GOAL_PATH)
    assert err is None, f"synthetic briefing rejected: {err!r}"
