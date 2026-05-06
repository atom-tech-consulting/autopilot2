# Compact `usage` blob rendering for token/cost events in the web events table

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The web view is the operator's scan-at-a-glance surface for catching up after a walk-away period — `http://127.0.0.1:8730/` and `/events` are where the operator looks first to understand "what happened while I was gone." Today's rendering breaks that scan-ability for any row carrying a `usage` blob: the dict is dumped inline via `_short()` truncation, producing rows like

```
2026-05-04T19:11:38Z judge_call task=TB-165 bullet_idx=7 bullet_kind=prose verdict=pass duration_s=8.002 model=claude-opus-4-7 num_turns=2 total_cost_usd=0.14617599999999997 stop_reason=end_turn usage={'input_tokens': 6, 'cache_creation_input_tokens': 17016, 'cache_read_input_tokens': 42310, 'output_tokens': 287, 'server_tool_use': {'web_search_requests': 0, 'web_fetch_requests': 0}, 'service_tier'… model_usage={'claude-haiku-4-5-20251001': {'inputTokens': 7636, 'outputTokens': 22, 'cacheReadInputTokens': 0, 'cacheCreationInputTokens': 0, 'webSearchRequests': 0, 'costUSD': 0.006605, 'contextWindow': 200000,…
```

…that wraps several lines, drowns the at-a-glance signal in JSON noise, and pushes the next event off-screen. Three event types carry this blob: `judge_call` (TB-157), `task_run_usage` (TB-165), `control_run_usage` (TB-166). Each task-agent run + each verifier prose-bullet judge produces multiple of them, so a busy hour of activity fills the page with this shape.

This task replaces the verbose dict-dump with a 5-6-field compact summary inline, keeping the full raw payload accessible via the existing `<details>raw json</details>` toggle (same pattern TB-158 used for `verification_failed` rows). The events table stays scan-able; the operator still has full forensic data one click away when they need it.

Why now: TB-165 and TB-166 (both shipped earlier today) added two new event types that ALSO carry the verbose `usage` payload, on top of the existing `judge_call`. The combined volume across a busy day saturates the events page — we just observed it firsthand inspecting today's session's events. Compacting now, before the project's history of these events grows, keeps every operator-scan-the-web session readable; deferring means the readability tax compounds with every task that ships.

## Scope

- `ap2/web.py::_events_table` (line ~754 area) — extend the special-case branch (which today only special-cases `verification_failed` per TB-158) to also rewrite the `extra` cell for `judge_call`, `task_run_usage`, `control_run_usage` event types. The compact summary uses `_event_token_summary` (TB-157, already at line 518) plus event-type-specific identity fields (task / label / bullet_idx).
- `ap2/web.py::_event_token_summary` (existing helper) — extend if needed to include the missing context fields, OR add a new sibling helper `_compact_usage_row(e: dict) -> str` that wraps `_event_token_summary` with the identity prefix. Either is fine; the verification bullet pins the OBSERVABLE rendering, not the internal structure.
- The existing `<details>raw json</details>` footer per row (already present in `_events_table`) is the escape hatch — operators clicking it see the full event payload unchanged. No data loss, just rendering compaction.
- `ap2/tests/test_web.py` — extend existing events-table render tests with fixtures for each of the three event types; assert the compact summary contains the 6 fields AND does NOT contain the verbose nested-dict shape (`server_tool_use`, `iterations`, `cache_creation` nested object, etc.).

## Design

### The 6 fields to display inline

Pick the fields that answer "what cost what?" at a glance:

1. **`input_tokens`** — from `usage`. The fresh-input cost driver.
2. **`output_tokens`** — from `usage`. The output cost driver.
3. **`cache_creation_input_tokens`** — from `usage`. New cache writes (cost ~3.75x base).
4. **`cache_read_input_tokens`** — from `usage`. Cache hits (cost ~0.1x base).
5. **`total_cost_usd`** — from event top-level. The dollars-summary.
6. **`duration_s`** — from event top-level. Wallclock time.

Per-event-type identity prefix (so the operator knows what they're looking at):

- `judge_call` → `task=TB-N bullet_idx=N bullet_kind=prose verdict=pass`
- `task_run_usage` → `task=TB-N status=complete run_id=<...>`
- `control_run_usage` → `label=<ideation|cron-status-report|MM-...> status=complete run_id=<...>`

### Compact rendering shape

Target one-line rendering width ~140 chars (fits ~80% of typical browser-width inspection):

```
2026-05-04T19:11:38Z  judge_call  task=TB-165 bullet=7/prose pass  in=6 out=287 cc=17016 cr=42310  $0.146  8.0s
```

Rough pattern: `<ts>  <type>  <identity>  <token-tuple>  <cost>  <duration>`. The verbose nested fields (`server_tool_use`, `iterations`, `cache_creation` nested object, `model_usage`) drop from the inline cell entirely — they live in the `<details>raw json</details>` toggle.

`model_usage` is a special case: it has its own compact summary opportunity (per-model breakdown). For v1, omit from the inline cell — operators rarely need the per-model split at a glance, and the raw-json toggle has it. If operators ask for per-model inline summary later, that's a follow-up.

### Identity-prefix variants per event type

The three event types carry different load-bearing identity fields:

```python
if typ == "judge_call":
    identity = f"task={task} bullet={bullet_idx}/{bullet_kind} {verdict}"
elif typ == "task_run_usage":
    identity = f"task={task} {status} run={run_id}"
elif typ == "control_run_usage":
    identity = f"label={label} {status} run={run_id}"
```

The `_compact_usage_row` helper returns the identity + token-tuple + cost + duration string; `_events_table` plugs it into the `extra` cell.

### CSS / layout

No new CSS. The compact rendering lives in the existing `extra` cell of the events row; the existing `<details>` toggle for raw json renders below the row when expanded. No layout changes required.

### CLI scope (out)

`ap2 logs` rendering is out of scope for this task. The CLI today already renders these event types via the generic `key=value` formatter, which truncates long values via `_short(limit=200)`. CLI is terminal-scrollable; scan-ability is less of an issue than on the web grid. If `ap2 logs` cleanup is wanted, it's a separate small TB.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "judge_call|task_run_usage|control_run_usage" ap2/web.py` — all three event types are referenced in the events-table compact rendering branch (verify by line-number proximity to the `verification_failed` special-case).
- `python3 -c "from ap2.web import _event_token_summary; assert callable(_event_token_summary)"` — helper still exists and is callable (regression check — TB-157's helper survives the refactor).
- prose: a test in `test_web.py` synthesizes an `events.jsonl` with one `judge_call` event carrying a full `usage` dict (with the nested fields), invokes `/events` rendering (or `_render_events` directly), and asserts the rendered HTML for that row:
  - **CONTAINS** the 6 numeric fields shown compactly: `input_tokens` value, `output_tokens` value, `cache_creation_input_tokens` value, `cache_read_input_tokens` value, the dollar amount from `total_cost_usd`, and the `duration_s` value
  - **CONTAINS** the identity prefix (`task=TB-165 bullet=7/prose pass` or close paraphrase)
  - **DOES NOT CONTAIN** in the inline `extra` cell: `server_tool_use`, `iterations`, `service_tier`, `inference_geo`, `cache_creation` (the nested-object key, distinct from `cache_creation_input_tokens`)
  - **DOES CONTAIN** in the row's `<details>raw json</details>` block (the escape-hatch): the full original payload including all the omitted-from-inline fields
- prose: a test pins the same shape for `task_run_usage` and `control_run_usage` event types — both render with their identity prefixes (`task=`/`label=`) and both omit the nested-dict fields from the inline cell.
- prose: a test pins backward-compat — events without a `usage` dict (e.g. legacy events from before TB-157) render via the existing generic `_short()` path; the new compact path is opt-in by event type, not a global rewrite.

## Out of scope

- CLI rendering compaction (`ap2 logs` for these event types). Separate concern; CLI is terminal-scrollable, less affected.
- Removing the verbose payload from `events.jsonl` storage. The data stays persisted in full; only the inline web display compacts. Operators inspecting `events.jsonl` directly or via `--json` continue to see the full shape.
- Per-model inline breakdown (`model_usage` field). v1 omits from inline; follow-up TB if operators ask for it.
- Configurable inline-field selection via env or query param. Six hard-coded fields are sufficient for v1.
- Web-UI sortable / filterable columns for the cost/token fields. Today's events table is row-streamed, not tabular. A separate, larger TB if the dashboard ever pivots to a structured tabular view.
- Aggregating `usage` across a task-run on the per-task-run detail page. The `/task-run/<run-id>` page already has a usage-totals footer (TB-157's `_render_run_usage_footer`); that's complementary to this task and unchanged.
- Color-coding rows by cost magnitude. Out of v1 scope; the compact rendering already addresses the readability problem without tinting.
