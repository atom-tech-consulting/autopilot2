# ap2 — autopilot v2

External Python daemon that drives a Claude Code project through a list of tasks. Each unit of work runs as a fresh Claude Agent SDK `query()` call; shared state lives on disk. The daemon never accumulates context.

## What it does

- **Picks the next Ready task** off `TASKS.md` and runs it as a task agent. The agent edits files, commits, and calls the `report_result` MCP tool. The daemon receives the structured payload and moves the task to Complete (or Backlog/Frozen on failure).
- **Auto-promotes Backlog → Ready** when Ready is empty, skipping any task with unmet `(blocked on: TB-X)` dependencies.
- **Fires ideation** when the working board (Active+Ready+Backlog) is fully empty. The ideation agent reads `goal.md` + `progress.md` + recent failures + the insights index, writes a per-cycle assessment, and proposes new Backlog tasks.
- **Runs cron jobs** from `.cc-autopilot/cron.yaml` (currently just `status-report` by default).
- **Polls Mattermost** for `@claude-bot` mentions and dispatches a handler agent per message.
- **Catches drift** — orphan recovery on startup, retry counter with Frozen shelving after `AP2_MAX_RETRIES`, idle watchdog that posts auto-diagnose to Mattermost when the daemon goes quiet for >3h.

## Quickstart

```bash
# 1. Initialize a project (idempotent — safe to re-run)
cd /path/to/your/repo
ap2 init

# 2. Add a task
ap2 add "Refactor the foo helper" -s Backlog -d "Pull out the inline string parsing"

# 3. Start the daemon (backgrounded)
ap2 start

# 4. Watch it work
ap2 status
ap2 logs -n 20
```

Stop / pause / resume:

```bash
ap2 pause --reason "AFK"
ap2 resume
ap2 stop
```

## Project layout

After `ap2 init`, your repo gains:

```
TASKS.md                       # 5-section board, daemon-owned
.cc-autopilot/
├── progress.md                # append-only session log
├── events.jsonl               # structured event stream (tail with `ap2 logs`)
├── cron.yaml                  # scheduled-job registry
├── cron_state.json            # last-fired timestamps per cron
├── retry_state.json           # per-task retry counts
├── mm_state.json              # mattermost cursor + thread-mention cache
├── auto_diagnose_state.json   # watchdog cooldown
├── pid                        # daemon process id (when running)
├── paused                     # presence-only: pause flag
├── env                        # KEY=VAL project-scoped overrides
├── tasks/                     # per-TB-N briefings (Goal/Scope/Verification)
├── insights/                  # project-output knowledge files (+ auto-index)
├── pipelines/                 # detached-pipeline logs (PID-named)
└── debug/                     # per-run prompt + stream + messages dumps
```

`TASKS.md`, `.cc-autopilot/progress.md`, `CLAUDE.md`, `.cc-autopilot/ideation_state.md`, `.cc-autopilot/tasks/`, and `.cc-autopilot/insights/` are committed by the daemon as state-file commits with subject `state: …`.

## CLI reference

| Command | Purpose |
|---|---|
| `ap2 init` | Scaffold project skeleton (idempotent). |
| `ap2 doctor` | One-shot environment-readiness check (sandbox user, OAuth token, project clone, CLI presence). |
| `ap2 check` | One-shot data-integrity check — `TASKS.md` shape, briefing-link resolution, `cron.yaml` schema, JSON state parseability, insights front matter. `--json` for machine-readable. Exit 1 on errors, warnings don't fail. |
| `ap2 start` | Start the daemon backgrounded. `--foreground` runs in-shell. |
| `ap2 stop` | SIGTERM the daemon. `-f` for SIGKILL. |
| `ap2 status` | Daemon liveness, board counts, cron jobs, next task. `--json`. |
| `ap2 logs -n 40` | Tail recent events. `--json`. |
| `ap2 add "<title>"` | Append a task. `-s Ready\|Backlog\|Frozen`, `-t #tag`, `-d <desc>`, `--briefing-file <path>`, `--no-verify`. |
| `ap2 backlog <TB-N>` | Move any task to Backlog. |
| `ap2 unfreeze <TB-N>` | Un-freeze + reset retry counter. Refuses if not in Frozen. |
| `ap2 delete <TB-N>` | Permanently remove a task. Refuses Active/Ready without `--force`. Emits `task_deleted`. |
| `ap2 pause --reason "..."` | Set the pause flag (daemon stops dispatching, stays running). |
| `ap2 resume` | Clear the pause flag. |
| `ap2 cron list` | List cron jobs + last-fired timestamps. |
| `ap2 ack [-t TB-N] "<note>"` | Append an operator-decision line to `.cc-autopilot/operator_log.md` (TB-106). Ideation reads this and won't re-propose actions you've logged. |
| `ap2 web` | Start a local read-only web UI (default `127.0.0.1:7820`). Routes: `/`, `/events`, `/tasks`, `/task/<TB-N>`, `/pipelines`, `/insights`, `/insight/<name>`, `/ideation_state`, `/commits`. Full event payloads (no truncation). |
| `ap2 sandbox …` | OS-level sandbox-user + project-clone helpers (see below). |
| `ap2 --version` | Print installed `autopilot2` version. |

`ap2 --project /abs/path …` runs against any project root; default is `cwd`.

### Sandbox subcommands

The daemon is designed to run as a separate OS user (`claude-agent` by default) so the SDK's tool calls can't reach the human's home, keychain, or git config. `ap2 sandbox` automates that setup.

| Command | Purpose |
|---|---|
| `ap2 sandbox user-setup [user]` | Create the sandbox user (prompts before sudo). `--skip-token`, `--skip-statusline`, `--mm-url-env`, `--mm-token-env`. |
| `ap2 sandbox user-audit [user]` | Verify the user exists and has no creds. |
| `ap2 sandbox install-token [user]` | Write `CLAUDE_CODE_OAUTH_TOKEN` to `~user/.zshenv`. |
| `ap2 sandbox install-statusline [user]` | Copy the statusline script + wire it into `~user/.claude/settings.json`. |
| `ap2 sandbox install-howto [user]` | Copy `ap2/howto.md` to `~user/.claude/ap2-howto.md` so a Claude session running as the sandbox user can read it for context. |
| `ap2 sandbox install-mm [user]` | Write `MATTERMOST_URL` + `MATTERMOST_TOKEN` to `~user/.zshenv`. |
| `ap2 sandbox install-channel <project> <channel>` | Resolve `#channel` → ID, write to `<project>/.cc-autopilot/env`. |
| `ap2 sandbox project-setup <source>` | Clone the source repo into `~user/repos/`. `--mm-channel <name>` resolves+wires in one shot. |
| `ap2 sandbox project-audit <path>` | Verify a sandbox clone is correctly isolated. |

`plan/sandboxed-user-setup.md` is the runbook.

## Configuration

### Environment variables

All `AP2_*` variables can be set in shell, in `<project>/.cc-autopilot/env` (KEY=VAL, project-scoped, shell wins on conflict), or in `~user/.zshenv` for the sandbox user.

| Variable | Default | Controls |
|---|---|---|
| `AP2_TICK_S` | `30` | Daemon tick interval (s). |
| `AP2_TASK_TIMEOUT_S` | `1200` | Per-task SDK query timeout (s). |
| `AP2_TASK_MAX_TURNS` | `50` | Max turns per task agent. |
| `AP2_CONTROL_TIMEOUT_S` | `300` | Per-control-agent SDK query timeout (s). |
| `AP2_MAX_RETRIES` | `3` | Failed-task retries before Frozen. |
| `AP2_VERIFY_CMD` | (unset) | Project-wide regression gate (e.g. `uv run pytest -q`). Runs after every successful task agent commit. |
| `AP2_VERIFY_TIMEOUT_S` | `600` | `AP2_VERIFY_CMD` timeout (s). |
| `AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S` | `10800` (3h) | Idle time before the watchdog posts auto-diagnose. |
| `AP2_AUTO_DIAGNOSE_COOLDOWN_S` | `21600` (6h) | Cooldown between auto-diagnose fires. |
| `AP2_IDEATION_DISABLED` | (unset) | Set `1`/`true`/`yes` to disable empty-board ideation. |
| `AP2_IDEATION_COOLDOWN_S` | `7200` (2h) | Cooldown between ideation fires. |
| `AP2_IDEATION_MAX_TURNS` | `30` | Max turns per ideation run. |
| `AP2_EVENT_CONTEXT` | `50` | Number of events included in agent prompts. |
| `AP2_MM_CHANNELS` | (unset) | Comma-separated Mattermost channel IDs to poll. |
| `AP2_MM_BOT_USER_ID` | (unset) | Bot user ID (for self-message filtering). |
| `AP2_MM_MENTION` | `@claude-bot` | Mention pattern that triggers handler dispatch. |
| `AP2_MM_TEAM_ID` | (unset) | Mattermost team ID (used by sandbox install-channel). |
| `CLAUDE_CODE_OAUTH_TOKEN` | (required) | SDK auth. Daemon refuses to start without it. |
| `MATTERMOST_URL` / `MATTERMOST_TOKEN` | (optional) | Mattermost integration. |

### CLAUDE.md `## Autopilot` section

Per-project overrides. The daemon reads:

```markdown
## Autopilot
- Task list: TASKS.md
- Task briefings: .cc-autopilot/tasks/
- Progress log: .cc-autopilot/progress.md
- Next task ID: TB-99
```

`Next task ID` is auto-bumped by `ap2 add` and `do_board_edit`.

## Event schema

Events are JSONL lines in `.cc-autopilot/events.jsonl`. Every line has `ts` (UTC ISO-8601) and `type`; other fields vary. `ap2 logs` tails them.

**Lifecycle.** `daemon_start`, `daemon_stop`, `daemon_pause`, `daemon_resume`, `task_start`, `task_complete`, `cron_start`, `cron_complete`, `ideation_empty_board`, `ideation_complete`.

**Failure.** `task_error`, `task_timeout`, `verification_failed` (per-task or project-wide gate), `verification_partial`, `retry_exhausted`, `cron_error`, `cron_timeout`, `ideation_error`, `ideation_timeout`, `mattermost_error`, `mattermost_timeout`, `mm_poll_error`, `state_commit_error`, `auto_diagnose_post_error`.

**State / observability.** `task_implicit_commit` (HEAD-salvage on crash), `task_unfrozen`, `backlog_auto_promoted`, `cron_bootstrap`, `cron_proposed`, `cron_proposal_rejected`, `cron_proposal_error`, `ideation_state_updated`, `pipeline_start`, `orphan_recovery`, `board_malformed_line`, `mattermost`, `auto_diagnose_fired`, `auto_diagnose_no_destination`.

`diagnose.MEANINGFUL_EVENT_TYPES` is the set the watchdog treats as "the daemon making progress"; `diagnose.FAILURE_EVENT_TYPES` is what it counts as broken. Both are in `ap2/diagnose.py` if you need to filter.

## Custom MCP tools

The daemon registers an `autopilot` MCP server with two pools of tools, partitioned by who can call them:

**Control agents** (mattermost handler, cron jobs, ideation): `board_edit`, `cron_edit`, `mattermost_reply`, `log_event`, `daemon_control`, `ideation_state_write`. Broad reads, narrow writes — every mutation goes through a single-purpose MCP tool, no `Write`/`Edit` access.

**Task agents**: `pipeline_task_start(name, command)` for long work (>~5 min wall-clock — data fetches, parameter sweeps, ML training). Launches `command` as a detached subprocess. The launching task moves to a `Pipeline Pending` board section (TB-115); the daemon's per-tick sweep re-runs the briefing's `## Verification` once every spawned pid dies and routes to Complete or Backlog/Frozen.

Task agents otherwise have `Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write` (project-scoped) — they edit code, commit, and exit.

## Tests

Three tiers, run independently:

```bash
# Default: ~349 tests, fast, no API cost. Run on every change.
uv run pytest -q ap2/tests/

# Real-SDK smokes: opt-in via env var. ~30s + a few cents per run.
# Validates MCP tool round-trips that FakeSDK can't.
AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s
```

The default suite skips `ap2/tests/smoke/` automatically (each smoke file
has a module-level `pytest.mark.skipif(not AP2_REAL_SDK)`). Run smokes
after any change to MCP tool registration (`tools.py`), task-agent prompt
(`prompts.py`), the ideation prompt (`ideation.default.md`), or the
verifier judge (`verify._judge_prose_bullet`). See
[`architecture.md`](architecture.md#tests) for what each tier covers.

## Versions

Read the version from `pyproject.toml` via `ap2 --version`. Single source of truth.

## Further reading

- [`ap2/architecture.md`](architecture.md) — design rationale, daemon loop, agent kinds, two-tier verification, sandbox model.
- [`plan/autopilot-v2.md`](../plan/autopilot-v2.md) — original design doc (predates TB-46..TB-98; treat as history).
- [`plan/sandboxed-user-setup.md`](../plan/sandboxed-user-setup.md) — runbook for the `claude-agent` sandbox user.
- [`ap2/ideation.default.md`](ideation.default.md) — the load-bearing ideation prompt body.
