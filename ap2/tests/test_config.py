"""Tests for ap2.config.load_project_env."""
from __future__ import annotations

import os

from ap2.config import load_project_env


def test_load_project_env_applies_unset_keys(tmp_path, monkeypatch):
    (tmp_path / ".cc-autopilot").mkdir()
    (tmp_path / ".cc-autopilot" / "env").write_text(
        "AP2_MM_CHANNELS=abc123,def456\nAP2_TICK_S=15\n"
    )
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_TICK_S", raising=False)

    applied = load_project_env(tmp_path)

    assert applied == {"AP2_MM_CHANNELS": "abc123,def456", "AP2_TICK_S": "15"}
    assert os.environ["AP2_MM_CHANNELS"] == "abc123,def456"
    assert os.environ["AP2_TICK_S"] == "15"


def test_load_project_env_does_not_override_existing(tmp_path, monkeypatch):
    (tmp_path / ".cc-autopilot").mkdir()
    (tmp_path / ".cc-autopilot" / "env").write_text("AP2_TICK_S=15\n")
    monkeypatch.setenv("AP2_TICK_S", "99")

    applied = load_project_env(tmp_path)

    assert applied == {}
    assert os.environ["AP2_TICK_S"] == "99"


def test_load_project_env_handles_quotes_comments_blanks(tmp_path, monkeypatch):
    (tmp_path / ".cc-autopilot").mkdir()
    (tmp_path / ".cc-autopilot" / "env").write_text(
        "# channel list (comment)\n"
        "\n"
        'AP2_MM_CHANNELS="abc123,def456"\n'
        "AP2_MM_MENTION='@claude-bot'\n"
        "BAD_LINE_WITHOUT_EQUALS\n"
    )
    for k in ("AP2_MM_CHANNELS", "AP2_MM_MENTION"):
        monkeypatch.delenv(k, raising=False)

    applied = load_project_env(tmp_path)

    assert applied["AP2_MM_CHANNELS"] == "abc123,def456"
    assert applied["AP2_MM_MENTION"] == "@claude-bot"


def test_load_project_env_missing_file_returns_empty(tmp_path):
    assert load_project_env(tmp_path) == {}


# TB-102: the `## Autopilot` section regex must tolerate trailing content on
# the heading (parenthetical, em-dash, etc.) — same brittleness pattern that
# bit `## Verification (launch-task — ...)` in TB-91/TB-146.


def test_read_autopilot_section_tolerates_parenthetical_heading(tmp_path):
    """A trailing `(per-project)` or similar disambiguator must not stop the
    Autopilot block from parsing. Pre-TB-102, `\\s*$` rejected anything but
    bare whitespace after `## Autopilot`."""
    from ap2.config import Config

    (tmp_path / "TASKS.md").write_text("# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n")
    (tmp_path / "CLAUDE.md").write_text(
        "# Project\n\n"
        "## Autopilot (per-project)\n\n"
        "- Task list: `TASKS.md`\n"
        "- Next task ID: TB-77\n\n"
        "## Other section\n\nbody\n"
    )
    cfg = Config.load(tmp_path)
    assert cfg.next_task_id == 77


def test_autopilot_header_re_rejects_lookalikes(tmp_path):
    """`## AutopilotPlus` must NOT match — `\\b` word-boundary keeps lookalikes
    out so a future doc heading doesn't collide with the anchor."""
    from ap2.init import _AUTOPILOT_HEADER_RE

    assert _AUTOPILOT_HEADER_RE.search("## Autopilot\n") is not None
    assert _AUTOPILOT_HEADER_RE.search("## Autopilot — config\n") is not None
    assert _AUTOPILOT_HEADER_RE.search("## Autopilot (per-project)\n") is not None
    assert _AUTOPILOT_HEADER_RE.search("## AutopilotPlus\n") is None
    assert _AUTOPILOT_HEADER_RE.search("## Autopilots\n") is None
