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
    # skip_token avoids the interactive getpass prompt in test runs.
    rc = sandbox.user_setup("existing", skip_token=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "already exists" in out
    # skip_token should suppress both the prompt and the "install-token later" nudge.
    assert "install-token" not in out


def test_user_setup_unsupported_os(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Windows")
    rc = sandbox.user_setup("fakeuser", skip_token=True)
    assert rc == 1
    assert "unsupported OS" in capsys.readouterr().err


def test_user_setup_yes_mode_prints_token_hint(monkeypatch, capsys):
    """`--yes` can't run the interactive token flow — we nudge instead."""
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    rc = sandbox.user_setup("existing", assume_yes=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "already exists" in out
    assert "ap2 sandbox install-token existing" in out


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


# ---------------------------------------------------------------------------
# install_oauth_token

def test_install_oauth_token_refuses_unknown_user(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: False)
    rc = sandbox.install_oauth_token("nobody", "tok")
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_install_oauth_token_refuses_empty_token(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    rc = sandbox.install_oauth_token("claude-agent", "   ")
    assert rc == 1
    assert "empty token" in capsys.readouterr().err


def test_install_oauth_token_writes_fresh_block(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)

    captured_writes: list[tuple[tuple[str, ...], str]] = []

    def fake_run(argv, *a, **kw):
        if argv[:2] == ["sudo", "-u"] and "sh" in argv:
            # cat .zshenv — file doesn't exist yet.
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["sudo", "-u", "claude-agent"] and argv[3] == "tee":
            captured_writes.append((tuple(argv), kw.get("input", "")))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["sudo", "-u", "claude-agent"] and argv[3] == "chmod":
            captured_writes.append((tuple(argv), ""))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.install_oauth_token("claude-agent", "oatoken-abc  ")
    assert rc == 0

    tee_write = next(w for w in captured_writes if "tee" in w[0])
    content = tee_write[1]
    assert f"# BEGIN ap2-managed: {sandbox._LBL_OAUTH}" in content
    assert f"# END ap2-managed: {sandbox._LBL_OAUTH}" in content
    assert "export CLAUDE_CODE_OAUTH_TOKEN=oatoken-abc" in content
    # Token trimmed — no trailing whitespace bleed.
    assert "oatoken-abc  " not in content
    # chmod 600 was issued on the right file.
    chmod = next(w for w in captured_writes if "chmod" in w[0])
    assert chmod[0][-1] == str(home / ".zshenv")
    assert "600" in chmod[0]


def test_install_oauth_token_replaces_existing_block(monkeypatch, tmp_path):
    """Re-running with a new token replaces the prior block, doesn't append."""
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)

    existing = (
        "# user's existing line\n"
        "export PATH=/opt/bin:$PATH\n"
        "\n"
        f"# BEGIN ap2-managed: {sandbox._LBL_OAUTH}\n"
        "export CLAUDE_CODE_OAUTH_TOKEN=old-token\n"
        f"# END ap2-managed: {sandbox._LBL_OAUTH}\n"
        "# trailing user content\n"
        "export EDITOR=vim\n"
    )
    writes: list[str] = []

    def fake_run(argv, *a, **kw):
        if argv[:2] == ["sudo", "-u"] and "sh" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=existing, stderr="")
        if argv[:2] == ["sudo", "-u"] and "tee" in argv:
            writes.append(kw.get("input", ""))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert sandbox.install_oauth_token("claude-agent", "new-token") == 0
    content = writes[0]
    # Old token is gone, new one is present, and the user's other lines survive.
    assert "old-token" not in content
    assert "new-token" in content
    assert "export PATH=/opt/bin:$PATH" in content
    assert "export EDITOR=vim" in content
    # No duplicate block.
    assert content.count(f"# BEGIN ap2-managed: {sandbox._LBL_OAUTH}") == 1


def test_install_oauth_token_propagates_tee_failure(monkeypatch, tmp_path, capsys):
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)

    def fake_run(argv, *a, **kw):
        if "tee" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.install_oauth_token("claude-agent", "tok")
    assert rc == 1
    assert "failed to write" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# user_audit: token presence/absence

def _audit_env_stub(token_value: str = ""):
    """Build a subprocess.run stub that returns `token_value` for the token
    lookup, and empty for every other env var (a clean user).
    """
    def fake(argv, *a, **kw):
        cmdline = " ".join(argv)
        if "printenv CLAUDE_CODE_OAUTH_TOKEN" in cmdline:
            return subprocess.CompletedProcess(argv, 0, stdout=token_value, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return fake


def test_user_audit_token_set_is_ok(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(subprocess, "run", _audit_env_stub("sk-ant-oat01-xxx\n"))

    res = sandbox.user_audit("fakeuser")
    assert res.ok is True
    assert any(
        lvl == "OK" and "CLAUDE_CODE_OAUTH_TOKEN" in m
        for lvl, m in res.messages
    )


def test_user_audit_token_missing_warns_on_darwin(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(subprocess, "run", _audit_env_stub(""))

    res = sandbox.user_audit("fakeuser")
    # WARN is non-fatal — the audit still passes but the message surfaces.
    assert res.ok is True
    warns = [m for lvl, m in res.messages if lvl == "WARN"]
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in m and "Keychain" in m for m in warns)


def test_user_audit_token_missing_info_on_linux(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", _audit_env_stub(""))

    res = sandbox.user_audit("fakeuser")
    assert res.ok is True
    infos = [m for lvl, m in res.messages if lvl == "INFO"]
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in m for m in infos)


# ---------------------------------------------------------------------------
# sentinel-block writer — pure string op

def test_replace_sentinel_block_appends_when_absent():
    out = sandbox._replace_sentinel_block("existing line\n", "X", "export FOO=1")
    assert "existing line" in out
    assert "# BEGIN ap2-managed: X" in out
    assert "export FOO=1" in out
    assert "# END ap2-managed: X" in out


def test_replace_sentinel_block_replaces_in_place():
    existing = (
        "line A\n"
        "# BEGIN ap2-managed: X\n"
        "export FOO=old\n"
        "# END ap2-managed: X\n"
        "line B\n"
    )
    out = sandbox._replace_sentinel_block(existing, "X", "export FOO=new")
    assert out.count("# BEGIN ap2-managed: X") == 1
    assert "export FOO=new" in out
    assert "export FOO=old" not in out
    assert "line A" in out and "line B" in out


def test_replace_sentinel_block_multiple_labels_coexist():
    """Different labels shouldn't interfere with each other."""
    existing = (
        "# BEGIN ap2-managed: A\n"
        "export A_VAR=1\n"
        "# END ap2-managed: A\n"
    )
    out = sandbox._replace_sentinel_block(existing, "B", "export B_VAR=2")
    assert "A_VAR=1" in out
    assert "B_VAR=2" in out
    assert out.count("# BEGIN ap2-managed:") == 2


# ---------------------------------------------------------------------------
# install_mm_credentials + install_project_channel

def test_install_mm_credentials_writes_both_vars(monkeypatch, tmp_path):
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(sandbox, "_user_home", lambda u: home)
    writes: list[str] = []

    def fake_run(argv, *a, **kw):
        if argv[:2] == ["sudo", "-u"] and "sh" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["sudo", "-u"] and "tee" in argv:
            writes.append(kw.get("input", ""))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.install_mm_credentials(
        "claude-agent", "https://mm.example.com/", "tok-abc",
    )
    assert rc == 0
    content = writes[0]
    assert "# BEGIN ap2-managed: mattermost-credentials" in content
    assert "export MATTERMOST_URL=https://mm.example.com" in content  # trailing / stripped
    assert "export MATTERMOST_TOKEN=tok-abc" in content


def test_install_mm_credentials_rejects_empty(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    assert sandbox.install_mm_credentials("claude-agent", "", "tok") == 1
    assert sandbox.install_mm_credentials("claude-agent", "http://x", "") == 1


def test_install_project_channel_writes_to_project_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    writes: list[tuple[tuple, str]] = []

    def fake_run(argv, *a, **kw):
        if argv[:2] == ["sudo", "-u"] and "sh" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["sudo", "-u"] and "tee" in argv:
            writes.append((tuple(argv), kw.get("input", "")))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = sandbox.install_project_channel(
        tmp_path, "claude-agent", "chan-id-123", channel_name="stoch",
    )
    assert rc == 0
    tee_argv, content = writes[0]
    assert tee_argv[-1] == str(tmp_path / ".cc-autopilot" / "env")
    assert "AP2_MM_CHANNELS=chan-id-123" in content
    assert "# channel name: #stoch" in content


# ---------------------------------------------------------------------------
# resolve_mm_channel — API helper

def test_resolve_mm_channel_auto_discovers_team(monkeypatch):
    calls: list[str] = []

    def fake_api(url, token, path):
        calls.append(path)
        if path == "/api/v4/users/me/teams":
            return [{"id": "team-1", "name": "primary"}]
        if path == "/api/v4/teams/team-1/channels/name/stoch":
            return {"id": "chan-42", "name": "stoch"}
        raise AssertionError(f"unexpected path: {path}")
    monkeypatch.setattr(sandbox, "_mm_api_get", fake_api)

    ch_id, team_id = sandbox.resolve_mm_channel("http://mm", "tok", "#stoch")
    assert ch_id == "chan-42"
    assert team_id == "team-1"
    assert calls[0] == "/api/v4/users/me/teams"


def test_resolve_mm_channel_honors_explicit_team(monkeypatch):
    def fake_api(url, token, path):
        assert path == "/api/v4/teams/t-forced/channels/name/foo"
        return {"id": "c-x", "name": "foo"}
    monkeypatch.setattr(sandbox, "_mm_api_get", fake_api)

    ch_id, team_id = sandbox.resolve_mm_channel(
        "http://mm", "tok", "foo", team_id="t-forced",
    )
    assert (ch_id, team_id) == ("c-x", "t-forced")


def test_resolve_mm_channel_raises_on_empty_name(monkeypatch):
    monkeypatch.setattr(sandbox, "_mm_api_get",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not call")))
    with pytest.raises(ValueError):
        sandbox.resolve_mm_channel("http://mm", "tok", "   ")


def test_resolve_mm_channel_raises_when_no_teams(monkeypatch):
    monkeypatch.setattr(sandbox, "_mm_api_get", lambda *a, **kw: [])
    with pytest.raises(RuntimeError):
        sandbox.resolve_mm_channel("http://mm", "tok", "stoch")
