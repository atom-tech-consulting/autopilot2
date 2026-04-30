# Concurrent Mattermost handler with restricted toolset during in-flight tasks

## Goal

Let the operator interact with the daemon via Mattermost while a task
agent is running, with a restricted handler toolset that is
read-mostly + board-manipulation-only — no cron/ideation/state-machine
mutations. Today's sequential `_tick` blocks MM polling for up to
`AP2_TASK_TIMEOUT_S` (1200s default; 3600s on stoch); long tasks
mean operator messages sit in the queue for 20+ minutes.

## Why

The MM handler is the operator's primary out-of-band channel. The
single-tick loop conflates "scheduled work" (cron, ideation,
auto-promotion, dispatch) with "operator interaction" (MM mentions).
Operator intent is interactive — it shouldn't have to wait on a slow
task agent. Concrete cases where the latency hurts:

- "@claude-bot pause" while a task is grinding on the wrong thing —
  operator wants the daemon to stop dispatching after this task,
  shouldn't have to wait 19 minutes.
- "@claude-bot status TB-N" — the operator can read the board
  themselves, but the daemon's status digest has additional context
  (recent events, retry count, etc.) and feels broken if it takes 20
  minutes.
- "@claude-bot delete TB-X" — the operator wants a queued task gone
  before it dispatches; sequential MM means it can dispatch first.
- "@claude-bot add a task to do X" — operator wants to enqueue while
  the current task is still running.

## Design — two concurrent loops, restricted toolset when a task is in flight

### Loop split

Refactor `daemon.main_loop` into two `asyncio` coroutines started
alongside each other:

- `_main_tick_loop` — existing `_tick` minus the Mattermost step.
  Cron, pipeline-pending sweep, task dispatch, ideation, watchdog. Tick
  interval `AP2_TICK_S` (30s default).
- `_mm_loop` — Mattermost polling on its own interval
  `AP2_MM_TICK_S` (default 10s — faster than main tick because it's
  cheap and operator-facing). For each new mention, spawn
  `asyncio.create_task(handle_message(...))` so back-to-back mentions
  don't serialize.

Both loops share `Config` and the MCP server. Both go through
`locked_board()` for any board mutation; `fcntl.flock` already
serializes the contention. Append-only `events.jsonl` is already
concurrency-safe (line-atomic write).

### Toolset modes

Two allowlists in `prompts.py`. Reference: `CONTROL_AGENT_TOOLS` in
`tools.py` is `[Read, Glob, Grep, board_edit, cron_edit,
mattermost_reply, log_event, daemon_control, ideation_state_write,
git_log_grep, operator_log_append]`.

- `MM_HANDLER_TOOLS_FULL` — same as today's `CONTROL_AGENT_TOOLS` (no
  change).
- `MM_HANDLER_TOOLS_RESTRICTED` — **drops** `cron_edit` and
  `ideation_state_write`. **Keeps** `Read`, `Glob`, `Grep`,
  `git_log_grep`, `board_edit`, `mattermost_reply`, `log_event`,
  `daemon_control`, `operator_log_append`.

`build_mattermost_prompt` (or its caller in `daemon.handle_message`)
checks `Board.iter_tasks("Active")` at handler-spawn time and picks
the toolset:

- `len(active) == 0` → `MM_HANDLER_TOOLS_FULL`. Same liberty as today.
- `len(active) > 0` → `MM_HANDLER_TOOLS_RESTRICTED`. Tells the agent
  in the prompt that a task is running and cron/ideation mutations are
  off-limits for this turn.

### Why restrict cron + ideation while a task is in flight

- **`cron_edit`** mutating the cron schedule mid-task changes when the
  next status-report / ideation tick fires; could land on the running
  task's working tree mid-edit. Defer until idle.
- **`ideation_state_write`** rewrites the ideation cycle assessment;
  conflicts with whatever ideation thought a moment ago when the
  current task was queued. Defer until idle.

### Why keep operator_log_append, daemon_control, board_edit

- **`operator_log_append`** (TB-106) is the operator's primary write
  surface during a running task — "@claude-bot ack: we decided
  against TB-X" or "ack: don't re-propose Y, it's a non-goal." Ideation
  reads `operator_log.md` and won't re-propose actions logged there.
  Dropping this would silence the operator's veto channel exactly
  when they most want to use it (mid-task course-correct). Distinct
  from `ideation_state_write` — `operator_log_append` is the human's
  voice, `ideation_state_write` is ideation's own per-cycle assessment.
- **`daemon_control`** — pause/resume mid-task is exactly the use
  case the operator needs.
- **`board_edit`** — manipulating the queue (add, delete, backlog,
  freeze, **approve**) is the operator's interactive surface — they
  need this *especially* while a task is running.

### Cross-reference: TB-121 task approval via MM

TB-121 adds an `approve` action to `board_edit` (strips `(blocked
on: review)` from an ideation-proposed task). Because
`MM_HANDLER_TOOLS_RESTRICTED` keeps `board_edit`, the operator can
say "@claude-bot approve TB-N" mid-flight and the handler routes
through the same path the `ap2 approve` CLI uses. No additional
MCP tool, no separate code path. The handler prompt should
explicitly mention this so the operator's "approve" intent is
recognized — pin the prompt instruction in
`tests/test_prompts.py`. Order of landing: TB-121 first (or land
together); TB-122 inherits the action.

### Alternative considered: always-restricted toolset

Drop the two-mode complexity; use restricted unconditionally. The few
cases where the operator wants `cron_edit` or `ideation_state_write` 
mid-conversation can wait until the daemon is idle. **Implementer's
choice** — the always-restricted variant is simpler and the dropped
liberty is rarely needed. Default to restricted unless we hit a
concrete need for the full set.

## Concurrency hazards

- **Board lock contention.** `locked_board()` is the serialization
  point. A slow handler holding the lock could delay the daemon's own
  `move_to_complete` after a task returns. Mitigate with short
  per-mutation lock holds (already the pattern — read, mutate,
  release).
- **MM handler deletes the running task.** `do_board_edit` action
  `delete` already refuses Active/Ready without `--force`. Document
  that `--force delete` of an in-flight task is a "let it finish but
  drop the result" semantic, not "kill the task." Killing in-flight
  tasks is a separate task (out of scope).
- **MM handler pauses the daemon mid-task.** Pause flag (presence-only
  file) takes effect on the *next* tick — running task continues to
  completion, then no further dispatch. This is the existing semantic;
  document it explicitly in the handler prompt.
- **Two MM mentions arrive in quick succession.** Each gets its own
  `asyncio.create_task`. They contend on the board lock if both hit
  `do_board_edit` — fcntl serializes; outcome is deterministic but
  order depends on scheduling. Acceptable.
- **Handler spawns during the brief window between `move_to_active`
  and the SDK query starting.** Active section already shows the
  task; restricted toolset is selected; correct.

## Scope

- `ap2/daemon.py` — split `main_loop` into `_main_tick_loop` +
  `_mm_loop`; remove the MM step from `_tick`. Use
  `asyncio.gather(_main_tick_loop(), _mm_loop())`.
- `ap2/config.py` — add `AP2_MM_TICK_S` (default 10).
- `ap2/prompts.py` — add `MM_HANDLER_TOOLS_RESTRICTED`. Branch in
  `build_mattermost_prompt` (or the caller) on Active-task presence;
  prompt header includes a one-liner ("a task is currently running;
  cron/ideation mutations off-limits this turn") so the agent knows
  why the toolset is narrower.
- `ap2/tools.py` — no changes needed; the existing MCP tools enforce
  their own contracts and the `allowed_tools` list at SDK options
  is what restricts.
- Tests — new `test_concurrent_mm.py` covering: (a) handler fires
  while task in flight, gets restricted toolset; (b) handler fires
  while idle, gets full toolset; (c) handler + daemon contend on
  board lock without deadlock; (d) `cron_edit` from restricted
  handler returns an error visible to the agent. Add to e2e.
- Docs — `ap2/README.md` mentions the two tick intervals and the
  toolset split. `ap2/architecture.md` updates the daemon-loop
  pseudocode and the agent-kinds table.

## Out of scope

- **Killing in-flight tasks from MM.** Separate task. Requires
  cancelling the SDK subprocess + wedging the result into
  `task_error` cleanly. Cooperative pause is the V1 surface.
- **Cron / ideation concurrency with task agents.** Same logic could
  apply (cron is mostly read-only-ish, ideation is whole-board
  mutation). Out of scope; user only asked for MM.
- **Per-message rate limiting.** If 50 mentions arrive at once we
  spawn 50 handlers. Not a current problem; revisit if it bites.
- **Operator-explicit toolset override.** No "use the full toolset
  even though a task is running" knob. If you need cron edited mid-
  task, it can wait or you can `ap2 cron edit` directly from the
  CLI.

## Verification

- [shell] `uv run pytest -q ap2/tests/` (regression gate)
- New unit test: `build_mattermost_prompt` returns
  `MM_HANDLER_TOOLS_RESTRICTED` when board has Active tasks and
  `MM_HANDLER_TOOLS_FULL` when it doesn't.
- New e2e test (`tests/e2e/test_concurrent_mm.py`): seed Active task
  + queued MM mention; run `_main_tick_loop` + `_mm_loop` for 3
  ticks. **Gating**: MM handler completes during the task agent's
  run (timestamps prove concurrency); MM handler's allowed_tools is
  the restricted set.
- New e2e test: idle board + MM mention. **Gating**: handler gets
  the full set.
- New e2e test: handler attempts `mcp__autopilot__cron_edit` while a
  task is in flight → SDK returns "tool not in allowed_tools"; the
  handler's reply explains the restriction. (If the SDK silently
  drops disallowed tool calls, this test asserts the absence of
  cron mutation in `events.jsonl`.)
- Manual: kick a long-running task on stoch, mention `@claude-bot
  status` → handler replies in <30s.

## Decision log

- 2026-04-29 (this briefing): Filed in Backlog. Reuses the existing
  toolset/allowlist primitive — no new MCP tools, no protocol
  changes. The async-task-per-mention pattern matches the existing
  pipeline-launch pattern. Restricted-by-default-when-task-active
  picked as the safer mode; full-set-when-idle preserves today's
  behavior. Implementer may choose always-restricted if that turns
  out simpler.
## Attempts

### 2026-04-30 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T073933Z-TB-122.prompt.md`, `stream: .cc-autopilot/debug/20260430T073933Z-TB-122.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T073933Z-TB-122.messages.jsonl`
### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] [shell] `uv run pytest -q ap2/tests/` (regression gate); [fail] New unit test: `build_mattermost_prompt` returns`MM_HANDLER_TOOLS_RESTRICTED` when board has Active tasks and`MM_HANDLER; [fail] New e2e test (`tests/e2e/test_concurrent_mm.py`): seed Active task; [fail] New e2e test: idle board + MM mention. : handler getsthe full set.; [fail] New e2e test: handler attempts `mcp__autopilot__cr
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T084719Z-TB-122.prompt.md`, `stream: .cc-autopilot/debug/20260430T084719Z-TB-122.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T084719Z-TB-122.messages.jsonl`
### 2026-04-30 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] [shell] `uv run pytest -q ap2/tests/` (regression gate); [fail] New unit test: `build_mattermost_prompt` returns`MM_HANDLER_TOOLS_RESTRICTED` when board has Active tasks and`MM_HANDLER; [fail] New e2e test (`tests/e2e/test_concurrent_mm.py`): seed Active task; [fail] New e2e test: idle board + MM mention. : handler getsthe full set.; [fail] New e2e test: handler attempts `mcp__autopilot__cr
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T085542Z-TB-122.prompt.md`, `stream: .cc-autopilot/debug/20260430T085542Z-TB-122.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T085542Z-TB-122.messages.jsonl`
### 2026-04-30 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260430T232427Z-TB-122.prompt.md`, `stream: .cc-autopilot/debug/20260430T232427Z-TB-122.stream.jsonl`, `messages: .cc-autopilot/debug/20260430T232427Z-TB-122.messages.jsonl`
