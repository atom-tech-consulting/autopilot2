"""TB-181 `/usage` token-cost dashboard.

TB-265: extracted from `ap2/web.py` as part of the route-group split.

Page owned by this module:
  - `/usage`   — `_render_usage`

The aggregator-side helpers (`_load_usage_events`, `_aggregate_*`,
`_top_n_expensive_tasks`, `_aggregate_by_model`) and SVG renderers
(`_render_cost_chart_svg`, `_render_cache_chart_svg`,
`_render_model_split_svg`) all live here — they are consumed only by
this page and the test suite that pins them.
"""
from __future__ import annotations

import datetime as _dt
import html
import json

from .board import Board
from .config import Config
from .web_chrome import _layout
from .web_home import _WebRouter


router = _WebRouter()
router.add("/usage")


# Three event types carry usage / cost data: per-task agent runs,
# per-control agent runs (cron / mattermost / ideation), and per-bullet
# judge calls. Anything else lacks a `total_cost_usd` and is excluded
# from the dashboard's aggregation pass.
_USAGE_EVENT_TYPES: frozenset[str] = frozenset({
    "task_run_usage",
    "control_run_usage",
    "judge_call",
})

# URL `?window=` accepted values. Anything outside the set falls back
# to the default `7d` so a typo'd query string doesn't 500 the page.
_USAGE_WINDOWS = ("24h", "7d", "30d", "all")
_DEFAULT_USAGE_WINDOW = "7d"
_DEFAULT_USAGE_STACK = "event_type"

# Stable colors for the event-type stack. Mirrors the row-tint logic's
# semantic palette: blue for task work, green for control-plane work,
# amber for judges (operationally a "warning-tier" cost spike when
# hot). Kept in lockstep with `.legend-swatch` colors below for visual
# consistency between the events page and the dashboard.
_EVENT_TYPE_COLORS = {
    "task_run_usage": "#3a6db5",
    "control_run_usage": "#2a8a4a",
    "judge_call": "#b87000",
}


def _normalize_usage_window(value: str | None) -> str:
    """Map a raw `?window=` query value onto a known one. Empty / typo /
    out-of-range falls back to the default so the page never 500s on
    bad input."""
    v = (value or "").strip().lower()
    return v if v in _USAGE_WINDOWS else _DEFAULT_USAGE_WINDOW


def _normalize_usage_stack(value: str | None) -> str:
    """Map `?stack=` onto `event_type` or `model`; default to event_type."""
    v = (value or "").strip().lower()
    return v if v in ("event_type", "model") else _DEFAULT_USAGE_STACK


def _usage_window_seconds(window: str) -> float | None:
    """Window string → seconds. `all` → None (no filter)."""
    return {
        "24h": 86400.0,
        "7d": 7 * 86400.0,
        "30d": 30 * 86400.0,
    }.get(window)


def _usage_window_chart_days(window: str) -> int:
    """How many daily bars the cost chart renders for each window. For
    `all`, we still cap at 30 — the chart is for trend-spotting, not a
    full historical archive (events.jsonl is the export)."""
    return {"24h": 1, "7d": 7, "30d": 30}.get(window, 30)


def _parse_event_dt(ts: str) -> _dt.datetime | None:
    """Parse an event row's `ts` field. Tolerates missing / malformed
    so a single bad line doesn't break the aggregation pass."""
    if not ts:
        return None
    try:
        return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _load_usage_events(cfg: Config) -> list[dict]:
    """Single-pass scan of events.jsonl, keeping only the three usage-
    bearing types. Pure read; no mutation. Tolerates a missing file
    (fresh project) and individual malformed lines."""
    if not cfg.events_file.exists():
        return []
    out: list[dict] = []
    try:
        with cfg.events_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in _USAGE_EVENT_TYPES:
                    out.append(obj)
    except OSError:
        return []
    return out


def _event_cost(e: dict) -> float:
    c = e.get("total_cost_usd")
    return float(c) if isinstance(c, (int, float)) else 0.0


def _event_token_breakdown(e: dict) -> tuple[int, int, int, int]:
    """Returns (input, output, cache_creation, cache_read). Zero on missing."""
    u = e.get("usage")
    if not isinstance(u, dict):
        return 0, 0, 0, 0
    return (
        int(u.get("input_tokens", 0) or 0),
        int(u.get("output_tokens", 0) or 0),
        int(u.get("cache_creation_input_tokens", 0) or 0),
        int(u.get("cache_read_input_tokens", 0) or 0),
    )


def _event_total_tokens(e: dict) -> int:
    inp, out, cc, cr = _event_token_breakdown(e)
    return inp + out + cc + cr


def _event_subtype(e: dict) -> str:
    """The "subtype" used by the breakdown table's expand-row.
    `task_run_usage` → `status`; `control_run_usage` → `label`
    (mm-handler post-ids collapsed); `judge_call` → `verdict`."""
    typ = e.get("type")
    if typ == "task_run_usage":
        return str(e.get("status") or "unknown")
    if typ == "control_run_usage":
        label = str(e.get("label") or "unknown")
        # Collapse `MM-<post-id>` into the `mm-handler` bucket per
        # briefing — operators don't care about each post-id, they
        # care about aggregate mattermost-handler cost.
        if label.startswith("MM-") or label.startswith("mm-handler"):
            return "mm-handler"
        return label
    if typ == "judge_call":
        return str(e.get("verdict") or "unknown")
    return "unknown"


def _aggregate_usage_by_day(
    events: list[dict],
    *,
    days: int,
    end: _dt.datetime,
) -> list[dict]:
    """Per-day totals for the `days` UTC days ending at `end`'s date.
    Days with no events still appear (zero-filled) so the chart
    renders N bars even on a sparse fixture. Each entry:
        {date, by_event_type, by_model, total_cost,
         input_tokens, cache_creation, cache_read}.
    Returned oldest first.
    """
    end_day = end.date()
    start_day = end_day - _dt.timedelta(days=max(0, days - 1))
    buckets: dict[_dt.date, dict] = {}
    for i in range(days):
        d = start_day + _dt.timedelta(days=i)
        buckets[d] = {
            "date": d.isoformat(),
            "by_event_type": {t: 0.0 for t in _USAGE_EVENT_TYPES},
            "by_model": {},
            "total_cost": 0.0,
            "input_tokens": 0,
            "cache_creation": 0,
            "cache_read": 0,
        }
    for e in events:
        edt = _parse_event_dt(e.get("ts", ""))
        if edt is None:
            continue
        d = edt.date()
        if d not in buckets:
            continue
        b = buckets[d]
        cost = _event_cost(e)
        typ = e.get("type", "")
        b["total_cost"] += cost
        if typ in b["by_event_type"]:
            b["by_event_type"][typ] += cost
        mu = e.get("model_usage")
        if isinstance(mu, dict):
            for model, mu_e in mu.items():
                if not isinstance(mu_e, dict):
                    continue
                mc = mu_e.get("costUSD")
                if isinstance(mc, (int, float)):
                    b["by_model"][model] = b["by_model"].get(model, 0.0) + float(mc)
        inp, _out, cc, cr = _event_token_breakdown(e)
        b["input_tokens"] += inp
        b["cache_creation"] += cc
        b["cache_read"] += cr
    return [buckets[k] for k in sorted(buckets.keys())]


def _aggregate_usage_by_event_type(events: list[dict]) -> dict:
    """Per-event-type aggregates with a nested per-subtype breakdown.

    Returns {type: {count, total_cost, total_tokens, avg_cost,
    cache_hit_pct, input_tokens, cache_creation, cache_read,
    by_subtype: {sub: {count, total_cost, total_tokens, avg_cost,
    cache_hit_pct, input_tokens, cache_creation, cache_read}}}}.
    """
    out: dict[str, dict] = {}
    for e in events:
        typ = e.get("type", "")
        if typ not in _USAGE_EVENT_TYPES:
            continue
        bucket = out.setdefault(typ, {
            "count": 0, "total_cost": 0.0, "total_tokens": 0,
            "input_tokens": 0, "cache_creation": 0, "cache_read": 0,
            "by_subtype": {},
        })
        cost = _event_cost(e)
        toks = _event_total_tokens(e)
        inp, _out, cc, cr = _event_token_breakdown(e)
        bucket["count"] += 1
        bucket["total_cost"] += cost
        bucket["total_tokens"] += toks
        bucket["input_tokens"] += inp
        bucket["cache_creation"] += cc
        bucket["cache_read"] += cr
        sub = _event_subtype(e)
        sb = bucket["by_subtype"].setdefault(sub, {
            "count": 0, "total_cost": 0.0, "total_tokens": 0,
            "input_tokens": 0, "cache_creation": 0, "cache_read": 0,
        })
        sb["count"] += 1
        sb["total_cost"] += cost
        sb["total_tokens"] += toks
        sb["input_tokens"] += inp
        sb["cache_creation"] += cc
        sb["cache_read"] += cr

    def _finish(b: dict) -> None:
        denom = b["input_tokens"] + b["cache_creation"] + b["cache_read"]
        b["cache_hit_pct"] = (b["cache_read"] / denom * 100.0) if denom else 0.0
        b["avg_cost"] = b["total_cost"] / b["count"] if b["count"] else 0.0

    for bucket in out.values():
        _finish(bucket)
        for sb in bucket["by_subtype"].values():
            _finish(sb)
    return out


def _aggregate_usage_by_subtype(events: list[dict], event_type: str) -> dict:
    """Subtype-only breakdown for a single event type. Same shape as
    the `by_subtype` value inside `_aggregate_usage_by_event_type`'s
    output, but keyed for callers (e.g. tests) that only care about
    one type's split."""
    out = _aggregate_usage_by_event_type(
        [e for e in events if e.get("type") == event_type]
    )
    return out.get(event_type, {}).get("by_subtype", {})


def _top_n_expensive_tasks(events: list[dict], n: int = 10) -> list[dict]:
    """Top-N tasks by total cost across `task_run_usage` + `judge_call`.
    Each row: {task, run_count, task_run_count, judge_count,
    total_cost, last_seen}. Sorted by total_cost desc."""
    by_task: dict[str, dict] = {}
    for e in events:
        typ = e.get("type")
        if typ not in ("task_run_usage", "judge_call"):
            continue
        task = str(e.get("task") or "").strip()
        if not task:
            continue
        b = by_task.setdefault(task, {
            "task": task,
            "run_count": 0,
            "task_run_count": 0,
            "judge_count": 0,
            "total_cost": 0.0,
            "last_seen": "",
        })
        b["run_count"] += 1
        if typ == "task_run_usage":
            b["task_run_count"] += 1
        else:
            b["judge_count"] += 1
        b["total_cost"] += _event_cost(e)
        ts = str(e.get("ts") or "")
        if ts > b["last_seen"]:
            b["last_seen"] = ts
    rows = sorted(by_task.values(), key=lambda r: r["total_cost"], reverse=True)
    return rows[:n]


def _aggregate_by_model(events: list[dict]) -> dict[str, float]:
    """Sum `model_usage[m].costUSD` across all events. Returns {model: cost}."""
    out: dict[str, float] = {}
    for e in events:
        mu = e.get("model_usage")
        if not isinstance(mu, dict):
            continue
        for model, mu_e in mu.items():
            if not isinstance(mu_e, dict):
                continue
            cost = mu_e.get("costUSD")
            if isinstance(cost, (int, float)):
                out[model] = out.get(model, 0.0) + float(cost)
    return out


def _model_color(model: str, idx: int) -> str:
    """Stable color for a model name. Opus = purple, Haiku = teal,
    Sonnet = blue, anything else cycles through a small fallback
    palette by `idx`. Same model always renders the same color across
    page reloads (the function is pure)."""
    m = model.lower()
    if "opus" in m:
        return "#6a4ca8"
    if "haiku" in m:
        return "#2a8a8a"
    if "sonnet" in m:
        return "#3a6db5"
    palette = ["#9a3f70", "#3a8a4a", "#b06030", "#5a5a5a", "#406070"]
    return palette[idx % len(palette)]


def _render_cost_chart_svg(
    daily_costs: list[dict],
    *,
    width: int = 720,
    height: int = 240,
    stack_by: str = "event_type",
) -> str:
    """Render N daily stacked bars as inline SVG. Stacks by event type
    (default) or by model (`stack_by="model"`). Each segment carries
    a `<title>` child so the browser shows date + series + dollar
    value on hover with no JS.
    """
    if not daily_costs:
        return (
            f'<svg class="cost-chart" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="12">'
            f'no token-usage events recorded yet</text></svg>'
        )

    pad_l, pad_r, pad_t, pad_b = 56, 12, 16, 48
    chart_w = max(1, width - pad_l - pad_r)
    chart_h = max(1, height - pad_t - pad_b)

    max_total = max((float(d.get("total_cost") or 0.0) for d in daily_costs), default=0.0)
    if max_total <= 0:
        # Flat-axis no-events case — still render the axis frame so the
        # `<svg>` element + axis pin the layout. No bars to draw.
        max_total = 1.0

    n = len(daily_costs)
    slot_w = chart_w / max(n, 1)
    bar_w = max(1.0, slot_w * 0.85)

    if stack_by == "model":
        models: set[str] = set()
        for d in daily_costs:
            models.update((d.get("by_model") or {}).keys())
        series = sorted(models)
        color_map = {m: _model_color(m, i) for i, m in enumerate(series)}
    else:
        series = ["task_run_usage", "control_run_usage", "judge_call"]
        color_map = {s: _EVENT_TYPE_COLORS[s] for s in series}

    parts: list[str] = []
    parts.append(
        f'<svg class="cost-chart" data-stack="{html.escape(stack_by)}" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )

    # Y gridlines + axis labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + chart_h * (1.0 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" '
            f'x2="{pad_l + chart_w}" y2="{y:.1f}" '
            f'stroke="#eee" stroke-width="1"/>'
        )
        v = max_total * frac
        parts.append(
            f'<text x="{pad_l - 4}" y="{y + 3:.1f}" text-anchor="end" '
            f'fill="#888" font-family="sans-serif" font-size="10">'
            f'${v:.2f}</text>'
        )
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + chart_h:.1f}" '
        f'x2="{pad_l + chart_w}" y2="{pad_t + chart_h:.1f}" '
        f'stroke="#888" stroke-width="1"/>'
    )

    # Bars (stacked)
    for i, d in enumerate(daily_costs):
        x = pad_l + slot_w * i + (slot_w - bar_w) / 2.0
        y_cursor = pad_t + chart_h
        stack_dict = (
            d.get("by_model") if stack_by == "model" else d.get("by_event_type")
        ) or {}
        for s in series:
            v = float(stack_dict.get(s, 0.0))
            if v <= 0:
                continue
            seg_h = chart_h * (v / max_total)
            y_cursor -= seg_h
            color = color_map.get(s, "#888")
            parts.append(
                f'<rect class="cost-seg" data-series="{html.escape(s)}" '
                f'x="{x:.1f}" y="{y_cursor:.1f}" '
                f'width="{bar_w:.1f}" height="{seg_h:.1f}" '
                f'fill="{color}">'
                f'<title>{html.escape(str(d.get("date") or ""))} · '
                f'{html.escape(s)}: ${v:.4f}</title>'
                f'</rect>'
            )

    # X labels: first / middle / last so a 30-bar chart doesn't pile
    # text on top of itself. Operators wanting a per-day value hover
    # the bar for the SVG `<title>` tooltip.
    label_indices = sorted({0, n // 2, n - 1}) if n > 0 else []
    for i in label_indices:
        if i < 0 or i >= n:
            continue
        x = pad_l + slot_w * (i + 0.5)
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t + chart_h + 14:.1f}" '
            f'text-anchor="middle" fill="#888" '
            f'font-family="sans-serif" font-size="10">'
            f'{html.escape(str(daily_costs[i].get("date") or ""))}</text>'
        )

    # Legend below
    legend_y = height - 14
    legend_x = pad_l
    for s in series:
        color = color_map.get(s, "#888")
        parts.append(
            f'<rect class="legend-swatch-svg" data-series="{html.escape(s)}" '
            f'x="{legend_x}" y="{legend_y - 9}" width="10" height="10" '
            f'fill="{color}"/>'
        )
        parts.append(
            f'<text x="{legend_x + 14}" y="{legend_y}" '
            f'fill="#444" font-family="sans-serif" font-size="11">'
            f'{html.escape(s)}</text>'
        )
        legend_x += 14 + max(8, len(s)) * 6.5 + 12
    parts.append("</svg>")
    return "".join(parts)


def _render_cache_chart_svg(
    daily_hit_ratios: list[float],
    *,
    width: int = 720,
    height: int = 120,
) -> str:
    """Render daily cache-hit ratio as a sparkline-style line chart with
    per-point dots (each carrying a `<title>` tooltip)."""
    if not daily_hit_ratios:
        return (
            f'<svg class="cache-chart" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="12">'
            f'no token-usage events recorded yet</text></svg>'
        )
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 24
    chart_w = max(1, width - pad_l - pad_r)
    chart_h = max(1, height - pad_t - pad_b)
    n = len(daily_hit_ratios)

    parts: list[str] = []
    parts.append(
        f'<svg class="cache-chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + chart_h * (1.0 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" '
            f'x2="{pad_l + chart_w}" y2="{y:.1f}" '
            f'stroke="#eee" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 4}" y="{y + 3:.1f}" text-anchor="end" '
            f'fill="#888" font-family="sans-serif" font-size="10">'
            f'{frac * 100:.0f}%</text>'
        )

    def _x(i: int) -> float:
        if n <= 1:
            return pad_l + chart_w / 2.0
        return pad_l + (chart_w / (n - 1)) * i

    points = []
    for i, ratio in enumerate(daily_hit_ratios):
        x = _x(i)
        y = pad_t + chart_h * (1.0 - max(0.0, min(1.0, ratio)))
        points.append(f"{x:.1f},{y:.1f}")
    parts.append(
        f'<polyline class="cache-line" points="{" ".join(points)}" '
        f'fill="none" stroke="#3a6db5" stroke-width="2"/>'
    )
    for i, ratio in enumerate(daily_hit_ratios):
        x = _x(i)
        y = pad_t + chart_h * (1.0 - max(0.0, min(1.0, ratio)))
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#3a6db5">'
            f'<title>day {i + 1}: {ratio * 100:.1f}%</title>'
            f'</circle>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _render_model_split_svg(
    model_costs: dict[str, float],
    *,
    width: int = 720,
    height: int = 60,
) -> str:
    """Horizontal stacked bar of total cost percentages by model.
    Sorted by cost desc; each segment + legend entry carries a
    `<title>` tooltip."""
    total = sum(v for v in model_costs.values() if isinstance(v, (int, float)))
    if not model_costs or total <= 0:
        return (
            f'<svg class="model-split" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="12">'
            f'no token-usage events recorded yet</text></svg>'
        )
    pad_l, pad_r = 12, 12
    bar_w = max(1, width - pad_l - pad_r)
    bar_y = 8
    bar_h = 20
    items = sorted(model_costs.items(), key=lambda kv: kv[1], reverse=True)

    parts: list[str] = []
    parts.append(
        f'<svg class="model-split" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )
    x = float(pad_l)
    for i, (model, cost) in enumerate(items):
        seg_w = bar_w * (cost / total)
        color = _model_color(model, i)
        pct = cost / total * 100.0
        parts.append(
            f'<rect class="model-seg" data-model="{html.escape(model)}" '
            f'x="{x:.1f}" y="{bar_y}" width="{seg_w:.1f}" height="{bar_h}" '
            f'fill="{color}">'
            f'<title>{html.escape(model)}: ${cost:.4f} ({pct:.1f}%)</title>'
            f'</rect>'
        )
        x += seg_w

    legend_y = bar_y + bar_h + 16
    legend_x = float(pad_l)
    for i, (model, cost) in enumerate(items):
        color = _model_color(model, i)
        pct = cost / total * 100.0
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 9}" width="10" height="10" '
            f'fill="{color}"/>'
        )
        label = f'{model} ({pct:.1f}%)'
        parts.append(
            f'<text x="{legend_x + 14}" y="{legend_y}" '
            f'fill="#444" font-family="sans-serif" font-size="11">'
            f'{html.escape(label)}</text>'
        )
        legend_x += 14 + max(8, len(label)) * 6.5 + 14
    parts.append("</svg>")
    return "".join(parts)


def _render_usage(
    cfg: Config,
    *,
    window: str | None = None,
    stack_by: str | None = None,
    now: _dt.datetime | None = None,
) -> str:
    """TB-181: token-cost dashboard.

    Reads events.jsonl once per page load, aggregates the three usage-
    bearing event types, and renders cost-over-time + breakdowns +
    top-N tasks + model-split + cache analysis as inline SVG + HTML.

    URL config: `?window=24h|7d|30d|all` (default 7d) and
    `?stack=event_type|model` (default event_type). Out-of-range values
    fall back to the default — no persistent state, the URL is the
    only configuration surface.

    `now` is injected for tests; the URL handler uses the current UTC
    time (the daemon's wall clock).
    """
    window = _normalize_usage_window(window)
    stack_by = _normalize_usage_stack(stack_by)
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    events_all = _load_usage_events(cfg)

    window_seconds = _usage_window_seconds(window)
    if window_seconds is None:
        window_events = list(events_all)
        prior_events: list[dict] = []
    else:
        cutoff = now - _dt.timedelta(seconds=window_seconds)
        prior_cutoff = cutoff - _dt.timedelta(seconds=window_seconds)
        window_events = []
        prior_events = []
        for e in events_all:
            edt = _parse_event_dt(e.get("ts", ""))
            if edt is None:
                continue
            if edt >= cutoff and edt <= now:
                window_events.append(e)
            elif edt >= prior_cutoff and edt < cutoff:
                prior_events.append(e)

    chart_days = _usage_window_chart_days(window)

    # ---- Header summary ------------------------------------------
    total_window = sum(_event_cost(e) for e in window_events)
    total_prior = sum(_event_cost(e) for e in prior_events)
    if total_prior > 0 and window != "all":
        delta_pct = (total_window - total_prior) / total_prior * 100.0
        arrow = "↑" if delta_pct >= 0 else "↓"
        trend_html = (
            f'{arrow} {abs(delta_pct):.0f}% vs prior {html.escape(window)}'
        )
    elif window == "all":
        trend_html = '(all-time view)'
    elif total_window > 0:
        trend_html = '(no prior-window comparison)'
    else:
        trend_html = '—'

    inp_w = sum(_event_token_breakdown(e)[0] for e in window_events)
    cc_w = sum(_event_token_breakdown(e)[2] for e in window_events)
    cr_w = sum(_event_token_breakdown(e)[3] for e in window_events)
    cache_denom_w = inp_w + cc_w + cr_w
    cache_hit_w = (cr_w / cache_denom_w * 100.0) if cache_denom_w else 0.0

    cc_p = sum(_event_token_breakdown(e)[2] for e in prior_events)
    cr_p = sum(_event_token_breakdown(e)[3] for e in prior_events)

    top_window = _top_n_expensive_tasks(window_events, n=1)
    if top_window:
        t = top_window[0]
        most_exp_html = (
            f'<a href="/task/{html.escape(t["task"])}">'
            f'{html.escape(t["task"])}</a> · ${t["total_cost"]:.2f}'
        )
    else:
        most_exp_html = '—'

    # Window / stack chips
    chip_lines: list[str] = ['<div class="usage-chips">window:']
    for w in _USAGE_WINDOWS:
        cls = "on" if w == window else ""
        chip_lines.append(
            f' <a href="/usage?window={w}&amp;stack={stack_by}" '
            f'class="{cls}">{w}</a>'
        )
    chip_lines.append(' &middot; stack:')
    for s in ("event_type", "model"):
        cls = "on" if s == stack_by else ""
        chip_lines.append(
            f' <a href="/usage?window={window}&amp;stack={s}" '
            f'class="{cls}">{s}</a>'
        )
    chip_lines.append('</div>')
    chips_html = "".join(chip_lines)

    summary_card = (
        '<div class="usage-card usage-summary">'
        f'{chips_html}'
        '<div class="usage-stats">'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">total cost ({html.escape(window)})</div>'
        f'<div class="usage-stat-value">${total_window:.2f}</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">trend</div>'
        f'<div class="usage-stat-value usage-stat-small">{trend_html}</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">cache hit</div>'
        f'<div class="usage-stat-value">{cache_hit_w:.1f}%</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">most expensive</div>'
        f'<div class="usage-stat-value usage-stat-small">{most_exp_html}</div></div>'
        '</div></div>'
    )

    # ---- Cost chart ----------------------------------------------
    # When `window=all` we still cap the chart at the most recent 30
    # days (`_usage_window_chart_days` returns 30 for `all`); the
    # underlying full event list is what feeds aggregation so older
    # days still tick the model/breakdown sections.
    daily = _aggregate_usage_by_day(events_all, days=chart_days, end=now)
    cost_chart = _render_cost_chart_svg(daily, stack_by=stack_by)

    # ---- Breakdown by event type ---------------------------------
    by_type = _aggregate_usage_by_event_type(window_events)
    sorted_types = sorted(
        by_type.items(), key=lambda kv: kv[1]["total_cost"], reverse=True
    )
    if sorted_types:
        rows = []
        for typ, agg in sorted_types:
            sub_rows = []
            for sub, sa in sorted(
                agg["by_subtype"].items(),
                key=lambda kv: kv[1]["total_cost"],
                reverse=True,
            ):
                sub_rows.append(
                    f'<tr class="usage-sub">'
                    f'<td class="usage-sub-label">↳ {html.escape(str(sub))}</td>'
                    f'<td>{sa["count"]}</td>'
                    f'<td>${sa["total_cost"]:.4f}</td>'
                    f'<td>{sa["total_tokens"]:,}</td>'
                    f'<td>${sa["avg_cost"]:.4f}</td>'
                    f'<td>{sa["cache_hit_pct"]:.1f}%</td>'
                    f'</tr>'
                )
            sub_block = (
                f'<details><summary>{html.escape(typ)} '
                f'<span class="meta">— {len(sub_rows)} subtype'
                f'{"s" if len(sub_rows) != 1 else ""}</span></summary>'
                f'<table class="usage-sub-table">'
                f'<thead><tr><th>subtype</th><th>count</th>'
                f'<th>total $</th><th>tokens</th><th>avg $/event</th>'
                f'<th>cache hit</th></tr></thead>'
                f'<tbody>{"".join(sub_rows)}</tbody></table></details>'
            )
            rows.append(
                f'<tr><td>{sub_block}</td>'
                f'<td>{agg["count"]}</td>'
                f'<td>${agg["total_cost"]:.4f}</td>'
                f'<td>{agg["total_tokens"]:,}</td>'
                f'<td>${agg["avg_cost"]:.4f}</td>'
                f'<td>{agg["cache_hit_pct"]:.1f}%</td></tr>'
            )
        breakdown_html = (
            '<table class="usage-breakdown"><thead>'
            '<tr><th>event type</th><th>count</th>'
            '<th>total $</th><th>tokens</th>'
            '<th>avg $/event</th><th>cache hit</th></tr>'
            '</thead><tbody>' + "".join(rows) + '</tbody></table>'
        )
    else:
        breakdown_html = (
            '<p class="meta">no token-usage events recorded yet '
            'in this window.</p>'
        )

    # ---- Top-10 expensive tasks ----------------------------------
    top_tasks = _top_n_expensive_tasks(window_events, n=10)
    if top_tasks:
        try:
            board = Board.load(cfg.tasks_file)
        except OSError:
            board = None
        rows = []
        for t in top_tasks:
            tb = t["task"]
            title = ""
            if board is not None:
                task_obj = board.get(tb)
                if task_obj is not None:
                    title = task_obj.title[:80]
            mix = (
                f'{t["task_run_count"]} run'
                f'{"s" if t["task_run_count"] != 1 else ""}'
                f' + {t["judge_count"]} judge'
                f'{"s" if t["judge_count"] != 1 else ""}'
            )
            rows.append(
                f'<tr><td><a href="/task/{html.escape(tb)}">'
                f'{html.escape(tb)}</a></td>'
                f'<td>{html.escape(title)}</td>'
                f'<td>{html.escape(mix)}</td>'
                f'<td>${t["total_cost"]:.4f}</td>'
                f'<td class="ts">{html.escape(t["last_seen"])}</td></tr>'
            )
        top_html = (
            '<table class="usage-top-tasks"><thead>'
            '<tr><th>task</th><th>title</th>'
            '<th>runs</th><th>total $</th><th>last seen</th></tr>'
            '</thead><tbody>' + "".join(rows) + '</tbody></table>'
        )
    else:
        top_html = (
            '<p class="meta">no task-attributable events recorded yet '
            'in this window.</p>'
        )

    # ---- Model split ---------------------------------------------
    model_costs = _aggregate_by_model(window_events)
    model_html = _render_model_split_svg(model_costs)

    # ---- Cache analysis ------------------------------------------
    daily_hit = []
    for d in daily:
        denom = d["input_tokens"] + d["cache_creation"] + d["cache_read"]
        ratio = (d["cache_read"] / denom) if denom else 0.0
        daily_hit.append(ratio)
    cache_chart = _render_cache_chart_svg(daily_hit)

    cache_callouts = (
        '<div class="usage-stats">'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">cache creation</div>'
        f'<div class="usage-stat-value">{cc_w:,}</div>'
        f'<div class="usage-stat-prior">prior {html.escape(window)}: {cc_p:,}</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">cache read</div>'
        f'<div class="usage-stat-value">{cr_w:,}</div>'
        f'<div class="usage-stat-prior">prior {html.escape(window)}: {cr_p:,}</div></div>'
        '</div>'
    )

    body = (
        f'<h1>usage <span class="meta">— window: {html.escape(window)}, '
        f'stack: {html.escape(stack_by)}'
        f' · {len(events_all):,} usage event(s) on file'
        f'</span></h1>'
        f'{summary_card}'
        '<h2>cost over time</h2>'
        f'<div class="usage-card">{cost_chart}</div>'
        '<h2>breakdown by event type</h2>'
        f'<div class="usage-card">{breakdown_html}</div>'
        '<h2>top-10 expensive tasks</h2>'
        f'<div class="usage-card">{top_html}</div>'
        '<h2>model split</h2>'
        f'<div class="usage-card">{model_html}</div>'
        '<h2>cache analysis</h2>'
        f'<div class="usage-card">{cache_callouts}{cache_chart}</div>'
    )
    return _layout("usage", body)
