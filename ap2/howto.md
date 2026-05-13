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

## Authoring goal.md

`goal.md` is operator-curated. Ideation reads it every cycle as the source
of truth for what the project is for and when it's done. Two queue-time
validators key off its content, so both the section shape and the prose
substance are load-bearing:

- **TB-161 anchor validator** — every briefing's `## Goal` body must cite
  (as a substring) text from goal.md's `## Current focus` or `## Done when`
  headings/bullets. `_goal_md_anchors` mines anchors only from those two
  sections; reword them so meaningful citations are possible.
- **TB-164 Why-now validator** — independent of goal.md content; checks
  the briefing itself has a `Why now:` line. goal.md doesn't need its own
  Why-now section.

Ideation reads goal.md in the order Mission → Done when → Current focus →
Non-goals → Constraints (per `ap2/ideation.default.md`). What each section
is for, how ideation reads it, and a worked example follow — the examples
are illustrative (a fictional Slack-bot-for-trade-alerts project threaded
through all five sections) so they teach the section's shape and validator
interaction without coupling docs tests to this repo's live `goal.md`
content. Fresh `ap2 init` projects ship a five-section placeholder
(`GOAL_TEMPLATE` in `ap2/init.py`); replace the placeholders with content
specific to your project.

### Mission

One sentence: what is this project FOR? Frames every proposal; ideation
reads it but doesn't quote-match against it.

- **Bad:** "improve developer experience" (unmeasurable, no subject).
- **Good:** "a Slack bot that ingests trade alerts and posts daily P&L
  summaries" (concrete subject + scope).

Validator interaction: NOT anchor surface for TB-161 — the matcher only
mines `## Current focus` / `## Done when`.

Worked example (fictional Slack-bot project, threaded through all five
sections below):

> A Slack bot that ingests trade alerts from a broker webhook and posts
> daily P&L summaries to a configured channel by 17:00 ET each weekday.

One sentence; names the subject (the bot), the activity (ingest alerts
and post summaries), and the value (daily P&L visibility by a deadline).
No measurable reliability claim — that's `## Done when`'s job.

### Done when

Bulleted list of concrete completion criteria. **Load-bearing** for
ideation's Step 0: when all criteria are met, the focus item flips to
`exhausted-needs-operator` and ideation stops proposing. Without `## Done
when` the only stop-signal is the operator intervening — which defeats
the walk-away promise.

- **Bad:** "the project is solid" (unmeasurable; nothing for ideation to
  check against).
- **Good:** "walks 1000 strategies through backtest at <10s/strategy on
  the prod box" (measurable, falsifiable, observable threshold).

Apply the **delete-test** to each bullet: remove it, and does the project's
done-signal genuinely change? If no, the bullet is filler — cut it.

Validator interaction: anchor surface for TB-161. The first 3-6 words of
each bullet become substrings a briefing's `## Goal` body can cite.

Worked example (fictional Slack-bot project):

> - The bot posts a P&L summary by 17:00 ET on 30 consecutive trading
>   days without operator intervention.
> - An operator can swap the broker integration without touching the
>   alert-routing or summary-rendering code paths.

Both bullets are measurable (30 consecutive trading days; swap without
touching named code paths) and falsifiable. The lead phrase "The bot
posts a P&L summary" is a usable TB-161 anchor — any briefing whose
`## Goal` body quotes those six words satisfies the substring check.

### Current focus

Narrative paragraphs naming the active theme(s). Ideation's Step 0 emits a
per-focus-item assessment (Progress / Gaps / Status / Reasoning) keyed on
each Current-focus heading. The heading title doubles as the canonical
TB-161 anchor for that focus item.

- **Bad:** "Make ap2 better in general." (no theme; nothing for a briefing
  to cite, nothing for ideation to assess against.)
- **Good:** "Current focus: webhook reliability" — a discrete noun phrase
  that names a theme broader than one task but narrower than the whole
  mission.

Validator interaction: anchor surface for TB-161. Both the full heading
title and any 4-6-word phrase from the body prose work as substring
citations. Quote the heading title verbatim when in doubt — it's the
cheap, unambiguous path.

Worked example (fictional Slack-bot project):

> ## Current focus: webhook reliability
>
> The broker webhook is the bot's single ingestion path — alerts dropped
> here never reach the summary. The focus is on retry semantics, dead-
> letter handling, and observability of webhook delivery so a missed
> alert is visible within minutes rather than discovered the next morning.

This heading title is the canonical anchor for every briefing that threads
back to this focus — a briefing whose `## Goal` body contains the
substring `Current focus: webhook reliability` (case-insensitive after
punctuation normalization) passes TB-161.

### Non-goals

Bulleted list of explicit non-goals. Ideation's Step 0 includes a
"non-goal risk check" — proposals straying into non-goal areas get flagged
in the assessment. Frame each bullet as "we are NOT trying to X because Y"
so the drift-detection signal is unambiguous.

- **Bad:** "Don't be slow." (negated wish, not a non-goal.)
- **Good:** "Generic task scheduler / project management tool: ap2 is
  opinionated about agent-driven dev work." (names the rejected shape AND
  the reason.)

Validator interaction: NOT anchor surface for TB-161 — non-goal text
doesn't feed the substring matcher.

Worked example (fictional Slack-bot project):

> - **Generic Slack notification framework**: the bot is opinionated about
>   trade alerts and P&L summaries. Don't add features whose primary use
>   case is "post arbitrary messages to a channel" — those compete with
>   native Slack incoming webhooks and dilute the bot's purpose.

Bold lede names the rejected shape; the body explains the reason
("opinionated about trade alerts and P&L summaries") so ideation can
flag a generic-notification proposal as off-goal in its per-cycle
assessment.

### Constraints

Bulleted list of hard constraints — tech stack, deadlines, dependencies,
blast-radius limits. Ideation respects constraints when ranking proposals
(e.g., "no API-key features" if OAuth-only is a constraint).

- **Bad:** "Try to keep things simple." (subjective; nothing to gate
  against.)
- **Good:** "OAuth auth (CLAUDE_CODE_OAUTH_TOKEN): not API-key. Features
  that require API-key (custom betas) are out of reach." (names the
  constraint AND the class of features it forbids.)

Validator interaction: NOT anchor surface for TB-161 today. Constraint-
specific TBs needing a goal-anchor citation have to thread their quote
through `## Current focus` or `## Done when` — e.g., bake the constraint
into a Current-focus narrative paragraph if you want it cite-able by
every related briefing.

Worked example (fictional Slack-bot project):

> - **Single broker webhook ingestion path**: alerts arrive through one
>   broker-owned webhook endpoint; no shared ingestion bus and no polling
>   fallback. Recovery is always "replay the broker's webhook log."

Bold lede names the constraint; the body spells out what it forbids ("no
shared ingestion bus and no polling fallback") so a briefing proposing
"add a backup polling fetcher" can be vetoed unambiguously by the
operator and called out by ideation's non-goal risk check.

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

To surface "this should fire on a schedule" without bundling it into the
result reporting, call the dedicated `cron_propose(name, schedule, prompt,
rationale)` tool one or more times (TB-123 lifted the legacy `cron='...'`
argument out of `report_result`). Proposals queue for operator review;
they do NOT mutate `cron.yaml`.

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
4. Ideation   — fire `_maybe_ideate` if Ready+Backlog count is below
                `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3, TB-160)
                + cooldown elapsed (default 2h). Operator can also
                trigger manually via `ap2 ideate [--force]` (TB-159),
                bypassing the cooldown / disable / queue-depth gates.
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
  invokes). The MM handler exposes it for `@claude-bot status`.

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
| `ap2 status [--json]` | One-screen snapshot — daemon pid, board section counts, cron jobs, decisions-needed nudges. | The "first thing to run" verb at the top of every operator session; pair `--json` with `jq` for tooling. |
| `ap2 init` | Idempotent scaffold of `.gitignore` + `.cc-autopilot/tasks/` skeleton in a fresh project. | Run once when bringing a repo under ap2; no-op if the structure already exists. |
| `ap2 doctor [--user U]` | Sanity-check that the project is ready to boot — skeleton present, sandbox user installed, OAuth token reachable. | Run before `ap2 start` on an unfamiliar machine to diagnose the "daemon won't start" silent-fail modes (TB-79's token-missing path is the most common hit). |
| `ap2 check [--json]` | Validate on-disk state-file integrity — TASKS.md shape, briefing-link resolution, cron.yaml schema, JSON state parseability, insights front matter (TB-108). | Exits 1 on errors; warnings (stale brief links, missing goal.md) don't fail. Run after any manual edit to a fenced file. |
| `ap2 logs [-n N] [--json]` | Tail `events.jsonl` with column truncation for human reading. | Faster than `tail \| jq` for the common "what just happened?" question; default trims fields to 120 chars and `--json` gives full payloads. |
| `ap2 backlog TB-N` | Move a task into Backlog from any section (last-ditch reset without retry-counter exhaustion). | Use when a stuck Active task needs to step back without burning retries; for permanent removal use `ap2 delete` instead. |
| `ap2 add --briefing-file PATH [-s SECTION] [-t TAGS...] [--no-verify] [--blocked CSV] [--skip-goal-alignment]` | Add a new operator-filed task with a real briefing the per-task verifier can read (TB-135). | `--briefing-file` is required because verification needs a `## Verification` section; pass `-` for stdin. `--skip-goal-alignment` (TB-170) bypasses the TB-161 goal-cite and TB-164 Why-now checks for legitimately-meta work (dep bumps, doc fixes). |
| `ap2 update TB-N [--title T] [--tags CSV] [--blocked CSV] [--description D] [--clear-tags] [--clear-blocked] [--briefing-file PATH] [--force] [--skip-goal-alignment]` | In-place edit a task's board-line fields and/or its briefing file (TB-153). | Routes through the operator queue so the mutation lands at a tick boundary, never mid-task-run; omitted flag = field unchanged. `--force` lets board-line edits land on Active / Pipeline Pending tasks (briefing edits stay hard-refused). |
| `ap2 delete TB-N [-f]` | Permanently remove a task from the board (row + briefing file) — emits `task_deleted` for audit. | Refuses Active/Ready without `--force`. Use `ap2 reject` instead for ideation proposals still gated by `@blocked:review`, so the rejection reason feeds ideation Step 0's "don't re-propose" learning. |
| `ap2 reject TB-N [--reason TEXT]` | Reject an ideation-proposed Backlog task (TB-152): drops the row + briefing AND logs the reason. | Writes `rejected ideation proposal → TB-N (<title>): <reason>` to `operator_log.md`; the reason becomes a learnable signal for the next ideation cycle, and `(no reason given)` is itself a (weak) signal. |
| `ap2 classify TB-N --impact VERDICT [--reason TEXT]` | Record the operator's retrospective impact verdict (`advanced-goal` / `pro-forma` / `unclear`) on a shipped proposal (TB-189). | Captures whether the task substantively moved the goal forward or merely satisfied validators (goal.md L66-76's failure mode); reasons feed TB-188 per-proposal records and `operator_log.md` so future ideation cycles can learn which proposal shapes actually pay off. |
| `ap2 ack NOTE [-t TB-N]` | Record an out-of-band operator decision in `operator_log.md` so ideation stops re-proposing actions whose effects aren't filesystem-visible (TB-106). | Use for "I already decided X out-of-band" announcements and for clearing decisions-needed nudges the daemon keeps surfacing. |
| `ap2 approve TB-N` | Approve an ideation-proposed task (TB-121) — strips its `@blocked:review` codespan so the next tick auto-promotes it out of Backlog. | The thumbs-up half of the `approve` / `reject` pair on freshly-ideated proposals; refuses if the task isn't on the board at all. |
| `ap2 unfreeze TB-N` | Move a Frozen task back to Backlog and clear its retry counter. | Run after fixing the underlying blocker (flaky test, missing dep); refuses if the task isn't currently Frozen so you can't accidentally reset a healthy task. |
| `ap2 ideate [--force]` | Manually trigger an ideation pass (TB-159), bypassing the natural empty-board / cooldown / `AP2_IDEATION_DISABLED` gates. | Routed through the operator queue; the daemon runs ideation on its next tick (≤30s). Use to refill a thin Ready/Backlog when waiting on cooldown is impractical; the cooldown clock still advances after the forced run. |
| `ap2 update-goal --file PATH [--reason TEXT]` | Refresh `goal.md` via the operator queue (TB-193) — full-file replacement applied at the next tick under `board_file_lock`. | Symmetric to `ap2 add --briefing-file`; operator-CLI-only by design — the MM handler has no path to mutate `goal.md`. The `--reason` line feeds future ideation cycles as a goal-drift signal. |
| `ap2 rollback [-n N \| --task TB-N \| --to SHA] [-y] [--force]` | Linear rollback (TB-111): walk back from HEAD by N tasks (or to a specific TB-N / sha) and `git reset --hard`. | Restores TASKS.md + every committed state file coherently in one shot. Refuses a dirty working tree by default; use when a sequence of recent task-completions needs to be undone together rather than one-by-one. |
| `ap2 backfill-proposals [--dry-run]` | Backfill historical ideation proposal records (TB-195) for every ideation-authored TB-N that lacks one. | Scans `operator_log.md` + briefing files + `events.jsonl` and writes per-proposal records. Idempotent; safe to re-run. Operator-driven one-off, NOT routed through the operator queue or daemon ticks. |
| `ap2 pause [--reason TEXT]` | Pause the daemon by setting a flag file — in-flight tasks finish but no new ones dispatch. | Use for short maintenance windows; pair with `ap2 resume` to re-enable. The reason is recorded in events for the operator audit trail. |
| `ap2 resume` | Clear the pause flag set by `ap2 pause`; the daemon picks up on its next tick (≤30s). | Symmetric pair to `ap2 pause`; no-op if the daemon isn't paused. |
| `ap2 web [--host H] [--port P]` | Start the read-only HTTP UI at `127.0.0.1:7820` with `/events`, `/tasks`, `/task/<TB-N>`, `/pipelines`, `/insights`, `/ideation_state`, `/commits` pages. | Useful when scanning visually beats asking the session for a summary; the daemon also spawns this automatically on `ap2 start` unless `AP2_WEB_DISABLED` is set. |
| `ap2 cron list` | List the cron jobs registered in `cron.yaml` with their next-fire timestamps. | The diagnostic for "why isn't the X routine firing?" — pair with `tail .cc-autopilot/cron_state.json` to confirm the last-fire timestamp. |
| `ap2 cron edit ACTION NAME [--interval I] [--prompt P] [--active-when E] [--max-turns N]` | Add / remove / update a cron job in `cron.yaml`. | Operator-CLI-only since TB-146 retired the agent-side `cron_edit` tool; the TB-202 refuse-if-active gate prevents a mid-task invocation from racing the fenced cron.yaml write against the task agent's snapshot window. |
| `ap2 sandbox user-audit [USER]` | Verify the sandbox user has no creds beyond `CLAUDE_CODE_OAUTH_TOKEN` (and optional Mattermost env). | The pre-flight before letting the daemon run code as that user — the sandbox model only holds if the user can't reach the human's `~/.ssh`, keychain, or other repos. |
| `ap2 sandbox user-setup [USER] [-y] [--skip-token] [--skip-statusline] [--mm-url/--mm-token]` | Create the sandbox user (prompts before running sudo). | One-time per machine; pairs with `install-token` / `install-howto` / `install-mm` to fill in creds + per-user config. Skip flags exist for partial setups. |
| `ap2 sandbox install-token [USER] [--token-env VAR]` | Install `CLAUDE_CODE_OAUTH_TOKEN` into `~<user>/.zshenv`. | Run after `claude setup-token`; the daemon refuses to start without the token in its env (TB-79), and the macOS keychain is locked for non-GUI shells so token-via-keychain doesn't work. |
| `ap2 sandbox install-statusline [USER]` | Copy `hooks/statusline-command.sh` into `~<user>/.claude/` and wire it into the per-user `settings.json`. | Convenience for matching the human's statusline customization on the sandbox user; purely cosmetic for the daemon itself. |
| `ap2 sandbox install-howto [USER]` | Copy `ap2/howto.md` to `~<user>/.claude/ap2-howto.md` so a Claude session running as the sandbox user can read it as context. | The agent's primary how-do-I-think-about-ap2 reference — keeping it on the sandbox user's home means an interactive session there has the same context the host shell does. |
| `ap2 sandbox install-mm [USER] [--mm-url/--mm-token]` | Install `MATTERMOST_URL` + `MATTERMOST_TOKEN` into `~<user>/.zshenv`. | Optional — only needed if the project wants the daemon's Mattermost loop active (poll mentions, post status reports, route `@claude-bot` chat verbs). |
| `ap2 sandbox project-setup SOURCE [--user U] [-y] [--mm-channel N] [--git-name N] [--git-email E]` | Clone `<source>` into `~<user>/repos/` with repo-local git identity set. | The "transfer this project to the sandbox" verb; pair with `--mm-channel` to wire the per-project channel routing in one step, or fall back to `install-channel` after the fact. |
| `ap2 sandbox install-channel PROJECT CHANNEL [--user U]` | Resolve a Mattermost channel name to an ID and write `AP2_MM_CHANNELS` into `<project>/.cc-autopilot/env`. | Run after `project-setup` if you skipped `--mm-channel` then; idempotent overwrite. |
| `ap2 sandbox project-audit PATH [--user U]` | Verify an isolated project clone is well-formed — ownership, git identity, env file. | The diagnostic for "did `project-setup` finish correctly?" — catches half-completed setups before they confuse `ap2 doctor` later. |
| `ap2 sandbox sync-skills [--apply] [--dest DIR]` | Sync `<repo>/skills/*` into `$HOME/.claude/skills/` (TB-140). | Default is a dry-run drift summary; pass `--apply` to actually copy. Run after editing a skill in the repo if you want the change live for the sandbox user's interactive Claude sessions. |

## Event schema (the canonical timeline)

`.cc-autopilot/events.jsonl` is append-only. Every line has `ts` (UTC
ISO-8601) + `type`; other fields vary. Categories:

**Lifecycle.** `daemon_start`, `daemon_stop`, `daemon_pause`,
`daemon_resume`, `task_start`, `task_complete`, `cron_start`,
`cron_complete`, `cron_skipped` (status-report no-op when there's
nothing new to summarize, TB-153), `cron_bootstrap` (first-run
seeding of `cron.yaml` from `cron.default.yaml`), `ideation_empty_board`
(skip — no slots OR cooldown), `ideation_forced` (operator forced via
`ap2 ideate --force`), `ideation_skipped` / `ideation_skipped_no_slots`,
`ideation_complete`, `ideation_state_updated`, `web_start`, `web_stop`.
Per-run cost/usage: `task_run_usage` (per task agent run, TB-180),
`control_run_usage` (per cron / ideation / MM-handler run, TB-179),
`judge_call` (per per-task-verifier prose-bullet judge invocation,
TB-69 + TB-181).

**Failure.** `task_error`, `task_timeout`, `task_state_violation` (TB-110
post-hoc fenced-file check tripped), `task_rollback` (TB-110
rollback to pre-task state), `verification_failed` (per-task or
project-wide), `verification_partial`, `retry_exhausted`,
`cron_error`, `cron_timeout`, `ideation_error`, `ideation_timeout`,
`mattermost_error`, `mattermost_timeout`, `mm_poll_error`,
`state_commit_error`, `rollback_error`, `web_error`,
`pipeline_pending_sweep_error`, `operator_queue_error` /
`operator_queue_drain_error`, `auto_diagnose_error` /
`auto_diagnose_post_error` / `auto_diagnose_no_destination`,
`classify_record_missing` / `classify_record_unreadable` (TB-194/195
post-task classify routine couldn't find or read its record).

**State / observability.** `task_implicit_commit` (HEAD-salvage),
`task_pipeline_pending` (TB-115 launching task parked while pipelines
run), `task_unfrozen`, `task_deleted` (TB-138 `ap2 delete`),
`task_updated` (TB-141 queue-routed update), `task_classified` (TB-194
post-task auto-classifier verdict), `backlog_auto_promoted`,
`cron_proposed`, `cron_proposal_error`, `pipeline_start`,
`orphan_recovery`, `board_malformed_line`, `mattermost`,
`mattermost_reply` (handler emitted a reply), `auto_diagnose_fired`,
`janitor_finding` (TB-178 chore-judge surfaced a candidate), `goal_updated`
(TB-189 operator-queued `update_goal` op landed), `pending_review_reminder`
(TB-184 unadopted cron-proposal nudge), `operator_ack` (TB-141
`@claude-bot ack: …`), `operator_queue_append` /
`operator_queue_drained`, `ideation_approved` (TB-121 operator
`ap2 approve TB-N` promoted a proposed task), `ideation_proposal_recorded`
/ `ideation_proposal_reconciled` (TB-188 per-proposal audit trail).

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
`~claude-agent/.zshenv`. The full set the ap2 source consults
(`grep -nE 'AP2_[A-Z_]+' ap2/*.py` is the source-of-truth — the
`test_every_env_knob_documented` gate in `ap2/tests/test_docs_drift.py`
fails CI if a new knob is added and not listed here):

**Loop cadence + per-run timeouts.**
- `AP2_TICK_S` (30) — main-loop tick interval.
- `AP2_MM_TICK_S` (10) — Mattermost polling tick interval (separate
  loop, TB-122).
- `AP2_TASK_TIMEOUT_S` (1200) — per-task SDK query timeout.
- `AP2_TASK_MAX_TURNS` (50) — max turns per task agent.
- `AP2_CONTROL_TIMEOUT_S` (300) — per-control-agent timeout (cron,
  ideation, MM handler).
- `AP2_CONTROL_MAX_TURNS` (15) — max turns per control agent (cron
  + MM handler share this default; ideation has its own).
- `AP2_IDEATION_MAX_TURNS` (30) — max turns for the ideation agent
  (bumped from the legacy `AP2_CONTROL_MAX_TURNS` default because
  ideation's Step 0 / 0.5 / 1.5 chain runs deeper than other control
  jobs).
- `AP2_MAX_RETRIES` (3) — failed-task retries before Frozen.
- `AP2_EVENT_CONTEXT` (50) — count of recent events inlined into agent
  prompts.

**Agent model + effort.** Per-run knobs that override the per-job default.
- `AP2_AGENT_MODEL` (`claude-opus-4-7`) — model for task agents and
  the SDK-judge plumbing (verifier, janitor).
- `AP2_AGENT_EFFORT` (`xhigh`) — global effort level. Each
  sub-job has its own override that falls back here:
  `AP2_STATUS_REPORT_EFFORT`, `AP2_VERIFY_JUDGE_EFFORT`,
  `AP2_JANITOR_JUDGE_EFFORT`.
- `AP2_VERIFY_JUDGE_MAX_TURNS` (20), `AP2_JANITOR_JUDGE_MAX_TURNS` (12)
  — max turns for the per-bullet prose-judge and the janitor chore-judge.

**Verification.**
- `AP2_VERIFY_CMD` — project-wide regression gate (e.g.
  `uv run pytest -q`). Unset = no project-wide gate.
- `AP2_VERIFY_TIMEOUT_S` (600) — timeout for the project-wide gate.

**Ideation.**
- `AP2_IDEATION_DISABLED` — set to `1`/`true` to opt out of empty-board
  ideation entirely.
- `AP2_IDEATION_COOLDOWN_S` (7200) — minimum gap between ideation runs.
- `AP2_IDEATION_TRIGGER_TASK_COUNT` (3) — fire ideation when Ready+Backlog
  count is BELOW this threshold (Active is still a hard gate). Set to
  `1` for the legacy "fire only when the working queue is fully empty"
  behavior; raise it (e.g. `5`) for projects with very fluid scope.
  Invalid (non-int, non-positive) values fall back to the default.

**Watchdog (auto-diagnose).**
- `AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S` (10800 = 3h) — idle duration
  before the watchdog posts a `DiagnoseReport`.
- `AP2_AUTO_DIAGNOSE_COOLDOWN_S` (21600 = 6h) — minimum gap between
  watchdog posts (re-fire spam guard).

**Janitor (chore-judge, TB-178).**
- `AP2_JANITOR_MAX_FINDINGS_LLM` (10) — cap on per-cycle findings sent
  to the SDK judge. `0` disables the judge call entirely (the janitor
  emits rule-based findings only).

**Mattermost.**
- `AP2_MM_CHANNELS` — comma-separated MM channel IDs to poll for
  `@claude-bot` mentions.
- `AP2_MM_REPORT_CHANNEL` (TB-190) — explicit channel ID for
  status-report posts. Unset → falls back to `AP2_MM_CHANNELS[0]`.
- `AP2_MM_MENTION` (`@claude-bot`) — pattern that triggers handler
  dispatch.
- `AP2_MM_BOT_USER_ID` — bot's user ID (used for self-message
  filtering so the handler doesn't loop on its own replies).
- `AP2_MM_TEAM_ID` — Mattermost team ID (sandbox install-channel
  helper uses this).

**Local web UI (`ap2 web`, daemon-spawned read-only HTTP).**
- `AP2_WEB_PORT` (7820) — bind port. Malformed values fall back to
  the default rather than crashing daemon startup.
- `AP2_WEB_DISABLED` — set to `1`/`true`/`yes`/`on` to skip starting
  the daemon-spawned web UI.

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
