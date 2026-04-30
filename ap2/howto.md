# ap2 — how it operates this project (Claude session quick-reference)

A condensed view of `ap2/README.md` + `ap2/architecture.md`, written for a
Claude Code session running inside an ap2-managed project (most often as
the `claude-agent` sandbox user). Covers what ap2 is, what's on disk,
the agent's contract, the operator-facing surfaces, and where to look
when answering questions like "why did TB-N fail" or "what's the
daemon doing right now."

## What ap2 is

A Python daemon (`ap2`) that drives a project through a list of tasks
without keeping any long-lived Claude session. Each unit of work — task,
cron, ideation, mattermost reply — runs as a fresh `claude_agent_sdk`
`query()` call. Shared state lives on disk in `TASKS.md`,
`.cc-autopilot/events.jsonl`, briefings, and a few state files. The
daemon never accumulates context.

Three design principles drive every other choice:
1. **Each unit of work runs in a fresh SDK query** — no compaction
   fatigue, no shared memory across runs.
2. **Shared awareness lives in files** — every spawned agent gets the
   relevant files inlined into its prompt (typically a briefing + a
   tail of `events.jsonl`).
3. **Mutations go through narrow MCP tools** — agents don't get
   `Write`/`Edit` access to daemon-owned files. Every state change goes
   through a typed handler that emits a structured event.

## On-disk layout

After `ap2 init`, the project gains:

```
TASKS.md                       # 5-section board, daemon-owned
goal.md                        # operator-curated mission (read by ideation)
CLAUDE.md                      # project conventions; daemon bumps Next task ID
.cc-autopilot/
├── progress.md                # append-only session log (per-task entries)
├── events.jsonl               # structured event stream (the canonical timeline)
├── cron.yaml                  # scheduled-job registry (status-report by default)
├── cron_state.json            # last-fired timestamps per cron
├── retry_state.json           # per-task retry counts
├── mm_state.json              # mattermost cursor + thread cache
├── auto_diagnose_state.json   # watchdog cooldown
├── ideation_state.md          # ideation's per-cycle progress assessment
├── daemon.pid                 # daemon process id (when running)
├── paused                     # presence-only: pause flag
├── env                        # KEY=VAL project-scoped overrides
├── tasks/                     # per-TB-N briefings (Goal/Scope/Verification)
├── insights/                  # project-output knowledge files (+ auto-index)
├── pipelines/                 # detached-pipeline logs (PID-named)
└── debug/                     # per-run prompt + stream + messages dumps
```

The 5-section board has a fixed order:
**Active → Ready → Backlog → Complete → Frozen**.

## The task agent contract

If you (the Claude session) are dispatched as a **task agent**, your
prompt is built from `_TASK_HEADER` + the briefing file + a tail of
recent events + `_TASK_FOOTER`. You must:

1. **Read the briefing first** at `.cc-autopilot/tasks/<task-slug>.md`.
   It has `## Goal` / `## Scope` / `## Verification` (your gate) /
   `## Out of scope`.
2. **Check for prior work.** Before you start: `git log --grep="<TASK_ID>" --oneline`.
   If a previous attempt committed but didn't report, decide whether to
   extend or accept the existing work — don't redo from scratch.
3. **Make code changes** with regular `Edit` / `Write` / `Bash`. **Do
   NOT touch** these files (the SDK actively rejects writes via
   `disallowed_tools`):
   - `TASKS.md` — daemon owns the board
   - `CLAUDE.md` — daemon bumps `Next task ID`
   - `goal.md` — operator-curated mission; if you think it needs an
     update, raise it in your `summary`, don't rewrite
   - `.cc-autopilot/progress.md` / `events.jsonl` /
     `ideation_state.md` / `cron.yaml`
4. **Commit your work** with subject starting `<TASK_ID>: ...`. The
   prefix is load-bearing — the daemon's HEAD-recovery path (TB-65)
   uses it to salvage runs where you crashed before reporting.
5. **Call `mcp__autopilot__report_result(...)` ONCE at the end.** This
   is the only completion signal the daemon listens for.

```python
report_result(
    status="complete",          # complete | incomplete | blocked | failed
    commit="a1b2c3d4",          # 7-40 char SHA, or "" if no commit
    summary="Added X to Y, all tests pass.",
    files_changed="foo/bar.py, foo/bar_test.py",
    tests_passed="true",        # "true" / "false"
)
```

Optional: `cron='[{"action": "add", "name": "...", "interval": "1h", "prompt": "..."}]'`
to register a recurring job; `add` requires action+name+interval+prompt.

If you forget to call the tool, the daemon reads `git log -1`. If HEAD's
subject starts with `<TASK_ID>:` it's salvaged as Complete; otherwise
the task shelves to Backlog and retries up to `AP2_MAX_RETRIES` (default
3), then Frozen.

### Long-running work — use `pipeline_task_start`

If your work would take >~5 minutes wall-clock (grid sweeps,
full-history backtests, Polygon-class data fetches, ML training,
anything with rate-limited APIs), don't run it inline. Call:

```python
pipeline_task_start(
    name="my-sweep",
    command="uv run python scripts/run_my_sweep.py",
)
```

The tool spawns the command detached, captures the pid +
`create_time()`, and emits a `pipeline_start` event. After your
`report_result(status="complete", ...)` the daemon moves THIS task
to a `Pipeline Pending` board section (TB-115). On every subsequent
tick, the daemon checks whether all of your spawned pids are dead.
Once they are, it re-runs your briefing's `## Verification`
against the post-pipeline working tree — pass → Complete, fail →
Backlog (with retry-counter bump) → Frozen on retry exhaustion.
You can call `pipeline_task_start` multiple times in one turn for
parallel pipelines (use distinct `name` values); the daemon waits
for ALL of them.

The briefing's `## Verification` IS the post-pipeline verification —
write it to check output artifacts (`test -f reports/foo.csv`,
JSON schema validation, etc.). Pre-TB-115's two-tier
launch-task-and-validation-task split is retired.

## What the daemon does each tick (~30s)

```
1. Mattermost — poll @claude-bot mentions → spawn handler agent per message
2. Cron       — run any due jobs from cron.yaml (status-report etc.)
3. Tasks      — pick next Ready, or auto-promote next dispatchable Backlog
                → run task agent
4. Ideation   — fire `_maybe_ideate` if working board is fully empty
                + cooldown elapsed (default 2h)
5. Watchdog   — `_maybe_auto_diagnose` posts to mattermost when daemon
                idle > 3h
```

Steps run sequentially. A failure in any step emits an event and
proceeds; one broken cron doesn't block task dispatch.

## Verification — what the daemon checks before Complete

Two layers wrap every successful task:

**Per-task** (`ap2/verify.py`). Parses the briefing's `## Verification`
section via mistune AST. Each bullet:
- **Shell** (` `cmd` ` or `` `` `cmd` `` ``) — runs via subprocess in
  the project root; exit 0 = pass.
- **Prose** (free text) — sent to an SDK judge that returns `pass` /
  `fail`; on judge crash or unparseable response, falls back to
  `unverified`.

Verdicts: `pass` → Complete. `partial` (some unverified, no fails)
→ Complete + `verification_partial` event. `fail` (any) → Backlog →
retry → Frozen at retry exhaustion.

The verifier picks the **last** `## Verification` heading. (Pre-TB-115
two-tier pipeline briefings used this property to keep the launch task's
own checks last while a sub-`validation_briefing` carried output-artifact
bullets earlier; the two-tier split is retired post-TB-115 — now the
single `## Verification` runs both at synchronous-completion time AND
post-pipeline as `_sweep_pipeline_pending` re-runs it.)

**Project-wide gate** (`AP2_VERIFY_CMD`, optional). Runs after the
per-task gate. Typical: `uv run pytest -q`. `--no-verify` tag opts
specific tasks out (e.g. docs-only changes).

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

## Custom MCP tools (reference)

The daemon registers the `autopilot` MCP server. Two pools, partitioned
by allowlist:

**Task agents** (`TASK_AGENT_TOOLS`):
- `report_result(status, commit, summary, files_changed, tests_passed, cron)`
- `pipeline_task_start(name, command)` (TB-115)
- Plus regular `Read`/`Glob`/`Grep`/`Bash`/`Edit`/`Write` (with the
  fenced paths blocked).

**Control agents** (cron, ideation, mattermost handler) —
`CONTROL_AGENT_TOOLS`. Read project state via `Read` / `Glob` / `Grep`;
mutate via narrow MCP tools. **No Bash** (TB-109 — closed the
shell-redirect-into-fenced-file corruption surface).
- `board_edit(action, task_id, title, tags, briefing, description, blocked_on)` — add/move/remove tasks
- `cron_edit(action, name, interval, prompt, active_when, max_turns)` — manage scheduled jobs
- `mattermost_reply(channel, text, thread_id)` — post to MM
- `log_event(type, summary)` — append a custom event (this is how
  cron emits `cron_complete` summaries and ideation emits
  `ideation_complete` summaries)
- `daemon_control(action, reason)` — pause/resume daemon
- `ideation_state_write(content)` — overwrite `ideation_state.md`
  atomically (only the ideation agent uses this)
- `git_log_grep(query, max_results)` — search git log by commit
  message (replaces ideation's old `Bash("git log --grep=...")`)
- `operator_log_append(note, task_id)` — append to
  `.cc-autopilot/operator_log.md` (mattermost handler uses this on
  `@claude-bot done: ...` messages)

## Event schema (the canonical timeline)

`.cc-autopilot/events.jsonl` is append-only. Every line has `ts` (UTC
ISO-8601) + `type`; other fields vary. Categories:

**Lifecycle.** `daemon_start`, `daemon_stop`, `daemon_pause`,
`daemon_resume`, `task_start`, `task_complete`, `cron_start`,
`cron_complete`, `ideation_empty_board`, `ideation_complete`.

**Failure.** `task_error`, `task_timeout`, `verification_failed` (per-
task or project-wide), `verification_partial`, `retry_exhausted`,
`cron_error`, `cron_timeout`, `ideation_error`, `ideation_timeout`,
`mattermost_error`, `mattermost_timeout`, `mm_poll_error`,
`state_commit_error`.

**State / observability.** `task_implicit_commit` (HEAD-salvage),
`task_unfrozen`, `backlog_auto_promoted`, `cron_proposed`,
`cron_proposal_error`, `ideation_state_updated`, `pipeline_start`,
`orphan_recovery`, `board_malformed_line`, `mattermost`,
`auto_diagnose_fired`.

`diagnose.MEANINGFUL_EVENT_TYPES` is what the watchdog counts as "the
daemon making progress"; `FAILURE_EVENT_TYPES` is what counts as broken.

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

`ap2 logs --json -n 30 \| jq` works too if the CLI is on PATH; defaults
truncate to 120 chars per field, `--json` gives full payloads.

The `ap2 web` command starts a read-only HTTP UI at `127.0.0.1:7820`
with `/events`, `/tasks`, `/task/<TB-N>`, `/pipelines`, `/insights`,
`/ideation_state`, `/commits` pages. Useful when you want to scan
visually rather than ask the session to summarize.

## Configuration knobs

Set in shell, in `<project>/.cc-autopilot/env`, or in
`~claude-agent/.zshenv`. A few that matter for understanding behavior:

- `AP2_TICK_S` (30) — daemon tick interval.
- `AP2_TASK_TIMEOUT_S` (1200) — per-task SDK query timeout.
- `AP2_TASK_MAX_TURNS` (50) — max turns per task agent.
- `AP2_CONTROL_TIMEOUT_S` (300) — per-control-agent timeout.
- `AP2_MAX_RETRIES` (3) — failed-task retries before Frozen.
- `AP2_VERIFY_CMD` — project-wide regression gate (e.g. `uv run pytest -q`).
- `AP2_IDEATION_DISABLED` — set to `1`/`true` to opt out of empty-board
  ideation.
- `AP2_IDEATION_COOLDOWN_S` (7200) — minimum gap between ideation runs.
- `AP2_MM_CHANNELS` — comma-separated MM channel IDs to poll.

Plus required: `CLAUDE_CODE_OAUTH_TOKEN`. Daemon refuses to start
without it.

## Sandbox model

The daemon runs as a separate OS user (`claude-agent` by default) so
its tools can't reach the human's `~/.ssh`, keychain, git config, or
other repos. OAuth token + Mattermost creds live in
`~claude-agent/.zshenv` (the macOS keychain is locked for non-GUI
shells, so token-via-keychain doesn't work for the daemon's `Popen`).
Per-project Mattermost channel routing lives in
`<project>/.cc-autopilot/env`.

## Convergence model

The daemon is intentionally not transactional across ticks. Every tick
is idempotent and corrective:
- Mid-task crash → `_recover_orphans` on next start, task retries.
- Pipeline died while daemon was off → next tick's
  `_sweep_pipeline_pending` notices and runs verification.
- Cron run crashed mid-run → next tick re-fires when due.
- Ideation crashed before writing state → cooldown still advances so
  the broken agent doesn't get hammered every tick.

This is why ap2 can run for weeks without operator attention.

## Reading order if you want depth

1. This file — what's on disk, what each thing means, where to look.
2. `.cc-autopilot/progress.md` (tail) — recent task outcomes in
   operator-readable prose.
3. `.cc-autopilot/events.jsonl` (tail) — the structured timeline.
4. `git log --oneline -30` — what code shipped.
5. The full ap2 docs — `ap2/README.md` and `ap2/architecture.md` in the
   ap2 source tree (https://github.com/lzhang/autopilot2) — for design
   rationale, agent kinds, MCP tool wiring, dependency graph.
