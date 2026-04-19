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
