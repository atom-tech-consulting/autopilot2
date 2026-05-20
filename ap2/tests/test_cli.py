"""Tests for the ap2 CLI top-level wiring (TB-77, TB-79).

Most of the verb-specific coverage moved out under TB-266 to mirror
the TB-264 source split:

- `cmd_start` / `cmd_stop` / `cmd_status` / `cmd_pause` / `cmd_resume`
  / `cmd_web`  → `test_cli_daemon.py`
- `cmd_add` / `cmd_update` / `cmd_backlog` / `cmd_unfreeze` /
  `cmd_delete` / `cmd_reject` / `cmd_approve` / `cmd_classify`
  → `test_cli_board.py`
- `cmd_audit` / `cmd_ack` / `cmd_rollback` / `cmd_ideate` /
  `cmd_update_goal` / `cmd_backfill_proposals`
  → `test_cli_review.py`
- `cmd_doctor` / `cmd_check` / `cmd_logs` / `cmd_cron_list` /
  `cmd_cron_edit` / `cmd_init`
  → `test_cli_diagnostic.py`

What remains here is the small set of CLI tests that are NOT tied to a
single verb group — currently the TB-139 `--version` helper-level pins
(`_git_suffix`, `get_version`, `_version_string`). The corresponding
`ap2 status` version-line tests went with the rest of `cmd_status` to
`test_cli_daemon.py`; the `daemon_start` event pin went there too
because it pairs directly with the `ap2 start` flow.
"""
from __future__ import annotations

import re as _re
import subprocess as _sp
from pathlib import Path


# ---------------------------------------------------------------------------
# TB-139: ap2 --version embeds source-commit timestamp on editable installs
# so an operator can confirm freshness without falling back to `git log`.
# Format: `ap2 <base>(+<sha>.<ts>)?` per the briefing's pinned regex.

_VERSION_RE = _re.compile(r"^ap2 0\.\d+\.\d+(\+[a-f0-9]{7,}\.\d{8}T\d{6}Z)?$")


def _git_init_with_one_commit(path: Path) -> None:
    """Bootstrap a minimal git repo at `path` with one commit so
    `git log -1` has something to return. Used by the tests below to
    exercise the editable-install code path without touching the real
    autopilot2 checkout."""
    _sp.run(["git", "init", "-q", str(path)], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README").write_text("hi\n")
    _sp.run(["git", "-C", str(path), "add", "README"], check=True)
    _sp.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
    )


def test_git_suffix_in_a_real_git_repo_returns_sha_and_ts(tmp_path: Path):
    """Editable-install path: `_git_suffix(repo_root)` against a checkout
    with at least one commit returns `<sha>.<timestamp>` matching the
    pinned format. The leading `+` is added by `get_version()`, not by
    `_git_suffix()`; here we just pin the inner shape."""
    from ap2 import _git_suffix

    _git_init_with_one_commit(tmp_path)
    suffix = _git_suffix(tmp_path)

    # Non-empty + matches `<7+ hex>.<YYYYMMDDTHHMMSSZ>`.
    assert suffix
    assert _re.match(r"^[a-f0-9]{7,}\.\d{8}T\d{6}Z$", suffix), suffix


def test_git_suffix_outside_a_git_repo_is_empty(tmp_path: Path):
    """Released-wheel path: `_git_suffix(repo_root)` on a directory with
    no `.git/` returns `""`. `get_version()` then prints just the base
    version, no `+suffix` — which is what we want for installs that
    don't have source-commit info to expose."""
    from ap2 import _git_suffix

    # tmp_path is freshly created — no `.git/` subdir.
    assert _git_suffix(tmp_path) == ""


def test_get_version_format_matches_pinned_regex():
    """End-to-end on the package's own checkout: `ap2 <get_version()>`
    matches the regex pinned in TB-139's briefing. Verifies the actual
    string operators see when they run `ap2 --version` is shaped the way
    downstream tooling expects (e.g. a sed/awk script that wants to
    extract the SHA)."""
    from ap2 import get_version

    rendered = f"ap2 {get_version()}"
    assert _VERSION_RE.match(rendered), rendered


def test_cli_version_string_matches_get_version():
    """Parity: the string the CLI prints is exactly the canonical
    accessor's output. Pins the daemon_start event field and the
    `ap2 status` line to the same source-of-truth as `ap2 --version`,
    so an operator post-mortem isn't comparing three slightly-different
    formats."""
    from ap2 import get_version
    from ap2.cli import _version_string

    assert _version_string() == get_version()
