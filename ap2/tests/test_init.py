"""Tests for `ap2 init` scaffolding (ap2/init.py).

The scaffolding is the only deterministic source of truth for what an
ap2-managed project ignores vs. tracks. Drift here is what stranded stoch's
`cron.yaml` for weeks and let `*.lock` files leak into the working tree.
"""
from __future__ import annotations

from pathlib import Path

from ap2.init import (
    NESTED_GITIGNORE_BLOCKS,
    ROOT_GITIGNORE_BLOCKS,
    init_project,
)


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def test_creates_files_in_empty_project(tmp_path: Path):
    report = init_project(tmp_path)

    nested = tmp_path / ".cc-autopilot" / ".gitignore"
    root = tmp_path / ".gitignore"
    tasks = tmp_path / ".cc-autopilot" / "tasks"

    assert nested.exists()
    assert root.exists()
    assert tasks.is_dir()
    assert report.tasks_dir_created is True

    # Every entry from each block landed in the right file.
    nested_text = nested.read_text()
    for _, entries in NESTED_GITIGNORE_BLOCKS:
        for e in entries:
            assert e in nested_text, f"missing {e!r} in nested gitignore"

    root_text = root.read_text()
    for _, entries in ROOT_GITIGNORE_BLOCKS:
        for e in entries:
            assert e in root_text, f"missing {e!r} in root gitignore"


def test_load_bearing_entries_present(tmp_path: Path):
    """Pin the entries whose absence caused real bugs in stoch."""
    init_project(tmp_path)
    nested = (tmp_path / ".cc-autopilot" / ".gitignore").read_text()
    root = (tmp_path / ".gitignore").read_text()

    # Secrets must never end up tracked.
    assert "env" in nested
    # Lock files (cron_state.json.lock, retry_state.json.lock) leak otherwise.
    assert "*.lock" in nested
    # On-disk backups created during ap2 upgrades.
    assert "*.bak" in nested
    # Board lock at project root, NOT under .cc-autopilot/.
    assert "TASKS.md.lock" in root


def test_idempotent_no_duplicates_on_rerun(tmp_path: Path):
    init_project(tmp_path)
    nested = tmp_path / ".cc-autopilot" / ".gitignore"
    root = tmp_path / ".gitignore"
    nested_first = nested.read_text()
    root_first = root.read_text()

    report2 = init_project(tmp_path)

    # Second run reports nothing added and writes nothing new.
    assert report2.nested_gitignore_added == []
    assert report2.root_gitignore_added == []
    assert report2.tasks_dir_created is False
    assert nested.read_text() == nested_first
    assert root.read_text() == root_first


def test_unions_with_existing_gitignore(tmp_path: Path):
    """Pre-existing entries are preserved; only missing ones are appended."""
    autopilot = tmp_path / ".cc-autopilot"
    autopilot.mkdir()
    nested = autopilot / ".gitignore"
    # Existing user content + one of our entries already.
    nested.write_text("# user-managed\nmy_local_thing/\nevents.jsonl\n")

    report = init_project(tmp_path)

    text = nested.read_text()
    # User content untouched.
    assert "# user-managed" in text
    assert "my_local_thing/" in text
    # The one of our entries that was already there isn't duplicated.
    assert text.count("events.jsonl") == 1
    # Entries we added are new arrivals, not the pre-existing one.
    assert "events.jsonl" not in report.nested_gitignore_added
    assert "*.lock" in report.nested_gitignore_added


def test_does_not_clobber_root_gitignore_entries(tmp_path: Path):
    """Project's own root .gitignore (e.g. .env, build/) must survive."""
    root = tmp_path / ".gitignore"
    root.write_text(".env\n.venv/\nbuild/\n")

    init_project(tmp_path)

    text = root.read_text()
    for keep in (".env", ".venv/", "build/", "TASKS.md.lock"):
        assert keep in text


def test_existing_tasks_dir_not_clobbered(tmp_path: Path):
    """Briefings already on disk must not be touched."""
    tasks = tmp_path / ".cc-autopilot" / "tasks"
    tasks.mkdir(parents=True)
    brief = tasks / "old-briefing.md"
    brief.write_text("# old briefing")

    report = init_project(tmp_path)

    assert report.tasks_dir_created is False
    assert brief.read_text() == "# old briefing"


def test_partial_state_only_appends_missing(tmp_path: Path):
    """If init had been run before but the template was extended later
    (e.g. we added *.lock and *.bak in TB-68), re-running picks up just the
    new entries — no header churn for blocks that are fully present.
    """
    autopilot = tmp_path / ".cc-autopilot"
    autopilot.mkdir()
    nested = autopilot / ".gitignore"
    # Simulate the pre-TB-68 template: full Runtime block, full debug block,
    # full env block, but missing *.lock and *.bak.
    pre = "\n".join(
        ["# Runtime — per-user, not committed"]
        + NESTED_GITIGNORE_BLOCKS[0][1]
        + ["", "# Per-run prompt + stream dumps for failure diagnosis (kept only on failure)"]
        + NESTED_GITIGNORE_BLOCKS[1][1]
        + ["", "# Local/sandbox-specific env (secrets, channel IDs) — keep out of git"]
        + NESTED_GITIGNORE_BLOCKS[2][1]
    )
    nested.write_text(pre + "\n")

    report = init_project(tmp_path)

    assert "*.lock" in report.nested_gitignore_added
    assert "*.bak" in report.nested_gitignore_added
    # Already-present entries don't show up as added.
    assert "events.jsonl" not in report.nested_gitignore_added
