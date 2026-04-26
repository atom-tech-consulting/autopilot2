"""Tests for `ap2 doctor` — environment-readiness check.

Exercises only the parts that don't require a real sandbox user (the project
skeleton check). Sandbox-user audits are covered separately in test_sandbox.
"""
from __future__ import annotations

from pathlib import Path

from ap2.doctor import _project_init_state
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
