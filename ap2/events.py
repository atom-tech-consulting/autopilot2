"""Append-only event log. Each line is a JSON object with at least `ts` and `type`.

Events are the shared awareness mechanism in v2: every `query()` call receives
the last N events as context, so stateless agents can reconstruct recent history
without accumulating it in any long-lived session.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterable


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(events_file: Path, type: str, **fields: Any) -> dict:
    """Append an event; returns the event dict actually written."""
    events_file.parent.mkdir(parents=True, exist_ok=True)
    evt = {"ts": _now(), "type": type, **fields}
    line = json.dumps(evt, default=str)
    fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (line + "\n").encode())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return evt


def tail(events_file: Path, n: int = 50) -> list[dict]:
    """Return the last `n` events as dicts (oldest first)."""
    if not events_file.exists():
        return []
    lines = _tail_lines(events_file, n)
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _tail_lines(path: Path, n: int) -> list[str]:
    """Efficient tail: read backwards in blocks until we have n newlines."""
    block = 8192
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            read = min(block, size)
            size -= read
            f.seek(size)
            data = f.read(read) + data
    lines = data.decode(errors="replace").splitlines()
    return lines[-n:]


def format_for_prompt(events: Iterable[dict], *, max_chars: int = 6000) -> str:
    """Render events as a compact string suitable for a prompt block."""
    rendered = []
    total = 0
    for e in events:
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        extras = {k: v for k, v in e.items() if k not in ("ts", "type")}
        extra_str = " ".join(f"{k}={_short(v)}" for k, v in extras.items())
        line = f"{ts} {typ} {extra_str}".rstrip()
        total += len(line) + 1
        if total > max_chars:
            break
        rendered.append(line)
    return "\n".join(rendered)


def _short(v: Any, limit: int = 200) -> str:
    s = str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# TB-158: shared formatter for `verification_failed` events. Both
# `ap2 logs` (CLI) and `ap2/web.py` (events table + task-run detail page)
# call this so the per-bullet summary, sort order, and truncation rules
# stay in lockstep — the surface-specific layer only handles ANSI vs HTML
# and chooses truncation lengths via the kwargs.
#
# Sort order: failed > unverified > pass within `failed_bullets` (only
# `fail` is included today; the buckets are listed for callers that want
# them). Within failed, source order is preserved so the rendering order
# matches the briefing's `## Verification` bullet order.
def summarize_verification_failed(
    event: dict,
    *,
    max_bullet: int = 240,
    max_note: int = 400,
) -> dict:
    """Compact, surface-agnostic summary of a `verification_failed` event.

    Returns a dict with:
        summary_line     "5/8 passed, 2 failed, 1 unverified" (or fallback)
        failed_bullets   list of {kind, bullet, notes} — fail-status only,
                         truncated per the max_* kwargs.
        pass_count       int
        fail_count       int
        unverified_count int
        total            int (sum of the three; 0 for legacy events)

    Two flavours of the event exist on disk today:
      - per-task (briefing-driven) — carries `criteria=[{kind, status,
        bullet, notes}, ...]`. We score and render from that list.
      - project-wide gate — carries `command`, `exit_code`, `stderr_tail`
        and NO `criteria`. We synthesize a single failed bullet from
        `command` + `stderr_tail` so the renderer still has something
        meaningful to display.

    Events with no recognizable structure (e.g. very old or hand-written
    test fixtures) return the empty fallback `pass=0, fail=0, total=0,
    failed_bullets=[]` rather than raising — operators reading old
    events.jsonl shouldn't see the page break on a missing field.
    """
    criteria = event.get("criteria")
    if not isinstance(criteria, list):
        cmd = str(event.get("command") or "").strip()
        if cmd:
            stderr = str(event.get("stderr_tail") or "").strip()
            return {
                "summary_line": (
                    f"project-wide verification failed "
                    f"(exit {event.get('exit_code', '?')})"
                ),
                "failed_bullets": [{
                    "kind": "project_gate",
                    "bullet": _truncate(cmd, max_bullet),
                    "notes": _truncate(stderr, max_note),
                }],
                "pass_count": 0,
                "fail_count": 1,
                "unverified_count": 0,
                "total": 1,
            }
        return {
            "summary_line": "verification failed (no criteria captured)",
            "failed_bullets": [],
            "pass_count": 0,
            "fail_count": 0,
            "unverified_count": 0,
            "total": 0,
        }

    def _status(c: Any) -> str:
        if not isinstance(c, dict):
            return ""
        return str(c.get("status") or "").strip().lower()

    pass_count = sum(1 for c in criteria if _status(c) == "pass")
    fail_count = sum(1 for c in criteria if _status(c) == "fail")
    unverified_count = sum(1 for c in criteria if _status(c) == "unverified")
    total = pass_count + fail_count + unverified_count

    failed_bullets = [
        {
            "kind": str((c or {}).get("kind") or ""),
            "bullet": _truncate(str((c or {}).get("bullet") or ""), max_bullet),
            "notes": _truncate(str((c or {}).get("notes") or ""), max_note),
        }
        for c in criteria
        if _status(c) == "fail"
    ]

    return {
        "summary_line": (
            f"{pass_count}/{total} passed, "
            f"{fail_count} failed, {unverified_count} unverified"
        ),
        "failed_bullets": failed_bullets,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "unverified_count": unverified_count,
        "total": total,
    }


def _truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"
