# Gate ideation-proposed tasks behind human review before dispatch

## Goal

Stop the daemon from autonomously working on tasks the ideation agent
just invented. Today: ideation writes Backlog → auto-promotion → Ready
→ task agent → commit, with no human in the loop. Wanted: ideation
proposes, the task waits in a "needs-review" state until the operator
explicitly approves, then it joins the normal dispatch flow.

## Why

Ideation is the *only* path that creates new work without operator
intent. Everything else (`ap2 add`, mattermost handler, `migrate-to-ap2`)
has a human at the keyboard. An autonomous proposal pipeline plus an
autonomous dispatch loop is one ideation hallucination away from the
daemon spending an hour committing code that the operator never wanted
written. The two-tier verifier doesn't help here — the task agent will
satisfy its own briefing's verification because the briefing is what
ideation invented.

Concrete failure modes this prevents:

- Ideation re-proposes a thing the operator decided against (operator
  forgot to `ap2 ack` it, or the `ap2 ack` lookup missed).
- Ideation invents a refactor that's directionally wrong (e.g.
  reverting an architectural decision the operator made off-board).
- Ideation proposes scope creep that's not aligned with `goal.md`.
- Ideation's verification bullets are wrong / unenforceable, so the
  task agent's "complete" status is meaningless.

## Design — `(blocked on: review)` clause

Reuse the existing blocker-clause mechanism. Ideation's task-emit
instructions get an additional rule: every proposed task line ends with
`(blocked on: review)`. Auto-promotion (`Board.next_dispatchable`)
already skips tasks with unsatisfied blockers, so these sit in Backlog
indefinitely.

New CLI: `ap2 approve TB-N` removes the `(blocked on: review)` clause
atomically (locked board), emits an `ideation_approved` event. After
approval the task auto-promotes on the next tick like any other Backlog
task.

`_is_blocker_satisfied` extends to recognize `review` as a known scheme
(returns False until the clause is removed; explicitly distinguishes
this from the `unsatisfiable_blocks` fail-safe in `diagnose`).

### Why not a new "Proposed" board section

Adds a 7th section to TASKS.md, the Board model, every test fixture,
the web UI, every section enum check. Higher blast radius for the same
semantics. The blocker-clause approach reuses primitives.

### Why not just a `#proposed` tag

Ideation already adds `#proposed` for informational purposes. Loading
it with dispatch-gating semantics conflates two ideas; an operator who
manually adds `#proposed` (e.g. when filing a half-baked thought from
a meeting) would unintentionally trigger the gate. Blocker clauses are
the existing "won't run until X" primitive — use them.

### Why not require approval for all tasks

`ap2 add` tasks come from a human at the keyboard. Mattermost-handler
tasks come from a human in chat. Migration tasks come from a human
running `migrate-to-ap2`. Only ideation creates work without explicit
human intent — so only ideation needs the gate. Other paths bypass.

## Behavioral consequences

When the gate is active, the daemon's empty-board ideation loop changes
shape:

1. Board fully empty → ideation fires → ideation writes N proposals to
   Backlog with `(blocked on: review)`.
2. Auto-promotion finds nothing dispatchable. Daemon goes idle.
3. **Idle watchdog should NOT fire auto-diagnose** for this case —
   add a "pending review" state to the watchdog so it distinguishes
   "operator AFK" from "daemon broken." Probably a Mattermost ping
   ("3 ideation proposals pending review") instead.
4. Operator returns, runs `ap2 approve TB-N` for some, `ap2 backlog
   TB-N` (no-op) for some, `ap2 delete TB-N --force` for the rest.
5. Approved tasks auto-promote on the next tick, dispatch normally.

Deliberate choice: ideation does NOT re-fire while proposals are
pending review (board is no longer empty). This prevents the daemon
from re-proposing the same thing every cycle while the operator is
asleep. Re-firing resumes once the queue drains (approved/deleted).

## Scope

- Modify `ap2/ideation.default.md` — add to the task-emit instructions
  that every proposed task line ends with `(blocked on: review)`.
- Modify `ap2/board.py::_is_blocker_satisfied` — recognize `review`
  scheme; return False (i.e. blocked) until the clause is removed.
- Add an `approve` action to `do_board_edit` —
  `do_board_edit({"action": "approve", "task_id": ...})` strips
  `(blocked on: review)` from the task line under a locked board,
  emits `ideation_approved` event. This is the shared mechanism:
  - **CLI** `ap2 approve TB-N` → `cli.cmd_approve` → `do_board_edit`.
  - **Mattermost handler** "@claude-bot approve TB-N" → handler agent
    calls `mcp__autopilot__board_edit({"action":"approve",...})`
    directly. No new MCP tool. The handler is a first-class consumer
    of the same action; cross-reference TB-122 (the MM handler keeps
    `board_edit` in its restricted toolset, so this works in-flight
    too).
  - The handler prompt should mention "approve TB-N" as a
    recognized operator command — pin in `tests/test_prompts.py`.
- Modify `ap2/diagnose.py` — distinguish `pending_review` blockers
  from `unsatisfiable_blocks` (currently both fall through the
  unknown-scheme fail-safe). Watchdog skips auto-diagnose when board
  is wholly pending-review.
- Modify `ap2/web.py` — task list shows a "Pending review" pill on
  tasks with `(blocked on: review)`; `/tasks?filter=pending-review`
  view.
- Optional: `ap2 status` includes `pending review: N` line when N>0.
- Tests — `test_ideation_defaults.py` pins the new prompt instruction;
  `test_board.py` pins `_is_blocker_satisfied("review")`; new
  `test_approve.py` covers the CLI happy-path + lock contention.

## Out of scope

- Mattermost-driven approval (`@claude-bot approve TB-N`). Nice
  follow-up but the CLI is the V1 surface.
- Bulk approval (`ap2 approve TB-1 TB-2 TB-3`). Easy add later if
  ergonomically needed.
- Approval policies (auto-approve trivial-tag tasks, etc.).
- Changing the existing `#proposed` tag semantics.
- Migrating existing in-flight ideation tasks (this is autopilot2 —
  there are none yet).

## Verification

- [shell] `uv run pytest -q ap2/tests/` (regression gate)
- [shell] `grep -q 'blocked on: review' ap2/ideation.default.md`
  (prompt instruction landed)
- New unit test: `_is_blocker_satisfied("review")` returns `False`.
- New unit test: removing `(blocked on: review)` from a task line via
  `do_board_edit({"action": "approve", ...})` produces a Backlog task
  with no blockers, ready for auto-promotion.
- New e2e test (`tests/e2e/`): seed board with one ideation-style
  proposal task `(blocked on: review)`. Run a tick → auto-promotion
  skips it, board count unchanged. Run `ap2 approve TB-N` → next tick
  auto-promotes to Ready.
- New e2e test: ideation cron fires against an empty board → produces
  N proposed tasks, ALL with `(blocked on: review)` clauses (gating —
  proves the prompt change took effect end-to-end).
- Manual: idle watchdog with N pending-review tasks does NOT post
  auto-diagnose; instead emits a `mattermost` "pending review"
  reminder.

## Decision log

- 2026-04-29 (this briefing): Filed in Backlog. Driver is risk
  reduction, not a recovered incident. Approach: extend the existing
  blocker-clause primitive rather than adding a new board section or
  overloading `#proposed`. Watchdog behavior change is part of the
  scope so the gate doesn't trigger spurious "daemon broken" alerts.
## Attempts

### 2026-05-01 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] New e2e test: ideation cron fires against an empty board → producesN proposed tasks, ALL with `(blocked on: review)` cla
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260501T064456Z-TB-121.prompt.md`, `stream: .cc-autopilot/debug/20260501T064456Z-TB-121.stream.jsonl`, `messages: .cc-autopilot/debug/20260501T064456Z-TB-121.messages.jsonl`
