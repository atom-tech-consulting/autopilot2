# Add `/usage` token-cost dashboard to the web UI

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The walk-away promise depends on operators being able to scan cost trends at a glance after a period away — without an aggregation surface, an operator returning after a week has no way to answer "is this project still within budget?" or "did anything spike token usage while I was gone?" without writing ad-hoc scripts.

TB-165 (`task_run_usage`) and TB-166 (`control_run_usage`) shipped earlier today and now persist per-run token totals + cost in `events.jsonl`. TB-157 had already done the same for `judge_call` events. The data is there — three event types covering every SDK call site — but the only aggregation surface today is `adhoc/token_breakdown.py`, a one-off script designed for the cache experiment that has explicit "not a long-term aggregation surface" framing in its docstring. The operator-readable view that actually serves the walk-away question is missing.

This task adds `/usage` to the bundled web UI — a dashboard rendering cost-over-time, breakdowns by event type and subtype, top-N expensive tasks, model-split, and cache analysis. Pure server-side render against `events.jsonl` on each page load; no DB, no JS framework, no auth. Operator scans, decides whether to act, navigates back to other surfaces.

Why now: the underlying data has been landing for hours; without a view, operators (and Claude assisting operators) have to compose ad-hoc grep+jq pipelines or run the adhoc script. As the project's cost scales — current ~$0.90 per ideation cycle × 12 cycles/day = $10.80/day on ideation alone before task agents and judges — visibility on the trend matters more, not less. Filing now lands the dashboard before the next cost-tuning experiment so operators can measure-then-tune rather than tune-blind.

## Scope

- `ap2/web.py` — new `/usage` route handler (`_render_usage`). Reads `events.jsonl` once per page load via `events.tail(cfg.events_file, n=None)` (or a paginated read), aggregates the three relevant event types (`task_run_usage`, `control_run_usage`, `judge_call`), renders the dashboard via inline SVG + HTML.
- `ap2/web.py` — `/` index page navigation gains a link to `/usage` near the existing `/events` / `/insights` links.
- `ap2/web.py` — new helpers: `_aggregate_usage_by_day`, `_aggregate_usage_by_event_type`, `_aggregate_usage_by_subtype`, `_top_n_expensive_tasks`, `_aggregate_by_model`, `_render_cost_chart_svg` (per-day stacked bars), `_render_cache_chart_svg` (per-day cache-hit-ratio).
- `ap2/web.py` — CSS additions for the dashboard's card-style layout, table styling for the breakdown tables, SVG container styling. Reuse existing `.verif-summary` / `.pending-queue` card-pattern colors for visual consistency.
- `ap2/tests/test_web.py` — page-render tests with synthetic `events.jsonl` fixtures: assert each section renders correctly with expected aggregate values.

## Design

### Page sections (in render order)

1. **Header summary card** — at-a-glance numbers:
   - Total cost (today / 7-day / 30-day / lifetime), switchable via URL query `?window=7d` (default `7d`)
   - Cost trend vs prior equivalent window (e.g., "↑ 12% vs prior 7d")
   - Cache hit ratio for the current window
   - Most expensive task of the current window (TB-N, total $)
2. **Cost-over-time chart** (inline SVG):
   - One stacked bar per UTC day for the configured window (default 30 days)
   - Stacked by event type (`task_run_usage` / `control_run_usage` / `judge_call`)
   - Toggle via URL query `?stack=model` re-stacks by model (`claude-opus-4-7[1m]` / `claude-haiku-4-5-20251001` / others)
   - Y-axis dollar values, X-axis dates, legend below
3. **Breakdown by event type** (primary table):
   - Columns: Event type, Count, Total $, Total tokens, Avg $/event, Cache hit %
   - Sorted by Total $ descending
   - Each row is a `<details>` element expanding a sub-breakdown:
     - `task_run_usage` → by `status` (complete / verification_failed / retry_exhausted / pipeline_pending)
     - `control_run_usage` → by `label` (ideation / cron-status-report / MM-<post-id> aggregated as `mm-handler`)
     - `judge_call` → by `verdict` (pass / fail / unverified) and by `bullet_kind` (prose / shell-if-ever-judged)
4. **Top-10 expensive tasks**:
   - Ranked by total $ across `task_run_usage` + `judge_call` events for that task
   - Columns: TB-N (linked to `/task/<TB-N>`), title (truncated), run count, total $, last seen
5. **Model split**:
   - Horizontal stacked bar (SVG) showing % of total cost by model
   - Pulls per-event from the `model_usage` field
   - Answers "are sub-calls actually using cheaper haiku where appropriate?"
6. **Cache analysis**:
   - Two numeric callouts: total cache-creation tokens, total cache-read tokens, both for the current window vs prior window
   - Daily cache-hit-ratio chart (inline SVG, smaller than the cost chart)

### Charting: inline SVG, server-rendered

All charts use Python-generated inline SVG markup (no JS framework, no chart library dependency). Two helper functions:

```python
def _render_cost_chart_svg(daily_costs: list[dict], *, width: int = 720,
                           height: int = 240, stack_by: str = "event_type") -> str:
    """Render N daily stacked bars as inline SVG. Returns a complete <svg>...</svg> string."""

def _render_cache_chart_svg(daily_hit_ratios: list[float], *, width: int = 720,
                            height: int = 120) -> str:
    """Render daily cache-hit-ratio as a sparkline-style SVG line chart."""
```

Each helper produces a self-contained `<svg>` element with axis labels, gridlines, color-coded segments, and a hover-friendly title via `<title>` children (browser tooltip on hover, no JS). Sharper than Unicode-block charts; copy-paste-friendly via View Source if the operator wants the data extracted.

### Aggregation logic

Single-pass over `events.jsonl` collecting events of the three relevant types into a list. From that list, derive:

- Per-day totals (group by UTC day from event `ts`)
- Per-event-type totals
- Per-subtype totals (group by `status` / `label` / `verdict` per event-type)
- Per-task totals (sum task_run_usage + judge_call costs grouped by `task` field)
- Per-model totals (sum cost from `model_usage[m].costUSD` across all events for each model)

Single-scan keeps cold-load fast. No caching needed at v1 — events.jsonl is typically <50MB even at months of operation; reading + aggregating takes <500ms.

### URL query parameters

- `?window=24h|7d|30d|all` — sets the time window for the header summary + cost chart (default `7d`)
- `?stack=event_type|model` — controls the cost chart's stacking dimension (default `event_type`)

Out-of-range or invalid values fall back to the default. No persistent state — URL is the only configuration surface, makes the views shareable / bookmarkable.

### Storage / data shape — no changes

This task is purely a read-path / rendering layer. No new event types emitted, no event-payload shape changes, no events.jsonl mutations.

### Performance

For a project with 10,000 events (~6 months of operation at ~50 events/day), full scan is well under 1 second. If the file ever grows past ~50MB and load times regress, paginated read or a periodic-aggregate cache file is a natural next step — explicit out-of-scope for v1.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "_render_usage|/usage" ap2/web.py` — route handler is wired into the URL dispatch table.
- `grep -nE "_render_cost_chart_svg|_render_cache_chart_svg" ap2/web.py` — both SVG helpers are present.
- prose: a test in `test_web.py` synthesizes a fixture `events.jsonl` containing 7 days of event mix (10 task_run_usage events with varied status, 5 control_run_usage with varied label, 30 judge_call with varied verdict). Calls the `/usage` route handler. Asserts the rendered HTML:
  - Contains a `<svg>` element for the cost chart with at least one `<rect>` per day in the window
  - Contains a breakdown table with a row for each event type AND the row's Total $ cell matches the fixture's expected sum
  - Contains a Top-10-tasks list with at least one `<a href="/task/TB-N">` link
  - Contains a model-split SVG with at least 2 segments (opus + haiku)
  - Contains a cache-hit-ratio chart SVG
- prose: a test pins the empty-data case — fixture with zero token-bearing events produces a page that renders cleanly (no Python exception) with placeholder text like "no token-usage events recorded yet" in each section.
- prose: a test pins URL query handling — `?window=24h` produces a different cost-chart range than `?window=7d` (assert by counting `<rect>` bar elements), `?stack=model` produces different SVG segments than `?stack=event_type` (assert by inspecting fill colors or segment IDs).
- prose: a test pins the `/` index page link — the home page contains `<a href="/usage">` so the dashboard is discoverable from the existing nav.
- prose: a test pins per-task aggregation — a fixture with 3 events for TB-X (1 task_run_usage @ $0.50, 2 judge_call @ $0.10 each) produces a Top-10 row for TB-X showing total $0.70 and run count 3 (or "1 task run + 2 judges" — pin one specific shape).

## Out of scope

- Live updates / auto-refresh. Page renders on load; hit reload to refresh.
- Per-bullet `judge_call` drill-down rows. Too granular for this view; the existing `/task-run/<run-id>` page already shows that detail.
- Cost forecasting / "you're on track to spend $X this month" projections.
- Cost-threshold alerting (Mattermost post when daily cost exceeds N). Separate concern.
- CSV / JSON export of the aggregates. The underlying events.jsonl IS the export — operators can run `jq` against it.
- Cross-project cost rollup. Each project's daemon has its own `/usage`; aggregating across projects is a separate problem.
- Cache-prediction — "if you bumped TTL to 1h, you'd save $X." Out of scope; the data is here for analysts but no built-in modeling.
- Custom date-range picker UI (calendar widget). The `?window=7d` query is sufficient for v1.
- Sortable / filterable table columns via JS. Server-side sort by Total $ desc; operators wanting other sorts can grep the events directly.
- Replacing or refactoring `adhoc/token_breakdown.py`. Both surfaces continue to exist; the script's framing as a one-off cache-experiment tool stays valid.
- Per-event-row drill-into-stream-jsonl from this view. Click-through to existing `/task-run/<run-id>` page covers the deep-dive case.
- Currency conversion (showing values in non-USD). USD only.
- Cost attribution to specific operators / agents beyond what `model_usage` and event-type already provide.
## Attempts

### 2026-05-06 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] prose: a test in `test_web.py` synthesizes a fixture `events.jsonl` containing 7 days of event mix (10 task_run_usage ev
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260506T012727Z-TB-181.prompt.md`, `stream: .cc-autopilot/debug/20260506T012727Z-TB-181.stream.jsonl`, `messages: .cc-autopilot/debug/20260506T012727Z-TB-181.messages.jsonl`
