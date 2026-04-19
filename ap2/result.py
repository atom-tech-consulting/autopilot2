"""Parse RESULT blocks emitted by task agents.

Task agents end their final message with:

    RESULT:
    status: complete|incomplete|blocked|failed
    commit: <hash or "none">
    summary: <short text>
    files_changed: a.py, b.py
    tests_passed: true|false

The block may be fenced or not; we look for the token RESULT: and parse lines
until a blank line or the end of the message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class TaskResult:
    status: str = "unknown"
    commit: str = ""
    summary: str = ""
    files_changed: list[str] | None = None
    tests_passed: bool | None = None
    raw: str = ""


_VALID = {"complete", "incomplete", "blocked", "failed"}


def parse(text: str) -> TaskResult:
    text = text or ""
    m = re.search(r"RESULT:\s*$(.*)", text, re.M | re.S)
    if not m:
        return TaskResult(status="unknown", raw=text[-500:])
    body = m.group(1)
    # Stop at closing fence or two blank lines.
    body = re.split(r"\n```|\n\n\n", body, maxsplit=1)[0]
    r = TaskResult(raw=body.strip())
    for line in body.splitlines():
        line = line.strip().lstrip("-").strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip("`")
        if key == "status" and val.lower() in _VALID:
            r.status = val.lower()
        elif key == "commit":
            r.commit = "" if val.lower() in ("none", "n/a", "") else val
        elif key == "summary":
            r.summary = val
        elif key == "files_changed":
            r.files_changed = [f.strip() for f in val.split(",") if f.strip()]
        elif key == "tests_passed":
            r.tests_passed = val.lower() in ("true", "yes", "pass", "passed", "1")
    if r.status == "unknown" and r.raw:
        # Permit naked `status: complete` without the leading RESULT: line.
        m2 = re.search(r"^status:\s*(\w+)", r.raw, re.M | re.I)
        if m2 and m2.group(1).lower() in _VALID:
            r.status = m2.group(1).lower()
    return r
