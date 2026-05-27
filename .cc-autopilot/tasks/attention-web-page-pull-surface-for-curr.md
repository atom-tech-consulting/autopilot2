## Goal

Add an `/attention` web page to the read-only web UI so the
walk-away operator has a pull-surface counterpart to the existing
status-report push of `## Attention needed` bullets. The page
renders the same `detect_attention_conditions(cfg)` output the
status-report cron forwards verbatim, but is always-available (no
2h cadence) and accessible from `ap2 status`'s reported URL or
the home page's chrome. Closes Current focus: operator-legible
reporting and monitoring's third Progress signal — "Attention-needing
conditions (stuck / failed / frozen tasks, decisions-needed, cost
or validator-judge anomalies) are surfaced proactively in
operator-legible terms, distinct from routine progress updates" —
on the pull-surface side; the status-report cron remains the push
surface and the home-page automation cards remain the per-axis
state cards.

Why now: the upstream attention vocabulary fully landed (5
detectors shipped: task_stuck, task_frozen, validator_judge_noisy,
auto_approve_paused, cost_cap_approach). The operator named "web
/attention page" verbatim as a remaining axis in the
2026-05-27T06:33:52Z rewind reason on focus-2. Without this page,
the operator must run `ap2 status` (CLI-pull, no rendering of the
attention summaries) or wait up to 2h for the next status-report
post to see currently-active attention conditions — turning what
should be a glance-able pull surface into a poll-or-wait
interaction.

## Scope

(1) New module `ap2/web_attention.py` exporting `router: _WebRouter`
(sibling pattern — same shape as `ap2/web_home.py` /
`ap2/web_events.py` / `ap2/web_stats.py` / `ap2/web_usage.py` /
`ap2/web_insights.py` / `ap2/web_tasks.py`). The router registers
a single GET `/attention` route returning HTML.

(2) The route handler calls
`attention.detect_attention_conditions(cfg)` and renders each
`AttentionCondition` as a bullet using the same operator-legible
shape the status-report's `render_attention_section` produces
(warn-glyph `⚠`, bold TB-N if `extras['task']` is set, em-dash,
detector-supplied `summary`). Empty conditions list renders an
explicit empty-state ("No attention conditions currently active.")
rather than a blank page.

(3) Page chrome reuses `ap2/web_chrome.py`'s shared `<head>` /
nav / CSS via the existing `render_chrome` helper (same call
shape `web_home.py` uses). Add an `/attention` entry to the nav
bar so the link appears on every page.

(4) `make_app()` in `ap2/web.py` imports the new router and calls
`app.include_router(web_attention.router)` alongside the other
six routers; the existing `app.routes` test pattern in
`ap2/tests/test_web.py` discovers it automatically.

(5) `/events` page (`ap2/web_events.py`): when an event row has
`type == "attention_raised"`, render its TB-N anchor (from the
event payload's `task` extras key when present) as a link to
`/attention` so an operator clicking through from the event log
lands on the current-state surface.

(6) Regression-pin module `ap2/tests/test_tb296_web_attention.py`
covers: route registers in `make_app().routes` (paths include
`/attention`); GET `/attention` returns 200 + content-type
`text/html`; with zero detectors firing the page renders the
empty-state string; with a synthetic `AttentionCondition`
(monkeypatched detector entry-point) the page renders one bullet
matching the documented shape; nav-bar HTML on `/` contains an
`/attention` link; `/events` row for an `attention_raised` event
renders an `/attention` link.

(7) Documentation: extend the routing comment near
`make_app()` in `ap2/web.py` (the `ap2/web_*.py` sibling
inventory comment) to name the new `web_attention.py` file, and
note the page in `ap2/architecture.md` alongside the other web
routes.

## Design

Module layout follows the axis-by-axis split established by the
existing `web_*.py` siblings (one cohesive responsibility per
file). The handler is read-only and pulls live state on each
request — no caching, no JSON sub-endpoint (defer to a follow-up
if a consumer materializes; see `## Out of scope`). The page
reuses the SAME detector entrypoint the status-report renderer
uses so push and pull surfaces can never disagree about what's
currently active. Empty-state text is explicit rather than blank
so an operator can distinguish "page loaded, nothing wrong" from
"page broken".

## Verification

- `test -f ap2/web_attention.py` — new sibling module exists.
- `grep -Eq "from .web_attention|web_attention\.router" ap2/web.py` — router wired into `make_app()`.
- `grep -Eq "detect_attention_conditions" ap2/web_attention.py` — handler pulls from the shared detector entrypoint.
- `grep -q "/attention" ap2/web_chrome.py` — nav bar link present.
- `test -f ap2/tests/test_tb296_web_attention.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb296_web_attention.py` — module passes.
- `uv run pytest -q ap2/tests/` — full suite passes.

## Out of scope

- JSON sub-endpoint `/attention.json` for external monitoring
  consumers (operator hasn't asked; per-project legibility scope
  guard at goal.md focus-2 L227-228 explicitly excludes
  cross-project aggregation).
- Auto-refresh / polling on the page (the conditions are
  point-in-time facts; the operator can reload, and a polling
  surface adds complexity without a clear ask).
- New detector kinds (the 5 enumerated condition kinds from
  Progress signal #3 are all shipped).
- `attention_cleared` event class — would enrich the page with
  "recently resolved" data but is a separate event-vocabulary
  expansion; deferred until a concrete consumer surfaces.
- Modifying the detector module itself — this task is purely a
  new consumer; the detector contract stays fixed.
