"""Tests for ap2.sandbox."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ap2 import sandbox


def _fake_run(return_map):
    """Build a subprocess.run stub keyed by the first 2 argv tokens."""
    def runner(argv, *a, **kw):
        key = tuple(argv[:2])
        result = return_map.get(key, return_map.get(argv[0], None))
        if result is None:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        rc, stdout = result
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")
    return runner


# ---------------------------------------------------------------------------
# command-builder shape

def test_darwin_create_commands_shape():
    cmds = sandbox._darwin_create_commands("claude-agent", 601)
    assert cmds[0] == ["sudo", "dscl", ".", "-create", "/Users/claude-agent"]
    assert ["sudo", "dscl", ".", "-create", "/Users/claude-agent", "UniqueID", "601"] in cmds
    assert cmds[-1] == ["sudo", "createhomedir", "-c", "-u", "claude-agent"]
    # All commands start with sudo — no bare side-effects.
    assert all(c[0] == "sudo" for c in cmds)


def test_linux_create_commands_shape():
    cmds = sandbox._linux_create_commands("claude-agent")
    assert ["sudo", "useradd", "--create-home", "--shell", "/bin/bash", "claude-agent"] in cmds
    assert ["sudo", "passwd", "-l", "claude-agent"] in cmds


# ---------------------------------------------------------------------------
# UID probe

def test_next_darwin_uid_parses_output(monkeypatch):
    def fake(argv, *a, **kw):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout="_mbsetupuser   248\nlzhang         501\n_otheruser  not_a_number\n",
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake)
    # Highest parseable UID is 501 → next is 601 (floor).
    assert sandbox._next_darwin_uid() == 601


def test_next_darwin_uid_picks_above_existing(monkeypatch):
    def fake(argv, *a, **kw):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout="u 700\nv 650\n",
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake)
    assert sandbox._next_darwin_uid() == 701


def test_next_darwin_uid_fallback_when_sudo_fails(monkeypatch):
    def fake(argv, *a, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    assert sandbox._next_darwin_uid() == 601


# ---------------------------------------------------------------------------
# AuditResult

def test_audit_result_fail_flips_ok():
    r = sandbox.AuditResult()
    r.add("OK", "a")
    assert r.ok is True
    r.add("INFO", "b")
    assert r.ok is True
    r.add("FAIL", "c")
    assert r.ok is False


# ---------------------------------------------------------------------------
# user_audit

def test_user_audit_missing_user(monkeypatch):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    res = sandbox.user_audit("nope")
    assert res.ok is False
    assert any("does not exist" in m for _, m in res.messages)


def test_user_audit_clean(monkeypatch, tmp_path):
    # Fake home with no credential files and no venv; subprocess returns empty env.
    home = tmp_path / "agent-home"
    home.mkdir()

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )

    res = sandbox.user_audit("fakeuser")
    assert res.ok is True
    # All credential paths absent → OK entries.
    ok_msgs = [m for lvl, m in res.messages if lvl == "OK"]
    assert any("SSH RSA key absent" in m for m in ok_msgs)
    assert any("GH_TOKEN unset" in m for m in ok_msgs)
    # cc-perms venv intentionally not checked — bypassed via setting_sources.
    assert not any("cc-perms" in m for _, m in res.messages)


def test_user_audit_flags_credential_file(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE")

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )

    res = sandbox.user_audit("fakeuser")
    assert res.ok is False
    fails = [m for lvl, m in res.messages if lvl == "FAIL"]
    assert any("SSH RSA key exists" in m for m in fails)


def test_user_audit_flags_env_token(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()

    def fake(argv, *a, **kw):
        # printenv GH_TOKEN returns a value; everything else is clean.
        if "printenv GH_TOKEN" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, stdout="ghp_secret\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(subprocess, "run", fake)

    res = sandbox.user_audit("fakeuser")
    assert res.ok is False
    assert any("$GH_TOKEN is set" in m for lvl, m in res.messages if lvl == "FAIL")


# ---------------------------------------------------------------------------
# project_audit

def test_project_audit_missing_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    res = sandbox.project_audit(tmp_path / "nope", "claude-agent")
    assert res.ok is False
    assert any("path does not exist" in m for lvl, m in res.messages if lvl == "FAIL")


def test_project_audit_not_a_git_repo(monkeypatch, tmp_path):
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    res = sandbox.project_audit(tmp_path / "repo", "claude-agent")
    assert res.ok is False
    assert any("not a git repo" in m for lvl, m in res.messages if lvl == "FAIL")


def test_project_audit_wrong_owner(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_path_owner", lambda p: "someone-else")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=""),
    )

    res = sandbox.project_audit(repo, "claude-agent")
    assert res.ok is False
    assert any(
        "owned by someone-else" in m for lvl, m in res.messages if lvl == "FAIL"
    )


def test_project_audit_flags_live_upstream_push(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_path_owner", lambda p: "claude-agent")

    def fake(argv, *a, **kw):
        if "--push" in argv and "upstream" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="git@github.com:owner/repo.git\n", stderr=""
            )
        # Everything else: return failure (no 'local' remote, no bare).
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)

    res = sandbox.project_audit(repo, "claude-agent")
    assert res.ok is False
    fails = [m for lvl, m in res.messages if lvl == "FAIL"]
    assert any("upstream push URL is live" in m for m in fails)


# ---------------------------------------------------------------------------
# confirmation

def test_confirm_assume_yes_bypasses_input():
    assert sandbox._confirm("go?", assume_yes=True) is True


def test_confirm_rejects_without_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda p: "n")
    assert sandbox._confirm("go?") is False


def test_confirm_accepts_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda p: "y")
    assert sandbox._confirm("go?") is True


def test_confirm_eof_rejects(monkeypatch):
    def boom(p):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    assert sandbox._confirm("go?") is False


# ---------------------------------------------------------------------------
# user_setup decline path (must not execute any commands)

def test_user_setup_declines_and_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr("builtins.input", lambda p: "n")

    called = []

    def fake_run(argv, *a, **kw):
        called.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.user_setup("fakeuser")
    assert rc == 1  # declined
    # No sudo invocations should have been issued.
    assert not any(argv and argv[0] == "sudo" for argv in called)
    out = capsys.readouterr().out
    assert "aborted." in out


def test_user_setup_already_exists(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    rc = sandbox.user_setup("existing")
    assert rc == 0
    assert "already exists" in capsys.readouterr().out


def test_user_setup_unsupported_os(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Windows")
    rc = sandbox.user_setup("fakeuser")
    assert rc == 1
    assert "unsupported OS" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# project_setup guard rails

def test_project_setup_rejects_missing_user(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    rc = sandbox.project_setup(tmp_path, "nope")
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_project_setup_rejects_non_repo(monkeypatch, tmp_path, capsys):
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    rc = sandbox.project_setup(tmp_path / "plain", "claude-agent")
    assert rc == 1
    assert "not a git repo" in capsys.readouterr().err
