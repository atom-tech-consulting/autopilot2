"""Tests for the TASKS.md parser."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ap2.board import Board, SECTIONS, locked_board, parse_task_line


SAMPLE = textwrap.dedent(
    """\
    # Tasks

    ## Active

    ## Ready

    - [ ] **TB-10** **Fix the thing** `#infra` `#urgent` — Patch edge case. [→ brief](.cc-autopilot/tasks/fix-the-thing.md)

    ## Backlog

    - [ ] **TB-11** **Write docs** `#docs` — Cover module X.

    ## Complete

    - [x] **TB-9** **Old task** `#done` — Was done.

    ## Frozen

    - [ ] **TB-5** **Frozen task** `#future` — Do later.
    """
)


def _write_board(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "TASKS.md"
    p.write_text(text)
    return p


def test_parse_all_sections(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    for s in SECTIONS:
        assert s in b.sections
    assert len(b.sections["Ready"]) == 1
    assert len(b.sections["Backlog"]) == 1
    assert len(b.sections["Complete"]) == 1
    assert len(b.sections["Frozen"]) == 1
    assert b.sections["Active"] == []


def test_parse_task_line():
    line = "- [ ] **TB-42** **Fix the thing** `#infra` `#urgent` — Do it [→ brief](.cc-autopilot/tasks/fix.md)"
    t = parse_task_line(line, "Ready")
    assert t is not None
    assert t.id == "TB-42"
    assert t.num == 42
    assert t.title == "Fix the thing"
    assert t.tags == ["#infra", "#urgent"]
    assert t.description == "Do it"
    assert t.briefing == ".cc-autopilot/tasks/fix.md"
    assert t.checked is False


def test_move_sets_checkbox_on_complete(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    t = b.move("TB-10", "Complete")
    assert t.section == "Complete"
    assert t.checked is True
    b.save()
    reloaded = Board.load(path)
    assert reloaded.find("TB-10")[0] == "Complete"
    line = reloaded.sections["Complete"][0]
    assert line.startswith("- [x]")


def test_add_assigns_into_right_section(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    b.add("Ready", task_id="TB-50", title="New task", tags=["#test"])
    # Ready inserts at top
    top = b.sections["Ready"][0]
    assert "TB-50" in top
    b.save()
    assert "TB-50" in path.read_text()


def test_next_ready(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    t = b.next_ready()
    assert t and t.id == "TB-10"


def test_remove(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    removed = b.remove("TB-11")
    assert removed and removed.id == "TB-11"
    assert b.find("TB-11") is None


def test_locked_board_roundtrip(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    with locked_board(path) as b:
        b.add("Backlog", task_id="TB-99", title="Concurrent add")
    reloaded = Board.load(path)
    assert reloaded.find("TB-99") is not None


def test_max_id(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    assert b.max_id() == 11


def test_roundtrip_preserves_sections(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    b.save()
    text = path.read_text()
    for s in SECTIONS:
        assert f"## {s}" in text


# ---------------------------------------------------------------------------
# Dependency enforcement: `(blocked on: TB-X)` in description gates promotion

def _make(task_id: str, section: str, description: str = "") -> "Task":  # noqa: F821
    from ap2.board import Task
    return Task(id=task_id, title="t", section=section, description=description)


def test_blocked_on_single_id():
    t = _make("TB-2", "Backlog", "needs this (blocked on: TB-5)")
    assert t.blocked_on == ["TB-5"]


def test_blocked_on_multiple_ids_comma_separated():
    t = _make("TB-2", "Backlog", "x (blocked on: TB-5, TB-7, TB-12) y")
    assert t.blocked_on == ["TB-5", "TB-7", "TB-12"]


def test_blocked_on_case_insensitive_and_natural_language():
    # Humans might type "Blocked on: TB-5 and TB-7" — should still parse.
    t = _make("TB-2", "Backlog", "(Blocked on: TB-5 and TB-7)")
    assert t.blocked_on == ["TB-5", "TB-7"]


def test_blocked_on_empty_when_no_clause():
    t = _make("TB-2", "Backlog", "plain description, no blockers here")
    assert t.blocked_on == []


def test_blocked_on_empty_for_no_description():
    t = _make("TB-2", "Backlog", "")
    assert t.blocked_on == []


def test_next_ready_skips_blocked_task(tmp_path):
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **needs TB-1** — go next after (blocked on: TB-1)
        - [ ] **TB-4** **standalone** — no blockers

        ## Backlog

        ## Complete

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    t = b.next_ready()
    assert t is not None
    # Top of Ready is TB-3 but it's blocked — next_ready picks TB-4 instead.
    assert t.id == "TB-4"


def test_next_ready_returns_blocked_task_once_blocker_completes(tmp_path):
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **depends** — needs it (blocked on: TB-1)

        ## Backlog

        ## Complete

        - [x] **TB-1** **blocker** — done

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    t = b.next_ready()
    assert t is not None
    assert t.id == "TB-3"


def test_next_dispatchable_backlog_skips_blocked(tmp_path):
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        ## Backlog

        - [ ] **TB-5** **first** — wants it (blocked on: TB-99)
        - [ ] **TB-6** **second** — clear path

        ## Complete

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    t = b.next_dispatchable("Backlog")
    assert t is not None
    assert t.id == "TB-6"


def test_next_dispatchable_returns_none_when_all_blocked(tmp_path):
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **a** — (blocked on: TB-99)
        - [ ] **TB-4** **b** — (blocked on: TB-99)

        ## Backlog

        ## Complete

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    assert b.next_ready() is None
    assert b.next_dispatchable("Ready") is None


def test_malformed_complete_line_is_surfaced(tmp_path):
    """A line like `**TB-9** (sha) **Title**` (manual edit injecting an
    annotation between ID and title) doesn't match TASK_LINE_RE — so the task
    is invisible to `completed_ids()` and silently blocks anything that depends
    on it. The parser should record it in `malformed_lines` so the daemon can
    emit a warning instead of mysteriously refusing to dispatch.
    """
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        ## Backlog

        - [ ] **TB-10** **needs TB-9** — (blocked on: TB-9)

        ## Complete

        - [x] **TB-9** (abc1234) **Old task** — was done with sha annotation.

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    assert b.malformed_lines, "expected the (abc1234)-annotated TB-9 line to be flagged"
    section, line = b.malformed_lines[0]
    assert section == "Complete"
    assert "TB-9" in line and "(abc1234)" in line
    # And the consequence: TB-9 isn't seen as completed, so TB-10 stays blocked.
    assert "TB-9" not in b.completed_ids()
    assert b.next_dispatchable("Backlog") is None


def test_clean_board_has_no_malformed_lines(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    assert b.malformed_lines == []
