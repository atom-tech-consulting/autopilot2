"""TB-255 stats dashboard (`/stats` + `/stats.json`).

TB-265: extracted from `ap2/web.py` as part of the route-group split.

Pages owned by this module:
  - `/stats`        — HTML stats dashboard
  - `/stats.json`   — JSON contract for scripting consumers

Aggregates live in `ap2.automation_stats`; this module owns the HTML
rendering + window chip selector only.
"""
from __future__ import annotations

import html
import json

from . import automation_stats
from .config import Config
from .web_chrome import _layout
from .web_home import _WebRouter


router = _WebRouter()
router.add("/stats")
router.add("/stats.json")


# Window-selector chips on the stats page header. Operator can flip
# windows without typing the URL; the chip for the active window gets
# a `.on` class via `_stats_window_chips` so it visually highlights.
_STATS_WINDOW_CHIPS = ("1d", "7d", "30d")


def _stats_window_chips(active: str) -> str:
    """Inline chip set for `?window=...`. Reuses the existing
    `.filter` CSS so the chrome matches /events and /usage."""
    parts = ['<div class="filter">window:']
    for w in _STATS_WINDOW_CHIPS:
        cls = "on" if w == active else ""
        parts.append(
            f' <a href="/stats?window={html.escape(w)}" '
            f'class="{cls}">{html.escape(w)}</a>'
        )
    parts.append(
        ' <a href="/stats.json'
        + (f'?window={html.escape(active)}' if active else '')
        + '" class="" title="JSON contract for /stats">'
        + 'json</a>'
    )
    parts.append("</div>")
    return "".join(parts)


def _fmt_duration_s(s: float) -> str:
    """Human-friendly duration. Sub-minute → `Xs`; sub-hour →
    `Xm Ys`; longer → `Xh Ym`. Used by the stats tables only — the
    JSON contract carries the raw `duration_s` number."""
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        m = int(s // 60)
        sec = int(s % 60)
        return f"{m}m {sec}s"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    return f"{h}h {m}m"


def _fmt_cost(c: float) -> str:
    """`$0.0123` — four-decimal USD for at-a-glance scanning."""
    return f"${float(c):.4f}"


def _fmt_pct(p: float) -> str:
    """`0.812 → 81.2%`."""
    return f"{float(p) * 100:.1f}%"


def _stats_summary_card(tasks: dict, window_label: str) -> str:
    """Summary tile strip at the top of /stats — total tasks,
    completion rate, avg duration, total cost, frozen rate."""
    total = tasks["total"]
    cost_total = tasks["cost_usd"]["total"]
    completion = _fmt_pct(tasks["completion_rate"])
    frozen = _fmt_pct(tasks["frozen_rate"])
    avg_dur = _fmt_duration_s(tasks["duration_s"]["avg"])
    tiles = [
        ("Total tasks", str(total)),
        ("Completion rate", completion),
        ("Avg duration", avg_dur),
        ("Total cost", _fmt_cost(cost_total)),
        ("Frozen rate", frozen),
    ]
    return (
        '<div class="stats">'
        + "".join(
            f'<div class="stat"><div class="stat-label">{html.escape(label)}</div>'
            f'<div class="stat-value">{html.escape(value)}</div></div>'
            for label, value in tiles
        )
        + "</div>"
    )


def _stats_duration_buckets_table(tasks: dict) -> str:
    """`bucket | count` table for task-duration distribution."""
    buckets = tasks.get("duration_buckets", {})
    # Stable display order matches the briefing's bucket list.
    order = [
        ("le_1m", "≤1m"),
        ("1m_5m", "1–5m"),
        ("5m_15m", "5–15m"),
        ("15m_30m", "15–30m"),
        ("30m_60m", "30–60m"),
        ("gt_60m", ">60m"),
    ]
    rows = "".join(
        f"<tr><td>{html.escape(label)}</td>"
        f'<td>{int(buckets.get(key, 0))}</td></tr>'
        for key, label in order
    )
    return (
        "<table><thead><tr><th>bucket</th><th>count</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _stats_attempts_table(tasks: dict) -> str:
    """`bucket | count` table for the attempts-per-task histogram."""
    hist = tasks.get("attempts_histogram", {})
    order = [
        ("1", "1st-try complete"),
        ("2", "2nd-try complete"),
        ("3", "3rd-try complete"),
        ("retry_exhausted", "retry-exhausted (frozen)"),
    ]
    rows = "".join(
        f"<tr><td>{html.escape(label)}</td>"
        f'<td>{int(hist.get(key, 0))}</td></tr>'
        for key, label in order
    )
    return (
        "<table><thead><tr><th>attempts</th><th>count</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _stats_top_tasks_table(rows: list[dict], *, value_key: str,
                           value_fmt, value_header: str) -> str:
    """Generic "top-N" table for longest / most-expensive tasks.
    Task ids link back to `/task/<TB-N>` so the operator can jump
    straight to the briefing + per-run history."""
    if not rows:
        return "<p><em>(no rows)</em></p>"
    body = []
    for r in rows:
        tid = str(r.get("task") or "")
        status = str(r.get("status") or "")
        val = r.get(value_key, 0)
        body.append(
            f"<tr>"
            f'<td><a href="/task/{html.escape(tid)}">{html.escape(tid)}</a></td>'
            f"<td>{html.escape(status)}</td>"
            f"<td>{html.escape(value_fmt(val))}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        f"<th>task</th><th>status</th><th>{html.escape(value_header)}</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _stats_verifier_table(verifier: dict) -> str:
    """Per-bullet verifier section: total count + duration stats +
    top-10 slowest prose-judge bullets."""
    dur = verifier.get("duration_s", {})
    count = int(verifier.get("judge_call_count", 0))
    tiles = (
        '<div class="stats">'
        f'<div class="stat"><div class="stat-label">prose-judge calls</div>'
        f'<div class="stat-value">{count}</div></div>'
        f'<div class="stat"><div class="stat-label">avg duration</div>'
        f'<div class="stat-value">{html.escape(_fmt_duration_s(dur.get("avg", 0.0)))}</div></div>'
        f'<div class="stat"><div class="stat-label">p50</div>'
        f'<div class="stat-value">{html.escape(_fmt_duration_s(dur.get("p50", 0.0)))}</div></div>'
        f'<div class="stat"><div class="stat-label">p95</div>'
        f'<div class="stat-value">{html.escape(_fmt_duration_s(dur.get("p95", 0.0)))}</div></div>'
        f'<div class="stat"><div class="stat-label">validator-judge fails</div>'
        f'<div class="stat-value">{int(verifier.get("validator_judge_fail_count", 0))}</div></div>'
        f'<div class="stat"><div class="stat-label">validator-judge timeouts</div>'
        f'<div class="stat-value">{int(verifier.get("validator_judge_timeout_count", 0))}</div></div>'
        "</div>"
    )

    slow = verifier.get("slowest_prose_judges", []) or []
    if not slow:
        slow_table = "<p><em>(no judge calls in window)</em></p>"
    else:
        rows = []
        for r in slow:
            tid = str(r.get("task") or "")
            bidx = r.get("bullet_idx")
            bkind = str(r.get("bullet_kind") or "")
            ident = f"{tid} bullet={bidx}/{bkind}" if bidx is not None else f"{tid} {bkind}"
            rows.append(
                f"<tr>"
                f'<td><a href="/task/{html.escape(tid)}">{html.escape(ident)}</a></td>'
                f"<td>{html.escape(_fmt_duration_s(r.get('duration_s', 0)))}</td>"
                f"<td>{html.escape(str(r.get('model', '')))}</td>"
                f"<td>{html.escape(_fmt_cost(r.get('cost_usd', 0)))}</td>"
                f"</tr>"
            )
        slow_table = (
            "<table><thead><tr>"
            "<th>bullet</th><th>duration</th><th>model</th><th>cost</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    return (
        f"{tiles}"
        f"<h3>Top-10 slowest prose-judge calls</h3>{slow_table}"
    )


def _stats_ideation_section(ideation: dict) -> str:
    """Ideation cycles in the window — count, avg/p50/p95 duration /
    turns / cost, proposals per cycle, rejection rate."""
    cycles = int(ideation.get("cycle_count", 0))
    dur = ideation.get("duration_s", {})
    turns = ideation.get("num_turns", {})
    cost = ideation.get("cost_usd", {})
    tiles = (
        '<div class="stats">'
        f'<div class="stat"><div class="stat-label">cycles</div>'
        f'<div class="stat-value">{cycles}</div></div>'
        f'<div class="stat"><div class="stat-label">avg duration</div>'
        f'<div class="stat-value">{html.escape(_fmt_duration_s(dur.get("avg", 0.0)))}</div></div>'
        f'<div class="stat"><div class="stat-label">avg turns</div>'
        f'<div class="stat-value">{float(turns.get("avg", 0.0)):.1f}</div></div>'
        f'<div class="stat"><div class="stat-label">avg cost</div>'
        f'<div class="stat-value">{html.escape(_fmt_cost(cost.get("avg", 0.0)))}</div></div>'
        f'<div class="stat"><div class="stat-label">proposals recorded</div>'
        f'<div class="stat-value">{int(ideation.get("proposals_recorded", 0))}</div></div>'
        f'<div class="stat"><div class="stat-label">proposals / cycle</div>'
        f'<div class="stat-value">{float(ideation.get("proposals_per_cycle", 0.0)):.2f}</div></div>'
        f'<div class="stat"><div class="stat-label">rejection rate</div>'
        f'<div class="stat-value">{html.escape(_fmt_pct(ideation.get("rejection_rate", 0.0)))}</div></div>'
        "</div>"
    )
    return tiles


def _stats_cron_section(cron: dict) -> str:
    """Per-cron-job aggregates: cycle count, avg duration, avg cost."""
    jobs = cron.get("jobs", []) or []
    if not jobs:
        return "<p><em>(no cron jobs ran in window)</em></p>"
    rows = []
    for j in jobs:
        rows.append(
            f"<tr>"
            f"<td>{html.escape(str(j.get('job', '')))}</td>"
            f"<td>{int(j.get('cycle_count', 0))}</td>"
            f"<td>{html.escape(_fmt_duration_s(j.get('avg_duration_s', 0)))}</td>"
            f"<td>{html.escape(_fmt_cost(j.get('avg_cost_usd', 0)))}</td>"
            f"<td>{html.escape(_fmt_cost(j.get('total_cost_usd', 0)))}</td>"
            f"</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>job</th><th>cycles</th><th>avg duration</th>"
        "<th>avg cost</th><th>total cost</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_stats(cfg: Config, *, window: str | None = None) -> str:
    """Server-rendered HTML for the TB-255 stats dashboard.

    All sections render even when empty so the structure is stable
    for the operator's muscle-memory scan. The window selector
    accepts `?window=1d` / `7d` / `30d` (and bare `Nh` / `Nm` /
    `Nd` for ad-hoc windows; out-of-range values clamp to
    `[1h, 90d]` per `parse_window`'s contract).

    No JS — the page is fully readable in a JS-disabled browser.
    The only inline `<style>` is the global `_CSS` block; this
    renderer carries no logic in a `<script>` tag.
    """
    window_label, window_s = automation_stats.parse_window(window)
    data = automation_stats.collect_stats(
        cfg, window_s=window_s, window_label=window_label,
    )
    tasks = data["tasks"]
    verifier = data["verifier"]
    ideation = data["ideation"]
    cron = data["cron"]

    chips = _stats_window_chips(window_label)
    header = (
        f"<h1>stats <span class=\"meta\">— window {html.escape(window_label)}"
        f" · computed {html.escape(data['computed_at'])}</span></h1>"
        f"{chips}"
    )

    summary_html = _stats_summary_card(tasks, window_label)
    duration_table = _stats_duration_buckets_table(tasks)
    attempts_table = _stats_attempts_table(tasks)
    longest_table = _stats_top_tasks_table(
        tasks.get("longest_tasks", []),
        value_key="duration_s",
        value_fmt=_fmt_duration_s,
        value_header="duration",
    )
    expensive_table = _stats_top_tasks_table(
        tasks.get("most_expensive_tasks", []),
        value_key="cost_usd",
        value_fmt=_fmt_cost,
        value_header="cost",
    )
    verifier_html = _stats_verifier_table(verifier)
    ideation_html = _stats_ideation_section(ideation)
    cron_html = _stats_cron_section(cron)

    body = (
        f"{header}"
        f"<h2>summary</h2>{summary_html}"
        f"<h2>task duration distribution</h2>{duration_table}"
        f"<h2>attempts-per-task histogram</h2>{attempts_table}"
        f"<h2>Top-10 longest tasks</h2>{longest_table}"
        f"<h2>Top-10 most expensive tasks</h2>{expensive_table}"
        f"<h2>per-bullet verifier</h2>{verifier_html}"
        f"<h2>ideation</h2>{ideation_html}"
        f"<h2>cron jobs</h2>{cron_html}"
    )
    return _layout("stats", body)


def _render_stats_json(cfg: Config, *, window: str | None = None) -> bytes:
    """JSON contract for `/stats.json`. Top-level shape is the
    durable scripting interface — `{window, computed_at, tasks,
    verifier, ideation, cron}` (plus `window_s` for callers that
    want the resolved numeric seconds without re-parsing the
    label). Stable across HTML rendering changes."""
    window_label, window_s = automation_stats.parse_window(window)
    data = automation_stats.collect_stats(
        cfg, window_s=window_s, window_label=window_label,
    )
    return json.dumps(data, indent=2, default=str).encode("utf-8")
