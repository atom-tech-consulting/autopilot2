# MM handler: always use RESTRICTED toolset; drop the in-flight check

## Goal

Replace the current TB-122 toolset-selection logic (`MM_HANDLER_TOOLS_FULL` when board is idle, `MM_HANDLER_TOOLS_RESTRICTED` when a task is active) with an unconditional `MM_HANDLER_TOOLS_RESTRICTED` for every MM handler invocation. Drop the `is_task_active` snapshot check and `MM_HANDLER_TOOLS_FULL` entirely.

## Why

The toolset switch was designed to give the handler its full powers (cron_edit, ideation_state_write) when nothing was at risk and narrow them when a task agent's snapshot window was active. In practice the check is a TOCTOU race:

1. **Stale-at-spawn**: `handle_message` reads the board, sees `Active=0`, picks FULL. The MM handler then takes 30s of SDK time. During those 30s, the daemon's `_main_tick_loop` runs `_tick`, auto-promotes a Backlog task, dispatches it. The MM handler is now mid-turn with `cron_edit` available against a snapshot the new task agent's TB-110 check is comparing against. If the handler edits cron, the new task dies on state_violation — same false-positive class TB-141, TB-142, TB-143 all chase.

2. **Stale-at-tool-call**: even if Active=0 holds for the entire handler turn (race-free in this run), the handler may decide to make a tool call mid-turn. The decision was anchored at spawn-time but the check has to be a single point — there's no way to re-evaluate "is a task active" at every tool-call boundary without serializing the whole MM handler with the main tick.

3. **Reliability vs convenience**: the only operator-facing capabilities lost in RESTRICTED are `cron_edit` and `ideation_state_write` — both of which have direct CLI alternatives (`ap2 cron list/edit`, manual `ideation_state.md` edit). The convenience of `@claude-bot edit cron status-report to every 1h` is rare; the safety cost of a race window every MM message is constant.

Post-TB-141/142/143, the queue-based design absorbs most of the "what if MM handler mutates fenced files during a task" concern anyway. RESTRICTED + queue + queue-fence-but-not-violation-check is now the load-bearing layer; toggling to FULL on idle is a leftover from before that design landed.

## Scope

(1) Drop `MM_HANDLER_TOOLS_FULL` from `tools.py:1453` (or rename + alias if other callers reference it). Replace the constant with a single `MM_HANDLER_TOOLS` (no FULL/RESTRICTED variants). The list = today's RESTRICTED.

(2) `daemon.handle_message` (or wherever the MM handler runs): drop the `if any_active_task:` branch — the handler always uses `MM_HANDLER_TOOLS`. Removes the snapshot check on the board.

(3) MM handler prompt in `prompts.py`: drop the "your toolset varies based on board state" language. Handler always knows it has the same fixed set; cron and ideation_state changes always go through the operator/CLI path.

(4) Update `test_prompts.py` and any toolset-pinning tests that asserted on FULL — they should now assert on the single MM_HANDLER_TOOLS list.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `! grep -qrE "MM_HANDLER_TOOLS_FULL" ap2/` — old constant is gone (recursive grep across the package).
- `! grep -qrE "MM_HANDLER_TOOLS_RESTRICTED" ap2/` — old restricted name also gone (renamed to plain MM_HANDLER_TOOLS).
- `grep -qE "MM_HANDLER_TOOLS\b" ap2/tools.py` — single new constant exists.
- New unit test in `test_tools.py`: `MM_HANDLER_TOOLS` does NOT contain `mcp__autopilot__cron_edit` or `mcp__autopilot__ideation_state_write`.
- New unit test in `test_tools.py`: `MM_HANDLER_TOOLS` DOES contain `Read`, `Glob`, `Grep`, `mattermost_reply`, `log_event`, `daemon_control`, `operator_log_append`, `operator_queue_append`, and `git_log_grep`.
- New unit test in `test_prompts.py`: the MM handler prompt does not mention conditional toolset switching (no "when a task is active" language).
- New unit test (`test_mattermost.py` or wherever handler-spawn is pinned): `handle_message` does not consult the board for toolset selection — passes the same fixed toolset regardless of any seeded Active task in the test fixture.
- The diff updates the MM handler prompt and any docs (architecture.md, README, skills/ap2/SKILL.md, skills/ap2-task/SKILL.md) that describe the conditional toolset.

## Out of scope

- Adding `cron_edit` / `ideation_state_write` back via a different mechanism (e.g. queueing them like operator board ops). If the convenience is missed, file separately.
- Removing other unconditional checks elsewhere in the daemon. Only the MM handler's toolset toggle is in scope here.
- Generalizing to other handler kinds (ideation, status-report) — those are cron-driven, run with their own toolsets, and don't have the chat-triggered race surface.
