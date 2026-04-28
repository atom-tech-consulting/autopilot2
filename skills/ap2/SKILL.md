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
- A bare name like `stoch` → resolved to `/Users/claude-agent/repos/<name>` (default sandbox location).
- An absolute path to a project root that has `.cc-autopilot/` in it.

If no argument is given, list projects under `/Users/claude-agent/repos/` and ask which one.

## Steps

Resolve `PROJECT_ROOT` from the argument (see Usage). The paths below are relative to that root.

Run all reads as **lzhang** (you), not claude-agent — state files are group-readable (`staff`) so no sudo is needed. Exception: `ap2 status` needs sudo because it resolves through the daemon's PID file and may check via process inspection; prefer to compute daemon state manually instead (see below).

### 1. Board state — parse `TASKS.md`

Count task lines under each section. Output:

```
board:    <A>A / <R>R / <B>B / <C>C / <F>F
```

Use: grep or a small awk pass. Don't invoke `ap2 status` for this — more robust and avoids sudo.

### 2. Daemon liveness — check `.cc-autopilot/daemon.pid`

Read the PID file. Then `ps -o pid,etime,command -p <pid>` to confirm the process is alive and show uptime. Output:

```
daemon:   running (pid <N>, up <ETIME>)  OR  stopped (stale pid file / no pid file)
```

### 3. Recent events — tail `.cc-autopilot/events.jsonl`

Show the last 10 events of interesting types (`task_start`, `task_complete`, `task_error`, `task_timeout`, `retry_exhausted`, `backlog_auto_promoted`, `cron_complete`, `ideation_empty_board`, `ideation_complete`, `ideation_error`, `ideation_timeout`, `daemon_start`, `daemon_stop`, `mattermost*`). Skip noisy `cron_start`. Format one line per event:

```
<ts>  <type>  <key=val key=val...>
```

Keep summaries truncated to ~120 chars.

### 4. Recent commits — `git -c safe.directory='*' -C PROJECT_ROOT log --oneline -5`

The real record of what shipped. Daemon commits carry task IDs in subject lines. The `safe.directory='*'` flag is required: the repo is owned by claude-agent, so git refuses to read it from lzhang without it.

### 5. Last task summary — tail `.cc-autopilot/progress.md`

progress.md is oldest-first (tasks append at the bottom). Print the LAST `## [timestamp]` section, not the first. Caps at ~40 lines.

```bash
awk '/^## \[/{start=NR} {lines[NR]=$0} END{for(i=start;i<=NR;i++)print lines[i]}' .cc-autopilot/progress.md | head -40
```

If the latest progress entry is much older than the latest `task_complete` in events.jsonl, flag it — means the daemon's task agents have stopped appending to progress.md (known issue on stoch through Apr 21).

### 6. Summary block

Write a 2-line summary at the top of the output:

```
<project>: <daemon-status>; <board-counts>; last task <TB-N> at <ts> (<commit>)
open issues: <retries/errors from last 50 events, or "none">
```

## Rules

- **Read-only.** Never edit files, restart daemon, or promote tasks. This skill reports, nothing else.
- **No sudo required.** Use file reads; claude-agent's project clone is group-readable.
- **Tolerate missing files.** If `daemon.pid` doesn't exist, report "stopped". If `events.jsonl` is empty, report "no events yet". If `PROJECT_ROOT` doesn't exist, say so and list available `~claude-agent/repos/*`.
- **Keep it under 40 lines of output.** If the user wants more, they can cat files directly.

## Example output

```
stoch: running (pid 98400, up 01:23:45); 0A / 0R / 0B / 10C / 1F; last task TB-11 at 2026-04-21T20:39Z (eb75288)
open issues: none

board:    0A / 0R / 0B / 10C / 1F
daemon:   running (pid 98400, up 01:23:45)

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
