"""TB-173: `parse_open_questions` — pin the `## Open questions for operator`
section reader that powers the CLI / web / cron-status-report surfaces.

The ideation prompt's Step 0 schema mandates an `## Open questions for
operator` section in `.cc-autopilot/ideation_state.md` whenever a focus
item is `exhausted-needs-operator`, when goal.md appears to need
updating, or when the ideator notices a gap outside any current focus
item. `parse_open_questions` is the single source of truth that
`ap2 status` (CLI), the web home page, and the cron status-report all
call so the three operator-facing surfaces stay in sync.

These tests pin:
  - missing-file / missing-section → empty list (no false positives).
  - 1 / 3 bullet shapes survive the slicer + bullet pass.
  - multi-line bullets collapse to one entry (continuation join).
  - >7 bullets get truncated with a synthetic "(+M more)" trailer.
  - prose-only fallback splits paragraphs by blank lines.
"""
from __future__ import annotations

from pathlib import Path

from ap2.ideation import parse_focus_statuses, parse_open_questions


def _write_ideation_state(tmp_path: Path, body: str) -> Path:
    """Write `body` to `<tmp_path>/.cc-autopilot/ideation_state.md` and
    return its absolute path. Mirrors the on-disk layout the helper reads
    in production."""
    autopilot_dir = tmp_path / ".cc-autopilot"
    autopilot_dir.mkdir(parents=True, exist_ok=True)
    path = autopilot_dir / "ideation_state.md"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Missing file / section / empty section all collapse to []. The CLI / web /
# status-report surfaces all key off this contract — a non-empty list is
# the trigger for rendering the bullet line / card.


def test_parse_open_questions_handles_missing_file_returns_empty_list(
    tmp_path: Path,
):
    """File doesn't exist on disk yet (fresh project, no ideation cycle has
    run) — must return []. No exception, no false-positive bullet lines
    on `ap2 status` for a clean project."""
    path = tmp_path / ".cc-autopilot" / "ideation_state.md"
    assert not path.exists()
    assert parse_open_questions(path) == []


def test_parse_open_questions_handles_missing_section_returns_empty_list(
    tmp_path: Path,
):
    """File exists but has no `## Open questions for operator` heading.
    Defends against ideator-prompt regressions that drop the section
    header — must not falsely scrape some other heading's bullets."""
    body = (
        "# Ideation State\n\n"
        "## Mission alignment\n\n- some bullet\n\n"
        "## Current focus assessment\n\n- another bullet\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_open_questions(path) == []


def test_parse_open_questions_handles_empty_section_returns_empty_list(
    tmp_path: Path,
):
    """The header is present but the body has no bullets and no prose.
    Common shape after an ideation cycle that surfaced no questions — the
    ideator left the section header but wrote nothing under it. Must be
    treated the same as a missing section: no rendering."""
    body = (
        "# Ideation State\n\n"
        "## Open questions for operator\n\n"
        "## Proposals this cycle\n\n- TB-1\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_open_questions(path) == []


# ---------------------------------------------------------------------------
# Bullet-pass: 1, 3, multi-line continuation. The 7-cap test below
# exercises the truncation path on top of the same bullet pass.


def test_parse_open_questions_returns_single_bullet(tmp_path: Path):
    """Single-bullet section — sanity check that the slicer + bullet
    regex produce one entry. Anchors against future regex regressions
    that might require `>= 2` bullets to match."""
    body = (
        "## Open questions for operator\n\n"
        "- Should we update goal.md to declare verifier robustness as the next focus?\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_open_questions(path) == [
        "Should we update goal.md to declare verifier robustness as the next focus?"
    ]


def test_parse_open_questions_returns_bullets(tmp_path: Path):
    """3-bullet section — pins the canonical shape the ideator emits each
    cycle. Each bullet survives as one entry; order is preserved."""
    body = (
        "# Ideation State\n\n"
        "## Open questions for operator\n\n"
        "- After TB-171/TB-172/TB-173 land, approve or reject via CLI.\n"
        "- Pending operator op TB-170 adds `--skip-goal-alignment`.\n"
        "- Insights index still empty — not blocking.\n\n"
        "## Proposals this cycle\n\n- TB-171\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_open_questions(path)
    assert result == [
        "After TB-171/TB-172/TB-173 land, approve or reject via CLI.",
        "Pending operator op TB-170 adds `--skip-goal-alignment`.",
        "Insights index still empty — not blocking.",
    ]


def test_parse_open_questions_collapses_multiline_bullets(tmp_path: Path):
    """A bullet whose body wraps onto subsequent indented continuation
    lines collapses to a single entry with single-space joins. Matches
    how the ideator actually writes long questions today (see the
    in-flight `.cc-autopilot/ideation_state.md` for examples)."""
    body = (
        "## Open questions for operator\n\n"
        "- After this cycle lands TB-171 / TB-172 / TB-173 to Backlog they will\n"
        "  all sit `@blocked:review`. Approve via `ap2 approve TB-N` or reject\n"
        "  via `ap2 reject TB-N --reason ...`.\n"
        "- Insights index still empty.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_open_questions(path)
    assert len(result) == 2
    # First bullet should have all three source lines collapsed into one
    # entry with single-space separators (no embedded \n).
    assert result[0] == (
        "After this cycle lands TB-171 / TB-172 / TB-173 to Backlog they will "
        "all sit `@blocked:review`. Approve via `ap2 approve TB-N` or reject "
        "via `ap2 reject TB-N --reason ...`."
    )
    assert "\n" not in result[0]
    assert result[1] == "Insights index still empty."


def test_parse_open_questions_caps_at_seven(tmp_path: Path):
    """>7 bullets get truncated with a trailing "(+M more)" entry so the
    rendering surfaces don't have to defend against unbounded sections.
    The cap protects the CLI status block (which truncates further to 5)
    and the web card (which renders all entries) from runaway sections.
    """
    bullets = "\n".join(
        f"- question number {i} from the ideator's cycle"
        for i in range(1, 11)  # 10 bullets
    )
    body = f"## Open questions for operator\n\n{bullets}\n"
    path = _write_ideation_state(tmp_path, body)
    result = parse_open_questions(path)
    # Cap: 7 real entries + 1 synthetic "(+M more)" trailer = 8 total.
    assert len(result) == 8
    # First 7 are the original bullets in source order.
    for i in range(7):
        assert result[i] == f"question number {i + 1} from the ideator's cycle"
    # 8th entry is the truncation marker, citing the residual count.
    assert result[7] == "(+3 more)"


def test_parse_open_questions_falls_back_to_paragraphs_when_no_bullets(
    tmp_path: Path,
):
    """Ideator may write the section as prose paragraphs instead of
    bullets (regression hazard: prompt says "bullets" but a future
    rewrite could relax it). The helper falls back to splitting the body
    on blank lines, treating each paragraph as one entry. Keeps the
    operator-visible signal flowing even when the schema slips."""
    body = (
        "## Open questions for operator\n\n"
        "First paragraph spans one\nline of prose.\n\n"
        "Second paragraph is its own entry.\n\n"
        "Third and final paragraph.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_open_questions(path)
    assert result == [
        "First paragraph spans one line of prose.",
        "Second paragraph is its own entry.",
        "Third and final paragraph.",
    ]


def test_parse_open_questions_section_at_end_of_file(tmp_path: Path):
    """`## Open questions for operator` is the LAST section in the file —
    the slicer must read to EOF rather than requiring a trailing `## `
    heading. Pins behavior for the common shape where the ideator ends
    the file with this section."""
    body = (
        "# Ideation State\n\n"
        "## Mission alignment\n\n- nothing to add\n\n"
        "## Open questions for operator\n\n"
        "- A trailing-section question.\n"
        "- Another one.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_open_questions(path)
    assert result == [
        "A trailing-section question.",
        "Another one.",
    ]


def test_parse_open_questions_accepts_star_bullets(tmp_path: Path):
    """Both `- ` and `* ` bullet markers are valid markdown — the helper
    accepts either shape so an ideator who uses `*` doesn't silently lose
    the surfacing path."""
    body = (
        "## Open questions for operator\n\n"
        "* First star-marked entry.\n"
        "* Second.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_open_questions(path) == [
        "First star-marked entry.",
        "Second.",
    ]


# ---------------------------------------------------------------------------
# TB-174: `parse_focus_statuses` — pin the `## Current focus assessment`
# parser that powers the `_maybe_ideate` focus-exhausted gate.
#
# The ideation prompt's Step 0 schema (`ap2/ideation.default.md` lines
# 60-66) mandates one top-level `- **<title>**` bullet per goal.md
# `## Current focus` entry, with a nested `- Status: <value>` sub-bullet
# whose value is one of `in-progress` / `exhausted-needs-operator` /
# `deferred`. `parse_focus_statuses` returns `{title: status}` so the
# daemon can short-circuit the natural ideation cron when every focus
# item self-reports `exhausted-needs-operator`. The forced operator
# path (`force_ideate`, TB-159) bypasses the gate.
#
# These tests pin:
#   - missing-file / missing-section / empty-section → {}.
#   - single in-progress, single exhausted-needs-operator, multi-mixed.
#   - multi-line title (the production shape — title wraps onto a
#     continuation line before the closing `**`).
#   - malformed status values map to "unknown" (never to a valid status).


def test_parse_focus_statuses_handles_missing_file_returns_empty_dict(
    tmp_path: Path,
):
    """File doesn't exist on disk yet (fresh project, no ideation cycle has
    run) — must return {}. The gate then reads `focus_statuses and
    all(...)`, which is False on `{}`, so the natural path stays
    unaffected."""
    path = tmp_path / ".cc-autopilot" / "ideation_state.md"
    assert not path.exists()
    assert parse_focus_statuses(path) == {}


def test_parse_focus_statuses_returns_empty_when_section_missing(
    tmp_path: Path,
):
    """File exists but has no `## Current focus assessment` heading —
    must return {} so the gate stays a no-op. Defends against ideator-
    prompt regressions that might drop the section header."""
    body = (
        "# Ideation State\n\n"
        "## Mission alignment\n\n- something\n\n"
        "## Open questions for operator\n\n- a question\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_focus_statuses(path) == {}


def test_parse_focus_statuses_handles_empty_section_returns_empty_dict(
    tmp_path: Path,
):
    """Header is present but the section body has no `- **<title>**`
    bullets. Must return {} — the gate stays a no-op. The pre-prose
    introduction in the production file (`goal.md "Current focus: X"
    remains the sole declared focus.`) lands here as a non-bullet line
    that the parser ignores."""
    body = (
        "## Current focus assessment\n\n"
        "goal.md says nothing actionable yet — placeholder section.\n\n"
        "## Open questions for operator\n\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_focus_statuses(path) == {}


def test_parse_focus_statuses_returns_status_per_focus_item(tmp_path: Path):
    """Multi-item canonical shape — three focus items, mixed statuses.
    Pins the parser's primary return shape: dict ordering preserved
    (Python 3.7+ guarantees insertion order), titles whitespace-clean,
    statuses lowercased and stripped of backticks."""
    body = (
        "# Ideation State\n\n"
        "## Mission alignment\n\nSome paragraph.\n\n"
        "## Current focus assessment\n\n"
        "- **First focus item**\n"
        "  - Progress so far: TB-50 shipped.\n"
        "  - Gaps: none material.\n"
        "  - Status: `in-progress`\n"
        "  - Reasoning: still landing follow-ups.\n"
        "- **Second focus item**\n"
        "  - Progress so far: TB-60 shipped.\n"
        "  - Gaps: none.\n"
        "  - Status: `exhausted-needs-operator`\n"
        "  - Reasoning: every reasonable next step has shipped.\n"
        "- **Third focus item**\n"
        "  - Progress so far: TB-70 partial.\n"
        "  - Gaps: blocked on operator.\n"
        "  - Status: `deferred`\n"
        "  - Reasoning: parked.\n\n"
        "## Open questions for operator\n\n- something\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_focus_statuses(path)
    assert result == {
        "First focus item": "in-progress",
        "Second focus item": "exhausted-needs-operator",
        "Third focus item": "deferred",
    }


def test_parse_focus_statuses_single_in_progress(tmp_path: Path):
    """Single-item / `in-progress` — sanity check the most common shape
    (the load-bearing default for a project with one active focus)."""
    body = (
        "## Current focus assessment\n\n"
        "- **Ideation quality**\n"
        "  - Progress so far: TB-100 shipped.\n"
        "  - Gaps: none.\n"
        "  - Status: `in-progress`\n"
        "  - Reasoning: still iterating.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_focus_statuses(path) == {"Ideation quality": "in-progress"}


def test_parse_focus_statuses_single_exhausted(tmp_path: Path):
    """Single-item / `exhausted-needs-operator` — the gate-tripping
    shape. The dict will carry exactly one entry whose value is the
    canonical `exhausted-needs-operator` literal."""
    body = (
        "## Current focus assessment\n\n"
        "- **Walk-away resilience**\n"
        "  - Progress so far: TB-200 shipped.\n"
        "  - Gaps: none material.\n"
        "  - Status: `exhausted-needs-operator`\n"
        "  - Reasoning: every reasonable next step has shipped.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_focus_statuses(path) == {
        "Walk-away resilience": "exhausted-needs-operator",
    }


def test_parse_focus_statuses_handles_multiline_title(tmp_path: Path):
    """The production format wraps long titles across one continuation
    line before the closing `**` (real example from the in-flight
    `.cc-autopilot/ideation_state.md`):

        - **Ideation quality (gap-covering without drift; push for progress
          without scope creep)**
          - Progress so far: ...

    The parser must collapse the wrapped title into a single
    whitespace-normalized string so the gate's all-exhausted check
    reads a stable key."""
    body = (
        "## Current focus assessment\n\n"
        "- **Ideation quality (gap-covering without drift; push for progress\n"
        "  without scope creep)**\n"
        "  - Progress so far: TB-100 shipped.\n"
        "  - Gaps: none.\n"
        "  - Status: `in-progress`\n"
        "  - Reasoning: iterating.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_focus_statuses(path)
    assert result == {
        "Ideation quality (gap-covering without drift; push for progress "
        "without scope creep)": "in-progress",
    }


def test_parse_focus_statuses_malformed_status_returns_unknown(tmp_path: Path):
    """A status value outside the canonical {`in-progress`,
    `exhausted-needs-operator`, `deferred`} set returns `unknown` for
    that item — never silently mapping to a valid status. The gate
    only short-circuits on `exhausted-needs-operator`, so `unknown`
    keeps the natural ideation path running (fail-open on parse
    glitches)."""
    body = (
        "## Current focus assessment\n\n"
        "- **First focus**\n"
        "  - Status: `bogus-value`\n"
        "- **Second focus**\n"
        "  - Status: in-progress\n"
        "- **Third focus**\n"
        "  - (no status sub-bullet at all)\n"
    )
    path = _write_ideation_state(tmp_path, body)
    result = parse_focus_statuses(path)
    assert result == {
        "First focus": "unknown",
        "Second focus": "in-progress",
        "Third focus": "unknown",
    }


def test_parse_focus_statuses_section_at_end_of_file(tmp_path: Path):
    """`## Current focus assessment` is the LAST section in the file —
    the slicer must read to EOF rather than requiring a trailing `## `
    heading. Pins behavior for layouts that omit later sections (e.g.
    a fresh ideation_state.md with no proposals yet)."""
    body = (
        "# Ideation State\n\n"
        "## Mission alignment\n\nIntro paragraph.\n\n"
        "## Current focus assessment\n\n"
        "- **Trailing focus**\n"
        "  - Status: `exhausted-needs-operator`\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_focus_statuses(path) == {
        "Trailing focus": "exhausted-needs-operator",
    }


def test_parse_focus_statuses_ignores_non_top_level_bold_spans(tmp_path: Path):
    """`**...**` spans that aren't top-level bullets (e.g. inside a
    `Reasoning:` sub-bullet's prose, or in the section's introductory
    paragraph) must not get scraped as focus items. The parser keys
    only on lines whose first non-whitespace token is `- **`."""
    body = (
        "## Current focus assessment\n\n"
        "Intro paragraph mentioning **goal.md** in passing.\n\n"
        "- **Real focus**\n"
        "  - Status: `in-progress`\n"
        "  - Reasoning: pinning **non-goal** drift watch.\n"
    )
    path = _write_ideation_state(tmp_path, body)
    assert parse_focus_statuses(path) == {"Real focus": "in-progress"}
