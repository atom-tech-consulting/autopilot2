"""Tests for `ap2 doctor` — environment-readiness check.

Exercises only the parts that don't require a real sandbox user (the project
skeleton check). Sandbox-user audits are covered separately in test_sandbox.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ap2 import doctor as doctor_mod
from ap2 import sandbox
from ap2.doctor import _ap2_installed_for_user, _project_init_state
from ap2.init import init_project


def test_project_init_state_fails_on_empty_dir(tmp_path: Path):
    res = _project_init_state(tmp_path)
    assert not res.ok
    msg = " ".join(t for _, t in res.messages)
    for expected in ("TASKS.md", ".cc-autopilot/progress.md", "CLAUDE.md"):
        assert expected in msg
    assert "ap2 init" in msg  # actionable next step


def test_project_init_state_passes_after_init(tmp_path: Path):
    init_project(tmp_path)
    res = _project_init_state(tmp_path)
    assert res.ok, [m for m in res.messages if m[0] == "FAIL"]


def test_project_init_state_flags_claude_md_without_autopilot(tmp_path: Path):
    """If someone has CLAUDE.md but not the Autopilot section, doctor must
    flag it — Config.load can run but the daemon won't find the right paths
    once they're customized.
    """
    init_project(tmp_path)
    # Strip the Autopilot section.
    claude_md = tmp_path / "CLAUDE.md"
    text = claude_md.read_text().replace("## Autopilot", "## NotAutopilot")
    claude_md.write_text(text)

    res = _project_init_state(tmp_path)
    assert not res.ok
    assert any("`## Autopilot`" in t for _, t in res.messages)


def test_ap2_installed_probes_via_user_login_shell(monkeypatch):
    """TB-124: the ap2-on-PATH probe must use the user's pw_shell.

    `uv tool install` puts ~/.local/bin on PATH via ~/.zshenv for zsh
    users; a hard-coded bash probe would miss that and report a false
    `ap2 not on $PATH` FAIL.
    """
    monkeypatch.setattr(sandbox, "_user_exists", lambda u: True)
    monkeypatch.setattr(doctor_mod, "_user_exists", lambda u: True)
    monkeypatch.setattr(doctor_mod, "_user_login_shell", lambda u: "/bin/zsh")

    seen_argvs: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        seen_argvs.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, stdout="/Users/fakeuser/.local/bin/ap2\n", stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)

    res = _ap2_installed_for_user("fakeuser")
    assert res.ok, res.messages
    # Single subprocess.run call: sudo -u fakeuser -i /bin/zsh -c '...'.
    assert len(seen_argvs) == 1
    argv = seen_argvs[0]
    assert argv[:5] == ["sudo", "-u", "fakeuser", "-i", "/bin/zsh"]
    assert "bash" not in argv


def test_ap2_installed_falls_back_when_user_missing(monkeypatch):
    """If the user doesn't exist, the probe should short-circuit before
    invoking sudo — exercises the early-return path."""
    monkeypatch.setattr(doctor_mod, "_user_exists", lambda u: False)

    def boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called")
    monkeypatch.setattr(subprocess, "run", boom)

    res = _ap2_installed_for_user("ghost")
    assert not res.ok
    assert any("does not exist" in m for _, m in res.messages)
