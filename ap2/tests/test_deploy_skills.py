"""Tests for `scripts/deploy-skills.sh` + `ap2 sandbox sync-skills` (TB-140).

The deploy script syncs `<repo>/skills/*` into `$HOME/.claude/skills/`
so `/ap2`, `/ap2-task`, and `/migrate-to-ap2` (live slash commands read
from the deployed copy) stay current with repo edits. The CLI
subcommand `ap2 sandbox sync-skills` is a thin wrapper.

These tests cover:
  - the script exists and is executable
  - dry-run (default) prints a per-skill diff summary and DOES NOT
    mutate the destination
  - `--apply` produces an exact mirror of the source
  - the CLI entrypoint resolves and exposes the same flags
  - the CLI delegates to the bash script (smoke check that
    `--dest <tmp>` actually writes there)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ap2 import cli, sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy-skills.sh"
SKILLS_SRC = REPO_ROOT / "skills"


# ---------------------------------------------------------------------------
# script presence + permissions

def test_deploy_script_exists_and_is_executable():
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    # Must be executable for the CLI wrapper (and direct invocation) to work.
    import os
    assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


def test_deploy_script_starts_with_bash_shebang():
    head = SCRIPT.read_text().splitlines()[0]
    assert head.startswith("#!"), head
    assert "bash" in head


def test_skills_source_dir_has_expected_subdirs():
    # Sanity check: the skills the script syncs must actually exist on disk
    # (so a run doesn't silently no-op).
    assert (SKILLS_SRC / "ap2").is_dir()
    assert (SKILLS_SRC / "ap2-task").is_dir()
    assert (SKILLS_SRC / "migrate-to-ap2").is_dir()


# ---------------------------------------------------------------------------
# dry-run behavior

def test_dry_run_does_not_mutate_destination(tmp_path):
    """No-args dry-run prints a summary; nothing is written under --dest."""
    dest = tmp_path / "claude-skills"
    # Pre-create dest with an unrelated sentinel file we can verify survives.
    dest.mkdir()
    sentinel = dest / "untouched-sibling-skill"
    sentinel.mkdir()
    (sentinel / "SKILL.md").write_text("not part of the repo\n")

    res = subprocess.run(
        [str(SCRIPT), "--dest", str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    # Per-skill drift summary was printed.
    assert "ap2:" in res.stdout
    assert "ap2-task:" in res.stdout
    assert "migrate-to-ap2:" in res.stdout
    assert "dry-run" in res.stdout
    # No skill was created in dest.
    assert not (dest / "ap2" / "SKILL.md").exists()
    assert not (dest / "ap2-task" / "SKILL.md").exists()
    # Sibling skill (taskboard, etc.) was not touched.
    assert sentinel.is_dir()
    assert (sentinel / "SKILL.md").read_text() == "not part of the repo\n"


def test_dry_run_in_sync_when_dest_already_matches(tmp_path):
    """Re-running dry-run after --apply reports zero drift."""
    dest = tmp_path / "claude-skills"
    # Apply once.
    apply = subprocess.run(
        [str(SCRIPT), "--apply", "--dest", str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert apply.returncode == 0, apply.stderr
    # Dry-run after apply reports zero drift.
    dry = subprocess.run(
        [str(SCRIPT), "--dest", str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "in sync" in dry.stdout
    assert "drift" not in dry.stdout
    assert "all skills in sync" in dry.stdout


# ---------------------------------------------------------------------------
# --apply behavior

def test_apply_produces_exact_mirror_of_source(tmp_path):
    """Verification spec: --apply against a fresh dest should match `diff -r`."""
    dest = tmp_path / "claude-skills"
    res = subprocess.run(
        [str(SCRIPT), "--apply", "--dest", str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr

    # `diff -r` exits 0 iff the two trees are identical.
    for skill in ("ap2", "ap2-task", "migrate-to-ap2"):
        diff = subprocess.run(
            ["diff", "-r", str(SKILLS_SRC / skill), str(dest / skill)],
            capture_output=True, text=True, check=False,
        )
        assert diff.returncode == 0, (
            f"diff -r mismatch for {skill}:\n"
            f"stdout:\n{diff.stdout}\nstderr:\n{diff.stderr}"
        )


def test_apply_is_idempotent(tmp_path):
    """Two consecutive --apply runs leave dest identical to source."""
    dest = tmp_path / "claude-skills"
    for _ in range(2):
        res = subprocess.run(
            [str(SCRIPT), "--apply", "--dest", str(dest)],
            capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0, res.stderr
    diff = subprocess.run(
        ["diff", "-r", str(SKILLS_SRC), str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert diff.returncode == 0, diff.stdout


def test_apply_deletes_files_no_longer_in_source(tmp_path):
    """Per-skill rsync --delete: stale files in dest get removed on --apply.

    This is the bug we're guarding against: if a skill's filename changes
    in the repo (or a file is deleted), the deployed copy must drop the
    old file. Otherwise the live slash command keeps reading stale
    content alongside the new.
    """
    dest = tmp_path / "claude-skills"
    # Initial apply.
    subprocess.run(
        [str(SCRIPT), "--apply", "--dest", str(dest)],
        capture_output=True, text=True, check=True,
    )
    # Plant a stale file in the dest copy of `ap2/` that has no source-side
    # counterpart. After re-applying, it must be gone.
    stale = dest / "ap2" / "stale-leftover.md"
    stale.write_text("an old file from a renamed skill\n")
    assert stale.exists()
    # Re-apply.
    subprocess.run(
        [str(SCRIPT), "--apply", "--dest", str(dest)],
        capture_output=True, text=True, check=True,
    )
    assert not stale.exists()


def test_apply_does_not_touch_unrelated_dest_skills(tmp_path):
    """Sibling skills the repo doesn't own (e.g. `taskboard`) are preserved.

    The script does per-skill `rsync --delete` against `dest/<name>/`, NOT
    against `dest/` itself. This is load-bearing — the live operator
    machine has a `taskboard` skill outside this repo, and an over-broad
    `--delete` would wipe it.
    """
    dest = tmp_path / "claude-skills"
    dest.mkdir()
    sibling = dest / "taskboard"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text("global skill, not in this repo\n")
    subprocess.run(
        [str(SCRIPT), "--apply", "--dest", str(dest)],
        capture_output=True, text=True, check=True,
    )
    assert sibling.is_dir()
    assert (sibling / "SKILL.md").read_text() == "global skill, not in this repo\n"


def test_apply_short_summary_includes_synced_keyword(tmp_path):
    """A successful --apply prints `synced (...)` for skills that updated.

    This is what an operator sees in the terminal after `ap2 sandbox
    sync-skills --apply` — make sure the line stays human-grepable.
    """
    dest = tmp_path / "claude-skills"
    res = subprocess.run(
        [str(SCRIPT), "--apply", "--dest", str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    assert "synced" in res.stdout
    assert "apply complete" in res.stdout


# ---------------------------------------------------------------------------
# CLI surface — `ap2 sandbox sync-skills`

def test_cli_sandbox_sync_skills_subcommand_registered():
    """The CLI parser exposes `sandbox sync-skills` with --apply / --dest."""
    parser = cli.build_parser()
    # Smoke: argparse accepts the full invocation.
    ns = parser.parse_args(["sandbox", "sync-skills", "--apply", "--dest", "/tmp/x"])
    assert ns.apply is True
    assert ns.dest == "/tmp/x"
    assert ns.func is sandbox.cmd_sync_skills


def test_cli_sandbox_sync_skills_dry_run_is_default():
    """Without --apply, the parser leaves apply=False."""
    parser = cli.build_parser()
    ns = parser.parse_args(["sandbox", "sync-skills"])
    assert ns.apply is False
    assert ns.dest is None


def test_sync_skills_helper_returns_script_exit_code(tmp_path, monkeypatch):
    """`sandbox.sync_skills` returns the bash script's exit code verbatim."""
    # Real run against a temp dest — should succeed.
    dest = tmp_path / "claude-skills"
    rc = sandbox.sync_skills(apply=True, dest=dest)
    assert rc == 0
    # Verify it actually wrote.
    assert (dest / "ap2" / "SKILL.md").exists()
    assert (dest / "ap2-task" / "SKILL.md").exists()


def test_sync_skills_helper_missing_script_returns_nonzero(monkeypatch, capsys):
    """If the bundled script vanishes, the helper reports + exits nonzero."""
    monkeypatch.setattr(
        sandbox, "_deploy_script_source",
        lambda: Path("/nonexistent/deploy-skills.sh"),
    )
    rc = sandbox.sync_skills(apply=False)
    assert rc == 1
    err = capsys.readouterr().err
    assert "script missing" in err


def test_cmd_sync_skills_glue_dispatches(tmp_path):
    """`cmd_sync_skills` routes its argparse Namespace into `sync_skills`."""
    parser = cli.build_parser()
    dest = tmp_path / "claude-skills"
    ns = parser.parse_args([
        "sandbox", "sync-skills", "--apply", "--dest", str(dest),
    ])
    rc = ns.func(None, ns)
    assert rc == 0
    assert (dest / "ap2" / "SKILL.md").exists()
