# Add update_goal as an operator queue op so goal.md can be safely refreshed while the daemon runs

Tags: #operator-queue #goal #cli

## Goal

Today, refreshing `goal.md` while the daemon is running is unsafe: ideation reads `goal.md` mid-cycle (anchors injected into the prompt; `_goal_md_anchors` consulted by `_validate_briefing_structure` at queue-append time for TB-161), the per-task verifier (TB-69) reads it as part of the rollback-cohesion state surface, and an in-place edit racing a snapshot-window write can be torn against any of those readers. The only safe workflow is `ap2 daemon-control --pause`, edit `goal.md`, commit, `--resume` — every minor focus-rotation forces a pause-cycle.

Every other operator state mutation that touches a file the daemon reads — board edits, briefing rewrites, ideation triggers, approvals, rejects — already routes through `.cc-autopilot/operator_queue.jsonl`, drained as the first stage of each tick under `board_file_lock`. The queue gives ordering vs. in-flight task / ideation runs (TB-131), survives `git reset --hard <pre_run_head>` rollback (TB-110), and lands as a single coherent `state: drained N operator op(s)` commit. `goal.md` should ride the same path. The decisions ideation surfaces in `## Decisions needed from operator` (web.py:2062-2071) routinely ask for "goal.md edits / focus-item rotations" — making that response require a daemon pause is exactly the friction that breaks the walk-away promise.

Why now: live operator workflow at `post-train` today — three goal.md edits in the last 24h (commits `1e69caf` Stage 0 narrowing, `4cc91d8` HP-sweep lever, `57524e2` curation strategies lever), each one forced through pause/edit/resume because there is no safer path. This is the most-friction operator op in the current toolkit and it directly contradicts the `## Done when` bullet "An operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention" — a week with zero focus-rotations is unrealistic for any non-trivial target project, and the current pause-required workflow trains operators to batch goal edits or skip them altogether (which degrades ideation quality, the "## Current focus: ideation quality signal collection" item).

## Scope

- `ap2/tools.py` — extend `OPERATOR_QUEUE_OPS` with `update_goal`; add the `update_goal` branch to `do_operator_queue_append` (queue-append handler with content + reason capture and content-validity check); add the `update_goal` branch to `_apply_operator_op` (drain-side: atomic write of `goal.md`, append touched paths); extend `_append_operator_audit_line` for the `update_goal` branch (`<ts> — operator updated goal.md (<reason>)` line in `operator_log.md`).
- `ap2/daemon.py` — add `goal.md` to `_STATE_FILE_NAMES` so the drain-side `_commit_state_files` allowlist accepts it (without this, `_filter_state_paths` silently drops it and the queue-routed write would land dirty in the worktree, the same failure mode TB-192 catches for `_index.md`).
- `ap2/cli.py` — new `ap2 update-goal` subcommand: takes `--file <path>` (read content from path) or `--file -` (read from stdin), optional `--reason "..."`, dispatches via `do_operator_queue_append({"op":"update_goal", ...})`. Symmetric to how `ap2 add --briefing-file` reads the briefing payload.
- `ap2/tests/` — new tests covering: queue-append validation (empty content rejected; valid content queued with reason captured); drain-side application (atomic write + audit line + touched-paths surfacing + `_STATE_FILE_NAMES` allowlist hit); CLI dispatch (file path + stdin paths + `--reason` flag plumbing).

## Design

**Op shape.** `{"op": "update_goal", "args": {"goal_content": "<full file>", "reason": "<optional>"}}`. Full-file replacement, not a diff/patch — symmetric to how `add_*` ops carry the full briefing payload, atomic-write semantics are simpler than 3-way merge, and `goal.md` is small enough that the size cost is negligible. `reason` defaults to `""` and feeds the operator-log audit line.

**MM-handler exposure: NO.** This op is operator-CLI-only. The `claude-bot` Mattermost-handler agent has no path to `update_goal` — `prompts.py:268` already documents the design intent ("operator-curated; if you think it needs updating, raise the recommendation in your RESULT summary; do NOT rewrite"), and the handlers restricted toolset (TB-145) does not expose this op. Same precedent as `cron_edit` / `board_edit` being CLI-only after TB-145.

**Queue-append-side validation** (`do_operator_queue_append`):
- `goal_content` must be non-empty (whitespace-stripped).
- Content must parse without raising — call `_goal_md_anchors(content)` defensively to catch parser explosions before the op lands in the queue. Empty anchor list is OK (placeholder goal.md is a documented valid state per `check.py:226-271`); a parser exception is not.
- Single-line `reason` (run through `_validate_single_line` like other free-text args, TB-134).

**Drain-side handler** (`_apply_operator_op` `update_goal` branch):
- Atomic write: write `goal_content` to a sibling tempfile under `cfg.project_root`, then `os.replace` to `goal.md`. (Same atomicity guarantee `Board.save` uses for `TASKS.md`.)
- Add `"goal.md"` and `".cc-autopilot/operator_log.md"` to the drains `touched` set so both ride the `state: drained N operator op(s)` commit. operator_log.md is already in `_STATE_FILE_NAMES`; `goal.md` is the new addition.
- Emit a `goal_updated` event with the reason snapshot (and content-byte-count for diagnostics) so post-mortems can grep.

**`_append_operator_audit_line`** (`update_goal` branch): write `<ts> — operator updated goal.md (<reason>)` to `operator_log.md`, mirroring the `reject` branchs audit-richness. Empty `reason` collapses to `<ts> — operator updated goal.md`.

**Why `goal.md` belongs in `_STATE_FILE_NAMES`.** TB-111/TB-112 introduced linear rollback against the daemon-owned state surface. Once `goal.md` becomes daemon-mutable via `update_goal`, rollback consistency demands it be in the snapshot baseline — otherwise an `ap2 rollback` past an `update_goal` commit would leave goal.md at the new content while everything else reverts. Adding it here also means out-of-band edits during pause get auto-picked up by the next snapshot/diff cron commit (acceptable: pause-edits are still rare and the auto-commit eliminates an entire class of "operator forgot to commit goal.md" footgun).

**CLI shape** (`cmd_update_goal`):
- `ap2 update-goal --file <path> [--reason "..."]`
- `ap2 update-goal --file - [--reason "..."]` (stdin)
- Read content, run minimal client-side sanity check (non-empty, < ~100KB), call `do_operator_queue_append({"op":"update_goal","args":{"goal_content":content,"reason":reason}})`, print `"queued update_goal (lands at next tick)"`.
- No `--skip-goal-alignment` analog: this op IS the goal-alignment update, no anchor check applies to it.

**No interaction with the briefing goal-anchor check (TB-161).** Briefings queued before an `update_goal` op were validated against the OLD anchors at append time; thats the intended audit-trail semantics (the operator authored the brief against the goal as-of-then). Drain order is queue-append order so a queued update_goal followed by a queued add_backlog applies update_goal first; the add lands with possibly-stale anchor citations, which is acceptable historical record. Conversely, briefings queued AFTER update_goal will validate against the new on-disk content because `_validate_briefing_structure` reads goal.md synchronously at append time.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_operator_queue.py` (or whichever file the new tests land in) — covers append-side validation (empty rejected, valid queued, reason captured), drain-side atomic write + audit line + touched_paths, CLI dispatch via file + stdin paths.
- prose: `OPERATOR_QUEUE_OPS` in `ap2/tools.py` includes `"update_goal"`; the constants docstring/comment block explains the op alongside `update` / `approve` / `reject` / `ideate`.
- prose: `_apply_operator_op` in `ap2/tools.py` has an `update_goal` branch that performs an atomic rename (e.g. write-tempfile-then-`os.replace`) onto `goal.md`, emits a `goal_updated` event, and is invoked under `board_file_lock` like all other drain-side handlers.
- prose: `_STATE_FILE_NAMES` in `ap2/daemon.py` contains `"goal.md"`; the comment-block above the constant is updated to call out the addition (rationale: rollback cohesion once `update_goal` makes goal.md daemon-mutable).
- prose: `ap2/cli.py` has a `cmd_update_goal` (or equivalently named) subcommand wired into the argparse dispatch table, accepting `--file` (path or `-`) and an optional `--reason`, and routing through `do_operator_queue_append`.
- prose: the MM-handlers allowed-tool surface (`MM_HANDLER_TOOLS` / `operator_queue_append` MCP tools op enum) does NOT include `update_goal` — the op is operator-CLI-only by design.

## Out of scope

- Editing `goal.md` via diff/patch instead of full-file replacement. Full file is simpler and `goal.md` is small; revisit only if operators routinely run into merge-conflict pain.
- A Mattermost / chat surface for proposing goal updates. The handler is already explicitly told not to rewrite goal.md (`prompts.py:268`); chat-side proposals stay as RESULT-summary recommendations the operator manually applies via `ap2 update-goal`.
- Validating that the new goal.md content "still cites" anchors that pre-existing Backlog briefings reference. Stale anchor citations on already-queued / already-on-board tasks are an audit-trail artifact, not a correctness bug — the operator who queued those briefings explicitly authored them against the prior goal.
- Auto-promoting Frozen tasks when goal.md changes. Frozen → Backlog already has its own operator path (`ap2 unfreeze`); coupling that to goal updates would surprise the operator more than it helps.
- Auto-rejecting in-flight Active tasks on goal change. Active tasks complete or fail on their own merits; goal drift mid-task is the operators problem to handle via `ap2 delete --force` if truly needed.
