"""Tests for the ap2 CLI subcommands (TB-77, TB-79).

Lightweight unit tests that call cmd_* directly with an argparse.Namespace
rather than spawning a subprocess.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, retry
from ap2.board import Board
from ap2.cli import _require_oauth_token, cmd_backlog, cmd_delete, cmd_start, cmd_unfreeze
from ap2.config import Config
from ap2.init import init_project


def _project(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_backlog_moves_from_frozen(tmp_path: Path):
    """Replaces what `cmd_skip` used to do: move-to-Backlog from any
    section, including Frozen."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-50", title="legacy frozen")
    board.save()

    rc = cmd_backlog(cfg, Namespace(task_id="TB-50"))
    assert rc == 0

    board2 = Board.load(cfg.tasks_file)
    assert board2.find("TB-50")[0] == "Backlog"


def test_backlog_moves_from_active(tmp_path: Path):
    """Same path also covers Active → Backlog (the original `skip` use case)."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-51", title="stuck active")
    board.save()

    rc = cmd_backlog(cfg, Namespace(task_id="TB-51"))
    assert rc == 0
    assert Board.load(cfg.tasks_file).find("TB-51")[0] == "Backlog"


def test_backlog_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_backlog(cfg, Namespace(task_id="TB-999"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not on board" in err


def test_unfreeze_moves_from_frozen_to_backlog(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-60", title="bug-frozen task")
    board.save()

    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-60"))
    assert rc == 0

    board2 = Board.load(cfg.tasks_file)
    assert board2.find("TB-60")[0] == "Backlog"


def test_unfreeze_clears_retry_state(tmp_path: Path):
    """The whole point of `unfreeze` over `backlog` is fresh retry budget.
    Without this, the next failure pushes the task straight back to Frozen."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-61", title="had retries")
    board.save()
    # Simulate the retry-exhausted state that Frozen tasks come from.
    retry.bump_attempt(cfg.retry_state_file, "TB-61")
    retry.bump_attempt(cfg.retry_state_file, "TB-61")
    retry.bump_attempt(cfg.retry_state_file, "TB-61")
    assert retry.attempt_count(cfg.retry_state_file, "TB-61") == 3

    cmd_unfreeze(cfg, Namespace(task_id="TB-61"))

    assert retry.attempt_count(cfg.retry_state_file, "TB-61") == 0


def test_unfreeze_emits_audit_event(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-62", title="audited unfreeze")
    board.save()

    cmd_unfreeze(cfg, Namespace(task_id="TB-62"))

    evts = events.tail(cfg.events_file, 5)
    unfrozen = [e for e in evts if e["type"] == "task_unfrozen"]
    assert len(unfrozen) == 1
    assert unfrozen[0]["task"] == "TB-62"


def test_unfreeze_refuses_non_frozen(tmp_path: Path, capsys):
    """The validation + move happens inside `locked_board()`; refusing on
    non-Frozen is also where the `backlog` command should be used instead.
    """
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-70", title="already backlog")
    board.save()

    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-70"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "not Frozen" in err
    assert "ap2 backlog" in err  # nudge to the right command
    # Task didn't move.
    assert Board.load(cfg.tasks_file).find("TB-70")[0] == "Backlog"


def test_unfreeze_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-999"))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_start oauth-token precondition (TB-79)


def test_require_oauth_token_passes_when_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-fake")
    assert _require_oauth_token() == 0


def test_require_oauth_token_refuses_when_unset(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    rc = _require_oauth_token()
    assert rc == 1
    err = capsys.readouterr().err
    assert "CLAUDE_CODE_OAUTH_TOKEN" in err
    # Operator-side remediation hints surfaced in the message.
    assert "sudo -u" in err
    assert "install-token" in err


def test_require_oauth_token_refuses_when_blank(monkeypatch):
    """Whitespace-only token = absent (the SDK would still fail). Refuse."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "   ")
    assert _require_oauth_token() == 1


def test_cmd_start_refuses_without_token(tmp_path: Path, monkeypatch, capsys):
    """End-to-end: cmd_start exits 1 + does NOT spawn a subprocess when
    the token is missing. Pinned via subprocess.Popen monkeypatch raising
    if called — the precondition must short-circuit before fork."""
    cfg = _project(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    # Sentinel: if Popen ever runs, fail loudly.
    import subprocess as _sp
    def boom(*a, **kw):
        raise AssertionError("Popen called despite missing token — precondition is broken")
    monkeypatch.setattr(_sp, "Popen", boom)

    rc = cmd_start(cfg, Namespace(foreground=False))
    assert rc == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_delete (TB-107)


def test_delete_removes_from_frozen(tmp_path: Path):
    """Primary use case: abandon a Frozen task that's been superseded.
    Ideation surfaces these in the open-questions list."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-91", title="superseded")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-91", force=False))
    assert rc == 0
    # Task is gone from the board entirely.
    assert Board.load(cfg.tasks_file).find("TB-91") is None


def test_delete_removes_from_backlog(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-80", title="never mind")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-80", force=False))
    assert rc == 0
    assert Board.load(cfg.tasks_file).find("TB-80") is None


def test_delete_refuses_active_without_force(tmp_path: Path, capsys):
    """Active means in-flight; deleting could orphan the SDK subprocess
    or break orphan recovery. Default refusal."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-50", title="running now")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-50", force=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Active" in err
    assert "--force" in err
    # Task untouched.
    assert Board.load(cfg.tasks_file).find("TB-50")[0] == "Active"


def test_delete_refuses_ready_without_force(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Ready", task_id="TB-51", title="next-up")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-51", force=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Ready" in err
    assert "ap2 backlog" in err  # nudge to the right alternative
    assert Board.load(cfg.tasks_file).find("TB-51")[0] == "Ready"


def test_delete_force_allows_active(tmp_path: Path):
    """--force overrides the Active/Ready safety. Use case: stale Active
    line left by a daemon crash, where the operator knows the task isn't
    really running."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-52", title="actually dead")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-52", force=True))
    assert rc == 0
    assert Board.load(cfg.tasks_file).find("TB-52") is None


def test_delete_emits_audit_event(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-92", title="auditable delete")
    board.save()

    cmd_delete(cfg, Namespace(task_id="TB-92", force=False))

    evts = events.tail(cfg.events_file, 5)
    deleted = [e for e in evts if e["type"] == "task_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["task"] == "TB-92"
    assert deleted[0]["section"] == "Frozen"
    assert deleted[0]["title"] == "auditable delete"


def test_delete_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_delete(cfg, Namespace(task_id="TB-999", force=False))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err

