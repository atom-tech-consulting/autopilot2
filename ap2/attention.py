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

Seeded with `task_stuck`; TB-287 added `task_frozen` (a fresh entry
into the Frozen section within `AP2_TASK_FROZEN_RECENCY_S` and no
intervening operator-driven `task_unfrozen` / `task_deleted` event);
TB-288 added `validator_judge_noisy` (rolling 24h sum of
`validator_judge_fail` + `validator_judge_timeout` events ≥
`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` — singleton condition, not
per-task). Future detectors land here as new functions following the
same shape (`def _detect_<name>(cfg, *, tail, now) ->
list[AttentionCondition]`) and are added to
`detect_attention_conditions`'s `out.extend(...)` list. The briefing's
Out-of-scope clause names the remaining obvious follow-ups (cost-cap
approach, decisions-needed-new) — each its own focused task to keep
this module landable.
"""
from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Any

from . import events
from .automation_status import (
    _count_events_24h as _automation_count_events_24h,
    validator_judge_noisy_threshold,
)
from .board import Board
from .config import (
    Config,
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_TASK_FROZEN_RECENCY_S,
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

# TB-287: events that mark a task's *entry* into the Frozen section.
# `retry_exhausted` is the daemon's `_handle_failure` route after
# `AP2_MAX_RETRIES` attempts; `task_failed` covers the manual-route
# freezes. The `task_frozen` detector identifies the most-recent
# freeze-entry event for each Frozen task and times its recency
# against `AP2_TASK_FROZEN_RECENCY_S` — distinct from
# `_TERMINAL_TASK_EVENT_TYPES` above which is `task_stuck`'s
# closed-run guard (task_complete + verification_failed belong there
# but NOT to freeze-entry).
_FREEZE_ENTRY_EVENT_TYPES: frozenset[str] = frozenset({
    "retry_exhausted",
    "task_failed",
})

# TB-287: operator-driven events that close out a freeze-recency
# window. A `task_frozen` candidate stops being a candidate as soon as
# the operator unfreezes or deletes the task — even before the next
# tick rewrites the board section. The walk in `_detect_task_frozen`
# aborts early when either event lands AFTER the freeze-entry event
# (chronologically later) so a still-Frozen board row with a stale
# `retry_exhausted` event doesn't surface during the brief window
# before the daemon's operator-queue drain moves it.
_FREEZE_RESOLVED_EVENT_TYPES: frozenset[str] = frozenset({
    "task_unfrozen",
    "task_deleted",
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


def _task_frozen_recency_s() -> int:
    """Resolve `AP2_TASK_FROZEN_RECENCY_S` with the documented default
    + invalid-value fallback (TB-287). Mirrors `_task_stuck_threshold_s`
    — fresh-read-each-call so env-reload propagates without re-
    threading state.
    """
    raw = os.environ.get("AP2_TASK_FROZEN_RECENCY_S", "")
    if not raw:
        return DEFAULT_TASK_FROZEN_RECENCY_S
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TASK_FROZEN_RECENCY_S
    if val <= 0:
        return DEFAULT_TASK_FROZEN_RECENCY_S
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


def _detect_task_frozen(
    cfg: Config,
    *,
    tail: list[dict],
    now: _dt.datetime,
) -> list[AttentionCondition]:
    """Return one `AttentionCondition` per Frozen task whose entry-into-
    Frozen timestamp (the most-recent `retry_exhausted` / `task_failed`
    event for that task) is within `AP2_TASK_FROZEN_RECENCY_S`
    AND has no intervening operator-driven `task_unfrozen` /
    `task_deleted` event (TB-287).

    Frozen section is the source-of-truth for "currently parked";
    we read the board fresh per call so a task that the operator just
    unfroze (queue ack pending drain) doesn't false-fire — the
    intervening-unfreeze guard catches the same race the
    `task_stuck` detector handles with `_TERMINAL_TASK_EVENT_TYPES`.

    Closes the "frozen tasks" leg of goal.md's Current focus #3:
    pre-TB-287 the only Frozen surface was the `3F` aggregate count
    on `ap2 status` / status-report headline; a walk-away operator
    saw the count tick up but got no proactive `ap2 unfreeze` nudge.
    """
    if not cfg.tasks_file.exists():
        return []
    try:
        board = Board.load(cfg.tasks_file)
    except Exception:  # noqa: BLE001
        return []

    frozen_ids = [t.id for t in board.iter_tasks("Frozen")]
    if not frozen_ids:
        return []

    recency_s = _task_frozen_recency_s()
    out: list[AttentionCondition] = []
    for task_id in frozen_ids:
        # Walk the tail in reverse looking for either an operator-
        # driven resolution event (→ NOT a candidate — operator
        # already acted) or a freeze-entry event (→ candidate). The
        # resolution-first stop semantics catches the brief window
        # between an `ap2 unfreeze` queue-ack landing and the drain
        # actually moving the row out of the Frozen section.
        freeze_ts: str | None = None
        for ev in reversed(tail):
            if (ev.get("task") or "").strip() != task_id:
                continue
            typ = ev.get("type", "")
            if typ in _FREEZE_RESOLVED_EVENT_TYPES:
                # Operator already acted — not a fresh-freeze candidate.
                freeze_ts = None
                break
            if typ in _FREEZE_ENTRY_EVENT_TYPES:
                freeze_ts = ev.get("ts") or ""
                break

        if not freeze_ts:
            continue
        freeze_dt = _parse_ts(freeze_ts)
        if freeze_dt is None:
            continue
        age_s = (now - freeze_dt).total_seconds()
        if age_s >= recency_s:
            # Old freeze — outside the recency window. Operator either
            # saw it on a prior tick or has been off long enough that
            # a fresh ping won't change the priority. Skip to keep the
            # walk-away-operator-friendly surface focused on freezes
            # that landed in the named window.
            continue
        if age_s < 0:
            # Clock skew or test-only future-dated event — treat as
            # "not yet a candidate" rather than raising.
            continue

        # Title resolution mirrors `_detect_task_stuck`'s best-effort
        # pattern: empty string on a board parse miss, renderer
        # substitutes a stable placeholder.
        title = ""
        task_obj = board.get(task_id)
        if task_obj is not None:
            title = task_obj.title or ""

        age_h = age_s / 3600.0
        summary = (
            f"{task_id} Frozen for {age_h:.1f}h since {freeze_ts}; "
            f"resume via `ap2 unfreeze {task_id}`"
        )
        out.append(AttentionCondition(
            type="task_frozen",
            key=f"task_frozen:{task_id}",
            summary=summary,
            ts=freeze_ts,
            extras={
                "task": task_id,
                "title": title,
                "age_s": int(age_s),
                "freeze_ts": freeze_ts,
                "recency_s": recency_s,
            },
        ))
    return out


def _detect_validator_judge_noisy(
    cfg: Config,  # noqa: ARG001 — accepted for parity with sibling detectors
    *,
    tail: list[dict],
    now: _dt.datetime,
) -> list[AttentionCondition]:
    """Return a SINGLETON `AttentionCondition` (zero or one element) when
    the rolling 24h sum
    `validator_judge_fail_count + validator_judge_timeout_count >=
    AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (TB-288).

    Closes the "validator-judge anomalies" leg of goal.md Current
    focus #3's Progress signal #3 — pre-TB-288 the noisy state
    surfaced ONLY as a pull-surface badge in `ap2 status`
    (`[noisy]` suffix; TB-243), as a bottom-of-digest sub-block in
    the status-report (TB-245), and as a warn-tint row in the web
    automation card (TB-243). Promoting it to a `## Attention
    needed` bullet at the TOP of the status-report post closes the
    visual-hierarchy gap — the operator-legible attention surface
    must be distinct from routine progress updates.

    Singleton (not per-task / per-event): the noisy state is a
    project-wide property of the gate's recent reliability, not a
    per-task condition. One `attention_raised` event per debounce
    window is the right cadence — a sustained noisy window stays
    suppressed via the per-(type, key) debounce in `should_suppress`
    until the next `AP2_ATTENTION_DEBOUNCE_S` boundary, mirroring
    `task_stuck` / `task_frozen` cadence.

    The count uses the same 24h-window walker
    (`automation_status._count_events_24h`) and the same threshold
    resolver (`automation_status.validator_judge_noisy_threshold`)
    that the `ap2 status` text/JSON, web automation card, and
    status-report sub-block already consult — drift between
    surfaces would mean the operator sees `[noisy]` in `ap2 status`
    but no Attention bullet (or vice versa). The briefing's
    "shared count logic" contract pins this no-drift property.

    Independent of TB-272's auto-approve `pause_reason` — a noisy
    state can fire here even when `AP2_AUTO_APPROVE` is OFF (the
    Attention surface is purely informational; pause behavior is
    an orthogonal axis-3 safety floor). And independent of the
    TB-243 / TB-245 surfaces — this addition is purely additive.
    """
    threshold = validator_judge_noisy_threshold()
    if threshold <= 0:
        # Defensive: the resolver normalizes non-positive values
        # back to the default (5), so this branch is effectively
        # unreachable. Kept as a belt-and-braces guard in case the
        # resolver's contract ever loosens.
        return []

    now_s = now.timestamp()
    fail_count = _automation_count_events_24h(
        tail, event_type="validator_judge_fail",
        now_s=now_s, window_s=86400,
    )
    timeout_count = _automation_count_events_24h(
        tail, event_type="validator_judge_timeout",
        now_s=now_s, window_s=86400,
    )
    total = fail_count + timeout_count
    if total < threshold:
        return []

    # Anchor timestamp = the freshest fail/timeout event in the
    # window. Same logic as the count walker but tracks the
    # most-recent `ts` so the renderer can show "noisy since <ts>"
    # if it ever extends the bullet shape. Best-effort — if no
    # in-window event has a parseable `ts` we fall back to `now`,
    # so the bullet still surfaces.
    anchor_dt: _dt.datetime | None = None
    for ev in tail:
        typ = ev.get("type")
        if typ != "validator_judge_fail" and typ != "validator_judge_timeout":
            continue
        ts_dt = _parse_ts(ev.get("ts"))
        if ts_dt is None:
            continue
        if (now - ts_dt).total_seconds() > 86400:
            continue
        if anchor_dt is None or ts_dt > anchor_dt:
            anchor_dt = ts_dt
    anchor_ts = (
        anchor_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if anchor_dt is not None
        else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    summary = (
        f"validator-judge noisy: {fail_count}+{timeout_count}={total} "
        f"fails+timeouts in last 24h (threshold {threshold}); "
        "see `ap2 status` or /usage"
    )
    return [AttentionCondition(
        type="validator_judge_noisy",
        key="validator_judge_noisy",
        summary=summary,
        ts=anchor_ts,
        extras={
            "fail_count_24h": fail_count,
            "timeout_count_24h": timeout_count,
            "threshold": threshold,
            "window_s": 86400,
        },
    )]


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
    out.extend(_detect_task_frozen(cfg, tail=tail, now=now))
    out.extend(_detect_validator_judge_noisy(cfg, tail=tail, now=now))
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
