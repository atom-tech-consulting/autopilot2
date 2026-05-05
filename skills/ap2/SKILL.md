---
name: ap2
description: "Check status + recent work of an ap2 daemon for a specific project (board state, daemon liveness, recent events, recent commits, last task summary)."
user_invocable: true
---

<command-name>ap2</command-name>

# ap2 — daemon status + recent work

One-command snapshot of an ap2-driven project: is the daemon alive, what's on the board, what did it do lately, what's the last task summary.

## Usage

```
/ap2 <project>
```

`<project>` is either:
- A bare name like `stoch` → resolved to `~$AP2_SANDBOX_USER/repos/<name>` (`AP2_SANDBOX_USER` defaults to `claude-agent` — the canonical sandbox layout from the runbook).
- An absolute path to a project root that has `.cc-autopilot/` in it.

If no argument is given, list projects under `~$AP2_SANDBOX_USER/repos/` and ask which one.

## Steps

Resolve `PROJECT_ROOT` from the argument (see Usage). The paths below are relative to that root. To resolve the sandbox home, use `eval echo "~${AP2_SANDBOX_USER:-claude-agent}"`.

Run all reads as the human user (you), not the sandbox user — state files in the sandbox clone are group-readable (`staff` on macOS, the daemon's primary group on Linux) so no sudo is needed. Exception: `ap2 status` needs sudo because it resolves through the daemon's PID file and may check via process inspection; prefer to compute daemon state manually instead (see below).

### 1. Board state — parse `TASKS.md`

Count task lines under each section. Output:

```
board:    <A>A / <R>R / <B>B / <P>P / <C>C / <F>F
```

The five sections are Active, Ready, Backlog, Pipeline Pending (TB-103 long-running pipelines), Complete, Frozen — match the order `ap2 status` itself prints.

Use: grep or a small awk pass. Don't invoke `ap2 status` for this — more robust and avoids sudo.

### 2. Daemon liveness — check `.cc-autopilot/daemon.pid`

Read the PID file. Then `ps -o pid,etime,command -p <pid>` to confirm the process is alive and show uptime. Output:

```
daemon:   running (pid <N>, up <ETIME>)  OR  stopped (stale pid file / no pid file)
```

### 3. Recent events — tail `.cc-autopilot/events.jsonl`

Show the last 10 events of interesting types (`task_start`, `task_complete`, `task_error`, `task_timeout`, `retry_exhausted`, `backlog_auto_promoted`, `cron_complete`, `ideation_empty_board`, `ideation_complete`, `ideation_error`, `ideation_timeout`, `daemon_start`, `daemon_stop`, `daemon_pause`, `daemon_resume`, `task_unfrozen`, `task_deleted`, `operator_queue_append`, `operator_queue_drained`, `mattermost*`). Skip noisy `cron_start`. Format one line per event:

```
<ts>  <type>  <key=val key=val...>
```

Keep summaries truncated to ~120 chars.

### 4. Recent commits — `git -c safe.directory='*' -C PROJECT_ROOT log --oneline -5`

The real record of what shipped. Daemon commits carry task IDs in subject lines. The `safe.directory='*'` flag is required: the repo is owned by the sandbox user, so git refuses to read it from a different user without it.

### 5. Last task summary — tail `.cc-autopilot/progress.md`

progress.md is oldest-first (tasks append at the bottom). Print the LAST `## [timestamp]` section, not the first. Caps at ~40 lines.

```bash
awk '/^## \[/{start=NR} {lines[NR]=$0} END{for(i=start;i<=NR;i++)print lines[i]}' .cc-autopilot/progress.md | head -40
```

If the latest progress entry is much older than the latest `task_complete` in events.jsonl, flag it — means the daemon's task agents have stopped appending to progress.md.

### 6. Summary block

Write a 2-line summary at the top of the output:

```
<project>: <daemon-status>; <board-counts>; last task <TB-N> at <ts> (<commit>)
open issues: <retries/errors from last 50 events, or "none">
```

If the daemon is up, also surface (one line each, only when present):

- `web:      http://127.0.0.1:<port>/` — the bundled read-only UI URL (TB-130). The daemon prints this line in `ap2 status` whenever `AP2_WEB_DISABLED` is unset, so the operator doesn't need a separate `ap2 web` step.
- `pending:  N operator op(s)` — depth of the operator queue (TB-131). Non-zero with the daemon up means the next tick will drain them; non-zero with the daemon down is a stalled-queue red flag worth surfacing in `open issues`.

These two are sourced from `ap2 status` itself when available, OR by reading `.cc-autopilot/operator_queue.jsonl` line count for `pending:` and resolving the URL from `AP2_WEB_HOST` / `AP2_WEB_PORT` / defaults otherwise.

## Rules

- **Read-only.** Never edit files, restart daemon, or promote tasks. This skill reports, nothing else.
- **No sudo required.** Use file reads; the sandbox user's project clone is group-readable.
- **Tolerate missing files.** If `daemon.pid` doesn't exist, report "stopped". If `events.jsonl` is empty, report "no events yet". If `PROJECT_ROOT` doesn't exist, say so and list available `~$AP2_SANDBOX_USER/repos/*`.
- **Keep it under 40 lines of output.** If the user wants more, they can cat files directly.

## Example output

```
stoch: running (pid 98400, up 01:23:45); 0A / 0R / 0B / 0P / 10C / 1F; last task TB-11 at 2026-04-21T20:39Z (eb75288)
open issues: none

board:    0A / 0R / 0B / 0P / 10C / 1F
daemon:   running (pid 98400, up 01:23:45)
web:      http://127.0.0.1:8729/
pending:  0 operator ops

recent events:
  2026-04-21T20:39:05Z  task_complete    task=TB-11 commit=eb75288 summary=TB-11 CLI + run-config landed...
  2026-04-21T20:29:49Z  task_start       task=TB-11 title=CLI entrypoint + run config
  2026-04-21T20:29:49Z  backlog_auto_promoted  task=TB-11
  2026-04-21T20:28:49Z  task_complete    task=TB-10 commit=2d5474a summary=Added stoch/io/...
  ...

recent commits:
  eb75288 TB-11: CLI entrypoint + run config
  2d5474a TB-10: Trade log + result export
  51149d9 TB-8: Baseline cross-sectional momentum strategy + runner
  d27cd03 TB-7: Reporting (equity curve + trade diagnostics)
  655f168 TB-6: Performance metrics module

last task:
  ## [2026-04-21 20:39] TB-11: CLI entrypoint + run config
  - Result: complete.
  - Summary: ...
```

## Adjacent commands the operator may want next

This skill is read-only. If the operator follows up with a board-mutating
ask, route them to the right CLI — the post-TB-131 surface is queue-routed,
so each prints `... (will land at next tick)` and the daemon drains on its
next tick:

- `ap2 unfreeze <TB-N>` — Frozen → Backlog + reset retry counter.
- `ap2 backlog <TB-N>` — any-section → Backlog (used to be `ap2 skip`).
- `ap2 delete <TB-N>` — permanent removal; refuses Active/Ready without `--force`.
- `ap2 reject <TB-N> [--reason "..."]` — ideation proposals only (Backlog + `@blocked:review`); drops the row AND captures the rejection reason in `.cc-autopilot/operator_log.md` so ideation Step 0 stops re-proposing it. Use `ap2 delete` for everything else (typos, superseded tasks, etc.).
- `ap2 ideate [--force]` — manual ideation trigger (TB-159). Bypasses the cooldown / disable / non-empty-Ready-or-Backlog gates so the operator can run an ideation cycle on demand. Refuses if a task is currently Active unless `--force` (concurrent task-agent + control-agent SDK runs are risky — TB-122 split MM-handler vs task agent for this reason). Forced runs still call `mark_run` so the next natural cooldown clock resets.
- `ap2 pause` / `ap2 resume` — pause/resume daemon ticks (the flag file is
  daemon-checked, not queue-routed; effective immediately).
- `ap2 ack <TB-N> <decision>` — record an operator decision so ideation
  stops re-proposing actions whose effects aren't filesystem-visible.
- `ap2 rollback ...` — destructive history walk; not queue-routed.

Do **not** invoke any of these from this skill — surfacing the command is
fine, executing it isn't.
