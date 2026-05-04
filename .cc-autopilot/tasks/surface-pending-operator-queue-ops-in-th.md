# TB-161 — Surface pending operator queue ops in the web view

## Goal

The operator queue (`.cc-autopilot/operator_queue.jsonl`) is the canonical board-mutation path for `ap2 add / approve / delete / unfreeze / backlog / update / reject / ideate` — every CLI verb returns "queued; will land at next tick" and the actual mutation happens on the next daemon tick (≤30s). Today the only way to see what's pending is `cat .cc-autopilot/operator_queue.jsonl` from a shell.

This is a real visibility gap when:
- An operator rapid-fires several ops and wants to confirm they all queued correctly
- The daemon is paused / unresponsive (the recent incident: pause silently held the queue at 731 bytes; only `tail .cc-autopilot/daemon.log` revealed why) — a queue card in the web view would have surfaced "ops pending, daemon hasn't drained" at a glance
- The operator wants to verify a specific op (e.g. an `update` to a briefing) actually queued before the daemon's tick boundary

This task adds a pending-ops card to the existing web index (`/`), rendered when the queue file is non-empty. Each entry shows op kind, task_id (when applicable), enqueue timestamp, uuid prefix, and a compact kwargs summary. The card is hidden when the queue is empty (no perpetual "0 pending" noise).

## Scope

- `ap2/web.py` — new helper `_render_pending_queue(cfg) -> str` that reads `.cc-autopilot/operator_queue.jsonl`, parses each line, and emits an HTML block. Index handler (`/`) calls it and inserts the result above the events table when the queue has at least one entry. Empty queue → empty string returned, no card shown.
- `ap2/web.py` (CSS) — small `.pending-queue` styling block consistent with existing card-style elements (`.verif-summary`, run header). Yellow-tinted to match "pending" semantics.
- Tests in `ap2/tests/test_web.py`.

## Design

### What to render per entry

Each line in `operator_queue.jsonl` is a JSON object with at least `uuid`, `op`, `args`, `ts`. Concrete shape (from a real entry observed earlier in the session):

```json
{"uuid": "a9661087-42e4-47ad-b624-215ed2d240ae", "op": "update",
 "args": {"task_id": "TB-152", "title": "...", "fields": [...]},
 "ts": "2026-05-04T17:15:30Z"}
```

Render shape per entry:

```
[update]  TB-152  · ts=17:15:30Z  · uuid=a9661087  · fields=title,description,briefing
[add_backlog]  TB-N/A  · ts=17:18:02Z  · uuid=8b3f...  · title="Surface pending operator..."
[approve]  TB-152  · ts=17:18:09Z  · uuid=2e1b...
```

- Op kind in a small bracketed badge (color-coded by kind family if cheap; otherwise neutral)
- task_id when present in args (most ops have it; `add_backlog` has its pre-allocated TB-N inside args; `ideate` has none — render `TB-N/A`)
- ts shown HH:MM:SSZ (date implied; queue drains in <60s typically)
- uuid prefix (8 chars — matches typical git-short-sha display)
- A compact summary of the most relevant arg field for each op kind:
  - add_backlog → `title="..."` (truncated to 80 chars)
  - update → `fields=<csv of changed fields>`
  - approve / unfreeze / delete / reject / backlog → no extra (task_id is the load-bearing signal)
  - ideate → `force=<bool>` (TB-159's flag)

Don't try to render the full args payload — that's noise for at-a-glance ops. The full payload belongs in a `<details>raw json</details>` footer (same pattern as the events table).

### Where the card sits

Above the events table on the `/` index page. The events table is the dashboard's primary surface; pending ops live above it as a "things about to happen" preamble. When the queue is empty the card is omitted entirely (CSS-hidden vs. server-side omitted: prefer server-side — fewer bytes, no flicker).

### Auto-refresh

The `/` page already auto-refreshes via meta-refresh (per TB-130-era plumbing). No new refresh logic needed; the next refresh re-reads the queue.

### What NOT to render

- The applied-uuid bookkeeping (`operator_queue_state.json`) — that's daemon-internal accounting, not operator-facing.
- Drained-but-still-on-disk entries — queue.jsonl is truncated by the drain handler; anything in the file IS pending. (Verify: read the drain code briefly to confirm this — if the daemon does a "mark applied + leave on disk" instead of truncate, the helper must filter against the applied-uuid bookkeeping.)
- Any mutation controls (no "cancel queued op" button). Web stays read-only; cancellation belongs in CLI if/when needed.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "def _render_pending_queue" ap2/web.py` — helper is wired.
- `grep -qE "pending-queue" ap2/web.py` — CSS class is referenced (renders when applicable).
- prose: a test in `test_web.py` writes a synthetic `operator_queue.jsonl` with three entries (one `add_backlog` with title, one `update` with fields list, one `approve`), invokes `_render_pending_queue(cfg)` (or fetches `/` via the test harness), and asserts the rendered HTML contains all three op kinds, the corresponding task_ids, AND the per-op-kind summary shape (`title="..."` for add_backlog, `fields=...` for update, no extra for approve).
- prose: a test pins the empty-queue case — synthesize an empty (or missing) `operator_queue.jsonl`, render `/`, assert the page does NOT contain the `pending-queue` CSS class anywhere (card omitted entirely, not just hidden).
- prose: a test pins UUID truncation — entries are rendered with a uuid prefix (≤16 chars), not the full 36-char uuid (avoids horizontal overflow on narrow viewports).
- prose: a test pins the drained-entry filter — if the implementer chose to read raw queue.jsonl and filter via `operator_queue_state.json`, the test seeds an applied-uuid in the state file matching one of the three queue entries and asserts that entry does NOT appear in the rendered HTML. (Skip this bullet entirely if the daemon truncates the queue file on drain — confirm during implementation.)
- prose: visual smoke test in the briefing's commit message — operator manually opens `/` after this task lands, queues 2-3 ops, and confirms the card appears within one auto-refresh cycle. (Operator-checklist; not a unit test, but worth calling out so the implementer notices the visual integration.)

## Out of scope

- A standalone `/queue` page. The queue is small (typically <5 entries between ticks); a dedicated page is over-design. Lives on the index card only for v1.
- Cancel / re-order buttons. Web stays read-only; cancellation belongs in CLI.
- Visual op-kind color coding beyond the existing failure-tinted-row palette. If the colors-by-kind feel useful in v1, fine; if it adds CSS without clear payoff, skip.
- Showing applied (drained) ops as historical records on the same card. The events table already carries `operator_queue_drained applied=N` rows for that.
- Surfacing the same data in `ap2 status` CLI. CLI would need its own renderer; defer until friction observed.
- A web-side mutation surface (operator clicks → queue op). Out of band; the queue is CLI/MCP-driven by design.
