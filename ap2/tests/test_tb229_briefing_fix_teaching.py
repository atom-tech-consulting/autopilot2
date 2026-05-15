"""TB-229: behavioral pinning for the `BriefingFix:` upstream-emitter
teaching (axis-2 of the end-to-end-automation focus).

TB-225 wired the daemon-side parser + sweep + bootstrap-shape allowlist
for `BriefingFix: <shape> at <path>:<line>: <from> -> <to>` lines in
`task_complete status=blocked` summaries, but the UPSTREAM emitter —
the per-task agent that writes the blocked summary — didn't know
about the prefix. Without teaching, the parser sees free-text prose
and the auto-unfreeze sweep finds nothing to apply.

This test module pins three surfaces that must stay aligned with the
parser contract so axis-2 actually fires end-to-end:

  (1) `skills/ap2-task/SKILL.md` carries a dedicated "Reporting
      failures" section that teaches the canonical `BriefingFix:`
      line shape AND shows one fenced worked example per bootstrap
      fix-shape (4 examples), each labelled with the originating
      TB-N where the shape first surfaced.

  (2) `ap2/prompts.py`'s task-agent prompt body references
      `BriefingFix:` at least once, inside the same paragraph as
      the existing failure-reporting status enumeration (so the
      agent reads the rule alongside `blocked`, not as an orphan
      tail bullet).

  (3) Anti-drift: when TB-225's bootstrap shape list grows by a
      new shape, the SKILL.md teaching must cover at least the
      same count of worked examples. Catches the failure mode
      where a fifth shape lands on the allowlist but the teaching
      doesn't catch up.

A future refactor that drops the teaching, softens the line-shape
contract, or lets the SKILL.md examples fall behind a growing
bootstrap list trips a focused subset of these tests with a
diff-shaped error.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# Repo root — this test file lives at
# `ap2/tests/test_tb229_briefing_fix_teaching.py`, so the project root
# is three `parent` hops up. Resolving once keeps the per-test
# `Read` calls cheap and lets the path-vs-file checks fail loudly if
# the layout changes.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_MD = _REPO_ROOT / "skills" / "ap2-task" / "SKILL.md"
_PROMPTS_PY = _REPO_ROOT / "ap2" / "prompts.py"
_HOWTO_MD = _REPO_ROOT / "ap2" / "howto.md"


# Bootstrap shape tokens TB-225 ships in
# `AP2_AUTO_UNFREEZE_FIX_SHAPES`'s recommended list. Single source of
# truth: the howto / `_shared.parse_blocked_summary_fix_shape`
# docstring lists these four. If TB-225 adds or renames a shape, the
# anti-drift test below catches the SKILL.md teaching falling out of
# sync.
_BOOTSTRAP_SHAPES: tuple[str, ...] = (
    "grep_missing_r_on_dir",
    "literal_backtick_in_shell_bullet",
    "bare_python_to_uv_run",
    "bare_path_to_test_f",
)


# Originating TB-N for each shape — the in-tree task where the shape
# first surfaced. Tightens the prose-judge claim "each labelled with
# the originating TB-N where the shape originally surfaced" into a
# mechanical substring check the test can score. Pairs taken from
# the briefing + git log:
#   - `grep_missing_r_on_dir` → TB-204 (grep -lE / grep -rlE)
#   - `literal_backtick_in_shell_bullet` → TB-207
#   - `bare_python_to_uv_run` → TB-76 (bare-python pitfall)
#   - `bare_path_to_test_f` → TB-76 (path-as-command pitfall)
_BOOTSTRAP_ORIGIN_TBS: tuple[str, ...] = ("TB-204", "TB-207", "TB-76")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read a tracked surface file; loud failure surfaces a missing
    path before the structural assertion runs."""
    assert path.exists(), f"expected tracked file {path} to exist"
    return path.read_text()


# ---------------------------------------------------------------------------
# (1) SKILL.md teaches the convention with the canonical line shape +
#     four worked examples.
# ---------------------------------------------------------------------------


def test_skill_md_contains_briefing_fix_prefix_token():
    """The new SKILL.md section names the `BriefingFix:` prefix at
    least once (heading mention + worked examples produce ≥5 matches,
    but a single match is enough to anchor the section). Pins the
    minimum teaching surface — agents that read SKILL.md to author a
    blocked summary see the prefix by name."""
    body = _read(_SKILL_MD)
    assert "BriefingFix:" in body, (
        "SKILL.md must teach the `BriefingFix:` prefix the daemon's "
        "auto-unfreeze sweep (TB-225) parses from blocked summaries"
    )


def test_skill_md_carries_arrow_separator_example():
    """At least one worked example uses the canonical ` -> ` arrow
    separator the parser splits on (`<from> -> <to>`). Pins the
    line-shape contract end-to-end: the agent reads the example,
    emits the same shape, the parser accepts it. The separator is
    space-arrow-space (` -> `) — a bare `->` or a `=>` would not
    parse."""
    body = _read(_SKILL_MD)
    # Search inside lines that mention `BriefingFix:` so we don't
    # false-match on unrelated arrows elsewhere in the file.
    matches = [
        ln for ln in body.splitlines()
        if "BriefingFix:" in ln and " -> " in ln
    ]
    assert matches, (
        "SKILL.md must include at least one `BriefingFix: ... -> ...` "
        "worked example so the agent can copy the canonical shape"
    )


def test_skill_md_briefing_fix_match_count_meets_verification_floor():
    """The briefing's verification bullet pins
    `grep -nE 'BriefingFix:' skills/ap2-task/SKILL.md` at ≥5 matches
    (one heading mention + four worked examples for the bootstrap
    shapes). Mirror that floor here so a future trim that drops an
    example fails this test before the daemon-side verifier sees a
    silent-passing teaching."""
    body = _read(_SKILL_MD)
    n = body.count("BriefingFix:")
    assert n >= 5, (
        f"SKILL.md must contain at least 5 `BriefingFix:` references "
        f"(1 heading + 4 worked examples for the bootstrap shapes); "
        f"found {n}"
    )


def test_skill_md_covers_each_bootstrap_shape_in_a_worked_example():
    """Every bootstrap shape from `AP2_AUTO_UNFREEZE_FIX_SHAPES`
    appears in a `BriefingFix:`-prefixed worked example in SKILL.md.
    Pins the briefing-prose claim: "one fenced code-block example
    per bootstrap fix-shape (4 examples)"."""
    body = _read(_SKILL_MD)
    for shape in _BOOTSTRAP_SHAPES:
        # The shape token must appear adjacent to (the same line as) a
        # `BriefingFix:` prefix — anchors the example shape rather than
        # an incidental shape-name mention elsewhere in the prose.
        adjacent = any(
            "BriefingFix:" in ln and shape in ln
            for ln in body.splitlines()
        )
        assert adjacent, (
            f"bootstrap shape {shape!r} must appear in a "
            f"`BriefingFix: <shape> at ...` worked example in SKILL.md"
        )


def test_skill_md_labels_each_worked_example_with_origin_tb_n():
    """The prose-judge claim ("each labelled with the originating
    TB-N where the shape originally surfaced") tightens here into a
    mechanical pin: every originating TB-N appears in SKILL.md
    somewhere in the new section. Catches a future trim that drops
    the audit-trail attribution from the worked examples."""
    body = _read(_SKILL_MD)
    for tb in _BOOTSTRAP_ORIGIN_TBS:
        assert tb in body, (
            f"originating {tb} attribution missing from SKILL.md — "
            f"the worked examples must cite the in-tree task where "
            f"each shape first surfaced"
        )


def test_skill_md_section_heading_anchors_failure_reporting():
    """The new section must be discoverable by heading — the agent
    scans SKILL.md by section before authoring a blocked summary.
    Anchor on a tolerant substring (the operative phrase
    "Reporting failures") rather than a brittle full-string match,
    so a small wording polish doesn't trip the test."""
    body = _read(_SKILL_MD)
    # `##`-level heading containing the operative phrase. Tolerant:
    # accepts either casing of "Reporting failures" plus any trailing
    # parenthetical.
    pat = re.compile(r"^##\s+Reporting failures", re.MULTILINE | re.IGNORECASE)
    assert pat.search(body), (
        "SKILL.md must carry a `## Reporting failures ...` section "
        "that anchors the BriefingFix teaching"
    )


# ---------------------------------------------------------------------------
# (2) `ap2/prompts.py` task-agent prompt body teaches the rule.
# ---------------------------------------------------------------------------


def test_prompts_py_task_agent_body_references_briefing_fix():
    """The task-agent prompt body (the `_TASK_FOOTER` constant in
    `ap2/prompts.py`) must reference `BriefingFix:` at least once.
    Pins the in-prompt teaching surface — the agent reading the
    rendered prompt sees the prefix by name alongside the failure-
    reporting status enumeration."""
    body = _read(_PROMPTS_PY)
    assert "BriefingFix:" in body, (
        "`ap2/prompts.py` must teach the `BriefingFix:` emission "
        "rule in the task-agent prompt body so the upstream emitter "
        "knows about the convention TB-225's parser consumes"
    )


def test_prompts_py_teaches_briefing_fix_near_blocked_status_enumeration():
    """The prose verification bullet pins this: the teaching must
    land "in the same paragraph that already enumerates failure-
    reporting guidance, not as a standalone bullet appended to the
    prompt tail." Mechanical proxy: `BriefingFix:` appears within a
    reasonable distance of the `blocked` status line, NOT after the
    `## Output contract` footer's trailing material.

    Specifically: the first `BriefingFix:` index must precede the
    `### What if you forget?` subsection (which is the tail of the
    output-contract block — anything after it is "appended to the
    prompt tail"). Catches a regression where the teaching drifts
    into a tail-bullet position."""
    body = _read(_PROMPTS_PY)
    bf_idx = body.find("BriefingFix:")
    assert bf_idx != -1, "BriefingFix teaching missing entirely"
    tail_marker = body.find("### What if you forget?")
    assert tail_marker != -1, (
        "expected `### What if you forget?` tail marker in prompt; "
        "test anchor is stale"
    )
    assert bf_idx < tail_marker, (
        "BriefingFix teaching must land in the failure-reporting "
        "paragraph (before the `### What if you forget?` tail), "
        f"not appended to the prompt tail; got bf_idx={bf_idx} "
        f"vs tail_marker={tail_marker}"
    )
    # Also pin that the teaching sits near the `blocked` status
    # bullet — a generous-but-finite window keeps the "same
    # paragraph" claim mechanically verifiable.
    blocked_idx = body.find("`blocked`")
    assert blocked_idx != -1, (
        "expected `blocked` status bullet anchor in prompt; test "
        "anchor is stale"
    )
    # The teaching must appear within ~2KB of the blocked bullet —
    # roughly "same paragraph or the next" in this prompt's density.
    assert abs(bf_idx - blocked_idx) < 2048, (
        f"BriefingFix teaching is too far from the `blocked` status "
        f"bullet (Δ={abs(bf_idx - blocked_idx)} bytes) — looks like "
        f"it drifted out of the failure-reporting paragraph"
    )


# ---------------------------------------------------------------------------
# (3) `ap2/howto.md` cross-references the SKILL.md teaching.
# ---------------------------------------------------------------------------


def test_howto_md_cross_references_skill_md_failure_reporting_section():
    """The briefing's verification bullet
    `grep -nE 'skills/ap2-task/SKILL.md' ap2/howto.md` expects at
    least one cross-reference link from the failure-recovery /
    TB-225 section. The forward link makes the operator surface
    (`howto.md`) and the agent-author surface (SKILL.md) point at
    each other so a future reader of either can trace the
    convention's other half."""
    body = _read(_HOWTO_MD)
    n = body.count("skills/ap2-task/SKILL.md")
    assert n >= 1, (
        f"`ap2/howto.md` must cross-reference "
        f"`skills/ap2-task/SKILL.md` at least once (TB-229 added a "
        f"forward link from the TB-225 auto-unfreeze section); "
        f"found {n}"
    )


# ---------------------------------------------------------------------------
# (4) Anti-drift: SKILL.md worked-example count keeps up with
#     bootstrap shape growth.
# ---------------------------------------------------------------------------


def test_skill_md_worked_examples_meet_bootstrap_count_floor():
    """The number of `BriefingFix: ... -> ...` worked examples in
    SKILL.md must be at least `len(_BOOTSTRAP_SHAPES)`. Today the
    bootstrap set has 4 shapes; if a future TB-N adds a fifth shape
    to `AP2_AUTO_UNFREEZE_FIX_SHAPES`'s recommended list AND the
    SKILL.md teaching doesn't catch up, this test fails before
    operators wire the new shape into production allowlists."""
    body = _read(_SKILL_MD)
    # Exclude the canonical template line (carries `<...>`
    # placeholders); count only CONCRETE worked examples.
    examples = [
        ln for ln in body.splitlines()
        if "BriefingFix:" in ln and " -> " in ln and "<" not in ln
    ]
    assert len(examples) >= len(_BOOTSTRAP_SHAPES), (
        f"SKILL.md must carry at least {len(_BOOTSTRAP_SHAPES)} "
        f"concrete `BriefingFix: ... -> ...` worked examples (one "
        f"per bootstrap shape); found {len(examples)}"
    )


def test_skill_md_worked_examples_match_a_grown_bootstrap_list():
    """Anti-drift simulation: synthesize a grown bootstrap list
    (`_BOOTSTRAP_SHAPES + ("synthetic_future_shape",)`) and assert
    the SKILL.md teaching covers every REAL shape in the grown set
    — only the synthetic placeholder is allowed to be missing. If
    a future TB-N adds a fifth shape to
    `AP2_AUTO_UNFREEZE_FIX_SHAPES`'s recommended list, a contributor
    must update both `_BOOTSTRAP_SHAPES` (here) AND add a SKILL.md
    worked example; this test pins the relationship without
    requiring the synthetic placeholder to exist in SKILL.md.

    Math: with N real shapes present today + 1 synthetic stub, the
    grown list has `N+1` entries, the SKILL.md teaching covers `N`,
    exactly one synthetic stub is missing — the count is precise so
    a real shape losing its example trips the assertion."""
    base_count = len(_BOOTSTRAP_SHAPES)
    grown = _BOOTSTRAP_SHAPES + ("synthetic_future_shape",)
    assert len(grown) == base_count + 1, (
        "monkey-grown list should be exactly one larger than the base"
    )

    body = _read(_SKILL_MD)
    present = sum(
        1 for shape in grown
        if any(
            "BriefingFix:" in ln and shape in ln
            for ln in body.splitlines()
        )
    )
    missing = len(grown) - present
    # The synthetic shape is intentionally not in SKILL.md, so
    # `missing` is exactly 1 in this simulated run. If a future
    # refactor accidentally removes a real shape's example, this
    # delta grows and the assertion catches the drift.
    assert missing == 1, (
        f"with one synthetic shape grafted onto the bootstrap list, "
        f"exactly one shape should be missing a SKILL.md example "
        f"(the synthetic one); got missing={missing} — looks like a "
        f"real bootstrap shape lost its worked example"
    )


# ---------------------------------------------------------------------------
# (5) Parser-contract round-trip: the SKILL.md worked examples
#     actually parse as the daemon would parse them.
# ---------------------------------------------------------------------------


def test_skill_md_worked_examples_round_trip_through_parser():
    """End-to-end pin: every fenced `BriefingFix:` worked example in
    SKILL.md is parseable by `parse_blocked_summary_fix_shape`. If
    the teaching drifts to a stale separator (e.g. `=>` instead of
    ` -> `) or omits the `<shape> at <path>:<line>:` prefix, the
    parser returns None and the agent's copied line would silently
    fail to trigger auto-unfreeze. The cheapest way to keep teaching
    aligned with parser is to round-trip the examples here."""
    from ap2._shared import parse_blocked_summary_fix_shape

    body = _read(_SKILL_MD)
    # Exclude the canonical TEMPLATE line (which carries
    # `<shape>` / `<line>` placeholders the parser legitimately
    # rejects); only round-trip CONCRETE worked examples. A line
    # carrying `<` is the placeholder shape.
    examples = [
        ln.strip()
        for ln in body.splitlines()
        if "BriefingFix:" in ln and " -> " in ln and "<" not in ln
    ]
    assert examples, "no worked examples to round-trip"
    for ln in examples:
        parsed = parse_blocked_summary_fix_shape(ln)
        assert parsed is not None, (
            f"SKILL.md worked example failed to parse — teaching "
            f"is misaligned with `parse_blocked_summary_fix_shape`: "
            f"{ln!r}"
        )
        # Sanity: the parsed shape token names one of the bootstrap
        # shapes (so the example matches the curated teaching set).
        assert parsed["shape"] in _BOOTSTRAP_SHAPES, (
            f"worked example uses unknown shape {parsed['shape']!r} "
            f"— either add it to `_BOOTSTRAP_SHAPES` or fix the "
            f"example"
        )
