"""TB-267: insights route group tests — mirror of `ap2/web_insights.py`.

Relocated from `ap2/tests/test_web.py` by the TB-267 split. Each test body
is byte-identical to its pre-TB-267 original; only the module home and the
shared `project` fixture's location (now `ap2/tests/conftest.py`) changed.

Covers pages owned by `ap2/web_insights.py`:
  - `/insights` — `_render_insights` (index listing).
  - `/insight/<name>` — `_render_insight` (per-file viewer + path-traversal guard).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import web
from ap2.config import Config


def test_insights_shows_files(project: Config, tmp_path):
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "alpha.md").write_text(
        "---\n"
        "tldr: Alpha decay observed in regime X\n"
        "updated: 2026-04-28T10:00:00Z\n"
        "updated_by: TB-50\n"
        "cites: [TB-49, TB-50]\n"
        "---\n\n"
        "Body content.\n"
    )
    html = web._render_insights(project)
    assert "alpha.md" in html
    assert "Alpha decay observed in regime X" in html
    assert "TB-50" in html


def test_insights_empty_state(project: Config):
    html = web._render_insights(project)
    assert "no insights dir" in html or "empty" in html


def test_insight_detail_shows_full_content(project: Config, tmp_path):
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    body = "---\ntldr: x\nupdated: 2026-04-28\nupdated_by: op\ncites: []\n---\n# Header\n\nBody.\n"
    (insights_dir / "alpha.md").write_text(body)
    html = web._render_insight(project, "alpha.md")
    assert "Header" in html
    assert "Body." in html


def test_insight_404(project: Config):
    html = web._render_insight(project, "nonexistent.md")
    assert "not found" in html


def test_insight_blocks_path_traversal(project: Config):
    html = web._render_insight(project, "../../../../etc/passwd")
    assert "invalid" in html.lower()
