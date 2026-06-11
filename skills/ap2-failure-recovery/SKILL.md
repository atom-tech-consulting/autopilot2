---
name: ap2-failure-recovery
description: "Use when an ap2 daemon hit trouble — a task failed / froze / retry-exhausted, the daemon restarted with an Active task, a pipeline or agent crashed, the watchdog went quiet, or you need to intervene (`ap2 unfreeze` / `ap2 delete`) or answer 'why did TB-N fail / what just happened?'."
---

# ap2 failure recovery — auto-recovery & the operator-intervention playbook

The recovery / escalation manual for an ap2 daemon: what the daemon
heals on its own, and where the operator looks (and which verb the
operator runs) when something has gone wrong. An operator should never
have to grep `ap2/howto.md` to learn how a crash is salvaged or how to
triage a Frozen task. Two self-contained surfaces:

- **Failure modes the daemon recovers from** — the auto-recovery
  catalogue: which failures (SDK crash, commit-without-report, restart
  mid-task, retry exhaustion, idle watchdog, stuck blocker, malformed
  task line) the daemon detects and heals itself, and the event /
  operator-verb each surfaces.
- **Operator-question playbook** — the "where do I look?" lookup table
  for the questions an operator asks when intervening (is the daemon
  running, why did TB-N fail, what did the agent commit, is a pipeline
  still running, what did ideation propose), plus the read-only `ap2 web`
  UI for scanning visually.

For the CLI verbs named below (`ap2 unfreeze`, `ap2 delete`, `ap2 check`,
`ap2 web`, `ap2 audit`) see the **ap2-board-ops** skill for the full
verb table; for the event types named below (`task_implicit_commit`,
`board_malformed_line`, `task_unfrozen`, `task_deleted`) and how to tail
them, see the **ap2-observability** skill.

## Failure modes the daemon recovers from

- **SDK subprocess crash with empty stderr.** All SDK calls capture
  stderr through a 200-line ring buffer; `task_error` / `cron_error` /
  `ideation_error` events carry `stderr_tail` + `prompt_dump` paths.
- **Agent committed but didn't report.** `_infer_result_from_head` reads
  `git log -1`; subject starting `<TASK_ID>:` → synthesize a complete
  result. Emits `task_implicit_commit`.
- **Active task on daemon restart.** `_recover_orphans` moves it back
  to Ready with retry counter incremented.
- **Failing task that retry-exhausts.** Goes to Frozen. Operator
  unfreezes with `ap2 unfreeze TB-N` (resets retry counter atomically),
  or permanently removes it with `ap2 delete TB-N` (atomic; emits
  `task_deleted` event; refuses Active/Ready without `--force`).
- **Daemon idle >3h.** Watchdog builds a `DiagnoseReport` (board,
  recent failures, cron staleness, board health) and posts to
  `AP2_MM_CHANNELS[0]`.
- **Stuck blocker.** `Board.next_dispatchable` skips Backlog tasks
  whose `(blocked on: TB-X)` blockers are unsatisfied. Diagnose
  surfaces unsatisfiable cases (Backlog blocked on Frozen).
- **Malformed task line.** `Board._parse` flags any line not matching
  `TASK_LINE_RE`; daemon emits dedup'd `board_malformed_line` event.

## Operator-question playbook

When you're asked questions about the daemon's state or behavior, here's
where to look:

| Question | Read |
|---|---|
| Daemon running? | `cat .cc-autopilot/daemon.pid && ps -p <pid>` |
| What's the board look like? | `awk` over `TASKS.md` for section counts |
| What just happened? | `tail -30 .cc-autopilot/events.jsonl \| jq -c` |
| Why did TB-N fail? | Filter `events.jsonl` for `task=TB-N` then read its briefing |
| What did the agent commit? | `git log --grep=TB-N --oneline` |
| Is a pipeline still running? | `ps -p <pid>` for the pid in the `pipeline_start` event |
| What were the verifier's bullets? | The briefing's `## Verification` section |
| What did ideation propose? | Last `ideation_complete` event's `summary` field |
| What's the latest assessment? | `cat .cc-autopilot/ideation_state.md` |
| What's been published as "learned"? | `cat .cc-autopilot/insights/_index.md` |
| What has the operator decided / acked? | `cat .cc-autopilot/operator_log.md` |
| Recent commits? | `git log --oneline -20` |
| Are state files well-formed? | `ap2 check` (errors: TASKS.md shape, JSON state, cron schema; warnings: stale brief links, insights front matter, missing goal.md) |

The `ap2 web` command starts a read-only HTTP UI at `127.0.0.1:7820`
with `/events`, `/tasks`, `/task/<TB-N>`, `/pipelines`, `/insights`,
`/ideation_state`, `/commits`, `/stats` pages. Useful when you want
to scan visually rather than ask the session to summarize.
