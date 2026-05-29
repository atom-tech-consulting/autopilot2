"""Proactive `attention_raised` detector surface (TB-282).

TB-315 (axis 5): module body relocated from `ap2/attention.py` to
`ap2/components/attention/__init__.py`. The detector layer + the
daemon-side wire-up (`_maybe_emit_attention_events`, `_maybe_push_attention`,
and the immediate-push state helpers) now live intra-package; core
reaches them through the registry's `hook_points` rather than a flat
`from ap2 import attention` import (the TB-311 import-direction gate
forbids static `from ap2.components.attention import …` in core).

Closes goal.md focus-1's Done-when bullet on shallow monitoring
("Attention-needing conditions ... are surfaced proactively in
operator-legible terms, distinct from routine progress updates").

Pre-TB-282 the periodic status-report cron post was the ONLY push
surface; a stuck Active task at minute 5 of a cron-interval window
waited up to the next tick to surface, and routine progress bullets
visually outweighed the embedded attention signal when it did. The shipped pull-surfaces
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
per-task); TB-289 added `auto_approve_paused` (any non-None
`pause_reason` from `collect_auto_approve_state` — per-reason
condition keyed `auto_approve_paused:<reason>`, surfaces the
`ap2 ack <verb>` resume nudge so the operator's first-touch
walk-away channel carries the pending decision proactively rather
than only as a TB-228 automation-digest sub-block line); TB-290
added `cost_cap_approach` (singleton pre-trip companion to the
post-trip `auto_approve_paused:window_token_cap_exceeded`
surface — rolling 24h auto-approved token sum
`>= AP2_AUTO_APPROVE_COST_APPROACH_PCT * AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`
AND strictly below the cap, so the walk-away operator gets a
budget-spending nudge hours before dispatch halts and they must
`ap2 ack auto_approve_window_resume`). Future detectors land here
as new functions following the same shape
(`def _detect_<name>(cfg, *, tail, now) -> list[AttentionCondition]`)
and are added to `detect_attention_conditions`'s `out.extend(...)`
list.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ap2 import events
from ap2.components.auto_approve import (
    _AUTO_APPROVE_WINDOW_S,
    _auto_approve_window_resume_idx,
    _auto_approved_task_ids,
    _event_combined_tokens,
    _parse_event_ts,
    _window_token_cap,
)
from ap2.automation_status import (
    _PAUSE_REASON_ACK_VERB,
    _count_events_24h as _automation_count_events_24h,
    collect_auto_approve_state,
    validator_judge_noisy_threshold,
)
from ap2.board import Board
from ap2.config import (
    Config,
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT,
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


def _task_stuck_threshold_s(cfg: Config) -> int:
    """Resolve the `task_stuck_threshold_s` knob with the documented
    default + invalid-value fallback.

    Resolution shape (TB-328 axis-5): routes through
    `cfg.get_component_value("attention", "task_stuck_threshold_s")` so
    the legacy flat env name `AP2_TASK_STUCK_THRESHOLD_S` still wins via
    the `FLAT_TO_SECTIONED` reverse-lookup back-compat path while a
    `[components.attention] task_stuck_threshold_s = N` TOML value flows
    through the same call when no env override is live. Call-time env-
    first precedence inside `get_component_value` preserves the
    pre-migration lazy-read pattern so the env-reload helper's mid-run
    knob refresh still takes effect on the next detector tick without
    re-threading state.
    """
    raw = cfg.get_component_value("attention", "task_stuck_threshold_s")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_TASK_STUCK_THRESHOLD_S
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TASK_STUCK_THRESHOLD_S
    if val <= 0:
        return DEFAULT_TASK_STUCK_THRESHOLD_S
    return val


def _task_frozen_recency_s(cfg: Config) -> int:
    """Resolve the `task_frozen_recency_s` knob with the documented
    default + invalid-value fallback (TB-287). Mirrors
    `_task_stuck_threshold_s` — same call-time env-first precedence so
    env-reload propagates without re-threading state.

    Resolution shape (TB-328 axis-5): routes through
    `cfg.get_component_value("attention", "task_frozen_recency_s")`; the
    flat env name `AP2_TASK_FROZEN_RECENCY_S` still wins via the
    `FLAT_TO_SECTIONED` reverse-lookup back-compat path while a
    `[components.attention] task_frozen_recency_s = N` TOML value flows
    through the same call when no env override is live.
    """
    raw = cfg.get_component_value("attention", "task_frozen_recency_s")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_TASK_FROZEN_RECENCY_S
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TASK_FROZEN_RECENCY_S
    if val <= 0:
        return DEFAULT_TASK_FROZEN_RECENCY_S
    return val


def _cost_approach_pct() -> int:
    """Resolve `AP2_AUTO_APPROVE_COST_APPROACH_PCT` with the documented
    default + invalid-value fallback (TB-290). Mirrors
    `_task_frozen_recency_s` — fresh-read-each-call from `os.environ`
    so env-reload propagates without re-threading state.

    Clamp semantics: any non-int / empty / negative value falls back
    to the default (`DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT`, 75).
    Values >= 100 are clamped to 99 — a 100% approach threshold would
    coincide with the trip line (which the post-trip
    `auto_approve_paused` detector owns), so a value >= 100 means
    "trip-not-approach" and the detector caps it just below the trip
    to avoid the double-bullet noise the briefing's Design clause
    pins. 0 is allowed (operator wants to fire on any auto-approved
    token spend while the cap is set) but is unusual; the cap itself
    (`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`) is the operator-facing
    opt-in.
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_COST_APPROACH_PCT", "")
    if not raw:
        return DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT
    if val < 0:
        return DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT
    if val >= 100:
        return 99
    return val


def _attention_debounce_s(cfg: Config) -> int:
    """Resolve the per-(type, key) debounce knob with the documented
    default + invalid-value fallback. Mirrors `_task_stuck_threshold_s`
    — same call-time env-first precedence so env-reload propagates.

    Resolution shape (TB-328 axis-5): routes through
    `cfg.get_component_value("attention", "debounce_s")`; the legacy
    flat env name `AP2_ATTENTION_DEBOUNCE_S` still wins via the
    `FLAT_TO_SECTIONED` reverse-lookup back-compat path while a
    `[components.attention] debounce_s = N` TOML value flows through the
    same call when no env override is live.
    """
    raw = cfg.get_component_value("attention", "debounce_s")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
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

    threshold_s = _task_stuck_threshold_s(cfg)
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

    recency_s = _task_frozen_recency_s(cfg)
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
    cfg: Config,
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
    threshold = validator_judge_noisy_threshold(cfg)
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


def _detect_auto_approve_paused(
    cfg: Config,
    *,
    tail: list[dict],  # noqa: ARG001 — accepted for parity with sibling detectors
    now: _dt.datetime,
) -> list[AttentionCondition]:
    """Return a SINGLE `AttentionCondition` (zero or one element) when
    `collect_auto_approve_state(cfg).pause_reason` is non-None (TB-289).

    Today's pause reasons: `consecutive_freezes` (TB-223 cumulative-
    freeze trip) and `validator_judge_noisy` (TB-272 safety-floor
    failure); future reasons registered via `_PAUSE_REASON_ACK_VERB`
    in `ap2/automation_status.py` (e.g. `per_task_token_cap_exceeded`
    / `window_token_cap_exceeded` / `task_error` from TB-224's
    cost/blast-radius halts) flow through here without code changes
    on this side — the detector reads the discriminator the
    aggregator already computes.

    Closes the "pending decision" leg of goal.md Current focus #3
    Progress signal #3: pre-TB-289 an active auto-approve pause
    surfaced ONLY as a TB-228 automation-digest sub-block line near
    the bottom of the status-report (`auto-approve: disabled
    (paused: <reason>)`) and as a single line in `ap2 status` text/
    JSON / the web automation card. The operator must scroll past the
    headline + 4-8 routine bullets + automation digest header to find
    the pause line — and must already know to run
    `ap2 ack auto_approve_unfreeze` (or `auto_approve_window_resume`
    for the cost halts) to resume. Promoting it to a `## Attention
    needed` bullet at the TOP of the status-report post closes the
    visual-hierarchy gap: a paused auto-approve IS a pending decision
    (the operator's `ack` is the only path back to dispatch) so it
    belongs in the proactive surface "distinct from routine progress
    updates", as goal.md L207-209 names.

    Per-reason dedup (`key=f"auto_approve_paused:{pause_reason}"`) so a
    sequential reason transition (e.g. `consecutive_freezes` → operator
    acks → `validator_judge_noisy` fires later) surfaces both
    bullets — they are distinct conditions with distinct ack verbs in
    the general case. The `should_suppress` check is per-(type, key),
    so a recently-fired `consecutive_freezes` pause won't suppress a
    fresh `validator_judge_noisy` pause.

    Independent of (and additive to) TB-272's pause logic and TB-228's
    automation-digest line — both remain. Read-only against
    `automation_status.collect_auto_approve_state`; the daemon's
    pause-state machinery is untouched.

    The detector is a no-op when `pause_reason is None`. A pause
    reason without a registered ack verb (defensive: unreachable
    today, every reason in `_PAUSE_REASON_ACK_VERB` resolves) skips
    the bullet rather than rendering with a missing verb — the
    bottom-of-digest TB-228 line + `ap2 status` pause text still
    surface the state so the operator isn't left without a signal.

    The anchor `ts` is `now` formatted as ISO-8601 — pause state is a
    point-in-time fact about the current aggregator output, not a
    timestamped event, so there's no upstream `ts` field to inherit.
    Renderers (`render_attention_section`) consume the pre-rendered
    `summary` verbatim via the generic fallback path; no per-detector
    branch is needed in the renderer.
    """
    try:
        state = collect_auto_approve_state(cfg, now=now)
    except Exception:  # noqa: BLE001 — never break the detector loop
        return []

    pause_reason = state.get("pause_reason")
    if not pause_reason:
        return []

    ack_verb = _PAUSE_REASON_ACK_VERB.get(pause_reason)
    if not ack_verb:
        # Defensive: a future pause_reason that landed in
        # `collect_auto_approve_state` without a corresponding
        # `_PAUSE_REASON_ACK_VERB` entry would render with a missing
        # verb. Skip the bullet rather than mislead the operator;
        # the TB-228 sub-block + `ap2 status` still carry the state.
        return []

    anchor_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = (
        f"auto-approve paused: {pause_reason}; "
        f"resume via `ap2 ack {ack_verb}`"
    )
    return [AttentionCondition(
        type="auto_approve_paused",
        key=f"auto_approve_paused:{pause_reason}",
        summary=summary,
        ts=anchor_ts,
        extras={
            "pause_reason": pause_reason,
            "ack_verb": ack_verb,
            "consecutive_freezes": int(
                state.get("consecutive_freezes") or 0,
            ),
            "validator_judge_fail_count_24h": int(
                state.get("validator_judge_fail_count_24h") or 0,
            ),
            "validator_judge_timeout_count_24h": int(
                state.get("validator_judge_timeout_count_24h") or 0,
            ),
        },
    )]


def _detect_cost_cap_approach(
    cfg: Config,
    *,
    tail: list[dict],
    now: _dt.datetime,
) -> list[AttentionCondition]:
    """Return a SINGLETON `AttentionCondition` (zero or one element) when
    the rolling 24h auto-approved `task_run_usage` token sum is
    `>= AP2_AUTO_APPROVE_COST_APPROACH_PCT * AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`
    (the approach floor) AND strictly below
    `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` (the trip line — handed off to
    the post-trip `auto_approve_paused` surface) (TB-290).

    Pre-trip companion to the post-trip `auto_approve_paused` detector
    (TB-289) for the `window_token_cap_exceeded` pause reason. The
    post-trip surface fires only once dispatch has already halted and
    the operator must `ap2 ack auto_approve_window_resume` to resume;
    this detector gives the walk-away operator a budget-spending nudge
    hours earlier so they can raise the cap or pause proactively. Closes
    the pre-trip path of goal.md Current focus #3 Progress signal #3
    "cost or validator-judge anomalies".

    Walk shape matches `auto_approve._auto_approve_check_violations`'s
    window-cap branch (auto_approve.py L442-463) verbatim — same
    `_auto_approve_window_resume_idx` reset, same `_auto_approved_task_ids`
    filter, same `_AUTO_APPROVE_WINDOW_S` 24h roll, same
    `_event_combined_tokens` sum, same `_parse_event_ts` ts gate. Drift
    between the approach-check sum and the trip-check sum would mean
    an Attention bullet that doesn't predict the eventual pause; we
    reuse the existing helpers to keep that no-drift property
    structural rather than just commented.

    Singleton (not per-task / per-event): the approach state is a
    project-wide property of the rolling-24h auto-approved spend,
    not a per-task condition. One condition per debounce window via
    the per-(type, key) `should_suppress` check.

    No-op branches (early returns to `[]`):
      - `cap <= 0` — operator hasn't set `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`,
        so there's no approach state to surface (parallel to the TB-224
        trip-check's "operators who haven't budgeted their project don't
        get a hardcoded cap surprising them" design).
      - `total < threshold` — below the approach floor, no bullet yet.
      - `total >= cap` — trip line already crossed; the post-trip
        `auto_approve_paused` surface fires here and a second
        "approach" bullet would be double-noise. Explicit hand-off
        (`>=` not `>`) to keep the boundary clean even though the
        trip-check uses `>` (at `total == cap` the trip doesn't fire
        either, but we still hand off — the approach detector's job is
        to surface BEFORE the cap, not AT it).

    Anchor `ts` = the freshest in-window `task_run_usage` event's `ts`
    (fall back to `now` on no parseable ts) — same defensive shape the
    sibling singleton `_detect_validator_judge_noisy` uses.
    """
    cap = _window_token_cap(cfg)
    if cap <= 0:
        return []
    approach_pct = _cost_approach_pct()
    # Exact integer-arithmetic threshold check: `total >= pct * cap / 100`
    # rewritten as `total * 100 >= approach_pct * cap` to avoid
    # floor-division surprises near the boundary (e.g. cap=1000,
    # pct=75 → threshold floor 750; sum=750 must fire).
    if not tail:
        return []
    resume_idx = _auto_approve_window_resume_idx(tail)
    relevant = tail[resume_idx + 1:]
    if not relevant:
        return []
    auto_ids = _auto_approved_task_ids(tail)
    if not auto_ids:
        return []
    now_s = now.timestamp()
    total = 0
    freshest_dt: _dt.datetime | None = None
    freshest_ts: str | None = None
    for ev in relevant:
        if ev.get("type") != "task_run_usage":
            continue
        tid = str(ev.get("task") or "").strip()
        if not tid or tid not in auto_ids:
            continue
        ts_s = _parse_event_ts(ev.get("ts"))
        if ts_s is None:
            continue
        if now_s - ts_s > _AUTO_APPROVE_WINDOW_S:
            continue
        total += _event_combined_tokens(ev)
        ts_dt = _parse_ts(ev.get("ts"))
        if ts_dt is not None and (freshest_dt is None or ts_dt > freshest_dt):
            freshest_dt = ts_dt
            freshest_ts = ev.get("ts")

    # Threshold check (`total >= pct * cap / 100`) in integer form so
    # the boundary is exact regardless of rounding.
    if total * 100 < approach_pct * cap:
        return []
    # Hand off to the post-trip surface above the cap.
    if total >= cap:
        return []

    pct_used = (total / cap) * 100.0
    anchor_ts = freshest_ts or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = (
        f"auto-approve cost cap approach: {total} tokens used in last 24h, "
        f"{pct_used:.0f}% of window cap {cap} (threshold {approach_pct}%); "
        f"consider raising AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP or pausing via "
        f"ap2 ack auto_approve_window_resume"
    )
    return [AttentionCondition(
        type="cost_cap_approach",
        key="cost_cap_approach:window",
        summary=summary,
        ts=anchor_ts,
        extras={
            "total_tokens_24h": total,
            "window_cap": cap,
            "approach_pct": approach_pct,
            "pct_used": pct_used,
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
    out.extend(_detect_auto_approve_paused(cfg, tail=tail, now=now))
    out.extend(_detect_cost_cap_approach(cfg, tail=tail, now=now))
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
    cfg: Config | None = None,
) -> bool:
    """Return True iff the most recent matching `attention_raised`
    event in `tail` is within the resolved attention debounce window
    of `now`.

    Per-(type, key) debounce, mirrors the briefing's contract. The
    daemon wire-up calls this before emitting `attention_raised` so a
    still-stuck task that was already surfaced 10 minutes ago doesn't
    re-fire every tick.

    TB-328 axis-5: when `debounce_s` is left unset, the value is
    resolved through `_attention_debounce_s(cfg)` which routes the read
    via `Config.get_component_value("attention", "debounce_s")`. The
    `cfg` kwarg is required only on that fallback branch — callers that
    pre-resolve the debounce (tests pinning explicit values) may keep
    passing `debounce_s` without cfg. Raises `TypeError` when both are
    None so a refactor that drops cfg from `_maybe_emit_attention_events`
    surfaces here instead of silently re-introducing an `os.environ`
    read.
    """
    if debounce_s is None:
        if cfg is None:
            raise TypeError(
                "should_suppress requires either `debounce_s` or `cfg` "
                "to be passed; the TB-328 cfg-side read path needs cfg "
                "to resolve `attention.debounce_s`."
            )
        debounce_s = _attention_debounce_s(cfg)
    prior = find_last_attention_fire(tail, type_=cond.type, key=cond.key)
    if prior is None:
        return False
    prior_dt = _parse_ts(prior.get("ts"))
    if prior_dt is None:
        return False
    age_s = (now - prior_dt).total_seconds()
    return age_s < debounce_s


# ---------------------------------------------------------------------------
# Daemon-side wire-up (TB-315 axis 5 relocation).
#
# Pre-TB-315 these helpers lived in `ap2/daemon.py` and the manifest's
# tick hook late-bound to `daemon._maybe_emit_attention_events`. Post-
# TB-315 the body lives intra-package so the manifest can source it
# via `from . import _maybe_emit_attention_events` rather than a
# late-binding `from ap2 import daemon as _daemon_mod`, and the
# daemon's module-level aliases resolve through
# `default_registry().get("attention").hook_points[…]` (the TB-311
# import-direction gate forbids static `from ap2.components.attention
# import …` in core).
# ---------------------------------------------------------------------------


def _maybe_emit_attention_events(cfg: Config) -> None:
    """Per-tick attention-detector → `attention_raised` event wire-up
    (TB-282; relocated from `daemon.py` by TB-315).

    Runs `detect_attention_conditions(cfg)`, debounces each candidate
    against the most recent matching `attention_raised` event in the
    tail (suppress when last fire was within
    `AP2_ATTENTION_DEBOUNCE_S`), and emits one `attention_raised`
    event per fresh condition.

    Per-(attention_type, key) debounce so a second stuck task doesn't
    get suppressed because a different stuck task fired recently —
    the briefing's load-bearing contract.

    Best-effort by design: a detector / events-emit hiccup must not
    take the tick down. The manifest's `_tick_hook` wraps this in
    try/except too; this helper only swallows the inner detector
    exceptions so a known-broken detector returns no candidates
    instead of raising.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        candidates = detect_attention_conditions(cfg, now=now)
    except Exception:  # noqa: BLE001 — never break the tick on detector bugs
        return
    if not candidates:
        return
    # Single tail read shared across all candidates' debounce checks
    # to keep the per-tick cost bounded (the candidate count for any
    # one detector is small — Active section length — but a future
    # extension that registers 5+ detectors would otherwise read the
    # tail 5+ times per tick).
    tail = (
        events.tail(cfg.events_file, 2000)
        if cfg.events_file.exists() else []
    )
    for cond in candidates:
        if should_suppress(cond, tail=tail, now=now, cfg=cfg):
            continue
        try:
            events.append(
                cfg.events_file,
                "attention_raised",
                attention_type=cond.type,
                key=cond.key,
                summary=cond.summary,
                **cond.extras,
            )
        except Exception:  # noqa: BLE001
            # An events-emit failure on one candidate must not block
            # the others — keep iterating.
            continue
        # TB-297: opt-in immediate-Mattermost-push. Runs AFTER the
        # `attention_raised` event has been appended so the push
        # piggybacks structurally on the existing per-(type, key)
        # debounce — a still-active condition that just got its
        # `attention_raised` fire will not get a second push until
        # the next debounce-window-elapsed fresh `attention_raised`
        # emits. Best-effort: a push hiccup must not abort the tick
        # or the rest of the candidate iteration.
        try:
            _maybe_push_attention(cfg, cond)
        except Exception:  # noqa: BLE001
            # Defensive — the helper already catches `_mm_post`
            # failures and emits `attention_push_error`; this outer
            # guard catches a surprise in the helper's own plumbing
            # (state-file write, environ read). The rest of the
            # candidate loop must continue regardless.
            continue


def _attention_push_state_path(cfg: Config) -> Path:
    """Return the per-project `attention_push_state.json` path (TB-297).

    Inline-computed (mirroring `goal._focus_pointer_path` /
    `focus_pointer.json`'s home) rather than threaded through `Config`
    — the state is a single sticky boolean (`warned_no_destination`)
    that no other module reads, so a Config field would be paperwork
    for a one-key file. Gitignored via
    `init.NESTED_GITIGNORE_BLOCKS`'s runtime-state block: an
    `ap2 rollback` should NOT resurrect a stale "we already warned"
    flag — a fresh daemon should re-warn against a freshly-misconfigured
    env file.
    """
    return cfg.project_root / ".cc-autopilot" / "attention_push_state.json"


def _load_attention_push_state(cfg: Config) -> dict:
    """Load the TB-297 push state file. Returns `{}` on missing /
    corrupt — defensive parse mirrors `_load_diagnose_state`.
    """
    path = _attention_push_state_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_attention_push_state(cfg: Config, state: dict) -> None:
    path = _attention_push_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _is_attention_immediate_push_enabled(cfg: Config) -> bool:
    """Parse the `immediate_push` knob at push-decision time.

    Read fresh from the resolved-config layer (NOT cached on `Config`)
    so a hot-reload of the env file flips the knob on the very next
    tick. Truthy set mirrors the sibling `AP2_FOCUS_AUTO_ADVANCE_DISABLED`
    style: `1` / `true` / `yes` / `on` (case-insensitive). Anything
    else, including the unset case, is false (conservative default per
    goal.md Non-goals L253-256). The TOML layer's typed `True` / `False`
    is also honored when an operator opts into the structured-config
    path.

    Resolution shape (TB-328 axis-5): routes through
    `cfg.get_component_value("attention", "immediate_push")`; the legacy
    flat env name `AP2_ATTENTION_IMMEDIATE_PUSH` still wins via the
    `FLAT_TO_SECTIONED` reverse-lookup back-compat path while a
    `[components.attention] immediate_push = true` TOML value flows
    through the same call when no env override is live. Call-time env-
    first precedence inside `get_component_value` preserves the
    pre-migration lazy-read pattern so an operator toggling the knob
    on/off takes effect on the next tick without a daemon restart.
    """
    raw = cfg.get_component_value("attention", "immediate_push")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _maybe_push_attention(cfg: Config, cond) -> None:
    """Post a one-line Mattermost message for a freshly-emitted
    `attention_raised` condition (TB-297; relocated from `daemon.py`
    by TB-315).

    Called from `_maybe_emit_attention_events` AFTER the
    `attention_raised` event has been appended for `cond`. Per-(type,
    key) debounce piggybacks structurally on the existing
    `attention_raised` debounce (the push runs only when a fresh event
    appended, which already honors `AP2_ATTENTION_DEBOUNCE_S`) — no
    new state file is needed for push-debounce bookkeeping.

    Conservative-default opt-in via `AP2_ATTENTION_IMMEDIATE_PUSH`:
    when unset / falsy, returns without posting. Operators flip the
    knob once they've sampled their own detector cadence and confirmed
    it's low enough not to noise the channel.

    Missing-destination handling mirrors the watchdog's
    `warned_no_destination` pattern: when `AP2_MM_CHANNELS` is unset
    so `_first_mm_channel()` returns "", emit ONE
    `attention_push_no_destination` audit event then sticky-suppress
    further such audits via a state-file flag — the sticky flag resets
    to `False` on a successful post so a destination that returns can
    re-warn on its next gap.

    Best-effort by design: on `_mm_post` failure emit an
    `attention_push_error` audit event and return (caller's outer
    try/except wraps this so an unexpected helper-plumbing failure
    also doesn't abort the tick). Success emits an
    `attention_pushed` audit event the status-report skip-gate
    consults (the event is in
    `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` so a fresh push
    un-skips the dedup/idle gate).
    """
    # Lazy imports for the watchdog / registry callouts so import-time
    # cycles stay quiet — the subpackage is discovered before
    # `ap2.watchdog` is imported by the daemon, but the registry lookup
    # below resolves only at call time.
    from ap2.registry import default_registry
    from ap2.watchdog import _first_mm_channel

    if not _is_attention_immediate_push_enabled(cfg):
        return

    # TB-312: route through `_deliver(cfg, text, **meta)` so the
    # destination is owned by the registered channel-adapter list
    # rather than hard-coded to Mattermost. `_first_mm_channel()`
    # still drives the no-destination warning shape — pre-TB-312
    # behavior preserved for operators who only have Mattermost
    # wired: when `AP2_MM_CHANNELS` is unset the mattermost
    # component is disabled by its `env_flag`, so
    # `channel_adapters(cfg)` returns [] and the sticky-warning
    # branch fires per the existing contract.
    channel = _first_mm_channel()
    adapters = default_registry().channel_adapters(cfg)
    if not adapters:
        # No destination configured. Emit one sticky audit event then
        # suppress further such audits via the state-file flag, parity
        # with the watchdog's no-destination short-circuit.
        state = _load_attention_push_state(cfg)
        if not state.get("warned_no_destination"):
            try:
                events.append(
                    cfg.events_file,
                    "attention_push_no_destination",
                    reason="AP2_MM_CHANNELS unset",
                    attention_type=cond.type,
                    key=cond.key,
                )
            except Exception:  # noqa: BLE001
                # Events-append plumbing surprise must not break the
                # tick. Skip the state-file flip too so the warning
                # can re-fire on the next push attempt.
                return
            state["warned_no_destination"] = True
            try:
                _save_attention_push_state(cfg, state)
            except OSError:
                # Best-effort: a state-write hiccup is acceptable; the
                # event already went to events.jsonl which is the
                # operator's primary audit surface.
                pass
        return

    # Compose the one-line post. Project-name prefix is looked up at
    # call-time from `cfg.project_name` (the existing helper the
    # status-report cron uses — TB-280) so a project rename
    # propagates immediately without duplicating that helper here.
    text = f"[{cfg.project_name}] ⚠ {cond.summary}"
    any_success = False
    for adapter in adapters:
        try:
            outcome = adapter.post(text, channel=channel)
        except Exception as e:  # noqa: BLE001
            try:
                events.append(
                    cfg.events_file,
                    "attention_push_error",
                    channel=channel,
                    attention_type=cond.type,
                    key=cond.key,
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:  # noqa: BLE001
                # Defensive — if even the audit-event append fails, swallow
                # so the tick continues. The remaining adapters in the
                # list still get their chance.
                pass
            continue
        if outcome is None:
            # Adapter is unconfigured (e.g. webhook with empty
            # AP2_WEBHOOK_URL). Skip — not an error, not a success.
            continue
        post_id = outcome.get("post_id", "") if isinstance(outcome, dict) else ""
        try:
            events.append(
                cfg.events_file,
                "attention_pushed",
                attention_type=cond.type,
                key=cond.key,
                channel=outcome.get("channel", channel) if isinstance(outcome, dict) else channel,
                post_id=post_id,
                summary=cond.summary,
            )
        except Exception:  # noqa: BLE001
            # The post DID happen; the audit-event append failed. The
            # operator already received the Mattermost line so the
            # observable behavior is intact — silently continue.
            pass
        any_success = True

    if not any_success:
        # All adapters failed (or all were unconfigured). The error
        # path already emitted per-adapter `attention_push_error`
        # events; nothing more to do — the sticky-flag reset below
        # is gated on `any_success` so a fully-failing dispatch
        # doesn't reset the no-destination flag inappropriately.
        return
    # Destination is back — reset the sticky no-destination flag so
    # a future env-config gap re-warns. Matches the watchdog's
    # `state["warned_no_destination"] = False` post-success reset.
    state = _load_attention_push_state(cfg)
    if state.get("warned_no_destination"):
        state["warned_no_destination"] = False
        try:
            _save_attention_push_state(cfg, state)
        except OSError:
            pass
