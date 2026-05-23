"""Proactive `attention_raised` detector surface (TB-282).

Closes goal.md focus-1's Done-when bullet on shallow monitoring
("Attention-needing conditions ... are surfaced proactively in
operator-legible terms, distinct from routine progress updates").

Pre-TB-282 the periodic 2h status-report cron post was the ONLY push
surface; a stuck Active task at minute 5 of a 2h window waited up to
2h to surface, and routine progress bullets visually outweighed the
embedded attention signal when it did. The shipped pull-surfaces
(`ap2 status`'s `[noisy]` badge, web `/automation` card) required the
operator to remember to look — contradicting a walk-away monitoring
promise.

Design split (axis-by-axis, mirrors TB-263):

  - This module is PURE detection. `detect_attention_conditions(cfg)`
    returns a list of `AttentionCondition` records; it never appends
    events itself. Keeps the function trivially testable without
    spinning up an events file.
  - The daemon's `_tick` (call sites in `daemon._tick`) owns the
    debounce-vs-tail check + `attention_raised` event emission. That
    keeps the side-effects in one place where the surrounding
    operator-queue / focus-advance / cron pipeline already lives.

Seeded with ONE detector: `task_stuck`. Future detectors land here as
new functions following the same shape (`def _detect_<name>(cfg, tail)
-> list[AttentionCondition]`) and are added to
`_DETECTORS`. The briefing's Out-of-scope clause names the obvious
follow-ups (validator-judge noisy, cost-cap approach, decisions-needed-
new, frozen-task recency) — each its own focused task to keep this one
landable.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Any

from . import events
from .board import Board
from .config import (
    Config,
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_TASK_STUCK_THRESHOLD_S,
)


# Event types that close out a `task_start` for the stuck-task detector.
# A task with any of these intervening events past its most recent
# `task_start` is NOT stuck — its run terminated even if the board
# section hasn't been updated yet.
_TERMINAL_TASK_EVENT_TYPES: frozenset[str] = frozenset({
    "task_complete",
    "task_failed",
    "verification_failed",
    "retry_exhausted",
})


@dataclass
class AttentionCondition:
    """One detector hit — a condition the operator needs to see.

    Fields:
      type     Short detector identifier (e.g. `task_stuck`). Maps 1:1
               to the event-payload `type` field and to the dedup key
               family the debounce check uses.
      key      Per-condition dedup key. For `task_stuck` it's
               `f"task_stuck:{task_id}"` so a second stuck task is NOT
               suppressed because a first one fired recently — debounce
               is per-(type, key), not per-type.
      summary  Operator-legible one-line summary the renderer surfaces.
               Pre-rendered here (not at status-report time) so the
               daemon's `attention_raised` event payload carries
               everything an event-stream reader needs without a
               separate Board lookup.
      ts       The condition's anchor timestamp — for `task_stuck`,
               the `ts` of the most recent `task_start`. Used by the
               renderer to compute "Active for Nh since <ts>".
      extras   Per-detector extension blob inlined into the event
               payload under the standard contract (caller-side
               `events.append(..., **cond.extras)` semantics live in
               the daemon wire-up).
    """

    type: str
    key: str
    summary: str
    ts: str
    extras: dict[str, Any] = field(default_factory=dict)


def _task_stuck_threshold_s() -> int:
    """Resolve `AP2_TASK_STUCK_THRESHOLD_S` with the documented default
    + invalid-value fallback. Read fresh from `os.environ` per call so
    the env-reload helper's mid-run knob refresh takes effect on the
    next detector tick without re-threading state.
    """
    raw = os.environ.get("AP2_TASK_STUCK_THRESHOLD_S", "")
    if not raw:
        return DEFAULT_TASK_STUCK_THRESHOLD_S
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TASK_STUCK_THRESHOLD_S
    if val <= 0:
        return DEFAULT_TASK_STUCK_THRESHOLD_S
    return val


def _attention_debounce_s() -> int:
    """Resolve `AP2_ATTENTION_DEBOUNCE_S` with the documented default
    + invalid-value fallback. Mirrors `_task_stuck_threshold_s` —
    same fresh-read-each-call semantics so env-reload propagates.
    """
    raw = os.environ.get("AP2_ATTENTION_DEBOUNCE_S", "")
    if not raw:
        return DEFAULT_ATTENTION_DEBOUNCE_S
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ATTENTION_DEBOUNCE_S
    if val <= 0:
        return DEFAULT_ATTENTION_DEBOUNCE_S
    return val


def _parse_ts(ts: str | None) -> _dt.datetime | None:
    """Parse the event log's `YYYY-MM-DDTHH:MM:SSZ` timestamp shape.

    Returns None on any parse miss so the caller can skip the event
    rather than raise — a malformed `ts` in events.jsonl must never
    take the detector down.
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc,
        )
    except (TypeError, ValueError):
        return None


def _detect_task_stuck(
    cfg: Config,
    *,
    tail: list[dict],
    now: _dt.datetime,
) -> list[AttentionCondition]:
    """Return one `AttentionCondition` per Active task whose most-recent
    `task_start` is older than `AP2_TASK_STUCK_THRESHOLD_S` AND has no
    intervening terminal event (`task_complete`, `task_failed`,
    `verification_failed`, `retry_exhausted`).

    Active section is the source-of-truth for "currently dispatched";
    we read the board fresh per call so a task that finished between
    ticks but hasn't been moved yet (rare — the daemon moves on the
    same tick) doesn't false-fire.

    Tail-driven freshness: scan the tail in reverse to find each
    task's most recent `task_start`; abort early on a terminal event
    for that task (means the dispatched run already closed). The tail
    is the daemon's standard reverse-chronological event slice.
    """
    if not cfg.tasks_file.exists():
        return []
    try:
        board = Board.load(cfg.tasks_file)
    except Exception:  # noqa: BLE001
        return []

    active_ids = [t.id for t in board.iter_tasks("Active")]
    if not active_ids:
        return []

    threshold_s = _task_stuck_threshold_s()
    out: list[AttentionCondition] = []
    for task_id in active_ids:
        # Walk the tail in reverse looking for either a terminal event
        # for this task (→ NOT stuck) or a `task_start` (→ candidate).
        # Terminal-first stop semantics: if a terminal event landed
        # after the most recent `task_start`, the run already closed.
        start_ts: str | None = None
        for ev in reversed(tail):
            if (ev.get("task") or "").strip() != task_id:
                continue
            typ = ev.get("type", "")
            if typ in _TERMINAL_TASK_EVENT_TYPES:
                # Terminal event closes the run — not stuck. Even if
                # an older `task_start` precedes this terminal event,
                # the dispatch closed and the board section hasn't
                # been moved YET (drift between sections and events).
                start_ts = None
                break
            if typ == "task_start":
                start_ts = ev.get("ts") or ""
                break

        if not start_ts:
            continue
        start_dt = _parse_ts(start_ts)
        if start_dt is None:
            continue
        age_s = (now - start_dt).total_seconds()
        if age_s < threshold_s:
            continue

        # Title resolution for the operator-legible summary. Best-effort —
        # a board parse miss here returns the empty string, and the
        # caller's renderer substitutes a stable placeholder.
        title = ""
        task_obj = board.get(task_id)
        if task_obj is not None:
            title = task_obj.title or ""

        age_h = age_s / 3600.0
        summary = (
            f"{task_id} Active for {age_h:.1f}h since {start_ts}"
        )
        out.append(AttentionCondition(
            type="task_stuck",
            key=f"task_stuck:{task_id}",
            summary=summary,
            ts=start_ts,
            extras={
                "task": task_id,
                "title": title,
                "age_s": int(age_s),
                "start_ts": start_ts,
                "threshold_s": threshold_s,
            },
        ))
    return out


def detect_attention_conditions(
    cfg: Config,
    *,
    tail: list[dict] | None = None,
    now: _dt.datetime | None = None,
) -> list[AttentionCondition]:
    """Run every registered attention detector and return the union.

    Pure read-only: reads `cfg.tasks_file` + `cfg.events_file` (via
    `events.tail`), never appends events. The daemon wire-up
    (`_maybe_emit_attention_events` in `daemon.py`) owns the debounce
    check + `attention_raised` event emission so the detector stays
    trivially testable.

    `tail` and `now` are injection points the test pins drive with
    synthetic values; production passes None and the function reads
    the live tail / wall-clock.
    """
    if tail is None:
        tail = (
            events.tail(cfg.events_file, 2000)
            if cfg.events_file.exists() else []
        )
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    out: list[AttentionCondition] = []
    out.extend(_detect_task_stuck(cfg, tail=tail, now=now))
    return out


def find_last_attention_fire(
    tail: list[dict],
    *,
    type_: str,
    key: str,
) -> dict | None:
    """Return the most recent `attention_raised` event in `tail` whose
    payload `attention_type` AND `key` match the caller's, or None.

    Per-(attention_type, key) lookup so a second stuck task doesn't
    get suppressed because a different stuck task fired recently — the
    briefing's load-bearing debounce contract. Scans in reverse for
    cheap-first short-circuit on the freshest match.

    Why `attention_type` (not `type`): the event-log's top-level
    `type` slot is reserved for the event class (`attention_raised`
    itself); piggybacking the detector's identifier on the same field
    would collide. Every payload reads `attention_type=<detector>` so
    a downstream filter pass over events.jsonl can pivot on the
    detector kind without parsing the `key` prefix.
    """
    for ev in reversed(tail):
        if ev.get("type") != "attention_raised":
            continue
        if (ev.get("attention_type") or "").strip() != type_:
            continue
        if (ev.get("key") or "").strip() != key:
            continue
        return ev
    return None


def should_suppress(
    cond: AttentionCondition,
    *,
    tail: list[dict],
    now: _dt.datetime,
    debounce_s: int | None = None,
) -> bool:
    """Return True iff the most recent matching `attention_raised`
    event in `tail` is within `AP2_ATTENTION_DEBOUNCE_S` of `now`.

    Per-(type, key) debounce, mirrors the briefing's contract. The
    daemon wire-up calls this before emitting `attention_raised` so a
    still-stuck task that was already surfaced 10 minutes ago doesn't
    re-fire every tick.
    """
    if debounce_s is None:
        debounce_s = _attention_debounce_s()
    prior = find_last_attention_fire(tail, type_=cond.type, key=cond.key)
    if prior is None:
        return False
    prior_dt = _parse_ts(prior.get("ts"))
    if prior_dt is None:
        return False
    age_s = (now - prior_dt).total_seconds()
    return age_s < debounce_s
