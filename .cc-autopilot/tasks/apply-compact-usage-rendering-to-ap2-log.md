# Apply compact `usage` rendering to `ap2 logs` (CLI parity with TB-179)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a goal.md (with Mission + `## Done when`), and walk away for a week without intervention." The CLI's `ap2 logs` is the operator's primary post-walk-away catch-up surface in the terminal — many operators reach for `ap2 logs -n 50` before they open the web UI. Today's rendering of `usage`-carrying events (`judge_call`, `task_run_usage`, `control_run_usage`) dumps the dict inline via `_short(limit=200)`, producing wrapped multi-line rows that drown signal in JSON noise — exactly the readability problem TB-179 is fixing on the web side.

TB-179 (blocking) ships a 6-field compact rendering for the same three event types in the web events table. The web and CLI surfaces should stay symmetric: an operator who reads the same event in `ap2 logs` and on `/events` should see the same shape so muscle-memory scanning works across both. This task adds the same compaction to `ap2 logs` so the CLI parity holds.

The implementation reuses TB-179's compact-summary helper directly. To enable cross-surface reuse cleanly, this task EXTRACTS the helper from `ap2/web.py` to `ap2/events.py` (the canonical event-formatting module — same architectural pattern TB-158 used to share `summarize_verification_failed` between CLI and web). After the move, both `cmd_logs` (CLI) and `_events_table` (web) consume the same single-source-of-truth formatter; future field-selection or formatting changes happen once.

Why now: TB-179's web compaction lands on its own cadence; without this CLI parity, an operator scanning the CLI gets the verbose dict dump even after the web is fixed — same readability tax operators just observed in this session, just on a different surface. Filing concurrent (blocked on TB-179) means the parity ships in lockstep so operators don't experience a "web fixed, CLI broken" intermediate state. The post-TB-179 helper is right there to consume; CLI parity is mostly plumbing.

## Scope

- `ap2/events.py` — extract / re-home the compact-summary helper from `ap2/web.py::_event_token_summary` (TB-157) and `ap2/web.py` whatever-helper-TB-179-introduces (likely `_compact_usage_row` or extension of `_event_token_summary`). Public name: `summarize_usage_event(event: dict, *, max_chars: int | None = None) -> str` — surface-agnostic compact string with the 6 fields + identity prefix from TB-179. The web-side renderer continues consuming it (no behavior change); CLI gains a consumer.
- `ap2/web.py` — replace the inline helper with an import + call to `events.summarize_usage_event`. No rendering change; same string content. Pin via web tests that the rendered HTML for these event types is byte-identical pre/post-extraction.
- `ap2/cli.py::cmd_logs` — extend the `if typ == "verification_failed":` special-case branch (added by TB-158) to include `judge_call`, `task_run_usage`, `control_run_usage`. Each routes through `events.summarize_usage_event` and prints the resulting line in place of the generic `<ts> <type:16s> key=val key=val ...` formatter.
- `ap2/cli.py` — `--json` path unchanged (regression-pinned per TB-158 pattern). Operators scripting against `ap2 logs --json | jq` continue to see the full event payload unchanged.
- `ap2/tests/test_cli.py` — extend the existing `test_cmd_logs_pretty_renders_verification_failed` pattern with three new tests, one per event type. Each synthesizes an event with the full verbose `usage` dict, runs `cmd_logs`, asserts:
  - The 6 compact fields appear in the rendered output
  - The verbose nested fields (`server_tool_use`, `iterations`, `service_tier`, `inference_geo`, the nested `cache_creation` object, `model_usage`) DO NOT appear inline
  - The `--json` flag produces the full unchanged payload (regression pin)
- `ap2/tests/test_events.py` — extend with a `test_summarize_usage_event_*` family pinning the helper's output shape (identity prefix per event type, 6 fields, no nested dicts, truncation behavior if `max_chars` is set).

## Design

### Why extract the helper to `ap2/events.py`

TB-158 set the precedent: when a rendering helper needs to be consumed by both CLI and web, it lives in `ap2/events.py` (or another shared module), not duplicated. The current TB-157 helper `_event_token_summary` is a private `_`-prefixed function in `ap2/web.py`; that placement made sense pre-TB-179 when only web consumed it. Post-TB-179 the helper grew (added identity-prefix logic per event type); both surfaces want the same logic. Re-homing to `events.py` matches the canonical-aggregation-surface principle the events module already serves.

Public name `summarize_usage_event` parallels `summarize_verification_failed` (TB-158's shared helper). Same shape, same module, same naming convention.

### CLI rendering shape

Mirror TB-179's web shape but adapted for terminal width. Target ~120 chars (typical terminal width):

```
2026-05-04T19:11:38Z  judge_call  task=TB-165 bullet=7/prose pass  in=6 out=287 cc=17016 cr=42310  $0.146  8.0s
2026-05-04T15:15:13Z  task_run_usage  task=TB-158 complete run=20260504T150009Z-TB-158  in=42 out=4123 cc=68234 cr=512891  $0.85  342.1s
2026-05-04T18:09:21Z  control_run_usage  label=ideation complete run=20260504T180620Z-ideation  in=18 out=2034 cc=49231 cr=104982  $0.42  178.3s
```

Alignment is loose (no fixed-width column widths) to keep the line short — TB-158 used the same approach for verification_failed rows. Operators looking for tabular alignment can pipe through `column -t` if they want.

### `--json` path unchanged

Same regression pin TB-158 used: when `args.json` is True, `cmd_logs` skips ALL pretty-formatters (including the new ones) and prints `json.dumps(e)` per event line. Operator scripts depending on raw output continue to work unchanged.

### Verbose-payload escape hatch

The CLI doesn't have an inline "click for raw json" toggle like the web does. Operators wanting the full payload of a specific event use:

```bash
ap2 logs --json -n 200 | jq 'select(.task=="TB-165")'
```

The pretty-formatted `ap2 logs` is for at-a-glance scanning; `--json` is the forensic surface. This split is consistent with TB-158's design.

### CSS / layout

N/A — CLI output is plaintext.

### Web parity check

After this task ships, the web events table and `ap2 logs` should render the same compact summary for the same event. A test in `test_web.py` (or `test_cli.py`, parallel) can assert that for a fixture event, both surfaces produce strings containing the same 6 numeric values. Optional but cheap to add as a sanity-check.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "summarize_usage_event" ap2/events.py ap2/cli.py ap2/web.py` — shared helper exists in events.py AND is consumed by BOTH cli.py and web.py (verify by line presence in all three).
- `python3 -c "from ap2.events import summarize_usage_event; assert callable(summarize_usage_event)"` — public helper is importable.
- `grep -nE "judge_call|task_run_usage|control_run_usage" ap2/cli.py` — all three event types referenced in `cmd_logs`'s pretty-rendering branch.
- prose: a test in `test_cli.py` named `test_cmd_logs_pretty_renders_judge_call` synthesizes a `judge_call` event with the full verbose `usage` dict, runs `cmd_logs` (without `--json`), asserts the captured stdout (a) contains the identity prefix `task=TB-N bullet=N/<kind>` and the verdict, (b) contains all 6 compact-field values (input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, total_cost_usd, duration_s), (c) does NOT contain `server_tool_use`, `iterations`, `service_tier`, or the nested `cache_creation` object's structure (nested braces).
- prose: parallel tests `test_cmd_logs_pretty_renders_task_run_usage` and `test_cmd_logs_pretty_renders_control_run_usage` exercise the other two event types, asserting the identity prefix shape per type (`task=TB-N status=...` for task_run_usage; `label=... status=...` for control_run_usage) plus the 6 compact fields.
- prose: a test pins the `--json` regression — `cmd_logs --json` against the same fixture produces output where every event line is parseable JSON containing the original verbose `usage` dict UNCHANGED (the nested fields ARE present in `--json` output). Pretty-formatting bypassed.
- prose: a test in `test_events.py` named `test_summarize_usage_event_returns_compact_string` calls the helper directly with each of the three event types and asserts the returned string is non-empty, contains the identity prefix appropriate to the type, contains the 6 numeric fields, and is shorter than ~200 chars (rough cap to ensure compaction is doing its job).
- prose: a test pins the events-jsonl storage pin — running through the cmd_logs pretty path does NOT mutate `events.jsonl` (read-only operation; pretty rendering is a display layer only).

## Out of scope

- Changing the storage shape of `events.jsonl`. The verbose payload stays persisted in full; only display compacts.
- Adding a CLI flag to opt out of compact rendering for these event types (`--no-compact`). v1 always compacts; if operators ask for the verbose form they use `--json | jq` or read events.jsonl directly.
- Per-model inline breakdown (`model_usage` field). Same as TB-179 — out of v1 inline scope; raw payload retains it.
- Color-coding rows by event type or cost magnitude. Plaintext CLI output; future TB if useful.
- Tabular alignment / fixed-column rendering. Loose alignment matches TB-158's verification_failed pattern.
- Updating `ap2 status` to render compact usage summaries. Status doesn't surface per-event detail; out of scope.
- Web UI changes. TB-179 owns the web-side compaction; this task is CLI parity only.
- Refactoring `_short()` truncation logic. The new compact path bypasses `_short()` for the three event types; legacy events without `usage` continue using `_short()` unchanged.
