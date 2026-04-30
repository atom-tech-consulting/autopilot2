# TB-129 — Web view: live task-detail page with prompt + streaming response

## Goal

Surface the SDK's per-run debug dumps in ap2 web so operators can watch in-flight task agents instead of waiting for task_complete/task_error events that may be hours away.

Scope:

(1) New `/task-run/<run-id>` detail page that reads `.cc-autopilot/debug/<id>.prompt.md` (full system+user prompt the agent saw), `.stream.jsonl` (compact per-message summaries — preferred for the live view), and `.messages.jsonl` (full bodies, expandable per-row). Use the existing seq + stream-event schema from `daemon.py:_summarize_message`.

(2) Link to the detail page from BOTH places where a run is referenced:
  - The `/events` page (and the home-page recent-events block): add a link/icon on each `task_start` event row.
  - The per-task view (`/task/<TB-N>`, `_render_task` in web.py): for each historical run of that task, link to its detail page if the debug files still exist on disk. Surface a "Runs" or "Attempts" section listing all runs (sourced by tailing events.jsonl for `task_start` events with matching task id) — most recent first, terminal status badge per row when known.

The run-id key is the debug-filename prefix (e.g. `20260430T064735Z-TB-123`), parseable from any `task_start` event's timestamp + task. The matching debug files may have been pruned (long-lived projects); the link should only render when at least the .stream.jsonl file is present on disk.

(3) Auto-refresh: detail page polls every ~3s while the task is in-flight (no terminal `task_complete`/`task_error`/`task_state_violation` event for that task yet); stops polling once a terminal event lands and shows the final verdict inline. Use a tiny `<script>` with `setInterval` + `fetch` on a JSON sub-endpoint that returns just the new stream rows since `seq=N` — avoids reloading the full page each tick.

(4) Render `text_preview`, `tool_calls` (name + args summary), `tool_results` (preview), `ResultMessage` usage/cost. Color-code rows by type (assistant/tool/tool-result/result).

(5) Local-only network binding stays as-is.

Why: today the only way to see what an agent is actually doing during a 1200s+ run is `tail -f` on the debug files in a separate terminal. The web UI is the natural place for this and avoids context-switching. The per-task linking lets operators audit past attempts (especially relevant for retry-loop tasks like TB-122/TB-123 hit) without grepping events.jsonl by hand.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)
