---
name: ap2-board-ops
description: "Use when operating an ap2 board — running an `ap2 <verb>` operator-CLI command (start/stop/status/add/approve/unfreeze/rollback/cron/sandbox/…), or reaching for an `autopilot` custom MCP tool (`report_result`, `board_edit`, `operator_queue_append`, `cron_propose`, …) from an agent toolset."
---

# ap2 board operations — operator CLI verbs & custom MCP tools

The operator-action reference for an ap2 daemon: the two surfaces that
move tasks across the board. An operator should never have to grep
`ap2/howto.md` to learn what an `ap2 <verb>` does or which MCP tool an
agent reaches for. Two self-contained surfaces:

- **Custom MCP tools** — the `autopilot` MCP server's tool catalogue,
  partitioned by agent toolset (task agents, control agents, the
  Mattermost handler, operator-only), with the one-line "what it does"
  description per tool.
- **Operator CLI verbs** — the full `ap2 <verb>` subcommand table the
  operator drives from the host shell, with the WHY / when-to-use
  companion the `--help` text doesn't carry.

Two docs-drift gates in `ap2/tests/test_docs_drift.py` keep this skill in
lock-step with the source: `test_every_cli_verb_documented` (every
non-suppressed `ap2 <verb>` in `ap2/cli.py`'s `build_parser()` is
mentioned here) and `test_every_mcp_tool_documented` (every MCP tool in
`CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` / `MM_HANDLER_TOOLS` is
mentioned here OR in `ap2/architecture.md`'s literal enumeration). A
source-side verb / tool addition that skips this skill trips one of those
gates until docs catch up.

## Board-section model

`TASKS.md` is the daemon-owned board the verbs and tools below mutate. It
has a fixed 5-section order — **Active → Ready → Backlog → Complete →
Frozen** — plus a transient **Pipeline Pending** section for tasks parked
while a detached `pipeline_task_start` subprocess runs (TB-115). What each
section means:

- **Ready** — dispatchable now; the daemon picks the next Ready task each
  tick (~30s) and moves it to Active.
- **Backlog** — queued; auto-promoted to Active when dispatchable (blockers
  satisfied, not gated by an `@blocked:review` codespan).
- **Active** — the one task currently running inside `await sdk.query(...)`
  (the daemon dispatches one at a time).
- **Pipeline Pending** — a task that reported complete while its detached
  pipeline(s) are still alive; the daemon re-runs its `## Verification`
  once they die, then routes pass → Complete / fail → Backlog.
- **Complete** — verified and done.
- **Frozen** — retry-exhausted; needs `ap2 unfreeze` (back to Backlog,
  retry counter cleared) or `ap2 delete` (permanent removal).

## Custom MCP tools (reference)

The daemon registers the `autopilot` MCP server. Two pools, partitioned
by allowlist:

**Task agents** (`TASK_AGENT_TOOLS`):
- `report_result(status, commit, summary, files_changed, tests_passed)` —
  the completion signal. TB-123 dropped the legacy `cron` argument;
  scheduling proposals now go through the dedicated `cron_propose` tool.
- `pipeline_task_start(name, command)` (TB-115) — detach long-running
  work (sweeps, data fetches, ML training); the daemon parks the
  launching task in Pipeline Pending until the pid dies.
- `cron_propose(name, schedule, prompt, rationale)` (TB-123) — emit a
  `cron_proposed` event for operator review. Does NOT mutate `cron.yaml`.
- Plus regular `Read`/`Glob`/`Grep`/`Bash`/`Edit`/`Write` (with the
  fenced paths blocked).

**Control agents** (cron, ideation, mattermost handler) —
`CONTROL_AGENT_TOOLS`. Read project state via `Read` / `Glob` / `Grep`;
mutate via narrow MCP tools. **No Bash** (TB-109 — closed the
shell-redirect-into-fenced-file corruption surface).
- `board_edit(action, task_id, title, tags, briefing, description, blocked_on)` — add/move/remove tasks
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
- `operator_queue_append(op, ...)` (TB-131) — queue a board op (add /
  move / unfreeze / delete / approve / update_goal / ideate); the
  daemon drains the queue between ticks so in-flight task windows
  never observe the mutation mid-run. The MM handler uses this in
  place of `board_edit` (TB-145).
- `status_report_run(channel, force)` (TB-144) — fire the
  status-report routine on demand (the same routine the cron job
  invokes). The MM handler exposes it for `@claude-bot status`. TB-281
  added a content-fingerprint dedup gate (`cron_skipped
  reason=duplicate_content`) so a chat-triggered post that would be
  structurally identical to the last cron / chat post is suppressed
  with an audit-event marker instead of re-firing the SDK; the gate
  is shared with the cron tick (idle check + fingerprint compare both
  honored on every entry).

**Mattermost handler only** (`MM_HANDLER_TOOLS` =
`CONTROL_AGENT_TOOLS` minus `ideation_state_write` + `board_edit`,
plus one handler-specific tool — TB-145, TB-149):
- `mattermost_thread_read(channel, thread_id, limit)` — fetch prior
  messages in the current thread for context. Not in
  `CONTROL_AGENT_TOOLS` because cron and ideation have no thread to
  read.

Operator-only (NOT in any agent toolset, TB-146):
- `cron_edit(action, name, interval, prompt, active_when, max_turns)` —
  manage scheduled jobs. The `do_cron_edit` handler is invoked by the
  operator CLI (`ap2 cron edit ...`) and unit tests, never by an agent.
  Task agents emit `cron_proposed` events via `cron_propose` for
  operator review; ideation surfaces unadopted proposals in its
  per-cycle assessment but cannot adopt them itself.

## Operator CLI verbs (reference)

Subcommands of `ap2` invoked by the operator from the host shell — distinct
from MCP tools (agent-internal, dispatched by the SDK through the
`autopilot` MCP server; see `## Custom MCP tools (reference)` above) and
from chat verbs (`@claude-bot <verb>` in Mattermost, which the handler
agent routes through `operator_queue_append` so the mutation lands at the
next tick boundary). The full `ap2 <verb> --help` text is the short-form
reference; this table is the WHY / when-to-use companion. Subcommand
groups (`ap2 cron`, `ap2 sandbox`) get one row per nested sub-verb.

The `test_every_cli_verb_documented` gate in `ap2/tests/test_docs_drift.py`
walks `ap2/cli.py`'s `build_parser()` and fails CI if a new non-suppressed
subcommand ships without a row below. Hidden / dev-only subparsers
(declared `help=argparse.SUPPRESS`, e.g. `ap2 _run`) are deliberately
excluded from both the table and the gate — the daemon spawns them,
never the operator.

| verb | purpose | notes |
|---|---|---|
| `ap2 start [--foreground]` | Boot the daemon for a project (backgrounded by default). | Pre-flight refuses if `CLAUDE_CODE_OAUTH_TOKEN` isn't in env (TB-79); `--foreground` is the debugging hook when `daemon.log` doesn't show why the loop died. |
| `ap2 stop [-f]` | Politely shut the daemon down (SIGTERM; `-f` escalates to SIGKILL). | The clean stop drains the operator queue before exiting, so an `ap2 update` queued just before `ap2 stop` doesn't get lost. |
| `ap2 status [--json]` | One-screen snapshot — daemon pid, board section counts, cron jobs, decisions-needed nudges. | The "first thing to run" verb at the top of every operator session; pair `--json` with `jq` for tooling. TB-319 appends a `## Components` block listing every component the registry discovered (text-mode) and a top-level `components` list in `--json` — see `ap2/howto.md`'s `## Components enumeration (`ap2 status`)` for the on/off polarity rules. |
| `ap2 init` | Idempotent scaffold of `.gitignore` + `.cc-autopilot/tasks/` skeleton in a fresh project. | Run once when bringing a repo under ap2; no-op if the structure already exists. |
| `ap2 doctor [--user U]` | Sanity-check that the project is ready to boot — skeleton present, sandbox user installed, OAuth token reachable. | Run before `ap2 start` on an unfamiliar machine to diagnose the "daemon won't start" silent-fail modes (TB-79's token-missing path is the most common hit). |
| `ap2 check [--json]` | Validate on-disk state-file integrity — TASKS.md shape, briefing-link resolution, cron.yaml schema, JSON state parseability, insights front matter (TB-108). | Exits 1 on errors; warnings (stale brief links, missing goal.md) don't fail. Run after any manual edit to a fenced file. |
| `ap2 logs [-n N] [--json] [--follow/-f] [--all]` | One-shot tail of `events.jsonl` with column truncation for human reading; `--follow`/`-f` switches to a live tail filtered to the operator-interest allowlist (compact one-line format), `--all` disables that filter, `--json` emits raw kept lines (TB-352, folded in the former `scripts/monitor_events.py`). | Faster than `tail \| jq` for the common "what just happened?" question; default trims fields to 120 chars and `--json` gives full payloads. `--follow` starts at end-of-file (ignores `-n`, like `tail -F -n 0`) so it survives daemon log rotation — reach for it to watch an active arc unfold live. |
| `ap2 backlog TB-N` | Move a task into Backlog from any section (last-ditch reset without retry-counter exhaustion). | Use when a stuck Active task needs to step back without burning retries; for permanent removal use `ap2 delete` instead. |
| `ap2 add --briefing-file PATH [-s SECTION] [-t TAGS...] [--no-verify] [--blocked CSV] [--skip-goal-alignment]` | Add a new operator-filed task with a real briefing the per-task verifier can read (TB-135). | `--briefing-file` is required because verification needs a `## Verification` section; pass `-` for stdin. `--skip-goal-alignment` (TB-170) bypasses the TB-161 goal-cite and TB-164 Why-now checks for legitimately-meta work (dep bumps, doc fixes). |
| `ap2 update TB-N [--title T] [--tags CSV] [--blocked CSV] [--description D] [--clear-tags] [--clear-blocked] [--briefing-file PATH] [--force] [--skip-goal-alignment]` | In-place edit a task's board-line fields and/or its briefing file (TB-153). | Routes through the operator queue so the mutation lands at a tick boundary, never mid-task-run; omitted flag = field unchanged. `--force` lets board-line edits land on Active / Pipeline Pending tasks (briefing edits stay hard-refused). |
| `ap2 delete TB-N [-f]` | Permanently remove a task from the board (row + briefing file) — emits `task_deleted` for audit. | Refuses Active/Ready without `--force`. Use `ap2 reject` instead for ideation proposals still gated by `@blocked:review`, so the rejection reason feeds ideation Step 0's "don't re-propose" learning. |
| `ap2 reject TB-N [--reason TEXT]` | Reject an ideation-proposed Backlog task (TB-152): drops the row + briefing AND logs the reason. | Writes `rejected ideation proposal → TB-N (<title>): <reason>` to `operator_log.md`; the reason becomes a learnable signal for the next ideation cycle, and `(no reason given)` is itself a (weak) signal. |
| `ap2 classify TB-N --impact VERDICT [--reason TEXT]` | Record the operator's retrospective impact verdict (`advanced-goal` / `pro-forma` / `negative` / `unclear`) on a shipped proposal (TB-189 / TB-251). | Captures whether the task substantively moved the goal forward, merely satisfied validators (goal.md L66-76's failure mode), or actively regressed the codebase; reasons feed TB-188 per-proposal records and `operator_log.md` so future ideation cycles can learn which proposal shapes actually pay off (and which to strongly avoid). See `ap2/howto.md`'s `## Classify verdicts` for the `pro-forma` vs `negative` distinction. |
| `ap2 audit [--interactive] [--json] [--since ISO] [--frozen-only \| --auto-approved-only]` | Retrospective walk through unreviewed Complete + Frozen tasks since the last `ap2 audit` cursor (TB-248). | The "I just came back from a week away" verb under `AP2_AUTO_APPROVE=1` — closes the retrospective review surface gap auto-approve opens. State derivation is grep over `operator_log.md` (no new state file); `--interactive` walks one task at a time with `[c]lassify / [s]kip / [n]ext / [q]uit` prompts. See `ap2/howto.md`'s `## Retrospective audit workflow`. TB-258 wires the unreviewed-count onto the natural-cadence return surfaces: `ap2 status` carries an `audit: N unreviewed since <ts>` line (text, omitted when N=0) + an always-present `audit` block in `--json`; the status-report Mattermost cron post carries a `*Retrospective audit (unreviewed shipped):*` sub-block (omitted when N=0). Walk-away operators see the count without running `ap2 audit` first. |
| `ap2 ack NOTE [-t TB-N]` | Record an out-of-band operator decision in `operator_log.md` so ideation stops re-proposing actions whose effects aren't filesystem-visible (TB-106). | Use for "I already decided X out-of-band" announcements and for clearing decisions-needed nudges the daemon keeps surfacing. |
| `ap2 approve TB-N` | Approve an ideation-proposed task (TB-121) — strips its `@blocked:review` codespan so the next tick auto-promotes it out of Backlog. | The thumbs-up half of the `approve` / `reject` pair on freshly-ideated proposals; refuses if the task isn't on the board at all. |
| `ap2 unfreeze TB-N` | Move a Frozen task back to Backlog and clear its retry counter. | Run after fixing the underlying blocker (flaky test, missing dep); refuses if the task isn't currently Frozen so you can't accidentally reset a healthy task. |
| `ap2 ideate [--force]` | Manually trigger an ideation pass (TB-159), bypassing the natural empty-board / cooldown / `AP2_IDEATION_DISABLED` gates. | Routed through the operator queue; the daemon runs ideation on its next tick (≤30s). Use to refill a thin Ready/Backlog when waiting on cooldown is impractical; the cooldown clock still advances after the forced run. |
| `ap2 update-goal --file PATH [--reason TEXT]` | Refresh `goal.md` via the operator queue (TB-193) — full-file replacement applied at the next tick under `board_file_lock`. | Symmetric to `ap2 add --briefing-file`; operator-CLI-only by design — the MM handler has no path to mutate `goal.md`. The `--reason` line feeds future ideation cycles as a goal-drift signal. TB-342 wired this verb to also reset the ideation-exhaustion halt: a `goal_updated` event clears `roadmap_complete_emitted` so editing goal.md is now the resume path (the pre-TB-342 `ap2 rewind-focus` verb went away with the multi-focus rotation collapse). |
| `ap2 rollback [-n N \| --task TB-N \| --to SHA] [-y] [--force]` | Linear rollback (TB-111): walk back from HEAD by N tasks (or to a specific TB-N / sha) and `git reset --hard`. | Restores TASKS.md + every committed state file coherently in one shot. Refuses a dirty working tree by default; use when a sequence of recent task-completions needs to be undone together rather than one-by-one. |
| `ap2 backfill-proposals [--dry-run]` | Backfill historical ideation proposal records (TB-195) for every ideation-authored TB-N that lacks one. | Scans `operator_log.md` + briefing files + `events.jsonl` and writes per-proposal records. Idempotent; safe to re-run. Operator-driven one-off, NOT routed through the operator queue or daemon ticks. |
| `ap2 pause [--reason TEXT]` | Pause the daemon by setting a flag file — in-flight tasks finish but no new ones dispatch. | Use for short maintenance windows; pair with `ap2 resume` to re-enable. The reason is recorded in events for the operator audit trail. |
| `ap2 resume` | Clear the pause flag set by `ap2 pause`; the daemon picks up on its next tick (≤30s). | Symmetric pair to `ap2 pause`; no-op if the daemon isn't paused. |
| `ap2 web [--host H] [--port P]` | Start the read-only HTTP UI at `127.0.0.1:7820` with `/events`, `/tasks`, `/task/<TB-N>`, `/pipelines`, `/insights`, `/ideation_state`, `/commits` pages. | Useful when scanning visually beats asking the session for a summary; the daemon also spawns this automatically on `ap2 start` unless `AP2_WEB_DISABLED` is set. |
| `ap2 cron list` | List the cron jobs registered in `cron.yaml` with their next-fire timestamps. | The diagnostic for "why isn't the X routine firing?" — pair with `tail .cc-autopilot/cron_state.json` to confirm the last-fire timestamp. |
| `ap2 cron edit ACTION NAME [--interval I] [--prompt P] [--active-when E] [--max-turns N]` | Add / remove / update a cron job in `cron.yaml`. | Operator-CLI-only since TB-146 retired the agent-side `cron_edit` tool; the TB-202 refuse-if-active gate prevents a mid-task invocation from racing the fenced cron.yaml write against the task agent's snapshot window. |
| `ap2 config list [--json]` | Enumerate every known config key with its current value + source (`file` / `env-override` / `default`). | The operator's introspection surface for the structured-config focus (TB-324, axis 4). Walks `aggregate_schemas(default_registry())` + the `FLAT_TO_SECTIONED` core contract surface; the source column tells you whether a value came from `.cc-autopilot/config.toml`, an `AP2_*` env override, or the in-source default. Use `--json` for scripting (the JSON form carries `type` + `hot_reloadable` per row alongside the four text-mode columns). |
| `ap2 config get PATH` | Print the current value at PATH. | Single-key lookup; non-zero exit with a did-you-mean suggestion on an unknown path (e.g. typo'd `components.janior.disabled`). |
| `ap2 config set PATH VALUE` | Queue a `config_set` op for the daemon to drain at the next tick — writes VALUE to PATH in config.toml. | Operator-CLI-only by design (TB-324 Out-of-scope — no MCP exposure to task agents). Validates PATH against the schema and coerces VALUE against the declared type (`bool` knob set to `"1"` lands as `true`). Routed through the operator queue so the write lands under `board_file_lock`, never inside a task agent's snapshot window. The next `env_reload` tick picks up the file's new mtime and propagates the value to hot-reloadable `Config` fields. |
| `ap2 config validate` | Dry-run schema check — load `.cc-autopilot/config.toml` + env overrides and run the same `validate_config` the daemon runs at startup. | Exits 0 on pass, non-zero with the validator's named-path error on a typed mismatch (e.g. `[components.janitor] disabled = "yes": expected bool, got str`). Useful pre-flight before `ap2 start` after hand-editing config.toml. |
| `ap2 sandbox user-audit [USER]` | Verify the sandbox user has no creds beyond `CLAUDE_CODE_OAUTH_TOKEN` (and optional Mattermost env). | The pre-flight before letting the daemon run code as that user — the sandbox model only holds if the user can't reach the human's `~/.ssh`, keychain, or other repos. |
| `ap2 sandbox user-setup [USER] [-y] [--skip-token] [--skip-statusline] [--mm-url/--mm-token]` | Create the sandbox user (prompts before running sudo). | One-time per machine; pairs with `install-token` / `sync-assets` / `install-mm` to fill in creds + per-user config (TB-276 folded the prior `install-howto` step into `sync-assets`). Skip flags exist for partial setups. |
| `ap2 sandbox install-token [USER] [--token-env VAR]` | Install `CLAUDE_CODE_OAUTH_TOKEN` into `~<user>/.zshenv`. | Run after `claude setup-token`; the daemon refuses to start without the token in its env (TB-79), and the macOS keychain is locked for non-GUI shells so token-via-keychain doesn't work. |
| `ap2 sandbox install-statusline [USER]` | Copy `hooks/statusline-command.sh` into `~<user>/.claude/` and wire it into the per-user `settings.json`. | Convenience for matching the human's statusline customization on the sandbox user; purely cosmetic for the daemon itself. |
| `ap2 sandbox install-mm [USER] [--mm-url/--mm-token]` | Install `MATTERMOST_URL` + `MATTERMOST_TOKEN` into `~<user>/.zshenv`. | Optional — only needed if the project wants the daemon's Mattermost loop active (poll mentions, post status reports, route `@claude-bot` chat verbs). |
| `ap2 sandbox project-setup SOURCE [--user U] [-y] [--mm-channel N] [--git-name N] [--git-email E]` | Clone `<source>` into `~<user>/repos/` with repo-local git identity set. | The "transfer this project to the sandbox" verb; pair with `--mm-channel` to wire the per-project channel routing in one step, or fall back to `install-channel` after the fact. |
| `ap2 sandbox install-channel PROJECT CHANNEL [--user U]` | Resolve a Mattermost channel name to an ID and write `AP2_MM_CHANNELS` into `<project>/.cc-autopilot/env`. | Run after `project-setup` if you skipped `--mm-channel` then; idempotent overwrite. |
| `ap2 sandbox project-audit PATH [--user U]` | Verify an isolated project clone is well-formed — ownership, git identity, env file. | The diagnostic for "did `project-setup` finish correctly?" — catches half-completed setups before they confuse `ap2 doctor` later. |
| `ap2 sandbox sync-assets [USER] [--sbuser] [--apply] [--dest DIR]` | Deploy BOTH `<repo>/skills/*` AND `ap2/howto.md` into a target `~/.claude/` (TB-276 unified the prior `sync-skills` + `install-howto` split). | Default is a dry-run drift summary; pass `--apply` to copy. Default mode `sudo`s as a positional sandbox user; `--sbuser` writes to the CURRENT user's `$HOME/.claude/` without sudo (the path a Claude session already running as the sandbox user — which lacks sudoer privileges — takes to refresh its own assets). |
