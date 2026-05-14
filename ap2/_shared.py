"""Internal helpers shared across `ap2/` modules.

Currently exposes two fcntl-based file-locking context managers. They share
99% of their body but differ in *what* file the lock fd points at — a
distinction with real on-disk consequences:

- `locked_inplace(path)` opens an fd on `path` itself and holds the
  exclusive flock on that fd. Use this when the file you want to lock IS
  the file you want serialized access to. The caller is responsible for
  not truncating or replacing `path` under the lock — doing so would
  invalidate the fd-bound lock for any future opener.

- `locked_sidecar(path)` opens an fd on a sibling
  `path.with_suffix(path.suffix + ".lock")` file instead, and locks
  *that*. The locked file (`path`) itself is never opened by the helper,
  so the body of the `with` block is free to rewrite/truncate/replace
  `path` atomically (e.g. write-to-temp + os.replace) without disturbing
  the lock. Use this when the protected resource is a file you mutate
  via whole-file rewrite under the lock — `.cc-autopilot/cron.yaml`,
  `.cc-autopilot/retry_state.json`, etc.

Both helpers create parent directories of the lock-fd path with
`mkdir(parents=True, exist_ok=True)` before opening, so the lock file
materialises on first use without a separate setup step.

Two named functions (not one helper with a `sidecar=` flag) because the
two locking modes are semantically distinct and every current caller picks
one variant and sticks with it; forcing the choice at the import site
makes the distinction visible at the call site.

Also exposes `short(v, limit)` — a string-or-value truncation helper used
across `ap2/cli.py`, `ap2/diagnose.py`, and `ap2/events.py` for rendering
event extras at one-line widths. No default `limit` argument: each caller
picks explicitly (the prior convention of three different module-local
defaults — 120 / 100 / 200 — was a smell, not a feature).

TB-220 added two more helpers that previously lived as private duplicates:

- `now()` — UTC ISO-8601 timestamp (`YYYY-MM-DDTHH:MM:SSZ`). Was `_now()`
  in `ap2/cron.py` and `ap2/events.py`; the two bodies were functionally
  identical (cron's inlined `import datetime as dt` inside the function,
  events used the module-level import — same external behavior).
- `read_pid(cfg)` — read daemon PID from `cfg.pid_file`, returning the int
  or None on missing / unparseable file. Was `_read_pid(cfg)` in
  `ap2/cli.py` and `ap2/web.py`; the two bodies were byte-identical.

These were below goal.md's threshold-three rule (n=2 each), but bundled
with the n=3 extractions while the shared module was fresh — incremental
cost is minimal and a future third call site lands on the shared helper
naturally.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from ap2.config import Config


@contextlib.contextmanager
def locked_inplace(path: Path) -> Iterator[int]:
    """Acquire an exclusive fcntl lock on `path` itself.

    Opens (creating if absent) an fd on `path` and holds `LOCK_EX` on it
    for the duration of the `with` block. The caller must not truncate
    or replace `path` under the lock — see module docstring for the
    sidecar variant when whole-file rewrite is needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def locked_sidecar(path: Path) -> Iterator[int]:
    """Acquire an exclusive fcntl lock on a `.lock` sidecar next to `path`.

    Opens (creating if absent) an fd on `path.with_suffix(path.suffix +
    ".lock")` and holds `LOCK_EX` on that. `path` itself is never opened
    by this helper, so the `with` body is free to truncate, rewrite, or
    atomic-replace `path` without invalidating the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def short(v: Any, limit: int) -> str:
    """Return `str(v)`, truncated to `limit` chars with a `…` suffix if needed.

    Returns `str(v)` unchanged when `len(str(v)) <= limit`; otherwise
    returns `str(v)[: limit - 1] + "…"` — the U+2026 horizontal-ellipsis
    character is the visual signal that truncation occurred. No default
    `limit` argument: every caller picks explicitly (TB-218 collapsed
    three module-local defaults of 120 / 100 / 200, each of which made
    sense at its own site; one shared default would have imposed one
    site's choice on the others without merit).
    """
    s = str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def now() -> str:
    """Return the current UTC time as an ISO-8601 string with `Z` suffix.

    Format: `YYYY-MM-DDTHH:MM:SSZ` — the format every event-log and
    cron-state writer in the codebase already produces. TB-220 consolidated
    this from two private `_now()` definitions in `ap2/cron.py` and
    `ap2/events.py` (the two bodies were functionally identical; cron's
    inlined `import datetime as dt` inside the function, events used the
    module-level import — same external behavior).
    """
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_pid(cfg: "Config") -> int | None:
    """Return the daemon PID stored in `cfg.pid_file`, or None if absent / unparseable.

    Returns None on FileNotFoundError, ValueError (file present but body
    isn't an int), or OSError (read failure). TB-220 consolidated this
    from two byte-identical private `_read_pid(cfg)` definitions in
    `ap2/cli.py` and `ap2/web.py`.
    """
    if not cfg.pid_file.exists():
        return None
    try:
        return int(cfg.pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def parse_blocked_summary_fix_shape(summary: str) -> dict | None:
    """Parse the canonical `BriefingFix:` agent prefix from a
    `task_complete status=blocked` summary (TB-225). Returns a dict on a
    clean parse, else None.

    Canonical agent contract (documented in `ap2/howto.md`'s
    `## Operator-in-the-loop relaxations` section):

        BriefingFix: <shape> at <briefing_path>:<line>: <from> -> <to>

    Where:
      - `<shape>` is a snake_case fix-shape token the operator-curated
        `AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist consults. Recommended
        bootstrap shapes (each names a known pitfall in the briefing-
        authoring prompt's `## Shell-bullet pitfalls to AVOID` section):
        `grep_missing_r_on_dir`, `bare_python_to_uv_run`,
        `literal_backtick_in_shell_bullet`, `bare_path_to_test_f`.
      - `<briefing_path>` is a project-relative path to the offending
        briefing file (typically `.cc-autopilot/tasks/<slug>.md`).
      - `<line>` is the 1-indexed line number where the daemon must
        verify the `from` pattern literally appears before patching.
      - `<from>` and `<to>` are the literal substrings the daemon will
        line-replace. The first `" -> "` (space-arrow-space) is the
        separator; subsequent occurrences are part of `<to>`.

    Returns ``{"shape", "from", "to", "file", "line"}`` on success.
    Returns None on any structural mismatch — malformed prefix, missing
    fields, non-integer line, empty shape/path/from. Callers fall back
    to today's manual-unfreeze behavior.

    Whitespace-tolerant on the canonical separators (` at `, `: `,
    ` -> `). The first `BriefingFix:` substring in `summary` wins;
    subsequent lines (e.g. agent re-statements) are ignored. The parser
    only consumes what the agent structurally emits — no regex-on-prose
    guessing, no inferring `from`/`to` from free text. TB-119 / TB-121
    history of `board_malformed_line` from prose-regex collisions is the
    reason this stays strictly structured.
    """
    if not isinstance(summary, str) or not summary:
        return None
    needle = "BriefingFix:"
    idx = summary.find(needle)
    if idx == -1:
        return None
    # Take from after the needle to the next newline (or end of string).
    after = summary[idx + len(needle):]
    nl = after.find("\n")
    if nl != -1:
        after = after[:nl]
    body = after.strip()
    # body == "<shape> at <path>:<line>: <from> -> <to>"
    at_idx = body.find(" at ")
    if at_idx == -1:
        return None
    shape = body[:at_idx].strip()
    rest = body[at_idx + 4:].strip()
    # rest == "<path>:<line>: <from> -> <to>" — split on the FIRST ": "
    # (colon-space) so a path/line containing no spaces is unambiguous.
    colon_space = rest.find(": ")
    if colon_space == -1:
        return None
    path_and_line = rest[:colon_space].strip()
    diff_part = rest[colon_space + 2:]
    # path_and_line == "<path>:<line>" — split on last `:` so a path
    # containing colons (unusual on .cc-autopilot/tasks/ but possible)
    # still parses correctly.
    last_colon = path_and_line.rfind(":")
    if last_colon == -1:
        return None
    file_part = path_and_line[:last_colon].strip()
    line_str = path_and_line[last_colon + 1:].strip()
    try:
        line_no = int(line_str)
    except ValueError:
        return None
    # diff_part == "<from> -> <to>" — split on the first " -> ".
    arrow = diff_part.find(" -> ")
    if arrow == -1:
        return None
    from_pat = diff_part[:arrow]
    to_pat = diff_part[arrow + 4:]
    if not shape or not file_part or not from_pat:
        return None
    return {
        "shape": shape,
        "from": from_pat,
        "to": to_pat,
        "file": file_part,
        "line": line_no,
    }
