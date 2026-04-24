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
