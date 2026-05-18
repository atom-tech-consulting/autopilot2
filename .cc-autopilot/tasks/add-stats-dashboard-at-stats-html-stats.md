# Add stats dashboard at `/stats` (HTML) + `/stats.json` (JSON) — task / bullet / ideation timing + turn + attempt aggregates from events.jsonl

Tags: `#autopilot` `#web` `#observability` `#operator-surface` `#cli` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis on the observability front. Today the operator's review surfaces are spot-checks: `ap2 status` shows current board + 24h auto-approve counts (TB-227 + TB-243 + TB-244); the cron status-report digest posts a 2h slice to Mattermost (TB-228); `ap2 logs` is raw event-tail. None of them answers "how is the loop performing over the last 7/30 days?" — operator returning from a multi-day walk-away has no consolidated trend view of per-task cost, time-to-complete, retry rates, ideation cycle frequency / cost / proposal output, or per-bullet verifier latency. Under `AP2_AUTO_APPROVE=1` the cost-and-quality picture is the operator's primary judgment surface; today it's scattered across three pull surfaces + one push surface, none aggregating beyond 24h. Close that gap with a single stats dashboard at `http://127.0.0.1:8730/stats` (HTML, server-rendered) backed by `http://127.0.0.1:8730/stats.json` (JSON, scripting-friendly), aggregating `events.jsonl` over an operator-configurable window (default 7d).

Why now: TB-254 (`214f027`) just cut the test-suite runtime from 1336s to 92s by surfacing TB-235's silent LLM-judge cost — an observation that came from TB-253's deliberate profiling investigation, not from the existing automation surfaces. The pattern is generalizable: silent overhead in the loop is invisible without dedicated metrics. The stats dashboard makes that overhead routinely visible, so the next TB-235-shape regression (silent cost accumulating somewhere) surfaces in operator's weekly glance rather than waiting for a deliberate investigation.

## Scope

(1) **Add aggregation helpers** to `ap2/automation_status.py` (or a new sibling `ap2/automation_stats.py` — implementer's call based on file-size growth) that compute the metrics from `events.jsonl` over an operator-configurable window:

  - **Task metrics** (per `task_run_usage` events + correlated `task_start` events):
    - Total task count (Complete + verification_failed + retry_exhausted)
    - Completion rate (Complete / total)
    - Avg / p50 / p95 task duration (seconds) — from `task_run_usage.duration_s`
    - Avg / p50 / p95 num_turns
    - Avg cost USD per task (from `task_run_usage.total_cost_usd`)
    - Top-10 longest tasks: TB-N, status, duration, link to briefing
    - Top-10 most expensive tasks: TB-N, status, cost, link to briefing
    - Attempts-per-task histogram: how many tasks landed Complete on 1st try, 2nd try, 3rd try (retry_exhausted)
    - Frozen rate (retry_exhausted / total)

  - **Per-bullet verifier metrics** (per `judge_call` events; shell bullets aggregated at task level):
    - Total prose-judge call count
    - Avg / p50 / p95 prose-judge duration per bullet
    - Per-bullet-kind breakdown (`prose` vs the implicit `shell` aggregate via task-level subtraction)
    - Top-10 slowest prose-judge calls: bullet text (first 80 chars), duration, model, cost
    - Validator-judge fail count + timeout count (TB-243's existing counters, surfaced in trend form)

  - **Ideation metrics** (per `control_run_usage` events with `label=ideation`):
    - Cycle count in window
    - Avg / p50 / p95 ideation cycle duration
    - Avg / p50 / p95 ideation num_turns
    - Avg cost per ideation cycle
    - Proposals generated per cycle (correlate with `ideation_proposal_recorded` events)
    - Rejection rate (correlate with `reject` operator-queue ops)

  - **Cron metrics** (per `control_run_usage` events with `label=cron-*`):
    - Per-job cycle count, avg duration, avg cost (status-report job today; future cron jobs auto-discovered by `label` prefix)

  - **Window selection**: default 7d; configurable via `?window=30d` query param (also accepts `1d`, `6h`, etc. via the existing `parse_duration` helper if present, or inline parsing).

(2) **Add `/stats.json` endpoint** to `ap2/web.py` that returns the aggregated data as JSON. Top-level shape:

```json
{
  "window": "7d",
  "computed_at": "2026-05-18T...",
  "tasks": {...},
  "verifier": {...},
  "ideation": {...},
  "cron": {...}
}
```

Operator can scrape this for external dashboards or use it in scripts; the HTML page is the primary UI but the JSON is the durable contract.

(3) **Add `/stats` HTML page** to `ap2/web.py`, server-rendered, no JS framework. Layout:

  - **Header**: "Stats — window: 7d (configurable)", last-computed timestamp, link to JSON endpoint, link back to home.
  - **Summary block**: total tasks, completion rate %, avg task duration, total cost USD, frozen rate %.
  - **Task time distribution**: simple inline HTML table — bucket | count (≤1m, 1-5m, 5-15m, 15-30m, 30-60m, >60m).
  - **Attempts histogram**: bucket | count (1st-try pass, 2nd-try, 3rd-try, retry-exhausted).
  - **Top-10 longest tasks** table.
  - **Top-10 most expensive tasks** table.
  - **Per-bullet verifier section**: total prose-judge calls, avg/p50/p95 duration, top-10 slowest prose-judge bullets.
  - **Ideation section**: cycles in window, avg duration / turns / cost, proposals per cycle, rejection rate.
  - **Cron section**: per-job stats.
  - **Window selector**: links to switch to `?window=1d`, `7d`, `30d` (just anchor tags re-rendering the page).

  No charts, no SVG, no inline JS. Plain HTML tables + minimal inline CSS matching `ap2/web.py`'s existing aesthetic (the home page uses simple bordered tables — keep that pattern). Operator can copy data to a spreadsheet if they want richer visualization.

(4) **Add a "Stats" link** to the existing home page (`_render_home` in `ap2/web.py`) navigation — wherever the existing nav links live. One-line addition.

(5) **Implementation guidance**:
  - The aggregation passes through `events.jsonl` in a single linear scan, computing all metrics in one pass. The file is small enough (single-digit MB at multi-year scale) that recompute-on-each-request is acceptable for v1; if perf becomes an issue, add 60s in-memory cache.
  - Use the existing event-parsing patterns from `automation_status.collect_auto_approve_state` (single-pass scan with structured per-event-type handlers).
  - The HTML rendering reuses the existing `_html_table` helper or equivalent from `ap2/web.py`. Don't introduce a template engine.
  - Attempts-per-task correlation: count `task_start` events with `task=<TB-N>` per task; the count of starts before a `task_complete status=complete` is the attempt-number (1, 2, 3). Tasks with `retry_exhausted=true` count as "3 attempts, frozen."

(6) **Tests** (`ap2/tests/test_stats_dashboard.py`):
  - `test_task_metrics_aggregation`: seed events.jsonl with synthetic task lifecycle events (3 complete, 2 verification_failed, 1 retry_exhausted); assert aggregator returns expected counts + rates.
  - `test_per_bullet_verifier_aggregation`: seed `judge_call` events with varied durations; assert avg/p50/p95 correct.
  - `test_ideation_metrics_aggregation`: seed `control_run_usage label=ideation` events; assert aggregations.
  - `test_window_param_filters_events`: events at T-2d and T-10d; with `window=7d` assert only the 2d-old event counted.
  - `test_stats_html_renders_without_error`: HTTP GET `/stats` → 200, contains expected section headers.
  - `test_stats_json_endpoint_shape`: HTTP GET `/stats.json` → 200, top-level keys present (`window`, `computed_at`, `tasks`, `verifier`, `ideation`, `cron`).
  - `test_attempts_histogram_correct`: seed 3 tasks: one 1st-try-complete, one 2nd-try-complete, one retry-exhausted; assert histogram buckets `{1: 1, 2: 1, retry_exhausted: 1}`.
  - `test_home_page_links_to_stats`: HTTP GET `/` → contains `<a href="/stats">` or equivalent.

(7) **Docs**: add a `## Stats dashboard` subsection to `ap2/howto.md` documenting the URLs (`/stats` + `/stats.json`), the `window` query param, the metrics surfaced, and the "what to look for during walk-away review" framing.

(8) **Not in scope** (so the scope contract is unambiguous):
  - Per-shell-bullet timing events (no event today; aggregation only goes to task-level for shell). If operator wants this later, file a separate TB to emit `verify_bullet_run` events for shells.
  - Chart libraries (Chart.js, D3, etc.) — plain HTML tables only.
  - Time-series graphs (e.g. tasks-per-day line chart) — sticking to current-window aggregates this iteration; line charts deserve their own TB if useful.
  - Authentication / access control on the dashboard — web UI is localhost-only per existing `_web_host_default` (127.0.0.1).
  - Mattermost push of stats digest — separate observability surface; TB-228's existing digest covers the high-signal subset.
  - Per-operator-classify-verdict aggregation (e.g. "X% of tasks classified `advanced-goal`") — depends on TB-189's classify activity volume, which is sparse today; revisit when classifications accumulate.
  - CLI verb mirror (`ap2 stats`) — web is the primary surface; CLI can use `/stats.json` via curl if needed.
  - WebSocket / live-updating dashboard — recompute-on-refresh is sufficient.

## Design

**Single-pass aggregation over events.jsonl**: each requested window-bounded scan reads the file once, dispatching events to per-type handlers that accumulate counters / lists. For 7d windows the file slice is small; for 30d it's still single-digit MB. Per-request recompute is simpler than cache invalidation; if perf becomes an issue (>5s per page load), a 60s in-memory cache is a one-line addition.

**Reuse existing collector pattern**: `automation_status.collect_auto_approve_state` (line 441 in current code) demonstrates the single-pass-with-per-type-handlers shape. The stats aggregator follows the same shape with more handlers. Sharing the file-read step (via a helper that yields parsed events from the file once) is a possible optimization but not required for v1.

**Attempts-per-task correlation is the most complex aggregation**: pairs `task_start` events with the eventual `task_complete` to determine which attempt number the completion landed on. Implementation: maintain a per-task counter; increment on each `task_start task=<TB-N>`; on `task_complete task=<TB-N> status=<X>`, record (TB-N, attempt-count, status) and reset the counter. Tasks that retry-exhaust → Frozen show up with attempt_count=3 + status=verification_failed (the daemon's retry-exhaust path emits `task_complete status=verification_failed` before transitioning to Frozen).

**Why expose JSON endpoint AS WELL AS HTML**: operator-facing UI is HTML; scripting / external dashboard integration is JSON. The JSON contract is more stable than the HTML rendering (HTML can change layout; JSON shape pins the data contract). Mirrors the `ap2 status --json` pattern.

**Window parameter `?window=7d`**: parses simple suffixes (`d`, `h`, `m`) into seconds. Default 7d. Sane caps: minimum 1h (any shorter is too noisy), maximum 90d (any longer means re-reading too much of events.jsonl for limited value).

**Goal-anchor**: the Done-when bullet "an operator can point ap2 at a fresh project, paste a `goal.md`, and walk away for a week without intervention" — the "walk away for a week" promise requires the operator to RETURN and review what happened. The stats dashboard is exactly the return-and-review surface for cost + performance metrics; today's surfaces only give 24h snapshots.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q ap2/tests/test_stats_dashboard.py` — new test module passes (minimum 8 cases per Scope §6).
- `grep -nE "def collect_stats|def render_stats|/stats\b" ap2/web.py ap2/automation_status.py ap2/automation_stats.py 2>/dev/null` — exit 0; the new collector + page-renderer + route handler are present (whichever file the implementer chose).
- `grep -nE "stats\.json" ap2/web.py` — exit 0; the JSON endpoint is wired.
- `grep -nE 'href="/stats"' ap2/web.py` — exit 0; the home page links to the stats page.
- `grep -nE "Stats dashboard|## Stats" ap2/howto.md` — exit 0; the docs section is present.
- `[ "$(grep -cE 'window' ap2/automation_status.py ap2/automation_stats.py 2>/dev/null)" -ge 3 ]` — at least 3 mentions of "window" across the collector code (window-param parsing, window-bounded filter, window default). Sanity check that windowing is implemented, not stubbed.
- Prose: the `/stats` HTML page renders without JS — operator can view in a JS-disabled browser and all data is visible. Judge confirms by reading the rendered HTML and verifying no `<script>` tags carrying logic (a `<style>` tag is fine).
- Prose: the `/stats.json` endpoint's top-level shape is `{window, computed_at, tasks, verifier, ideation, cron}` — the keys form the durable contract. Judge confirms via `Read` of the endpoint handler.
- Prose: the attempts-per-task correlation handles the edge case where a task's lifecycle spans the window boundary (task_start before window, task_complete inside window OR vice versa) — implementer's call on whether to include or exclude these partial-window tasks; judge verifies the choice is documented in the collector function's docstring.

## Out of scope

- Per-shell-bullet timing events (no current event; aggregation only to task-level for shell).
- Chart libraries (Chart.js / D3 / Plotly) — plain HTML tables.
- Time-series graphs / sparklines — current-window aggregates only.
- Authentication / access control — web UI is localhost-only.
- Mattermost push of stats digest — separate observability TB if needed.
- Per-operator-classify-verdict aggregation (sparse signal today; revisit post-engagement).
- CLI verb mirror (`ap2 stats`) — web is primary surface.
- WebSocket / live-updating — recompute-on-refresh is sufficient.
- Backfilling pre-event-stream historical data — events.jsonl only.
- Cross-project aggregation — single-project model per goal.md non-goals.
## Attempts

### 2026-05-18 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `[ "$(grep -cE 'window' ap2/automation_status.py ap2/automation_stats.py 2>/dev/null)" -ge 3 ]` — at least 3 mentions of
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260518T172430Z-TB-255.prompt.md`, `stream: .cc-autopilot/debug/20260518T172430Z-TB-255.stream.jsonl`, `messages: .cc-autopilot/debug/20260518T172430Z-TB-255.messages.jsonl`
### 2026-05-18 — blocked
Work is fully implemented in 891c406 (1785/1785 tests green, all 13 new test_stats_dashboard.py cases pass, all other Verification bullets pass: collect_stats/render_stats/route grep, /stats.json wire, href="/stats" link, "## Stats dashboard" howto section). Sole blocker: the line-128 verification bullet `[ "$(grep -cE 'window' ap2/automation_status.py ap2/automation_stats.py 2>/dev/null)" -ge 3 ]` is structurally unsatisfiable — `grep -c` over TWO file args always emits multi-line `file:N` output (e.g. "ap2/automation_status.py:78\nap2/automation_stats.py:66"), which bash `[ ... -ge 3 ]` rejects as "integer expression expected" regardless of how many 'window' mentions each file contains (current counts: 78 + 66 = 144, vastly exceeding the intended ≥3 threshold). No combination of file presence/absence/symlink/empty-content produces a pure integer from this command. Fix: pipe the files through cat first so grep sees stdin and emits a single integer. BriefingFix: grep_c_multifile_to_cat_pipe at .cc-autopilot/tasks/add-stats-dashboard-at-stats-html-stats.md:128: grep -cE 'window' ap2/automation_status.py ap2/automation_stats.py 2>/dev/null -> cat ap2/automation_status.py ap2/automation_stats.py 2>/dev/null | grep -cE 'window'
- **commit:** 891c406
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260518T173840Z-TB-255.prompt.md`, `stream: .cc-autopilot/debug/20260518T173840Z-TB-255.stream.jsonl`, `messages: .cc-autopilot/debug/20260518T173840Z-TB-255.messages.jsonl`
