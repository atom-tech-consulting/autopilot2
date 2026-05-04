# TB-159 — `ap2 ideate` CLI for manual ideation trigger that bypasses the natural gates

## Goal

Today ideation only fires when the daemon tick finds (a) `AP2_IDEATION_DISABLED` unset, (b) no Active/Ready/Backlog tasks, AND (c) `AP2_IDEATION_COOLDOWN_S` (default 7200s = 2h) elapsed since the last fire. The operator has no manual trigger — to test a fresh proposal pass NOW the only path is to clear every Ready/Backlog item AND wait up to 2h for the cooldown.

This is friction against the current focus (ideation prompt iteration). Iterating the ideation prompt requires running ideation, observing the proposals, adjusting the prompt, running again — but the natural cadence is 2h between fires. A manual `ap2 ideate` lets the operator deliberately trigger an ideation run on demand, bypassing cooldown / disabled / non-empty-Backlog gates, so the iteration loop is operator-paced rather than cron-paced.

This task adds `ap2 ideate` (CLI, queue-routed) that signals the daemon to run ideation on the next tick. The natural cron-driven path (`_maybe_ideate`) is unchanged; this is a parallel manual trigger.

## Scope

- `ap2/tools.py` — register `ideate` in `OPERATOR_QUEUE_OPS`. Drain handler invokes the forced ideation path on the next tick boundary (or sets a transient flag the daemon's `_tick` reads before its natural ideation check).
- `ap2/ideation.py` — refactor `_maybe_ideate` so the gating logic and the actual run are in separate helpers. Add `force_ideate(cfg, sdk, mcp_server) -> None` (or equivalent) that runs the ideation control-agent unconditionally — same prompt, same SDK options, same MCP server. Forced runs still call `mark_run(IDEATION_NAME)` so the next natural cooldown clock resets (otherwise repeated `ap2 ideate` invocations would do back-to-back ideation passes with no rate limit).
- `ap2/daemon.py` — `_tick` reads the forced-ideate signal from the operator-queue drain output; when set, runs `force_ideate` instead of (or in addition to) `_maybe_ideate` on that tick. Refuses to fire forced ideation when a task agent is currently in flight in the same tick — concurrent control-agent + task-agent SDK runs in `_main_tick_loop` were the precedent for the TB-122 mattermost-handler split. The CLI returns a clear error in that case (see below).
- `ap2/cli.py` — new `cmd_ideate` subcommand. Args: `--force` (also bypass the Active-task refusal — operator escape hatch). Calls `do_operator_queue_append({"op": "ideate", "force": <bool>})`. CLI returns immediately with `TB-N/A — ideation queued; will run at next tick (≤30s)` (no SDK call from the CLI itself).
- Tests in `ap2/tests/test_ideation.py` (or `test_ideation_defaults.py`), `ap2/tests/test_cli.py`, `ap2/tests/test_operator_queue.py`.

## Design

### Why route through the operator queue

The daemon owns the SDK + MCP server + ideation prompt plumbing for control-agent runs. Routing the manual trigger through `operator_queue.jsonl` (drained at the tick boundary) keeps the single-owner property — the CLI doesn't spin up its own SDK; it asks the daemon "next tick, run ideation forced." This mirrors how `add_backlog`, `approve`, `delete`, `update`, `unfreeze`, `backlog`, `reject` (TB-152) all route the same way.

### What's bypassed by default

- `AP2_IDEATION_DISABLED` — bypassed. Operator typing `ap2 ideate` is an explicit override of the disable knob.
- Cooldown (`AP2_IDEATION_COOLDOWN_S`) — bypassed. The whole point of the verb.
- Empty Ready/Backlog gate — bypassed. Proposing on top of existing Backlog is fine; new items just append. Ideation Step 0 already accounts for current Backlog state when ranking.

### What's NOT bypassed by default

- **Active task running** — refused unless `--force`. Concurrent task-agent + control-agent runs share the same `_main_tick_loop` SDK process; TB-122 split mattermost-handler vs task agent for exactly this reason. A forced ideate while a task is active risks contention or conflicting board edits during the active task's snapshot window. Default refuses with a clear message; `--force` lets the operator override at their own risk.

### Cooldown bookkeeping after a forced run

`force_ideate` still calls `mark_run(cfg.cron_state_file, IDEATION_NAME)` after the run completes (success, timeout, or error — same as the natural path). This resets the natural cooldown clock so an operator running `ap2 ideate` ten times in a row would still hit a real 2h gap before the NEXT natural fire (and ideation Step 0 would observe the back-to-back ideation_state.md rewrites in operator_log.md / events.jsonl as forced-run audit lines).

### Audit trail

The drain handler emits an `ideation_forced` event (alongside the `ideation_empty_board` natural-fire event) so post-hoc inspection distinguishes manual from natural fires. operator_log.md gets an `applied operator-queued ideate → (forced)` line. The standard `_run_control_agent` events (`ideation_timeout`, `ideation_error`, the task agent's `add_backlog` events) are emitted unchanged.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `python3 -c "from ap2.tools import OPERATOR_QUEUE_OPS; assert 'ideate' in OPERATOR_QUEUE_OPS"` — op registered in the queue.
- `grep -nE "def cmd_ideate" ap2/cli.py` — CLI command wired.
- `grep -nE "def force_ideate|def _force_ideate" ap2/ideation.py` — forced-run helper present.
- `grep -q "ideation_forced" ap2/tools.py ap2/daemon.py ap2/ideation.py` — forced-run audit event emitted from at least one of these modules.
- prose: a test in `test_ideation*.py` calls the forced-run helper with `AP2_IDEATION_DISABLED=1` set in the env, cooldown unmet (recent `mark_run`), AND a non-empty Backlog (synthesize one Backlog task) — assert the ideation control-agent is invoked anyway (spy on `_run_control_agent`) and `mark_run` is called after.
- prose: a test pins the Active-task refusal — with one task in the Active section, `cmd_ideate` (without `--force`) exits non-zero with a message that mentions `--force`, and no `ideate` op is appended to `operator_queue.jsonl`. With `--force`, the queue append happens.
- prose: a test pins the queue-append shape — `cmd_ideate` (no `--force`) writes `{"op": "ideate", "force": false, ...}` to `operator_queue.jsonl`; the drain handler in `do_operator_queue_drain` (or wherever) recognizes `op="ideate"` and triggers the forced path on that tick.
- prose: a test pins the audit trail — applying an `ideate` op writes an `ideation_forced` event to events.jsonl AND an `applied operator-queued ideate → (forced)` line to operator_log.md (consistent with the existing `applied operator-queued <op> → ...` pattern for other queue ops).
- prose: `cmd_ideate` is non-blocking — it returns immediately after the queue append; the actual ideation run happens asynchronously on the next tick. Tested by mocking the queue append and asserting the CLI returns within a small wallclock budget without invoking any SDK code.

## Out of scope

- A `--reason` argument capturing why the operator triggered manually. Optional; defer until friction observed (the operator-log audit line is already a unique signal — manual fires distinguished from natural ones).
- Bulk / scheduled forced ideation (e.g. via cron). The verb is operator-on-demand only; if scheduled forced ideation is wanted, the natural cooldown path covers it (set `AP2_IDEATION_COOLDOWN_S` lower).
- Web UI button. CLI is enough surface for v1.
- Bypassing the per-task review gate. Forced ideation still produces `@blocked:review` proposals; `ap2 approve TB-N` is still required to dispatch them.
- Topic-steering arguments (e.g. `ap2 ideate --focus "test coverage"`). The verb fires the standard ideation prompt; topic-steering is a separate task that would touch the prompt itself.
- Removing or replacing the natural cooldown-driven path. Both coexist.
- Adding `--force` semantics to bypass `AP2_IDEATION_DISABLED` separately from the Active-task check. The default already bypasses the disable knob; `--force` is purely for the Active-task escape hatch.
