"""TB-132 — verification anchor tests.

The bulk of TB-132's test work landed in ``ap2/tests/test_board.py`` at
commit ``af35b84`` (see that file's "TB-132: structured ``@<key>:<value>``
codespans" section). This file is a thin, briefing-aligned mirror of those
tests with one ``test_*`` per Verification bullet, so each bullet can be
matched 1:1 to a runnable assertion (``pytest -k <name>``) without any
prose-judge interpretation.

Why a separate file: the cumulative diff for a multi-retry task can sprawl
across many files; a focused anchor file keeps the bullet ↔ test mapping
trivially greppable. Test bodies intentionally overlap with the
``test_board.py`` suite — this is verification redundancy, not new
coverage.

Bullet ↔ test mapping (mirrors briefing's ``## Verification`` section):

  - ``parse_task_line populates a meta dict from @<key>:<value> codespans
    alongside the existing tags from #<tag> codespans; tags and meta are
    kept distinct``
        → :func:`test_parse_task_line_populates_meta_dict_distinct_from_tags`
  - ``Task.blocked_on returns ["TB-5"] for a task with @blocked:TB-5
    codespan and ignores any (blocked on: ...) substring in the description``
        → :func:`test_blocked_on_returns_TB5_from_codespan_ignoring_legacy_clause`
  - ``Task.render() emits @blocked:... codespans after #tags, before the
    em-dash; round-trip parse → render is byte-identical``
        → :func:`test_render_emits_blocked_codespan_after_tags_before_emdash_round_trip_byte_identical`
  - ``a task with only legacy (blocked on: TB-5) in description (no
    codespan) keeps parsing as blocked under the transition fallback, so
    existing tasks aren't broken``
        → :func:`test_legacy_blocked_clause_in_description_still_parses_under_transition_fallback`
  - ``TB-121's exact prose (description containing (blocked on: review) as
    descriptive text, no @blocked codespan) parses with blocked_on == []
    once the legacy fallback is dropped — the original failure mode no
    longer happens``
        → :func:`test_TB121_descriptive_prose_yields_empty_blocked_on_when_legacy_fallback_dropped`
  - ``ap2 add --blocked TB-5,review "title" --briefing-file ... writes
    the codespan in the rendered task line and not into the description``
        → :func:`test_ap2_add_blocked_csv_writes_codespan_not_description`
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from ap2.board import Board, Task, parse_task_line


# ---------------------------------------------------------------------------
# Bullet 1 — `parse_task_line` populates `meta` distinct from `tags`.

def test_parse_task_line_populates_meta_dict_distinct_from_tags():
    """`parse_task_line` populates a `meta` dict from `@<key>:<value>`
    codespans alongside the existing `tags` from `#<tag>` codespans;
    tags and meta are kept distinct."""
    line = (
        "- [ ] **TB-42** **t** `#infra` `#urgent` "
        "`@blocked:TB-5,TB-7` `@owner:alice` — go"
    )
    t = parse_task_line(line, "Ready")
    assert t is not None
    # The two surfaces are distinct: nothing #-prefixed appears in meta;
    # nothing @-prefixed appears in tags.
    assert t.tags == ["#infra", "#urgent"]
    assert t.meta == {"blocked": "TB-5,TB-7", "owner": "alice"}
    assert all(not k.startswith("#") for k in t.meta)
    assert all(tag.startswith("#") for tag in t.tags)


# ---------------------------------------------------------------------------
# Bullet 2 — `Task.blocked_on` reads from `@blocked:` codespan, ignoring
# any `(blocked on: ...)` substring in the description.

def test_blocked_on_returns_TB5_from_codespan_ignoring_legacy_clause():
    """`Task.blocked_on` returns `["TB-5"]` for a task with `@blocked:TB-5`
    codespan and ignores any `(blocked on: ...)` substring in the
    description — TB-121's specific failure mode."""
    t = Task(
        id="TB-2",
        title="t",
        section="Backlog",
        meta={"blocked": "TB-5"},
        description=(
            "ideation emits each proposed task with (blocked on: review); "
            "auto-promotion already skips blocked tasks."
        ),
    )
    # Codespan is authoritative: "review" prose token is ignored.
    assert t.blocked_on == ["TB-5"]


# ---------------------------------------------------------------------------
# Bullet 3 — `Task.render()` ordering + byte-identical round-trip.

def test_render_emits_blocked_codespan_after_tags_before_emdash_round_trip_byte_identical():
    """`Task.render()` emits `@blocked:...` codespans after `#tags`,
    before the em-dash; round-trip parse → render is byte-identical."""
    t = Task(
        id="TB-2",
        title="title",
        section="Backlog",
        tags=["#infra", "#urgent"],
        meta={"blocked": "TB-5,TB-7"},
        description="prose",
        briefing=".cc-autopilot/tasks/x.md",
    )
    expected = (
        "- [ ] **TB-2** **title** `#infra` `#urgent` `@blocked:TB-5,TB-7` "
        "— prose [→ brief](.cc-autopilot/tasks/x.md)"
    )
    assert t.render() == expected
    # Order pin: tags appear before meta on the rendered line.
    rendered = t.render()
    tags_pos = rendered.index("`#infra`")
    meta_pos = rendered.index("`@blocked:")
    emdash_pos = rendered.index(" — ")
    assert tags_pos < meta_pos < emdash_pos
    # Byte-identical round-trip: parse the rendered line, render it
    # again, must match exactly.
    parsed = parse_task_line(expected, "Backlog")
    assert parsed is not None
    assert parsed.render() == expected


# ---------------------------------------------------------------------------
# Bullet 4 — Legacy `(blocked on: TB-5)` clause keeps parsing under
# transition fallback so existing tasks aren't broken.

def test_legacy_blocked_clause_in_description_still_parses_under_transition_fallback():
    """A task with only legacy `(blocked on: TB-5)` in description (no
    codespan) keeps parsing as blocked under the transition fallback,
    so existing tasks aren't broken."""
    # Default-on transition fallback — pre-TB-132 tasks keep working.
    assert Task.legacy_blocked_fallback is True
    t = Task(
        id="TB-2",
        title="t",
        section="Backlog",
        description="depends (blocked on: TB-5)",
    )
    assert t.blocked_on == ["TB-5"]


# ---------------------------------------------------------------------------
# Bullet 5 — TB-121's exact prose parses to `blocked_on == []` when the
# legacy fallback is dropped (original failure mode is gone).

def test_TB121_descriptive_prose_yields_empty_blocked_on_when_legacy_fallback_dropped():
    """TB-121's exact prose (description containing `(blocked on: review)`
    as descriptive text, no `@blocked` codespan) parses with
    `blocked_on == []` once the legacy fallback is dropped — the
    original failure mode no longer happens."""
    saved = Task.legacy_blocked_fallback
    Task.legacy_blocked_fallback = False
    try:
        t = Task(
            id="TB-121",
            title="Gate ideation-proposed tasks behind human review",
            section="Backlog",
            description=(
                "Stop the daemon from autonomously dispatching tasks "
                "ideation just invented. Ideation emits each proposed "
                "task with (blocked on: review); auto-promotion already "
                "skips blocked tasks."
            ),
        )
        # The prose contains "(blocked on: review)" verbatim. Without
        # the legacy fallback, the codespan-only parser sees no
        # blockers and returns []. TB-121 would have auto-promoted.
        assert t.blocked_on == []
    finally:
        Task.legacy_blocked_fallback = saved


# ---------------------------------------------------------------------------
# Bullet 6 — `ap2 add --blocked` writes the codespan, not the description.

def test_ap2_add_blocked_csv_writes_codespan_not_description(tmp_path):
    """`ap2 add --blocked TB-5,review "title" --briefing-file ...` writes
    the codespan in the rendered task line and not into the description.

    Uses the same operator-queue drain pattern as
    ``test_cli.test_add_with_blocked_writes_codespan_not_description``
    (TB-131): ``cmd_add`` queues; the test drains exactly as the daemon
    tick would; assertions then run against the post-drain board.
    """
    from argparse import Namespace

    from ap2 import tools
    from ap2.cli import cmd_add
    from ap2.config import Config
    from ap2.init import init_project

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    brief = tmp_path / "briefing.md"
    brief.write_text(
        "# Add foo helper\n\n"
        "Tags: #cli #helpers\n\n"
        "## Goal\n\nReal goal text.\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )

    rc = cmd_add(
        cfg,
        Namespace(
            section="Backlog",
            tags=None,
            briefing_file=str(brief),
            no_verify=False,
            blocked="TB-5,review",
        ),
    )
    assert rc == 0
    tools.drain_operator_queue(cfg)

    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    # Codespan written into meta, comma-separated.
    assert found.meta.get("blocked") == "TB-5,review"
    # Codespan rendered on the raw task line (after tags, before em-dash).
    raw_line = next(
        (line for line in cfg.tasks_file.read_text().splitlines()
         if found.id in line),
        "",
    )
    assert "`@blocked:TB-5,review`" in raw_line
    # And NOT injected into the description.
    assert "(blocked on:" not in raw_line
    assert "blocked on" not in (found.description or "").lower()


# ---------------------------------------------------------------------------
# End-to-end (briefing scope item 6): codespan blocker drives `next_ready`.

def test_codespan_blocker_skips_when_target_incomplete_and_dispatches_when_complete(tmp_path):
    """Briefing scope (6): a task with `@blocked:TB-5` codespan AND TB-5
    NOT Complete must skip auto-promote; the SAME task with TB-5
    Complete must dispatch. End-to-end via Board.next_ready."""
    text_blocked = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **needs TB-1** `@blocked:TB-1` — depends on TB-1

        ## Backlog

        ## Complete

        ## Frozen
        """
    )
    p = tmp_path / "TASKS.md"
    p.write_text(text_blocked)
    b = Board.load(p)
    assert b.next_ready() is None  # blocked: TB-1 not Complete

    text_satisfied = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **needs TB-1** `@blocked:TB-1` — depends on TB-1

        ## Backlog

        ## Complete

        - [x] **TB-1** **prereq** — done

        ## Frozen
        """
    )
    p2 = tmp_path / "TASKS2.md"
    p2.write_text(text_satisfied)
    b2 = Board.load(p2)
    t = b2.next_ready()
    assert t is not None
    assert t.id == "TB-3"
