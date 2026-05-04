# Persist control-agent token usage + stream/messages dumps for ideation, cron, MM handler

## Goal

TB-165 wired `task_run_usage` events + debug-dump retention into `run_task` so task-agent token cost is durable in `events.jsonl` and per-message detail survives in `.cc-autopilot/debug/`. The parallel call site `_run_control_agent` (`ap2/daemon.py:754-812`) — used by **ideation** (`ap2/ideation.py`), **cron status-report** (`ap2/status_report.py` via `run_status_report`), and the **mattermost handler** (`ap2/daemon.py` mm-loop) — has no equivalent instrumentation:

- Only `prompt.md` is written; `stream.jsonl` and `messages.jsonl` are NEVER written. The SDK message stream is consumed with `async for _ in sdk.query(...): pass` — every `ResultMessage` (carrying `usage` + `total_cost_usd`) is discarded.
- Existing control-agent events (`ideation_timeout`, `ideation_error`, `ideation_state_updated`, `ideation_empty_board`, `mattermost_reply`, `status_report`, `cron_complete`) carry NO token / cost / cache-hit data.

Today's ideation timeout (2026-05-04 18:11Z, ~5 min of opus-xhigh) is a concrete example: the cost is unrecoverable. Same gap applies to every status-report cron (~12/day) and every chat exchange via the MM handler.

This task brings control-agent runs to parity with TB-165's task-agent instrumentation: capture stream + messages on disk, and emit a `control_run_usage` event to events.jsonl on every terminal path (success, timeout, error).

## Scope

- `ap2/daemon.py` — refactor `_run_control_agent` so the SDK iteration captures each envelope via `_summarize_message` + `_serialize_message_full` (already used by `run_task`) and writes to `<run_id>.stream.jsonl` + `<run_id>.messages.jsonl`. The `<run_id>` follows the same `<compact_ts>-<label>` pattern that `_prep_debug_dumps` already emits — `_prep_debug_dumps` returns all three paths today; `_run_control_agent` currently only uses the prompt path.
- `ap2/daemon.py` — emit a new `control_run_usage` event from `_run_control_agent` on every terminal branch (success, timeout, error). Same payload shape as TB-165's `task_run_usage` plus a `label` field naming the run kind (`"ideation"`, `"cron-status-report"`, `"MM-<post-id>"`, etc.) — equivalent to the existing label vocabulary used in `_prep_debug_dumps` filenames and parsed by `adhoc/token_breakdown.py`'s `classify_label`.
- `ap2/daemon.py` — `_run_control_agent` return signature stays compatible — the helper still returns `(timed_out, error, stderr_tail, prompt_dump_path)`. Callers in `ap2/ideation.py`, `ap2/status_report.py`, and the MM-handler loop don't change.
- `ap2/tests/test_daemon*.py` (or `test_ideation*.py`) — extend control-agent run tests to assert the new behavior.
- `adhoc/token_breakdown.py` — DOCSTRING-only update noting that `control_run_usage` events now exist as a parallel surface to the per-run `stream.jsonl` walk. Implementation can stay (walking stream.jsonl now covers control runs too).

## Design

### `control_run_usage` event shape

Mirror TB-165's `task_run_usage` shape. Add a `label` field; replace `task` with `label` (or carry both — `task=null` for control runs). Recommended:

```json
{
  "ts": "<iso>",
  "type": "control_run_usage",
  "label": "ideation | cron-status-report | MM-<post-id> | cron-<name>",
  "run_id": "<compact_ts>-<label>",
  "status": "complete | timeout | error",
  "duration_s": <float>,
  "num_turns": <int>,
  "model": "claude-opus-4-7[1m]",
  "usage": {
    "input_tokens": <int>,
    "output_tokens": <int>,
    "cache_creation_input_tokens": <int>,
    "cache_read_input_tokens": <int>
  },
  "model_usage": {...},
  "total_cost_usd": <float>,
  "error": "<type: msg>" | null,
  "stderr_tail": "<last 30 stderr lines>" | null
}
```

`status` mirrors the natural triple — `complete` for the no-timeout / no-exception path, `timeout` for `asyncio.TimeoutError`, `error` for any other exception. Pre-existing event names (`ideation_timeout`, `ideation_error`) do NOT change; `control_run_usage` is purely additive.

### Source of usage data

The trailing `ResultMessage` carries the totals, exactly as in `run_task` post-TB-157. Reuse `_summarize_message` (already in `ap2/daemon.py:1002`) which returns a dict with `usage` + `model_usage` + `total_cost_usd` + `num_turns` + `model` + `stop_reason`. Read the LAST `ResultMessage` summary from the in-memory consume list. If the run timed out before the SDK emitted a `ResultMessage`, emit `control_run_usage` with empty usage (`{}`) and `note=stream_incomplete` — same pattern TB-165 used for crash paths.

### Storage cost concern

Control agents fire frequently:

- ideation: at most every 2h when board empty (rare today, more frequent post-TB-160's threshold)
- status-report cron: every 2h
- MM handler: every chat message in the watched channel

Persisting `stream.jsonl` + `messages.jsonl` for every fire could grow `.cc-autopilot/debug/` rapidly. A status-report at xhigh effort might generate tens of KB; over 24h that's ~12 reports × ~50KB ≈ 600KB/day for status-report alone. Acceptable for an internal daemon; pruning is **out of scope** here, same as TB-165. Operator can `find .cc-autopilot/debug -mtime +30 -delete` if needed; structured retention is a separate TB.

### Refactor opportunity (optional)

`run_task` and `_run_control_agent` now share substantial plumbing: prep dumps, consume + summarize + serialize messages, write streams to disk, capture trailing ResultMessage, emit a `*_run_usage` event. A shared helper (`_consume_and_persist(cfg, sdk_query, label, *, stream_dump, messages_dump) -> ConsumeResult`) would make the two call sites symmetric and reduce duplication. NOT required by this task — the implementer can choose to refactor or copy-paste; the verification bullets pin the OBSERVABLE behavior, not the internal structure.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "control_run_usage" ap2/daemon.py` — event emission is wired into `_run_control_agent`.
- `grep -nE '"control_run_usage"' ap2/tests/` — at least one test asserts the event shape.
- prose: a test in `test_daemon*.py` (or `test_ideation*.py`) runs a control-agent invocation (real or stubbed SDK) to a successful complete and asserts that (a) the corresponding `<compact_ts>-<label>.stream.jsonl` AND `<compact_ts>-<label>.messages.jsonl` files exist on disk after `_run_control_agent` returns, AND (b) `events.jsonl` contains a `control_run_usage` event whose `label` matches the run's label, `status="complete"`, and `usage.input_tokens` / `output_tokens` / `total_cost_usd` are non-zero (or match the stubbed ResultMessage values).
- prose: a test pins the timeout path — synthesize a control-agent run that times out (the existing TimeoutError branch); assert `control_run_usage` is emitted with `status="timeout"`, the prompt + (partial) stream + (partial) messages files all exist on disk, and the existing label-specific event (`ideation_timeout` etc.) ALSO fires unchanged.
- prose: a test pins the error path — synthesize a control-agent run that raises an exception inside `sdk.query` (mock the SDK to raise); assert `control_run_usage` with `status="error"` AND `error="<Type>: <msg>"` is emitted, dumps survive, and the existing `ideation_error` (or equivalent) event also fires.
- prose: a test pins the run_id format — `control_run_usage.run_id` equals the `<compact_ts>-<label>` filename prefix shared with the on-disk dumps, so an operator can `ls .cc-autopilot/debug/<run_id>.*` after grepping the event for forensic inspection.
- prose: a test pins coverage for all three call sites — at minimum, ideation (`_maybe_ideate` calling `_run_control_agent`) AND status-report (`run_status_report`) both emit `control_run_usage` with the appropriate `label` value. (MM handler can be a stretch test if the test harness supports it; otherwise pin the helper-level behavior and trust call-site uniformity.)

## Out of scope

- Retention / pruning policy for `.cc-autopilot/debug/`. Same out-of-scope rationale as TB-165: operators can manually `find … -mtime +30 -delete` if it grows; structured retention is a separate TB.
- Cost alerting / thresholds (e.g. "warn if a single control run cost > $X"). Observability layer first; thresholds later if friction observed.
- Backfilling the cost of control runs that already happened (today's ideation timeout, prior status-reports). Pre-this-task data is gone; the gap is accepted.
- Web UI surface for `control_run_usage` events specifically. The existing events table will pick up the new event kind once it lands; a dedicated `/control-run/<run_id>` detail page mirroring `/task-run/<id>` is a separate TB if useful.
- Refactoring `adhoc/token_breakdown.py` to read `control_run_usage` events instead of stream.jsonl. Both surfaces work; switching is optional polish.
- Renaming the existing label-specific events (`ideation_timeout` → `control_run_timeout`). Additive only; existing event vocabulary stays.
- Splitting per-run-kind event types (`ideation_run_usage` vs `cron_run_usage` vs `mm_run_usage`). One unified `control_run_usage` event with a `label` field is sufficient and keeps `events.jsonl` grep-friendly.
