# TB-152 — `ap2 reject TB-N` (CLI + chat) with explicit operator_log.md entry

## Why
The ideation prompt treats `.cc-autopilot/operator_log.md` as
authoritative on operator decisions: "Each line is authoritative:
do NOT re-propose actions or decisions logged here, even if your
prior assessment surfaced them as 'Open questions for operator'."

Today the only way to dispose of an ideation proposal the operator
disagrees with is `ap2 delete TB-N` (or chat-routed delete via the
operator queue). That removes the briefing + the TASKS.md row but
writes nothing to operator_log.md beyond a generic
`applied operator-queued delete → TB-N` audit line — there's no
"this proposal was rejected on its merits" signal. Result:
ideation can re-propose the same idea next cycle if the project
state still motivates it.

A dedicated `reject` verb closes the feedback loop: the line
"`<ts>` — rejected ideation proposal → TB-N (<title>): <reason>"
is grep-able by the next ideation cycle and serves as a permanent
"don't re-propose" record alongside the existing
`applied operator-queued ...` lines.

## Scope
1. New op `reject` in `ap2/tools.py` `OPERATOR_QUEUE_OPS`:
   - Operator-side: `ap2 reject TB-N [--reason "..."]` appends a
     queued op `{op: "reject", task_id: TB-N, reason: "..."}` to
     `.cc-autopilot/operator_queue.jsonl`.
   - Drain-side: `drain_operator_queue` handles `reject` by (a)
     calling the same removal codepath as `delete` (move to a
     deleted state + remove the briefing file, exactly mirroring
     today's delete behavior — no scope creep) and (b) appending
     to `operator_log.md` a one-line entry of the shape
     `<ts> — rejected ideation proposal → TB-N (<title>): <reason>`.
     Reason defaults to `(no reason given)` when --reason is
     omitted.
   - The standard `applied operator-queued delete → TB-N` audit
     line is replaced by `applied operator-queued reject → TB-N`
     for reject ops, so the audit trail distinguishes the two
     verbs.
2. New CLI subcommand `cmd_reject` in `ap2/cli.py`:
   - Args: `task_id` (positional, required), `--reason` (optional
     freeform string; rejected if it contains `\n` or `\r` per the
     TB-134 single-line rule).
   - Pre-validates the task exists and is currently in Backlog
     with `@blocked:review`. If not, exits non-zero with a clear
     message ("TB-N is not a pending-review proposal — use
     `ap2 delete TB-N` to remove tasks not awaiting review").
3. New chat verb in the MM-handler prompt (`ap2/prompts.py`):
   add `reject TB-N [reason: ...]` to the supported verb list with
   the same routing as `approve` — the handler calls
   `mcp__autopilot__operator_queue_append({"op": "reject", ...})`
   so the existing queue path applies.
4. Update `skills/ap2/SKILL.md` and `ap2/README.md` with the new
   verb, including the "for ideation proposals only; use delete
   for everything else" guidance.
5. Tests:
   - `ap2/tests/test_cli.py`: `test_reject_appends_operator_log`
     (synthetic Backlog with one review-gated task → run
     `cmd_reject` → drain queue → assert TASKS.md no longer
     contains the row, briefing file is gone, operator_log.md
     contains the rejected-proposal line with the supplied
     reason).
   - Same file: `test_reject_rejects_non_review_task` (Active task
     → cmd_reject exits non-zero, no queue append).
   - `ap2/tests/test_operator_queue.py`: pin that the drain
     handler distinguishes the audit line shape (delete vs reject).
   - `ap2/tests/test_prompts.py`: pin the MM-handler prompt
     contains the `reject TB-N` verb description.

## Verification
- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "\"reject\"" ap2/tools.py` — `reject` is registered in
  `OPERATOR_QUEUE_OPS`.
- `grep -nE "def cmd_reject" ap2/cli.py` — CLI command is wired.
- `grep -q "rejected ideation proposal" ap2/tools.py` — the
  operator_log.md line shape is emitted from the drain path.
- `grep -q "reject TB-N" ap2/prompts.py` — the MM-handler prompt
  documents the new verb.
- prose: `cmd_reject` in `ap2/cli.py` validates the task is in
  Backlog with `@blocked:review`, exits non-zero with a clear
  message otherwise, and on the success path appends a queued
  `reject` op to `operator_queue.jsonl` rather than mutating
  TASKS.md directly.
- prose: the drain handler in `ap2/tools.py` (or wherever
  `drain_operator_queue` lives) writes a
  `<ts> — rejected ideation proposal → TB-N (<title>): <reason>`
  line to `operator_log.md` when applying a `reject` op, so the
  next ideation cycle sees the decision in its authoritative
  source.

## Out of scope
- Auto-rejecting tasks based on heuristics — `reject` is operator-
  driven only.
- Renaming or removing `ap2 delete` — both verbs coexist; reject
  is the explicit "decided against an ideation proposal" path,
  delete remains the generic remove.
- Bulk `reject TB-X TB-Y TB-Z` — single-task only this round;
  defer until friction observed.
- Web UI button — chat + CLI is enough surface for v1.
