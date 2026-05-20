"""Tests for `ap2.briefing_validators` — relocated from `ap2/tests/test_tools.py`
as part of TB-268 (test-file mirror of the TB-262 source split).

Covers: `_validate_briefing_structure` (TB-154 canonical-sections gate,
TB-161 goal-anchor cite, TB-164 Why-now rationale, TB-170 skip-goal-alignment
escape hatch, TB-171 Manual-bullet refusal), `_validate_single_line`'s
asterisk-in-title gate (TB-216), and helper plumb (`_goal_md_anchors`,
`_why_now_paragraph`). Tests are pure mechanical relocations from
`test_tools.py` — identical bodies, no logic edits — per the TB-268
briefing's "relocation only" rule. The validator's MCP-tool docstring
pins (TB-154 / TB-164) also live here because they test the validator
contract surface, not MCP dispatch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2.board import Board
from ap2.config import Config
from ap2 import tools
from ap2.tests._briefing_fixtures import (
    briefing_missing,
    briefing_with_manual_bullet,
    canonical_briefing,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "- [ ] **TB-5** **Existing** `#x` — An old task.\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    return Config.load(tmp_path)


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


# ---------------------------------------------------------------------------
# TB-216: `_validate_single_line` rejects titles containing `*`.
#
# TASK_LINE_RE's bold-fence title group `\*\*(?P<title>[^*]+)\*\*` collapses
# on any embedded asterisk → the rendered task line lands in
# `Board.malformed_lines` and `Board.find(task_id)` returns None. Operator-
# queue verbs (`approve` / `update` / `delete`) then all KeyError. Hit live
# on TB-214 (`Pin 4 sandbox install-* CLI verbs`). Gate is field-specific
# so description / tag / blocked values keep round-tripping with `*`.


def test_validate_single_line_rejects_asterisk_in_title():
    """Helper-level unit test: title with `*` returns a non-None error
    that mentions `*`; clean title returns None; description with `*`
    returns None (field-specific gate)."""
    err = tools._validate_single_line("title", "foo*bar")
    assert err is not None
    assert "*" in err
    assert tools._validate_single_line("title", "foo bar") is None
    # Field-specific: description / tag / blocked may contain `*`.
    assert tools._validate_single_line("description", "foo*bar") is None
    assert tools._validate_single_line("tag", "#foo*bar") is None
    assert tools._validate_single_line("blocked", "TB-5*review") is None


def test_validate_single_line_title_asterisk_uses_named_constant():
    """The new error returns `TITLE_NO_ASTERISK_ERR` verbatim so the
    constant is the single source of truth for the message text."""
    assert (
        tools._validate_single_line("title", "install-*")
        == tools.TITLE_NO_ASTERISK_ERR
    )


# ---------------------------------------------------------------------------
# TB-154: structural validation of briefings at the queue-append /
# board_edit boundary. The TB-153 chat thread surfaced an MM-handler-
# authored briefing whose `## Acceptance` heading (instead of `##
# Verification`) silently produced a `parse_verification_section` ==
# None, which the per-task verifier treated as "skip" — completing
# tasks with zero scope-specific verification. These tests pin the
# four reject paths plus the canonical happy path.

_TB154_CANONICAL_BRIEFING = canonical_briefing(
    "TB-154", title="anchor briefing",
)


def test_tb154_validate_briefing_structure_accepts_canonical(cfg):
    """A briefing with all five canonical `##` sections + at least one
    Verification bullet is accepted at the queue-append boundary; the
    queue gains exactly one record and the briefing materializes on
    disk."""
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "canonical brief",
            "briefing": _TB154_CANONICAL_BRIEFING,
        },
    )
    body = _unwrap(res)
    assert body["task_id"].startswith("TB-")
    queue_path = tools.operator_queue_path(cfg)
    lines = [
        ln for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1


def test_tb154_validate_briefing_structure_rejects_missing_verification(
    cfg, tmp_path,
):
    """No `## Verification` heading → reject with a structural error
    that names the missing section. Queue file unchanged; CLAUDE.md
    `Next task ID` unchanged (no leaked TB-N)."""
    before_claude = (tmp_path / "CLAUDE.md").read_text()
    queue_path = tmp_path / ".cc-autopilot" / "operator_queue.jsonl"
    before_queue = queue_path.read_text() if queue_path.exists() else ""

    body = briefing_missing(
        "TB-154", title="no-verification", drop="Verification",
    )
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "no verification", "briefing": body},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "briefing structure invalid" in text
    assert "## Verification" in text
    # No leaked TB-N: CLAUDE.md and queue both byte-identical.
    assert (tmp_path / "CLAUDE.md").read_text() == before_claude
    after_queue = queue_path.read_text() if queue_path.exists() else ""
    assert after_queue == before_queue


def test_tb154_validate_briefing_structure_rejects_acceptance_for_verification(
    cfg, tmp_path,
):
    """TB-153 exact failure mode: briefing has `## Acceptance` instead
    of `## Verification`. The structural pass catches this — the
    silent `parse_verification_section -> None` skip path is the bug
    we're closing."""
    before_claude = (tmp_path / "CLAUDE.md").read_text()
    # Canonical scaffold with `## Verification` renamed to `## Acceptance`
    # — the TB-153 failure mode we're closing.
    body = briefing_missing(
        "TB-154", title="acceptance-renamed", drop="Verification",
    ).replace(
        "## Out of scope",
        "## Acceptance\n\n- `uv run pytest -q` — gates pass\n\n## Out of scope",
    )
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "tb-153 shape", "briefing": body},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "briefing structure invalid" in text
    assert "## Verification" in text
    # No TB-N leaked.
    assert (tmp_path / "CLAUDE.md").read_text() == before_claude


def test_tb154_validate_briefing_structure_rejects_empty_verification(cfg):
    """Briefing has the `## Verification` heading but zero bullets — the
    per-task verifier would have nothing to score against the agent's
    diff. Reject at the queue-append boundary instead of letting the
    verifier silently skip."""
    body = canonical_briefing(
        "TB-154", title="empty-verification", verification="",
    )
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "empty verif", "briefing": body},
    )
    assert res.get("isError"), res
    assert "empty" in res["content"][0]["text"].lower()


def test_tb154_validate_briefing_structure_rejects_missing_goal(cfg):
    """Same gate covers any missing canonical section, not just
    `## Verification`. Drop `## Goal` and the validator names it in the
    error so the operator knows what to fix."""
    body = briefing_missing("TB-154", title="no-goal", drop="Goal")
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "no goal", "briefing": body},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "briefing structure invalid" in text
    assert "## Goal" in text


def test_tb154_validate_briefing_structure_extra_sections_allowed(cfg):
    """Extra `##`-level sections (e.g. `## Decision log`, `## Why`) are
    allowed — the validator checks for omission/rename of the canonical
    set, not for an exact match. Pin so future tightening doesn't
    accidentally start rejecting authoring extensions."""
    # Canonical scaffold plus an extra `## Decision log` section spliced
    # between Design and Verification — the validator must still accept.
    body = canonical_briefing(
        "TB-154", title="canonical + extras",
    ).replace(
        "## Verification",
        "## Decision log\n\n- decided X\n\n## Verification",
    )
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "with extras", "briefing": body},
    )
    body_out = _unwrap(res)
    assert body_out["task_id"].startswith("TB-")


def test_tb154_validate_briefing_structure_fires_for_do_board_edit(cfg, tmp_path):
    """The same gate runs on `do_board_edit`'s add_* paths (used by
    ideation / control agents). TB-153's failure mode would also have
    been catchable via `board_edit` if the toolset hadn't been
    restricted; the gate-at-both-surfaces invariant is what prevents
    a future toolset relaxation from silently re-opening the hole."""
    before_claude = (tmp_path / "CLAUDE.md").read_text()
    body = briefing_missing(
        "TB-154", title="no-verification via board_edit",
        drop="Verification",
    )
    res = tools.do_board_edit(
        cfg,
        {"action": "add_backlog", "title": "via board edit", "briefing": body},
    )
    assert res.get("isError"), res
    assert "briefing structure invalid" in res["content"][0]["text"]
    # CLAUDE.md untouched — no TB-N leaked.
    assert (tmp_path / "CLAUDE.md").read_text() == before_claude


def test_tb154_validate_briefing_structure_unit_function():
    """Direct call to `_validate_briefing_structure` — pure function,
    no Config / IO. None for canonical input; non-None message for each
    reject path."""
    assert tools._validate_briefing_structure(_TB154_CANONICAL_BRIEFING) is None

    missing_verif = (
        "# x\n\n## Goal\n\nx\n\n## Scope\n\n- a\n\n"
        "## Design\n\ny\n\n## Out of scope\n\n- z\n"
    )
    err = tools._validate_briefing_structure(missing_verif)
    assert err is not None and "## Verification" in err

    empty_briefing = ""
    # Empty payload defers to TB-135's "briefing is required" gate
    # (the dedicated error there names the right fix); the structural
    # validator returns None to avoid double-reporting.
    assert tools._validate_briefing_structure(empty_briefing) is None


def test_tb154_validate_briefing_structure_fires_for_update_op(cfg, tmp_path):
    """TB-153's `update` op also routes a `briefing` payload through
    the queue, slug-stable overwriting the existing briefing file. Without
    the structural gate on this branch, an operator (or MM-handler) could
    replace a canonical briefing with a `## Acceptance`-shaped one and
    re-introduce TB-153's exact failure mode — the per-task verifier
    silently skipping. Pin: the update path rejects the same shapes as
    the add_* path.
    """
    # cfg's TB-5 lives in Backlog (idle section, fence doesn't fire).
    # Briefing is None on the seeded task — the legacy / pre-TB-135
    # branch exercises the same validator before allocating a slug.
    bad_acceptance = briefing_missing(
        "TB-153", title="reprised", drop="Verification",
    ).replace(
        "## Out of scope",
        "## Acceptance\n\n- `pytest -q`\n\n## Out of scope",
    )
    pre_tasks_dir = sorted(
        p.name for p in (cfg.tasks_dir).glob("*.md")
    ) if cfg.tasks_dir.exists() else []
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-5", "briefing": bad_acceptance},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "briefing structure invalid" in text
    assert "## Verification" in text
    # No briefing file leaked to disk: tasks_dir contents unchanged.
    post_tasks_dir = sorted(
        p.name for p in (cfg.tasks_dir).glob("*.md")
    ) if cfg.tasks_dir.exists() else []
    assert post_tasks_dir == pre_tasks_dir
    # No queue line written either.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""

    # The same gate covers a missing-Verification briefing on `update`.
    missing_verif = briefing_missing(
        "TB-154", title="missing-verif via update", drop="Verification",
    )
    res2 = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-5", "briefing": missing_verif},
    )
    assert res2.get("isError"), res2
    assert "briefing structure invalid" in res2["content"][0]["text"]
    assert "## Verification" in res2["content"][0]["text"]

    # Empty-Verification (heading present, zero bullets) is rejected too.
    empty_verif = canonical_briefing(
        "TB-154", title="empty-verif via update", verification="",
    )
    res3 = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-5", "briefing": empty_verif},
    )
    assert res3.get("isError"), res3
    assert "empty" in res3["content"][0]["text"].lower()

    # Sanity check: a canonical briefing on the same `update` call is
    # accepted and queued — pins that we didn't accidentally turn the
    # update op into "always rejects briefing".
    res_ok = tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": "TB-5",
            "briefing": _TB154_CANONICAL_BRIEFING,
        },
    )
    body_ok = _unwrap(res_ok)
    assert body_ok["op"] == "update"


# ---------------------------------------------------------------------------
# TB-161: goal-anchor extension to the structural validator. The hard gate
# rejects briefings whose `## Goal` body cites no token from a derived
# `goal_anchors` set (Current focus heading title or Done-when bullet).
# Closes the "gap-covering without drift" failure mode (goal.md lines
# 50-59) — proposals whose Goal is pure ap2-meta-polish, unconnected to
# any operator-stated focus item, get refused before TB-N is allocated.

_TB161_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\nOne-sentence statement of project purpose.\n\n"
    "## Done when\n"
    "- Operators can run the full pipeline without intervention.\n"
    "- Verification gates fire on every committed change.\n\n"
    "## Current focus: ideation quality\n"
    "Folding goal-relevance into proposals before TB-N allocation.\n"
)


def _write_goal_md(tmp_path: Path, body: str = _TB161_GOAL_MD) -> Path:
    p = tmp_path / "goal.md"
    p.write_text(body)
    return p


def test_validate_briefing_rejects_goal_section_without_anchor(tmp_path):
    """Briefing's `## Goal` body cites no anchor from goal.md → reject
    with an error that names the goal.md anchor source so the operator
    knows what to fix. Closes goal.md's "gap-covering without drift"
    failure mode at queue-append time."""
    goal_md = _write_goal_md(tmp_path)
    body = (
        "# off-anchor\n\n"
        "## Goal\n\n"
        "Polish ap2's internal logging shape — make daemon.log prettier.\n\n"
        "## Scope\n\n- daemon.py\n\n"
        "## Design\n\nRework the log format.\n\n"
        "## Verification\n\n- `uv run pytest -q` — gates pass\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(body, goal_md_path=goal_md)
    assert err is not None, "expected non-None error string"
    # Error message names goal.md and the anchor concept so the
    # operator can find the fix without re-reading the validator source.
    assert "goal.md" in err.lower() or "anchor" in err.lower()


def test_validate_briefing_accepts_goal_section_with_done_when_quote(tmp_path):
    """Briefing's `## Goal` body quotes the leading words of a
    `## Done when` bullet → accepted. The validator returns None — the
    proposal has demonstrated goal-relevance via direct citation."""
    goal_md = _write_goal_md(tmp_path)
    body = (
        "# done-when-quote\n\n"
        "## Goal\n\n"
        "Closes the failure mode where operators can run the full "
        "pipeline but verification silently skips. Reinforces the "
        "Done-when bullet about pipeline-without-intervention.\n\n"
        "Why now: the verifier-skip is silent, so without this gate "
        "operators only catch it after the fact (TB-164).\n\n"
        "## Scope\n\n- ap2/verify.py\n\n"
        "## Design\n\nGate on verifier invocation count per task.\n\n"
        "## Verification\n\n- `uv run pytest -q` — gates pass\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(body, goal_md_path=goal_md)
    assert err is None, f"expected None, got: {err!r}"


def test_validate_briefing_accepts_goal_section_with_current_focus_heading(tmp_path):
    """Citing the `## Current focus: ideation quality` heading verbatim
    is also a valid anchor — pin so the heading-title path stays
    accepted alongside the Done-when bullet path."""
    goal_md = _write_goal_md(tmp_path)
    body = (
        "# current-focus-quote\n\n"
        "## Goal\n\n"
        "Advances goal.md's Current focus: ideation quality — folds "
        "goal-anchor checking into the queue-append validator.\n\n"
        "Why now: closes the silent-bypass failure mode the briefing "
        "scope names (TB-164).\n\n"
        "## Scope\n\n- ap2/tools.py\n\n"
        "## Design\n\nExtend the validator.\n\n"
        "## Verification\n\n- `uv run pytest -q` — gates pass\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(body, goal_md_path=goal_md)
    assert err is None, f"expected None, got: {err!r}"


def test_validate_briefing_skips_anchor_check_when_goal_md_missing(tmp_path):
    """A nonexistent / unreadable goal.md → skip the anchor check
    entirely. The validator falls back to the TB-154 structural-only
    pass so a fresh project (or one without a real goal.md) doesn't
    get every proposal rejected."""
    missing = tmp_path / "no-goal-here.md"
    assert not missing.exists()
    body = briefing_missing(
        "TB-161",
        title="off-anchor",
        drop="goal-anchor",
        scope="- daemon.py\n",
        design="Rework logs.\n",
    )
    err = tools._validate_briefing_structure(body, goal_md_path=missing)
    assert err is None, f"expected None (skip), got: {err!r}"


def test_validate_briefing_skips_anchor_check_when_goal_md_all_placeholder(tmp_path):
    """A goal.md that's still the `init_project` template — bare
    `## Current focus` heading with no topic suffix and no `## Done
    when` section — contributes no anchors and the validator skips the
    goal-anchor check. Pins that day-one project state doesn't fire
    spurious rejections.
    """
    placeholder = tmp_path / "goal.md"
    placeholder.write_text(
        "# Project Goals\n\n"
        "## Mission\n(one-sentence statement of what this project is FOR)\n\n"
        "## Current focus\n- (area or theme actively in flight now)\n\n"
        "## Non-goals\n- (explicit non-goals)\n\n"
        "## Constraints\n- (hard constraints)\n"
    )
    body = briefing_missing(
        "TB-161",
        title="placeholder-friendly",
        drop="goal-anchor",
    )
    err = tools._validate_briefing_structure(
        body, goal_md_path=placeholder,
    )
    assert err is None, (
        f"all-placeholder goal.md should not trip the anchor check; got: {err!r}"
    )


def test_validate_briefing_anchor_check_unit_function_default_is_skip():
    """Direct call to `_validate_briefing_structure` with no goal_md_path
    keyword arg → backward-compat: skips the goal-anchor check. Pins
    that callers that haven't been updated to pass the path don't
    accidentally start rejecting briefings they used to accept."""
    body = briefing_missing(
        "TB-161",
        title="no-goal-md-arg",
        drop="goal-anchor",
    )
    assert tools._validate_briefing_structure(body) is None


def test_validate_briefing_anchor_check_fires_via_operator_queue_append(
    cfg, tmp_path,
):
    """End-to-end at the queue-append boundary: a goal-anchor-missing
    briefing routed through `do_operator_queue_append` is rejected and
    no queue line / briefing file is written. Mirrors the
    TB-154-style "no leak on reject" pin."""
    _write_goal_md(tmp_path)
    body = briefing_missing(
        "TB-161",
        title="off-anchor via queue",
        drop="goal-anchor",
        scope="- daemon.py\n",
        design="Rework logs.\n",
    )
    pre_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "off-anchor via queue", "briefing": body},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "briefing structure invalid" in text
    assert "goal.md" in text.lower() or "anchor" in text.lower()
    # No briefing file leaked to disk.
    post_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    assert post_tasks_dir == pre_tasks_dir
    # No queue line written.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_goal_md_anchors_extracts_done_when_bullets_and_focus_titles(tmp_path):
    """Direct unit test on `_goal_md_anchors` — pins the phrase shapes
    so a future tweak (e.g. word-count window) doesn't silently change
    which goal.md content survives normalization. The anchor set should
    include the Current focus heading title and Done-when bullet
    leading-words; bare `## Done when` heading title is dropped (it's a
    GOAL_ANCHOR_HEADINGS prefix on its own — too generic)."""
    goal_md = _write_goal_md(tmp_path)
    anchors = tools._goal_md_anchors(goal_md)
    assert anchors, "expected non-empty anchor set"
    # Focus-item heading title survives normalization.
    assert "current focus ideation quality" in anchors
    # At least one Done-when bullet's leading words survive.
    assert any(
        a.startswith("operators can run the full") for a in anchors
    ), anchors
    # Bare prefix words are dropped — they'd false-positive too easily.
    assert "current focus" not in anchors
    assert "done when" not in anchors


# ---------------------------------------------------------------------------
# TB-164: "Why now" rationale extension to the structural validator. The
# hard gate rejects briefings whose `## Goal` body lacks a line-anchored
# `Why now` marker OR whose marker is present but the rationale paragraph
# is shorter than `WHY_NOW_MIN_CHARS`. Closes goal.md's "push for progress
# without scope creep" failure mode (goal.md lines 61-70) at queue-append
# time — every proposal must articulate goal.md's delete-test ("if we
# delete this and the goal still ships, was it useful?") in writing.

from ap2.init import WHY_NOW_MIN_CHARS as _WHY_NOW_MIN_CHARS


def test_validate_briefing_rejects_goal_without_why_now():
    """A `## Goal` body that doesn't contain a `Why now` marker → reject
    with an error string that names `Why now` and TB-164 so the operator
    knows what to fix. The rule fires regardless of whether goal.md is
    supplied — the delete-test is intrinsic to the briefing contract,
    distinct from the TB-161 anchor-skip path."""
    body = briefing_missing("TB-164", title="no-why-now", drop="Why now")
    err = tools._validate_briefing_structure(body)
    assert err is not None, "expected non-None error string"
    assert "Why now" in err, err
    assert "TB-164" in err, err


def test_validate_briefing_rejects_why_now_below_min_chars():
    """`Why now: yes` is structurally present but the rationale (3 chars)
    is below `WHY_NOW_MIN_CHARS` — reject. Pins the floor: trivial passes
    don't satisfy the delete-test."""
    body = (
        "# trivial-why-now\n\n"
        "## Goal\n\nA goal.\n\nWhy now: yes\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nA thing.\n\n"
        "## Verification\n\n- `uv run pytest -q`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(body)
    assert err is not None
    assert "Why now" in err, err
    assert str(_WHY_NOW_MIN_CHARS) in err, err
    assert "TB-164" in err, err


def test_validate_briefing_accepts_goal_with_why_now_paragraph():
    """A `## Goal` body whose `Why now` paragraph is ≥`WHY_NOW_MIN_CHARS`
    chars passes the delete-test gate. Pin the accept-side so the rule
    doesn't quietly inflate the threshold without test coverage."""
    body = canonical_briefing("TB-164", title="good-why-now")
    err = tools._validate_briefing_structure(body)
    assert err is None, f"expected None, got: {err!r}"


def test_validate_briefing_why_now_check_is_line_anchored():
    """`why now` mid-prose (no leading line break, no trailing
    `:`/whitespace-only-after-marker delimiter) does NOT satisfy the
    gate. Pins the line-anchor: a Goal body that incidentally contains
    "the question of why now is hard…" inline should still be rejected."""
    body = (
        "# inline-why-now\n\n"
        "## Goal\n\n"
        "Some prose that mentions the question of why now is hard "
        "to answer in the abstract — but that's just narrative text, "
        "not a delete-test rationale paragraph.\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nA thing.\n\n"
        "## Verification\n\n- `uv run pytest -q`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(body)
    assert err is not None, (
        "mid-prose 'why now' must NOT satisfy the line-anchored marker"
    )
    assert "Why now" in err
    assert "TB-164" in err


def test_validate_briefing_why_now_marker_tolerates_template_parenthetical():
    """The `BRIEFING_TEMPLATE` ships with `Why now (delete-test):` —
    the validator must accept that exact shape. Strips the
    `(delete-test)` parenthetical before measuring the rationale
    length, so a real rationale isn't double-counted against itself.
    """
    body = (
        "# template-shape\n\n"
        "## Goal\n\nA real goal.\n\n"
        "Why now (delete-test): closes the failure mode the briefing "
        "scope names (TB-164).\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nA thing.\n\n"
        "## Verification\n\n- `uv run pytest -q`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(body)
    assert err is None, f"expected None, got: {err!r}"


def test_validate_briefing_why_now_check_fires_via_operator_queue_append(
    cfg, tmp_path,
):
    """End-to-end at the queue-append boundary: a why-now-missing
    briefing routed through `do_operator_queue_append` is rejected and
    no queue line / briefing file is written. Mirrors the
    TB-154/TB-161-style "no leak on reject" pin."""
    body = briefing_missing(
        "TB-164",
        title="off-rationale via queue",
        drop="Why now",
        scope="- daemon.py\n",
        design="Rework logs.\n",
    )
    pre_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "no rationale", "briefing": body},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "Why now" in text
    assert "TB-164" in text
    # No briefing file leaked to disk.
    post_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    assert post_tasks_dir == pre_tasks_dir
    # No queue line written.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_why_now_paragraph_helper_returns_none_when_no_marker():
    """Direct unit test on `_why_now_paragraph` — pins the contract that
    a missing marker returns `None` (distinct from "" so the validator
    can render distinct error messages for missing-marker vs
    too-short-rationale)."""
    assert tools._why_now_paragraph("Just a paragraph.\n") is None
    # Mid-prose mention also returns None (line-anchor).
    assert tools._why_now_paragraph(
        "Some prose mentioning why now is hard.\n"
    ) is None


def test_tb164_operator_queue_append_docstring_names_requirement():
    """Pinned phrasing — the MCP tool docstring spells out the TB-164
    requirement so the MM handler / control agent reads it before
    authoring a briefing payload."""
    import inspect
    src = inspect.getsource(tools.build_mcp_server)
    assert "TB-164" in src, "operator_queue_append docstring missing TB-164"
    # Either the marker name or the delete-test phrasing is enough.
    assert "Why now" in src or "delete-test" in src, src


def test_tb154_operator_queue_append_docstring_carries_canonical_template():
    """Pinned phrasing — the MCP tool docstring tells the agent the
    same thing as the validator's error message. Future edits that
    silently weaken the contract get caught here.

    The docstring is the description string passed to `@tool(...)` in
    `build_mcp_server`; we read it back via the SDK server's tool
    registry for a faithful round-trip pin."""
    from ap2.config import Config
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as td:
        # build_mcp_server needs a real Config; minimal scaffolding here.
        os.makedirs(os.path.join(td, ".cc-autopilot", "tasks"), exist_ok=True)
        with open(os.path.join(td, "TASKS.md"), "w") as f:
            f.write(
                "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
                "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
            )
        with open(os.path.join(td, "CLAUDE.md"), "w") as f:
            f.write("## Autopilot\n\n- Next task ID: TB-1\n")
        cfg2 = Config.load(td)
        cfg2.ensure_dirs()
        # Read the @tool docstring straight off the build_mcp_server
        # source — the description string is the second argument to
        # the @tool decorator. We grab it via the registered handler's
        # closure metadata.
        import inspect
        src = inspect.getsource(tools.build_mcp_server)
    # Every canonical section name appears verbatim in the docstring.
    for section in ("## Goal", "## Scope", "## Design",
                    "## Verification", "## Out of scope"):
        assert section in src, (
            f"operator_queue_append docstring missing {section!r}"
        )
    # And the rejection contract is named so the agent reads it.
    assert "TB-154" in src


# ---------------------------------------------------------------------------
# TB-170: `--skip-goal-alignment` operator-CLI escape hatch from the
# TB-161 goal-cite + TB-164 Why-now checks. The bypass is opt-in via
# the kwarg on `_validate_briefing_structure` and rides on the operator-
# queue payload as `skip_goal_alignment: true`. Every other validation
# (canonical sections, parseable + non-empty Verification, single-line
# title/tags/description) still fires.


_TB170_NO_ALIGNMENT_BRIEFING = (
    # Canonical-shape briefing that intentionally fails BOTH TB-161
    # (Goal body cites no goal.md anchor) and TB-164 (no Why-now
    # marker). With `skip_goal_alignment=False` the validator must
    # reject; with True it must accept (every other gate passes).
    "# tb-170 op-cli bypass\n\n"
    "## Goal\n\nFix a one-line typo in a comment.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


def test_validate_briefing_structure_signature_exposes_skip_goal_alignment():
    """`_validate_briefing_structure(skip_goal_alignment=...)` is the
    contract the CLI / queue-append callers wire onto. Pin via
    `inspect.signature` so a refactor that drops the kwarg gets caught
    immediately."""
    import inspect
    sig = inspect.signature(tools._validate_briefing_structure)
    assert "skip_goal_alignment" in sig.parameters
    p = sig.parameters["skip_goal_alignment"]
    # Default-False so every existing caller (ideation / MM handler /
    # migrate-to-ap2) keeps running every check unchanged.
    assert p.default is False


def test_validate_briefing_skip_goal_alignment_bypasses_anchor_and_why_now(
    tmp_path,
):
    """A briefing that's canonically-shaped but lacks BOTH the goal-
    anchor citation AND the Why-now marker:
      - rejected with `skip_goal_alignment=False` (default, the
        TB-161/164 gate fires).
      - accepted with `skip_goal_alignment=True` (the operator-CLI
        bypass).
    Pin: the bypass is the ONE behavior change; every other validation
    still fires (covered by the other tests below).
    """
    # Use a real goal.md so the TB-161 anchor check has anchors to miss.
    goal_md = tmp_path / "goal.md"
    goal_md.write_text(
        "# Project Goals\n\n"
        "## Mission\nOne-sentence statement of project purpose.\n\n"
        "## Done when\n"
        "- Operators can run the full pipeline without intervention.\n\n"
        "## Current focus: ideation quality\n\nstuff\n"
    )

    # Default behavior: rejected.
    err = tools._validate_briefing_structure(
        _TB170_NO_ALIGNMENT_BRIEFING, goal_md_path=goal_md,
    )
    assert err is not None, (
        "default validator must reject a no-anchor + no-why-now briefing"
    )

    # With the bypass: accepted.
    err_bypass = tools._validate_briefing_structure(
        _TB170_NO_ALIGNMENT_BRIEFING,
        goal_md_path=goal_md,
        skip_goal_alignment=True,
    )
    assert err_bypass is None, (
        f"skip_goal_alignment=True must accept a briefing that fails only "
        f"TB-161/164; got: {err_bypass!r}"
    )


def test_validate_briefing_skip_goal_alignment_still_fires_other_checks(
    tmp_path,
):
    """The bypass is scoped to TB-161/164 — every OTHER validator keeps
    firing even when the flag is True. Concretely: a missing
    `## Verification` section is still rejected, a parseable but empty
    Verification is still rejected, and a missing canonical section
    (e.g. `## Scope`) is still rejected. Pinning at least the missing-
    Verification case per the briefing's verification scope."""
    # 1. Missing `## Verification` — TB-154 canonical-sections gate.
    missing_verif = briefing_missing(
        "TB-170", title="missing-verif via skip", drop="Verification",
    )
    err = tools._validate_briefing_structure(
        missing_verif, skip_goal_alignment=True,
    )
    assert err is not None, (
        "skip_goal_alignment must NOT bypass the missing-Verification gate"
    )
    assert "## Verification" in err

    # 2. Empty Verification — TB-138 / TB-154 parseable-but-empty gate.
    empty_verif = canonical_briefing(
        "TB-170", title="empty-verif via skip", verification="",
    )
    err2 = tools._validate_briefing_structure(
        empty_verif, skip_goal_alignment=True,
    )
    assert err2 is not None
    assert "empty" in err2.lower()

    # 3. Missing `## Scope` — covers the broader canonical-sections gate.
    missing_scope = briefing_missing(
        "TB-170", title="missing-scope via skip", drop="Scope",
    )
    err3 = tools._validate_briefing_structure(
        missing_scope, skip_goal_alignment=True,
    )
    assert err3 is not None
    assert "## Scope" in err3


def test_validate_briefing_skip_goal_alignment_default_preserves_behavior():
    """Pin the default-False kwarg: every existing call site that omits
    the kwarg sees the same TB-161/164 enforcement as before TB-170.
    Concretely: a no-why-now briefing is rejected when the kwarg is
    omitted, identical to the pre-TB-170 contract."""
    body = briefing_missing(
        "TB-170", title="default-still-strict", drop="Why now",
    )
    err = tools._validate_briefing_structure(body)
    assert err is not None
    assert "Why now" in err and "TB-164" in err


def test_queue_append_skip_goal_alignment_accepts_and_persists_flag(
    cfg, tmp_path,
):
    """End-to-end at the queue-append boundary: a no-anchor + no-why-now
    briefing routed through `do_operator_queue_append` with
    `skip_goal_alignment=True` is ACCEPTED (the bypass works) and the
    queue record carries `skip_goal_alignment: true` so the drain-side
    audit line can decorate the operator_log.md entry."""
    # Use the cfg's goal.md path — the validator falls back to "skip
    # the anchor check" when goal.md is the placeholder template, so
    # we need to seed real anchors to actually exercise the bypass.
    goal_md = cfg.project_root / "goal.md"
    goal_md.write_text(
        "# Project Goals\n\n"
        "## Mission\nOne-sentence statement of project purpose.\n\n"
        "## Done when\n"
        "- Operators can run the full pipeline without intervention.\n\n"
        "## Current focus: ideation quality\n\nstuff\n"
    )
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "operator-meta typo fix",
            "briefing": _TB170_NO_ALIGNMENT_BRIEFING,
            "skip_goal_alignment": True,
        },
    )
    body = _unwrap(res)
    assert body["task_id"].startswith("TB-")
    # Queue record carries the flag so the drain-side audit line can
    # surface it.
    qpath = tools.operator_queue_path(cfg)
    lines = [
        json.loads(ln) for ln in qpath.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["op"] == "add_backlog"
    assert rec["args"].get("skip_goal_alignment") is True


def test_queue_append_without_skip_flag_rejects_no_alignment_briefing(
    cfg, tmp_path,
):
    """Pin the default contract: WITHOUT `skip_goal_alignment=True`, the
    same briefing routed through `do_operator_queue_append` is REJECTED
    by TB-161/164 — the queue stays empty, no briefing file leaks."""
    goal_md = cfg.project_root / "goal.md"
    goal_md.write_text(
        "# Project Goals\n\n"
        "## Mission\nstuff.\n\n"
        "## Done when\n"
        "- Operators can run the full pipeline without intervention.\n\n"
        "## Current focus: ideation quality\n\nstuff\n"
    )
    pre_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "would-be-meta but no flag",
            "briefing": _TB170_NO_ALIGNMENT_BRIEFING,
        },
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    # Either TB-161 (anchor) or TB-164 (why-now) must surface — both are
    # designed to fire on this briefing without the bypass.
    assert "TB-161" in text or "TB-164" in text or "Why now" in text
    # No briefing file leaked.
    post_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    assert post_tasks_dir == pre_tasks_dir
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_queue_append_skip_flag_does_not_bypass_other_validators(
    cfg, tmp_path,
):
    """Pin scope of the bypass at the queue-append boundary: a briefing
    missing `## Verification` is still rejected even with the flag
    set, and a multi-line title is still rejected. The flag only
    covers TB-161 + TB-164."""
    # Missing Verification — canonical-sections gate must still fire.
    missing_verif = briefing_missing(
        "TB-170", title="missing-verif via queue + skip", drop="Verification",
    )
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": "no verif",
            "briefing": missing_verif,
            "skip_goal_alignment": True,
        },
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "## Verification" in text


def test_skip_goal_alignment_only_applies_at_queue_append_when_flag_set(
    cfg, tmp_path,
):
    """Pin the bypass scope: ideation-style callers that DO NOT pass
    `skip_goal_alignment` always run the full goal-alignment gate
    regardless of payload. Concretely: `do_board_edit` (the ideation /
    control-agent surface) refuses a no-anchor + no-why-now briefing —
    no bypass for ideation. The goal.md anchor check is what fires
    here, mirroring the queue-append path with a real goal.md.
    """
    goal_md = cfg.project_root / "goal.md"
    goal_md.write_text(
        "# Project Goals\n\n"
        "## Mission\nstuff.\n\n"
        "## Done when\n"
        "- Operators can run the full pipeline without intervention.\n\n"
        "## Current focus: ideation quality\n\nstuff\n"
    )
    # Even if `skip_goal_alignment=True` is in the payload, `do_board_edit`
    # ignores it (the kwarg is operator-CLI-only by design — passed only
    # through the queue-append path). Validator runs the full gate.
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": "ideation-style with bogus flag",
            "briefing": _TB170_NO_ALIGNMENT_BRIEFING,
            "skip_goal_alignment": True,  # ignored on this surface
        },
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    # TB-161 (anchor) or TB-164 (why-now) surfaces — bypass NOT honored.
    assert "TB-161" in text or "TB-164" in text or "Why now" in text


def test_validate_briefing_skip_goal_alignment_unit_function_pure():
    """Direct-call pin without any cfg / fixture: pure function semantics.
    Useful for the prose verification bullet that pins the kwarg
    contract specifically."""
    body = _TB170_NO_ALIGNMENT_BRIEFING
    # Without the kwarg → reject (TB-164 fires regardless of goal.md).
    err = tools._validate_briefing_structure(body)
    assert err is not None
    # With the kwarg → accept.
    err2 = tools._validate_briefing_structure(body, skip_goal_alignment=True)
    assert err2 is None


# ---------------------------------------------------------------------------
# TB-171: `Manual:` / `[manual]` bullets in `## Verification` are rejected
# at queue-append time. Mirrors the TB-138 prompt rule + the
# `_check_briefings_manual_bullets` operator-facing warning into the
# pre-allocation gate so a Manual bullet can't slip into a queued briefing
# and re-run the TB-122 retry_exhausted failure mode.

_TB171_CANONICAL_GOAL = (
    "## Goal\n\nCloses an enforcement gap in the briefing validator.\n\n"
    "Why now: closes the last documented auto-verifiable-bullets "
    "enforcement gap so a Manual bullet can't cost a TB-N + a task-agent "
    "run before being caught (TB-171).\n\n"
)


def _tb171_brief_with_verification(verification_body: str,
                                   out_of_scope: str = "- nothing\n") -> str:
    """Helper: assemble a canonical-shape briefing whose `## Verification`
    body is the caller-supplied string. Saves the per-test boilerplate."""
    return (
        "# TB-171 manual-bullet test\n\n"
        + _TB171_CANONICAL_GOAL
        + "## Scope\n\n- ap2/tools.py\n\n"
        + "## Design\n\nstub\n\n"
        + "## Verification\n\n" + verification_body + "\n"
        + "## Out of scope\n\n" + out_of_scope
    )


def test_validate_briefing_structure_rejects_manual_bullet_in_verification():
    """TB-171 core unit: a `## Verification` bullet starting with
    `Manual:` is rejected at the queue-append boundary with an error
    string that names the offending bullet plus the auto-verifiable
    rationale."""
    body = _tb171_brief_with_verification(
        "- Manual: operator runs the daemon and observes Mattermost\n"
        "- `uv run pytest -q` — gates pass\n",
    )
    err = tools._validate_briefing_structure(body)
    assert err is not None, "expected non-None error for Manual: bullet"
    # Error message names the rule + cross-references TB-171 + TB-138.
    assert "Manual" in err or "auto-verifiable" in err.lower(), err
    assert "TB-171" in err, err


def test_validate_briefing_structure_accepts_manual_bullet_in_out_of_scope():
    """TB-171 scope pin: the validator only scans `## Verification`. A
    `Manual:` bullet under `## Out of scope` is fine — that's exactly
    where the rule says manual procedures belong. The `## Verification`
    body must still carry at least one auto-verifiable bullet."""
    body = _tb171_brief_with_verification(
        "- `uv run pytest -q` — gates pass\n",
        out_of_scope=(
            "- Manual: operator-only smoke test (out of validator scope)\n"
        ),
    )
    err = tools._validate_briefing_structure(body)
    assert err is None, f"expected None, got: {err!r}"


def test_validate_briefing_structure_rejects_manual_bullet_case_insensitive():
    """TB-171 case-insensitive pin: `manual:`, `[Manual]`, and `[manual]`
    all match. Mirrors `ap2/check.py::_MANUAL_BULLET_RE`'s tolerance so
    the queue-append gate doesn't false-pass an alternate spelling that
    the operator-facing lint catches."""
    variants = (
        "- manual: lowercase operator runs X\n",
        "- [Manual] bracketed form, capitalized\n",
        "- [manual] bracketed form, lowercase\n",
        "- MANUAL: shouted form\n",
        "* Manual: asterisk bullet marker, not dash\n",
    )
    for variant in variants:
        body = _tb171_brief_with_verification(
            variant + "- `uv run pytest -q`\n",
        )
        err = tools._validate_briefing_structure(body)
        assert err is not None, (
            f"expected reject for variant: {variant!r}, got None"
        )
        assert "TB-171" in err, err


def test_validate_briefing_structure_does_not_false_positive_on_inline_manual():
    """TB-171 anchor pin: prose that incidentally mentions the word
    `manual` inline (no bullet marker, no `Manual:` / `[manual]` token
    starting the bullet) does NOT trigger the gate. Mirrors the
    `_MANUAL_BULLET_RE` line-anchor on the bullet marker."""
    body = _tb171_brief_with_verification(
        "- `uv run pytest -q` — also covers the manual-fallback path\n"
        "- `grep -q manual ap2/check.py` — pins the lint regex name\n",
    )
    err = tools._validate_briefing_structure(body)
    assert err is None, f"expected None for inline 'manual' prose, got: {err!r}"


def test_tb171_validate_briefing_manual_bullet_fires_via_queue_append(
    cfg, tmp_path,
):
    """End-to-end at the queue-append boundary: a Manual-bullet briefing
    routed through `do_operator_queue_append` is rejected, no TB-N is
    leaked into CLAUDE.md, and no queue line / briefing file is written.
    Mirrors the TB-154 / TB-164 "no leak on reject" pin."""
    before_claude = (tmp_path / "CLAUDE.md").read_text()
    body = _tb171_brief_with_verification(
        "- Manual: operator runs the daemon and watches Mattermost\n"
        "- `uv run pytest -q`\n",
    )
    pre_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "add_backlog", "title": "manual bullet", "briefing": body},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "TB-171" in text
    assert "Manual" in text or "auto-verifiable" in text.lower()
    # CLAUDE.md untouched — no TB-N leaked.
    assert (tmp_path / "CLAUDE.md").read_text() == before_claude
    # No briefing file leaked to disk.
    post_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    assert post_tasks_dir == pre_tasks_dir
    # No queue line written.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_tb171_validate_briefing_manual_bullet_fires_for_update_op(
    cfg, tmp_path,
):
    """TB-153/154-style update-op coverage: the same gate fires when the
    Manual-bullet briefing is routed through `op="update"`. Without this,
    an operator could overwrite a clean briefing with a Manual-bullet
    one and re-introduce the TB-122 failure mode. Mirrors
    `test_tb154_validate_briefing_structure_fires_for_update_op`."""
    bad = _tb171_brief_with_verification(
        "- Manual: operator runs the daemon and watches Mattermost\n"
        "- `uv run pytest -q`\n",
    )
    pre_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-5", "briefing": bad},
    )
    assert res.get("isError"), res
    text = res["content"][0]["text"]
    assert "TB-171" in text
    assert "Manual" in text or "auto-verifiable" in text.lower()
    # No briefing file leaked.
    post_tasks_dir = sorted(p.name for p in cfg.tasks_dir.glob("*.md"))
    assert post_tasks_dir == pre_tasks_dir
    # No queue line written.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""

    # Sanity: a clean update on the same TB-5 with no Manual bullet is
    # accepted. Pins that we didn't flip the update branch into
    # "always rejects briefing".
    clean = _tb171_brief_with_verification("- `uv run pytest -q` — gates pass\n")
    res_ok = tools.do_operator_queue_append(
        cfg,
        {"op": "update", "task_id": "TB-5", "briefing": clean},
    )
    body_ok = _unwrap(res_ok)
    assert body_ok["op"] == "update"


def test_tb171_manual_bullet_check_in_sync_with_check_py():
    """Pin the duplicated-regex contract: `tools._MANUAL_BULLET_RE` must
    behave identically to `check._MANUAL_BULLET_RE` so the queue-append
    gate and the operator-facing `ap2 check` lint can never disagree on
    what counts as a Manual bullet. The briefing's design explicitly
    chose duplication over a tools→check coupling — this test is the
    safety net that catches a future drift."""
    from ap2 import check
    samples_match = (
        "- Manual: x",
        "- manual: x",
        "  - Manual: leading whitespace",
        "* Manual: asterisk bullet",
        "- [manual] bracketed",
        "- [Manual] bracketed cap",
    )
    samples_skip = (
        "Manual: not a bullet (no marker)",
        "- a bullet that mentions manual inline",
        "  the manual fallback path",
        "- `grep -q manual foo.py`",
    )
    for s in samples_match:
        assert tools._MANUAL_BULLET_RE.match(s), s
        assert check._MANUAL_BULLET_RE.match(s), s
    for s in samples_skip:
        assert not tools._MANUAL_BULLET_RE.match(s), s
        assert not check._MANUAL_BULLET_RE.match(s), s
