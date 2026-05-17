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


# TB-228: ack-verb mapping used by the status-report digest. Same
# vocabulary as TB-227's CLI/web rendering — operators see one verb
# regardless of which surface flagged the halt.
_PAUSE_REASON_ACK_VERB: dict[str, str] = {
    "consecutive_freezes": "auto_approve_unfreeze",
    "per_task_token_cap_exceeded": "auto_approve_window_resume",
    "window_token_cap_exceeded": "auto_approve_window_resume",
    "task_error": "auto_approve_window_resume",
}


def _is_truthy(raw: str | None) -> bool:
    """Same truthy-set as `ideation._is_auto_approve_enabled` (TB-223).

    Aliased here rather than imported to keep this module's import graph
    free of `ap2.ideation` (which pulls in board / events / goal —
    overkill for a status aggregator that the CLI and web both import
    on every request).
    """
    return (raw or "").strip() in ("1", "true", "yes")


def _is_auto_approve_dry_run() -> bool:
    """TB-232: True iff `AP2_AUTO_APPROVE_DRY_RUN` is set to a truthy
    value.

    Monitor-only on-ramp for the `AP2_AUTO_APPROVE` master switch
    (TB-223). When both `AP2_AUTO_APPROVE=1` AND
    `AP2_AUTO_APPROVE_DRY_RUN=1` are set, the auto-approve gate chain
    (tags + freeze-threshold + token caps) still runs, but the WRITE
    step changes: instead of stripping `@blocked:review` and emitting
    `auto_approved`, the daemon emits a `would_auto_approve` audit
    event and leaves the row's `@blocked:review` codespan intact for
    operator-manual approval. The operator runs with both knobs on
    for ≥24h, reads the events to confirm the gate's decisions match
    their judgment, then unsets the dry-run knob to engage real
    dispatch.

    Mirrors `_is_truthy`'s permissive-parse shape so operators tuning
    the autopilot env file see one consistent boolean convention
    across knobs. Default unset → False (current TB-223 behavior; the
    knob has no effect when `AP2_AUTO_APPROVE` itself is unset because
    the gate chain doesn't fire at all in that case).
    """
    return _is_truthy(os.environ.get("AP2_AUTO_APPROVE_DRY_RUN"))


def _is_auto_unfreeze_dry_run() -> bool:
    """TB-238: True iff `AP2_AUTO_UNFREEZE_DRY_RUN` is set to a truthy
    value.

    Sibling to `_is_auto_approve_dry_run` (TB-232) on the axis-2
    auto-unfreeze side. The actual write-step gating lives in
    `daemon._auto_unfreeze_dry_run` (TB-233); this helper is the
    aggregator-side mirror so the operator-facing surfaces
    (`ap2 status` JSON, web home, the status-report digest) can render
    a "dry-run" badge without dragging in the daemon's import graph.
    Source-of-truth env name is identical to the daemon helper, so a
    refactor that renames the knob trips both helpers at the same
    grep.

    Same permissive truthy-set as `_is_truthy` / `_is_auto_approve_
    dry_run` so operators tuning the autopilot env file see one
    consistent boolean convention across knobs. Default unset → False
    (current TB-225 behavior; byte-identical to pre-TB-233 when the
    knob has never been set).
    """
    return _is_truthy(os.environ.get("AP2_AUTO_UNFREEZE_DRY_RUN"))


def validator_judge_noisy_threshold() -> int:
    """TB-243: effective `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`.

    When `(validator_judge_fail_count_24h + validator_judge_timeout_count_24h)
    >= threshold`, the `ap2 status` text sub-line gets a ` [noisy]`
    suffix and the web home automation card's "Validator judge (24h)"
    row gets a warn-tint class. Default 5 chosen so a single transient
    SDK blip doesn't flip the surface to warn-tint, but a sustained
    issue (>5 fails in 24h) does — same parse semantics as TB-224 /
    TB-234 token caps: unset / empty / non-int / non-positive → treat
    as default (5).

    Public (no leading `_`) so both `ap2/cli.py` and `ap2/web.py` can
    consult one source-of-truth; tests pin the parser independently.
    """
    raw = os.environ.get("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "").strip()
    if not raw:
        return 5
    try:
        v = int(raw)
    except ValueError:
        return 5
    return v if v > 0 else 5


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
      - `dry_run_enabled` (bool) — TB-232 `AP2_AUTO_APPROVE_DRY_RUN`
        truthy. Operator on-ramp: when on, the gate chain still runs
        but the WRITE step emits `would_auto_approve` instead of
        stripping `@blocked:review`. The CLI / web home / JSON
        surfaces render this as a "dry-run" badge so operators can
        confirm the loop is in monitor mode at a glance.
      - `would_auto_approve_count_24h` (int) — TB-232 rolling 24h
        count of `would_auto_approve` events (parallel to
        `auto_approved_count_24h`). Operator watches this rise during
        the dry-run window to confirm the gate is making decisions
        before flipping the dry-run knob off.
      - `auto_unfreeze_dry_run_enabled` (bool) — TB-238 sibling of
        `dry_run_enabled` on the axis-2 auto-unfreeze side.
        `AP2_AUTO_UNFREEZE_DRY_RUN` truthy. Naming note: the
        auto-approve key shipped (TB-232) without an `auto_approve_`
        prefix; the new key carries the `auto_unfreeze_` prefix to
        disambiguate when both surfaces render together (e.g. the
        status-report digest's dry-run window sub-block, which lists
        both counts in one block).
      - `would_auto_unfreeze_count_24h` (int) — TB-238 rolling 24h
        count of `would_auto_unfreeze` events (parallel to
        `would_auto_approve_count_24h`). Operator watches this rise
        during the dry-run window to confirm the auto-unfreeze gate
        is exercising decisions on the live Frozen set before
        flipping the dry-run knob off.
      - `validator_judge_fail_count_24h` (int) — TB-243 rolling 24h
        count of `validator_judge_fail` events emitted by the TB-235
        dependency-coherence judge (check #7 in
        `tools._validate_briefing_structure`). The judge fails open
        on SDK / parse errors so the briefing is admitted regardless;
        this counter surfaces the silent-degradation hazard so an
        operator with `AP2_AUTO_APPROVE=1` can notice when the gate's
        coverage is thinning. Zero on fresh / no-events projects.
      - `validator_judge_timeout_count_24h` (int) — TB-243 sibling of
        `validator_judge_fail_count_24h` for the timeout branch (judge
        SDK call exceeded `AP2_VALIDATOR_JUDGE_TIMEOUT_S`). Split from
        `_fail` so the operator can tell a flaky API (mostly
        timeouts) from a model / parse regression (mostly fails)
        without alt-tabbing to `ap2 logs`.

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
    would_auto_approve_24h = _count_events_24h(
        tail, event_type="would_auto_approve",
        now_s=now_s, window_s=window_s,
    )
    would_auto_unfreeze_24h = _count_events_24h(
        tail, event_type="would_auto_unfreeze",
        now_s=now_s, window_s=window_s,
    )
    # TB-243: validator-judge fail-open audit events (TB-235 dependency-
    # coherence judge in `tools._validate_briefing_structure` check #7).
    # Same 24h-window arithmetic as the auto-approve counters above; two
    # keys (not one combined `judge_skipped`) so the operator can tell
    # a flaky API (mostly timeouts) from a model / parse regression
    # (mostly fails) without alt-tabbing to `ap2 logs`.
    validator_judge_fail_24h = _count_events_24h(
        tail, event_type="validator_judge_fail",
        now_s=now_s, window_s=window_s,
    )
    validator_judge_timeout_24h = _count_events_24h(
        tail, event_type="validator_judge_timeout",
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
        # TB-232: monitor-only on-ramp surface. `dry_run_enabled` flips
        # the operator-facing CLI / web / JSON surfaces to render a
        # "dry-run" badge; `would_auto_approve_count_24h` is the
        # rolling counter of `would_auto_approve` events so the
        # operator can confirm the gate is exercising decisions before
        # flipping the dry-run knob off.
        "dry_run_enabled": _is_auto_approve_dry_run(),
        "would_auto_approve_count_24h": would_auto_approve_24h,
        # TB-238: auto-unfreeze sibling surface. Placed directly after
        # the TB-232 auto-approve dry-run keys so the JSON ordering
        # reflects axis-pairing (auto-approve dry-run → auto-unfreeze
        # dry-run). The status-report digest renders both counts as
        # one "dry-run window" sub-block when either knob is on.
        "auto_unfreeze_dry_run_enabled": _is_auto_unfreeze_dry_run(),
        "would_auto_unfreeze_count_24h": would_auto_unfreeze_24h,
        # TB-243: validator-judge fail-open audit counts. Surfacing
        # closes the silent-degradation hazard left by TB-235's
        # fail-open design — the auto-approve safety claim (goal.md
        # L82-85) depends on the dep-coherence gate being healthy, so
        # an operator with `AP2_AUTO_APPROVE=1` needs to see whether
        # the gate is rendering verdicts or quietly skipping. Two
        # always-present keys regardless of TB-235 knob state.
        "validator_judge_fail_count_24h": validator_judge_fail_24h,
        "validator_judge_timeout_count_24h": validator_judge_timeout_24h,
    }


# ---------------------------------------------------------------------------
# TB-228: inter-status-report-window aggregation for the cron digest block.
#
# TB-227's `collect_auto_approve_state` already exposes a 24h rolling
# view used by `ap2 status` / web home. The status-report cron needs a
# *different* window — "since the previous `cron_complete name=status-
# report` event" — so an operator returning to the Mattermost post sees
# exactly what happened between report N-1 and report N. The function
# below shares the tail-walking primitives but parameterizes the start
# index instead of `now - 24h`.
#
# Decision: keep `collect_auto_approve_state`'s contract (knob + 24h)
# untouched and add a sibling helper here. The two surfaces want
# different windows; coupling them through a single `since_event_idx`
# kwarg would force one or the other to refetch the tail to get its
# preferred window, which is wasteful.

# `auto_unfreeze_skipped` events carry a `reason` discriminator from
# `daemon._maybe_auto_unfreeze` — the operator-facing digest renders
# the breakdown so a noisy reason ("per_task_cap=12") is legible
# without alt-tabbing to `ap2 logs`.
_AUTO_UNFREEZE_SKIPPED_REASONS: frozenset[str] = frozenset({
    "shape_not_in_allowlist",
    "briefing_mismatch",
    "briefing_path_missing",
    "per_task_cap",
    "per_day_cap",
    "queue_error",
    "sweep_error",
})


def _ack_verb_for_pause_reason(reason: str | None) -> str | None:
    """Map a pause_reason token to the operator ack verb that clears it.

    Mirrors the CLI / web rendering in TB-227 so the cron digest names
    the same verb the operator sees on the other surfaces. `None` when
    not paused.
    """
    if reason is None:
        return None
    return _PAUSE_REASON_ACK_VERB.get(reason)


def collect_window_loop_activity(
    cfg: "Config",
    *,
    since_event_idx: int,
    tail: list[dict] | None = None,
) -> dict:
    """Aggregate auto-approve / auto-unfreeze loop activity in the
    inter-status-report window for TB-228's digest block.

    `since_event_idx` is the *positional* index of the previous
    `cron_complete job=status-report` event in the events tail; events
    at indices `> since_event_idx` count toward the digest. Use `-1` to
    count from the start of the tail (first-ever status report, or
    last report rolled out of the tail window).

    `tail` is passed in when the caller already has it (the routine
    walks the tail once to find `since_event_idx`); when omitted, the
    helper loads the same 2000-event tail `collect_auto_approve_state`
    uses.

    Returned dict (always present, machine-stable shape):

      - `auto_approved` (int) — count of `auto_approved` events.
      - `auto_approved_completed` (int) — of those tasks, the count
        with a subsequent `task_complete status=complete` in the
        window. Operator-facing as "M completed".
      - `auto_approved_froze` (int) — of those tasks, the count whose
        most-recent subsequent `task_complete` was a failure status.
        Operator-facing as "K froze".
      - `auto_unfreeze_applied` (int) — count of
        `auto_unfreeze_applied` events (one per shape application;
        same task may carry multiple if multiple shapes auto-fix).
      - `auto_unfreeze_tasks` (int) — distinct task_ids that had at
        least one `auto_unfreeze_applied` event. Operator-facing as
        "L tasks auto-unfrozen".
      - `auto_unfreeze_succeeded` (int) — of those L tasks, the count
        with a subsequent `task_complete status=complete`.
      - `auto_unfreeze_refroze` (int) — of those L tasks, the count
        whose most-recent subsequent `task_complete` was a failure
        status.
      - `auto_unfreeze_skipped` (int) — count of `auto_unfreeze_skipped`
        events.
      - `auto_unfreeze_skipped_by_reason` (dict[str, int]) — breakdown
        by the event's `reason` field; only non-zero buckets are
        included so the digest doesn't carry empty `per_day_cap=0`
        noise.
      - `auto_approve_paused` (int) — count of `auto_approve_paused`
        events (TB-223 cumulative-freeze trips).
      - `auto_approve_halted` (int) — count of `auto_approve_halted`
        events (TB-224 cost/blast-radius trips).
      - `latest_halt` (dict | None) — the most recent halt-class event
        in the window: `{ts, event_type, reason, ack_verb}` for digest
        rendering. `None` when no halt-class event fired.

    Pure / no I/O beyond reading `cfg.events_file` when `tail` is
    omitted; safe to call from request handlers.
    """
    if tail is None:
        if cfg.events_file.exists():
            tail = events.tail(cfg.events_file, 2000)
        else:
            tail = []

    slice_ = tail[since_event_idx + 1:] if since_event_idx >= -1 else tail

    # Indices of `auto_approved` events keyed by task_id — used below
    # to find the next `task_complete` for each auto-approved TB-N.
    auto_approve_idx: dict[str, int] = {}
    unfreeze_idx_by_task: dict[str, int] = {}

    auto_approved = 0
    auto_unfreeze_applied = 0
    auto_unfreeze_skipped = 0
    skipped_by_reason: dict[str, int] = {}
    auto_approve_paused_count = 0
    auto_approve_halted_count = 0
    latest_halt: dict | None = None
    latest_halt_idx = -1

    for i, e in enumerate(slice_):
        typ = e.get("type")
        if typ == "auto_approved":
            auto_approved += 1
            tid = str(e.get("task") or "").strip()
            if tid:
                auto_approve_idx[tid] = i
        elif typ == "auto_unfreeze_applied":
            auto_unfreeze_applied += 1
            tid = str(e.get("task") or "").strip()
            if tid:
                unfreeze_idx_by_task[tid] = i
        elif typ == "auto_unfreeze_skipped":
            auto_unfreeze_skipped += 1
            reason = str(e.get("reason") or "").strip() or "unknown"
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        elif typ == "auto_approve_paused":
            auto_approve_paused_count += 1
            if i > latest_halt_idx:
                latest_halt_idx = i
                latest_halt = {
                    "ts": str(e.get("ts") or ""),
                    "event_type": "auto_approve_paused",
                    "reason": "consecutive_freezes",
                    "ack_verb": _PAUSE_REASON_ACK_VERB[
                        "consecutive_freezes"
                    ],
                }
        elif typ == "auto_approve_halted":
            auto_approve_halted_count += 1
            if i > latest_halt_idx:
                latest_halt_idx = i
                raw = str(e.get("reason") or "").strip()
                renamed = _HALT_REASON_RENAME.get(raw, raw or "unknown")
                latest_halt = {
                    "ts": str(e.get("ts") or ""),
                    "event_type": "auto_approve_halted",
                    "reason": renamed,
                    "ack_verb": _PAUSE_REASON_ACK_VERB.get(
                        renamed, "auto_approve_window_resume",
                    ),
                }

    # Outcome breakdown: for each auto-approved task, find the next
    # `task_complete` event in the slice and bucket on status.
    auto_approved_completed, auto_approved_froze = _outcome_breakdown(
        slice_, auto_approve_idx,
    )
    auto_unfreeze_succeeded, auto_unfreeze_refroze = _outcome_breakdown(
        slice_, unfreeze_idx_by_task,
    )

    return {
        "auto_approved": auto_approved,
        "auto_approved_completed": auto_approved_completed,
        "auto_approved_froze": auto_approved_froze,
        "auto_unfreeze_applied": auto_unfreeze_applied,
        "auto_unfreeze_tasks": len(unfreeze_idx_by_task),
        "auto_unfreeze_succeeded": auto_unfreeze_succeeded,
        "auto_unfreeze_refroze": auto_unfreeze_refroze,
        "auto_unfreeze_skipped": auto_unfreeze_skipped,
        "auto_unfreeze_skipped_by_reason": skipped_by_reason,
        "auto_approve_paused": auto_approve_paused_count,
        "auto_approve_halted": auto_approve_halted_count,
        "latest_halt": latest_halt,
    }


def _outcome_breakdown(
    slice_: list[dict],
    seed_idx_by_task: dict[str, int],
) -> tuple[int, int]:
    """Score the (completed, froze) outcome buckets for tasks in
    `seed_idx_by_task` (each value is the seed event's positional
    index inside `slice_`).

    For each TB-N, walk forward looking for the FIRST subsequent
    `task_complete task=TB-N` event. A complete-status hit increments
    the completed bucket; a failure-status hit increments the froze
    bucket. Tasks with no subsequent `task_complete` in the slice are
    excluded from both buckets (the task is still pending — won't be
    surfaced as either outcome).

    Naming pinned by the briefing's "M succeeded, K froze" phrasing.
    """
    completed = 0
    froze = 0
    for tid, seed_idx in seed_idx_by_task.items():
        for e in slice_[seed_idx + 1:]:
            if e.get("type") != "task_complete":
                continue
            if str(e.get("task") or "").strip() != tid:
                continue
            status = str(e.get("status") or "").strip()
            if status == "complete":
                completed += 1
            elif status in _FAILURE_STATUSES:
                froze += 1
            break
    return completed, froze


# ---------------------------------------------------------------------------
# TB-244: axis-4 focus-rotation activity in the inter-status-report window.
#
# TB-226 ships `focus_advanced` + `roadmap_complete` events; TB-242 added
# the pull surfaces (`ap2 status` text/JSON + web home active-focus
# card). TB-244 closes the push-surface gap by surfacing both event
# types in the 2h status-report Mattermost post — the operator's primary
# walk-away channel. Helper parallels `collect_window_loop_activity`
# above (since-last-report scoping, pure tail-walk, machine-stable
# return shape) so the renderer in `status_report.py` can consume both
# helpers with the same `since_event_idx` kwarg.


def collect_window_focus_rotation(
    cfg: "Config",
    *,
    since_event_idx: int,
    tail: list[dict] | None = None,
) -> dict:
    """Aggregate axis-4 focus-rotation activity in the inter-status-
    report window for TB-244's digest sub-block.

    `since_event_idx` is the *positional* index of the previous
    `cron_complete job=status-report` event in the tail; events at
    indices `> since_event_idx` count toward the digest. Use `-1` to
    count from the start of the tail (first-ever status report).

    `tail` is passed in when the caller already has it (the routine
    walks the tail once to find `since_event_idx`); when omitted, the
    helper loads the same 2000-event tail
    `collect_auto_approve_state` / `collect_window_loop_activity` use.

    Returned dict (always present, machine-stable shape):

      - `focus_advanced` (list[dict]) — one entry per
        `focus_advanced` event in the window, in tail order. Each
        entry: `{"from": <old_title>, "to": <new_title>,
        "new_index": <int>, "total_foci": <int>}`. The TB-226 event
        payload carries these fields directly; the helper preserves
        them so the renderer can emit `(N of M)`-shaped lines
        without re-reading goal.md.
      - `roadmap_complete` (list[dict]) — one entry per
        `roadmap_complete` event in the window, in tail order. Each
        entry: `{"exhausted_count": <int>}` (the foci-list length
        at exhaustion; mirrors TB-226's event payload). The list is
        usually 0 or 1 entries (the daemon emits at most once per
        exhaustion episode), but tail bounds can carry more across a
        multi-day window where the operator extended + re-exhausted
        the roadmap.
      - `total` (int) — sum of the two list lengths. Renderers use
        this to gate the entire sub-block (omit-on-empty rule:
        `total == 0` → return "").

    Pure / no I/O beyond reading `cfg.events_file` when `tail` is
    omitted; safe to call from request handlers.
    """
    if tail is None:
        if cfg.events_file.exists():
            tail = events.tail(cfg.events_file, 2000)
        else:
            tail = []

    slice_ = tail[since_event_idx + 1:] if since_event_idx >= -1 else tail

    advanced: list[dict] = []
    completed: list[dict] = []

    for e in slice_:
        typ = e.get("type")
        if typ == "focus_advanced":
            # Preserve the four payload fields TB-226 emits. `from` /
            # `to` are required (the daemon never emits the event
            # without them); defensive `str(...)` keeps the renderer
            # safe against a future schema drift that stores ints.
            advanced.append({
                "from": str(e.get("from") or ""),
                "to": str(e.get("to") or ""),
                "new_index": e.get("new_index"),
                "total_foci": e.get("total_foci"),
            })
        elif typ == "roadmap_complete":
            completed.append({
                "exhausted_count": e.get("exhausted_count"),
            })

    return {
        "focus_advanced": advanced,
        "roadmap_complete": completed,
        "total": len(advanced) + len(completed),
    }


def find_previous_status_report_idx(tail: list[dict]) -> int:
    """Return the positional index of the most recent
    `cron_complete job=status-report` event in `tail`, or `-1` if none
    exists (first-ever status report, or the previous one rolled out
    of the tail window).

    Used by the cron status-report routine to scope the digest's
    counts to the inter-report window. Lives here (alongside the
    helper that consumes the index) so callers don't sprinkle
    tail-scanning idioms across modules.
    """
    for i in range(len(tail) - 1, -1, -1):
        e = tail[i]
        if (
            e.get("type") == "cron_complete"
            and e.get("job") == "status-report"
        ):
            return i
    return -1
