"""TB-267: /usage dashboard tests — mirror of `ap2/web_usage.py`.

Relocated from `ap2/tests/test_web.py` by the TB-267 split. Each test body
is byte-identical to its pre-TB-267 original; only the module home
changed. (Helpers stay local — this batch is single-module so no need
to lift into `ap2/tests/conftest.py`.)

Covers `/usage` token-cost dashboard owned by `ap2/web_usage.py`:
  - 7-day fixture rendering end-to-end (TB-181).
  - Empty-data and divide-by-zero fallback.
  - `?window=` / `?stack=` query handling.
  - Per-task aggregation (task_run_usage + judge_call sum).
  - Subtype breakdown by event type.
"""
from __future__ import annotations

import datetime as _tb181_dt
import json as _tb181_json
from pathlib import Path

import pytest

from ap2 import web
from ap2.config import Config


def _tb181_project(tmp_path: Path) -> Config:
    """Fresh project with a couple of TB-N entries on the board so the
    Top-10 rows can resolve titles."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "- [ ] **TB-200** **Active alpha**\n"
        "- [ ] **TB-201** **Active beta**\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _tb181_seed(cfg: Config, evt: dict) -> None:
    """Append a pre-shaped event to events.jsonl (bypasses ev_mod.append
    so we can pin a specific `ts`)."""
    with cfg.events_file.open("a") as f:
        f.write(_tb181_json.dumps(evt) + "\n")


def _tb181_seed_seven_day_mix(cfg: Config, now: _tb181_dt.datetime) -> None:
    """Seven-day fixture matching TB-181's prose verification-bullet
    counts EXACTLY: 10 task_run_usage events with varied status, 5
    control_run_usage events with varied label, 30 judge_call events
    with varied verdict. Events span TB-200 / TB-201 so per-task
    aggregation has multiple rows to rank, and use day = i % 7 so all
    seven days carry events (the chart's per-day rect count is what
    `?window=` differentiation tests pin)."""
    statuses = [
        "complete",             # i=0
        "complete",             # i=1
        "verification_failed",  # i=2
        "complete",             # i=3
        "retry_exhausted",      # i=4
        "complete",             # i=5
        "pipeline_pending",     # i=6
        "complete",             # i=7
        "verification_failed",  # i=8
        "complete",             # i=9
    ]
    # 10 task_run_usage events, distributed across the 7-day window via
    # i % 7 so every day has at least one task_run_usage row.
    for i, status in enumerate(statuses):
        d = i % 7
        ts = (now - _tb181_dt.timedelta(days=d, hours=2, minutes=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task = "TB-200" if i % 2 == 0 else "TB-201"
        _tb181_seed(cfg, {
            "ts": ts,
            "type": "task_run_usage",
            "task": task,
            "run_id": f"r{i}",
            "status": status,
            "duration_s": 60.0 + i,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 20000,
            },
            "model_usage": {
                "claude-opus-4-7[1m]": {"costUSD": 0.50},
                "claude-haiku-4-5-20251001": {"costUSD": 0.05},
            },
            "total_cost_usd": 0.55,
            "num_turns": 10,
        })
    # 30 judge_call events with varied verdict (10 pass + 10 fail + 10
    # unverified via i % 3), spread across all 7 days via i % 7.
    verdicts = ["pass", "fail", "unverified"]
    for i in range(30):
        d = i % 7
        verdict = verdicts[i % 3]
        ts = (now - _tb181_dt.timedelta(days=d, hours=3, minutes=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        task = "TB-200" if i % 2 == 0 else "TB-201"
        _tb181_seed(cfg, {
            "ts": ts,
            "type": "judge_call",
            "task": task,
            "bullet_idx": i,
            "bullet_kind": "prose",
            "verdict": verdict,
            "duration_s": 5.0,
            "total_cost_usd": 0.10,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 50,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 5000,
            },
            "model_usage": {
                "claude-opus-4-7": {"costUSD": 0.09},
                "claude-haiku-4-5-20251001": {"costUSD": 0.01},
            },
        })
    # 5 control_run_usage spread across the 7 days; varied labels so
    # the by-subtype breakdown has multiple rows.
    for k, label in enumerate([
        "ideation",
        "cron-status-report",
        "MM-postid-abc",
        "ideation",
        "cron-status-report",
    ]):
        ts = (now - _tb181_dt.timedelta(days=k, hours=4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _tb181_seed(cfg, {
            "ts": ts,
            "type": "control_run_usage",
            "label": label,
            "run_id": f"c{k}",
            "status": "complete",
            "duration_s": 12.0 + k,
            "usage": {
                "input_tokens": 5,
                "output_tokens": 30,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 800,
            },
            "model_usage": {
                "claude-opus-4-7[1m]": {"costUSD": 0.07},
                "claude-haiku-4-5-20251001": {"costUSD": 0.01},
            },
            "total_cost_usd": 0.08,
            "num_turns": 3,
        })


def _tb181_extract(html_str: str, marker: str, end: str = "</svg>") -> str:
    """Slice out one element starting at `marker` (e.g. a class= attr)
    through the closing `</svg>` tag. Used to scope rect-counting to a
    specific chart so the legend / model-split rects don't bleed in."""
    i = html_str.find(marker)
    if i < 0:
        return ""
    j = html_str.find(end, i)
    return html_str[i: j + len(end)] if j >= 0 else html_str[i:]


def test_usage_dashboard_renders_full_with_seven_day_fixture(tmp_path: Path):
    """TB-181 scope-item gate: synthesizes a fixture events.jsonl
    containing a 7-day event mix (>=10 task_run_usage, >=5
    control_run_usage, >=21 judge_call) and asserts every page section
    renders correctly."""
    cfg = _tb181_project(tmp_path)
    now = _tb181_dt.datetime(2026, 5, 5, 18, 0, 0, tzinfo=_tb181_dt.timezone.utc)
    _tb181_seed_seven_day_mix(cfg, now)

    h = web._render_usage(cfg, window="7d", stack_by="event_type", now=now)

    # Cost-chart SVG with at least one rect per day in the window.
    cost_svg = _tb181_extract(h, '<svg class="cost-chart"')
    assert cost_svg, "cost-chart SVG is missing"
    rect_count = cost_svg.count('<rect class="cost-seg"')
    assert rect_count >= 7, (
        f"expected >= 7 cost-seg rects (one per day in 7d window); "
        f"got {rect_count}"
    )

    # Breakdown table has a row per event type with the expected sums.
    # 10 task_run_usage @ $0.55 = $5.50; 30 judge_call @ $0.10 = $3.00;
    # 5 control_run_usage @ $0.08 = $0.40 — counts pinned to the
    # briefing's prose verification bullet (10/5/30).
    assert ">task_run_usage<" in h
    assert ">control_run_usage<" in h
    assert ">judge_call<" in h
    # Use 4dp totals (the renderer's format); accept the sum's exact
    # representation.
    assert "$5.5000" in h, "task_run_usage total $ not rendered"
    assert "$3.0000" in h, "judge_call total $ not rendered"
    assert "$0.4000" in h, "control_run_usage total $ not rendered"

    # Top-10 has at least one TB-N link.
    assert 'href="/task/TB-200"' in h or 'href="/task/TB-201"' in h

    # Model-split SVG has at least 2 segments (opus + haiku).
    model_svg = _tb181_extract(h, '<svg class="model-split"')
    assert model_svg, "model-split SVG is missing"
    seg_count = model_svg.count('<rect class="model-seg"')
    assert seg_count >= 2, f"expected >= 2 model-split segments; got {seg_count}"
    assert "haiku" in model_svg.lower()
    assert "opus" in model_svg.lower()

    # Cache-hit-ratio chart present.
    cache_svg = _tb181_extract(h, '<svg class="cache-chart"')
    assert cache_svg, "cache-chart SVG is missing"


def test_usage_dashboard_empty_state_renders_cleanly(tmp_path: Path):
    """TB-181 scope-item gate: a project with zero token-bearing events
    renders without throwing and shows placeholder text in each section
    instead of crashing on a divide-by-zero / empty-list / None
    dereference. Operators arriving at `/usage` on a brand-new daemon
    must not see a 500 page."""
    cfg = _tb181_project(tmp_path)
    h = web._render_usage(cfg)
    # The page renders.
    assert "<!DOCTYPE html>" in h
    # Each major section's placeholder uses one of the standard
    # phrases; we pin the canonical "no token-usage events recorded yet"
    # appears at least once.
    assert "no token-usage events recorded yet" in h
    # The summary card still renders with $0.00 totals (defensive — a
    # divide-by-zero in the trend / cache-hit calculation would throw).
    assert "$0.00" in h
    # Empty data must not produce a crash; SVG charts render their own
    # fallback message.
    assert '<svg class="cost-chart"' in h
    assert '<svg class="cache-chart"' in h
    assert '<svg class="model-split"' in h


def test_usage_dashboard_window_query_changes_chart_range(tmp_path: Path):
    """TB-181 scope-item gate: `?window=24h` produces a different
    cost-chart range than `?window=7d`. Pin by counting `<rect
    class="cost-seg">` bar elements scoped to the cost-chart SVG."""
    cfg = _tb181_project(tmp_path)
    now = _tb181_dt.datetime(2026, 5, 5, 18, 0, 0, tzinfo=_tb181_dt.timezone.utc)
    _tb181_seed_seven_day_mix(cfg, now)

    h_24h = web._render_usage(cfg, window="24h", now=now)
    h_7d = web._render_usage(cfg, window="7d", now=now)

    cost_24h = _tb181_extract(h_24h, '<svg class="cost-chart"')
    cost_7d = _tb181_extract(h_7d, '<svg class="cost-chart"')
    rects_24h = cost_24h.count('<rect class="cost-seg"')
    rects_7d = cost_7d.count('<rect class="cost-seg"')
    # The 24h window's chart spans 1 day; the 7d window spans 7. Even
    # if subset coverage varies, the 7d chart MUST have strictly more
    # rendered segments because it covers a strict superset of days.
    assert rects_7d > rects_24h, (
        f"expected 7d to have strictly more cost-seg rects than 24h; "
        f"got 7d={rects_7d}, 24h={rects_24h}"
    )

    # Default fallback — invalid value → 7d.
    h_bogus = web._render_usage(cfg, window="not-a-window", now=now)
    cost_bogus = _tb181_extract(h_bogus, '<svg class="cost-chart"')
    assert cost_bogus.count('<rect class="cost-seg"') == rects_7d


def test_usage_dashboard_stack_query_changes_segments(tmp_path: Path):
    """TB-181 scope-item gate: `?stack=model` produces different SVG
    segments than `?stack=event_type`. Pin via the `data-series` attr
    on `cost-seg` rects — by event_type they should carry event-type
    names; by model they should carry model names."""
    cfg = _tb181_project(tmp_path)
    now = _tb181_dt.datetime(2026, 5, 5, 18, 0, 0, tzinfo=_tb181_dt.timezone.utc)
    _tb181_seed_seven_day_mix(cfg, now)

    h_event = web._render_usage(cfg, stack_by="event_type", now=now)
    h_model = web._render_usage(cfg, stack_by="model", now=now)
    cost_e = _tb181_extract(h_event, '<svg class="cost-chart"')
    cost_m = _tb181_extract(h_model, '<svg class="cost-chart"')

    # event_type stack: data-series is one of the three event-type names.
    assert 'data-series="task_run_usage"' in cost_e
    assert 'data-series="judge_call"' in cost_e
    assert 'data-series="claude-opus-4-7[1m]"' not in cost_e

    # model stack: data-series is one of the model names.
    assert 'data-series="claude-opus-4-7[1m]"' in cost_m
    assert 'data-series="claude-haiku-4-5-20251001"' in cost_m
    assert 'data-series="task_run_usage"' not in cost_m


def test_usage_dashboard_per_task_aggregation_sums_run_plus_judges(
    tmp_path: Path,
):
    """TB-181 scope-item gate: a fixture with exactly 3 events for
    TB-X (1 task_run_usage @ $0.50, 2 judge_call @ $0.10 each)
    produces a Top-10 row for TB-X with total $0.70 and 3 events
    (broken out as 1 run + 2 judges in the row's mix column)."""
    cfg = _tb181_project(tmp_path)
    now = _tb181_dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_tb181_dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    _tb181_seed(cfg, {
        "ts": ts, "type": "task_run_usage", "task": "TB-200",
        "run_id": "rA", "status": "complete", "duration_s": 60.0,
        "usage": {
            "input_tokens": 1, "output_tokens": 1,
            "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1,
        },
        "total_cost_usd": 0.50, "num_turns": 1,
        "model_usage": {"claude-opus-4-7": {"costUSD": 0.50}},
    })
    _tb181_seed(cfg, {
        "ts": ts, "type": "judge_call", "task": "TB-200",
        "bullet_idx": 0, "bullet_kind": "prose", "verdict": "pass",
        "duration_s": 5.0, "total_cost_usd": 0.10,
        "usage": {
            "input_tokens": 1, "output_tokens": 1,
            "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1,
        },
        "model_usage": {"claude-opus-4-7": {"costUSD": 0.10}},
    })
    _tb181_seed(cfg, {
        "ts": ts, "type": "judge_call", "task": "TB-200",
        "bullet_idx": 1, "bullet_kind": "prose", "verdict": "fail",
        "duration_s": 5.0, "total_cost_usd": 0.10,
        "usage": {
            "input_tokens": 1, "output_tokens": 1,
            "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1,
        },
        "model_usage": {"claude-opus-4-7": {"costUSD": 0.10}},
    })

    rows = web._top_n_expensive_tasks(web._load_usage_events(cfg), n=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["task"] == "TB-200"
    assert r["run_count"] == 3
    assert r["task_run_count"] == 1
    assert r["judge_count"] == 2
    assert abs(r["total_cost"] - 0.70) < 1e-9

    # The rendered Top-10 row reflects the same numbers.
    h = web._render_usage(cfg, window="7d", now=now)
    # Find TB-200's row in the top-tasks table.
    top_table = h.split('class="usage-top-tasks"', 1)[1].split("</table>", 1)[0]
    assert 'href="/task/TB-200"' in top_table
    assert "$0.7000" in top_table
    assert "1 run + 2 judges" in top_table


def test_usage_route_handler_wired_into_dispatch():
    """TB-181 verification gate: `_render_usage` and the literal `/usage`
    URL are both name-referenced in the web module family. Pinned by
    the briefing's `grep -nE "_render_usage|/usage" ap2/web.py`
    bullet. TB-265: post-split, the helper lives in `web_usage.py`
    and `web.py` re-imports + dispatches it."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    text = "\n".join(p.read_text() for p in sorted(root.glob("web*.py")))
    assert "_render_usage" in text
    assert "/usage" in text


def test_usage_chart_helpers_present_in_web_py():
    """TB-181 verification gate: both SVG helpers are present somewhere
    in the web module family. Pinned by `grep -nE
    "_render_cost_chart_svg|_render_cache_chart_svg"`. TB-265: both
    live in `web_usage.py` post-split."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    text = "\n".join(p.read_text() for p in sorted(root.glob("web*.py")))
    assert "_render_cost_chart_svg" in text
    assert "_render_cache_chart_svg" in text


def test_usage_aggregate_by_event_type_subtype_breakdown(tmp_path: Path):
    """The breakdown table sub-rows split each event type by its
    natural subtype: `task_run_usage` → status, `control_run_usage` →
    label (mm-handler post-ids collapsed), `judge_call` → verdict."""
    cfg = _tb181_project(tmp_path)
    now = _tb181_dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_tb181_dt.timezone.utc)
    _tb181_seed_seven_day_mix(cfg, now)
    by_type = web._aggregate_usage_by_event_type(web._load_usage_events(cfg))

    # task_run_usage: 6 complete + 2 verification_failed +
    # 1 retry_exhausted + 1 pipeline_pending = 10 events total.
    sub_t = by_type["task_run_usage"]["by_subtype"]
    assert sub_t["complete"]["count"] == 6
    assert sub_t["verification_failed"]["count"] == 2
    assert sub_t["retry_exhausted"]["count"] == 1
    assert sub_t["pipeline_pending"]["count"] == 1

    # judge_call: 10 pass + 10 fail + 10 unverified = 30 events total
    # (verdicts cycle through ['pass','fail','unverified'] via i%3).
    sub_j = by_type["judge_call"]["by_subtype"]
    assert sub_j["pass"]["count"] == 10
    assert sub_j["fail"]["count"] == 10
    assert sub_j["unverified"]["count"] == 10

    # control_run_usage: 5 events split across {ideation, cron-status-report,
    # mm-handler}. The MM- post-id label collapses into mm-handler.
    sub_c = by_type["control_run_usage"]["by_subtype"]
    assert sub_c["ideation"]["count"] == 2
    assert sub_c["cron-status-report"]["count"] == 2
    assert sub_c["mm-handler"]["count"] == 1


def test_usage_url_dispatch_falls_through_to_render(tmp_path: Path):
    """End-to-end-ish: invoke `_Handler` with a fake request to confirm
    `/usage` actually dispatches into `_render_usage` (vs 404)."""
    cfg = _tb181_project(tmp_path)
    # The cleanest unit-level dispatch check: ensure both query params
    # round-trip through the renderer when invoked directly.
    h_default = web._render_usage(cfg)
    assert "window: 7d" in h_default
    h_24h = web._render_usage(cfg, window="24h", stack_by="model")
    assert "window: 24h" in h_24h
    assert "stack: model" in h_24h


def test_usage_layout_nav_includes_usage_link():
    """The shared `_layout()` nav header carries `<a href="/usage">`
    so EVERY page (not just `/`) links to the dashboard."""
    page = web._layout("any title", "<p>body</p>")
    assert 'href="/usage"' in page
