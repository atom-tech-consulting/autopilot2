# Token-usage instrumentation across all SDK call sites

## Goal

Capture `usage.input_tokens`, `usage.output_tokens`, `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`, and `model_usage` from every `ResultMessage` emitted by every `sdk.query()` call across the four agent kinds (task, control, MM handler, prose-bullet judge), and surface the data so it can be aggregated by call-site/agent-kind for cost-tradeoff experiments.

Today only `total_cost_usd` is captured per call (and only on call sites that route through the daemon's `_log_message` — judge calls bypass it entirely). That's enough to answer "what did this run cost in dollars" but not enough to answer "where are the cache hits / misses, what's the input vs output token breakdown, how do agent kinds compare." Without those, cache-tuning experiments (`exclude-dynamic-system-prompt-sections`, judge batching, prompt restructuring) have no measurement surface.

## Scope

Files to touch:

- `ap2/daemon.py` — extend `_summarize_message` and `_serialize_message_full` to pull `usage` (dict with `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) and `model_usage` (per-model breakdown) from `ResultMessage`. Three more lines per function.
- `ap2/verify.py` — `_judge_prose_bullet` currently iterates `async for msg in sdk.query(...)` and discards the messages. Wire a per-judge-call stream-dump (or a lighter "ResultMessage extractor") so the usage / cost / cache fields land somewhere durable. Either: (a) write a per-judge `<ts>-judge-<task_id>-<bullet_idx>.stream.jsonl` alongside the existing per-run dumps, or (b) emit a `judge_call` event to `events.jsonl` carrying the usage payload, task_id, bullet_idx, and final verdict. Pick (b) — events.jsonl is the canonical aggregation surface and a `judge_call` event row composes naturally with the existing event-tail tooling.
- `ap2/web.py` — extend the events-table renderer (and the per-task-run detail page from TB-129) to display token / cache counters when present, so operators can spot expensive runs at a glance without leaving the UI. New columns / fields on the existing tables; opt-in via a `?show=tokens` flag if it adds clutter.
- `adhoc/token_breakdown.py` — small one-off script that walks `.cc-autopilot/debug/*.stream.jsonl` + `events.jsonl`, groups by agent kind (parsed from run-id pattern: `<ts>-TB-N` = task agent, `<ts>-status-report` = cron, `<ts>-ideation` = ideation, `<ts>-MM-*` = MM handler, `judge_call` events for judge), and prints a totals table: `{agent_kind, calls, input_tokens, output_tokens, cache_creation, cache_read, hit_rate, total_usd}`. Gitignored under `adhoc/`; not a long-term aggregation surface but enough for the cache experiment.
- Tests in `ap2/tests/test_daemon_recovery.py` (or wherever `_summarize_message` / `_serialize_message_full` are pinned) and `ap2/tests/test_verify_per_task.py` (or equivalent) for the judge instrumentation.

## Design

### Capture shape on the existing daemon-side dump

Today `_summarize_message`'s for-loop pulls scalar fields by name. Extend with:

```python
for k in ("model", "stop_reason", "num_turns", "total_cost_usd", "subtype"):
    v = getattr(msg, k, None)
    if v is not None:
        out[k] = v
# Token / cache counters live in nested dicts on ResultMessage.
for k in ("usage", "model_usage"):
    v = getattr(msg, k, None)
    if isinstance(v, dict) and v:
        out[k] = v
```

Same pattern in `_serialize_message_full`. The `usage` dict's shape is well-known (Anthropic API response): `{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, ...}` — pass through verbatim.

### Judge-call instrumentation

The judge today doesn't go through `_log_message` — it has its own loop in `_judge_prose_bullet`. Cleanest: emit a `judge_call` event to `events.jsonl` after each judge SDK call returns, with payload:

```json
{
  "ts": "...",
  "type": "judge_call",
  "task": "TB-N",
  "bullet_idx": 3,
  "bullet_kind": "prose",
  "verdict": "pass" | "fail" | "unverified",
  "duration_s": 12.4,
  "model": "claude-opus-4-7",
  "num_turns": 3,
  "total_cost_usd": 0.042,
  "usage": {
    "input_tokens": 8200,
    "output_tokens": 90,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 7800
  }
}
```

This is parallel to existing event vocabulary (`task_complete`, `task_state_violation`, etc.) — composable with `events.tail`, the web events table, the diagnose report. No new file format.

### Aggregator (`adhoc/token_breakdown.py`)

Walks `events.jsonl` for `judge_call` events; walks `.cc-autopilot/debug/*.stream.jsonl` for the per-run ResultMessage payloads (now carrying usage). Groups by:

- **Agent kind** — parsed from run-id filename:
  - `<ts>-TB-N.stream.jsonl` → `task`
  - `<ts>-status-report.stream.jsonl` → `cron:status-report`
  - `<ts>-ideation.stream.jsonl` → `cron:ideation`
  - `<ts>-MM-<post-id>.stream.jsonl` → `mm-handler`
  - `judge_call` events → `judge`
- **Total calls** per kind
- **Sum input_tokens / output_tokens / cache_creation / cache_read**
- **Cache hit rate** = `cache_read / (cache_read + cache_creation + input_tokens_uncached)`
- **Total $USD**

Single `print(table)` at the end. ~50 LOC, no dependencies beyond stdlib + existing event-reading helpers.

### Web view enhancement

Per-task-run detail page (TB-129) gains a small footer block with the run's totals (input / output / cache hit rate / cost). Events table optionally shows the same per-row when `?show=tokens` is set. Doesn't change default rendering — opt-in.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE "\"usage\"" ap2/daemon.py` — usage capture wired into `_summarize_message` / `_serialize_message_full`.
- `grep -qE "judge_call" ap2/verify.py` — judge-call event emission wired in.
- `grep -qE "judge_call" ap2/web.py` — events table recognizes the new event type (rendered like other event rows).
- New unit test in `test_daemon_recovery.py` (or wherever `_summarize_message` is pinned): a stubbed `ResultMessage` with `usage={"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":80}` round-trips through `_summarize_message` and `_serialize_message_full` with the `usage` dict present in the output.
- New unit test in `test_verify_per_task.py`: after a `_judge_prose_bullet` call against a stubbed SDK that returns a `ResultMessage` with usage fields, exactly one `judge_call` event lands in `events.jsonl` carrying `task`, `bullet_idx`, `verdict`, and the full `usage` dict.
- New unit test in `test_web.py`: the per-task-run detail page renders the run's `usage` totals (sum across all the run's ResultMessages in stream.jsonl) when present; gracefully omits the block when no usage data exists (legacy runs).
- `python3 adhoc/token_breakdown.py` runs against the real `.cc-autopilot/` and prints a table with rows for each agent kind and a non-empty totals row. Visual check; not gated.

## Out of scope

- A long-term aggregation surface (Grafana, time-series DB, cost dashboards). The aggregator script + web-view footer are enough for the experiment; if cost monitoring becomes an ongoing concern, file a separate task for a real dashboard.
- Per-call cost alerts ("status-report cost > $0.50, ping operator"). Out of scope for this task; could be a watchdog cron addition later.
- Aggregation across multiple ap2 projects. Each project has its own `events.jsonl` + debug dumps; cross-project totals can be done by hand if needed.
- Re-emitting historical usage data for runs already on disk. The instrumentation captures NEW runs; old runs stay untouched (their stream.jsonl already exists without usage).
- Capturing per-tool-call token cost. Tool-call costs aren't separately reported by the API; only message-level totals exist.
