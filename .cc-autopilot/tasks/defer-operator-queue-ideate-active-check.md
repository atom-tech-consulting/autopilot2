# Defer operator-queue ideate Active-check from append time to drain time

Tags: #operator-queue #ideation #ux

## Goal

`do_operator_queue_append` rejects `op="ideate"` at append time when `Active` is non-empty (`ap2/tools.py:1494-1508`), forcing the operator to either pass `--force` (escape hatch with a stern warning) or wait for the in-flight task to land before re-typing `ap2 ideate`. This contradicts the operator-queue contract that every other op honors — the queue exists *specifically* so operator commands dont have to fight in-flight task runs (TB-131 docstring: "Ideation reads a stable board snapshot for an entire SDK turn — a queued `ap2 add` arriving mid-thought lands BEFORE ideations next read, not during it"). `add_*` / `update` / `approve` / `reject` / `unfreeze` / `delete` all queue regardless of board state and apply when safe; `ideate` is the only outlier.

The TB-159 comment block (lines 1485-1493) reasons about "concurrent task-agent + control-agent SDK runs share the same in-process slot", but the actual interleaving is benign: the drain runs as `_tick`s first stage, BEFORE task dispatch, AFTER the previous ticks `run_task` already returned (Active was cleared back to Complete/Backlog/Frozen). When the drain processes the `ideate` op, Active is empty by construction. The post-drain `force_ideate` SDK call also runs within the same `_tick`, sequentially before task dispatch — theres no path for it to overlap a task-agent SDK run on the same loop.

The append-time check was guarding a race that the loop topology (TB-122s `_main_tick_loop` ↔ `_mm_loop` split) already prevents. Drain-time is the correct place for the assertion if we want one at all (sanity check, not a hard reject).

Why now: live UX friction. Operator types `ap2 ideate` while a task happens to be running, gets the hard reject, has to either `--force` (which the comment block warns against) or babysit the daemon until the task lands. Same operator who just hit pause-required friction with `goal.md` (TB-193). The whole point of routing ops through the queue (TB-131, TB-141, TB-142, TB-152, TB-153, TB-159, and now TB-193) is to remove "is the daemon busy right now?" from the operators mental model. Goal anchor: the walk-away promise ("an operator can point ap2 at a fresh project, paste a `goal.md` ... and walk away for a week without intervention") is what every operator-queue routing decision serves — a `--force` reject for typing `ap2 ideate` at the wrong second is exactly the per-touchpoint friction that erodes walk-away.

## Scope

- `ap2/tools.py` — `do_operator_queue_append` `ideate` branch (lines 1484-1509): drop the append-time Active check. The `force` arg becomes a no-op for the queue-routing decision (kept on the queue payload as audit metadata for now; see Out of scope on whether to deprecate it).
- `ap2/tools.py` — optionally add a drain-time sanity assertion in `_apply_operator_op` `ideate` branch: if Active is somehow non-empty at drain time (loop-topology invariant violated), emit `operator_queue_error` and skip the `force_ideate` signal. This is paranoia, not the primary fix — by current loop semantics this branch is unreachable.
- `ap2/cli.py` — `cmd_ideate` no longer needs to plumb the at-append-time error path; the `--force` flag remains as a no-op (deprecation messaging optional).
- `ap2/tests/` — extend the operator-queue tests to cover: (a) `ideate` queues successfully when Active is non-empty (no append-time reject); (b) drain still emits `ideation_forced` and signals `force_ideate=True`; (c) `--force=False` no longer fails the append. Remove or update any existing test that asserted the rejection behavior.

## Design

**The minimal fix is removing lines 1494-1508 of `ap2/tools.py`.** The remainder of the `ideate` branch (`rec_args = {"force": force}`, queue write, event emit, return) stays as-is. The drain-side handler (`_apply_operator_op` `ideate` branch in `tools.py:1992-2007`) is already correct: it emits the `ideation_forced` audit event and surfaces `force_ideate=True` on the drain return dict for `_tick` to consume.

**Why the loop topology guarantees Active=∅ at drain time.** `run_task` is called synchronously inside `_tick` step 4. `_tick` itself is awaited from `_main_tick_loop`. A long-running task BLOCKS `_tick` from re-entering until `run_task` returns — at which point Active has been moved to Complete/Backlog/Frozen by the run-completion handler. The NEXT `_tick` starts from step 0 (drain) with Active already empty. Pipeline Pending tasks (which have their own subprocess in flight) live in a DIFFERENT board section — Active is reserved for the synchronous task-agent slot. `_recover_orphans` at startup (`daemon.py:656`) confirms this invariant: any task left in Active at boot is a crash artifact, not a steady-state.

**Why `_mm_loop` is irrelevant to this check.** MM handlers run on `_mm_loop`, a separate asyncio coroutine. They share the SDK slot with task agents (TB-122 explicitly accepted this), but they DONT touch the operator queue or board sections. An MM-handler agent running concurrently with `_main_tick_loop`s drain is fine — the drain holds `board_file_lock`, the MM handler doesnt need that lock, and the `ideate` ops post-drain SDK call (force_ideate) is sequenced inside `_main_tick_loop` between drain-end and task-dispatch-start, with no MM-handler interference at that decision boundary.

**Optional drain-time sanity assertion.** If we want belt-and-braces, the drain-side `_apply_operator_op` `ideate` branch could check `next(board.iter_tasks(section="Active"), None) is None` and refuse to set `force_ideate_pending=True` if violated, emitting `operator_queue_error op="ideate" reason="active_present_at_drain"`. This is unreachable under current loop topology — included as a guardrail in case future refactors introduce concurrent task agents (which would require a much bigger redesign than this TB anyway).

**Migration of `force` arg.** The CLI `--force` flag becomes a no-op for the queue-routing decision. Keep accepting it for one release (no deprecation warning yet — the flag was rarely used and silently ignoring it is friendlier than emitting noise). The audit-trail value of "operator passed --force" is preserved on the queue record for grep-ability if anyone cares post-hoc.

**No interaction with TB-193 (`update_goal`).** Both ops share the queue-append → drain pipeline; their handlers are independent. `update_goal` queued before `ideate` would land in goal-then-ideate order (drain processes records in append order), which means a forced ideation pass would read the freshly-updated goal.md — which is the obvious right semantics for an operator doing a goal-rotation followed by an explicit "now ideate against the new goal" trigger.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_operator_queue.py` (or whichever file the new tests land in) — new tests cover (a) `ideate` queues successfully with Active non-empty, (b) drain emits `ideation_forced` and surfaces `force_ideate=True`, (c) `--force=False` is no longer required at append time.
- prose: `do_operator_queue_append` in `ap2/tools.py` has NO board-state read in the `ideate` branch — the at-append-time `Active` check (current lines 1494-1508) is gone. The branch reduces to: capture `force` from args (optional, audit-only), build `rec_args`, append-and-emit-event like the other ops.
- prose: the TB-159 comment block above the `ideate` branch is updated (or replaced) to reflect the new semantics — no append-time check, drain-time invariant, and the rationale ("by drain time Active is empty by loop-topology invariant; force_ideate runs within the same `_tick` before task dispatch").
- prose: the drain-side `_apply_operator_op` `ideate` branch (`tools.py:1992-2007`) is unchanged in behavior, OR (if the optional sanity assertion is added) its guard is documented in the inline comment as "should be unreachable; included as a guardrail against future loop-topology changes".
- prose: any pre-existing test asserting the at-append-time rejection (search for `"a task is currently Active"` or similar) is updated or removed; no test is left asserting the OLD reject behavior.

## Out of scope

- Deprecating or removing the `--force` CLI flag entirely. The flag becomes a no-op for routing but remains accepted as an audit-trail signal; deprecation can come in a follow-up if it accumulates noise.
- Reworking how `force_ideate` signal flows from drain return dict to `_tick`. Unchanged.
- Adding a Mattermost / chat surface for `ideate`. The handlers restricted toolset (TB-145) doesnt expose it; this TB doesnt change that.
- Allowing concurrent task-agent + control-agent SDK runs. The loop topology (TB-122) is what makes the drain-time invariant hold; relaxing that is a much bigger redesign and is not implied by this TB.
- Validating that `ideate`-queued ops dont pile up. If an operator queues 5 `ideate` ops in a row, they all drain in order, each fires a `force_ideate` signal, and the daemon runs ideation 5 times back-to-back. Thats acceptable — forced ideation is operator-explicit; if they queued 5, they presumably wanted 5.
