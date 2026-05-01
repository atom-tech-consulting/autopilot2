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
# Dependency enforcement: `@blocked:TB-X,...` codespan on the task line gates promotion

def _make(task_id: str, section: str, *, blocked: str = "", description: str = "") -> "Task":  # noqa: F821
    from ap2.board import Task
    meta = {"blocked": blocked} if blocked else {}
    return Task(
        id=task_id, title="t", section=section,
        description=description, meta=meta,
    )


def test_blocked_on_single_id():
    t = _make("TB-2", "Backlog", blocked="TB-5")
    assert t.blocked_on == ["TB-5"]


def test_blocked_on_multiple_ids_comma_separated():
    t = _make("TB-2", "Backlog", blocked="TB-5,TB-7,TB-12")
    assert t.blocked_on == ["TB-5", "TB-7", "TB-12"]


def test_blocked_on_empty_when_no_meta():
    t = _make("TB-2", "Backlog", description="plain description, no blockers here")
    assert t.blocked_on == []


def test_blocked_on_empty_for_no_description():
    t = _make("TB-2", "Backlog")
    assert t.blocked_on == []


def test_next_ready_skips_blocked_task(tmp_path):
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **needs TB-1** `@blocked:TB-1` — go next after blocker clears
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

        - [ ] **TB-3** **depends** `@blocked:TB-1` — needs the blocker

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

        - [ ] **TB-5** **first** `@blocked:TB-99` — wants a never-completing blocker
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

        - [ ] **TB-3** **a** `@blocked:TB-99` — never-completing blocker
        - [ ] **TB-4** **b** `@blocked:TB-99` — also blocked

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

        - [ ] **TB-10** **needs TB-9** `@blocked:TB-9` — gated on TB-9

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


def test_orphan_non_task_lines_are_flagged_malformed(tmp_path):
    """TB-92: non-task lines that wedge into a section (e.g. unfinalized
    `/tb prep` prose that never got wrapped in a `- [ ]` bullet) must show
    up in `malformed_lines`. Otherwise they silently inflate `ap2 status`
    section counts — diagnosed live in stoch where 3 lines of orphan
    README scope text were reported as `3B` Backlog tasks despite
    `iter_tasks('Backlog')` returning 0.
    """
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        ## Backlog

          README.md: one-line purpose (swing equity backtester, 7-14 day holds), install
          configs/baseline_momentum.yaml), data cache layout, current baseline snapshot
          (stoch/{data,engine,strategy,metrics,reports,io,sweep}), link to project.md.

        ## Complete

        - [x] **TB-1** **first** — done

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    # All 3 orphan prose lines flagged.
    backlog_malformed = [(s, line) for (s, line) in b.malformed_lines if s == "Backlog"]
    assert len(backlog_malformed) == 3
    assert all("README.md" in line or "configs/" in line or "stoch/" in line
               for _, line in backlog_malformed), backlog_malformed
    # Dispatch path remains correct: orphan lines are not iter_tasks-visible.
    assert list(b.iter_tasks("Backlog")) == []
    assert b.next_dispatchable("Backlog") is None
    # The legitimate task in Complete still parses and isn't flagged.
    assert any(t.id == "TB-1" for t in b.iter_tasks("Complete"))
    complete_malformed = [(s, line) for (s, line) in b.malformed_lines if s == "Complete"]
    assert complete_malformed == []


def test_clean_board_has_no_malformed_lines(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    assert b.malformed_lines == []


# ---------------------------------------------------------------------------
# TB-81: blocked_on now returns ALL comma-separated tokens — TB-N and external
# `<scheme>:<value>` schemes (currently `pid:<N>@<TS>`).

def test_blocked_on_returns_pid_scheme_token():
    t = _make("TB-2", "Backlog", blocked="pid:12345@1700000000")
    assert t.blocked_on == ["pid:12345@1700000000"]


def test_blocked_on_returns_mixed_tb_and_pid_tokens():
    t = _make("TB-2", "Backlog", blocked="TB-5,pid:99@1700000000")
    assert t.blocked_on == ["TB-5", "pid:99@1700000000"]


def test_is_blocker_satisfied_tb_in_completed(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    assert b._is_blocker_satisfied("TB-9", {"TB-9"}) is True
    assert b._is_blocker_satisfied("TB-99", {"TB-9"}) is False


def test_is_blocker_satisfied_unknown_scheme_fails_safe(tmp_path):
    path = _write_board(tmp_path, SAMPLE)
    b = Board.load(path)
    # Typo / unknown prefix → treat as unsatisfied so the task stays put
    # rather than silently dispatching. Includes the TB-117-retired
    # `pid:N@TS` scheme — any straggler from a pre-TB-115 board
    # remains stuck Backlog until the operator removes the clause.
    assert b._is_blocker_satisfied("pidd:1@2", set()) is False
    assert b._is_blocker_satisfied("file:/tmp/foo", set()) is False
    assert b._is_blocker_satisfied("pid:1@2", set()) is False


# ---------------------------------------------------------------------------
# TB-132: structured `@<key>:<value>` codespans replace prose-regex blocker
# parsing. Tags (`#tag`) and meta (`@k:v`) live in the same backtick-span
# list; the parser keeps them distinct in `tags` vs `meta` on Task.

def test_parse_task_line_populates_meta_alongside_tags():
    """TASK_LINE_RE captures all backtick spans; parse_task_line splits
    them into `tags` (`#`-prefix) vs `meta` (`@key:value`). The two
    surfaces are kept distinct so a future `@<key>:<value>` field
    doesn't accidentally show up in `tags` or vice versa."""
    line = (
        "- [ ] **TB-42** **t** `#infra` `#urgent` "
        "`@blocked:TB-5,TB-7` `@owner:alice` — go"
    )
    t = parse_task_line(line, "Ready")
    assert t is not None
    assert t.tags == ["#infra", "#urgent"]
    assert t.meta == {"blocked": "TB-5,TB-7", "owner": "alice"}


def test_blocked_on_reads_from_meta_codespan_only():
    """TB-132: when a `@blocked:` codespan is set, `Task.blocked_on`
    sources its tokens from there and ignores any `(blocked on: ...)`
    substring in the description — including descriptive prose that
    used to false-trigger the legacy regex (TB-121's failure mode)."""
    from ap2.board import Task

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
    assert t.blocked_on == ["TB-5"]


def test_render_emits_meta_after_tags_before_emdash():
    """Render order: id → title → tags → meta → em-dash → description →
    briefing link. Round-trip parse → render is byte-identical so the
    Board's lossless preservation invariant still holds."""
    from ap2.board import Task

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
    # Round-trip: parse the rendered line, render it back, must be byte-
    # identical. Pins ordering (tags before meta) and that no whitespace
    # gets lost in either direction.
    t2 = parse_task_line(expected, "Backlog")
    assert t2 is not None
    assert t2.render() == expected


def test_tb121_prose_does_not_block():
    """TB-132's specific failure-mode test: TB-121's exact prose contains
    `(blocked on: review)` as descriptive text (the design literally
    quoted the proposed clause syntax). Pre-TB-132 the legacy regex
    auto-blocked TB-121 on the non-existent token `review` and stranded
    it in Backlog forever. With TB-132's codespan format now exclusive
    (the legacy fallback was dropped after the in-flight backlog
    migrated), a task whose only blocker signal is the description prose
    parses with `blocked_on == []`."""
    from ap2.board import Task

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
    assert t.blocked_on == []


def test_codespan_blocker_skipped_when_target_incomplete(tmp_path):
    """End-to-end: a task with `@blocked:TB-5` and TB-5 NOT in Complete
    must not be auto-dispatched. Mirrors `test_next_ready_skips_blocked_task`
    but exercises the codespan path instead of the legacy clause."""
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **needs TB-1** `@blocked:TB-1` — depends on TB-1
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
    assert t.id == "TB-4"


def test_codespan_blocker_dispatches_when_target_completes(tmp_path):
    """The same task becomes dispatchable once TB-5 lands in Complete —
    exercises that the codespan path drives `next_ready` exactly the
    way the legacy clause does, just sourced from `meta` instead of
    description regex."""
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        - [ ] **TB-3** **depends** `@blocked:TB-1` — needs TB-1

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


def test_next_dispatchable_pid_blocker_strands_task(tmp_path):
    """TB-117: the retired `pid:N@TS` scheme always evaluates to "not
    satisfied" — any pre-TB-115 task whose Backlog line still carries
    such a blocker stays put until an operator removes the clause."""
    text = textwrap.dedent(
        """\
        # Tasks

        ## Active

        ## Ready

        ## Backlog

        - [ ] **TB-5** **validate** `@blocked:pid:12345@1700000000` — runs after the pipeline

        ## Pipeline Pending

        ## Complete

        ## Frozen
        """
    )
    path = _write_board(tmp_path, text)
    b = Board.load(path)
    assert b.next_dispatchable("Backlog") is None
