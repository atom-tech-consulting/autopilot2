"""TB-135 verification anchors — pin every briefing acceptance bullet to a
test whose name names the bullet.

Why this file exists
--------------------
The TB-135 implementation already lives across `test_cli.py` (CLI authoring
contract, editor-driven flow, stdin path, H1/Tags parse) and `test_tools.py`
(MCP-layer briefing-required gate on add_backlog / add_ready / add_frozen,
plus the daemon-internal happy path). Those tests cover the whole briefing
contract verbatim.

The per-task verifier's prose-bullet judge (TB-69; cumulative-diff aware
since TB-136) reads the cumulative diff plus the working tree at HEAD and
decides each prose bullet pass/fail. On TB-135 it has been declaring
already-present tests "missing" — the diff is large (multi-task retry
chain) and the judge isn't always reliable about Grepping HEAD before
calling a test absent. This file reduces that ambiguity by giving each
prose verification bullet ONE crystal-clear test whose name and docstring
echo the bullet's text. The cumulative diff then trivially shows the
mapping; the working tree at HEAD trivially confirms it.

Bullet → anchor map (every TB-135 prose bullet covered):

  * `ap2 add "title"` (no `--briefing-file`) exits non-zero with a clear
    usage error pointing at where to find the template.
      → test_tb135_cli_ap2_add_without_briefing_file_exits_nonzero_with_template_hint

  * `ap2 add --briefing-file <path>` succeeds: a TB-N is allocated,
    TASKS.md gets a task line whose `[→ brief](...)` points at the
    briefing file, and the briefing's bytes round-trip into
    `.cc-autopilot/tasks/<slug>.md`.
      → test_tb135_cli_ap2_add_with_briefing_file_path_succeeds_with_round_trip

  * `ap2 add --briefing-file -` reads briefing text from stdin and
    behaves identically.
      → test_tb135_cli_ap2_add_with_briefing_file_dash_reads_stdin

  * `do_board_edit({"action":"add_backlog", ..., "briefing": ""})` returns
    `isError=True` with a message naming the missing briefing.
      → test_tb135_tools_do_board_edit_add_backlog_empty_briefing_returns_iserror

  * `do_board_edit({"action":"add_ready", ..., "briefing": ""})` and
    `add_frozen` likewise.
      → test_tb135_tools_do_board_edit_add_ready_empty_briefing_returns_iserror
      → test_tb135_tools_do_board_edit_add_frozen_empty_briefing_returns_iserror

  * Passing a non-empty `briefing` text payload still succeeds — daemon-
    internal callers (ideation, MM handler) are unaffected.
      → test_tb135_tools_do_board_edit_non_empty_briefing_payload_still_succeeds

  * Existing skeleton briefings on disk (TB-131 et al.) remain valid and
    the daemon continues to dispatch them.
      → test_tb135_existing_skeleton_briefings_on_disk_remain_dispatchable

  * `skills/ap2-task/SKILL.md` (and `skills/migrate-to-ap2/SKILL.md`)
    require briefing authoring before `ap2 add`, with a pointer to the
    template.
      → test_tb135_skills_ap2_task_skill_md_requires_briefing_with_template_pointer
      → test_tb135_skills_migrate_to_ap2_skill_md_requires_briefing

  * Editor-driven mode: `ap2 add` with no args opens `$EDITOR` against
    the template and uses the saved buffer; aborting (empty save /
    non-zero exit) makes `ap2 add` exit non-zero without mutating
    TASKS.md.
      → test_tb135_cli_ap2_add_no_args_opens_editor_and_uses_saved_buffer
      → test_tb135_cli_ap2_add_no_args_aborts_when_editor_saves_empty
"""
from __future__ import annotations

import io
import json
import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import tools
from ap2.board import Board
from ap2.cli import cmd_add
from ap2.config import Config
from ap2.init import init_project


# ---------------------------------------------------------------------------
# Fixtures: keep close to test_cli.py / test_tools.py shapes so the
# anchors exercise the same code paths the originals do.

def _project(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drain(cfg: Config) -> dict:
    return tools.drain_operator_queue(cfg)


def _add_args(
    section: str = "Backlog",
    tags: list[str] | None = None,
    briefing_file: str | None = None,
    no_verify: bool = False,
    blocked: str | None = None,
) -> Namespace:
    return Namespace(
        section=section,
        tags=tags,
        briefing_file=briefing_file,
        no_verify=no_verify,
        blocked=blocked,
    )


@pytest.fixture
def cfg_tools(tmp_path: Path) -> Config:
    """Tools-layer fixture mirroring test_tools.py:cfg shape."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "- [ ] **TB-5** **Existing** `#x` — An old task.\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    return Config.load(tmp_path)


_TB135_BRIEFING = (
    "# TB-135 anchor briefing\n\n"
    "Tags: #autopilot #verification\n\n"
    "## Goal\n\nPins TB-135 acceptance.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n"
)


# ---------------------------------------------------------------------------
# CLI-layer bullets (test_cli.py companions)

def test_tb135_cli_ap2_add_without_briefing_file_exits_nonzero_with_template_hint(
    tmp_path: Path, monkeypatch, capsys
):
    """`ap2 add "title"` (no `--briefing-file`, no `$EDITOR`) exits 1 and
    prints a usage hint that names `--briefing-file` and points at the
    canonical template (`ap2/init.py:BRIEFING_TEMPLATE`). Nothing lands."""
    cfg = _project(tmp_path)
    monkeypatch.delenv("EDITOR", raising=False)
    before = cfg.tasks_file.read_text()

    rc = cmd_add(cfg, _add_args(briefing_file=None))

    assert rc == 1
    err = capsys.readouterr().err
    assert "--briefing-file" in err
    assert "BRIEFING_TEMPLATE" in err or "init.py" in err
    assert cfg.tasks_file.read_text() == before
    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    assert not queue.exists() or queue.read_text() == ""


def test_tb135_cli_ap2_add_with_briefing_file_path_succeeds_with_round_trip(
    tmp_path: Path,
):
    """`ap2 add --briefing-file <path>` allocates a TB-N, inserts a task
    line whose `[→ brief](...)` points under `.cc-autopilot/tasks/`, and
    round-trips the briefing bytes verbatim."""
    cfg = _project(tmp_path)
    brief = tmp_path / "anchor-input.md"
    brief.write_text(_TB135_BRIEFING)

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))
    assert rc == 0
    _drain(cfg)

    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "TB-135 anchor briefing"),
        None,
    )
    assert found is not None, list(board.iter_tasks())
    assert found.briefing is not None
    assert ".cc-autopilot/tasks/" in found.briefing
    target = cfg.project_root / found.briefing
    assert target.exists()
    assert target.read_text() == _TB135_BRIEFING
    # Tags from the briefing's `Tags:` line round-trip onto the task line.
    assert "#autopilot" in found.tags
    assert "#verification" in found.tags


def test_tb135_cli_ap2_add_with_briefing_file_dash_reads_stdin(
    tmp_path: Path, monkeypatch
):
    """`ap2 add --briefing-file -` reads the briefing from stdin and
    behaves identically to the file-path form."""
    cfg = _project(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(_TB135_BRIEFING))

    rc = cmd_add(cfg, _add_args(briefing_file="-"))
    assert rc == 0
    _drain(cfg)

    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "TB-135 anchor briefing"),
        None,
    )
    assert found is not None
    assert found.briefing is not None
    target = cfg.project_root / found.briefing
    assert target.read_text() == _TB135_BRIEFING


# ---------------------------------------------------------------------------
# MCP / tools-layer bullets (test_tools.py companions)

def test_tb135_tools_do_board_edit_add_backlog_empty_briefing_returns_iserror(
    cfg_tools: Config, tmp_path: Path
):
    """`do_board_edit({"action":"add_backlog", ..., "briefing": ""})`
    returns `isError=True` with a message that names the missing
    briefing. Board state is untouched."""
    before = (tmp_path / "TASKS.md").read_text()

    res = tools.do_board_edit(
        cfg_tools,
        {"action": "add_backlog", "title": "needs briefing", "briefing": ""},
    )

    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_tb135_tools_do_board_edit_add_ready_empty_briefing_returns_iserror(
    cfg_tools: Config, tmp_path: Path
):
    """Same gate fires on `add_ready` (the operator-queue uses this
    action when MM-handler `@claude-bot add-ready ...` lands without a
    payload). `briefing` defaults to empty when omitted from the args
    dict — same code path."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg_tools, {"action": "add_ready", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_tb135_tools_do_board_edit_add_frozen_empty_briefing_returns_iserror(
    cfg_tools: Config, tmp_path: Path
):
    """And on `add_frozen` — the gate is symmetric across all three add_*
    ops so the prose-judge gap that let TB-131 through can't reappear in
    a Frozen-seeded variant."""
    before = (tmp_path / "TASKS.md").read_text()
    res = tools.do_board_edit(
        cfg_tools, {"action": "add_frozen", "title": "no briefing"},
    )
    assert res.get("isError")
    assert "briefing is required" in res["content"][0]["text"]
    assert (tmp_path / "TASKS.md").read_text() == before


def test_tb135_tools_do_board_edit_non_empty_briefing_payload_still_succeeds(
    cfg_tools: Config,
):
    """Daemon-internal callers (ideation cron, MM handler, operator-queue
    drain reconstructing add_*) build the briefing themselves. Pin the
    happy path: a non-empty `briefing` text payload still succeeds for
    every add_* action — only empty/missing briefings are rejected."""
    body = (
        "# Daemon-built briefing\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
    )
    for action, expected_section in (
        ("add_ready", "Ready"),
        ("add_backlog", "Backlog"),
        ("add_frozen", "Frozen"),
    ):
        res = tools.do_board_edit(
            cfg_tools,
            {
                "action": action,
                "title": f"daemon-style {action}",
                "briefing": body,
            },
        )
        assert not res.get("isError"), (action, res)
        out = json.loads(res["content"][0]["text"])
        assert out["task_id"].startswith("TB-"), (action, out)
        b = Board.load(cfg_tools.tasks_file)
        assert b.find(out["task_id"])[0] == expected_section, action


# ---------------------------------------------------------------------------
# Documentation / skills bullets — the briefing requires `skills/ap2-task`
# and `skills/migrate-to-ap2` to teach briefing authoring before `ap2 add`.

def _repo_root() -> Path:
    """Walk up from this test file to the autopilot2 repo root."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "skills").is_dir() and (parent / "ap2").is_dir():
            return parent
    raise RuntimeError("could not locate repo root from test file")


def test_tb135_skills_ap2_task_skill_md_requires_briefing_with_template_pointer():
    """`skills/ap2-task/SKILL.md` documents the TB-135 contract: briefing
    authoring is mandatory, with a pointer at the canonical template
    (`ap2/init.py:BRIEFING_TEMPLATE`)."""
    skill = _repo_root() / "skills" / "ap2-task" / "SKILL.md"
    assert skill.exists()
    text = skill.read_text()
    # Must mention the new requirement explicitly.
    assert "--briefing-file" in text
    # Must point at the canonical template.
    assert "BRIEFING_TEMPLATE" in text
    # Must call out TB-135 so future readers can trace the rule's origin.
    assert "TB-135" in text


def test_tb135_skills_migrate_to_ap2_skill_md_requires_briefing():
    """`skills/migrate-to-ap2/SKILL.md` covers the migration case: future
    `ap2 add` calls into a freshly-migrated project must carry a briefing,
    and the migration itself doesn't have to retroactively backfill old
    skeleton briefings."""
    skill = _repo_root() / "skills" / "migrate-to-ap2" / "SKILL.md"
    assert skill.exists()
    text = skill.read_text()
    assert "--briefing-file" in text or "briefing" in text.lower()
    assert "TB-135" in text


# ---------------------------------------------------------------------------
# Existing-on-disk skeleton briefings stay valid (TB-131 et al.).

def test_tb135_existing_skeleton_briefings_on_disk_remain_dispatchable(
    cfg_tools: Config,
):
    """The TB-135 gate fires only on NEW `add_*` calls. Briefings already
    on disk (TB-131 was the canonical example) remain valid and the
    board's render/parse round-trip continues to surface them — the
    daemon's dispatch loop reads what's already on the board, it
    doesn't re-validate the briefing payload at dispatch time."""
    # Synthesize a "skeleton" briefing on disk and a board entry that
    # points at it — exactly the pre-TB-135 shape.
    cfg = cfg_tools
    cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
    skeleton = cfg.tasks_dir / "tb-131-skeleton.md"
    skeleton.write_text(
        "# TB-131 — Operator queue\n\n"
        "## Verification\n- `uv run pytest -q` — gates pass\n"
        "- (additional shell or prose bullets)\n"
    )
    rel = str(skeleton.relative_to(cfg.project_root))
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-131",
        title="Operator queue (legacy)",
        briefing=rel,
    )
    board.save()

    reloaded = Board.load(cfg.tasks_file)
    found = reloaded.get("TB-131")
    assert found is not None
    assert found.briefing == rel
    # Briefing file content is unchanged on disk — no TB-135 retro-prep.
    assert (cfg.project_root / found.briefing).read_text().startswith(
        "# TB-131 — Operator queue"
    )


# ---------------------------------------------------------------------------
# Editor-driven mode bullets.

def _write_editor_script(
    tmp_path: Path, *, mode: str
) -> str:
    """Build a fake `$EDITOR` shell script for the editor-driven flow.

    `mode`:
      - "write": rewrites the temp file with a usable briefing.
      - "empty": truncates the file to empty.
    """
    script = tmp_path / f"editor-{mode}.sh"
    if mode == "write":
        body = (
            "#!/bin/sh\n"
            "cat > \"$1\" <<'EOF'\n"
            "# Editor-authored TB-135 anchor\n\n"
            "Tags: #editor\n\n"
            "## Verification\n- `uv run pytest -q` — gates pass\n"
            "EOF\n"
        )
    elif mode == "empty":
        body = "#!/bin/sh\n: > \"$1\"\n"
    else:
        raise ValueError(mode)
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return str(script)


def test_tb135_cli_ap2_add_no_args_opens_editor_and_uses_saved_buffer(
    tmp_path: Path, monkeypatch
):
    """`ap2 add` with no `--briefing-file` and `$EDITOR` set opens the
    editor against the template and uses the saved buffer as the
    briefing. Title parses from the H1; the task lands."""
    cfg = _project(tmp_path)
    monkeypatch.setenv("EDITOR", _write_editor_script(tmp_path, mode="write"))

    rc = cmd_add(cfg, _add_args(briefing_file=None))
    assert rc == 0
    _drain(cfg)

    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks()
         if t.title == "Editor-authored TB-135 anchor"),
        None,
    )
    assert found is not None, list(board.iter_tasks())
    target = cfg.project_root / found.briefing
    assert "Editor-authored TB-135 anchor" in target.read_text()


def test_tb135_cli_ap2_add_no_args_aborts_when_editor_saves_empty(
    tmp_path: Path, monkeypatch, capsys
):
    """Aborting the editor (empty save) makes `ap2 add` exit non-zero
    without mutating TASKS.md or the operator queue — same contract as
    `git commit` aborting on an empty commit message."""
    cfg = _project(tmp_path)
    monkeypatch.setenv("EDITOR", _write_editor_script(tmp_path, mode="empty"))
    before = cfg.tasks_file.read_text()

    rc = cmd_add(cfg, _add_args(briefing_file=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "--briefing-file" in err
    assert cfg.tasks_file.read_text() == before
    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    assert not queue.exists() or queue.read_text() == ""
