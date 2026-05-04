# TB-152 — `ap2 reject TB-N` (CLI + chat) — capture rejection reasons in operator_log.md for ideation learning

## Goal

Capture the operator's REASON for rejecting an ideation-proposed task, not just the action. Today the only disposal path is `ap2 delete TB-N`, which writes `applied operator-queued delete → TB-N` to operator_log.md — the action is captured but the *why* isn't, and ideation has no signal to avoid re-proposing the same idea next cycle when project state still motivates it.

This task adds a dedicated `reject` verb (CLI + chat) that mirrors `delete`'s removal semantics but takes an `--reason "..."` argument and writes a richer audit line:

    <ts> — rejected ideation proposal → TB-N (<title>): <reason>

Ideation Step 0 already reads operator_log.md as authoritative ground truth on operator decisions — these new lines surface there alongside the existing `applied operator-queued ...` audit trail, so the next cycle's "what's still uncovered" pass learns from both the approve side (already captured today) and the reject side (the gap this task closes).

This is the load-bearing prompt-iteration enabler called out in goal.md's "Current focus: ideation quality" section — the approve side of operator decisions is already pinned to disk in a way ideation reads back; the reject side isn't, and without REASONS the prompt has no signal to calibrate against.

## Scope

- `ap2/tools.py` — register `reject` in `OPERATOR_QUEUE_OPS`; the drain handler removes the task (same removal codepath as `delete` — no scope creep on removal semantics) AND writes the `<ts> — rejected ideation proposal → TB-N (<title>): <reason>` line to operator_log.md. The standard `applied operator-queued reject → TB-N` audit line replaces the `delete` variant for reject ops so the audit trail distinguishes the verbs.
- `ap2/cli.py` — new `cmd_reject` subcommand; takes `task_id` (positional) and optional `--reason` (single-line per TB-134; defaults to `(no reason given)` when omitted). Pre-validates the task is in Backlog with `@blocked:review`; refuses with a helpful message ("not a pending-review proposal — use `ap2 delete TB-N`") otherwise.
- `ap2/prompts.py` — add `reject TB-N [reason: ...]` to the MM-handler verb list, routed through `mcp__autopilot__operator_queue_append({"op": "reject", ...})`.
- `skills/ap2/SKILL.md`, `ap2/README.md` — document the new verb with the "ideation proposals only; use delete for everything else" guidance.
- Tests in `ap2/tests/test_cli.py`, `ap2/tests/test_operator_queue.py`, `ap2/tests/test_prompts.py`.

## Design

### Why a separate verb (not `--reason` on `delete`)

A `delete --reason` flag would muddy the verb's semantic — `delete` covers "remove a task that was a typo / no-longer-relevant / superseded," not specifically "I considered this ideation proposal and decided against it on its merits." The audit-line distinction (`reject` vs `delete`) is what the next ideation cycle keys off; collapsing both into one verb forces the prompt to disambiguate intent from prose.

Reasons stay optional (operator may want to reject quickly) but the placeholder `(no reason given)` is itself signal — ideation can spot the difference between "rejected, no reason" and "rejected because X" and decide whether to re-propose.

### Drain-path implementation

The reject op shares ~all of `delete`'s removal logic: locate the task, drop the row from TASKS.md, remove the briefing file, emit the `task_deleted` event. The added work is:

- Build the title-aware operator_log.md line BEFORE removal (need the title before TASKS.md mutation).
- Use the `reject`-flavored `applied operator-queued reject → TB-N` audit line in place of the `delete` one.

### Pre-validation in `cmd_reject`

`cmd_reject` only fires the queue-append if the task is currently Backlog + `@blocked:review`. If not, exit non-zero with the message above. This keeps the verb specific to ideation proposals; the chat path mirrors via the prompt's verb description ("for ideation proposals only").

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `python3 -c "from ap2.tools import OPERATOR_QUEUE_OPS; assert 'reject' in OPERATOR_QUEUE_OPS"` — op registered in the queue.
- `grep -nE "def cmd_reject" ap2/cli.py` — CLI command wired.
- `grep -q "rejected ideation proposal" ap2/tools.py` — drain path emits the operator_log.md line shape.
- `grep -q "reject TB-N" ap2/prompts.py` — MM-handler prompt documents the verb.
- prose: `cmd_reject` validates the task is Backlog + `@blocked:review` before queueing; for non-review tasks (Active, no review token, etc.) it exits non-zero with a message pointing the operator to `ap2 delete`.
- prose: the drain handler in `ap2/tools.py` writes a `<ts> — rejected ideation proposal → TB-N (<title>): <reason>` line to `operator_log.md`. The line uses the operator's `--reason` value, OR `(no reason given)` when omitted — both flavours pinned in tests.
- prose: a test in `test_cli.py` exercises the end-to-end flow — synthesize a Backlog with a `@blocked:review` task, run `cmd_reject` with a reason, drain the queue, assert TASKS.md no longer contains the row, the briefing file is gone, AND operator_log.md contains the rejected-proposal line with the supplied reason text (not just the action verb).
- prose: a test pins the audit-line distinction — applying a `reject` op writes `applied operator-queued reject → TB-N`, applying a `delete` op writes `applied operator-queued delete → TB-N`; the two are not collapsed.

## Out of scope

- Auto-rejecting tasks based on heuristics. `reject` is operator-driven only.
- Renaming or removing `ap2 delete`. Both verbs coexist; `reject` is the explicit "decided against an ideation proposal" path, `delete` remains the generic remove.
- Bulk `reject TB-X TB-Y TB-Z`. Single-task only this round; defer until friction observed.
- Web UI button. Chat + CLI is enough surface for v1.
- Updating ideation's prompt to *use* the new lines beyond what's already there. The existing prompt already reads operator_log.md as ground truth (Step 0); the new lines surface there automatically. Any further prompt tuning to weight rejection reasons is a separate task.
