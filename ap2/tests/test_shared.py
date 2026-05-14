"""TB-222: happy + error path coverage for the five `ap2/_shared.py` helpers.

`ap2/_shared.py` shipped via TB-217 / TB-218 / TB-220 and is now imported by
seven modules (`ap2/board.py`, `ap2/cli.py`, `ap2/cron.py`, `ap2/diagnose.py`,
`ap2/events.py`, `ap2/retry.py`, `ap2/web.py`). Prior to this module,
`grep -rn "from ap2._shared" ap2/tests/` returned zero — every helper's
contract rode on implication via callers, with three semantic edges
particularly easy to break:

  - `locked_sidecar` vs `locked_inplace`: the on-disk distinction (which fd
    the lock is bound to, and therefore whether the body of the `with`
    block may rewrite or truncate the protected path) is the entire reason
    TB-217 shipped two named functions instead of one helper with a
    `sidecar=` flag. A future refactor that collapses the two onto one fd
    would silently invalidate the lock for every future opener of
    `cron.yaml` / `retry_state.json`.
  - `short()`'s ellipsis boundary is `s[: limit - 1] + "…"` (U+2026). An
    off-by-one to `s[: limit]` or a swap to the ASCII `"..."` triple-dot
    would silently truncate one char early/late, or change the visual
    truncation marker the event renderers depend on.
  - `read_pid()`'s exception fallback covers three branches (FileNotFound,
    ValueError, OSError); a future refactor that narrows the `except`
    tuple to only `ValueError` would surface unhandled OSErrors at every
    `ap2 status` call against a daemon-down project with a permissions
    issue.

Each helper gets focused happy + error coverage here. The shape mirrors
TB-205 (`test_env_knobs.py`) and TB-210 (`test_tb210_env_knobs.py`):
one focused test module, descriptive function names that name the
contract being pinned, and minimal stubs (a tmp-path `Config`-shaped
`SimpleNamespace` for `read_pid`, a frozen `datetime` module shim for
`now`).

No changes to `ap2/_shared.py` itself, no changes to existing callers,
no changes to drift gates — `_shared.py` is internal infrastructure, not
a public surface axis tracked by `test_coverage_drift.py`.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import _shared
from ap2._shared import (
    locked_inplace,
    locked_sidecar,
    now,
    read_pid,
    short,
)


# ===========================================================================
# (1) locked_inplace(path) — exclusive fcntl lock on `path` itself.
#
# Source: ap2/_shared.py L65-81. Opens an fd on `path` and holds LOCK_EX
# on it for the with-block. Caller must NOT truncate or replace `path` —
# doing so invalidates the fd-bound lock for any future opener (this is
# the semantic distinction vs `locked_sidecar` that motivated the
# two-named-function design).
# ===========================================================================


def test_locked_inplace_acquires_exclusive_lock_and_yields_fd(tmp_path):
    """Happy path: `locked_inplace(path)` opens an fd on `path` itself,
    yields it, holds an exclusive flock for the with-block. Pin: the
    yielded value is a real fd that resolves to the same inode as
    `path` on disk."""
    target = tmp_path / "in_place.bin"
    with locked_inplace(target) as fd:
        assert isinstance(fd, int)
        # The fd points at `target` itself — fstat inode matches.
        assert os.fstat(fd).st_ino == target.stat().st_ino
        # The exclusive lock is held: a non-blocking acquire from a
        # second fd on the same path raises BlockingIOError (would
        # block).
        contender = os.open(target, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)


def test_locked_inplace_releases_lock_after_with_block(tmp_path):
    """Happy path: after the `with` block exits, the lock is released
    (a fresh non-blocking acquire from a new fd succeeds). Pins the
    `finally: fcntl.flock(fd, LOCK_UN)` cleanup so a refactor that drops
    it surfaces here."""
    target = tmp_path / "in_place_release.bin"
    with locked_inplace(target):
        pass
    # After the with-block, a new opener can acquire LOCK_EX | LOCK_NB
    # without blocking.
    new_fd = os.open(target, os.O_RDWR)
    try:
        fcntl.flock(new_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(new_fd, fcntl.LOCK_UN)
    finally:
        os.close(new_fd)


def test_locked_inplace_creates_parent_directory_when_missing(tmp_path):
    """Happy path: `path.parent` doesn't exist → helper auto-creates it
    with `mkdir(parents=True, exist_ok=True)` before opening the fd. A
    refactor that drops the mkdir would surface here as FileNotFoundError."""
    nested = tmp_path / "a" / "b" / "c" / "lock_target.bin"
    assert not nested.parent.exists()
    with locked_inplace(nested):
        assert nested.parent.is_dir()
        assert nested.exists()


# ===========================================================================
# (2) locked_sidecar(path) — exclusive fcntl lock on `path.lock` sidecar.
#
# Source: ap2/_shared.py L84-101. Opens an fd on
# `path.with_suffix(path.suffix + ".lock")` and locks THAT — `path` itself
# is never opened by the helper, so the with-body may rewrite, truncate,
# or atomic-replace `path` without invalidating the lock. This is the
# whole reason `cron.yaml` and `retry_state.json` writers use the
# sidecar variant (write-to-temp + os.replace pattern).
# ===========================================================================


def test_locked_sidecar_creates_sidecar_at_path_dot_lock(tmp_path):
    """Happy path: lock fd is bound to `path.with_suffix(path.suffix +
    ".lock")` — NOT to `path` itself. Pin the exact sidecar naming
    convention: a refactor that switches to a hidden-dot prefix or a
    different suffix would surface here, AND any future opener that
    grabs the wrong file as the lock target would silently lose
    serialization."""
    target = tmp_path / "state.json"
    expected_sidecar = tmp_path / "state.json.lock"
    assert not expected_sidecar.exists()
    with locked_sidecar(target) as fd:
        assert expected_sidecar.exists(), (
            "sidecar must materialize at path.with_suffix(suffix + '.lock')"
        )
        # The fd is on the sidecar, not on `target`.
        assert os.fstat(fd).st_ino == expected_sidecar.stat().st_ino
        # `target` itself is NOT opened by the helper — it should not
        # have been auto-created.
        assert not target.exists(), (
            "locked_sidecar must NOT touch the protected path itself; "
            "only the sidecar file should be opened/created"
        )


def test_locked_sidecar_permits_safe_rewrite_under_lock(tmp_path):
    """Highest-leverage assertion in the file: pin the critical semantic
    distinction vs `locked_inplace`. Under `locked_sidecar`, the with-body
    may freely truncate, rewrite, or atomic-replace the protected `path`
    (canonical write-to-temp + os.replace pattern) WITHOUT invalidating
    the lock — because the fd that holds the flock points at the sidecar,
    not at `path` itself.

    A refactor that collapses the two helpers onto one fd-on-the-protected-path
    implementation (or adds a `sidecar=` flag and accidentally inverts the
    default) would silently break this contract: every cron-job rewrite of
    `cron.yaml` / `retry_state.json` would invalidate the lock for any
    future opener. This test is the regression pin."""
    target = tmp_path / "cron.yaml"
    target.write_text("old: contents\n")

    with locked_sidecar(target) as fd:
        # Canonical write-to-temp + os.replace under the lock.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text("new: contents\n")
        os.replace(tmp, target)
        # The lock is STILL held — verify by trying a non-blocking
        # acquire on the sidecar fd from a separate opener.
        sidecar_path = target.with_suffix(target.suffix + ".lock")
        contender = os.open(sidecar_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
        # And the held fd is still on the sidecar (not the replaced path).
        assert os.fstat(fd).st_ino == sidecar_path.stat().st_ino

    # After the with-block: target carries the new contents and the
    # sidecar still exists for the next acquirer.
    assert target.read_text() == "new: contents\n"
    assert (tmp_path / "cron.yaml.lock").exists()


def test_locked_sidecar_creates_parent_directory_when_missing(tmp_path):
    """Happy path: `path.parent` doesn't exist → helper auto-creates it
    before opening the sidecar fd. Mirrors `locked_inplace`'s same
    contract."""
    nested = tmp_path / "x" / "y" / "z" / "state.json"
    assert not nested.parent.exists()
    with locked_sidecar(nested):
        assert nested.parent.is_dir()
        assert (nested.parent / "state.json.lock").exists()


def test_locked_inplace_vs_sidecar_target_different_files(tmp_path):
    """Cross-helper pin: for the SAME input `path`, the two helpers bind
    their fd to DIFFERENT files on disk. `locked_inplace` opens `path`;
    `locked_sidecar` opens `path.lock`. A refactor that accidentally
    aliases the two (e.g. one calling the other internally) would
    surface here as identical inodes."""
    target = tmp_path / "shared_target.bin"
    target.touch()

    with locked_inplace(target) as in_fd:
        in_ino = os.fstat(in_fd).st_ino

    # locked_sidecar on the SAME `target` should yield an fd on a
    # different file (the sidecar), with a different inode.
    with locked_sidecar(target) as side_fd:
        side_ino = os.fstat(side_fd).st_ino

    assert in_ino != side_ino, (
        "locked_inplace and locked_sidecar must bind to different files "
        "for the same input path — that semantic distinction is the "
        "entire reason TB-217 shipped two named helpers"
    )
    assert in_ino == target.stat().st_ino
    assert side_ino == (tmp_path / "shared_target.bin.lock").stat().st_ino


# ===========================================================================
# (3) short(v, limit) — `str(v)` truncated to `limit` chars with `…` suffix.
#
# Source: ap2/_shared.py L104-116. Returns `str(v)` unchanged when
# `len(str(v)) <= limit`, else `str(v)[: limit - 1] + "…"`. The U+2026
# horizontal-ellipsis (NOT ASCII `"..."`) is the visual truncation
# signal across event renderers.
# ===========================================================================


def test_short_returns_str_unchanged_when_within_limit():
    """Happy path: `len(str(v)) <= limit` → returns `str(v)` verbatim
    with no ellipsis appended. Pin both the boundary cases (len == limit
    and len < limit) and the str() conversion."""
    assert short("abc", 5) == "abc"
    assert short("abcde", 5) == "abcde"  # len == limit, no truncation
    assert short("", 0) == ""
    assert short("", 5) == ""


def test_short_truncates_with_ellipsis_at_limit_minus_one():
    """Highest-leverage `short` assertion: pin the EXACT truncation
    boundary as `s[: limit - 1] + "…"` — NOT `s[: limit] + "…"` (would
    overshoot by one char), NOT `s[: limit - 3] + "..."` (would undershoot
    AND swap U+2026 for ASCII triple-dot). The U+2026 horizontal-ellipsis
    is the visual signal every event renderer downstream depends on; a
    silent off-by-one or marker swap is the most likely regression mode."""
    # 10-char string truncated to limit=5 → 4 chars from input + "…".
    assert short("abcdefghij", 5) == "abcd…"
    # Verify the marker is U+2026 (length-1 char), NOT ASCII "..." (length-3).
    assert short("abcdefghij", 5)[-1] == "…"
    assert short("abcdefghij", 5)[-1] == "…"
    assert "..." not in short("abcdefghij", 5)
    # Total length of result MUST equal `limit` — that's the contract
    # `short` exists to enforce for one-line event renderers.
    assert len(short("abcdefghij", 5)) == 5
    # Boundary pin: when len(s) == limit + 1, truncation kicks in (the
    # `<=` is inclusive, so `len > limit` is the trigger).
    assert short("abcdef", 5) == "abcd…"
    # Pin the prefix slice is `[:limit-1]`, not `[:limit]` — for limit=5,
    # the first 4 chars of input survive, NOT the first 5.
    assert short("abcdefghij", 5)[:4] == "abcd"
    assert short("abcdefghij", 5) != "abcde…"  # would be the off-by-one bug


def test_short_handles_non_string_inputs_via_str():
    """Happy path: `v` is coerced through `str()` before length / slicing.
    Pin: ints, dicts, lists, None all round-trip through `str()`; the
    truncation boundary is then on the stringified form's length."""
    # Short int — no truncation.
    assert short(42, 10) == "42"
    # Long int — truncated on the str() form.
    assert short(12345678901234567890, 10) == "123456789…"
    # None — no truncation.
    assert short(None, 10) == "None"
    # Dict / list — coerced via repr-like str().
    assert short({"a": 1}, 100) == str({"a": 1})


def test_short_limit_one_yields_only_ellipsis_when_truncating():
    """Edge case: `limit=1` and `len(s) > 1` → result is exactly `"…"`
    (the slice is `s[:0] = ""`, and `"" + "…" = "…"`). Pin so a future
    refactor that adds `if limit < 2: return ""` or similar safety
    short-circuit surfaces as a behavioral change."""
    assert short("abc", 1) == "…"
    assert len(short("abc", 1)) == 1
    # With limit=1 and len(s) == 1, no truncation triggers (1 <= 1).
    assert short("a", 1) == "a"


def test_short_limit_zero_edge_case():
    """Edge case: `limit=0`. For empty string, `len("") == 0 <= 0` so the
    no-truncation branch returns `""`. For non-empty string, the
    truncation branch evaluates `s[: -1] + "…"` (since `limit - 1 == -1`,
    Python's negative-index slicing drops the last character). This is
    the CURRENT behavior — pin it so a future refactor that adds a
    `max(0, limit - 1)` guard surfaces as a deliberate change."""
    assert short("", 0) == ""
    # Non-empty + limit=0: s[:-1] + "…". For "abc": "ab" + "…" = "ab…".
    assert short("abc", 0) == "ab…"
    # The result length is len(s), NOT 0 — pinning the quirky current
    # behavior so a callers-pass-limit=0-deliberately bug surfaces.
    assert len(short("abc", 0)) == 3


# ===========================================================================
# (4) now() — UTC ISO-8601 timestamp with `Z` suffix.
#
# Source: ap2/_shared.py L119-129. Returns
# `dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`.
# Format: `YYYY-MM-DDTHH:MM:SSZ`. Every event-log and cron-state writer
# in the codebase already produces this exact shape.
# ===========================================================================


def test_now_matches_iso8601_z_pattern():
    """Happy path: return value matches the canonical `YYYY-MM-DDTHH:MM:SSZ`
    pattern. A refactor that drops the `Z` suffix or switches to
    `isoformat()` (which produces `+00:00` instead) would surface here."""
    value = now()
    assert isinstance(value, str)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        value,
    ), f"now() returned {value!r}, does not match YYYY-MM-DDTHH:MM:SSZ"


def test_now_uses_utc_via_monkeypatched_datetime(monkeypatch):
    """UTC pin: monkey-patch `_shared.dt` to a frozen module shim whose
    `datetime.now(tz)` asserts that `tz is dt.timezone.utc` and returns
    a known fixed datetime. This proves `now()` requests UTC explicitly
    (not naive `datetime.now()` followed by `.strftime("...Z")`, which
    would mislabel local time as UTC). A refactor that drops the `tz=`
    arg surfaces both as the assertion below AND as the wrong rendered
    string."""
    fixed = dt.datetime(2026, 5, 14, 15, 30, 45, tzinfo=dt.timezone.utc)
    requested_tz: list = []

    class _FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            requested_tz.append(tz)
            return fixed

    class _FrozenModule:
        datetime = _FrozenDatetime
        timezone = dt.timezone

    monkeypatch.setattr(_shared, "dt", _FrozenModule)

    assert _shared.now() == "2026-05-14T15:30:45Z"
    assert requested_tz == [dt.timezone.utc], (
        "now() must call datetime.now(tz=dt.timezone.utc) — naive "
        ".now() with no tz arg silently mislabels local time as UTC"
    )


# ===========================================================================
# (5) read_pid(cfg) — read daemon PID from `cfg.pid_file`, returning int
# or None on missing / unparseable / unreadable file.
#
# Source: ap2/_shared.py L132-145. Returns None on FileNotFoundError
# (via the `exists()` guard), ValueError (non-integer body), or OSError
# (read failure, e.g. directory-as-file or permission). Call-site shape
# matches `ap2/cli.py` and `ap2/web.py`: a `Config`-like object exposing
# `pid_file: Path`. Tests use a `SimpleNamespace` stub of the same shape.
# ===========================================================================


def _cfg_stub(pid_file: Path) -> SimpleNamespace:
    """Minimal `Config`-shaped stub matching the `cfg.pid_file` access
    pattern from `ap2/cli.py` line 64/94/107 and `ap2/web.py` line 2266."""
    return SimpleNamespace(pid_file=pid_file)


def test_read_pid_returns_int_when_pid_file_contains_digits(tmp_path):
    """Happy path: pid file exists and contains an integer (with the
    typical trailing newline written by `daemon._write_pid_file`).
    `read_pid` strips and int-parses, returning the int."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("12345\n")
    assert read_pid(_cfg_stub(pid_file)) == 12345


def test_read_pid_returns_int_without_trailing_whitespace(tmp_path):
    """Happy path: explicit `.strip()` in the implementation handles
    leading/trailing whitespace. Pin so a refactor that drops the strip
    surfaces (a literal newline at the end would trip `int()` parsing
    and silently land in the ValueError branch returning None)."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("  9999  \n")
    assert read_pid(_cfg_stub(pid_file)) == 9999


def test_read_pid_returns_none_when_pid_file_missing(tmp_path):
    """Error path 1: pid file does NOT exist → `exists()` guard returns
    None without attempting `read_text()`. This is the dominant case
    on a fresh project (daemon never started). A refactor that drops
    the `exists()` guard would surface here as FileNotFoundError leaking
    through to the caller."""
    pid_file = tmp_path / "does_not_exist.pid"
    assert not pid_file.exists()
    assert read_pid(_cfg_stub(pid_file)) is None


def test_read_pid_returns_none_when_pid_file_has_non_integer_body(tmp_path):
    """Error path 2: pid file exists but body isn't parseable as int →
    ValueError caught, return None. Catches a refactor that narrows the
    `except (ValueError, OSError)` tuple to drop ValueError. Common
    real-world cause: stale pid file holding a hex / log-line / empty
    string written by an aborted daemon start."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("not-a-number\n")
    assert read_pid(_cfg_stub(pid_file)) is None


def test_read_pid_returns_none_when_pid_file_is_empty(tmp_path):
    """Error path 2b: pid file exists but is empty → `int("")` raises
    ValueError → caught, return None. Pins a real-world failure mode
    (atomic-write race where the file was created but the content
    wasn't yet flushed at the moment `ap2 status` peeked)."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("")
    assert read_pid(_cfg_stub(pid_file)) is None


def test_read_pid_returns_none_when_pid_file_is_unreadable_oserror(tmp_path):
    """Error path 3: pid file exists but `read_text()` raises OSError
    (a subclass thereof) → caught, return None. Triggered here by
    pointing `pid_file` at a DIRECTORY: `Path.exists()` returns True
    (directories exist), but `Path.read_text()` raises IsADirectoryError
    which is an OSError subclass.

    Pins the third branch of the `except (ValueError, OSError)` tuple —
    a refactor that narrows to just `ValueError` would surface here as
    an unhandled IsADirectoryError leaking through to the `ap2 status`
    caller (the pre-TB-220 behavior matched this; this test ensures the
    consolidated helper preserves it)."""
    pid_file = tmp_path / "daemon.pid"
    pid_file.mkdir()  # `pid_file` is now a directory, not a regular file.
    assert pid_file.exists()
    assert read_pid(_cfg_stub(pid_file)) is None
