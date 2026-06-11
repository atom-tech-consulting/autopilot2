"""TB-273: Pin `ap2/ideation.default.md`'s "Shell-bullet pitfalls to
AVOID" section to the howto's authoritative four-pitfall list.

`ap2/howto.md` L462-505 is the single source of truth for shell-bullet
pitfalls — it carries all four pitfalls plus a worked example combining
them. Pre-TB-273, `ap2/ideation.default.md`'s pitfalls section listed
only three pitfalls (bare `python`, bare-path-as-command, multi-line
bullets) — none of which named the four howto pitfalls and, critically,
none of which warned about the absence-check `!` exit-inversion prefix
that caused the TB-270 retry storm on 2026-05-20T04:54-05:59Z (3
retries → operator-manual unfreeze).

This module is the regression pin for the prompt-vs-howto sync. The
assertions are pure file-pattern matches with no runtime path
exercised; the value is preventing a future edit to the ideation prompt
from silently dropping or rewording one of the four pitfall headings —
the failure surface would be ideation-authored briefings re-tripping
the exact class of failure TB-270 forced a manual unfreeze on.

Pinned bullet-by-bullet per briefing §Scope §3:

  (a) all four pitfall-identifying substrings present in the section
      (`literal backtick`, `! grep`, `grep -r`, `Prose:`);
  (b) cross-reference to `ap2/howto.md` L462-505 (worked example
      stays in a single source of truth — the howto);
  (c) section heading still present AND the new `! grep` + `Prose:`
      strings are present in the same section (the absence of either
      would be the regression we're pinning against).

TB-400 note: the four-pitfall worked example + section were consolidated
out of `ap2/howto.md` into the operator-facing `ap2-task` skill
(`skills/ap2-task/SKILL.md`), so the howto-side companion check
(`test_skill_still_carries_all_four_pitfalls`) now reads the skill. The
`ap2/ideation.default.md` content pins (the four `test_pitfall_*` checks +
`test_section_cross_references_howto_worked_example`) are UNCHANGED — the
canonical prompt still anchors its cross-reference at `ap2/howto.md`
L462-505 because TB-400 left `ideation.default.md` untouched (it stays the
daemon-canonical copy); repointing the prompt's own cross-reference at the
skill is deferred to the later howto-retirement work.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
IDEATION_PROMPT = REPO_ROOT / "ap2" / "ideation.default.md"
HOWTO = REPO_ROOT / "ap2" / "howto.md"
# TB-400 — the four-pitfall worked example + section were consolidated out
# of `ap2/howto.md` into the operator-facing `ap2-task` skill, which now
# mirrors `ap2/ideation.default.md`'s canonical pitfalls section. The
# howto-side companion check below reads the skill instead of the howto.
TASK_SKILL = REPO_ROOT / "skills" / "ap2-task" / "SKILL.md"

SECTION_HEADING = "## Shell-bullet pitfalls to AVOID (TB-76 — observed in prod)"


def _read_pitfalls_section() -> str:
    """Return the body of `## Shell-bullet pitfalls to AVOID ...`
    in `ap2/ideation.default.md` — heading line inclusive, stopping
    at the next `## ` heading or EOF.

    The section-scoped read is load-bearing: assertions below check
    substrings that COULD appear elsewhere in the prompt (e.g.
    `Prose:` is mentioned in the briefing-shape teaching earlier on)
    so naive whole-file greps would mask a regression that dropped
    the bullet from THIS section.
    """
    text = IDEATION_PROMPT.read_text(encoding="utf-8")
    start = text.find(SECTION_HEADING)
    assert start >= 0, (
        f"TB-273: section heading {SECTION_HEADING!r} missing from "
        f"{IDEATION_PROMPT}. The pitfalls section was removed or "
        "renamed; the ideation prompt no longer carries the "
        "shell-bullet authoring guidance."
    )
    # Find the next `## ` heading after this one (or EOF). The
    # section ends just before the next `## ` line.
    rest = text[start + len(SECTION_HEADING):]
    next_h2 = re.search(r"\n## ", rest)
    end = start + len(SECTION_HEADING) + (next_h2.start() if next_h2 else len(rest))
    return text[start:end]


# ---------------------------------------------------------------------------
# Scope §3(a) — all four pitfall-identifying substrings present
# ---------------------------------------------------------------------------


def test_pitfall_literal_backtick_present():
    """TB-207 pitfall #1: "No literal backticks in the command body."
    Verbatim-aligned to `ap2/howto.md` L464. Absence here means the
    ideation prompt lost the pitfall and a future ideation-authored
    briefing could ship a single-backtick codespan with an inner
    backtick — the kind/malformed surface TB-219 added as a backstop
    is a verifier FAIL, not a teaching channel for the ideation
    agent, so the pitfall must live in the prompt itself.
    """
    section = _read_pitfalls_section()
    assert "literal backtick" in section, (
        "TB-273: 'literal backtick' (TB-207 pitfall #1) is missing "
        "from the ideation prompt's Shell-bullet pitfalls section. "
        "Sync with ap2/howto.md L462-505."
    )


def test_pitfall_absence_check_bang_grep_present():
    """TB-270 pitfall #2: absence-check shell bullets need `! grep`.
    This is the bullet whose miss caused the 2026-05-20T04:54-05:59Z
    retry storm — 3 retries exhausted, operator-manual unfreeze
    required. The headline regression-pin: any future edit that
    drops `! grep` from the pitfalls section recreates the exact
    failure class TB-273 was queued to close.
    """
    section = _read_pitfalls_section()
    assert "! grep" in section, (
        "TB-273: '! grep' (TB-270 absence-`!` pitfall — the bullet "
        "whose miss caused the 2026-05-20 retry storm) is missing "
        "from the ideation prompt's Shell-bullet pitfalls section. "
        "Sync with ap2/howto.md L479-484."
    )


def test_pitfall_directory_walk_grep_r_present():
    """TB-204 pitfall #3: directory-walking grep needs `-r`. A bare
    `grep -lE 'pat' dir/` exits 2 with 'Is a directory' and reads
    as FAIL even when the work is correct. Pinning the substring
    `grep -r` covers both the prose form (`Use grep -rlE ...`) and
    a future cleaner rewrite that says `grep -r` more directly.
    """
    section = _read_pitfalls_section()
    assert "grep -r" in section, (
        "TB-273: 'grep -r' (TB-204 directory-walk pitfall) is "
        "missing from the ideation prompt's Shell-bullet pitfalls "
        "section. Sync with ap2/howto.md L485-488."
    )


def test_pitfall_prose_prefix_present():
    """TB-219 pitfall #4: `Prose:` prefix for judge bullets. The
    complement to the three shell pitfalls — if a bullet's
    grammatical subject is a backtick-fenced filename/symbol, lead
    with `Prose:` so the verifier routes the bullet to the judge
    instead of trying to `exec` the filename. Without this in the
    ideation prompt, ideation-authored Prose bullets risk landing
    as bare-path commands (the same class as TB-204 pitfall #3).
    """
    section = _read_pitfalls_section()
    assert "Prose:" in section, (
        "TB-273: 'Prose:' (TB-219 prose-prefix pitfall) is missing "
        "from the ideation prompt's Shell-bullet pitfalls section. "
        "Sync with ap2/howto.md L489-492."
    )


# ---------------------------------------------------------------------------
# Scope §3(b) — cross-reference to ap2/howto.md (worked example anchor)
# ---------------------------------------------------------------------------


def test_section_cross_references_howto_worked_example():
    """The worked example combining all four pitfalls lives in a
    single source of truth — `ap2/howto.md` L462-505 — and the
    ideation prompt's section must POINT to it rather than
    duplicate it. Duplication is the future-drift surface this
    proposal explicitly avoids (briefing §Design).

    Pin both:
      - the cross-reference path `ap2/howto.md` is named, AND
      - the L462-505 line-range anchor is named (so a future
        howto edit that shifts the example must update the
        ideation prompt's pointer in lockstep).
    """
    section = _read_pitfalls_section()
    assert "ap2/howto.md" in section, (
        "TB-273: cross-reference to ap2/howto.md is missing from "
        "the ideation prompt's Shell-bullet pitfalls section. The "
        "worked example must live in a single source of truth; "
        "the ideation prompt should reference it, not duplicate it."
    )
    assert "L462-505" in section, (
        "TB-273: the L462-505 line-range anchor for ap2/howto.md's "
        "worked example is missing from the ideation prompt's "
        "Shell-bullet pitfalls section. The line-range anchor makes "
        "future howto-vs-prompt drift greppable."
    )


# ---------------------------------------------------------------------------
# Scope §3(c) — heading still present AND new bullets co-located in section
# ---------------------------------------------------------------------------


def test_section_heading_still_present_with_new_bullets_co_located():
    """Defensive regression pin: the `## Shell-bullet pitfalls`
    heading exists AND the two highest-value new strings (`! grep`
    from TB-270, `Prose:` from TB-219) live in the SAME section as
    that heading.

    A future regression that split the section in half — moving the
    heading one place and the new bullets another — would mask
    itself from individual scope-§3(a) checks (substrings still
    present globally) but fail this co-location assertion. The
    legacy three-pitfall-only shape is what this pin rules out: if
    the section heading exists but the `! grep` + `Prose:` bullets
    are missing from THIS section, we've reverted to legacy.
    """
    section = _read_pitfalls_section()

    # Heading present (also asserted in _read_pitfalls_section but
    # repeated here so this test stands alone in failure messages).
    assert SECTION_HEADING in section, (
        f"TB-273: section heading {SECTION_HEADING!r} is missing — "
        "the pitfalls section was removed or renamed."
    )

    # Both new strings present in THIS section (not elsewhere).
    assert "! grep" in section and "Prose:" in section, (
        "TB-273: the section heading exists but the new "
        "post-TB-273 bullets (`! grep`, `Prose:`) are not co-located "
        "in the same section. This is the legacy three-pitfall-only "
        "shape regression — sync with ap2/howto.md L462-505."
    )


# ---------------------------------------------------------------------------
# Sanity pin: the ap2-task skill carries the operator-facing mirror of the
# four pitfalls (companion check so a future skill edit that drops one of
# the four pitfalls trips this module and forces the operator to decide
# whether to retire the pitfall from BOTH surfaces in lockstep).
#
# TB-400 repointed this from `ap2/howto.md` to `skills/ap2-task/SKILL.md`:
# the four-pitfall worked example + section were consolidated out of the
# howto into the operator-facing ap2-task skill. `ap2/ideation.default.md`
# stays the canonical daemon copy (pinned by the four `test_pitfall_*`
# checks above); the skill is now the operator-facing mirror.
# ---------------------------------------------------------------------------


def test_skill_still_carries_all_four_pitfalls():
    """If the ap2-task skill ever drops one of the four pitfalls, the
    ideation prompt's verbatim-aligned bullets lose their operator-facing
    mirror. Pin the skill-side too so the sync-direction (canonical
    prompt ↔ skill mirror) stays enforced.

    TB-400 moved the four-pitfall section + worked example out of
    `ap2/howto.md` into `skills/ap2-task/SKILL.md`; this check reads the
    skill (was: the howto).
    """
    skill_text = TASK_SKILL.read_text(encoding="utf-8")
    # Anchor on the section heading so we don't false-positive on text
    # that happens to appear elsewhere in the skill.
    assert "Shell bullets — four authoring pitfalls" in skill_text, (
        "TB-400: skills/ap2-task/SKILL.md's 'Shell bullets — four "
        "authoring pitfalls' section heading is missing. The skill "
        "mirrors the four-pitfall convention for operators; if it's "
        "been retired, retire the ideation prompt's bullets in "
        "lockstep."
    )
    for substring in ("literal backtick", "! grep", "grep -r", "Prose:"):
        assert substring in skill_text, (
            f"TB-400: skills/ap2-task/SKILL.md no longer carries "
            f"{substring!r}. The skill is the operator-facing mirror of "
            "the authoritative pitfall list in ap2/ideation.default.md; "
            "reconcile both together — don't let the prompt out-pace the "
            "skill."
        )
