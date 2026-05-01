# Web UI: tint task_complete rows by status, not uniform green

## Goal

`task_complete` events render with the same row class (and color) regardless of the embedded `status` field, so a `complete` and a `verification_failed` look identical in the events list and the operator has to expand each row to tell them apart. Differentiate visually by status so a glance at `/events` reads "5 done, 1 timed out, 2 frozen" without clicking.

## Why

Operator workflow: when something looks off, the first place to look is `/events`. The current uniform green for every `task_complete` hides the most operationally relevant signal — did the task actually pass, or did it fall through one of the failure modes? Existing failure-mode signals (`verification_failed`, `task_state_violation`, `task_error`, `task_timeout`, `retry_exhausted`) DO get distinct row colors today via `_row_class` in `web.py`. The gap is specifically that `task_complete` is the "post-decision" event that carries the resolved `status`, and the row class doesn't read it.

## Scope

(1) `web._row_class` (currently around line 149 of `ap2/web.py`) takes only `typ` (event type). Extend its signature to accept the full event dict so it can look at `status` for `task_complete`.

(2) Mapping for `task_complete`:
   - `status="complete"` → green (today's color, retain)
   - `status="verification_failed"` → orange/yellow (soft warning — the implementation may have committed but verification didn't pass)
   - `status="state_violation"` → red (rolled back; the run produced no useful artifacts)
   - `status="timeout"` → red (similar — incomplete work, daemon abandoned)
   - `status="error"` → red
   - `status="retry_exhausted"` (if it appears as a task_complete status) → dark red / different shade signaling "frozen / abandoned permanently"
   - `status="unknown"` or missing → gray

(3) Apply the same status-aware tinting to the home-page recent-events block, since it shares the table renderer.

(4) The CSS hooks already exist for the failure-mode event rows (orange/red on verification_failed etc.); reuse those classes for task_complete with matching status to keep the palette consistent — operator brain pattern-matches "row colored like a verification_failed = something went wrong" without needing a new color memory.

(5) Add a tiny legend block near the top of `/events` (one-line "complete=green, partial-fail=orange, hard-fail=red, frozen=dark-red") so first-time viewers map color to meaning. Hidden behind a `<details>` if it adds clutter.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `python3 -c "from ap2.web import _row_class; e1={'type':'task_complete','status':'complete'}; e2={'type':'task_complete','status':'verification_failed'}; e3={'type':'task_complete','status':'state_violation'}; assert _row_class(e1)!=_row_class(e2) and _row_class(e2)!=_row_class(e3)"` — the three statuses produce three distinct row classes (post-implementation; will fail until the function is updated).
- New unit test in `test_web.py`: `_row_class` returns `green`-equivalent class for `task_complete` with `status="complete"`, `orange`-equivalent for `verification_failed`, `red`-equivalent for `state_violation` / `error` / `timeout`, `gray` for missing/unknown status.
- New unit test: rendering `/events` against a fixture with mixed-status `task_complete` events produces HTML containing each expected CSS class once.
- Manual / e2e smoke: `ap2 web` started, `/events` page colors visibly differ across the same task that passed once and failed once. (Manual confirmation acceptable for visual; gated bullets above prove the row class wiring.)
- The diff includes the legend block (or reasoned-out trade-off comment if dropped from scope).

## Out of scope

- Re-coloring other event types whose row class is already type-driven (cron_complete, mattermost_reply, etc.). Only `task_complete` has the status-not-reflected gap today.
- A separate `/complete` filter view on the events page (already filterable via `?type=task_complete`; users can stack filters later).
- Visual changes beyond the row tint (icon, badge, etc.) — small surface, keep diff minimal.
- Backfilling old events with retroactive coloring — not needed; the renderer reads each event fresh.
