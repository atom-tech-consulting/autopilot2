# ap2 — autopilot v2

External Python daemon that drives a Claude Code project through a list of tasks. Each unit of work runs as a fresh agent dispatch through a pluggable backend — Claude Code by default, with OpenAI Codex selectable per agent kind (each backend brings its own auth) — and shared state lives on disk. The daemon never accumulates context.

## What it does

- **Picks the next Ready task** off `TASKS.md` and runs it as a task agent. The agent edits files, commits, and calls the `report_result` MCP tool. The daemon receives the structured payload and moves the task to Complete (or Backlog/Frozen on failure).
- **Auto-promotes Backlog → Ready** when Ready is empty, skipping any task with unmet `(blocked on: TB-X)` dependencies.
- **Fires ideation** when the working board (Active+Ready+Backlog) is fully empty. The ideation agent reads `goal.md` + `progress.md` + recent failures + the insights index, writes a per-cycle assessment, and proposes new Backlog tasks.
- **Runs cron jobs** from `.cc-autopilot/cron.yaml` (currently just `status-report` by default).
- **Polls Mattermost** for `@claude-bot` mentions and dispatches a handler agent per message. The MM loop (`_mm_loop`, `AP2_MM_TICK_S` = 10s) runs concurrently with the main tick loop so operator messages are handled even while a task agent is running. The handler always runs with the same fixed `MM_HANDLER_TOOLS` toolset (TB-145) — drops `cron_edit`, `ideation_state_write`, and `board_edit` (route board mutations through `operator_queue_append` instead) but keeps the operator-facing actions (queue, daemon_control, mattermost_reply, operator_log_append, status_report_run, reads).
- **Catches drift** — orphan recovery on startup, retry counter with Frozen shelving after `AP2_MAX_RETRIES`, idle watchdog that posts auto-diagnose to Mattermost when the daemon goes quiet for >3h.

## Quickstart

The landing-page quickstart lives in the [root `README.md`](../README.md#quickstart)
(install → `ap2 init` → `ap2 add --briefing-file` → `ap2 start` → `ap2 status`)
— single-sourced there so the two READMEs can't drift apart. Pause / resume /
stop:

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
| `ap2 status` | Daemon liveness, board counts, cron jobs, pending operator queue ops, pending-review TB-Ns (TB-151), the latest "Open questions for operator" from `ideation_state.md` (TB-173), and currently-active attention conditions (TB-298 — CLI-pull sibling of the TB-282 status-report cron post and the TB-296 web `/attention` page; all three share `attention.detect_attention_conditions(cfg)` so the surfaces can't disagree). `--json`. |
| `ap2 logs -n 40` | Tail recent events. `--json`. |
| `ap2 add "<title>"` | Append a task. `-s Ready\|Backlog\|Frozen` (default: `Backlog` — TB-167; operator-filed tasks land in triage alongside ideation proposals and the daemon auto-promotes when capacity opens), `-t #tag`, `-d <desc>`, `--briefing-file <path>`, `--no-verify`. |
| `ap2 backlog <TB-N>` | Move any task to Backlog. |
| `ap2 unfreeze <TB-N>` | Un-freeze + reset retry counter. Refuses if not in Frozen. |
| `ap2 rewind-focus TITLE [--reason TEXT]` | Re-engage an exhausted `## Current focus:` heading (TB-295). Atomically updates `focus_pointer.json`, emits synthetic `focus_advanced trigger=operator_rewind` so the empty-cycles counter respects the rewind, logs to operator_log.md. Title-as-key, resolved to index at drain time. |
| `ap2 delete <TB-N>` | Permanently remove a task. Refuses Active/Ready without `--force`. Emits `task_deleted`. |
| `ap2 reject <TB-N> [--reason "..."]` | Reject an ideation-proposed task (Backlog + `@blocked:review` only) AND capture the rejection reason in `.cc-autopilot/operator_log.md` so ideation Step 0 stops re-proposing it (TB-152). Drops the row + briefing same as `delete`. For non-proposals use `ap2 delete`. |
| `ap2 ideate [--force]` | Manually trigger an ideation cycle (TB-159). Bypasses the cooldown / `AP2_IDEATION_DISABLED` / non-empty-Ready-or-Backlog gates. TB-194: queues regardless of board state — the prior Active-task refusal was guarding a race the loop topology already prevents (drain runs before task dispatch with Active cleared by the previous tick). `--force` is now a no-op for routing (audit-only metadata on the queue payload). Forced runs still call `mark_run`, so the next natural cooldown resets. |
| `ap2 update <TB-N> [--title ...] [--tags ...] [--description ...] [--blocked ...] [--briefing-file ...]` | In-place edit of a queued task's title / tags / description / blocked codespan / briefing (TB-153). Briefing path is slug-stable so git history of the briefing file stays contiguous. Hard-refused on tasks in Active or Pipeline Pending. |
| `ap2 pause --reason "..."` | Set the pause flag (daemon stops dispatching, stays running). |
| `ap2 resume` | Clear the pause flag. |
| `ap2 cron list` | List cron jobs + last-fired timestamps. |
| `ap2 ack [-t TB-N] "<note>"` | Append an operator-decision line to `.cc-autopilot/operator_log.md` (TB-106). Ideation reads this and won't re-propose actions you've logged. |
| `ap2 web` | Start a local read-only web UI standalone (default `127.0.0.1:7820`). Used when the daemon is not running and you just want to browse past events; `ap2 start` already spawns the UI in the daemon process on port 8729 (configurable via `AP2_WEB_PORT`, opt out with `AP2_WEB_DISABLED=1`). Routes: `/`, `/events`, `/tasks`, `/task/<TB-N>`, `/task-run/<run-id>`, `/pipelines`, `/insights`, `/insight/<name>`, `/ideation_state`, `/commits`. Full event payloads (no truncation). |
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
| `ap2 sandbox sync-assets [user] [--sbuser] [--apply] [--dest DIR]` | Deploy BOTH `<repo>/skills/*` and `ap2/howto.md` into a target `~/.claude/` (TB-276 unified the prior `sync-skills` + `install-howto` verbs). Default mode `sudo`s as the positional user; `--sbuser` writes to the current user's `$HOME` without sudo. Default is dry-run; `--apply` to copy. |
| `ap2 sandbox install-mm [user]` | Write `MATTERMOST_URL` + `MATTERMOST_TOKEN` to `~user/.zshenv`. |
| `ap2 sandbox install-channel <project> <channel>` | Resolve `#channel` → ID, write to `<project>/.cc-autopilot/env`. |
| `ap2 sandbox project-setup <source>` | Clone the source repo into `~user/repos/`. `--mm-channel <name>` resolves+wires in one shot. |
| `ap2 sandbox project-audit <path>` | Verify a sandbox clone is correctly isolated. |

[`sandboxed-user-setup.md`](../sandboxed-user-setup.md) (at the repo root) is
the runbook.

## Configuration

### Environment variables

All `AP2_*` variables can be set in shell, in `<project>/.cc-autopilot/env` (KEY=VAL, project-scoped, shell wins on conflict), or in `~user/.zshenv` for the sandbox user.

| Variable | Default | Controls |
|---|---|---|
| `AP2_TICK_S` | `30` | Main tick interval for scheduled work (cron, pipeline sweep, task dispatch, ideation). |
| `AP2_MM_TICK_S` | `10` | Mattermost polling interval (s). Runs in a separate concurrent loop so operator messages are handled promptly even during long-running tasks. |
| `AP2_WEB_PORT` | `8729` | Port for the bundled read-only web UI that `ap2 start` spawns alongside the daemon. Bound to `127.0.0.1` only. Standalone `ap2 web` keeps its own default of `7820`. |
| `AP2_WEB_DISABLED` | (unset) | Set `1`/`true`/`yes` to skip spawning the bundled web UI when the daemon starts (headless / CI). The standalone `ap2 web` command is unaffected. |
| `AP2_TASK_TIMEOUT_S` | `1200` | Per-task SDK query timeout (s). |
| `AP2_TASK_MAX_TURNS` | `200` | Max turns per task agent. |
| `AP2_CONTROL_TIMEOUT_S` | `1200` | Per-control-agent SDK query timeout (s). |
| `AP2_MAX_RETRIES` | `3` | Failed-task retries before Frozen. |
| `AP2_VERIFY_CMD` | (unset) | Project-wide regression gate (e.g. `uv run pytest -q`). Runs after every successful task agent commit. |
| `AP2_VERIFY_TIMEOUT_S` | `600` | `AP2_VERIFY_CMD` timeout (s). |
| `AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S` | `10800` (3h) | Idle time before the watchdog posts auto-diagnose. |
| `AP2_AUTO_DIAGNOSE_COOLDOWN_S` | `21600` (6h) | Cooldown between auto-diagnose fires. |
| `AP2_IDEATION_DISABLED` | (unset) | Set `1`/`true`/`yes` to disable empty-board ideation. |
| `AP2_IDEATION_COOLDOWN_S` | `7200` (2h) | Cooldown between ideation fires. |
| `AP2_IDEATION_TRIGGER_TASK_COUNT` | `3` | Fire ideation when the Ready+Backlog count is BELOW this threshold (Active is still a hard gate — concurrent task-agent + control-agent SDK runs are not allowed) (TB-160). Set to `1` for the legacy "fire only when the working queue is fully empty" behavior; raise it (e.g. `5`) for projects with very fluid scope. Invalid (non-int, non-positive) values fall back to the default. |
| `AP2_IDEATION_MAX_TURNS` | `100` | Max turns per ideation run. |
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

**Lifecycle.** `daemon_start`, `daemon_stop`, `daemon_pause`, `daemon_resume`, `task_start`, `task_complete`, `cron_start`, `cron_complete`, `ideation_empty_board`, `ideation_complete`, `ideation_cycle_summary` (TB-300 — agent-emitted exit marker for no-proposal cycles, paired with the daemon's `ideation_empty_board` entry marker so the empty-cycles counter recognizes both `_complete` and `_cycle_summary` exits), `focus_advanced` (TB-226), `roadmap_complete` (TB-226 / TB-275), `web_start`, `web_stop`.

**Failure.** `task_error`, `task_timeout`, `verification_failed` (per-task or project-wide gate), `verification_partial`, `retry_exhausted`, `cron_error`, `cron_timeout`, `ideation_error`, `ideation_timeout`, `mattermost_error`, `mattermost_timeout`, `mm_poll_error`, `state_commit_error`, `auto_diagnose_post_error`, `web_error`.

**State / observability.** `task_implicit_commit` (HEAD-salvage on crash), `task_unfrozen`, `task_updated` (TB-153), `task_run_usage` (TB-165 — per-run token usage + cost emitted on every task-agent terminal path; stream/messages dumps are also retained on success post-TB-165), `control_run_usage` (TB-166 — same shape for ideation / cron / mattermost-handler runs, with a `label` field naming the run kind; control-agent stream/messages dumps land in `.cc-autopilot/debug/` alongside the prompt), `backlog_auto_promoted`, `cron_bootstrap`, `cron_proposed`, `cron_proposal_rejected`, `cron_proposal_error`, `ideation_state_updated`, `ideation_state_scrubbed` (TB-284 — periodic scrub of stale `## Open questions for operator` / `## Decisions needed from operator` items), `ideation_state_scrub_error` (TB-294 — fail-audit when the scrubber's write path errors), `ideation_skipped` (TB-174 — natural ideation cron self-skipped without invoking the SDK; carries a `reason` field, currently `focus_exhausted` when every `## Current focus assessment` item self-reported `Status: exhausted-needs-operator`, or `roadmap_complete` post-TB-275 when the pointer is past the last focus), `attention_raised` (TB-282 — daemon-emitted per fresh `AttentionCondition` from `attention.detect_attention_conditions(cfg)`), `attention_pushed` / `attention_push_error` / `attention_push_no_destination` (TB-297 — opt-in immediate Mattermost push companion to `attention_raised`), `pipeline_start`, `orphan_recovery`, `board_malformed_line`, `mattermost`, `auto_diagnose_fired`, `auto_diagnose_no_destination`.

`diagnose.MEANINGFUL_EVENT_TYPES` is the set the watchdog treats as "the daemon making progress"; `diagnose.FAILURE_EVENT_TYPES` is what it counts as broken. Both are in `ap2/diagnose.py` if you need to filter.

## Custom MCP tools

The daemon registers an `autopilot` MCP server with two pools of tools, partitioned by who can call them:

**Control agents** (mattermost handler, cron jobs, ideation): `board_edit`, `mattermost_reply`, `log_event`, `daemon_control`, `ideation_state_write`, `git_log_grep`, `operator_log_append`, `operator_queue_append`, `status_report_run`. Broad reads, narrow writes — every mutation goes through a single-purpose MCP tool, no `Write`/`Edit` access. Note: `cron_edit` is NOT in any agent toolset (TB-146); cron schedule mutation is operator-CLI-only via `ap2 cron edit`. Task agents emit `cron_proposed` events via `cron_propose` for operator review, and ideation surfaces unadopted proposals in its per-cycle assessment but cannot adopt them itself.

Ideation runs with a narrower toolset (`IDEATION_TOOLS`; TB-291) — `CONTROL_AGENT_TOOLS` minus `operator_queue_append`. The TOCTOU defense the queue path provides is unnecessary during ideation, which only fires when Active == 0; fencing ideation off `operator_queue_append` keeps the proposal-path event vocabulary 1:1 with `ideation_proposal_recorded` (the empty-cycles counter's reset signal).

The Mattermost handler always runs with `MM_HANDLER_TOOLS` (TB-145, replacing the FULL/RESTRICTED toggle TB-122 introduced) — `CONTROL_AGENT_TOOLS` minus `ideation_state_write` and `board_edit` (and minus `cron_edit`, which TB-146 already removed from `CONTROL_AGENT_TOOLS` itself). This keeps the operator's pause / add / delete / approve / freeze / ack channel open (board mutations route through `operator_queue_append`, which the daemon drains between tick stages) while preventing cron-schedule, ideation-state, and direct TASKS.md mutations that would race a running task agent's snapshot window. Cron and ideation-state edits go through the operator CLI (`ap2 cron list/edit`, manual `ideation_state.md` edit) when the daemon is idle. The `mattermost` event in `events.jsonl` always records `toolset: "restricted"`.

**Task agents**: `pipeline_task_start(name, command)` for long work (>~5 min wall-clock — data fetches, parameter sweeps, ML training). Launches `command` as a detached subprocess. The launching task moves to a `Pipeline Pending` board section (TB-115); the daemon's per-tick sweep re-runs the briefing's `## Verification` once every spawned pid dies and routes to Complete or Backlog/Frozen.

Task agents otherwise have `Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write` (project-scoped) — they edit code, commit, and exit.

## Tests

Two tiers, run independently:

```bash
# Default suite: fast, no API cost. Run on every change.
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
- [`sandboxed-user-setup.md`](../sandboxed-user-setup.md) — runbook for the `claude-agent` sandbox user.
- [`ap2/ideation.default.md`](ideation.default.md) — the load-bearing ideation prompt body.
