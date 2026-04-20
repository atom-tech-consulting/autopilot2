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
import shlex
from dataclasses import dataclass, field


@dataclass
class TaskResult:
    status: str = "unknown"
    commit: str = ""
    summary: str = ""
    files_changed: list[str] | None = None
    tests_passed: bool | None = None
    cron: list[dict] = field(default_factory=list)
    raw: str = ""


_VALID = {"complete", "incomplete", "blocked", "failed"}
_CRON_ACTIONS = {"add", "remove", "update"}


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
        elif key == "cron":
            r.cron.append(_parse_cron_directive(val))
    if r.status == "unknown" and r.raw:
        # Permit naked `status: complete` without the leading RESULT: line.
        m2 = re.search(r"^status:\s*(\w+)", r.raw, re.M | re.I)
        if m2 and m2.group(1).lower() in _VALID:
            r.status = m2.group(1).lower()
    return r


def _parse_cron_directive(text: str) -> dict:
    """Parse a RESULT `cron:` directive into a dict suitable for do_cron_edit.

    Format: `<action> key=value key="quoted value" ...`
    Returns {"_error": "<reason>", "_raw": "<text>"} on malformed input so the
    daemon can log and skip, without crashing the whole RESULT parse.
    """
    text = text.strip()
    if not text:
        return {"_error": "empty directive", "_raw": text}
    try:
        tokens = shlex.split(text)
    except ValueError as e:
        return {"_error": f"shlex: {e}", "_raw": text}
    if not tokens:
        return {"_error": "no tokens", "_raw": text}
    action = tokens[0].lower()
    if action not in _CRON_ACTIONS:
        return {"_error": f"unknown action {action!r}", "_raw": text}
    out: dict = {"action": action}
    for tok in tokens[1:]:
        if "=" not in tok:
            return {"_error": f"bad token {tok!r}", "_raw": text}
        k, _, v = tok.partition("=")
        out[k.strip().lower()] = v
    if "name" not in out:
        return {"_error": "missing name", "_raw": text}
    if action == "add":
        if "interval" not in out or "prompt" not in out:
            return {"_error": "add requires interval and prompt", "_raw": text}
    return out
