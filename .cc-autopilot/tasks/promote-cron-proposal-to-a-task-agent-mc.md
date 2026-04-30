# Promote cron proposal to a task-agent MCP tool, drop `report_result.cron`

## Goal

Replace the `cron` field on `report_result` with a dedicated
`mcp__autopilot__cron_propose` MCP tool that task agents can call
independently. Cron-proposal becomes a first-class action with its own
schema, error handling, and discoverable description — not a
JSON-stringified list piggybacked on the result payload.

## Why

Today's contract (TB-101) bundles cron proposal into `report_result`:

```
report_result(
    status, commit, summary, files_changed, tests_passed,
    cron='[{"action":"add","name":"foo","interval":"1h","prompt":"…"}]'
)
```

This conflates two concerns:

1. **Reporting task completion.** What the daemon needs to route the
   task (status + commit + summary + verification metadata).
2. **Proposing scheduled work for review.** A separate, optional, side
   request — "while I was working on X I noticed we should also fire Y
   periodically."

Problems with the conflation:

- **Discoverability.** `cron=<JSON list of ...>` is a string parameter
  buried in the `report_result` description. A task agent reading the
  available tools doesn't see "you can propose cron jobs" — it sees
  "the result tool takes a cron string." Easy to miss.
- **Schema brittleness.** The agent has to JSON-encode a list of dicts
  inside a string parameter. Each level of escaping is a place to
  fumble. A real MCP tool with structured args (`name`, `schedule`,
  `prompt`, `rationale`) is unambiguous.
- **Atomicity mismatch.** If the agent wants to propose three crons,
  it has to bundle them into one list inside `report_result`. With a
  dedicated tool, it can call `cron_propose` three times — each one
  gets its own `cron_proposed` event with its own rationale, which is
  what the operator's review surface wants anyway.
- **Failure isolation.** A malformed `cron` string today can crash
  result parsing. A separate tool fails in isolation; `report_result`
  still lands cleanly.
- **Symmetry with control agents.** Control agents have
  `mcp__autopilot__cron_edit` (direct mutation). Task agents would
  have `mcp__autopilot__cron_propose` (proposal, queued for operator
  review). Same domain, two clearly named tools partitioned by
  privilege.

## Design

### New MCP tool

```
mcp__autopilot__cron_propose(
    name: str,         # e.g. "weekly-perf-snapshot"
    schedule: str,     # interval like "1h" / "1d" / cron-expr (whichever
                       # cron.yaml currently accepts)
    prompt: str,       # the prompt body the cron job will use
    rationale: str,    # short prose: why this should fire on a schedule
) -> {"ok": True, "event_id": "<uuid>"}
```

Implementation in `ap2/tools.py`:

- `do_cron_propose(cfg, args)` — appends a `cron_proposed` event with
  `name` / `schedule` / `prompt` / `rationale` / `proposed_by_task`
  fields. **Does NOT mutate `cron.yaml`.** That's the existing
  semantics of the field-on-result path, kept intact.
- Tool decorator + registration in the autopilot MCP server, same
  pattern as `pipeline_task_start`.
- Add `mcp__autopilot__cron_propose` to `TASK_AGENT_TOOLS`.

### `report_result` schema change

Drop the `cron` parameter entirely:

```
report_result(
    status, commit, summary, files_changed, tests_passed
)
```

`result.TaskResult.cron` field stays for now (default empty list) but
is no longer populated from `report_result`. Could be deleted in a
follow-up once nothing reads it.

### Event vocabulary

`cron_proposed` event keeps its current shape, gains a
`proposed_by_task` field with the calling task's TB-id (the daemon can
plumb this through from the task context). `cron_proposal_rejected` /
`cron_proposal_error` unchanged.

### Operator review surface

Out of scope for this task — the existing surface (whatever it is —
`ap2 cron list`, web UI, manual yaml edit) carries over. The proposal
event stream is unchanged in shape, so consumers don't need to
update.

## Migration

This codebase is young (TB-101 added `report_result` recently; no
external callers). Drop the `cron` field, ship the new tool, update
the prompt + tests in one go. No deprecation window.

For projects already running ap2 (stoch): the daemon's editable
install means a redeploy lands both ends atomically. Task agents that
were going to use the old `cron=...` field will simply stop emitting
it; new task agents pick up the new tool.

## Scope

- `ap2/tools.py` — add `do_cron_propose` + register
  `mcp__autopilot__cron_propose`. Add to `TASK_AGENT_TOOLS`. Strip
  the `cron` parameter from `report_result`'s schema.
- `ap2/result.py` — remove the `cron` field from `TaskResult`, OR
  keep it default-empty if other code paths read it (verify with
  grep first).
- `ap2/daemon.py` — `run_task` no longer parses
  `result.cron`; ensure `cron_proposed` events are still emitted
  (they now come from the tool path, not the result-parsing path).
- `ap2/prompts.py` — `build_task_prompt` mentions the new tool,
  drops the `cron=...` instruction. Pinned in
  `tests/test_prompts.py` (or wherever).
- `ap2/tests/test_mcp_inventory.py` — pin
  `mcp__autopilot__cron_propose` in the advertised-tools set.
- New unit test: `do_cron_propose` emits `cron_proposed` event with
  the right shape.
- New e2e test: task agent calls `cron_propose` once + `report_result`
  → daemon receives both, emits `cron_proposed` with
  `proposed_by_task=TB-N`, completes the task.
- New real-SDK smoke (`tests/smoke/`): real Claude calls
  `cron_propose`, daemon captures it. Mirrors the pattern of
  `test_report_result_real_sdk.py`.

## Out of scope

- **Auto-accepting cron proposals.** `cron_propose` queues for review;
  separate path makes them live.
- **Operator review UI / CLI.** `ap2 cron approve <name>` is a nice
  follow-up; not required for this task.
- **`cron_propose` from control agents.** Control agents already have
  `cron_edit` (direct mutation); they don't need the proposal layer.
- **Removing `TaskResult.cron` entirely.** Keep the field as a
  default-empty for one cycle in case any reader hasn't been audited
  yet; delete in a follow-up.

## Verification

- [shell] `uv run pytest -q ap2/tests/` (regression gate)
- [shell] `grep -q 'cron_propose' ap2/tools.py` (tool registered)
- [shell] `! grep -E '"cron":\s*str' ap2/tools.py` (the
  `report_result` `cron` field is gone) (gating)
- [shell] `! grep -E 'cron=<JSON' ap2/tools.py` (description text
  for the dropped field is gone)
- New unit test: `do_cron_propose({"name":"x","schedule":"1h",
  "prompt":"do x","rationale":"y"})` writes a `cron_proposed`
  event with all four fields populated.
- New e2e test: task agent calls `cron_propose` once + `report_result`
  with the slimmed schema → daemon completes the task and emits the
  `cron_proposed` event with `proposed_by_task=TB-N`. (gating —
  proves end-to-end wiring)
- New real-SDK smoke: real Claude makes a single `cron_propose` call
  in response to a prompt that asks for one. Validates tool advert
  reaches the model + daemon's stream-walking captures the structured
  payload.
- All 423+ default tests still green; smokes still green.

## Decision log

- 2026-04-29 (this briefing): Filed in Backlog. The schema split is
  cheap (young codebase, no external callers) and pays back
  immediately in tool clarity + atomicity. Keeping
  `cron_proposed` event shape stable so the operator-review surface
  (whatever it is today) doesn't need to change. Symmetric with
  `cron_edit` for control agents.
## Attempts

### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] [shell] `uv run pytest -q ap2/tests/` (regression gate); [fail] All 423+ default tests still green; smokes still green.
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T075758Z-TB-123.prompt.md`, `stream: .cc-autopilot/debug/20260430T075758Z-TB-123.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T075758Z-TB-123.messages.jsonl`
### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] [shell] `uv run pytest -q ap2/tests/` (regression gate); [fail] [shell] `grep -q 'cron_propose' ap2/tools.py` (tool registered); [fail] [shell] `! grep -E '"cron":\s*str' ap2/tools.py` (the`report_result` `cron` field is gone) (gating); [fail] [shell] `! grep -E 'cron=<JSON' ap2/tools.py` (description textfor the dropped field is gone); [fail] New unit test: `do_cron_propose({"name":"x","sc
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T085130Z-TB-123.prompt.md`, `stream: .cc-autopilot/debug/20260430T085130Z-TB-123.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T085130Z-TB-123.messages.jsonl`
