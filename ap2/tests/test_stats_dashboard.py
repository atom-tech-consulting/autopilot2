"""TB-255: behavioral pinning for the stats dashboard at `/stats` +
`/stats.json`.

The aggregator is `ap2.automation_stats.collect_stats` — pure-function,
events.jsonl-tail-only, window-bounded. The renderers are
`ap2.web._render_stats` (HTML) and `ap2.web._render_stats_json` (JSON
bytes). This module covers four arcs:

  (1) Aggregator contract: task metrics (counts, rates, durations,
      cost, longest/expensive tops), per-bullet verifier (count +
      avg/p50/p95), ideation cycles (correlated with
      `ideation_proposal_recorded`), cron jobs (per-label rollup),
      attempts-per-task histogram.

  (2) Window parameter: filters events outside the requested
      window, accepts simple `Nd` / `Nh` / `Nm` suffixes, clamps
      out-of-range values to the [1h, 90d] sanity bounds.

  (3) HTML rendering: `_render_stats` returns 200-class HTML with
      every documented section heading present; no `<script>` tags
      carrying logic (JS-disabled browsers must see all data).

  (4) JSON endpoint: `_render_stats_json` returns the durable
      contract — `{window, computed_at, tasks, verifier, ideation,
      cron}` top-level keys + `window_s` for callers that want the
      resolved numeric seconds.

A future refactor that drops a top-level key, breaks the windowing
math, or softens the JS-free rendering rule trips a focused subset
of these tests with a diff-shaped error.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json
import socket
import urllib.request
from pathlib import Path

import pytest

from ap2 import automation_stats, events, web
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _rewrite_last_event_ts(cfg: Config, ts: str) -> None:
    """Replace the `ts` field on the most recent line in events.jsonl.

    The aggregator's window filter reads `ts` to bucket events;
    appending via the public `events.append` always stamps `now()`,
    so tests that need an event "in the past" rewrite the line after
    the append. Mirrors the helper used in `test_tb227_automation_status.py`
    so the test patterns stay symmetric across stats / automation
    aggregators.
    """
    lines = cfg.events_file.read_text().splitlines()
    if not lines:
        return
    last = _json.loads(lines[-1])
    last["ts"] = ts
    lines[-1] = _json.dumps(last)
    cfg.events_file.write_text("\n".join(lines) + "\n")


def _ts(now: _dt.datetime, *, hours_ago: float) -> str:
    """ISO8601 Zulu timestamp `hours_ago` before `now` (the canonical
    event-format spelling)."""
    when = now - _dt.timedelta(hours=hours_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_at(cfg: Config, ts: str, type_: str, **fields) -> None:
    """Append an event and rewrite its `ts` to the desired wall-clock
    placement. Convenience wrapper for the common test pattern."""
    events.append(cfg.events_file, type_, **fields)
    _rewrite_last_event_ts(cfg, ts)


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ===========================================================================
# (1) Aggregator contract — task metrics
# ===========================================================================


def test_task_metrics_aggregation(cfg: Config):
    """Seed the canonical 3-complete + 2-verification_failed +
    1-retry_exhausted lifecycle and assert the aggregator's counts
    + rates land where the briefing's `## Scope` block prescribes.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    # Three completes.
    for i, tid in enumerate(("TB-100", "TB-101", "TB-102")):
        _append_at(
            cfg, _ts(now, hours_ago=10 + i),
            "task_run_usage",
            task=tid, run_id=f"r-{tid}", status="complete",
            duration_s=120.0 + i * 30,  # 120, 150, 180
            usage={"input_tokens": 1, "output_tokens": 1},
            total_cost_usd=0.50 + i * 0.10,
            num_turns=5,
        )
        _append_at(
            cfg, _ts(now, hours_ago=10 + i),
            "task_complete",
            task=tid, status="complete", commit=f"{tid}-aaa", summary="ok",
        )
    # Two verification_failed.
    for i, tid in enumerate(("TB-200", "TB-201")):
        _append_at(
            cfg, _ts(now, hours_ago=5 + i),
            "task_run_usage",
            task=tid, run_id=f"r-{tid}", status="verification_failed",
            duration_s=300.0, total_cost_usd=0.20, num_turns=10,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        _append_at(
            cfg, _ts(now, hours_ago=5 + i),
            "task_complete",
            task=tid, status="verification_failed", commit="", summary="",
        )
    # One retry_exhausted (frozen).
    _append_at(
        cfg, _ts(now, hours_ago=1),
        "task_run_usage",
        task="TB-300", run_id="r-TB-300", status="retry_exhausted",
        duration_s=600.0, total_cost_usd=2.00, num_turns=20,
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    _append_at(
        cfg, _ts(now, hours_ago=1),
        "task_complete",
        task="TB-300", status="retry_exhausted", commit="", summary="",
    )

    data = automation_stats.collect_stats(cfg, now=now, window_s=7 * 86400)
    tasks = data["tasks"]

    assert tasks["total"] == 6
    assert tasks["complete_count"] == 3
    assert tasks["failure_count"] == 3  # 2 verif + 1 retry_exhausted
    assert tasks["frozen_count"] == 1
    # 3 / 6 = 0.5
    assert tasks["completion_rate"] == pytest.approx(0.5)
    # 1 / 6 ≈ 0.1667
    assert tasks["frozen_rate"] == pytest.approx(1 / 6)
    # Durations: [120, 150, 180, 300, 300, 600]; avg = 275.0
    assert tasks["duration_s"]["count"] == 6
    assert tasks["duration_s"]["avg"] == pytest.approx(275.0)
    # Cost total = 0.5+0.6+0.7 + 0.2*2 + 2.0 = 4.2
    assert tasks["cost_usd"]["total"] == pytest.approx(4.2)
    # Longest task: TB-300 at 600s.
    assert tasks["longest_tasks"][0]["task"] == "TB-300"
    assert tasks["longest_tasks"][0]["duration_s"] == pytest.approx(600.0)
    # Most expensive: TB-300 at $2.00.
    assert tasks["most_expensive_tasks"][0]["task"] == "TB-300"
    assert tasks["most_expensive_tasks"][0]["cost_usd"] == pytest.approx(2.00)


# ===========================================================================
# (1) Aggregator contract — per-bullet verifier
# ===========================================================================


def test_per_bullet_verifier_aggregation(cfg: Config):
    """`judge_call` events with varied durations roll up to the
    documented `{count, avg, p50, p95}` shape. Pins the percentile
    helper's nearest-rank semantics."""
    now = _dt.datetime.now(_dt.timezone.utc)
    # 5 prose-judge calls with durations [1, 2, 3, 4, 20]s.
    for i, d in enumerate((1.0, 2.0, 3.0, 4.0, 20.0)):
        _append_at(
            cfg, _ts(now, hours_ago=1 + i * 0.1),
            "judge_call",
            task="TB-400", bullet_idx=i, bullet_kind="prose",
            verdict="pass", duration_s=d, total_cost_usd=0.01 * (i + 1),
            model="claude-haiku-4-5",
        )
    data = automation_stats.collect_stats(cfg, now=now, window_s=7 * 86400)
    v = data["verifier"]
    assert v["judge_call_count"] == 5
    # Avg = 6.0; p50 nearest-rank with 5 elements → index 3 → value 3.
    assert v["duration_s"]["avg"] == pytest.approx(6.0)
    assert v["duration_s"]["p50"] == pytest.approx(3.0)
    # p95 nearest-rank with 5 elements → ceil(0.95 * 5) = 5 → value 20.
    assert v["duration_s"]["p95"] == pytest.approx(20.0)
    # Per-kind breakdown: only `prose` here.
    assert "prose" in v["by_bullet_kind"]
    assert v["by_bullet_kind"]["prose"]["count"] == 5
    # Slowest list orders by duration desc.
    slow = v["slowest_prose_judges"]
    assert slow[0]["duration_s"] == pytest.approx(20.0)
    assert slow[-1]["duration_s"] == pytest.approx(1.0)


# ===========================================================================
# (1) Aggregator contract — ideation
# ===========================================================================


def test_ideation_metrics_aggregation(cfg: Config):
    """`control_run_usage label=ideation` events plus
    `ideation_proposal_recorded` events roll into the ideation block
    with the documented per-cycle math."""
    now = _dt.datetime.now(_dt.timezone.utc)
    # Three ideation cycles.
    for i in range(3):
        _append_at(
            cfg, _ts(now, hours_ago=20 - i * 5),
            "control_run_usage",
            label="ideation", run_id=f"id-{i}", status="complete",
            duration_s=200.0 + i * 50, total_cost_usd=0.30 + i * 0.05,
            num_turns=15 + i,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    # Two proposals recorded across the three cycles.
    for tid in ("TB-500", "TB-501"):
        _append_at(
            cfg, _ts(now, hours_ago=18),
            "ideation_proposal_recorded",
            task_id=tid, focus_anchor="anchor", why_now_chars=120,
        )

    data = automation_stats.collect_stats(cfg, now=now, window_s=7 * 86400)
    i = data["ideation"]
    assert i["cycle_count"] == 3
    # Avg duration = (200 + 250 + 300) / 3 = 250
    assert i["duration_s"]["avg"] == pytest.approx(250.0)
    # Avg cost = (0.30 + 0.35 + 0.40) / 3 = 0.35
    assert i["cost_usd"]["avg"] == pytest.approx(0.35)
    assert i["cost_usd"]["total"] == pytest.approx(1.05)
    assert i["proposals_recorded"] == 2
    # 2 / 3 ≈ 0.667
    assert i["proposals_per_cycle"] == pytest.approx(2 / 3)


# ===========================================================================
# (2) Window parameter filters events outside the window
# ===========================================================================


def test_window_param_filters_events(cfg: Config):
    """An event at T-2d shows up in `window=7d`; an event at T-10d
    does not. Pins the window-bound filter so a refactor that
    accidentally widens the scan (or drops it entirely) trips."""
    now = _dt.datetime.now(_dt.timezone.utc)
    # In-window: 2 days ago.
    _append_at(
        cfg, _ts(now, hours_ago=2 * 24),
        "task_run_usage",
        task="TB-IN", run_id="r-in", status="complete",
        duration_s=60.0, total_cost_usd=0.10, num_turns=1,
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    _append_at(
        cfg, _ts(now, hours_ago=2 * 24),
        "task_complete",
        task="TB-IN", status="complete", commit="abc", summary="ok",
    )
    # Out-of-window: 10 days ago.
    _append_at(
        cfg, _ts(now, hours_ago=10 * 24),
        "task_run_usage",
        task="TB-OUT", run_id="r-out", status="complete",
        duration_s=60.0, total_cost_usd=0.10, num_turns=1,
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    _append_at(
        cfg, _ts(now, hours_ago=10 * 24),
        "task_complete",
        task="TB-OUT", status="complete", commit="def", summary="ok",
    )

    # 7d window — only TB-IN.
    data_7d = automation_stats.collect_stats(cfg, now=now, window_s=7 * 86400)
    assert data_7d["tasks"]["total"] == 1
    assert data_7d["tasks"]["complete_count"] == 1
    assert {r["task"] for r in data_7d["tasks"]["longest_tasks"]} == {"TB-IN"}

    # 30d window — both.
    data_30d = automation_stats.collect_stats(
        cfg, now=now, window_s=30 * 86400,
    )
    assert data_30d["tasks"]["total"] == 2
    assert {r["task"] for r in data_30d["tasks"]["longest_tasks"]} == {
        "TB-IN", "TB-OUT",
    }


# ===========================================================================
# (2) parse_window contract — labels, clamps, defaults
# ===========================================================================


def test_parse_window_defaults_and_clamps():
    """Unset / unparseable → default `7d`; bare suffix accepted;
    out-of-range clamps to [1h, 90d]. Pins the URL-parameter
    contract."""
    # Default.
    assert automation_stats.parse_window(None) == ("7d", 7 * 86400)
    assert automation_stats.parse_window("") == ("7d", 7 * 86400)
    assert automation_stats.parse_window("not-a-window") == ("7d", 7 * 86400)
    # Bare suffixes.
    assert automation_stats.parse_window("1d") == ("1d", 86400)
    assert automation_stats.parse_window("30d") == ("30d", 30 * 86400)
    assert automation_stats.parse_window("6h") == ("6h", 6 * 3600)
    # Clamp: 0m → 1h floor.
    label, secs = automation_stats.parse_window("0m")
    assert secs == automation_stats.MIN_WINDOW_S
    assert label == "1h"
    # Clamp: 365d → 90d ceiling.
    label, secs = automation_stats.parse_window("365d")
    assert secs == automation_stats.MAX_WINDOW_S
    assert label == "90d"


# ===========================================================================
# (1) Aggregator contract — attempts-per-task histogram
# ===========================================================================


def test_attempts_histogram_correct(cfg: Config):
    """Three task lifecycles:
      - TB-700: one task_start + task_complete=complete   → bucket "1"
      - TB-701: two task_starts then task_complete=complete → bucket "2"
      - TB-702: three task_starts then task_complete=retry_exhausted
                                                            → bucket "retry_exhausted"
    Tests the per-task counter logic that pairs starts with terminals.
    """
    now = _dt.datetime.now(_dt.timezone.utc)

    # TB-700: 1st-try complete.
    _append_at(cfg, _ts(now, hours_ago=20), "task_start", task="TB-700",
               title="t")
    _append_at(cfg, _ts(now, hours_ago=19), "task_complete",
               task="TB-700", status="complete", commit="a", summary="")

    # TB-701: 2nd-try complete (first attempt verification_failed,
    # second attempt complete).
    _append_at(cfg, _ts(now, hours_ago=18), "task_start", task="TB-701",
               title="t")
    _append_at(cfg, _ts(now, hours_ago=17), "task_complete",
               task="TB-701", status="verification_failed",
               commit="", summary="")
    _append_at(cfg, _ts(now, hours_ago=16), "task_start", task="TB-701",
               title="t")
    _append_at(cfg, _ts(now, hours_ago=15), "task_complete",
               task="TB-701", status="complete", commit="b", summary="")

    # TB-702: retry-exhausted on 3rd attempt.
    _append_at(cfg, _ts(now, hours_ago=14), "task_start", task="TB-702",
               title="t")
    _append_at(cfg, _ts(now, hours_ago=13), "task_complete",
               task="TB-702", status="verification_failed",
               commit="", summary="")
    _append_at(cfg, _ts(now, hours_ago=12), "task_start", task="TB-702",
               title="t")
    _append_at(cfg, _ts(now, hours_ago=11), "task_complete",
               task="TB-702", status="verification_failed",
               commit="", summary="")
    _append_at(cfg, _ts(now, hours_ago=10), "task_start", task="TB-702",
               title="t")
    _append_at(cfg, _ts(now, hours_ago=9), "task_complete",
               task="TB-702", status="retry_exhausted",
               commit="", summary="")

    data = automation_stats.collect_stats(cfg, now=now, window_s=7 * 86400)
    hist = data["tasks"]["attempts_histogram"]
    # Histogram only counts the terminal-terminal events
    # (status=complete OR status=retry_exhausted). Intermediate
    # verification_failed events don't bucket — the daemon retries.
    #   TB-700: 1 task_start + complete   → bucket "1"
    #   TB-701: 2 task_starts + complete  → bucket "2"
    #   TB-702: 3 task_starts + retry_exhausted → bucket "retry_exhausted"
    assert hist["1"] == 1, hist
    assert hist["2"] == 1, hist
    assert hist["3"] == 0, hist
    assert hist["retry_exhausted"] == 1, hist


# ===========================================================================
# (1) Aggregator contract — cron metrics
# ===========================================================================


def test_cron_metrics_per_label(cfg: Config):
    """`control_run_usage label=cron-status-report` events roll up
    per-job. Future cron jobs are auto-discovered by the `cron-`
    label prefix."""
    now = _dt.datetime.now(_dt.timezone.utc)
    for i in range(4):
        _append_at(
            cfg, _ts(now, hours_ago=5 - i),
            "control_run_usage",
            label="cron-status-report", run_id=f"sr-{i}", status="complete",
            duration_s=10.0 + i, total_cost_usd=0.05,
            num_turns=2, usage={"input_tokens": 1, "output_tokens": 1},
        )
    data = automation_stats.collect_stats(cfg, now=now, window_s=7 * 86400)
    jobs = data["cron"]["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job"] == "status-report"
    assert job["cycle_count"] == 4
    # Avg = (10 + 11 + 12 + 13) / 4 = 11.5
    assert job["avg_duration_s"] == pytest.approx(11.5)
    assert job["total_cost_usd"] == pytest.approx(0.20)


# ===========================================================================
# (3) HTML rendering — section headers + no inline JS logic
# ===========================================================================


def test_stats_html_renders_without_error(cfg: Config):
    """`_render_stats` returns HTML containing every documented
    section heading (the briefing's `## Layout` block enumerates
    them). The page has no `<script>` tags — JS-disabled browsers
    must see every metric.
    """
    # Seed at least one of each event type so the page isn't all
    # empty-state placeholders.
    now = _dt.datetime.now(_dt.timezone.utc)
    _append_at(cfg, _ts(now, hours_ago=2), "task_start",
               task="TB-800", title="t")
    _append_at(cfg, _ts(now, hours_ago=1), "task_run_usage",
               task="TB-800", run_id="r-800", status="complete",
               duration_s=60.0, total_cost_usd=0.10, num_turns=1,
               usage={"input_tokens": 1, "output_tokens": 1})
    _append_at(cfg, _ts(now, hours_ago=1), "task_complete",
               task="TB-800", status="complete", commit="x", summary="ok")

    html = web._render_stats(cfg)
    assert "<!DOCTYPE html>" in html
    # Every documented section heading present.
    assert "<h1>stats" in html
    assert ">summary<" in html
    assert ">task duration distribution<" in html
    assert ">attempts-per-task histogram<" in html
    assert ">Top-10 longest tasks<" in html
    assert ">Top-10 most expensive tasks<" in html
    assert ">per-bullet verifier<" in html
    assert ">ideation<" in html
    assert ">cron jobs<" in html
    # Window chip exists.
    assert 'href="/stats?window=7d"' in html
    # No `<script>` tag carrying logic — JS-disabled browser must
    # see all data. (The page uses inline `<style>` from the global
    # `_CSS` block; that's the only sanctioned tag.)
    assert "<script" not in html.lower()


def test_stats_html_window_chip_active(cfg: Config):
    """The chip matching the active window gets the `.on` class —
    operator can see which window they're on at a glance without
    re-reading the URL bar."""
    html = web._render_stats(cfg, window="30d")
    # 30d chip is marked .on, 7d is not.
    assert 'href="/stats?window=30d" class="on">30d</a>' in html
    assert 'href="/stats?window=7d" class="">7d</a>' in html


# ===========================================================================
# (4) JSON endpoint — top-level shape contract
# ===========================================================================


def test_stats_json_endpoint_shape(cfg: Config):
    """`_render_stats_json` returns valid JSON with the documented
    top-level keys. The shape is the durable contract — operators
    scripting against it must be able to rely on these key names."""
    raw = web._render_stats_json(cfg)
    data = _json.loads(raw)
    assert set(data.keys()) >= {
        "window", "computed_at", "tasks", "verifier", "ideation", "cron",
    }
    # `window_s` is a bonus key for the resolved numeric seconds —
    # callers that want it shouldn't have to re-parse the label.
    assert "window_s" in data
    assert isinstance(data["window_s"], int)
    assert data["window"] == "7d"  # default
    # Each section is a dict so the JSON consumer can drill in.
    assert isinstance(data["tasks"], dict)
    assert isinstance(data["verifier"], dict)
    assert isinstance(data["ideation"], dict)
    assert isinstance(data["cron"], dict)


def test_stats_json_window_param_honored(cfg: Config):
    """The JSON endpoint echoes the requested window back so scripts
    can confirm what they got. `?window=30d` → `"window": "30d"`."""
    raw = web._render_stats_json(cfg, window="30d")
    data = _json.loads(raw)
    assert data["window"] == "30d"
    assert data["window_s"] == 30 * 86400


# ===========================================================================
# Home page links to /stats (briefing's nav-link clause)
# ===========================================================================


def test_home_page_links_to_stats(cfg: Config):
    """The home page nav must include a link to /stats — operators
    landing on `/` should be able to click through, not have to
    type the URL."""
    html = web._render_home(cfg)
    assert 'href="/stats"' in html


# ===========================================================================
# End-to-end: real HTTP server returns 200 for /stats and /stats.json
# ===========================================================================


def test_stats_routes_serve_via_real_http(cfg: Config):
    """End-to-end check that the route handlers are wired into the
    HTTP dispatch: spin up `serve_async`, hit `/stats` and
    `/stats.json`, assert both return 200 and the JSON endpoint
    carries `application/json`.

    Pinned because the renderer tests above call `_render_stats` /
    `_render_stats_json` directly — they don't catch a broken
    URL-to-renderer wiring.
    """
    port = _free_port()

    async def _go() -> dict:
        task = asyncio.create_task(
            web.serve_async(cfg, host="127.0.0.1", port=port)
        )
        try:
            html_status: int | None = None
            json_status: int | None = None
            json_ct: str | None = None
            html_body = ""
            json_body = ""
            for _ in range(50):
                await asyncio.sleep(0.02)
                try:
                    r1 = await asyncio.to_thread(
                        urllib.request.urlopen,
                        f"http://127.0.0.1:{port}/stats", None, 2.0,
                    )
                    html_status = r1.status
                    html_body = r1.read().decode()
                    r1.close()
                    r2 = await asyncio.to_thread(
                        urllib.request.urlopen,
                        f"http://127.0.0.1:{port}/stats.json", None, 2.0,
                    )
                    json_status = r2.status
                    json_ct = r2.headers.get("Content-Type", "")
                    json_body = r2.read().decode()
                    r2.close()
                    break
                except Exception:  # noqa: BLE001
                    continue
            return {
                "html_status": html_status,
                "html_body": html_body,
                "json_status": json_status,
                "json_ct": json_ct,
                "json_body": json_body,
            }
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    out = asyncio.run(_go())
    assert out["html_status"] == 200, out
    assert "<h1>stats" in out["html_body"]
    assert out["json_status"] == 200
    assert "application/json" in (out["json_ct"] or "")
    parsed = _json.loads(out["json_body"])
    assert {"window", "computed_at", "tasks", "verifier", "ideation", "cron"} \
        <= set(parsed.keys())
