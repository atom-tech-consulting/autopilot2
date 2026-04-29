"""Self-diagnose report (TB-71) — daemon's idle-watchdog informant.

Pure inspection module: reads the board, events.jsonl tail, cron state, and
retry counters; returns a structured report. No SDK calls, no agent — fully
deterministic so a stalled daemon can still produce one.

The report is consumed by `daemon._maybe_auto_diagnose`, which posts the
markdown rendering to mattermost when the watchdog fires. It can also be
invoked directly for ad-hoc inspection (e.g. an `ap2 diagnose` subcommand —
out of scope here, but the public API is shaped for it).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cron as cron_mod
from . import events
from .board import Board
from .config import Config


# Events that count as "the daemon did something". Anything outside this set
# is treated as background noise (idle ticks, error logs that already exist
# elsewhere). Triggering a meaningful event resets `since_last_activity_s`.
#
# `daemon_start` and `daemon_resume` are deliberately included so that an
# operator restarting/resuming the daemon (e.g. stoch coming back from its
# pause) doesn't cause a spurious watchdog fire on the first post-resume tick
# even if the previous session's last meaningful event was hours old.
#
# `auto_diagnose_fired` is included so that consecutive idle ticks don't
# keep retriggering immediately after a fire (the cooldown is the primary
# guard, but this makes the math cleaner).
MEANINGFUL_EVENT_TYPES = frozenset({
    "task_start",
    "task_complete",
    "task_implicit_commit",
    "task_rollback",  # TB-111
    "cron_complete",
    "cron_start",
    "mattermost",
    "mattermost_reply",
    "backlog_auto_promoted",
    "ideation_empty_board",
    "auto_diagnose_fired",
    "daemon_start",
    "daemon_resume",
    "orphan_recovery",
})


# Failure-class events surfaced in `recent_failures`. Distinct from the
# meaningful set because failures don't count as "the daemon making progress"
# — many of these can fire repeatedly while the daemon is effectively stuck.
FAILURE_EVENT_TYPES = frozenset({
    "task_error",
    "task_timeout",
    "task_state_violation",  # TB-110
    "retry_exhausted",
    "rollback_error",
    "state_commit_error",
    "verification_failed",  # TB-66 + TB-69
    "board_malformed_line",  # TB-68
    "cron_proposal_rejected",
    "cron_proposal_error",
    "cron_error",
    "cron_timeout",
    "mattermost_timeout",
    "mattermost_error",
    "mm_poll_error",
    "ideation_error",
    "ideation_timeout",
    "auto_diagnose_post_error",
})


@dataclass
class DiagnoseReport:
    project_root: Path
    timestamp: str
    since_last_activity_s: int | None
    last_meaningful_event: dict | None
    board_summary: dict[str, Any]
    recent_failures: list[dict]
    cron_status: list[dict]
    board_health: dict[str, Any]


def build_report(
    cfg: Config,
    *,
    events_window: int = 500,
    recent_failure_limit: int = 5,
    now: float | None = None,
) -> DiagnoseReport:
    """Inspect the project state and return a structured diagnose report."""
    import time as _time

    if now is None:
        now = _time.time()

    events_tail = events.tail(cfg.events_file, n=events_window)

    last_meaningful = _last_meaningful_event(events_tail)
    if last_meaningful is None:
        since_s: int | None = None
    else:
        ts = _parse_event_ts(last_meaningful.get("ts", ""))
        since_s = max(0, int(now - ts)) if ts is not None else None

    board = Board.load(cfg.tasks_file) if cfg.tasks_file.exists() else None

    return DiagnoseReport(
        project_root=cfg.project_root,
        timestamp=_iso(now),
        since_last_activity_s=since_s,
        last_meaningful_event=last_meaningful,
        board_summary=_board_summary(board),
        recent_failures=_recent_failures(events_tail, recent_failure_limit),
        cron_status=_cron_status(cfg, now),
        board_health=_board_health(board, events_tail),
    )


def render_markdown(report: DiagnoseReport) -> str:
    """Render a compact MM-friendly summary (target <2k chars)."""
    bs = report.board_summary
    counts = (
        f"A:{bs.get('Active', 0)} R:{bs.get('Ready', 0)} "
        f"B:{bs.get('Backlog', 0)} F:{bs.get('Frozen', 0)} "
        f"C:{bs.get('Complete', 0)}"
    )
    idle_str = (
        _pretty_duration(report.since_last_activity_s)
        if report.since_last_activity_s is not None
        else "(no meaningful events yet)"
    )

    lines: list[str] = [
        f"**ap2 watchdog** — `{report.project_root.name}` idle for {idle_str}",
        "",
        f"**Board:** {counts}",
    ]
    active_id = bs.get("active_task")
    if active_id:
        lines.append(f"**Active:** {active_id}")

    health = report.board_health
    health_lines: list[str] = []
    if health.get("malformed_lines"):
        health_lines.append(
            f"- malformed task lines: {len(health['malformed_lines'])} "
            f"(see `board_malformed_line` events)"
        )
    if health.get("unsatisfiable_blocks"):
        ids = ", ".join(health["unsatisfiable_blocks"][:5])
        health_lines.append(f"- unsatisfiable Backlog blockers: {ids}")
    if health.get("frozen_retry_exhausted"):
        ids = ", ".join(health["frozen_retry_exhausted"][:5])
        health_lines.append(f"- Frozen via retry_exhausted: {ids}")
    if health_lines:
        lines.append("")
        lines.append("**Health:**")
        lines.extend(health_lines)

    overdue = [c for c in report.cron_status if c.get("overdue")]
    if overdue:
        lines.append("")
        lines.append("**Overdue crons:** " + ", ".join(c["name"] for c in overdue))

    if report.recent_failures:
        lines.append("")
        lines.append("**Recent failures (most recent last):**")
        for f in report.recent_failures:
            ts = f.get("ts", "")
            typ = f.get("type", "?")
            extras = " ".join(
                f"{k}={_short(v)}"
                for k, v in f.items()
                if k not in ("ts", "type")
            )
            lines.append(f"- `{ts}` {typ} {extras}".rstrip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers


def _last_meaningful_event(events_tail: list[dict]) -> dict | None:
    """Most recent event whose type is in `MEANINGFUL_EVENT_TYPES`.

    `events.tail` returns oldest-first, so we walk in reverse to short-circuit
    on the most recent meaningful event.
    """
    for evt in reversed(events_tail):
        if evt.get("type") in MEANINGFUL_EVENT_TYPES:
            return evt
    return None


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z?$")


def _parse_event_ts(s: str) -> float | None:
    """Parse the events-module timestamp format (`%Y-%m-%dT%H:%M:%SZ`)."""
    if not s:
        return None
    m = _TS_RE.match(s)
    if not m:
        return None
    try:
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return ts.replace(tzinfo=dt.timezone.utc).timestamp()


def _iso(epoch_s: float) -> str:
    return (
        dt.datetime.fromtimestamp(epoch_s, tz=dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _board_summary(board: Board | None) -> dict[str, Any]:
    if board is None:
        return {"Active": 0, "Ready": 0, "Backlog": 0,
                "Complete": 0, "Frozen": 0, "active_task": None}
    out: dict[str, Any] = {}
    for s in ("Active", "Ready", "Backlog", "Complete", "Frozen"):
        out[s] = sum(1 for _ in board.iter_tasks(s))
    active = next(board.iter_tasks("Active"), None)
    out["active_task"] = active.id if active else None
    return out


def _recent_failures(events_tail: list[dict], limit: int) -> list[dict]:
    """Last `limit` failure-class events. Keeps the chronological order
    (oldest-first within the window) so the rendered list reads naturally."""
    failures = [e for e in events_tail if e.get("type") in FAILURE_EVENT_TYPES]
    return failures[-limit:] if len(failures) > limit else failures


def _cron_status(cfg: Config, now: float) -> list[dict]:
    """Per-job timing status: name, interval_s, last_fired, overdue."""
    if not cfg.cron_file.exists():
        return []
    try:
        jobs = cron_mod.load_jobs(cfg.cron_file)
    except Exception:
        return []
    state = cron_mod.load_state(cfg.cron_state_file)
    out: list[dict] = []
    for j in jobs:
        last = state.get(j.name, 0.0) or 0.0
        # Overdue if no last-fired AND been alive long enough; or last_fired
        # is more than 2 intervals ago. "More than 2x" rather than "1x past
        # the interval" so a normal scheduling jitter doesn't flip it.
        overdue = bool(last and (now - last) > j.interval_s * 2)
        out.append({
            "name": j.name,
            "interval_s": j.interval_s,
            "last_fired": int(last) if last else None,
            "seconds_since_last": int(now - last) if last else None,
            "overdue": overdue,
        })
    return out


def _board_health(board: Board | None, events_tail: list[dict]) -> dict[str, Any]:
    if board is None:
        return {"malformed_lines": [],
                "unsatisfiable_blocks": [],
                "frozen_retry_exhausted": []}

    completed = board.completed_ids()
    frozen_ids = {t.id for t in board.iter_tasks("Frozen")}
    unsatisfiable: list[str] = []
    for t in board.iter_tasks("Backlog"):
        for blocker in t.blocked_on:
            if blocker not in completed and blocker in frozen_ids:
                unsatisfiable.append(t.id)
                break
            # A blocker that doesn't exist anywhere on the board is also
            # unsatisfiable — there's no way for it to ever land in Complete.
            if blocker not in completed and board.find(blocker) is None:
                unsatisfiable.append(t.id)
                break

    # Frozen tasks paired with a recent retry_exhausted event for the same id.
    exhausted_ids = {
        e.get("task")
        for e in events_tail
        if e.get("type") == "retry_exhausted" and e.get("task")
    }
    frozen_exhausted = sorted(frozen_ids & exhausted_ids)

    return {
        "malformed_lines": list(board.malformed_lines),
        "unsatisfiable_blocks": unsatisfiable,
        "frozen_retry_exhausted": frozen_exhausted,
    }


def _pretty_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, m = divmod(seconds, 3600)
    return f"{h}h{(m // 60):02d}m" if m // 60 else f"{h}h"


def _short(v: Any, limit: int = 100) -> str:
    s = str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"
