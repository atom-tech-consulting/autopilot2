# MM handler RESTRICTED: drop board_edit; add `approve` to queue

## Goal

Close the second instance of the false-positive `task_state_violation` class — the one TB-141 closes for operator-side `ap2 add` but leaves open via the Mattermost-handler chat-command path. When the MM handler runs with `MM_HANDLER_TOOLS_RESTRICTED` (a task agent is in flight, TB-122) it currently keeps `mcp__autopilot__board_edit` in its toolset. Any operator chat command that triggers `board_edit` (e.g. `@claude-bot freeze TB-X`, `@claude-bot approve TB-Y`) directly mutates TASKS.md during the running task agent's window, trips TB-110's snapshot check, and rolls back the running task — same blast radius as the operator-side `ap2 add` case TB-141 fixes.

## Why

TB-141 narrows the fenced-list and defers CLAUDE.md bumps so operator-side board edits via the queue stop tripping TB-110. The MM handler is the second mutation surface and needs the same treatment: when a task is in flight, route every board mutation through the operator queue (`operator_queue_append`) instead of `board_edit`. The queue's drain happens between ticks, so its effects never overlap with the running task agent's snapshot window.

There's also a concrete missing piece: TB-121's `approve` action (strip `(blocked on: review)` from an ideation-proposed task) is invoked via `board_edit({"action":"approve",...})` today. If we drop `board_edit` from RESTRICTED, the chat-command path for `@claude-bot approve TB-N` breaks unless we extend the queue to cover `approve` too.

## Scope

(1) Remove `mcp__autopilot__board_edit` from `MM_HANDLER_TOOLS_RESTRICTED` (`tools.py:1454`). Idle handler runs (board has no Active tasks) keep the FULL set, so direct `board_edit` is still available when there's nothing to violate against. The handler prompt that explains the toolset (`prompts.py`) needs the parallel update.

(2) Add `approve` to `OPERATOR_QUEUE_OPS` (`tools.py:549`):
   - `do_operator_queue_append` (`tools.py:567`) accepts `op="approve"` with `task_id` arg, validates the task exists in Backlog (or whatever section ideation lands review-gated tasks in) under the snapshot lock, queues a record.
   - `drain_operator_queue` (`tools.py:840+`) handles `approve` records by calling the same `do_board_edit({"action":"approve","task_id":...})` it uses for the other queueable ops, so the codepath that strips `(blocked on: review)` is shared.
   - The MM handler prompt is updated so `@claude-bot approve TB-N` uses `operator_queue_append({"op":"approve",...})` when a task is in flight; it can still use `board_edit` directly when idle (or always go through the queue for symmetry — implementer's call).

(3) MM handler prompt updates in `prompts.py`:
   - When the toolset is RESTRICTED, instruct the agent to ALWAYS use `operator_queue_append` for board mutations and explain why (running task's state-violation snapshot).
   - Mention `approve` is queueable post-this-change.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `! grep -qE "mcp__autopilot__board_edit" <(python3 -c "import ap2.tools as t; print('\n'.join(t.MM_HANDLER_TOOLS_RESTRICTED))")` — board_edit is absent from the restricted toolset.
- New unit test in `test_prompts.py` (or wherever the toolsets are pinned): `MM_HANDLER_TOOLS_RESTRICTED` does NOT contain `mcp__autopilot__board_edit`. `MM_HANDLER_TOOLS_FULL` still does.
- New unit test in `test_tools.py`: `OPERATOR_QUEUE_OPS` includes `"approve"`. `do_operator_queue_append({"op":"approve","task_id":"TB-X"})` queues a record with `op="approve"`. `drain_operator_queue` applies a queued `approve` op by invoking the existing `board_edit` approve action, leaving the task with no remaining `(blocked on: review)` clause.
- New e2e test (`tests/e2e/`): seed Active task; queue an MM handler invocation that needs to add a Backlog task and approve a separate review-gated task; assert (a) both ops complete, (b) running task agent's snapshot check does NOT fire `task_state_violation`, (c) drain at next tick applies both, (d) the approved task is dispatchable on the following tick.
- The diff updates the MM handler prompt instruction so `@claude-bot freeze/add/approve/...` chat commands route through the queue when restricted. Pin the prompt instruction with a `test_prompts.py` test.

## Out of scope

- Forcing `operator_queue_append` even on idle (FULL toolset still has direct `board_edit`). Could tighten later, but symmetry is not required for the state-violation fix.
- Adding `move_to_active`, `move_to_complete`, `move_to_pipeline_pending` to the queue — those are daemon- or task-agent-driven, never operator-facing chat commands.
- Re-architecting the MM handler to use a single mutation primitive everywhere; we only need the two changes above to close the false-positive class.
