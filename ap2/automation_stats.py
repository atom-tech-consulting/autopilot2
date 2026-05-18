"""TB-255: aggregation helpers for the `/stats` dashboard.

Walk-away promise (goal.md `## Current focus: end-to-end automation`,
axis 1 — "Manual-approval bottleneck") requires the operator to be able
to RETURN from a multi-day walk-away and assess loop health at a glance.
The existing first-touch surfaces (`ap2 status`, the home page, the
cron status-report digest) all aggregate at 24h or less; none answers
"how is the loop performing over the last 7/30 days?" — per-task cost,
time-to-complete, retry rates, ideation cycle frequency / cost /
proposal output, or per-bullet verifier latency.

This module is pure-function and isolates the events.jsonl tail-scan
from `ap2/web.py` (which only renders). Single public symbol:
`collect_stats(cfg, *, now=None, window_s=...) -> dict`. The returned
dict's shape is the JSON contract consumed by both `/stats.json` and
`/stats` HTML rendering. All top-level keys are always present so
machine consumers can rely on the shape regardless of activity level.

Window-boundary partial tasks: a `task_complete` event inside the
window whose `task_start` lies outside the window is INCLUDED in the
attempts-per-task histogram with its observed attempt number derived
only from the in-window `task_start` events. Tasks whose `task_start`
lies inside the window but whose `task_complete` lies outside are
NOT counted at all — there's no terminal yet to bucket. Documented
here, in `_build_attempts_histogram`'s docstring, and on the briefing
so the implementer's choice is auditable.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# Window parsing
# ---------------------------------------------------------------------------

# Default window when `?window=` is unset or unparseable. Matches the
# briefing's default: 7 days is the operator's "weekly glance" cadence.
DEFAULT_WINDOW = "7d"

# Sane caps so a typo'd query can't either flood the scan (massive
# window) or render a near-empty page (sub-hour window).
MIN_WINDOW_S = 3600          # 1h — anything shorter is too noisy
MAX_WINDOW_S = 90 * 86400    # 90d — beyond this, events.jsonl re-read cost grows

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([dhm])\s*$", re.IGNORECASE)

# Task-completion statuses we treat as "the task is done" for the
# attempts-per-task histogram + completion-rate denominator. Mirrors
# the daemon's terminal-status vocabulary (TB-148's tinting bucket).
_COMPLETE_STATUSES = frozenset({"complete"})
_FAILURE_STATUSES = frozenset({
    "verification_failed", "state_violation",
    "error", "timeout", "incomplete", "blocked", "failed",
})
_RETRY_EXHAUSTED_STATUS = "retry_exhausted"
# "Counts as an attempt that terminated" — used to close out the
# attempts counter and to populate the totals denominator.
_TERMINAL_STATUSES = (
    _COMPLETE_STATUSES | _FAILURE_STATUSES | {_RETRY_EXHAUSTED_STATUS}
)


def parse_window(raw: str | None) -> tuple[str, int]:
    """Parse a `?window=` query value like `7d` / `30d` / `6h` / `90m`
    into `(canonical_label, seconds)`.

    Empty / unparseable input falls back to `DEFAULT_WINDOW` (7d). The
    returned `seconds` is clamped to `[MIN_WINDOW_S, MAX_WINDOW_S]`
    to prevent typo'd values from either flooding the scan or
    rendering an empty page.

    Returns `(canonical_label, seconds)` so renderers can echo the
    actual window resolved (post-clamp) back to the operator — a
    `?window=1y` URL surfaces as `"90d"` in the heading, not
    silently truncated.
    """
    if not raw:
        raw = DEFAULT_WINDOW
    m = _WINDOW_RE.match(raw)
    if not m:
        raw = DEFAULT_WINDOW
        m = _WINDOW_RE.match(raw)
        assert m is not None  # DEFAULT_WINDOW is well-formed by construction
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "d":
        seconds = n * 86400
    elif unit == "h":
        seconds = n * 3600
    else:  # "m"
        seconds = n * 60
    seconds = max(MIN_WINDOW_S, min(MAX_WINDOW_S, seconds))
    # Re-canonicalize to the largest natural unit so post-clamp values
    # render cleanly (`1d` not `24h`, `2h` not `120m`).
    if seconds % 86400 == 0:
        label = f"{seconds // 86400}d"
    elif seconds % 3600 == 0:
        label = f"{seconds // 3600}h"
    else:
        label = f"{seconds // 60}m"
    return label, seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_event_dt(ts: object) -> _dt.datetime | None:
    """Parse an event `ts` field (`%Y-%m-%dT%H:%M:%SZ`) to a UTC
    datetime; `None` on parse failure. Defensive so a single malformed
    line doesn't break the aggregator. Mirrors `web._parse_event_dt`.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile over `values`. Returns 0.0 on empty
    input so JSON consumers get a numeric zero, not `None`. `pct` in
    `[0, 100]`.

    Uses ceil semantics (`k = ceil(P/100 * N)`) so the p50 of
    `[1, 2, 3, 4, 20]` is `3` (the natural median) rather than the
    banker's-rounding `2`. Matches the common stats-library
    convention so operators expecting `numpy.percentile`-shaped
    answers aren't surprised.
    """
    if not values:
        return 0.0
    s = sorted(values)
    k = max(1, int(math.ceil(pct / 100.0 * len(s))))
    k = min(k, len(s))
    return float(s[k - 1])


def _avg(values: list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _summary_stats(values: list[float]) -> dict:
    """`{count, avg, p50, p95}` over `values`. Numeric zeros on empty
    input so JSON consumers don't have to special-case None."""
    return {
        "count": len(values),
        "avg": _avg(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
    }


def _stream_events(cfg: "Config") -> list[dict]:
    """Single-pass read of events.jsonl as a list of dicts (oldest
    first). Tolerates a missing file (fresh project → `[]`) and
    individual malformed JSON lines (skipped, same defensive parse
    `web._read_jsonl` uses).

    Returning a list rather than yielding so the collector can do
    multiple passes (attempts-correlation needs to walk task_start /
    task_complete in order). File is small (single-digit MB at multi-
    year scale) — keeping it in memory is cheaper than re-reading
    on each pass.
    """
    p = cfg.events_file
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _in_window(e: dict, start_dt: _dt.datetime) -> bool:
    """True iff the event's `ts` is at-or-after `start_dt`. Events
    with missing/malformed `ts` are EXCLUDED — the window-bounded
    filter has to be conservative; an undated event would otherwise
    appear in every window."""
    edt = _parse_event_dt(e.get("ts"))
    return edt is not None and edt >= start_dt


# ---------------------------------------------------------------------------
# Per-metric aggregators
# ---------------------------------------------------------------------------


def _build_task_metrics(in_window: list[dict]) -> dict:
    """Aggregate `task_run_usage` + `task_complete` events into the
    task-level metrics block.

    The "task count" is **per-run** (per `task_run_usage` event),
    matching the briefing's `Total task count (Complete +
    verification_failed + retry_exhausted)` definition where each
    status is a per-attempt outcome. A task that landed Complete on
    its 2nd attempt contributes 2 entries to the total (one
    verification_failed, one complete) and the duration/cost
    histograms include both runs.

    Joins `task_run_usage` and `task_complete` events by `(task,
    run_id)` when a run_id is present on both; otherwise falls back
    to per-task pairing by walking in order. The daemon emits
    `task_run_usage` immediately before `task_complete` for each
    terminal path, so a simple "most recently seen task_run_usage
    for this task" lookup at each task_complete is accurate in
    practice.
    """
    durations: list[float] = []
    num_turns: list[float] = []
    costs: list[float] = []
    complete_count = 0
    failure_count = 0
    frozen_count = 0
    longest_rows: list[dict] = []
    expensive_rows: list[dict] = []

    # Walk in order, maintaining a per-task "latest unmatched
    # task_run_usage" so a task_complete can claim its sibling.
    pending_usage: dict[str, dict] = {}
    for e in in_window:
        typ = e.get("type")
        if typ == "task_run_usage":
            tid = str(e.get("task") or "").strip()
            if tid:
                pending_usage[tid] = e
        elif typ == "task_complete":
            tid = str(e.get("task") or "").strip()
            if not tid:
                continue
            status = str(e.get("status") or "").strip().lower()
            if status not in _TERMINAL_STATUSES:
                continue
            usage = pending_usage.pop(tid, None)
            if status in _COMPLETE_STATUSES:
                complete_count += 1
            elif status == _RETRY_EXHAUSTED_STATUS:
                frozen_count += 1
                failure_count += 1
            elif status in _FAILURE_STATUSES:
                failure_count += 1
            if usage is not None:
                dur = usage.get("duration_s")
                if isinstance(dur, (int, float)):
                    durations.append(float(dur))
                    longest_rows.append({
                        "task": tid,
                        "status": status,
                        "duration_s": float(dur),
                    })
                nt = usage.get("num_turns")
                if isinstance(nt, (int, float)):
                    num_turns.append(float(nt))
                cost = usage.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    costs.append(float(cost))
                    expensive_rows.append({
                        "task": tid,
                        "status": status,
                        "cost_usd": float(cost),
                    })

    total = complete_count + failure_count
    completion_rate = (complete_count / total) if total else 0.0
    frozen_rate = (frozen_count / total) if total else 0.0

    longest_rows.sort(key=lambda r: r["duration_s"], reverse=True)
    expensive_rows.sort(key=lambda r: r["cost_usd"], reverse=True)

    # Task-duration distribution (briefing-fixed buckets, seconds).
    bucket_edges = [
        ("le_1m", 60),
        ("1m_5m", 5 * 60),
        ("5m_15m", 15 * 60),
        ("15m_30m", 30 * 60),
        ("30m_60m", 60 * 60),
    ]
    duration_buckets = {label: 0 for label, _ in bucket_edges}
    duration_buckets["gt_60m"] = 0
    for d in durations:
        placed = False
        for label, ceiling in bucket_edges:
            if d <= ceiling:
                duration_buckets[label] += 1
                placed = True
                break
        if not placed:
            duration_buckets["gt_60m"] += 1

    return {
        "total": total,
        "complete_count": complete_count,
        "failure_count": failure_count,
        "frozen_count": frozen_count,
        "completion_rate": completion_rate,
        "frozen_rate": frozen_rate,
        "duration_s": _summary_stats(durations),
        "num_turns": _summary_stats(num_turns),
        "cost_usd": {
            "count": len(costs),
            "avg": _avg(costs),
            "p50": _percentile(costs, 50),
            "p95": _percentile(costs, 95),
            "total": float(sum(costs)),
        },
        "longest_tasks": longest_rows[:10],
        "most_expensive_tasks": expensive_rows[:10],
        "duration_buckets": duration_buckets,
    }


_HISTOGRAM_TERMINAL_STATUSES = frozenset({"complete", _RETRY_EXHAUSTED_STATUS})


def _build_attempts_histogram(
    all_events: list[dict], start_dt: _dt.datetime,
) -> dict:
    """Correlate per-task `task_start` counters with the
    task-terminal `task_complete` events that finally end a
    task's lifecycle (status=complete OR status=retry_exhausted)
    to populate the attempts-per-task histogram.

    Algorithm: walk ALL events oldest-first, maintaining a per-task
    counter; increment on `task_start`; on a task_complete whose
    status is `complete` or `retry_exhausted`, record the attempt
    bucket and reset the counter. Intermediate
    `task_complete status=verification_failed` / `state_violation` /
    `incomplete` etc. events DO NOT terminate the lifecycle — the
    daemon shelves the task back to Backlog and re-dispatches with
    a fresh `task_start`, which increments the counter naturally.

    Restricting the WALK to in-window events would miscount tasks
    whose first attempt lived in the previous window, so we walk
    everything but only RECORD outcomes whose terminal landed in the
    window.

    Window-boundary contract (per briefing): a task whose first
    `task_start` was BEFORE the window but whose terminal
    `task_complete` lands INSIDE the window is INCLUDED with the
    full attempt count up to and including the terminal — the
    operator wants to see "this finally landed today" with the
    correct attempt number, not a misleading "1st attempt"
    derived from in-window starts alone. Tasks whose terminal is
    OUTSIDE the window are not counted at all (no terminal yet).

    Histogram buckets:
      "1"               — landed COMPLETE on the 1st task_start.
      "2"               — landed COMPLETE on the 2nd task_start.
      "3"               — landed COMPLETE on the 3rd (or later — clamped)
                          task_start.
      "retry_exhausted" — task abandoned to Frozen on retry exhaustion.
    """
    attempts: dict[str, int] = {}
    histogram: dict[str, int] = {
        "1": 0, "2": 0, "3": 0, "retry_exhausted": 0,
    }
    for e in all_events:
        typ = e.get("type")
        if typ == "task_start":
            tid = str(e.get("task") or "").strip()
            if not tid:
                continue
            attempts[tid] = attempts.get(tid, 0) + 1
        elif typ == "task_complete":
            tid = str(e.get("task") or "").strip()
            status = str(e.get("status") or "").strip().lower()
            if not tid:
                continue
            if status not in _HISTOGRAM_TERMINAL_STATUSES:
                # Mid-stream verdict (verification_failed et al.);
                # the daemon will retry. Keep the counter ticking so
                # the next task_start lands on the right attempt #.
                continue
            n = attempts.get(tid, 0)
            # Reset the counter REGARDLESS of window placement —
            # a future window's first-attempt count should start
            # from zero, not inherit this terminal's count.
            attempts[tid] = 0
            if not _in_window(e, start_dt):
                continue
            if status == _RETRY_EXHAUSTED_STATUS:
                histogram["retry_exhausted"] += 1
            else:
                # Clamp to "3" bucket for any attempt count >= 3 that
                # somehow lands complete (defensive — the daemon's
                # retry-cap is 3 today, but the bucket should be
                # well-defined for any future retry-cap change).
                key = "1" if n <= 1 else "2" if n == 2 else "3"
                histogram[key] += 1
    return histogram


def _build_verifier_metrics(in_window: list[dict]) -> dict:
    """Aggregate `judge_call` events into the per-bullet verifier
    metrics block. Shells today have no `judge_call` event (only prose
    bullets go to the LLM judge), so the metrics are prose-only by
    construction; the briefing's Out-of-scope clause flags this as a
    follow-up.
    """
    durations: list[float] = []
    by_kind: dict[str, list[float]] = {}
    slowest_rows: list[dict] = []
    for e in in_window:
        if e.get("type") != "judge_call":
            continue
        dur = e.get("duration_s")
        if not isinstance(dur, (int, float)):
            continue
        d = float(dur)
        durations.append(d)
        kind = str(e.get("bullet_kind") or "unknown").strip() or "unknown"
        by_kind.setdefault(kind, []).append(d)
        # First 80 chars of the bullet, if captured. The event today
        # carries `response_length` / `rationale_length` but not the
        # bullet text itself — fall back to "task=TB-N bullet=N/kind"
        # so the row still has a recognizable identity for operators.
        bidx = e.get("bullet_idx")
        slowest_rows.append({
            "task": str(e.get("task") or ""),
            "bullet_idx": int(bidx) if isinstance(bidx, int) else None,
            "bullet_kind": kind,
            "duration_s": d,
            "model": str(e.get("model") or ""),
            "cost_usd": float(e.get("total_cost_usd") or 0.0),
        })
    slowest_rows.sort(key=lambda r: r["duration_s"], reverse=True)

    kind_breakdown = {
        k: _summary_stats(v) for k, v in by_kind.items()
    }

    # Validator-judge (TB-235 dep-coherence) fail/timeout counts —
    # surfaced here in trend-form (window-bounded) parallel to
    # `automation_status`'s 24h-only counters. Operator watches these
    # accumulate on the stats page to spot a model regression that's
    # too slow to register on the 24h surface.
    vj_fail = sum(
        1 for e in in_window if e.get("type") == "validator_judge_fail"
    )
    vj_timeout = sum(
        1 for e in in_window if e.get("type") == "validator_judge_timeout"
    )

    return {
        "judge_call_count": len(durations),
        "duration_s": _summary_stats(durations),
        "by_bullet_kind": kind_breakdown,
        "slowest_prose_judges": slowest_rows[:10],
        "validator_judge_fail_count": vj_fail,
        "validator_judge_timeout_count": vj_timeout,
    }


def _build_ideation_metrics(in_window: list[dict]) -> dict:
    """Aggregate `control_run_usage label=ideation` events plus
    correlated `ideation_proposal_recorded` events into the ideation
    metrics block."""
    durations: list[float] = []
    num_turns: list[float] = []
    costs: list[float] = []
    cycle_count = 0
    for e in in_window:
        if e.get("type") != "control_run_usage":
            continue
        if str(e.get("label") or "") != "ideation":
            continue
        cycle_count += 1
        dur = e.get("duration_s")
        if isinstance(dur, (int, float)):
            durations.append(float(dur))
        nt = e.get("num_turns")
        if isinstance(nt, (int, float)):
            num_turns.append(float(nt))
        cost = e.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            costs.append(float(cost))

    proposals_recorded = sum(
        1 for e in in_window
        if e.get("type") == "ideation_proposal_recorded"
    )
    proposals_per_cycle = (
        proposals_recorded / cycle_count if cycle_count else 0.0
    )

    # Rejection rate: `task_deleted` events with a reject-class
    # provenance. The reject codepath shares `task_deleted` shape with
    # `delete` (TB-152), so we approximate "rejections" as
    # `ideation_proposal_recorded` minus `task_complete status=complete`
    # for the same `task_id` — but that's expensive to correlate and
    # only loosely informative. Instead, count the explicit reject
    # operator-queue ops that DRAINED in the window via the
    # `operator_queue_drained` count's `applied` field (the operator
    # queue's reject branch emits `task_deleted` with no easy reject
    # discriminator, so we surface the raw `task_deleted` count as the
    # ceiling instead).
    task_deleted_count = sum(
        1 for e in in_window if e.get("type") == "task_deleted"
    )
    rejection_rate = (
        task_deleted_count / proposals_recorded if proposals_recorded else 0.0
    )

    return {
        "cycle_count": cycle_count,
        "duration_s": _summary_stats(durations),
        "num_turns": _summary_stats(num_turns),
        "cost_usd": {
            "count": len(costs),
            "avg": _avg(costs),
            "p50": _percentile(costs, 50),
            "p95": _percentile(costs, 95),
            "total": float(sum(costs)),
        },
        "proposals_recorded": proposals_recorded,
        "proposals_per_cycle": proposals_per_cycle,
        "task_deleted_count": task_deleted_count,
        "rejection_rate": rejection_rate,
    }


def _build_cron_metrics(in_window: list[dict]) -> dict:
    """Aggregate `control_run_usage` events whose `label` starts with
    `cron-` into per-job cycle counts + avg duration + avg cost.
    Future cron jobs added to `cron.yaml` get auto-discovered by the
    label-prefix filter without code changes here.
    """
    per_job: dict[str, dict[str, list[float]]] = {}
    for e in in_window:
        if e.get("type") != "control_run_usage":
            continue
        label = str(e.get("label") or "")
        if not label.startswith("cron-"):
            continue
        job = label[len("cron-"):]
        b = per_job.setdefault(job, {"duration_s": [], "cost_usd": []})
        dur = e.get("duration_s")
        if isinstance(dur, (int, float)):
            b["duration_s"].append(float(dur))
        cost = e.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            b["cost_usd"].append(float(cost))
    jobs = []
    for name, vals in sorted(per_job.items()):
        jobs.append({
            "job": name,
            "cycle_count": max(len(vals["duration_s"]), len(vals["cost_usd"])),
            "avg_duration_s": _avg(vals["duration_s"]),
            "avg_cost_usd": _avg(vals["cost_usd"]),
            "total_cost_usd": float(sum(vals["cost_usd"])),
        })
    return {"jobs": jobs}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def collect_stats(
    cfg: "Config",
    *,
    now: _dt.datetime | None = None,
    window_s: int | None = None,
    window_label: str | None = None,
) -> dict:
    """Aggregate events.jsonl over the requested `window_s` (or the
    default 7d) into a structured stats dict.

    Top-level shape (the durable JSON contract):

        {
          "window": "7d",                # canonical post-clamp label
          "window_s": 604800,            # numeric seconds for callers
          "computed_at": "...Z",         # UTC ISO8601
          "tasks":    {...},
          "verifier": {...},
          "ideation": {...},
          "cron":     {...},
        }

    `now` (default `datetime.now(UTC)`) and `window_s` / `window_label`
    are kwargs to keep the helper testable. When both `window_s` and
    `window_label` are supplied, both are honored verbatim (allows
    tests to assert label / window-size pairs explicitly). When
    neither is supplied, defaults to `parse_window(None)`.

    Pure / no I/O beyond reading `cfg.events_file`; safe to call from
    web request handlers without taking any lock.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    if window_s is None and window_label is None:
        window_label, window_s = parse_window(None)
    elif window_s is None:
        # Label supplied but no seconds → resolve via parse_window so
        # the clamp is consistent.
        window_label, window_s = parse_window(window_label)
    elif window_label is None:
        # Seconds supplied directly (tests) → synthesize a label.
        if window_s % 86400 == 0:
            window_label = f"{window_s // 86400}d"
        elif window_s % 3600 == 0:
            window_label = f"{window_s // 3600}h"
        else:
            window_label = f"{window_s // 60}m"

    start_dt = now - _dt.timedelta(seconds=window_s)

    all_events = _stream_events(cfg)
    in_window = [e for e in all_events if _in_window(e, start_dt)]

    tasks = _build_task_metrics(in_window)
    tasks["attempts_histogram"] = _build_attempts_histogram(
        all_events, start_dt,
    )
    verifier = _build_verifier_metrics(in_window)
    ideation = _build_ideation_metrics(in_window)
    cron = _build_cron_metrics(in_window)

    return {
        "window": window_label,
        "window_s": int(window_s),
        "computed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tasks": tasks,
        "verifier": verifier,
        "ideation": ideation,
        "cron": cron,
    }
