# Queue-route `ap2 ack` + `operator_log_append` MCP tool (eliminate false-positive state violations on operator_log.md)

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention." The walk-away promise rests on the operator NOT having to firefight false-positive rollbacks burning real SDK cost. Today the operator-log append path violates that promise.

Two entry points share `tools.do_operator_log_append` (`ap2/tools.py:1436-1481`) and both write `.cc-autopilot/operator_log.md` synchronously — bypassing the operator queue:

- **CLI: `ap2 ack`** (`cmd_ack`, `ap2/cli.py:1360-1375`)
- **MCP tool: `operator_log_append`** (`ap2/tools.py:3231-3232`), invoked by the Mattermost handler when an operator sends `@claude-bot ack: ...` / `@claude-bot done: ...` chat messages

Both write paths fire from contexts that race with running task agents:
- CLI: the operator types `ap2 ack` while a task is in flight (the most common operator behavior — they're reasoning aloud while the task they just dispatched runs)
- MCP: the Mattermost handler control agent runs concurrently with the task loop post-TB-122 split, so chat-driven acks land mid-task

`operator_log.md` is fenced (`TASK_AGENT_FENCED_PATHS`, `ap2/tools.py:3646`) and IS NOT in `_VIOLATION_CHECK_EXCLUDED_PATHS` (`ap2/rollback.py:67-70`), so the post-hoc snapshot diff (TB-110) attributes the operator's mid-run write to the task agent and rolls the run back. Concrete cost on post-train at 2026-05-12T06:40-07:14Z: three runs (TB-70 ×2, TB-71) burned ~$12.55 in SDK tokens to false-positive rollbacks attributable to operator-side `ap2 ack` calls between task_start and task_complete.

This task retrofits the ack path onto the operator-queue routing pattern — Shape B from the design discussion: rename the existing synchronous-write logic to a drain-only internal helper, give the CLI and MCP tool new queue-append entry points, register an `ack` op in `OPERATOR_QUEUE_OPS`, drain-side handler invokes the renamed helper. Mirrors TB-189 (`classify`) and TB-193 (`update-goal`) retrofits exactly.

Why now: post-train just demonstrated the bug live, costing real SDK money and creating a 30-minute operator-cleanup cascade (freeze attempts, fake-TB-N blocker workarounds, multiple correction acks each of which would trigger ANOTHER violation in any newly-running task). Every hour this stays unfixed risks repeating the cascade on any project where the operator uses `ap2 ack` while a task is dispatched.

## Scope

- `ap2/tools.py` — rename existing `do_operator_log_append` (the synchronous-write logic, lines 1436-1481) to `_apply_operator_ack`. Keep the same signature and semantics — it becomes a drain-only internal helper invoked from the queue-drain handler.
- `ap2/tools.py` — new public `enqueue_operator_ack(cfg: Config, args: dict) -> dict` that validates args (same `note`-required check the old function did) and then calls `do_operator_queue_append({"op": "ack", "note": ..., "task_id": ...})`. Returns the queue-append result.
- `ap2/tools.py` — register `"ack"` in `OPERATOR_QUEUE_OPS` (the tuple at line 1519 or wherever the registry is). Add a drain-side branch in `drain_operator_queue` (locate via `grep -n "OPERATOR_QUEUE_OPS\|def drain_operator_queue" ap2/tools.py`) that handles `op == "ack"` by calling `_apply_operator_ack(cfg, rec["args"])`. The drain runs at tick boundary, BEFORE task dispatch, so the operator_log.md write lands during the daemon's lock window and doesn't race a task's snapshot.
- `ap2/cli.py::cmd_ack` (line 1360-1375) — call `tools.enqueue_operator_ack(...)` instead of `tools.do_operator_log_append(...)`. Output prints "queued ack (will land at next tick)" — slight UX change from today's immediate "appended to operator_log.md" but consistent with the rest of the queue-routed CLI verbs (`approve`, `reject`, `classify`, `update-goal`, etc.).
- `ap2/tools.py` MCP tool registration (line 3221-3232) — the `operator_log_append` MCP tool's body changes to `return enqueue_operator_ack(cfg, args)` (queue-routed). The tool's public name and arg shape stay the same so existing chat handlers don't break.
- `ap2/tests/test_tools.py`, `ap2/tests/test_cli.py`, `ap2/tests/test_operator_queue.py` — new and updated tests.

## Design

### Why shape B (rename + new entry) over shape A (retrofit-in-place)

Shape B keeps the drain-side `_apply_operator_ack` function single-purpose (write to operator_log.md, emit `operator_ack` event). The new `enqueue_operator_ack` is also single-purpose (validate + queue-append). The two roles are operationally distinct — drain happens at tick boundary under board lock; queue-append can happen any time. Co-mingling them in one function (shape A) would force every caller to think about whether they're calling at queue-append time or drain time.

Shape B mirrors how `do_board_edit` and `do_operator_queue_append` separate the surface from the apply: the CLI/MCP surfaces queue-append, the drain applies.

### What `op="ack"` carries in the queue payload

```json
{
  "uuid": "<generated>",
  "op": "ack",
  "args": {
    "note": "<operator's prose>",
    "task_id": "TB-N or empty string"
  },
  "ts": "<iso>"
}
```

No additional fields. Mirrors the `do_operator_log_append`'s existing arg dict.

### Drain-side audit line shape

The existing `applied operator-queued <op> → TB-N` audit line that the drain emits for every op also fires for `op="ack"` — that's a SEPARATE operator_log.md line from the ack itself. The drain order:

1. Apply the op (call `_apply_operator_ack(cfg, args)`) — appends the ack line to operator_log.md
2. Append the audit line `applied operator-queued ack → TB-N` to operator_log.md (or `applied operator-queued ack` when no task_id)

So a single ack produces TWO lines in operator_log.md: the operator's note (rich content) and the audit pointer. This matches today's pattern for other ops (e.g. an `approve` op produces both `ideation_approved task=TB-N` in events.jsonl AND `applied operator-queued approve → TB-N` in operator_log.md). The audit line is grep-able for "operator did X via the queue"; the rich line is grep-able for "what did the operator say."

Both writes happen at drain time, inside the daemon's lock window — neither races a task agent.

### Backwards compatibility

- `do_operator_log_append` is renamed `_apply_operator_ack`. Existing callers (`cmd_ack`, MCP tool, any tests) are updated to call `enqueue_operator_ack` instead. No external API breaks if external callers exist (locate via `grep -rn "do_operator_log_append" --include="*.py"` — pre-rename verify nothing outside ap2/ depends on the old name).
- The MCP tool's external name (`operator_log_append`) and arg shape stay unchanged. Chat handlers that send `@claude-bot ack: ...` continue to work; only the implementation under the tool changes.
- CLI: `ap2 ack` exit code stays 0 on success. Output changes from `<the appended line>` to `queued ack (will land at next tick)`. Operator scripts that parse the output (unlikely) would need updating; document in commit message.

### Should operator_log.md be added to `_VIOLATION_CHECK_EXCLUDED_PATHS`?

NO. Once ack is queue-routed, NO path writes operator_log.md mid-task (the drain runs pre-task-dispatch). Adding it to the exclusion list would be a workaround that masks future regressions — if some future path forgets to queue-route, we want the violation check to catch it. Keep the exclusion list minimal; the architectural fix is the queue-routing.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `python3 -c "from ap2.tools import OPERATOR_QUEUE_OPS; assert 'ack' in OPERATOR_QUEUE_OPS"` — ack op is registered.
- `grep -nE "def _apply_operator_ack|def enqueue_operator_ack" ap2/tools.py` — both new helpers present.
- `grep -qE "do_operator_log_append" ap2/cli.py ap2/tools.py` — old name absent from production code (only acceptable hit is a docstring or comment cross-reference).
- prose: a test in `test_cli.py` exercises `cmd_ack` end-to-end — synthesizes a project, calls `cmd_ack` with a note + task_id, asserts (a) `operator_queue.jsonl` contains one record with `op="ack"` AND the supplied note/task_id, (b) `operator_log.md` is NOT YET modified (write happens at drain time), (c) the CLI's stdout matches the "queued ack" shape (≤200 chars, contains "queued").
- prose: a test pins the drain-side behavior — synthesize an `ack` op in the queue, run `drain_operator_queue`, assert (a) `operator_log.md` now contains the ack line in the documented shape (`- <ts> [TB-N] — <note>`), (b) `events.jsonl` contains an `operator_ack` event with the note + task fields, (c) the audit line `applied operator-queued ack → TB-N` is also written to operator_log.md.
- prose: a test pins the MCP-tool path — invoke the `operator_log_append` MCP tool with the same args; assert it produces the same queue-append record (verify by reading queue.jsonl).
- prose: a regression test pinning the bug fix — synthesize a state where a task agent's run window encloses an `ap2 ack` call. Concretely: create a fake task in Active, run `cmd_ack`, run the daemon's snapshot-hash check helper (locate via `grep -nE "FENCED_PATHS_FOR_VIOLATION_CHECK\|state_violation" ap2/rollback.py ap2/daemon.py`), assert NO state_violation fires for operator_log.md (because the ack is now in the queue, not in operator_log.md — the file is unchanged until drain).
- prose: a test pins backwards-compat — the `operator_log_append` MCP tool's `name` field stays `"operator_log_append"` (not renamed); chat handlers calling it by name continue to work.

## Out of scope

- Queue-routing `ap2 backfill-proposals` (TB-195 surface) and `ap2 cron edit` (TB-146 surface). Sibling TB covers those with a simpler fix (refuse-if-active).
- A repo-wide lint or test asserting "no CLI verb writes a fenced path synchronously." Useful invariant but separate concern; this task fixes the load-bearing case (ack).
- Migrating existing operator_log.md content. Forward-looking only.
- Renaming the `operator_ack` event type. Existing event consumers (ideation Step 0 reads it via operator_log.md tail; tests grep by name) keep working.
- Adding a `--queue-now` flag to `ap2 ack` to opt OUT of the queue. The whole point is consistent queue-routing; no escape hatch.
- Touching `do_operator_queue_append` or `drain_operator_queue`'s general behavior. The ack op uses the existing machinery unchanged.
