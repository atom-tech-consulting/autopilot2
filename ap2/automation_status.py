"""TB-227: operator-facing aggregator for the auto-approve / auto-unfreeze
state machine shipped in TB-223 / TB-224 / TB-225.

Walk-away promise (goal.md L28-29) requires the operator's first-touch
surfaces — `ap2 status` (text + JSON) and the web home page — to expose
loop health at a glance. Before TB-227 those surfaces returned empty on
`grep -n auto_approve`: operators had to run `ap2 logs` to learn whether
auto-approve was enabled, whether it had paused, how close to the
freeze-threshold the streak was, and how much window-token spend had
accumulated against the cap.

This module is pure-function and isolates the events.jsonl tail-scan
from `ap2/cli.py` and `ap2/web.py` (neither should grow event-walking
inline). The daemon owns the live state machinery (TB-223 / TB-224 / TB-225);
this module replays the same scans the daemon does, plus the operator-
facing aggregates the daemon doesn't compute (24h event counts,
consecutive-freeze streak length, pause-reason discriminator).

Single public symbol: `collect_auto_approve_state(cfg, *, now=None,
window_s=86400) -> dict`. The returned dict's shape is the JSON contract
surfaced by `ap2 status --json` and consumed by `_render_automation_card`
on the web home page. All keys are always present (machine consumers
get a stable shape regardless of knob-state); text and HTML rendering
decide whether to display zero / disabled rows.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import TYPE_CHECKING

from . import events

if TYPE_CHECKING:
    from .config import Config


# Aliased here so refactors that rename the daemon-side tokens trip a
# focused import-error rather than a silent miss in this aggregator.
_UNFREEZE_TOKEN = "auto_approve_unfreeze"
_WINDOW_RESUME_TOKEN = "auto_approve_window_resume"

_FAILURE_STATUSES: frozenset[str] = frozenset(
    {"verification_failed", "blocked", "error", "failed"},
)


# pause_reason discriminator: maps the most recent halt-class event to
# one of four operator-facing tokens. Two distinct event types feed in:
#   - `auto_approve_paused` (TB-223 cumulative-regression) is single-
#     reason → "consecutive_freezes".
#   - `auto_approve_halted` (TB-224 cost/blast-radius) carries its own
#     `reason` discriminator (per_task_cap / window_cap / task_error)
#     which we surface verbatim modulo a friendlier rename.
# Renamed forms align with the briefing's explicit token vocabulary so
# text + JSON + web all share one string for each cause.
_HALT_REASON_RENAME: dict[str, str] = {
    "per_task_cap": "per_task_token_cap_exceeded",
    "window_cap": "window_token_cap_exceeded",
    "task_error": "task_error",
}


def _is_truthy(raw: str | None) -> bool:
    """Same truthy-set as `ideation._is_auto_approve_enabled` (TB-223).

    Aliased here rather than imported to keep this module's import graph
    free of `ap2.ideation` (which pulls in board / events / goal —
    overkill for a status aggregator that the CLI and web both import
    on every request).
    """
    return (raw or "").strip() in ("1", "true", "yes")


def _freeze_threshold() -> int:
    """Effective `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, mirroring
    `daemon._auto_approve_freeze_threshold`.

    Returns the default (3) on unset / non-int. `<= 0` is preserved as-is
    (operator opt-out: 0 / negative effectively disables the
    circuit-breaker) so the surfaced number matches what the daemon's
    check sees — surfacing a "default 3" when the operator explicitly
    set `0` would mislead the reader.
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "").strip()
    if not raw:
        return 3
    try:
        return int(raw)
    except ValueError:
        return 3


def _positive_int_cap(env_name: str) -> int | None:
    """Parse a non-negative-integer cap env knob the same way
    `daemon._per_task_token_cap` / `_window_token_cap` does, but return
    `None` for "cap disabled" instead of `0` so the JSON surface can
    distinguish "operator hasn't budgeted" from "operator set cap = 0".
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _parse_event_ts(ts: object) -> float | None:
    """Parse an event `ts` field (ISO8601 with `Z` suffix) to epoch
    seconds; `None` on parse failure (mirrors
    `daemon._parse_event_ts`). Defensive so a single malformed line in
    events.jsonl doesn't break the aggregator."""
    if not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return _dt.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _event_combined_tokens(event: dict) -> int:
    """Combined `input_tokens + output_tokens` from a `task_run_usage`
    event's `usage` blob (TB-165 schema). Same shape as
    `daemon._event_combined_tokens`; aliased here so this module can
    stand alone without importing daemon."""
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return 0
    inp = int(usage.get("input_tokens", 0) or 0)
    outp = int(usage.get("output_tokens", 0) or 0)
    return inp + outp


def _auto_approved_task_ids(tail: list[dict]) -> set[str]:
    """TB-Ns ideation auto-approved within `tail`, with subsequent
    `ideation_approved` events removing them (operator's explicit
    approval overrides the auto stamp — mirrors
    `daemon._auto_approved_task_ids` exactly). Materialized as a set so
    per-task scans below are O(1)."""
    auto: set[str] = set()
    for e in tail:
        tid = str(e.get("task") or "").strip()
        if not tid:
            continue
        typ = e.get("type")
        if typ == "auto_approved":
            auto.add(tid)
        elif typ == "ideation_approved":
            auto.discard(tid)
    return auto


def _consecutive_freezes(tail: list[dict], unfreeze_idx: int) -> int:
    """Length of the current consecutive-failure streak among
    `task_complete` events since the last `auto_approve_unfreeze` ack.

    Walks `task_complete` events forward in the post-ack slice and
    counts the trailing streak (any non-failure status resets the
    counter). Naming pinned by the briefing's `consecutive_freezes`
    JSON key — "freezes" reads operator-naturally even though
    `task_complete status=verification_failed` is the dominant failure
    shape (not all of these end up `retry_exhausted`'d; the pause
    decision still requires the final completion to be followed by a
    `retry_exhausted`, but the *streak count* surfaces the precursor
    signal so operators see "2 of 3 freezes" before the trip).
    """
    relevant = tail[unfreeze_idx + 1:]
    streak = 0
    for e in relevant:
        if e.get("type") != "task_complete":
            continue
        status = str(e.get("status", "")).strip()
        if status in _FAILURE_STATUSES:
            streak += 1
        else:
            streak = 0
    return streak


def _window_tokens_used(
    tail: list[dict],
    *,
    resume_idx: int,
    auto_ids: set[str],
    now_s: float,
    window_s: int,
) -> int:
    """Cumulative input+output tokens for auto-approved
    `task_run_usage` events whose `ts` lies within `window_s` of `now_s`
    AND whose index is past the last `auto_approve_window_resume` ack.

    Same arithmetic as `daemon._auto_approve_check_violations`'s
    window-cap branch — extracted here so the surface read can match
    the daemon's decision without re-running the violation check (which
    short-circuits on `task_error` / `per_task_cap` first).
    """
    relevant = tail[resume_idx + 1:]
    total = 0
    for e in relevant:
        if e.get("type") != "task_run_usage":
            continue
        tid = str(e.get("task") or "").strip()
        if not tid or tid not in auto_ids:
            continue
        ts = _parse_event_ts(e.get("ts"))
        if ts is None:
            continue
        if now_s - ts > window_s:
            continue
        total += _event_combined_tokens(e)
    return total


def _count_events_24h(
    tail: list[dict],
    *,
    event_type: str,
    now_s: float,
    window_s: int,
) -> int:
    """Count events of `event_type` whose `ts` lies within `window_s`
    of `now_s`. Single-pass scan over the tail. Events with malformed /
    missing `ts` are skipped (defensive — same shape as
    `_window_tokens_used`)."""
    count = 0
    for e in tail:
        if e.get("type") != event_type:
            continue
        ts = _parse_event_ts(e.get("ts"))
        if ts is None:
            continue
        if now_s - ts <= window_s:
            count += 1
    return count


def _pause_reason(
    tail: list[dict],
    *,
    unfreeze_idx: int,
    resume_idx: int,
) -> str | None:
    """Discriminate the most recent halt-class event since its
    respective ack idx.

    Two halt-class events share the auto-promote-paused state but ack
    via distinct tokens (TB-223 → `auto_approve_unfreeze`; TB-224 →
    `auto_approve_window_resume`). The pause reason is the most recent
    event of either kind that is past its ack idx. Returns `None` when
    no halt-class event is in-effect (i.e. the daemon's auto-promote
    isn't currently paused on either axis).
    """
    latest_idx = -1
    latest_reason: str | None = None
    for i, e in enumerate(tail):
        typ = e.get("type")
        if typ == "auto_approve_paused" and i > unfreeze_idx:
            if i > latest_idx:
                latest_idx = i
                latest_reason = "consecutive_freezes"
        elif typ == "auto_approve_halted" and i > resume_idx:
            if i > latest_idx:
                latest_idx = i
                raw = str(e.get("reason") or "").strip()
                latest_reason = _HALT_REASON_RENAME.get(raw)
    return latest_reason


def collect_auto_approve_state(
    cfg: "Config",
    *,
    now: _dt.datetime | None = None,
    window_s: int = 86400,
) -> dict:
    """Aggregate the auto-approve / auto-unfreeze loop's operator-facing
    state into a single structured dict.

    Keys (always present, machine consumers can rely on the shape
    regardless of knob-state):

      - `auto_approve_enabled` (bool) — `AP2_AUTO_APPROVE` truthy.
      - `auto_approve_paused`  (bool) — auto-promote is currently
        halted by any of the four halt conditions (TB-223 freeze
        threshold OR TB-224 per-task / window / task_error).
      - `consecutive_freezes`  (int)  — current count of trailing
        `task_complete` failure-status events since the last
        `auto_approve_unfreeze` ack. Resets to 0 at the first
        non-failure completion in the streak.
      - `freeze_threshold`     (int)  — effective
        `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`. `<= 0` means the
        circuit-breaker is operator-disabled.
      - `per_task_token_cap`   (int|None) — effective
        `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`. `None` when unset / `0`.
      - `window_token_cap`     (int|None) — effective
        `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`. `None` when unset / `0`.
      - `window_tokens_used`   (int)  — cumulative input+output tokens
        across auto-approved tasks in the rolling `window_s` window,
        since the last `auto_approve_window_resume` ack.
      - `auto_approved_count_24h` (int)
      - `auto_unfreeze_applied_count_24h` (int)
      - `auto_unfreeze_skipped_count_24h` (int)
      - `pause_reason` (str|None) — one of `"consecutive_freezes"`,
        `"per_task_token_cap_exceeded"`,
        `"window_token_cap_exceeded"`, `"task_error"`, or `None` when
        not currently paused.

    `now` (default `datetime.now(UTC)`) and `window_s` are kwargs to
    keep the helper testable without `freezegun` — tests can pass a
    pinned `now` and a small `window_s` to exercise the 24h-counter
    edge cases.

    Pure / no I/O beyond reading `cfg.events_file`; safe to call from
    either CLI or web request handlers without taking the board lock.
    """
    enabled = _is_truthy(os.environ.get("AP2_AUTO_APPROVE"))
    threshold = _freeze_threshold()
    per_task_cap = _positive_int_cap("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP")
    window_cap = _positive_int_cap("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP")

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    now_s = now.timestamp()

    # 2000-event tail comfortably covers >24h of typical activity
    # (matches the daemon's window-cap scan in
    # `_auto_approve_check_violations`). Bounded by the file size on
    # fresh projects; an empty events file short-circuits below.
    if cfg.events_file.exists():
        tail = events.tail(cfg.events_file, 2000)
    else:
        tail = []

    unfreeze_idx = -1
    resume_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") != "operator_ack":
            continue
        note = str(e.get("note") or "")
        if _UNFREEZE_TOKEN in note:
            unfreeze_idx = i
        if _WINDOW_RESUME_TOKEN in note:
            resume_idx = i

    consecutive = _consecutive_freezes(tail, unfreeze_idx)
    auto_ids = _auto_approved_task_ids(tail)
    window_used = _window_tokens_used(
        tail,
        resume_idx=resume_idx,
        auto_ids=auto_ids,
        now_s=now_s,
        window_s=window_s,
    )

    auto_approved_24h = _count_events_24h(
        tail, event_type="auto_approved", now_s=now_s, window_s=window_s,
    )
    unfreeze_applied_24h = _count_events_24h(
        tail, event_type="auto_unfreeze_applied",
        now_s=now_s, window_s=window_s,
    )
    unfreeze_skipped_24h = _count_events_24h(
        tail, event_type="auto_unfreeze_skipped",
        now_s=now_s, window_s=window_s,
    )

    pause_reason = _pause_reason(
        tail, unfreeze_idx=unfreeze_idx, resume_idx=resume_idx,
    )
    paused = pause_reason is not None

    return {
        "auto_approve_enabled": enabled,
        "auto_approve_paused": paused,
        "consecutive_freezes": consecutive,
        "freeze_threshold": threshold,
        "per_task_token_cap": per_task_cap,
        "window_token_cap": window_cap,
        "window_tokens_used": window_used,
        "auto_approved_count_24h": auto_approved_24h,
        "auto_unfreeze_applied_count_24h": unfreeze_applied_24h,
        "auto_unfreeze_skipped_count_24h": unfreeze_skipped_24h,
        "pause_reason": pause_reason,
    }
