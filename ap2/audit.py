"""Retrospective-audit state derivation (TB-248).

Pure-read helpers that compute the "unreviewed shipped tasks since last
audit" surface for `ap2 audit` from existing on-disk state — TASKS.md
(Complete + Frozen sections), `events.jsonl` (task_complete timestamps
+ `auto_approved` audit markers), and `.cc-autopilot/operator_log.md`
(audit cursor + reviewed-set: `classified TB-N` / `audit-skipped TB-N` /
`rejected TB-N` lines).

State design promise: **no new state file**. The audit cursor and the
reviewed-set are both derived from operator_log.md grep. A separate
audit-state sidecar file would create a sync question ("if the sidecar
says reviewed but operator_log.md doesn't, who wins?") that the
log-only model elides by being the single source of truth. The cost is
one linear scan of operator_log.md per `ap2 audit` invocation —
trivial at multi-year scale (the file stays single-digit MB by design;
ideation reads the same file every cycle without performance
complaints).

Companion to the CLI's `cmd_audit` in `ap2/cli.py` — kept here as a
separate module so the parser logic is unit-testable without invoking
argparse / capsys plumbing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import events
from .board import Board
from .config import Config


# Line shapes written by `tools.py::_append_operator_audit_line` for the
# `reject` / `classify` branches and the new (TB-248) `audit_skip`
# branch. The audit cursor uses the existing `ack` op's literal
# `ran audit (...)` note shape (no new op-shape — per the briefing's
# "avoid op-shape proliferation" call). Each regex matches a single
# bullet line in operator_log.md; the file's bullet shape is always
# `- <ts> — <body>`.
_CLASSIFIED_RE = re.compile(
    r"^-\s+\S+\s+—\s+classified\s+(?P<task>TB-\d+)\s+impact="
)
_AUDIT_SKIPPED_RE = re.compile(
    r"^-\s+\S+\s+—\s+audit-skipped\s+(?P<task>TB-\d+):"
)
_REJECTED_RE = re.compile(
    r"^-\s+\S+\s+—\s+rejected ideation proposal\s+→\s+(?P<task>TB-\d+)"
)
# Cursor: matches `<ts> — ran audit (...)` where the parens body is
# free-form (e.g. `(3 unreviewed)` or `(reviewed 2, skipped 1,
# deferred 0)`). We capture the ts so the caller can use it as the
# "since" window boundary.
_RAN_AUDIT_RE = re.compile(
    r"^-\s+(?P<ts>\S+)\s+—\s+ran audit\s+\(.*\)\s*$"
)


@dataclass
class UnreviewedTask:
    """One row in the `ap2 audit` output table.

    All fields are best-effort: a task with no `task_complete` event in
    the events.jsonl tail (e.g. an ancient task whose event aged out of
    the daemon's tail window) falls back to `completed_at=""` /
    `summary=""` / `commit=""`. The CLI renders missing fields as `(?)`
    so the operator sees them as data-incomplete rather than silently
    missing rows.
    """

    task_id: str
    status: str  # "Complete" | "Frozen"
    commit: str = ""
    auto_approved: bool = False
    summary: str = ""
    completed_at: str = ""  # ISO-8601 UTC; "" when unknown
    briefing_path: str = ""


def parse_audit_cursor(cfg: Config) -> str | None:
    """Return the timestamp of the most recent `ran audit (...)` line in
    operator_log.md, or `None` when no such line exists (first-ever
    invocation; cursor = epoch).

    Scans the whole file linearly — operator_log.md is small enough
    that this is trivial, and reading fresh on each call sidesteps any
    cache-coherence question with a separate state file (the design
    promise: single source of truth = operator_log.md).
    """
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text()
    except OSError:
        return None
    last_ts: str | None = None
    for line in text.splitlines():
        m = _RAN_AUDIT_RE.match(line)
        if m:
            last_ts = m.group("ts")
    return last_ts


def parse_reviewed_set(cfg: Config) -> set[str]:
    """Return the set of TB-N ids the operator has already weighed in on
    via `classified` / `audit-skipped` / `rejected` lines in
    operator_log.md.

    Union of three signals — each reflects a distinct operator
    decision shape:
      - `classified TB-N`: explicit retrospective impact verdict (TB-189).
      - `audit-skipped TB-N`: operator considered the task during an
        audit walk and chose not to record a verdict (TB-248).
      - `rejected TB-N`: operator rejected the ideation proposal before
        it ever shipped (TB-152) — included so a rejected-then-deleted
        task doesn't show up as unreviewed if it somehow lands back in
        the Complete section (defensive; the reject path also removes
        the row, so this branch primarily covers replayed-audit cases).
    """
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    if not log_path.exists():
        return set()
    try:
        text = log_path.read_text()
    except OSError:
        return set()
    reviewed: set[str] = set()
    for line in text.splitlines():
        for pat in (_CLASSIFIED_RE, _AUDIT_SKIPPED_RE, _REJECTED_RE):
            m = pat.match(line)
            if m:
                reviewed.add(m.group("task"))
                break
    return reviewed


def _events_index(cfg: Config) -> dict[str, dict]:
    """Build a `{task_id: {auto_approved, completed_at, commit, summary}}`
    map from a tail of events.jsonl.

    Reads up to 5000 recent events — enough to cover the typical
    multi-month operator window without scanning the full file on every
    invocation. The audit is an operator-pulled action; the cost of a
    5000-line tail is negligible compared to the operator's reaction
    time. Tasks whose events have aged out of the window get partial
    data (the CLI renders missing fields as `(?)`).
    """
    tail = events.tail(cfg.events_file, 5000)
    by_task: dict[str, dict] = {}
    for evt in tail:
        tid = str(evt.get("task") or "")
        if not tid:
            continue
        slot = by_task.setdefault(tid, {})
        typ = evt.get("type")
        if typ == "auto_approved":
            slot["auto_approved"] = True
        elif typ == "ideation_approved":
            # TB-248: an explicit operator approve event flips the
            # auto-approved bucket off — symmetry with daemon's
            # `_was_auto_approved` rule. Last writer wins (the tail
            # is ordered chronologically; we process in order).
            slot["auto_approved"] = False
        elif typ == "task_complete":
            status = str(evt.get("status") or "")
            # Track the most recent terminal task_complete (status in
            # {"complete", "verification_failed", "pipeline_pending",
            # "state_violation"}); the LAST one in the tail wins —
            # retries / re-runs naturally overwrite earlier attempts.
            slot["completed_at"] = str(evt.get("ts") or "")
            slot["commit"] = str(evt.get("commit") or "")
            slot["summary"] = str(evt.get("summary") or "")
            slot["task_complete_status"] = status
    return by_task


def list_unreviewed(
    cfg: Config,
    *,
    since: str | None = None,
    frozen_only: bool = False,
    auto_approved_only: bool = False,
) -> list[UnreviewedTask]:
    """Return the unreviewed shipped tasks (Complete + Frozen) in
    chronological completion order (oldest first).

    `since` overrides the natural cursor derivation. When `None`, the
    most recent `ran audit (...)` line in operator_log.md is used (and
    when no such line exists, the cursor defaults to the empty string —
    i.e. all shipped tasks are returned).

    `frozen_only` restricts to the Frozen section (operator triaging
    the freeze pile). `auto_approved_only` restricts to tasks the
    daemon auto-promoted via the `AP2_AUTO_APPROVE` path (the natural
    "after walk-away" review filter). Both default to False, meaning
    ALL unreviewed Complete + Frozen tasks are returned — the
    chronological-by-default UX.
    """
    cursor = since if since is not None else (parse_audit_cursor(cfg) or "")
    reviewed = parse_reviewed_set(cfg)
    by_task = _events_index(cfg)

    board = Board.load(cfg.tasks_file)
    sections = ["Frozen"] if frozen_only else ["Complete", "Frozen"]

    rows: list[UnreviewedTask] = []
    for section in sections:
        for task in board.iter_tasks(section):
            if task.id in reviewed:
                continue
            slot = by_task.get(task.id, {})
            completed_at = slot.get("completed_at", "") or ""
            # Cursor compare: ISO-8601 UTC sorts lexically. Tasks
            # without a known completion timestamp (event aged out)
            # are kept under the natural-cursor path (we can't prove
            # they were completed BEFORE the cursor, and surfacing
            # them once is strictly better than silently dropping
            # them; the operator can `[s]kip` if uninterested).
            if cursor and completed_at and completed_at <= cursor:
                continue
            auto_flag = bool(slot.get("auto_approved", False))
            if auto_approved_only and not auto_flag:
                continue
            rows.append(
                UnreviewedTask(
                    task_id=task.id,
                    status=section,
                    commit=str(slot.get("commit", "") or ""),
                    auto_approved=auto_flag,
                    summary=str(slot.get("summary", "") or ""),
                    completed_at=completed_at,
                    briefing_path=str(task.briefing or ""),
                )
            )

    # Sort by completion timestamp (oldest first). Tasks without a
    # timestamp sort to the end via the empty-string fallback — they
    # surface, but after the dated rows so the chronological-by-default
    # UX holds for the common case.
    rows.sort(key=lambda r: (r.completed_at == "", r.completed_at, r.task_id))
    return rows


def format_table(rows: list[UnreviewedTask]) -> str:
    """Render `rows` as a fixed-column table for human stdout consumption.

    Column order matches the briefing's Scope §1: `TB-N | status |
    commit | auto_approved | one-line summary | completed_at`. Summary
    is truncated to 60 chars so the row fits a typical 120-column
    terminal even when the other columns are at their max width.
    """
    if not rows:
        return ""
    header = ("TB-ID", "status", "commit", "auto?", "summary", "completed_at")
    body: list[tuple[str, str, str, str, str, str]] = []
    for r in rows:
        body.append(
            (
                r.task_id,
                r.status,
                (r.commit or "")[:7] or "(?)",
                "yes" if r.auto_approved else "no",
                (r.summary or "(?)")[:60],
                r.completed_at or "(?)",
            )
        )
    widths = [max(len(header[i]), max(len(row[i]) for row in body))
              for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*("-" * w for w in widths))]
    for row in body:
        lines.append(fmt.format(*row))
    return "\n".join(lines)
