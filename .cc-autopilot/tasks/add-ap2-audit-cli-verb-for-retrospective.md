# Add `ap2 audit` CLI verb for retrospective review of unreviewed Complete + Frozen tasks; state derived from operator_log.md (no new file)

Tags: `#autopilot` `#cli` `#operator-surface` `#code-quality` `#regression-pin`

## Goal

Advance the Mission's walk-away promise (Done-when bullet 1: **"an operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention"**) by closing the retrospective review surface gap. Today an operator returning after a multi-day walk-away has to assemble the review picture from 4-5 sources: `ap2 status` for current state, `ap2 logs --since X` for the audit trail, Mattermost scrollback for the 2h-cadence digests, `git log` for the codebase delta, and per-task `ap2 classify TB-N` invocations for retrospective verdicts. None of these is consolidated; none answers "which tasks have I NOT yet reviewed?" directly. Under `AP2_AUTO_APPROVE=1` this gap is acute — every auto-approved task ships without operator-in-the-loop review at dispatch time, so retrospective review is the operator's ONLY judgment surface, yet that surface is unfocused. Close it with a single `ap2 audit` CLI verb that lists unreviewed shipped tasks (Complete + Frozen) since the operator's last audit, with an optional `--interactive` walkthrough that prompts per-task for classify / skip / rollback. State for "what's been reviewed" lives entirely in `operator_log.md` (no new state file) — derived from existing `classified TB-N` entries (written by `ap2 classify` per TB-189) plus new `audit-skipped TB-N` entries (written by the audit walk's `[s]kip` action via the operator queue).

Why now: TB-223 (auto-approve) shipped; TB-232 (dry-run on-ramp) shipped; TB-227/238/241/242/243/244 surface live state + 24h counters. Operator is approaching real auto-approve enablement. The "live state visible" gap is closed by those tasks; the "retrospective review consolidated" gap is the next-largest barrier to operator-trustable walk-away. Closing it now means the first `AP2_AUTO_APPROVE=1` deployment has a coherent return-and-review surface from day one.

## Scope

(1) **New CLI subcommand `ap2 audit`** in `ap2/cli.py`. Default mode lists unreviewed shipped tasks; flags add interactive walkthrough, JSON output, and cursor reset.

  - Default invocation (`ap2 audit`): prints a table to stdout with one row per unreviewed task, columns: `TB-N | status | commit | auto_approved | one-line summary | completed_at`. Tasks are listed in completion-time order (oldest first — operator gets to address them chronologically). At the end, prints `N unreviewed since <last-audit-ts>; run \`ap2 audit --interactive\` to walk through` OR `0 unreviewed since <last-audit-ts>; nothing to review` AND appends a `<ts> — ran audit (N unreviewed)` line to `operator_log.md` via the operator queue. The `ran audit` log entry is the audit cursor — the next invocation's "since" window starts at this timestamp.

  - `--interactive`: walks through each unreviewed task one at a time. For each, displays full task summary + the task_complete event's `summary` field + auto_approved status + briefing path. Prompt: `[c]lassify | [s]kip | [r]ollback | [n]ext | [q]uit`. `c` triggers a sub-prompt for verdict (`good` / `acceptable` / `wasteful` / `negative` per existing TB-189 enum) + reason, then queues `ap2 classify TB-N <verdict> --reason "..."` through the operator queue (re-uses existing CLI helper, doesn't duplicate the queue-append code). `s` queues a `audit-skipped TB-N` line append to `operator_log.md` via the operator queue (new operator-queue op-shape, see Scope §3). `r` triggers `ap2 rollback --to <commit-before-TB-N>` after confirming — uses existing rollback path. `n` advances to next task without recording anything (operator wants to think later). `q` exits, recording a `ran audit (reviewed M, skipped K, deferred L)` line to operator_log.md.

  - `--since <iso-date>`: override the cursor. Useful for "I want to re-review tasks from last month."

  - `--json`: machine-readable output. Useful for scripting (e.g. an external dashboard tool consuming `ap2 audit --json` output).

  - `--frozen-only` / `--auto-approved-only` / `--all`: filter shape. Default lists ALL unreviewed Complete + Frozen tasks; `--frozen-only` restricts to Frozen (operator triaging the freeze pile); `--auto-approved-only` restricts to tasks that landed via the auto-approve path (filtered by presence of `auto_approved` event for that TB-N).

(2) **State source: `operator_log.md` only** (no new state file). The audit cursor + reviewed-set are both derived from grep:

  - **Cursor (last-audit-ts)**: most recent line matching `<ts> — ran audit \(.*\)$`. If no such line exists (first-ever invocation), cursor = epoch — audit lists ALL shipped tasks.
  - **Reviewed set**: union of (a) tasks with a `<ts> — classified TB-N` line (existing TB-189 writer), (b) tasks with a `<ts> — audit-skipped TB-N` line (new writer per §3), (c) tasks with a `<ts> — rejected TB-N` line (existing reject writer — explicit operator decision).
  - **Unreviewed set**: tasks in TASKS.md's Complete + Frozen sections, with completion timestamp > cursor, NOT in the reviewed set.

(3) **New operator-queue op-shape `audit_skip`** routed through `do_operator_queue_append` with payload `{op: "audit_skip", task_id: "TB-N", reason: "<one-line>"}`. Drain handler in `_apply_operator_op` appends `<ts> — audit-skipped TB-N: <reason>` to `operator_log.md` under the existing `operator_log_lock`. Mirrors the existing `ack` op-shape (TB-106 pattern) — operator-CLI-only, single-line reason, daemon-safe serialization.

(4) **Tests** (`ap2/tests/test_audit_cmd.py`):
  - `test_audit_lists_unreviewed_since_cursor`: seed operator_log.md with a `ran audit` line at T0; seed TASKS.md Complete section with tasks T1 < T2 (both after T0), one with a classified entry T0 < t < T2; assert `ap2 audit` lists only T2 (T1 is classified).
  - `test_audit_lists_all_when_no_prior_cursor`: empty operator_log.md → all Complete + Frozen tasks listed.
  - `test_audit_filter_frozen_only`: only Frozen tasks listed under `--frozen-only`.
  - `test_audit_filter_auto_approved_only`: only tasks with an `auto_approved` event in events.jsonl listed under `--auto-approved-only`.
  - `test_audit_skip_queues_correct_op`: simulate `[s]kip` action; assert operator queue file gets a `{op: "audit_skip", task_id: "TB-N", reason: ...}` entry.
  - `test_audit_skip_drain_appends_operator_log`: drain handler runs the queue entry; assert operator_log.md gets the `<ts> — audit-skipped TB-N: <reason>` line.
  - `test_audit_run_appends_cursor_line`: `ap2 audit` invocation queues a `ran audit (N unreviewed)` line via the operator queue.
  - `test_audit_cursor_derives_from_most_recent_ran_audit`: operator_log.md has two `ran audit` lines (older, newer); cursor used is the newer.
  - `test_audit_classified_task_excluded`: task with `<ts> — classified TB-N good: ...` in operator_log → excluded from unreviewed set.
  - `test_audit_rejected_task_excluded`: task with `<ts> — rejected TB-N: ...` in operator_log → excluded from unreviewed set.

(5) **Howto.md update**: add `## Retrospective audit workflow` section documenting the `ap2 audit` verb, the [c]/[s]/[r]/[n]/[q] interactive prompts, the state-derivation logic (operator_log.md grep, no new file), and the `--auto-approved-only` filter as the "after walk-away" workflow.

(6) **Don't touch fenced paths**: the audit command READS TASKS.md (board structure for task list + sections), `events.jsonl` (timestamps + auto_approved markers), `operator_log.md` (state derivation), and per-task briefing files (display purposes). It WRITES nothing directly; all mutations route through the operator queue (new `audit_skip` op + re-use of existing `classify` op via the existing CLI helper). This preserves the agent-fence on briefings AND keeps daemon-vs-operator races serialized via the queue.

(7) **Not in scope**: 
  - Web UI for the audit (CLI-only this iteration; web extension is a separate TB if operator engagement shows value).
  - LLM auto-classification ("infer the verdict from task content") — explicit non-goal; classify is operator judgment, replacing it loses the signal that feeds back into ideation prompts.
  - Pre-flight blocking the daemon when audit backlog grows past N tasks (separate policy question; deferrable until backlog accumulation is empirically observed).
  - Mattermost notification when audit backlog crosses a threshold (separate observability surface).
  - A new operator-queue op-shape for `audit_run` (the cursor line) — re-uses the existing `ack` op-shape with a structured reason string (e.g. `ack op=audit_run reason="N unreviewed"`); avoids op-shape proliferation.

## Design

**Why state in operator_log.md rather than a new state file**: operator_log.md is already the single source of truth for every operator decision (reject, classify, ack, goal update). Adding "audit-skipped" + "ran audit" entries fits the same shape; no schema migration, no separate state file to keep in sync with the log. The grep-derivation cost is trivial (operator_log.md is small — single-digit MB at multi-year scale) and the file is already daemon-protected via `operator_log_lock`. A separate state file would create a sync question ("if audit_state.json says reviewed but operator_log.md doesn't, who wins?") that operator_log.md as single source elides entirely.

**Why route writes through operator queue**: the audit command runs in an operator-CLI process (not the daemon). Direct file writes to operator_log.md from the CLI would race with the daemon's own writes (cron status-report's ack, drain operations, etc.). The operator queue is the existing serialization mechanism — every other CLI verb that mutates daemon-touched state (`ap2 classify`, `ap2 ack`, `ap2 update-goal`, `ap2 approve`) routes through it. The audit command follows the same pattern, so the daemon-vs-CLI race window is closed by reuse, not by new locking primitives.

**Why `audit_skip` as a new op-shape rather than re-using `ack`**: `ack` is a generic operator-decision recorder; an audit-skip is semantically narrower ("operator reviewed this task and explicitly recorded 'no opinion'"). Distinguishing them lets future ideation read operator_log.md and tell "operator considered this and skipped" from "operator never noticed this and ack'd something unrelated." Cost: one new case in the drain handler. Benefit: cleaner downstream signal for the prompt-tuning iteration the next focus rotation will require.

**Why include Frozen tasks in the default review set**: Frozen tasks are the highest-signal review candidates — they've already cost agent attempts and operator attention. An audit that surfaces them lets operator unfreeze (after fixing whatever caused the freeze) or roll back (if the work is no longer wanted) without needing to remember which TBs are Frozen.

**Why operator-CLI-only (no Mattermost push, no daemon-side gating)**: audit is a deliberate operator action ("I'm sitting down to review the loop's work"), not an event-driven flow. Pushing audit prompts to Mattermost or gating the daemon on backlog size shifts it from operator-pulled to system-pushed, which conflicts with the walk-away model (operator pulls when they're ready, not when the system decides). Mattermost push for an audit backlog could be a future TB if operator engagement shows it's needed.

**Auto-approve interaction**: the audit doesn't require `AP2_AUTO_APPROVE=1` to be enabled — it works on manually-approved tasks too (they're equally "unreviewed" after completion). But `--auto-approved-only` is the natural filter for the walk-away workflow: after a week of unattended operation, the operator wants to see specifically what the loop chose to ship without their review at dispatch time.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q ap2/tests/test_audit_cmd.py` — new test module passes; minimum 10 cases per Scope §4.
- `grep -nE "def cmd_audit\b|add_subparsers.*audit" ap2/cli.py` — exit 0; the new subcommand is wired into the argparse tree.
- `grep -nE "audit_skip" ap2/tools.py` — exit 0; the new operator-queue op-shape is handled in the drain path.
- `grep -nE "audit-skipped|ran audit" ap2/tools.py` — exit 0; the operator_log.md writer formats are present.
- `[ "$(grep -nE 'ap2 audit' ap2/howto.md | wc -l)" -ge 3 ]` — at least 3 references to the new verb in the howto (header + workflow paragraph + flag example).
- `! grep -rE "audit_state\.json|audit_cursor\.json" ap2/` — exit 0 (zero matches; no new state file introduced — the design promise). The `!` inverts the no-match exit per the TB-187 idiom.
- Prose: the `ap2 audit` command WRITES nothing to disk directly — all mutations (audit-skip lines, ran-audit cursor lines) route through `do_operator_queue_append`. Judge confirms via `Read` of `cmd_audit` and verifying the only file writes go through `enqueue_*` helpers (not direct `open(..., 'w').write(...)` calls on operator_log.md or any briefing file).
- Prose: the cursor derivation (most recent `ran audit` line in operator_log.md) is computed by scanning the file linearly, not via cached state. Judge confirms by reading the cursor-derivation helper and verifying it reads operator_log.md fresh on each invocation.

## Out of scope

- Web UI surface for the audit walkthrough — CLI-only this iteration. Web extension is a separate TB if operator engagement shows value.
- LLM auto-classification — explicit non-goal. Classify is operator judgment; replacing it with LLM inference loses the signal that feeds back into ideation's prompt iteration.
- Pre-flight gating the daemon when audit backlog grows past a threshold — separate policy question; defer until empirical backlog accumulation is observed.
- Mattermost push notification when audit backlog crosses a threshold — separate observability surface; same defer-until-observed reasoning.
- Backfilling classify verdicts for tasks shipped before this command exists — historical; the command surfaces them as "unreviewed" and the operator works through them via the normal interactive flow if they want to.
- Multi-operator support (concurrent audit sessions) — single-operator model holds; if two `ap2 audit --interactive` runs happen concurrently, operator queue serializes the audit-skip ops but the operator sees stale state in their terminal. Acceptable for the single-operator design.
- Adding `audit_run` as a distinct operator-queue op-shape — re-uses existing `ack` op with structured reason string to avoid op-shape proliferation.
