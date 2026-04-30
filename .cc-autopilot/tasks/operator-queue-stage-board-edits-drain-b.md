# TB-131 — Operator queue: stage board edits, drain between daemon runs

## Goal

Today operator board ops (ap2 add, ap2 backlog, ap2 unfreeze, ap2 delete) write straight to TASKS.md under board_file_lock. The lock guarantees atomicity but not serializability vs concurrent ideation/MM/task runs that read the board, think for many SDK turns, then write. Concrete failure modes observed: (a) state-violation rollback (git reset --hard pre_run_head) wipes operator adds made during a task run. (b) ideation reads TASKS.md, thinks for 8+ turns, operator ap2 add lands during turn 4, ideation commits a near-duplicate based on its now-stale view. (c) once TB-122's concurrent MM loop ships, MM-handler board edits face the same rollback exposure as operator adds. Proposed fix: route all operator board mutations through a queue.\n\nScope:\n\n(1) New file .cc-autopilot/operator_queue.jsonl — append-only, gitignored, one JSON record per pending op: {uuid, op, args, ts, optional preallocated_task_id}. Fenced from task agents (add to TASK_AGENT_FENCED_PATHS).\n\n(2) New handler do_operator_queue_append(cfg, args) in tools.py shared by two write paths, mirroring how do_operator_log_append shares CLI + MCP today: (a) operator-side: ap2 add and friends append to the queue instead of directly mutating TASKS.md. (b) MM-handler side: a new operator_queue_append MCP tool the handler can call when @claude-bot is asked to add a task during an in-flight run.\n\n(3) ID pre-allocation: ap2 add still grabs the board lock briefly to (a) bump CLAUDE.md next_task_id, (b) write the briefing file, (c) append the queued op carrying the pre-allocated TB-N. Preserves today's UX where ap2 add prints 'TB-124 (queued; will land at next tick)'. The TASKS.md insertion is what's deferred, not the ID allocation.\n\n(4) New first stage in daemon._tick: drain_operator_queue(cfg) — under board_file_lock, replay each queued op through do_board_edit, then commit state files. Runs BEFORE MM, cron, ideation, task — so all daemon-side reads happen against an up-to-date board. Idempotent via uuid: a state file (.cc-autopilot/operator_queue_state.json) records applied uuids so a crash mid-drain resumes without double-applying.\n\n(5) Audit trail: each successfully drained op appends a one-line summary to .cc-autopilot/operator_log.md ('applied operator-queued add_backlog → TB-124 at <ts>'). Keeps history visible in the existing operator-decisions surface without bloating the queue file with completed ops.\n\n(6) ap2 status surfaces queue depth: 'pending: 2 operator ops' line when non-empty.\n\nWhy: this is structurally cleaner than fixing rollback (which only addresses the wipe symptom, not the semantic race with ideation). Operator can set-and-forget; ideation/task runs always see a stable board snapshot; rollback never needs to distinguish operator vs agent commits because operator ops aren't in HEAD until between-runs anyway.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)
