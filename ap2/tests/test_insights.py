"""Tests for `ap2.insights` — the project-output read hook (TB-89).

Pins the lazy-regen behavior (no I/O when nothing changed), the front-matter
parser's tolerance for malformed files, the placeholder rendering, and
edge cases (empty dir, file added/removed/touched).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ap2 import insights
from ap2.config import Config


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    insights.insights_dir(cfg).mkdir(parents=True, exist_ok=True)
    return cfg


def _write_insight(cfg: Config, name: str, *,
                   tldr: str = "Sample insight summary",
                   updated: str = "2026-04-27T15:30:00Z",
                   updated_by: str = "TB-89",
                   cites: list[str] | None = None) -> Path:
    cites_str = "[" + ", ".join(cites or ["TB-1"]) + "]"
    body = (
        "---\n"
        f"tldr: {tldr}\n"
        f"updated: {updated}\n"
        f"updated_by: {updated_by}\n"
        f"cites: {cites_str}\n"
        "---\n\n"
        f"# {name}\n\n"
        "<full body>\n"
    )
    p = insights.insights_dir(cfg) / name
    p.write_text(body)
    return p


# ---------------- front matter parser ----------------


def test_parse_front_matter_basic():
    fm = insights._parse_front_matter(
        "---\n"
        "tldr: hello\n"
        "updated: 2026-04-27T15:30:00Z\n"
        "updated_by: TB-89\n"
        "cites: [TB-1, TB-2, TB-3]\n"
        "---\n\n"
        "# topic\n"
    )
    assert fm["tldr"] == "hello"
    assert fm["updated_by"] == "TB-89"
    assert fm["cites"] == ["TB-1", "TB-2", "TB-3"]


def test_parse_front_matter_missing_block_returns_empty():
    assert insights._parse_front_matter("# no front matter\n\nbody") == {}


def test_parse_front_matter_unterminated_returns_empty():
    assert insights._parse_front_matter("---\ntldr: oops\n# never closed\n") == {}


def test_parse_front_matter_quoted_values():
    fm = insights._parse_front_matter(
        '---\ntldr: "hello: with colon"\nupdated_by: \'TB-7\'\n---\n\nbody\n'
    )
    assert fm["tldr"] == "hello: with colon"
    assert fm["updated_by"] == "TB-7"


# ---------------- regenerate_index ----------------


def test_regenerate_writes_placeholder_for_empty_dir(tmp_path):
    cfg = _cfg(tmp_path)
    assert insights.regenerate_index(cfg) is True
    text = insights.index_path(cfg).read_text()
    assert "Insights index" in text
    assert "no insights yet" in text


def test_regenerate_writes_one_line_per_insight(tmp_path):
    cfg = _cfg(tmp_path)
    _write_insight(cfg, "alpha.md", tldr="Alpha summary",
                   updated="2026-04-27T10:00:00Z", updated_by="TB-89",
                   cites=["TB-1", "TB-2"])
    _write_insight(cfg, "beta.md", tldr="Beta summary",
                   updated="2026-04-27T15:00:00Z", updated_by="operator",
                   cites=["TB-3"])
    assert insights.regenerate_index(cfg) is True
    text = insights.index_path(cfg).read_text()
    # Newest first.
    assert text.index("beta.md") < text.index("alpha.md")
    assert "Alpha summary" in text and "Beta summary" in text
    # Citation list rendered.
    assert "TB-1/TB-2" in text and "TB-3" in text


def test_regenerate_handles_malformed_front_matter(tmp_path):
    cfg = _cfg(tmp_path)
    bad = insights.insights_dir(cfg) / "broken.md"
    bad.write_text("# no front matter at all\n\njust prose\n")
    assert insights.regenerate_index(cfg) is True
    text = insights.index_path(cfg).read_text()
    assert "broken.md" in text
    # Placeholder used when tldr is missing — keeps the index from crashing.
    assert "(no tldr — needs update)" in text


# ---------------- maybe_regenerate_index (lazy) ----------------


def test_maybe_regenerates_on_first_run(tmp_path):
    cfg = _cfg(tmp_path)
    _write_insight(cfg, "alpha.md")
    assert insights.maybe_regenerate_index(cfg) is True


def test_maybe_does_not_regenerate_when_nothing_changed(tmp_path):
    cfg = _cfg(tmp_path)
    _write_insight(cfg, "alpha.md")
    insights.regenerate_index(cfg)
    # Force the index's mtime forward so the dir mtime is older.
    index = insights.index_path(cfg)
    future = time.time() + 60
    os.utime(index, (future, future))
    assert insights.maybe_regenerate_index(cfg) is False


def test_maybe_regenerates_when_file_touched(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write_insight(cfg, "alpha.md")
    insights.regenerate_index(cfg)
    # Bump the file's mtime past the index's.
    later = time.time() + 30
    os.utime(f, (later, later))
    assert insights.maybe_regenerate_index(cfg) is True


def test_maybe_regenerates_when_file_added(tmp_path):
    cfg = _cfg(tmp_path)
    _write_insight(cfg, "alpha.md")
    insights.regenerate_index(cfg)
    # Adding a file bumps the dir mtime on Unix.
    later = time.time() + 30
    os.utime(insights.insights_dir(cfg), (later, later))
    _write_insight(cfg, "beta.md")
    assert insights.maybe_regenerate_index(cfg) is True
    text = insights.index_path(cfg).read_text()
    assert "beta.md" in text


def test_maybe_regenerates_when_file_removed(tmp_path):
    cfg = _cfg(tmp_path)
    _write_insight(cfg, "alpha.md")
    f = _write_insight(cfg, "beta.md")
    insights.regenerate_index(cfg)
    f.unlink()
    # Bump dir mtime to simulate filesystem updating it on remove.
    later = time.time() + 30
    os.utime(insights.insights_dir(cfg), (later, later))
    assert insights.maybe_regenerate_index(cfg) is True
    text = insights.index_path(cfg).read_text()
    assert "beta.md" not in text


def test_maybe_returns_false_when_dir_does_not_exist(tmp_path):
    cfg = _cfg(tmp_path)
    insights.insights_dir(cfg).rmdir()
    assert insights.maybe_regenerate_index(cfg) is False
