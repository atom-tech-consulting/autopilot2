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
