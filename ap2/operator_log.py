"""Read-only helpers for `.cc-autopilot/operator_log.md`.

The operator log is written by `tools.py::do_operator_log_append` and
`tools.py::_append_operator_audit_line` (operator-`ack` notes plus the
per-queued-op audit lines, including TB-152's `rejected ideation
proposal → TB-N (<title>): <reason>` line). This module is the read-side
counterpart — pure functions that parse the log without mutating it.

TB-163: `tail_rejections` is the first reader. It surfaces recent
rejection lines so `prompts.build_control_prompt` can render them as a
"Recent operator rejections" snapshot block in the ideation prompt
header — pattern-level operator-veto signal that previously only reached
ideation per-line via the file's full text.
"""
from __future__ import annotations

import re

from .config import Config


# TB-163: line shape from `tools.py::_append_operator_audit_line` (the
# `op == "reject"` branch). Example:
#     - 2026-05-04T05:53:25Z — rejected ideation proposal → TB-150 (web /pending-review section): superseded by web tag-pill renderer
# Match permissively on the prefix so future title/reason variations stay
# captured. Title parens and the trailing reason are optional — a quiet
# reject without a reason still emits `(no reason given)` post-colon, so
# the "after the arrow" capture group is always non-empty in practice but
# we don't enforce it here.
_REJECTION_RE = re.compile(
    r"^-\s+(?P<ts>\S+)\s+—\s+rejected ideation proposal\s+→\s+"
    r"(?P<rest>TB-\d+.*)$"
)

# TB-163: cap how far back we read so the helper stays O(1) regardless
# of operator_log.md's growth. 200 lines is enough headroom to find ~5
# recent rejections under any realistic ratio of rejects to other audit
# lines (`applied operator-queued <op> → TB-N`, `ack` notes, etc.).
_MAX_LINES_TO_SCAN = 200


def tail_rejections(cfg: Config, limit: int = 5) -> list[str]:
    """Return up to `limit` recent `rejected ideation proposal` entries
    from operator_log.md, in chronological order (oldest first, newest
    last) — matching the events-block convention.

    Each returned string is the bullet body (i.e. with the leading `- `
    bullet marker stripped, but the timestamp + ` — ` + `TB-N (...)`
    body preserved). Callers re-bullet at render time.

    The reader walks the LAST `_MAX_LINES_TO_SCAN` lines of the file
    backwards, stopping after `limit` matches. No I/O side effects beyond
    a single text read; returns `[]` if the file is missing or
    unreadable.
    """
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    if not log_path.exists():
        return []
    try:
        text = log_path.read_text()
    except OSError:
        return []

    lines = text.splitlines()
    tail = lines[-_MAX_LINES_TO_SCAN:] if len(lines) > _MAX_LINES_TO_SCAN else lines

    matches: list[str] = []
    for line in reversed(tail):
        m = _REJECTION_RE.match(line)
        if not m:
            continue
        ts = m.group("ts")
        rest = m.group("rest").strip()
        # Drop the redundant "rejected ideation proposal → " preamble at
        # render time — the block heading already says "Recent operator
        # rejections", so each bullet just carries `<ts> — TB-N (...): <reason>`.
        matches.append(f"{ts} — {rest}")
        if len(matches) >= limit:
            break

    # Reverse to chronological (newest last), matching events-block convention.
    return list(reversed(matches))
