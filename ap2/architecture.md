# ap2 architecture

Technical design for the autopilot v2 daemon. Companion to [`README.md`](README.md), which is the operator quickstart and reference.

## Design principles

These three constraints drive every other decision in the codebase.

**1. Each unit of work runs in a fresh SDK `query()` call.** A task agent gets a clean context with its briefing + recent events; it never sees other tasks' working memory. Same for the cron, mattermost, and ideation agents. This is the answer to v1's compaction-fatigue problem: long-running Claude Code sessions degraded as their context filled, and post-compaction agents lost track of what they had been doing. v2 has nothing to compact — the daemon is a Python scheduler, not a Claude session.

**2. Shared awareness lives in files, not in any agent's context.** `TASKS.md`, `events.jsonl`, `progress.md`, `cron_state.json`, the briefings, and `ideation_state.md` are all on disk. Each spawned agent gets the relevant files inlined into its prompt — typically a briefing + a tail of `events.jsonl`. No state crosses query boundaries via memory.

**3. Mutations go through narrow tools.** Control agents can write to the board, but only via the `board_edit` MCP tool — no `Write`/`Edit` access to `TASKS.md`. Same pattern for `cron_edit`, `ideation_state_write`, `pipeline_task_start`. Broad reads, narrow writes. This keeps mutation paths auditable (each emits a structured event) and makes accidental clobbering impossible without going out of band.

## The daemon loop

`daemon.main_loop` is a plain `asyncio` loop. Each tick (default 30s, `AP2_TICK_S`):

```
_tick(cfg, sdk, mcp_server):
  1. Mattermost — check_new_messages → handle_message per @claude-bot mention
  2. Cron       — load_jobs + due_jobs → run_cron per due job
  3. Tasks      — board.next_ready, or auto-promote next_dispatchable("Backlog")
                  → run_task on the picked task
  4. Ideation   — _maybe_ideate (no-op unless board fully empty + cooldown)
  5. Watchdog   — _maybe_auto_diagnose (no-op unless idle threshold passed)
```

Steps run sequentially. A failure in any step emits an event and continues to the next — one broken cron job doesn't block task dispatch. The whole tick is wrapped in try/except so the daemon never exits on an unhandled error.

The pause flag (`<root>/.cc-autopilot/paused`, presence-only) short-circuits the entire tick body except for `daemon_resume` event detection.

## Agent kinds

There are four kinds of SDK queries, each with its own prompt builder, tool allowlist, and lifecycle event vocabulary.

| Kind | Trigger | Prompt builder | Tools | Timeout |
|---|---|---|---|---|
| **Task** | `run_task` (step 3) | `prompts.build_task_prompt` | `TASK_AGENT_TOOLS` (Read/Edit/Write/Bash + `pipeline_task_start`) | `AP2_TASK_TIMEOUT_S` (1200s) |
| **Cron** | `run_cron` (step 2) | `prompts.build_control_prompt` | `CONTROL_AGENT_TOOLS` (board/cron/mm/log_event/daemon_control/ideation_state_write) | `AP2_CONTROL_TIMEOUT_S` (300s) |
| **Mattermost** | `handle_message` (step 1) | `prompts.build_mattermost_prompt` | `CONTROL_AGENT_TOOLS` | `AP2_CONTROL_TIMEOUT_S` |
| **Ideation** | `_maybe_ideate` (step 4) | `prompts.build_control_prompt` + `ap2/ideation.default.md` body | `CONTROL_AGENT_TOOLS` | `AP2_CONTROL_TIMEOUT_S` |

Task agents are the only kind that gets `Write`/`Edit`. They commit code; everything else mutates state through MCP tools.

Ideation and cron share the same prompt builder (`build_control_prompt`) — the framing is `## Control job: <name>`, deliberately neutral on whether the run is on a schedule. Ideation has its own lifecycle and event vocabulary on top of that shared prompt (see "TB-98" below).

### Shared SDK plumbing

`daemon._run_control_agent(label, prompt, allowed_tools, max_turns)` is the shared SDK plumbing for cron + ideation. It does:

- `_prep_debug_dumps(label)` — write the prompt to `.cc-autopilot/debug/<ts>-<label>.prompt.md`.
- `_make_stderr_sink()` — 200-line ring buffer attached to `ClaudeAgentOptions.stderr` so an opaque SDK subprocess crash leaves us a tail to diagnose.
- `await asyncio.wait_for(consume(), timeout=cfg.control_timeout_s)` — bounded SDK consume.
- Returns `(timed_out, error, stderr_tail, prompt_dump)`.

The caller owns the surrounding event vocabulary (`cron_*` for `run_cron`, `ideation_*` for `_maybe_ideate`), the cooldown bookkeeping (`mark_run`), and the state commit. This split is what keeps ideation off the `cron_*` event channel without duplicating the SDK plumbing.

`run_task` doesn't use `_run_control_agent` because it has a salvage path: on timeout or crash, `_infer_result_from_head` checks `git log` for a commit prefixed with the task ID, and if found, treats the task as completed (the agent committed before the SDK subprocess died). That branch is too divergent to share cleanly.

## Task lifecycle

A task moves through the board sections:

```
Backlog → Ready → Active → Complete  (happy path; auto-promotion at the
                                       Backlog→Ready boundary)
              ↓        ↓
         (skipped     Backlog (status: blocked / failed)
          if blocked      ↓
          on TB-X)    Frozen (after AP2_MAX_RETRIES)
```

`run_task`:
1. `move_to_active` (board lock).
2. Build the prompt: header + briefing + recent events + RESULT format spec.
3. `sdk.query()` consumed turn-by-turn; messages dumped to `.stream.jsonl` + `.messages.jsonl` for diagnosis (TB-85).
4. Parse the agent's final `RESULT:` block — `status` + `commit` + `summary` + `files_changed` + `tests_passed` + optional `cron:` directives.
5. Two-tier verify:
   - Per-task verification (`verify.verify_task`) runs the briefing's `## Verification` bullets — shell bullets via subprocess, prose bullets via SDK judge.
   - Project-wide gate (`AP2_VERIFY_CMD`, e.g. `uv run pytest -q`) runs after the per-task verify. `#no-verify` tag opts out.
6. `move_to_complete` on success, `move_to_backlog` on `blocked` (with a Retry counter increment), `move_to_frozen` after `AP2_MAX_RETRIES`.
7. `_commit_state_files` stages + commits all daemon-owned files with subject `state: TB-N → <section>`.

Failure paths (`task_timeout`, `task_error`) try `_infer_result_from_head` first — if the agent committed before the crash, we keep the work and emit `task_implicit_commit` (with `reason=timeout_recovered` / `error_recovered`). This is what unstuck stoch's TB-58/TB-59 retry loops where the agent kept re-doing already-committed work.

## State files and ownership

| File | Owner | Lock | Committed |
|---|---|---|---|
| `TASKS.md` | daemon (via `do_board_edit`) | `fcntl.flock` per-board mutation | yes (state-file commits) |
| `.cc-autopilot/events.jsonl` | daemon + tools (append-only) | none (line-atomic write) | no (gitignored) |
| `.cc-autopilot/progress.md` | daemon (`_append_progress`) | none (single-writer) | yes |
| `.cc-autopilot/cron.yaml` | daemon (via `do_cron_edit`) + operator | none (single-writer) | yes |
| `.cc-autopilot/cron_state.json` | daemon (`mark_run`) | `fcntl.flock` | no (gitignored) |
| `.cc-autopilot/retry_state.json` | daemon | `fcntl.flock` | no |
| `.cc-autopilot/mm_state.json` | daemon | none (single-writer) | no |
| `.cc-autopilot/auto_diagnose_state.json` | daemon | none | no |
| `.cc-autopilot/ideation_state.md` | ideation agent (via `ideation_state_write`) | atomic write (tmpfile + rename) | yes |
| `.cc-autopilot/tasks/<TB-N>.md` | operator + ideation + `do_board_edit` | none | yes |
| `.cc-autopilot/insights/<topic>.md` | task agents + operator | none | yes |
| `.cc-autopilot/insights/_index.md` | daemon (`maybe_regenerate_index`) | none | yes |
| `.cc-autopilot/pipelines/<name>-<pid>.log` | detached pipeline subprocess | none | gitignored |
| `.cc-autopilot/debug/<ts>-<label>.{prompt,stream,messages}` | daemon (`_prep_debug_dumps`) | none | gitignored |
| `CLAUDE.md` | operator (Next task ID auto-bumped by daemon) | none | yes |

State-file commits land with subject `state: TB-N → Complete` (per task) or `state: cron <name>` / `state: ideation` (per cron/ideation run). They ride alongside the task agent's source commit so `git log` tracks board evolution next to code evolution.

## Module map

```
ap2/
├── cli.py            # argparse → cmd_* handlers; reads pid file
├── daemon.py         # main_loop, _tick, run_task, run_cron, handle_message,
│                     # _run_control_agent, _make_stderr_sink, _commit_state_files
├── config.py         # Config dataclass, env-var resolution, .cc-autopilot/env loader
├── board.py          # Board (TASKS.md parser), locked_board, malformed_lines,
│                     # next_ready, next_dispatchable
├── events.py         # append-only JSONL writer, tail()
├── cron.py           # CronJob dataclass, load_jobs, due_jobs, mark_run, bootstrap
├── ideation.py       # _maybe_ideate (empty-board trigger + cooldown)
├── insights.py       # maybe_regenerate_index (.cc-autopilot/insights/_index.md)
├── verify.py         # parse_verification_section, verify_task (per-task gate)
├── diagnose.py       # build_report, render_markdown (watchdog informant — pure)
├── retry.py          # retry counter (fcntl-locked .json)
├── tools.py          # MCP server: do_board_edit, do_cron_edit, do_mattermost_reply,
│                     # do_log_event, do_daemon_control, do_ideation_state_write,
│                     # do_pipeline_task_start
├── pipelines.py      # is_blocking (pid:N@TS dependency check)
├── prompts.py        # build_task_prompt, build_control_prompt, build_mattermost_prompt
├── web.py            # local read-only web UI (TB-99 + TB-93 thaw):
│                     # /, /events, /tasks, /task/<TB-N>, /pipelines,
│                     # /insights, /insight/<name>, /ideation_state, /commits.
│                     # Full event payloads (vs `ap2 logs` which truncates).
├── mattermost.py     # check_new_messages (one-shot poll), reply
├── result.py         # parse RESULT block (status/commit/summary/files/cron)
├── init.py           # init_project (gitignores, dirs, board templates)
├── doctor.py         # ap2 doctor: user_audit + project_audit + CLI presence
├── sandbox.py        # claude-agent setup, project-clone, MM creds, statusline
├── ideation.default.md  # the ideation prompt body (load-bearing)
├── cron.default.yaml    # bootstrapped cron jobs (status-report)
├── README.md         # operator quickstart + CLI reference
└── architecture.md   # this file
```

Cycles to watch out for:
- `daemon` ↔ `ideation`: `daemon` imports `ideation` at top-level (step 4 calls `_maybe_ideate`); `ideation` lazy-imports `daemon` inside `_maybe_ideate` to call `_run_control_agent` + `_commit_state_files`. The lazy import is load-bearing.
- `daemon` ↔ `tools`: `daemon` imports `tools`; `tools` does NOT import `daemon`. Tool handlers receive a `Config` and read events directly.

## Custom MCP tools

The daemon registers an MCP server with `claude_agent_sdk.create_sdk_mcp_server` and passes it as `mcp_servers={"autopilot": mcp_server}` in every `ClaudeAgentOptions`. Two tool pools, partitioned by `allowed_tools`:

```python
CONTROL_AGENT_TOOLS = [
    # Filesystem (broad reads)
    "Read", "Glob", "Grep", "Bash",
    # Custom MCP (narrow writes)
    "mcp__autopilot__board_edit",
    "mcp__autopilot__cron_edit",
    "mcp__autopilot__mattermost_reply",
    "mcp__autopilot__log_event",
    "mcp__autopilot__daemon_control",
    "mcp__autopilot__ideation_state_write",
]

TASK_AGENT_TOOLS = [
    "Read", "Glob", "Grep", "Bash", "Edit", "Write",
    "mcp__autopilot__pipeline_task_start",
]
```

The "broad reads, narrow writes" split is what makes the system auditable. Every state mutation goes through a typed handler that emits a structured event. The agent can't silently rewrite `TASKS.md` because it doesn't have `Write` access to it (control agents) or because the file is daemon-owned and the agent's prompt forbids touching it (task agents).

`do_log_event` is the escape hatch: an agent can emit any custom event type with a summary. This is how the ideation agent emits `ideation_complete` (its success summary) and how status-report emits `cron_complete` from inside the prompt.

## Two-tier verification

A task is verified twice before landing in Complete:

**Per-task verification** (`ap2/verify.py`). The briefing's `## Verification` section is parsed for shell bullets (`[shell] cmd` or backtick-quoted `\`cmd\``) and prose bullets (free text). Shell bullets run via `subprocess` in the project root; prose bullets go to a small SDK judge that returns `{status, rationale}`. Verdicts: `pass`, `partial` (some unverified, none failed — proceeds to Complete with a `verification_partial` event), `fail` (routes through retry).

The verifier picks the **last** `## Verification` section in the briefing. This matters for two-tier pipeline-launch briefings (TB-86), which embed a `validation_briefing` sub-document with its own `## Verification`. The launch task's own bullets must come last; the inline validation's output-artifact checks (which run after the pipeline dies) come earlier.

**Project-wide regression gate** (`AP2_VERIFY_CMD`). Runs after a successful per-task verify. Default unset = skip. Typical values: `uv run pytest -q`, `cargo test`, `npm test`. Failure routes the task through `_handle_failure` like any other crash. `--no-verify` on the original `ap2 add` opts the task out (tag `#no-verify`).

This split lets the per-task gate stay narrow ("did the agent do THIS task's work") while the project-wide gate stays generic ("did the project break") — the two answer different questions.

## Pipelines (`pipeline_task_start`)

Long-running work (>10 min — sweeps, full-history backtests, anything with progress bars) goes through `pipeline_task_start` instead of being run inline. The tool:

1. Spawns the command via `Popen(shell=True, start_new_session=True)`.
2. Captures `psutil.Process(pid).create_time()` for PID-recycling defense.
3. Writes a `pipeline_start` event with the pid + log path.
4. Inside `locked_board`, creates one Backlog **validation task** with `(blocked on: pid:<N>@<TS>)`.

`Board.next_dispatchable("Backlog")` skips any task whose blockers aren't satisfied. `pipelines.is_blocking(pid, ts)` returns `True` while the process is alive AND its `create_time` matches — both checks survive a crashed daemon and a long-lived daemon over weeks. When the pipeline exits and the validation task auto-promotes, that task runs the *output-artifact* checks (`test -f reports/foo.csv`, JSON schema validation), which the launch task can't run because the pipeline hadn't produced output yet.

The TB-86 prompt guidance + TB-91 verifier last-match fix together ensure ideation authors briefings of this shape correctly and the verifier picks the right `## Verification` section at each tier.

## Failure modes and recovery

**SDK subprocess crash with empty stderr** — pre-TB-94, `cron_error` events fired with the useless "Check stderr output for details" sentinel. Now every SDK call routes through a stderr-sink ring buffer (`_make_stderr_sink`), and `task_error` / `cron_error` / `ideation_error` carry `stderr_tail` + `prompt_dump` paths so the operator can replay the prompt and see what actually broke.

**Agent committed but didn't emit RESULT** — `_infer_result_from_head` checks `git log -1` for a subject prefixed with the task ID. If found, the daemon synthesizes a `complete` result and emits `task_implicit_commit` with reason `status_unknown` / `timeout_recovered` / `error_recovered`. This was load-bearing for stoch's TB-59.

**Task in Active when the daemon crashes** — `_recover_orphans` runs at startup, moves any Active task back to Ready, increments its retry counter, and emits `orphan_recovery`. Without this, a crashed daemon would leave its in-flight task wedged.

**Failing task that retry-exhausts** — bumps to Frozen via `move_to_frozen`. Operator unfreezes with `ap2 unfreeze TB-N`, which atomically moves to Backlog and resets the retry counter inside the same `locked_board()`.

**Daemon goes silent for >3h** — the watchdog (`_maybe_auto_diagnose`) builds a `DiagnoseReport` (board summary + recent failures + cron staleness + board health), renders it as Mattermost-friendly markdown, and posts to `AP2_MM_CHANNELS[0]`. Cooldown 6h. Skips when no MM destination is configured (sticky one-shot warning so it doesn't spam).

**Stuck-blocker** — `Board._is_blocker_satisfied` checks each `(blocked on: ...)` token. `TB-N` blockers are satisfied when the named task is in Complete; `pid:N@TS` blockers go through `pipelines.is_blocking`; unknown schemes fail-safe. `diagnose.board_health["unsatisfiable_blocks"]` surfaces the corner case where a Backlog task is blocked on a Frozen task (will never auto-promote).

**Malformed task line** — `Board._parse` flags any line that doesn't match `TASK_LINE_RE`; the daemon emits a deduped `board_malformed_line` event in step 3 of `_tick`. Without this, an out-of-band edit (e.g. a `(<sha>)` annotation between `**TB-N**` and `**Title**`) silently strands every task that depends on the affected one.

## Sandbox model

`ap2 sandbox` creates a separate OS user (default `claude-agent`) with:
- Its own home directory (`/Users/claude-agent` on macOS).
- A NOPASSWD sudoers grant for the human user to run `ap2 sandbox …` and `sudo -u claude-agent -i …`.
- Its own `.claude/` config tree (statusline, settings, OAuth token in `~/.zshenv`).
- `~/repos/<project>/` clones of each managed project.

The daemon runs as `claude-agent`. Its tools can't reach the human's `~/.ssh`, keychain, git config, or other repos. Mattermost creds and Anthropic OAuth token live in `~claude-agent/.zshenv` so non-GUI shells (the daemon's `Popen`) get them — the macOS keychain is locked for non-GUI sessions, so token-via-keychain doesn't work.

Per-project Mattermost channel routing lives in `<project>/.cc-autopilot/env` (`AP2_MM_CHANNELS=<id>`), so different projects post to different channels without polluting `~/.zshenv` with project-specific config.

## Continuity & evolution

The daemon is intentionally not transactional across ticks. State files are point-in-time snapshots; recovery is "do the right thing on the next tick." Examples:

- A daemon restart mid-task → orphan recovery on startup; the task gets retried.
- A pipeline that died while the daemon was off → the validation task auto-promotes on the next tick (the `pid:N@TS` blocker resolves the moment `is_blocking` returns False).
- A cron run that crashed mid-run → next tick checks `cron_state.json` and re-fires when due.
- An ideation run that crashed before writing `ideation_state.md` → cooldown still advances (`mark_run` always fires) so the broken agent doesn't get hammered every tick. Operator can `ap2 logs` to see `ideation_error` and decide whether to manually retry.

This convergence model — every tick is idempotent and corrective — is why the daemon can run for weeks without operator attention and self-heal from most local failures.

## Tests

`uv run pytest -q ap2/tests` runs the suite (312 tests as of TB-98). Notable test files:
- `tests/test_board.py` — TASK_LINE_RE, malformed-line detection, blocked_on parsing.
- `tests/test_cron.py` / `test_cron_defaults.py` — cron yaml parsing + bootstrapped jobs.
- `tests/test_ideation_defaults.py` — pins on `ideation.default.md` content (Step 0 / Step 0.5 / Step 1.5 phrases — these are load-bearing for ideation behavior).
- `tests/test_verify.py` / `test_briefing.py` — per-task verification + last-`## Verification`-section parsing.
- `tests/test_diagnose.py` — watchdog report shape.
- `tests/test_pipelines.py` — `pid:N@TS` blocker semantics.
- `tests/e2e/test_single_tick.py` / `test_multi_tick_cron.py` / `test_pipeline.py` / `test_mattermost_cron.py` — full `_tick` exercises with `FakeSDK`.

The e2e tests use `FakeSDK` (`tests/e2e/_fakes.py`) — a programmable mock that responds to prompt substrings with canned message streams. Lets a single tick run through `run_task` / `run_cron` / `handle_message` deterministically without spawning a real subprocess.

## Reading order for new contributors

1. `ap2/README.md` — what it is, how to use it.
2. This file — why it's shaped this way.
3. `ap2/daemon.py` — `_tick` is the entry point; everything fans out from there.
4. `ap2/board.py` — the `Board` model and `locked_board` are the core data structure.
5. `ap2/tools.py` — the MCP tools are the only mutation surface; reading them tells you the system's full state-change vocabulary.
6. `ap2/ideation.default.md` — the load-bearing prompt that drives the only path that creates new work.

The `.cc-autopilot/tasks/*.md` briefings are per-task historical records of design decisions; reach for them when you want to understand why a specific feature exists, not how it works today.
