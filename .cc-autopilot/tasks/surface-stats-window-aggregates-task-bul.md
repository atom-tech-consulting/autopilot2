## Goal

This task targets the Current focus: end-to-end automation focus, axis 1 (Manual-approval bottleneck) — specifically the walk-away push-channel parity gap on the stats-dashboard surface. The `/stats` HTML page and `/stats.json` endpoint ship the pull surface for task / bullet / ideation timing + turn + attempt aggregates over `events.jsonl`, with all data sourced from the existing-in-HEAD `collect_stats(cfg, *, now=None, window_s=...)` helper at `ap2/automation_stats.py`. But the cron status-report digest (`ap2/status_report.py`) — the Mattermost push channel for walk-away monitoring — carries no top-line aggregates summary. `grep -n collect_stats ap2/status_report.py` returns zero. The operator's natural-cadence return surface stays silent on the new dashboard data; the dashboard pays rent only during active operator sessions, not during the walk-away promise the focus is built around.

This is the same push-vs-pull surface-parity shape several prior tasks have closed on different axes: TB-241 closed it for dry-run readiness, TB-242 closed it for axis-4 focus-pointer state, TB-244 closed it for `focus_advanced`/`roadmap_complete` cron digest, and TB-245 closed it for validator-judge fail-open activity. All consumed helpers are pure-read functions already available in HEAD; this work composes them onto the existing cron status-report surface with no daemon-side changes and no new state.

Why now: walk-away monitoring needs a top-line "what's happened since last report" digest of task volume + median duration + ideation cadence without the operator opening a browser tab — without it, the stats dashboard pays rent only during active operator sessions, not during the walk-away promise (goal.md L28-30 "walk away for a week without intervention") the focus is built around.

## Scope

1. `ap2/status_report.py` — add a `render_stats_window_section(stats_dict)` renderer that takes a `collect_stats(cfg, window_s=since_last_report_s)` dict and emits a 3-5 line bullet sub-block summarizing: total tasks completed in window, median + p90 task duration, ideation runs + skipped count, total bullet evaluations + failure count. Omit the section entirely when the window's task-completion count is zero (omit-on-empty).

2. Wire the renderer into `run_status_report` `state_extras` alongside the existing axis renderers; window scoped to `now - last_report_ts` (reuse the same window-derivation pattern the existing automation-loop digest already uses, OR fall back to the existing 24h default if no prior report).

3. Add a `stats_window` field to `_STATUS_REPORT_CONTRACT` in `ap2/prompts.py` so the SDK status-report agent verbatim-forwards the rendered sub-block (same forwarding contract TB-244 / TB-245 rely on).

4. Cross-reference in `ap2/howto.md` under the status-report contract section noting the new digest sub-block.

5. Tests at `ap2/tests/test_tb259_status_report_stats_window.py`: (a) empty-window omit-block, (b) populated-window happy-path emits ≥3 lines, (c) the renderer output mentions both `tasks` and `ideation` substrings, (d) `_STATUS_REPORT_CONTRACT` contract-string pin includes the new field, (e) `run_status_report` `state_extras` plumbing pin (the contract field appears in the rendered output when populated).

## Design

Pure read-layer composition over the existing-in-HEAD `automation_stats.collect_stats` helper. No new state file, no daemon-side changes, no new env knobs. Mirrors the TB-244 wrap-helper + renderer + `state_extras` wiring pattern. Renderer is omit-on-empty so quiet windows don't grow zero-noise digest lines.

## Verification

- `grep -q "collect_stats" ap2/status_report.py` — status_report wires in the existing stats collector.
- `grep -q "def render_stats_window_section" ap2/status_report.py` — renderer exists at the documented name.
- `grep -q '"stats_window"' ap2/prompts.py` — `_STATUS_REPORT_CONTRACT` enumerates the new `stats_window` field.
- `uv run pytest -q ap2/tests/test_tb259_status_report_stats_window.py` — new pin module passes.
- `uv run pytest -q ap2/tests/` — full suite green (regression gate).

## Out of scope

- Adding new aggregates to `collect_stats` itself (this task uses the existing shape only).
- Per-window time-series of any aggregate (this is a digest, not a chart).
- Mattermost-side rendering changes — the existing verbatim-forwarding contract carries the new block as-is.
- `ap2 status` text/JSON surface for the stats window — a separate parity task can extend this if needed; this one scopes to the cron push channel only.
- Web home surface changes — the dashboard already lives at `/stats`.
