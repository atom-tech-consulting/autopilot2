# Refuse `ap2 backfill-proposals` and `ap2 cron edit` when a task is Active

## Goal

This task is anchored in goal.md's `## Done when` criterion: "An operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention." Walk-away reliability depends on operator-driven CLI verbs not silently triggering false-positive rollbacks that burn real SDK cost.

Two operator-CLI verbs write fenced files synchronously, bypassing the operator-queue routing pattern:

- **`ap2 backfill-proposals`** (`cmd_backfill_proposals`, `ap2/cli.py:1298-1331`) — writes `.cc-autopilot/ideation_proposals/<TB-N>.json` records directly (the directory is fenced by TB-188 in `TASK_AGENT_FENCED_PATHS`).
- **`ap2 cron edit`** (operator-CLI-only per TB-146) — writes `.cc-autopilot/cron.yaml` directly (cron.yaml is fenced).

If the operator runs either while a task agent is in flight, the post-hoc snapshot diff (TB-110) detects the fenced-file mutation and rolls back the task's run — same false-positive pattern that just cost ~$12.55 on post-train via the `ap2 ack` path (see sibling TB).

Unlike `ap2 ack` (frequent operator surface; warrants full queue-routing retrofit), `backfill-proposals` and `cron edit` are RARE operator operations — `backfill-proposals` is a one-off historical-record seed (TB-195 docstring: "Operator-driven one-off"); `cron edit` is operational tuning that happens during the project's setup phase or when adjusting cadence, not routine. For these, the cheapest fix that prevents the false-positive is a pre-flight refuse-if-active check: if the board's Active section has any task, exit non-zero with a clear message pointing the operator at `ap2 status`. The operator waits for the task to complete (or pauses the daemon), then retries.

Why now: the post-train cascade at 2026-05-12T06:40-07:14Z surfaced the false-positive-rollback class of bug via `ap2 ack`. The sibling TB queue-routes `ack`; this TB closes the two adjacent surfaces with the simpler "refuse when active" approach so the same class of bug can't bite via these less-frequent verbs.

## Scope

- `ap2/cli.py::cmd_backfill_proposals` (line 1298-1331) — pre-flight: read the board, refuse if Active is non-empty with message `"ap2 backfill-proposals: a task is currently active (TB-N) — refusing.\n  backfill-proposals writes to fenced .cc-autopilot/ideation_proposals/ and racing the active task would trigger a state_violation rollback.\n  Wait for the task to complete (see ap2 status) or pause the daemon, then retry."`. Exit code 1.
- `ap2/cli.py` — locate the cron edit CLI handler (probably `cmd_cron` or `cmd_cron_edit` — grep `def cmd_cron` returns `cmd_cron_list` at line 1350; the edit handler may be elsewhere or invoked from a subcommand structure). Apply the same pre-flight refuse-if-active check with an analogous message naming cron.yaml as the fenced path.
- New tests in `ap2/tests/test_cli.py` covering both refuse paths.
- No queue-routing — these surfaces stay synchronous-write. Just blocked from running mid-task.

## Design

### Why refuse-if-active rather than queue-routing

Queue-routing has bookkeeping overhead — register the op in `OPERATOR_QUEUE_OPS`, add a drain-side handler, design the queue payload, update tests. Worth it for frequent surfaces (ack, classify, update-goal). For one-off operations that the operator runs once a week (or less), the operator-visible cost of "wait for the active task to finish, retry" is small, and the implementation cost is one pre-flight check per surface. Avoids architectural retrofitting that doesn't pay rent.

### How to check "is a task active"

Two equivalent approaches:

1. Read `TASKS.md` via `Board.load(cfg.tasks_file)` and check `board.sections["Active"]`. This is what `ap2 status` does; consistent surface.
2. Check `events.jsonl` tail for `task_start` events not followed by `task_complete`. Less reliable (events can lag); not recommended.

Use approach (1). The check is a few lines:

```python
board = Board.load(cfg.tasks_file)
active = list(board.iter_tasks(section="Active"))
if active:
    print(
        f"ap2 backfill-proposals: a task is currently active "
        f"({active[0].id}) — refusing.\n"
        f"  <fenced path explanation>\n"
        f"  Wait for the task to complete (see `ap2 status`) or pause "
        f"the daemon, then retry.",
        file=sys.stderr,
    )
    return 1
```

### Pause-daemon escape hatch

The error message points operators at "pause the daemon" as an alternative to waiting. `ap2 pause` flips the daemon's pause flag, which halts task dispatch — but does NOT abort a currently-running task. So pausing helps for "I want to run backfill-proposals AFTER this task finishes without competing with new ones," not for "I want to run it RIGHT NOW while a task is in flight." Worth saying so in the message so operators don't expect pause to be a shortcut.

Alternative: a `--force` flag that bypasses the check. NOT in v1 — the whole point is preventing the operator from accidentally creating the cascade. If a real need for force surfaces, add it as a follow-up TB.

### No state_violation exclusion needed

This task does NOT add `cron.yaml` or `.cc-autopilot/ideation_proposals/` to `_VIOLATION_CHECK_EXCLUDED_PATHS` — both files remain in the violation check. The refuse-if-active gate prevents operator-driven writes from racing the snapshot window; the violation check stays strict so a task-agent attempting to write either is still caught.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "a task is currently active" ap2/cli.py` — error message is wired in both handlers (≥2 hits).
- prose: a test in `test_cli.py` exercises `cmd_backfill_proposals` with a synthetic Active task on the board; asserts the CLI exits non-zero, stderr contains both "backfill-proposals" and "active" and "refusing", and `.cc-autopilot/ideation_proposals/` is unmodified.
- prose: a test pins the empty-Active happy path — `cmd_backfill_proposals` succeeds when board has 0 Active (uses the existing TB-195 test fixture pattern).
- prose: a test pins the same shape for the cron edit CLI — synthetic Active task → cmd refuses with stderr containing "cron" and "active" and "refusing"; empty Active → cmd proceeds normally.
- prose: a test pins that the refuse path does NOT mutate `cron.yaml` or `.cc-autopilot/ideation_proposals/` — capture the directory's mtime / file list before and after; assert no changes when the refuse fires.

## Out of scope

- Queue-routing either verb. Sibling TB covers the ack case which justifies the architectural overhead; these don't.
- `--force` flag to bypass the active-task check. Add if friction observed, not pre-emptively.
- Refusing `ap2 rollback` when active. Rollback already has a dirty-working-tree pre-flight that mitigates; bundling that re-check is scope creep.
- A repo-wide lint asserting "every CLI verb writing a fenced path is either queue-routed OR has a refuse-if-active pre-flight." Useful invariant for future maintenance but separate concern.
- Backfilling historical records that may have been lost to prior false-positive rollbacks. Forward-looking only.
- Updating ap2/howto.md operator docs to describe the new refusal behavior. Documentation TB if desired separately; the CLI's stderr message is the primary surface.
- Pausing the daemon automatically on first invocation. Operator decides.
