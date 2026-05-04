# Persist task-run token usage in events.jsonl + retain debug dumps on success

## Goal

Today, on successful task complete, the daemon deletes the per-run debug dumps (`prompt.md` + `stream.jsonl` + `messages.jsonl`) at `ap2/daemon.py:539-545`. Failures preserve all three (`_prep_debug_dumps` docstring at `ap2/daemon.py:1360-1367`).

Two consequences:

- **Token usage for cleanly-successful runs is unrecoverable.** TB-157 captured `usage` + `total_cost_usd` per `ResultMessage` into `_summarize_message` / `_serialize_message_full` and surfaced it on the per-task-run web detail page (`_render_run_usage_footer`, `ap2/web.py:1262`), but never persisted run-level totals to `events.jsonl` — only `judge_call` events emit token data there. When the daemon deletes the stream on success, the only on-disk record of task-agent token cost goes with it.

- **`adhoc/token_breakdown.py` systematically over-represents failure cost.** Its inputs are `debug/*.stream.jsonl` (only failed runs survive) plus `judge_call` events. The script's "by agent kind" totals report failure-tinted task-agent numbers; "what does a typical clean implementation task cost?" can't be answered from current state.

Fix both halves:

1. **Emit a `task_run_usage` event** capturing run-level totals (input/output/cache-creation/cache-read tokens, total_cost_usd, num_turns, duration_s, model, status) before any deletion happens. Persistent in events.jsonl regardless of success/failure. Cheap (one event per run).

2. **Stop deleting debug dumps on success.** Same retention as failures — all three files survive. Per-message detail stays available for cache-tuning experiments, prompt-iteration, and post-hoc debugging.

## Scope

- `ap2/daemon.py` — `run_task` (around line 532-545): replace the success-path `unlink` block with a no-op (or simply remove). Both success and failure paths now retain debug files.
- `ap2/daemon.py` — `run_task` (success and failure branches both): emit a `task_run_usage` event after the SDK stream completes and before any post-run dispatch. The usage data lives on the trailing `ResultMessage` envelope already captured by `_summarize_message` (TB-157); read from the in-memory summary list rather than re-parsing stream.jsonl.
- `ap2/events.py` (only if a structured helper is warranted) — the existing `events.append(cfg.events_file, type, **payload)` pattern is enough; no new helper unless `task_run_usage` payload assembly gets repeated.
- `ap2/daemon.py` — `_prep_debug_dumps` docstring (`ap2/daemon.py:1360-1367`): update the retention statement — "Files survive both success and failure; old `delete-on-success` behavior was removed in TB-XXX."
- `ap2/tests/test_daemon*.py` — extend existing run-task tests to assert the new behavior.
- `adhoc/token_breakdown.py` — DOCSTRING-only update noting that `task_run_usage` events are now an alternative aggregation surface (queryable directly from events.jsonl). Implementation unchanged: walking stream.jsonl already covers all runs after this change.

## Design

### `task_run_usage` event shape

```json
{
  "ts": "<iso>",
  "type": "task_run_usage",
  "task": "TB-N",
  "run_id": "<compact_ts>-TB-N",
  "status": "complete | verification_failed | error_max_turns | error_timeout | ...",
  "duration_s": <float>,
  "num_turns": <int>,
  "model": "claude-opus-4-7[1m]",
  "usage": {
    "input_tokens": <int>,
    "output_tokens": <int>,
    "cache_creation_input_tokens": <int>,
    "cache_read_input_tokens": <int>
  },
  "model_usage": {
    "claude-opus-4-7": {"inputTokens": ..., "outputTokens": ...},
    "claude-haiku-4-5-20251001": {...}
  },
  "total_cost_usd": <float>
}
```

`run_id` matches the `<compact_ts>-<task_id>` debug-dump filename prefix so a tooling user can grep for it across both surfaces.

`status` mirrors the `parsed.status` value used in the existing post-run branch — same vocabulary as `task_complete` and `_handle_failure`.

### When to emit

Emit on BOTH success and failure paths (mirror `task_complete` / `task_complete status=verification_failed` shape). The event is purely observational — no behavior depends on it. Emit AFTER the SDK stream completes and `parsed` is built, BEFORE the move-to-complete / failure-handler branches. Crash paths where no `ResultMessage` was ever received (SDK error before stream end) MAY skip the event — log a `task_run_usage_missing` debug event instead, OR emit a `task_run_usage` with empty usage and a `note=stream_incomplete` field. Implementer's call; the existing crash path already preserves dumps so no data is lost either way.

### Source of usage data

The trailing `ResultMessage` carries the totals. `_summarize_message` already extracts them into a dict shape with `usage`, `model_usage`, `total_cost_usd`, `num_turns`, `model`, `stop_reason`. Read the last `ResultMessage` summary from the in-memory list (the one already written to stream.jsonl) — don't re-parse the file. If multiple ResultMessages appear in a single run (shouldn't happen today, but be defensive), use the LAST one.

### Storage cost of retaining success dumps

Today's debug/ has ~131 files for ~25 retained (failure) runs — ~5 files per run on average (3 per task run + cron prompt-only files). At ~100KB-1MB per stream/messages file, that's ~5-50MB. Retaining successful runs likely doubles the count over the next 50 task runs (~100MB worst case). Acceptable for an internal daemon. Pruning policy is **out of scope** — operator can `find .cc-autopilot/debug -mtime +30 -delete` if it grows; if a structured retention policy is wanted later, it's a separate TB.

### Backwards compatibility

- `adhoc/token_breakdown.py` continues to work — its "walk stream.jsonl" pass now covers ALL runs (success + failure), making its totals MORE accurate after this change.
- Web `/task-run/<run-id>` continues to work — `_render_run_usage_footer` reads from stream.jsonl rows; success runs now have stream.jsonl preserved, so the page renders usage for them too (was rendering nothing on clean runs).
- Existing tests asserting "successful run deletes debug dumps" must be updated. Locate via `grep -rn "unlink\|stream_dump\|messages_dump" ap2/tests/`.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "task_run_usage" ap2/daemon.py` — event emission is wired into `run_task`.
- `grep -nE '"task_run_usage"' ap2/tests/` — at least one test asserts the event shape.
- prose: a test in `test_daemon*.py` runs a task agent (real or stubbed SDK) to a successful complete and asserts that (a) the `prompt.md` / `stream.jsonl` / `messages.jsonl` files for that run still exist on disk after the post-run state-commit step, AND (b) `events.jsonl` now contains a `task_run_usage` event whose `task` matches the run's TB-N AND whose `usage.input_tokens` / `output_tokens` / `total_cost_usd` are non-zero (or match the values placed in the stub ResultMessage).
- prose: a test pins the failure-path parity — a task that hits `verification_failed` ALSO emits `task_run_usage`, with `status=verification_failed` and the same usage fields populated. Pre-existing failure-path retention behavior (debug files preserved) is unchanged.
- prose: a test pins the run_id format — `task_run_usage.run_id` equals the `<compact_ts>-<task_id>` filename prefix of the debug dumps, so an operator can `ls .cc-autopilot/debug/<run_id>.*` after grepping for the event.
- prose: a test pins the absence of the deletion — synthesizing a successful run, assert `prompt.md` / `stream.jsonl` / `messages.jsonl` are STILL on disk after `run_task` returns. (Pairs with the existing failure-path retention test, if one exists; otherwise add both.)
- prose: the `_prep_debug_dumps` docstring (`ap2/daemon.py:1360-1367`) reflects the new retention rule. The "Failures keep all three files; `run_task` deletes them on successful complete" sentence is updated/removed.

## Out of scope

- Retention / pruning policy for debug/. Operators can manually clean up via `find` if it grows; structured retention is a separate TB if needed.
- Web UI surface for `task_run_usage` events specifically. The existing `/task-run/<run-id>` page already renders run-level usage from the stream; the new event makes the same data available cross-run via `events.jsonl`, but no new page or column is required for v1.
- Cost alerting / per-task threshold notifications.
- Compressing debug dumps. Plain-text retention is fine at current volume; gzip-on-archive is a separate TB if storage becomes a concern.
- Refactoring `adhoc/token_breakdown.py` to read `task_run_usage` events instead of stream.jsonl. Both surfaces work; switching is optional polish.
- Backfilling missing usage data from past `task_complete` events. Pre-this-task-runs are gone; we accept the gap.
## Attempts

### 2026-05-04 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -nE '"task_run_usage"' ap2/tests/` — at least one test asserts the event shape.
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260504T183101Z-TB-165.prompt.md`, `stream: .cc-autopilot/debug/20260504T183101Z-TB-165.stream.jsonl`, `messages: .cc-autopilot/debug/20260504T183101Z-TB-165.messages.jsonl`
