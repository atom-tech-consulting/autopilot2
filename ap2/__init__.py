"""Autopilot v2 — external daemon architecture built on the Claude Agent SDK.

See plan/autopilot-v2.md for the design.

Version string (TB-139): the canonical accessor is :func:`get_version`. It
returns the installed base version (`autopilot2` PyPI metadata, normally
`X.Y.Z`) plus, when the package is loaded from an editable checkout, a
PEP 440-style local-version suffix `+<short-sha>.<commit-ts>`. With editable
installs the base version stays pinned across many source-tree changes, so
operators previously had no in-CLI way to confirm which commit the running
daemon was actually loading. The suffix lets a single `ap2 --version` answer
"is this build current?" by visual comparison against `git log -1`.

Released wheels and CI environments without a `.git` directory simply omit
the suffix and print the base version unchanged.
"""
from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path


def _base_version() -> str:
    """Installed PyPI version (single source of truth: pyproject.toml).

    Falls back to `"unknown"` when the package isn't installed (e.g. running
    out of a source tree that wasn't `pip install -e .`'d). The fallback is
    distinct from the editable-install case — `get_version()` would still
    try to graft a git suffix on top.
    """
    try:
        return _pkg_version("autopilot2")
    except PackageNotFoundError:
        return "unknown"


def _git_suffix(repo_root: Path) -> str:
    """Return `<short-sha>.<commit-ts>` for HEAD of `repo_root`, or `""`.

    Empty string covers every "not a git checkout" path so callers can
    `f"{base}+{suffix}" if suffix else base` without further conditionals:
    - `repo_root` has no `.git/` (released wheel install).
    - `git` binary not on PATH.
    - `git log` returned non-zero (corrupt repo, no commits).
    - subprocess timed out (filesystem stall, hung worktree lock).

    The timestamp format `%Y%m%dT%H%M%SZ` matches the regex pinned in the
    TB-139 verification briefing — operators can eyeball-compare this
    against `git log -1 --date=iso-strict`.
    """
    if not (repo_root / ".git").exists():
        return ""
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%h"],
            capture_output=True, text=True, timeout=2,
        )
        ts = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1",
             "--format=%cd", "--date=format:%Y%m%dT%H%M%SZ"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if sha.returncode != 0 or ts.returncode != 0:
        return ""
    sha_s = sha.stdout.strip()
    ts_s = ts.stdout.strip()
    if not sha_s or not ts_s:
        return ""
    return f"{sha_s}.{ts_s}"


def get_version() -> str:
    """Full ap2 version string: base + optional `+<sha>.<ts>` git suffix.

    Computed each call (cheap — `git log -1` is local and runs in a few
    ms). Editable installs reflect the current HEAD of the source tree on
    every invocation, so `ap2 --version` is a one-shot freshness check.

    The package's own source dir (`Path(__file__).resolve().parent.parent`)
    is the git root we query — that's where `git log -1` sees changes the
    operator just made even if cwd is somewhere else when the CLI runs.
    """
    base = _base_version()
    repo_root = Path(__file__).resolve().parent.parent
    suffix = _git_suffix(repo_root)
    return f"{base}+{suffix}" if suffix else base


# Snapshot at import time. Most callers should prefer `get_version()` (which
# re-reads HEAD on each call) — this constant is here for `import ap2;
# ap2.__version__` ergonomics and for tools that introspect packages.
__version__ = get_version()
