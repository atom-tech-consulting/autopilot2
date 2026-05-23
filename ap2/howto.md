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
of truth for what the project is for and when it's done. Three queue-time
validators key off its content (and the briefing's prose), so both the
section shape and the prose substance are load-bearing:

- **TB-161 anchor validator** — every briefing's `## Goal` body must cite
  (as a substring) text from goal.md's `## Current focus` or `## Done when`
  headings/bullets. `_goal_md_anchors` mines anchors only from those two
  sections; reword them so meaningful citations are possible.
- **TB-164 Why-now validator** — independent of goal.md content; checks
  the briefing itself has a `Why now:` line. goal.md doesn't need its own
  Why-now section.
- **TB-235 dependency-coherence validator (LLM judge, Haiku-4.5)** —
  reads the briefing's prose (Scope / Design / Why now / description)
  and asks a judge to identify any hard predecessors (other tasks
  whose work must be on disk before this task's agent can run). Any
  judge-named TB-N missing from the task's `@blocked:` codespan
  rejects the briefing with a message naming the missing dependency.
  Fail-open on judge timeout / SDK error (logs a
  `validator_judge_{timeout,fail}` event and lets the briefing
  through — refusing to gate on transient infra failures is the load-
  bearing trade-off; the cron status-report surfaces a climbing skip
  rate). Hard off-switch: `AP2_VALIDATOR_JUDGE_DISABLED=1`. Briefing
  authors: if your prose names another TB-N's artifact as a
  precondition (a module, env knob, schema field), declare
  `--blocked TB-N` on the `ap2 add` invocation so the codespan
  matches what the prose claims.

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

### Prose-judge diagnostics

The prose judge (`ap2/verify.py::_judge_prose_bullet`) emits a
`judge_call` event for every call (TB-157) carrying usage / cost /
verdict. TB-236 extended that event with prevention- and observability-
fields so silently-skipped prose bullets under `AP2_AUTO_APPROVE=1` are
no longer invisible:

- **Prompt constraint (prevention).** The judge prompt now caps the
  rationale at ≤200 characters and is explicit that the FINAL message
  must be a JSON object only (no markdown fences, no preamble, no
  trailing prose). Intermediate `Read` / `Grep` / `Glob` tool calls are
  unconstrained — only the last message is.
- **`response_length`** (always present on every `judge_call`). Length
  in characters of the judge's final assistant text. Lets operators
  watch the prompt-tightening effect over time.
- **`rationale_length`** (present on successful parse). Length of the
  extracted `rationale` field. If this drifts above ~200 over a week
  the prompt constraint is slipping and either the model is ignoring it
  or the prompt rewrite lost the cap.
- **`parse_error`** (present on parse failure). One of:
  - `no_json_object` — response had no `{` / `}` at all.
  - `trailing_prose_after_json` — `{...}` parses cleanly but non-
    whitespace follows the closing brace (judge added commentary).
  - `unescaped_in_string` — usually an unescaped `"` or `\` inside a
    string value.
  - `json_truncated` — response cut off mid-string-value.
  - `parse_error_other` — catch-all.
  The full enum lives in `ap2/verify.py::PARSE_ERROR_CATEGORIES`.
- **`judge_response_dump`** (present on parse failure). Absolute path to
  the per-bullet dump file at
  `.cc-autopilot/debug/<run_ts>-<task>-judge-bullet<idx>-response.txt`.
  The file holds the FULL raw last-assistant-text — not the 200-char
  preview the event's `notes` field carries. Open it when you need to
  see what the judge actually emitted (unescaped backticks, prose
  preamble, etc.). Successful judge parses leave no dump on disk; the
  field is absent on those events.

Pattern-detection workflow:

```
ap2 events tail -n 500 | jq 'select(.type=="judge_call" and .parse_error)'
```

Counts by category, last 24h:

```
ap2 events tail -n 2000 | jq -r 'select(.type=="judge_call") | .parse_error // "ok"' | sort | uniq -c
```

Open the worst-offender dump:

```
ap2 events tail -n 500 | jq -r 'select(.type=="judge_call" and .judge_response_dump) | .judge_response_dump' | tail -1 | xargs cat
```

Failure recovery for prose-judge parse failures stays soft-pass:
`verification_partial` → Complete (per the existing aggregator). The
fields above don't change that policy — they just make the partials
diagnosable rather than silent. If a single category dominates (e.g.
`unescaped_in_string` >50% of failures over a week), the appropriate
follow-up is a TB to either tighten the prompt further or harden the
parser — informed by the dump files instead of guessing.

## Authoring `## Verification` bullets (briefing convention)

Bullets in a briefing's `## Verification` section are the per-task gate's
input — the daemon parses them into one of three kinds and dispatches
each: **shell** (run via subprocess; exit 0 = pass), **prose** (judged by
SDK against the cumulative task diff + working tree), or **malformed**
(classifier-detected unrecoverable shape; recorded as fail). The
classifier in `ap2/verify.py::parse_verification_section` (TB-219) decides
the kind from the bullet's markdown shape. Four pitfalls have caused
n=4 retry cascades in the 2026-05-12 → 2026-05-13 window alone
(TB-204/TB-206/TB-207/TB-209). The conventions below close every one.

### Prose bullets — use the `Prose:` prefix for explicit classification

Prose bullets that DON'T lead with a backtick-fenced token (e.g.
`- the new feature is documented in CLAUDE.md`) classify as prose
automatically. Prose bullets that DO lead with a backtick-fenced subject
(e.g. `- ``ap2/tests/test_x.py`` exists with the expected fixture`)
would otherwise classify as shell — and the verifier would try to exec
the bare path. To force prose classification, prefix the post-codespan
text with the literal token `Prose:` (case-sensitive, single colon):

> `` `ap2/tests/test_x.py` Prose: the file includes the expected
> `_COVERAGE_DRIFT_EXEMPT_SURFACES` fixture; judge confirms via Read.``

The `Prose:` prefix is a hard override — it wins over every other
classifier signal. Operators have been writing the convention organically
since the TB-206/207/209 fix briefings; TB-219 codified it.

A heuristic fallback also routes codespan-leading bullets to prose if the
bullet text contains any of the phrases in
`ap2/verify.py::JUDGE_INDICATOR_PHRASES` (e.g. `Judge confirms`,
`judged via`). It's a safety net for briefings that don't use the
`Prose:` prefix; the prefix is the canonical signal — reach for it first.

### Shell bullets — four authoring pitfalls

1. **No literal backticks in the command body.** Markdown's
   single-backtick codespan cannot represent a literal backtick — mistune
   truncates the codespan at the inner backtick and the rest of the
   command leaks into the bullet's prose body. Workarounds:
   - If the literal backtick is part of a regex pattern, replace it with
     the regex any-char `.` (e.g. `'^\| .pat'` instead of
     `'^\| `pat'`). This is the simplest fix and what TB-207's operator
     post-mortem ships.
   - If the literal backtick is genuinely required, wrap the codespan
     with **double backticks**: `` `` `cmd-with-`backtick`-in-it` `` ``.
     Mistune preserves the inner backtick under double-backtick wrapping.
   - The TB-219 classifier detects the broken single-backtick shape and
     emits `kind="malformed"` rather than silently exec'ing a truncated
     half-command, so a slip-up here surfaces as a verification fail
     with a rewrite suggestion in the event payload.
2. **Absence-check shell bullets must use the `!` exit-inversion prefix.**
   `grep "absent string" file` exits 1 when the string is absent, which
   the verifier reads as a FAIL. The intent is the inverse: pass iff
   absent. Use bash's exit-status negation: `! grep "absent string" file`
   passes when `grep` exits non-zero (string not found) and fails when
   `grep` exits 0 (string found — the absence claim is violated).
3. **Directory-walking grep must use `-r`.** `grep -lE 'pat' dir/` exits
   2 with "Is a directory" because plain `grep` is a file-only matcher.
   The bullet looks correct but always fails at runtime. Use `grep -rlE
   'pat' dir/` (or pre-list files via `find dir/ -type f`).
4. **`Prose:` prefix for judge bullets.** Covered above — the
   complement to the three shell pitfalls. If a bullet's grammatical
   subject is a backtick-fenced filename / symbol and the rest is a
   claim to judge against the diff, lead the suffix with `Prose:`.

A worked example combining all four:

```
## Verification

- `uv run pytest -q ap2/tests/` — full suite green (the canonical happy-path bullet).
- `! grep "deprecated_symbol" ap2/` — the symbol is gone (absence check; `!` is required).
- `grep -rlE 'pat' ap2/` — directory walk needs `-r` (file-only without it).
- `[ "$(grep -rcE '^| .pat' ap2/cli.py)" -ge 1 ]` — regex pattern; `.` substitutes for a literal backtick the codespan couldn't represent.
- `ap2/tests/test_new.py` Prose: the new test asserts on the documented fixture set; judge confirms via Read.
- `ap2/howto.md` Prose: the new convention section names all four pitfalls. Judge confirms via Read.
```

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
| `ap2 classify TB-N --impact VERDICT [--reason TEXT]` | Record the operator's retrospective impact verdict (`advanced-goal` / `pro-forma` / `negative` / `unclear`) on a shipped proposal (TB-189 / TB-251). | Captures whether the task substantively moved the goal forward, merely satisfied validators (goal.md L66-76's failure mode), or actively regressed the codebase; reasons feed TB-188 per-proposal records and `operator_log.md` so future ideation cycles can learn which proposal shapes actually pay off (and which to strongly avoid). See `## Classify verdicts` below for the `pro-forma` vs `negative` distinction. |
| `ap2 audit [--interactive] [--json] [--since ISO] [--frozen-only \| --auto-approved-only]` | Retrospective walk through unreviewed Complete + Frozen tasks since the last `ap2 audit` cursor (TB-248). | The "I just came back from a week away" verb under `AP2_AUTO_APPROVE=1` — closes the retrospective review surface gap auto-approve opens. State derivation is grep over `operator_log.md` (no new state file); `--interactive` walks one task at a time with `[c]lassify / [s]kip / [n]ext / [q]uit` prompts. See `## Retrospective audit workflow` below. TB-258 wires the unreviewed-count onto the natural-cadence return surfaces: `ap2 status` carries an `audit: N unreviewed since <ts>` line (text, omitted when N=0) + an always-present `audit` block in `--json`; the 2h cron status-report Mattermost post carries a `*Retrospective audit (unreviewed shipped):*` sub-block (omitted when N=0). Walk-away operators see the count without running `ap2 audit` first. |
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
| `ap2 sandbox user-setup [USER] [-y] [--skip-token] [--skip-statusline] [--mm-url/--mm-token]` | Create the sandbox user (prompts before running sudo). | One-time per machine; pairs with `install-token` / `sync-assets` / `install-mm` to fill in creds + per-user config (TB-276 folded the prior `install-howto` step into `sync-assets`). Skip flags exist for partial setups. |
| `ap2 sandbox install-token [USER] [--token-env VAR]` | Install `CLAUDE_CODE_OAUTH_TOKEN` into `~<user>/.zshenv`. | Run after `claude setup-token`; the daemon refuses to start without the token in its env (TB-79), and the macOS keychain is locked for non-GUI shells so token-via-keychain doesn't work. |
| `ap2 sandbox install-statusline [USER]` | Copy `hooks/statusline-command.sh` into `~<user>/.claude/` and wire it into the per-user `settings.json`. | Convenience for matching the human's statusline customization on the sandbox user; purely cosmetic for the daemon itself. |
| `ap2 sandbox install-mm [USER] [--mm-url/--mm-token]` | Install `MATTERMOST_URL` + `MATTERMOST_TOKEN` into `~<user>/.zshenv`. | Optional — only needed if the project wants the daemon's Mattermost loop active (poll mentions, post status reports, route `@claude-bot` chat verbs). |
| `ap2 sandbox project-setup SOURCE [--user U] [-y] [--mm-channel N] [--git-name N] [--git-email E]` | Clone `<source>` into `~<user>/repos/` with repo-local git identity set. | The "transfer this project to the sandbox" verb; pair with `--mm-channel` to wire the per-project channel routing in one step, or fall back to `install-channel` after the fact. |
| `ap2 sandbox install-channel PROJECT CHANNEL [--user U]` | Resolve a Mattermost channel name to an ID and write `AP2_MM_CHANNELS` into `<project>/.cc-autopilot/env`. | Run after `project-setup` if you skipped `--mm-channel` then; idempotent overwrite. |
| `ap2 sandbox project-audit PATH [--user U]` | Verify an isolated project clone is well-formed — ownership, git identity, env file. | The diagnostic for "did `project-setup` finish correctly?" — catches half-completed setups before they confuse `ap2 doctor` later. |
| `ap2 sandbox sync-assets [USER] [--sbuser] [--apply] [--dest DIR]` | Deploy BOTH `<repo>/skills/*` AND `ap2/howto.md` into a target `~/.claude/` (TB-276 unified the prior `sync-skills` + `install-howto` split). | Default is a dry-run drift summary; pass `--apply` to copy. Default mode `sudo`s as a positional sandbox user; `--sbuser` writes to the CURRENT user's `$HOME/.claude/` without sudo (the path a Claude session already running as the sandbox user — which lacks sudoer privileges — takes to refresh its own assets). |

## Retrospective audit workflow

The `ap2 audit` verb (TB-248) is the operator's retrospective review
surface — the "I just came back from a week of unattended operation,
what shipped and what's worth a verdict?" path. It's distinct from
queue-time approval (`ap2 approve` / `ap2 reject` for ideation
proposals), distinct from per-task classification (`ap2 classify` for
shipped-task impact verdicts), and complementary to both: `ap2 audit`
surfaces the list of unreviewed-shipped tasks since the last walk and
routes per-task decisions back through those existing verbs.

**When to reach for it.** Under `AP2_AUTO_APPROVE=1` (the focus item's
unblock-condition for the walk-away promise), every auto-approved task
ships without operator-in-the-loop review at dispatch time, so
retrospective review is the operator's ONLY judgment surface. Without
`ap2 audit`, the surface is fragmented across `ap2 status`,
`ap2 logs --since`, Mattermost scrollback, `git log`, and per-task
`ap2 classify` invocations — none consolidated; none answering "which
tasks have I NOT yet reviewed?" directly. `ap2 audit` consolidates the
five-place pull into one verb with a coherent cursor and reviewed-set.

**Default invocation — the consolidated table.** `ap2 audit` (no
flags) prints a table of every unreviewed Complete + Frozen task since
the most recent `<ts> — ran audit (...)` line in operator_log.md, in
chronological completion order (oldest first). Columns: TB-ID, status,
commit, auto_approved flag, one-line summary, completed_at. After the
table the command appends a `ran audit (N unreviewed)` line to
operator_log.md via the operator queue (the existing `ack` op-shape
with a structured note — no new op-shape per the briefing's
op-shape-proliferation guard) so the next invocation's cursor advances
past this walk's completion timestamp.

**`--interactive` — per-task walkthrough.** Walks the unreviewed list
one task at a time, displaying the full task summary + auto-approved
status + briefing path. Per-task prompt:

    [c]lassify | [s]kip | [n]ext | [q]uit

- `c` — sub-prompt for `--impact <verdict>` (must be one of
  `advanced-goal` / `pro-forma` / `negative` / `unclear` per
  `IMPACT_VERDICTS`; TB-251 added `negative` as the actively-harmful
  bucket distinct from `pro-forma`'s neutral-no-impact — see
  `## Classify verdicts` below) + optional reason; queues
  `ap2 classify` through the operator queue. Reuses the existing
  TB-189 classify path so the per-proposal record's `impact` block
  lands alongside the operator_log line.
- `s` — sub-prompt for an optional skip reason; queues the new
  `audit_skip` operator-queue op-shape. The drain handler appends
  `<ts> — audit-skipped TB-N: <reason>` to operator_log.md and emits
  a `task_audit_skipped` event. Distinct from `classify`: the operator
  considered the task and chose NOT to record a verdict (vs. the
  pre-audit "operator hasn't looked yet" state). Future audit walks
  treat `audit-skipped` as "reviewed" — the task won't re-surface
  unless the operator explicitly `--since`-rewinds.
- `n` — advance to next task without recording anything (operator
  wants to think about this one later; the task stays in the
  unreviewed set on the next walk).
- `q` — exit the walk and record a `ran audit (reviewed M, skipped K,
  deferred L)` cursor line so the next walk's cursor sits at the
  end-of-walk timestamp.

**Rollback intentionally out-of-scope.** A `[r]ollback` action is
deliberately NOT in the first iteration — see the briefing's Out-of-
scope §1 for why (the rollback shape question — walk-back-N vs.
rollback-this-specific-TB vs. revert-and-classify-as-pro-forma — is
non-obvious and deserves its own TB after `ap2 audit` lands and
operator-engagement reveals which shape is wanted). The operator can
still `ap2 rollback` outside the audit walk; the audit just doesn't
have a one-keystroke shortcut for it yet.

**State derivation (no new state file).** The audit cursor + reviewed-
set both come from grep over `.cc-autopilot/operator_log.md`:

- **Cursor (last-audit-ts)**: most recent line matching
  `^- (\S+) — ran audit \(.*\)$`. When no such line exists (first-ever
  invocation), cursor defaults to the beginning of time — all shipped
  tasks are listed.
- **Reviewed set**: union of (a) tasks with a
  `<ts> — classified TB-N impact=...` line (TB-189 classify writer),
  (b) tasks with a `<ts> — audit-skipped TB-N: ...` line (TB-248
  audit-skip writer), (c) tasks with a `<ts> — rejected ideation
  proposal → TB-N` line (TB-152 reject writer — counted as reviewed
  because the operator made an explicit decision).
- **Unreviewed set**: tasks in TASKS.md's Complete + Frozen sections
  with `task_complete` timestamps strictly greater than the cursor,
  minus the reviewed set.

The design promise — no new state file — buys two things: (1) no sync
question between operator_log.md and a hypothetical audit-state
sidecar (if the sidecar says reviewed but the log doesn't, who wins?),
and (2) the grep cost is trivial because operator_log.md stays
single-digit MB at multi-year scale (ideation already reads it every
cycle).

**Filter flags.**

- `--since <iso-date>` — override the natural cursor. Useful for
  "re-review tasks from last month" sweeps.
- `--frozen-only` — restrict to Frozen tasks. Frozen tasks are the
  highest-signal review candidates (they've already cost agent attempts
  and operator attention); operator may want to triage the freeze pile
  separately from the Complete-task review.
- `--auto-approved-only` — restrict to tasks the daemon auto-promoted
  via the `AP2_AUTO_APPROVE` path (identified by an `auto_approved`
  event in events.jsonl). The natural filter for the after-walk-away
  workflow: shows specifically what shipped without operator review
  at dispatch time. Pair this with `ap2 audit --interactive
  --auto-approved-only` for the canonical "I was gone, what did the
  loop choose to ship?" walk.

**`--json` output.** Machine-readable shape for scripting / external
dashboards consuming `ap2 audit --json`. Top-level dict with `cursor`
(the last-audit-ts string), `filter` (which restriction flags were
active), and `unreviewed` (a list mirroring `UnreviewedTask`'s
dataclass fields: task_id, status, commit, auto_approved, summary,
completed_at, briefing_path).

**No direct writes.** `ap2 audit` itself WRITES nothing to disk
directly — every mutation routes through `do_operator_queue_append`
(the cursor line via the `ack` op-shape; the `[s]kip` action via the
new `audit_skip` op-shape; the `[c]lassify` action via the existing
`classify` op-shape). This preserves the daemon-vs-CLI race
serialization the operator queue exists for and keeps operator_log.md
under a single writer at any moment (the drain holds `board_file_lock`).

**Natural-cadence return surfaces (TB-258).** TB-248 ships the PULL
surface — the operator runs `ap2 audit` to see the unreviewed pile.
TB-258 closes the push-vs-pull parity gap by wiring the same
unreviewed-count onto the two natural-cadence return surfaces the
walk-away operator hits without thinking: (a) `ap2 status` text
mode prints an `audit: N unreviewed since <cursor-ts> — `ap2 audit``
line in the operator-attention cluster (after `decisions needed`,
before `auto-approve:`); omit-on-empty so fresh / fully-reviewed
projects stay silent; (b) `ap2 status --json` ALWAYS carries an
`audit: {unreviewed_count, cursor_ts}` block (parser-stability
mirror of the `auto_approve` block); (c) the 2h status-report
Mattermost cron post carries a `*Retrospective audit (unreviewed
shipped):*` sub-block with the count + cursor + `ap2 audit` nudge;
also omit-on-empty so quiet windows stay byte-identical to the
pre-TB-258 baseline. Pure read-layer composition over the existing
`audit.list_unreviewed` + `audit.parse_audit_cursor` helpers — no
new state file, no daemon-side changes, no new env knobs. Mirrors
the wrap-helper-into-status-extras pattern shipped across prior
axis-parity tasks (TB-241 / TB-242 / TB-244). The count is window-
independent (cursor-based, not 24h-rolling) so a multi-day audit
pile surfaces on every report until cleared.

## Classify verdicts

`ap2 classify TB-N --impact <verdict>` accepts one of four values from
`IMPACT_VERDICTS` (single source of truth at `ap2/briefing_validators.py`; still importable via `ap2.tools.IMPACT_VERDICTS` thanks to TB-262's re-export). The four
buckets form a gradient — substantive-positive → compliance-neutral →
actively-harmful — with `unclear` as the explicit "can't tell yet"
bucket. Pick the verdict by running two delete-tests in sequence:

- **`advanced-goal`** — substantively advanced the goal (positive).
  Passes the base delete-test: "if we deleted this task, would the
  goal still ship?" Answer: no — the goal would be visibly worse off
  without this work. Use when the task moved the focus's Done-when
  criteria closer, unblocked a downstream task, or shipped a
  user-visible capability the goal names.

- **`pro-forma`** — goal-shaped but didn't advance — compliance signal
  (no-impact + no-harm). Fails the base delete-test: deleting this
  task would leave the goal in the same place. But also passes the
  stronger delete-test below: deleting it wouldn't make the codebase
  BETTER either — it just sat there, goal-shaped, satisfying
  validators without moving the needle. Use when the task satisfied
  its briefing on paper but the operator can't point to where the
  goal moved (goal.md L66-76's named failure mode).

- **`negative`** — actively regressed something OR made the codebase
  worse (no-impact + harm). Fails BOTH the base delete-test AND the
  stronger delete-test: "if we deleted this work, would the codebase
  be BETTER, not just neutral?" Yes → `negative`. Use when a
  regression slipped through, test coverage was inadvertently
  weakened, a refactor landed but increased complexity beyond the
  briefing's intent, or some other codebase-WORSE outcome — the kind
  of shape ideation should strongly avoid proposing again. The load-
  bearing distinction from `pro-forma` is the harm dimension:
  `pro-forma` is "neutral, didn't help"; `negative` is "neutral on
  the goal AND made the codebase worse."

- **`unclear`** — impact not yet legible (uncertain — defer). Use
  when the operator can't honestly answer either delete-test yet —
  the work is too recent, depends on downstream behavior that hasn't
  shipped, or surfaces a question rather than a verdict. Distinct
  from skipping (`ap2 audit [s]kip`): `unclear` records that you
  looked AND decided you can't decide; skip records that you didn't
  decide. Re-classify later when the impact becomes legible.

The `pro-forma` ↔ `negative` distinction (TB-251) is the load-bearing
new signal: under `AP2_AUTO_APPROVE=1` the classify stream is the
primary judgment surface for ideation prompt-tuning, and collapsing
"neutral-but-low-value" and "actively-harmful" into one bucket loses
the signal ideation needs to strongly avoid harmful shapes vs merely
de-prioritize compliance-shaped ones. When in doubt between the two,
ask: "after this shipped, was the codebase in a strictly worse state
than before? (regressed test, weakened invariant, accreted
complexity)" — if yes, `negative`; if no, `pro-forma`.

Historical classifications stand — TB-251 did not backfill prior
`pro-forma` records as `negative`. Future classifications use the
richer vocabulary.

## Event schema (the canonical timeline)

`.cc-autopilot/events.jsonl` is append-only. Every line has `ts` (UTC
ISO-8601) + `type`; other fields vary. Categories:

**Lifecycle.** `daemon_start`, `daemon_stop`, `daemon_pause`,
`daemon_resume`, `task_start`, `task_complete`, `cron_start`,
`cron_complete`, `cron_skipped` (status-report no-op — carries a
`reason` field naming which gate fired:
`reason=no_activity_since_last_report` (TB-128/153 — the inter-report
window carries zero "interesting" events past the previous
`cron_complete name=status-report`); `reason=duplicate_content`
(TB-281 — events DID land but the prospective post is structurally
identical to the last one, per the SHA-1 fingerprint stashed under
`status-report.last_post_fingerprint` in `cron_state.json` over
board counts + pending-review TB-Ns + decisions-needed bullets +
digest sub-section contents + halt reason; closes goal.md focus-1's
Done-when bullet "no two consecutive reports repeat unchanged
content")), `cron_bootstrap` (first-run
seeding of `cron.yaml` from `cron.default.yaml`), `ideation_empty_board`
(skip — no slots OR cooldown), `ideation_forced` (operator forced via
`ap2 ideate --force`), `ideation_skipped` / `ideation_skipped_no_slots`,
`ideation_complete`, `ideation_state_updated`, `web_start`, `web_stop`,
`env_reloaded` (TB-271 — daemon `_tick` re-sourced `.cc-autopilot/env`
at tick-top and detected at least one knob whose value changed; payload
`changed` / `hot` / `fixed` / `other` knob lists; mutates the tunable
`Config` dataclass fields in-place AND overwrites `os.environ` for
file-sourced keys while preserving "shell export wins" for keys never
set by the file; removes the restart-to-apply-a-knob friction TB-260
only warned about; mtime-gated so a static env file is a cheap no-op
each tick — see `## Configuration knobs` for the hot-reloadable vs
fixed split).
Per-run cost/usage: `task_run_usage` (per task agent run, TB-180),
`control_run_usage` (per cron / ideation / MM-handler run, TB-179),
`judge_call` (per per-task-verifier prose-bullet judge invocation,
TB-69 + TB-181). Verifier per-run wall-clock: `verify_passed` (TB-252
— project-wide `AP2_VERIFY_CMD` ran to completion AND exited zero;
payload mirrors `verification_failed` shape (task, command,
exit_code, duration_s); consumed by `verify_timeout_audit` to size
`AP2_VERIFY_TIMEOUT_S` against observed-typical successful run
duration).

**Failure.** `task_error`, `task_timeout`, `task_state_violation` (TB-110
post-hoc fenced-file check tripped), `task_rollback` (TB-110
rollback to pre-task state), `verification_failed` (per-task or
project-wide), `verification_partial`, `retry_exhausted`,
`cron_error`, `cron_timeout`, `ideation_error`, `ideation_timeout`,
`mattermost_error`, `mattermost_timeout`, `mm_poll_error`,
`env_reload_error` (TB-271 — `env_reload.maybe_reload_env` raised at
tick-top; swallowed defensively so the rest of the tick continues on
whatever cfg state survived; payload `error=<ExceptionType>: <message>`),
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
/ `ideation_proposal_reconciled` (TB-188 per-proposal audit trail),
`auto_approved` (TB-223 — ideation omitted the `@blocked:review` codespan
on a proposed task because `AP2_AUTO_APPROVE` is on and the task carries
no `AP2_AUTO_APPROVE_GATE_TAGS` tag; `knob=` payload field captures the
env value at proposal time so the forensic trail survives env changes
during the daemon's lifetime), `would_auto_approve` (TB-232 monitor-only
dry-run sibling — fires at proposal time when both `AP2_AUTO_APPROVE=1`
AND `AP2_AUTO_APPROVE_DRY_RUN=1` and the tags gate would have stripped
`@blocked:review`; payload `task`, `knob`, `dry_run=true`; the codespan
is preserved so operator-manual `ap2 approve` is still required),
`auto_approve_paused` (TB-223 —
cumulative-regression circuit-breaker tripped; auto-promote of
auto-approved tasks halted until operator emits `ap2 ack
auto_approve_unfreeze`), `auto_unfreeze_applied` (TB-225 —
agent-diagnosed briefing-shape fix from a `BriefingFix:` prefix was
auto-applied to a Frozen task; payload `task`, `shape`, `from`, `to`),
`auto_unfreeze_skipped` (TB-225 — auto-unfreeze attempt refused at
one of the layered guards; payload `task` + `reason` token, where
reason is one of `shape_not_in_allowlist`, `briefing_mismatch`,
`briefing_path_missing`, `per_task_cap`, `per_day_cap`, `queue_error`,
`sweep_error`), `would_auto_unfreeze` (TB-233 monitor-only dry-run
sibling of `auto_unfreeze_applied` — fires when both
`AP2_AUTO_UNFREEZE_FIX_SHAPES` and `AP2_AUTO_UNFREEZE_DRY_RUN=1` are
set and the full guard chain would have passed; payload `task`,
`shape`, `file`, `line`, `from`, `to`; the briefing file is NOT
mutated and no operator-queue ops are appended).

**Briefing-validator LLM judge (TB-235).** `validator_judge_timeout`
and `validator_judge_fail` are fail-open audit events from check #7
in `briefing_validators._validate_briefing_structure` (LLM-driven
dependency-coherence judge; TB-262 split this out of `tools.py`). They fire when the Haiku-4.5 judge call exceeds
`AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default 60s; TB-269 calibration)
or fails for any other reason (network, parse error, model
unavailable). The validator's policy on judge failure is fail-open
— refusing to gate `ap2 add` / `ap2 update` on a transient Anthropic
API hiccup is the load-bearing trade-off — so each skipped call
lands as an event for operator visibility. Payload:
`validator_judge_timeout` carries `timeout_s` + `error`;
`validator_judge_fail` carries `error` (the exception repr or
`"non-dict judge response"`). When `AP2_VALIDATOR_JUDGE_DISABLED=1`
is set, the check is skipped entirely and neither event fires
(clean bypass, not a fail-open).

`validator_judge_passed` (TB-269) is the successful sibling: emitted
when the SDK worker returns without timeout / SDK exception, BEFORE
the JSON parse, so the wall-clock distribution feeds the doctor's
`validator_judge_timeout_audit` surface (axis-1 mirror of TB-252's
`verify_timeout_audit`) regardless of whether the response parsed
cleanly. Payload: `duration_s`, `briefing_bytes`, `max_turns`,
`timeout_s`. Completes the happy-path / fail-open / timeout
triangle on a single event namespace.

TB-243 surfaces the rolling 24h counts of both event types on
`ap2 status` (text: a `validator-judge: N fail | M timeout (24h)`
sub-line under the `auto-approve:` block, omitted when both counts
are zero; JSON: a nested `auto_approve.validator_judge.{fail_count_24h,
timeout_count_24h}` object, always present) and on the web home
Automation card (a "Validator judge (24h)" row, omitted when both
counts are zero, warn-tinted amber when
`(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`, default
5). Closes the silent-degradation hazard the fail-open design
otherwise left for an operator with `AP2_AUTO_APPROVE=1`: 10
silently-timed-out judge calls used to take ≥2h (the next
status-report cron tick) to surface — now they appear on the
on-demand pull surfaces immediately.

TB-245 closes the push-surface half of the same observability gap:
the 2h status-report Mattermost cron post (operator's primary
walk-away channel) now also carries a
`*Validator-judge fail-open window (24h):*` sub-block listing the
same two 24h counts, with the same `[noisy]` suffix when
`(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default
5). Window is identical to TB-243's pull-surface 24h so the
operator never has to reconcile two different validator-judge
counts between `ap2 status` and the cron post. Sub-block is
omitted when both counts are zero (quiet windows stay
byte-identical to the pre-TB-245 baseline); both event types are
also listed in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` in
`ap2/status_report.py` so a lone fresh fail-open event keeps the
skip-gate from firing — operator never misses a degradation
signal because the 2h post coincided with an otherwise-quiet
window.

**Focus rotation (TB-226 axis 4).** `focus_advanced` and
`roadmap_complete` track the daemon's in-memory focus-list pointer
against goal.md's `## Current focus:` headings. See
`### Focus rotation (axis 4)` below for the full design.

- `focus_advanced` (TB-226) — daemon advanced its in-memory pointer
  past an exhausted `## Current focus:` heading. Trigger field is
  one of `done_when_judge` (LLM-judge ruled the focus's `Done when:`
  bullets substantively met) or `empty_cycles_heuristic` (focus had
  no explicit `Done when:` block; `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
  consecutive 0-proposal cycles tripped the fallback). Payload also
  carries `from` (old title), `to` (new title — empty string when
  the advance crossed the last focus), `new_index`, `total_foci`.
- `roadmap_complete` (TB-226; rescoped TB-275) — pointer crossed
  past the last `## Current focus:` heading; the ideation TRIGGER
  parks (`_maybe_ideate` emits `ideation_skipped reason=
  roadmap_complete`) until the operator extends the roadmap (`ap2
  update-goal`) or dismisses the notice (`ap2 ack
  roadmap_complete`). Task dispatch is NOT affected — already-
  queued Backlog tasks continue to drain. Use `ap2 pause` for an
  explicit full-stop. Payload: `exhausted_count`, `trigger`. Fired
  once per exhaustion episode (suppression via pointer's
  `roadmap_complete_emitted` flag).

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
`/ideation_state`, `/commits`, `/stats` pages. Useful when you want
to scan visually rather than ask the session to summarize.

## Stats dashboard

The `/stats` page (HTML, server-rendered, no JS) and `/stats.json`
endpoint (JSON, scripting-friendly) surface trend aggregates over
an operator-configurable window — the return-and-review surface for
multi-day walk-away cycles. URLs:

- `http://127.0.0.1:8730/stats` — human-readable dashboard.
- `http://127.0.0.1:8730/stats.json` — machine-readable contract.

`?window=` accepts `1d` / `7d` (default) / `30d`, plus arbitrary
`Nh` / `Nm` / `Nd` suffixes. Values are clamped to `[1h, 90d]` so a
typo doesn't either flood the events.jsonl scan or render an empty
page.

Metrics surfaced:

| Section | Metric |
|---|---|
| Tasks | total count, completion rate, avg/p50/p95 duration + num_turns, total + avg cost, top-10 longest, top-10 most expensive, duration-bucket histogram (≤1m / 1–5m / 5–15m / 15–30m / 30–60m / >60m), attempts-per-task histogram (1st-try / 2nd / 3rd / retry-exhausted), frozen rate |
| Per-bullet verifier | total prose-judge call count, avg/p50/p95 duration, top-10 slowest, validator-judge fail + timeout counts (window-bounded — `automation_status`'s `_24h` counters are the 24h-only sibling) |
| Ideation | cycle count, avg/p50/p95 duration + turns + cost, proposals recorded, proposals/cycle, rejection rate |
| Cron | per-job cycle count + avg duration + avg cost (auto-discovered by `control_run_usage label=cron-*`) |

**What to look for during walk-away review**: rising avg cost or
p95 duration relative to a prior week is the silent-overhead-creep
signal TB-235 (the LLM-judge regression that quintupled test-suite
runtime; see `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md`)
would have surfaced earlier. Climbing frozen-rate or
validator-judge-fail counts indicate gate erosion. Climbing top-10-
most-expensive against a fairly stable top-10-longest indicates
silent token spend per turn — likely a model regression or prompt
bloat.

The JSON contract is the stable interface; HTML layout can change
without breaking scripted consumers. Top-level shape:

```json
{
  "window": "7d",
  "window_s": 604800,
  "computed_at": "2026-05-18T16:42:00Z",
  "tasks":    {...},
  "verifier": {...},
  "ideation": {...},
  "cron":     {...}
}
```

**Status-report push surface (TB-259).** The 2h status-report
Mattermost cron post (operator's primary walk-away channel) also
carries a top-line digest of the same aggregates as a
`*Stats window aggregates (<window>):*` sub-block — three bullets
summarizing task completions (with p50/p95 duration), ideation
cycles + proposals, and bullet-judge evaluations + fail-open count
over the inter-report window. Closes the push-vs-pull parity gap
TB-255 left open: the dashboard pays rent only during active
operator sessions, but the walk-away promise (goal.md L28-30
"walk away for a week without intervention") needs the digest to
land without the operator opening a browser tab. Window is scoped
to "now - last status-report cron_complete ts" so the sub-block
matches the inter-report window the TB-228 / TB-244 / TB-245 /
TB-258 sub-blocks above it scope against; falls back to 24h when
no parseable previous-report ts exists (first-ever run, or the
previous one rolled out of the tail). Omit-on-empty: the sub-block
is suppressed when the window's task-completion count is zero, so
quiet windows stay byte-identical to the pre-TB-259 baseline and
the `/stats` pull surface still renders the full zero-state
dashboard for operators who load it directly. Mirrors the
wrap-helper-into-state-extras pattern shipped across prior
axis-parity tasks (TB-241 / TB-242 / TB-244 / TB-245 / TB-258).
Pure read-layer composition over the existing `collect_stats`
helper — no new aggregates, no new state file, no daemon-side
changes, no new env knobs.

## Configuration knobs

Set in shell, in `<project>/.cc-autopilot/env`, or in
`~claude-agent/.zshenv`. The full set the ap2 source consults
(`grep -nE 'AP2_[A-Z_]+' ap2/*.py` is the source-of-truth — the
`test_every_env_knob_documented` gate in `ap2/tests/test_docs_drift.py`
fails CI if a new knob is added and not listed here):

**Hot-reload vs restart (TB-271).** Most tunable knobs (timeouts,
max-turns, model/effort, auto-approve / auto-unfreeze thresholds,
verify gate, tick intervals, ideation knobs, watchdog thresholds)
hot-reload — the daemon re-sources `.cc-autopilot/env` at the top of
every `_tick`, refreshes the tunable `Config` fields in-place, and
overwrites `os.environ` for file-sourced keys. A bumped knob takes
effect on the next tick (≤30s) without `ap2 stop && ap2 start`. The
canonical set is `env_reload.HOT_RELOADABLE_KNOBS`; the reload emits
an `env_reloaded` event with the changed keys for the audit trail.
A small fixed-knob set (`env_reload.FIXED_KNOBS` — `AP2_WEB_PORT`,
`AP2_WEB_DISABLED`, `AP2_MM_CHANNELS`) still requires a restart:
each configures a stateful resource (a bound HTTP socket, a
subscribed MM channel set) wired up once at daemon-start and not
re-applied by the reload. TB-260's `WARN: .cc-autopilot/env modified
... ap2 stop && ap2 start` line persists for the fixed-knob set and
clears automatically after a hot-reload that only touched
hot-reloadable knobs. "Shell export wins" still holds for keys
never sourced from the file: a `export AP2_FOO=bar` in the
operator's shell takes precedence over a `AP2_FOO=baz` later added
to the file, even on reload (you'd need to either un-export and
restart, or set the value via the file before daemon-start).

**Loop cadence + per-run timeouts.**
- `AP2_TICK_S` (30) — main-loop tick interval.
- `AP2_MM_TICK_S` (10) — Mattermost polling tick interval (separate
  loop, TB-122).
- `AP2_TASK_TIMEOUT_S` (1200) — per-task SDK query timeout.
- `AP2_TASK_MAX_TURNS` (200) — max turns per task agent (raised from
  50 in TB-278 after TB-122 hit `error_max_turns` at 51 turns; this
  project's own env bumps further to 500 for heavy refactors).
- `AP2_CONTROL_TIMEOUT_S` (1200) — per-control-agent timeout (cron,
  ideation, MM handler). Raised from 300s in TB-278 — `xhigh`-effort
  ideation routinely blew the old 5-min wall.
- `AP2_CONTROL_MAX_TURNS` (15) — max turns per control agent (cron
  + MM handler share this default; ideation has its own).
- `AP2_IDEATION_MAX_TURNS` (100) — max turns for the ideation agent
  (raised from 30 in TB-278 after a goal.md rewrite mid-cycle hit
  `error_max_turns` at 31 turns; ideation's Step 0 / 0.5 / 1.5 chain
  runs deeper than other control jobs).
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
  `ap2 doctor` warns when set below observed-typical successful verify
  duration (TB-252; reads `verify_passed` events for the last 7 days
  or 20 samples, whichever is larger; uses `max()` of durations so the
  worst-case successful run sizes the recommendation).

**Briefing validator (LLM-judge dependency coherence, TB-235).** Check
#7 in `ap2/briefing_validators.py::_validate_briefing_structure` (TB-262
split out of `ap2/tools.py`) runs a Haiku-4.5
judge over a freshly-authored briefing AFTER the six deterministic
checks (TB-154 canonical sections, TB-91/TB-102 parseable Verification,
≥1 bullet, TB-161 goal-anchor, TB-164 Why-now, TB-171 no-Manual)
pass. The judge identifies "hard predecessors" the briefing's prose
names implicitly (e.g. "ap2/_shared.py must already exist — created
by the _locked extraction") and the validator rejects when any judge-
named TB-N is missing from the task's `@blocked:` codespan. Closes
the dependency-coherence hole that under `AP2_AUTO_APPROVE=1`
(TB-223) would let ideation auto-promote a task out of dispatch
order — TB-220's prose vs codespan mismatch is the canonical
historical instance. Fail-open by design: on judge timeout / parse
failure / SDK error the validator logs a `validator_judge_timeout`
or `validator_judge_fail` event and lets the briefing through (the
cron status-report surfaces a climbing skip rate so operators
notice). The check fires on both `do_operator_queue_append`
(primary surface — ideation, MM handler, operator CLI all hit it)
and `do_board_edit` (legacy direct-board-mutation path) for shape
symmetry.

- `AP2_VALIDATOR_JUDGE_DISABLED` — hard off-switch. When set to a
  truthy value (`1` / `true` / `yes`), check #7 is bypassed
  entirely and the validator falls back to the six deterministic
  checks. Operator escape hatch if the judge is causing false-
  positives during a specific workflow; the deterministic gates
  still fire so the briefing-shape contract is preserved.
- `AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default 60) — wall-clock timeout
  for the per-briefing judge call. Exceeded → log
  `validator_judge_timeout` event + skip the check. TB-269 bumped the
  default from 15 → 60 after the TB-257 investigation artifact
  (`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
  measured the real SDK call at 17.6-46.8s wall-clock — the previous
  20s ceiling (15s default + 5s outer-thread grace) sat below the
  median completion of even the smallest measured briefing, so the
  axis-1 dep-coherence gate was silently fail-open on essentially
  every operator queue-append. The doctor surface
  `validator_judge_timeout_audit` in `ap2/doctor.py` (TB-269; axis-1
  mirror of TB-252's `verify_timeout_audit`) closes the calibration-
  drift loop — it reads `validator_judge_passed` events from
  `.cc-autopilot/events.jsonl` and surfaces a WARN with a one-line
  fix recommendation if a future workload shift takes the observed-
  typical successful call duration back above the configured floor.
  TB-270 ships the complementary axis-1 lever the same artifact named
  as the secondary factor (`prompt-too-heavy`):
  `_slice_briefing_for_dep_judge(briefing_text)` in
  `ap2/validator_judge.py` narrows the user payload's
  `briefing_markdown` field to the briefing's `## Goal` + `## Scope`
  sections only (Design / Verification / Out-of-scope are bytes the
  judge wouldn't have used to change its hard-predecessor verdict).
  Shrinks typical input from ~6KB → ~1-2KB and the SDK wall-clock
  proportionally — independent of the timeout knob, so the two
  levers compound. Defensive fallback in the helper returns the full
  `briefing_text` on briefings missing either canonical heading or
  with empty section bodies, guaranteeing the judge is never blind
  on legacy / hand-edited shapes.
- `AP2_VALIDATOR_JUDGE_MAX_TURNS` (default 2) — TB-249 canonical
  budget knob. Bounds the judge's SDK turn count. The validator is a
  single-shot JSON-emitting judge: one assistant message (the verdict)
  + one optional tool call (Read/Grep) is plenty; `2` keeps the call
  bounded and the cost ≤$0.005 at Haiku rates. Mirrors the
  `AP2_VERIFY_JUDGE_MAX_TURNS` / `AP2_JANITOR_JUDGE_MAX_TURNS` knob
  pattern (the SDK's native budget primitive).
- `AP2_VALIDATOR_JUDGE_MAX_TOKENS` — **deprecated** alias kept for
  one-cycle backward compatibility (TB-249). If set AND
  `AP2_VALIDATOR_JUDGE_MAX_TURNS` is unset, the value is reused as
  `max_turns`, ceiling-capped at 5 (so a stale `500` from the pre-
  TB-249 default doesn't translate into a 500-turn runaway). Emits a
  one-shot-per-process `validator_judge_deprecated_knob` event the
  first time the alias resolves; a future TB removes the alias once
  operator engagement confirms no env files still carry it. Migration:
  rename to `AP2_VALIDATOR_JUDGE_MAX_TURNS` with a value in `[1, 5]`
  (default `2`) — the old `500` (output-token cap) translates to a
  turn budget of `5` after the cap.
- `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5) — TB-243 surface
  threshold. When the rolling 24h sum
  `validator_judge_fail_count_24h + validator_judge_timeout_count_24h`
  is ≥ this number, `ap2 status` appends ` [noisy]` to its
  `validator-judge:` sub-line and the web home Automation card's
  "Validator judge (24h)" row gets the warn-tint (amber). Below the
  threshold both surfaces stay in the neutral palette so a single
  transient SDK blip doesn't tint the card. Closes the silent-
  degradation hazard left by the fail-open design above: an
  operator with `AP2_AUTO_APPROVE=1` whose judge has been quietly
  timing out for the last N briefings sees the warn-tint before the
  next audit. Unset / empty / non-int / non-positive → default
  (matches the TB-224 / TB-234 token-cap parse semantics). TB-272
  promotes the same threshold to a load-bearing safety floor: the
  auto-approve dispatch path now pauses (emits
  `auto_approve_skipped reason=validator_judge_noisy`) when the
  rolling-24h sum crosses this threshold — see
  `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` below for the opt-out.
- `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED` — TB-272 opt-out for the
  validator-judge noisy-state auto-approve pause. **Unset by default
  → pause ACTIVE** (the safety-floor closure for the axis-1+3
  cross-cut hazard goal.md L82-88 names: the TB-235 dep-coherence
  judge that the auto-approve safety claim depends on can silently
  fail-open at high rate while `AP2_AUTO_APPROVE=1` continues
  stripping `@blocked:review` and dispatching ideation proposals).
  When the rolling 24h sum
  `validator_judge_fail_count_24h + validator_judge_timeout_count_24h`
  crosses `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5; TB-243),
  the daemon refuses to auto-promote `auto_approved` Backlog tasks
  and emits `auto_approve_skipped reason=validator_judge_noisy
  fail_count_24h=<N> timeout_count_24h=<M> threshold=<T>` per
  preempted promotion attempt. The pause-reason discriminator
  surfaces as `validator_judge_noisy` on the existing TB-227 `ap2
  status` text/JSON + web home Automation card + TB-228 cron
  status-report digest renderers (no new operator-facing surfaces).
  Resume: the rolling-24h window self-clears as old events age out,
  OR the operator runs `ap2 ack auto_approve_unfreeze` (same verb
  `consecutive_freezes` uses — no new ack token), OR they set this
  knob to a truthy value (`1` / `true` / `yes`, matching the
  sibling auto-approve knobs' parse). Set the knob when you
  explicitly trust the upstream judge degradation surface and want
  the pre-TB-272 cosmetic-only TB-243 behavior — the `[noisy]`
  badge stays on `ap2 status` / web home but the dispatch path
  isn't gated on it. Hot-reloadable (TB-271) so the operator can
  flip it without a daemon restart.

**Ideation.**
- `AP2_IDEATION_DISABLED` — set to `1`/`true` to opt out of empty-board
  ideation entirely.
- `AP2_IDEATION_COOLDOWN_S` (7200) — minimum gap between ideation runs.
- `AP2_IDEATION_TRIGGER_TASK_COUNT` (3) — fire ideation when Ready+Backlog
  count is BELOW this threshold (Active is still a hard gate). Set to
  `1` for the legacy "fire only when the working queue is fully empty"
  behavior; raise it (e.g. `5`) for projects with very fluid scope.
  Invalid (non-int, non-positive) values fall back to the default.

**Operator-in-the-loop relaxations (TB-223).** Three layered safety
knobs that let an operator who trusts the upstream gates dispatch
ideation-proposed tasks without running `ap2 approve` on each one.
Defaults are unset / conservative — current behavior is preserved for
operators who haven't opted in. Cross-references `goal.md`'s
**Current focus: end-to-end automation** axis on the manual-approval
bottleneck: a representative ap2 session approves 10-20 tasks per
cycle, which contradicts the Mission's "walk away for a week without
intervention" promise. The trio is layered so an operator can dial
trust precisely: `AP2_AUTO_APPROVE` is the master switch,
`AP2_AUTO_APPROVE_GATE_TAGS` is the per-shape opt-out (operator names
tag categories that retain manual review even in auto-approve mode),
and `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` is the systemic-regression
circuit-breaker (auto-promote halts when consecutive task failures
land in Frozen).

- `AP2_AUTO_APPROVE` — master switch. **Unset by default.** When set
  to a truthy value (`1` / `true` / `yes`, matching
  `AP2_IDEATION_DISABLED`'s convention), ideation-authored
  `add_backlog` rows omit the `@blocked:review` codespan so the
  daemon's next-tick auto-promote dispatches the task immediately. The
  operator decision-log entry in `ap2 logs` still surfaces what
  auto-approval shipped (the `auto_approved` event — see `## Event
  schema`), so the audit trail is preserved for offline review.
  Off-by-default keeps the legacy approve-every-task behavior in place
  for operators who haven't verified the upstream gates (briefing
  structural validation, goal-alignment validation, per-task
  verification, retry budget, rollback).
- `AP2_AUTO_APPROVE_DRY_RUN` — TB-232 monitor-only on-ramp. **Unset
  by default.** When set to a truthy value alongside
  `AP2_AUTO_APPROVE=1`, the auto-approve gate chain (tags +
  freeze-threshold + token caps) still runs but the WRITE step is a
  no-op on the board row: instead of stripping `@blocked:review` and
  emitting `auto_approved`, the daemon emits a `would_auto_approve`
  audit event (same `task` + `knob` payload, plus `dry_run=true`) and
  leaves the codespan intact for operator-manual `ap2 approve`. Use
  this to observe the loop's decisions without committing to the
  binary cliff. **Enablement on-ramp:** set both
  `AP2_AUTO_APPROVE=1` AND `AP2_AUTO_APPROVE_DRY_RUN=1`, leave the
  daemon running for ≥24h, read `ap2 status --json` and grep
  `events.jsonl` for `would_auto_approve` events to confirm the
  gate's decisions match your judgment, then unset
  `AP2_AUTO_APPROVE_DRY_RUN` (keep `AP2_AUTO_APPROVE=1`) to engage
  real dispatch. The `would_auto_approve_count_24h` field on
  `collect_auto_approve_state` (surfaced via `ap2 status` + web home)
  rises as decisions accumulate so you can confirm at a glance the
  gate is exercising decisions before flipping the switch. TB-238
  also surfaces the same count as a trailing `*Dry-run window:*`
  sub-block on the scheduled `status-report` Mattermost post's
  `## Automation loop activity` section, so a walk-away operator
  sees the readiness signal in their primary return surface
  without alt-tabbing to `ap2 status --json`.
- `AP2_AUTO_APPROVE_GATE_TAGS` (default `#breaking-change,#high-risk`)
  — comma-separated list of tag strings. When auto-approve is on, a
  proposed task carrying ANY of these tags **retains** its
  `@blocked:review` codespan so it still requires `ap2 approve`. This
  is the operator's escape hatch for categories of work they don't
  trust to auto-ship; the defaults align with the tags ideation itself
  uses to self-mark elevated-risk proposals. Operators may type the
  tag with or without the leading `#` (both parse identically); empty
  string falls back to the default set.
- `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` (default `3`) — integer count.
  When N consecutive `task_complete` events have status in
  `{verification_failed, blocked, error, failed}` AND end in
  `retry_exhausted` (the failure chain actually froze a task rather
  than looping a single TB through retries), the daemon halts
  auto-promotion of `auto_approved` tasks. Operator-approved tasks
  (those promoted via `ap2 approve` → `ideation_approved` event)
  continue to dispatch normally — the freeze is targeted at the auto
  layer, not blanket. Operator unfreezes via `ap2 ack
  auto_approve_unfreeze --reason "<one-line rationale>"` (uses the
  existing TB-106 ack pattern — the daemon scans `operator_ack`
  events' `note` field for the `auto_approve_unfreeze` token and
  resets the failure counter). Setting the threshold to `0` (or any
  non-positive int) disables the circuit-breaker entirely — the
  explicit escape hatch for operators who trust the upstream gates
  beyond this layer.

**Cost + blast-radius guards (TB-224).** Two layered token caps and a
single-event `task_error` halt that ride on top of TB-223's auto-approve
gate. Without these, `AP2_AUTO_APPROVE=1` trades manual review for
unbounded token spend — a "successful-but-wasteful" loop can satisfy
verification while burning tokens indefinitely, and a `task_error`
cascade (SDK timeout, agent OOM, kernel SIGKILL) needs operator
attention not a silent retry. **Defaults are unset on both knobs** —
operators who haven't done the cost-budgeting math for their project
don't get a hardcoded cap surprising them. The recommended pattern:
set both caps BEFORE flipping `AP2_AUTO_APPROVE=1`. Cross-references
`goal.md`'s **Current focus: end-to-end automation** axis 3 ("Cost and
blast-radius guards").

- `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` — integer cap on combined
  `input_tokens + output_tokens` per task. **Unset by default → no
  cap.** When set to a positive integer, the daemon checks each
  `task_run_usage` event (TB-165, emitted at every terminal path) for
  auto-approved tasks; an event whose combined tokens exceed the cap
  trips a `per_task_cap` halt — the daemon emits
  `auto_approve_halted reason=per_task_cap used=<N> cap=<M>` and
  pauses auto-promote of `auto_approved` tasks until operator emits
  `ap2 ack auto_approve_window_resume`. Catches the single-runaway
  pattern (one task in an infinite tool-call loop). Manual
  `ap2 approve` continues to dispatch even while halted — the pause
  is targeted at the auto-approved bucket only.
- `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` — integer cap on cumulative
  `input_tokens + output_tokens` across all auto-approved tasks in a
  rolling **24-hour window**. **Unset by default → no cap.** Computed
  by summing `task_run_usage` token fields over tasks identified as
  auto-approved (via TB-223's `auto_approved` audit event) within
  `now - 24h`. No new state file; tail-scan of events.jsonl, same
  shape the cron status-report uses. When the sum exceeds the cap,
  the daemon emits `auto_approve_halted reason=window_cap
  window_used=<N> cap=<M>` and pauses auto-promote. The rolling-24h
  shape matches the operator's natural rhythm without calendar-day
  timezone ambiguity. Catches the drift pattern: 50 small tasks each
  within the per-task cap but cumulatively unbounded.
- `task_error` single-event halt — distinct from
  `verification_failed` (which TB-223's `FREEZE_THRESHOLD` requires
  N=3 of). A `task_error` event indicates an infrastructure failure
  (SDK timeout, agent OOM, briefing read failure) per `events.jsonl`
  conventions; **one occurrence is enough** to halt auto-promote
  because infrastructure failures aren't statistical noise — they
  need operator attention immediately. When a `task_error` lands for
  an `auto_approved` task, the daemon emits
  `auto_approve_halted reason=task_error task=TB-N
  error_excerpt=<...>` AND appends a `## Decisions needed from
  operator` bullet to `.cc-autopilot/ideation_state.md` naming the
  failing TB-N + error excerpt (so `ap2 status` and the web home
  page surface it without waiting for the next ideation cron).
- **Shared resume ack:** `ap2 ack auto_approve_window_resume --reason
  "<rationale>"` clears any of the three halt reasons above (one ack
  covers all three since they share the same auto-promote-paused
  state). Different verb from TB-223's `auto_approve_unfreeze`
  because the two halts have semantically-distinct entry paths
  (cumulative-regression vs. cost/blast-radius) and the audit trail
  benefits from one log line per class of issue. Reuses the existing
  TB-106 ack pattern (the daemon scans `operator_ack` events' `note`
  field for the `auto_approve_window_resume` token and resets the
  halt state).

Audit events: `auto_approve_halted` fires once per triggering
episode (deduped via tail scan); `auto_approve_skipped` fires once
per preempted auto-promote tick (with the would-have-promoted TB-N)
so the cumulative skipped-count is visible in `ap2 logs` for
operators tuning the cap values.

**Pre-flight surface for cap misconfiguration (TB-234).** `ap2 doctor`
has an `auto-approve safety floor` audit section that fires WARN when
`AP2_AUTO_APPROVE` is set to a truthy value (`1` / `true` / `yes`) but
`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` and/or
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` is unset, empty, zero, or
non-integer. With both caps disabled, an additional summary WARN names
the configuration as "safety floor OFF" and cross-links `goal.md`
L102-113. WARN, not FAIL — the operator may have a reason to run
uncapped for a short window — but loud enough that an `ap2 doctor`
run after flipping `AP2_AUTO_APPROVE=1` reveals the gap before the
SDK bill arrives. When auto-approve is unset (the default), the section
emits a single INFO line stating manual approval is required per task.
The audit is purely diagnostic: no events written, no daemon state
mutated. Pair with `ap2 status`'s continuous `automation_status`
surface (TB-227) — doctor is the one-shot pre-flight, status is the
ongoing snapshot.

**Auto-unfreeze on agent-diagnosed briefing-shape fixes (TB-225).**
Self-heals the recurring class of retry-exhausted Frozen tasks whose
root cause is a briefing-shape regression the agent already diagnosed
in its `task_complete status=blocked` summary. Two known prod examples
the brief calls out: TB-204 (`grep -lE` → `grep -rlE` on a directory
target — missing the `-r` flag returns nothing), TB-207 (literal-
backtick in shell bullets truncates the shell command at the first
backtick). Both shapes are catalogued in
`ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID`
section. With this gate on, the daemon parses the agent's structured
`BriefingFix:` prefix, verifies the briefing-line literal match,
patches the briefing via the operator-queue `update` op, and unfreezes
the task — all without operator-manual `ap2 unfreeze`. **Defaults are
unset / conservative — feature is opt-in only.** Cross-references
`goal.md`'s **Current focus: end-to-end automation** axis 2
("Failure-recovery operator dependency").

The canonical agent-prefix contract (task agents emit this line as
part of their `report_result(status="blocked", summary=...)` payload
when they diagnose a briefing-shape regression as the root cause):

    BriefingFix: <shape> at <briefing_path>:<line>: <from> -> <to>

Worked example:

    BriefingFix: grep_missing_r_on_dir at .cc-autopilot/tasks/foo.md:23: grep -lE 'pattern' ap2/tests/ -> grep -rlE 'pattern' ap2/tests/

The parser (`ap2._shared.parse_blocked_summary_fix_shape`) is
strictly structured — no regex-on-prose guessing — so an agent that
authors free-text diagnoses (no `BriefingFix:` line) falls through
to today's manual-unfreeze path identically.

See also `skills/ap2-task/SKILL.md` § "Reporting failures
(`task_complete blocked` summaries)" — the upstream emitter contract
the per-task agent reads at run time, with one fenced worked example
per bootstrap fix-shape (TB-229).

- `AP2_AUTO_UNFREEZE_FIX_SHAPES` — comma-separated allowlist of
  fix-shape tokens. **Unset by default → feature disabled.** The
  daemon refuses to auto-apply any shape that isn't in this
  allowlist; unknown shapes still require manual `ap2 unfreeze`.
  The env-knob string IS the trust contract: operators audit each
  shape and opt in by listing tokens. Recommended bootstrap list
  (each names a known pitfall in
  `ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID`
  section):
  - `grep_missing_r_on_dir` — `grep -lE 'pattern' <dir>/` returns
    nothing without `-r`. Fix: `grep -rlE 'pattern' <dir>/`.
  - `bare_python_to_uv_run` — `python -c '...'` exits 127 in the
    daemon environment. Fix: `uv run python -c '...'`.
  - `literal_backtick_in_shell_bullet` — a bullet with literal
    backticks like `` `grep ... | wc -l` `` truncates at the first
    backtick. Fix: drop the wrapping backticks; the bullet body IS
    the command.
  - `bare_path_to_test_f` — a bullet whose body is a bare path
    (e.g. `reports/foo.md`) tries to execute the file (exit 126).
    Fix: `test -f reports/foo.md`.
- `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default `1`) — integer cap on
  auto-unfreeze attempts per task before fallback to manual
  `ap2 unfreeze`. Bounds oscillation when the patched briefing
  ALSO fails. `0` disables the per-task cap (unbounded retries —
  intentionally not the default; disabling should be an explicit
  operator decision).
- `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default `3`) — rolling 24h cap
  on total auto-unfreeze applications across all tasks. When
  exceeded, the daemon halts further auto-unfreeze attempts on the
  tick AND appends a `## Decisions needed from operator` bullet to
  `.cc-autopilot/ideation_state.md` so `ap2 status` surfaces the
  systemic-regression signal. `0` disables the per-day cap.
- `AP2_AUTO_UNFREEZE_DRY_RUN` — TB-233 monitor-only on-ramp.
  **Unset by default.** When set to a truthy value (`1` / `true` /
  `yes`, case-insensitive) alongside a non-empty
  `AP2_AUTO_UNFREEZE_FIX_SHAPES`, the auto-unfreeze guard chain
  (allowlist + per-task cap + per-day cap + briefing-line match)
  still runs but the WRITE step is a no-op: instead of calling
  `_apply_auto_unfreeze_patch` (which queues `update` + `unfreeze`
  ops on the operator queue and mutates the briefing file), the
  daemon emits a `would_auto_unfreeze` audit event with the same
  payload shape as `auto_unfreeze_applied` plus the
  `file` + `line` fields from the parsed `BriefingFix:` prefix.
  The per-day-count + per-task-prior counters do NOT increment in
  dry-run (no real application). Use this to observe the loop's
  decisions on the live Frozen set without committing to the binary
  cliff. **Enablement on-ramp:** set both
  `AP2_AUTO_UNFREEZE_FIX_SHAPES=<shapes>` AND
  `AP2_AUTO_UNFREEZE_DRY_RUN=1`, leave the daemon running for a
  window (e.g. ≥24h), read `ap2 logs --type would_auto_unfreeze` to
  confirm the gate's decisions match your judgment, then unset
  `AP2_AUTO_UNFREEZE_DRY_RUN` to engage real patching. Sibling
  on-ramp to `AP2_AUTO_APPROVE_DRY_RUN` (TB-232) on the axis-1
  auto-approve side. **Pre-flight diagnostic** (TB-239): `ap2 doctor`
  emits a WARN in the `auto-unfreeze safety floor` section when
  `AP2_AUTO_UNFREEZE_DRY_RUN=1` is set without
  `AP2_AUTO_UNFREEZE_FIX_SHAPES` — `_maybe_auto_unfreeze` early-
  returns on empty allowlist BEFORE the dry-run check, so the
  observation knob is a silent no-op without the allowlist. Run
  `ap2 doctor` after flipping the dry-run knob to confirm both env
  vars are wired. **Operator's primary readiness surface** (TB-238):
  the scheduled `status-report` Mattermost post's
  `## Automation loop activity` section grows a trailing
  `*Dry-run window:*` sub-block while either dry-run knob is on,
  listing the 24h rolling count of `would_auto_unfreeze`
  (and/or `would_auto_approve`) events. Watch the count rise post-by-
  post for confidence the gate is exercising decisions before
  flipping the knob off; the sub-block is omitted entirely when
  both dry-runs are off so default-off projects stay byte-identical
  to TB-228 output.

Audit events: `auto_unfreeze_applied` (success — payload `task`,
`shape`, `from`, `to`); `auto_unfreeze_skipped` (any guarded skip —
payload `task` + `reason` token; one of
`shape_not_in_allowlist`, `briefing_mismatch`,
`briefing_path_missing`, `per_task_cap`, `per_day_cap`,
`queue_error`, `sweep_error`). The `knob_unset` baseline does NOT
emit per-tick — the feature is opt-in and operators who haven't
set `AP2_AUTO_UNFREEZE_FIX_SHAPES` shouldn't see noise.

Why operator-curated allowlist (not heuristic detection): arbitrary
briefing edits by the daemon are blast-radius-unsafe. The allowlist
lets the operator audit each fix-shape and opt in specifically;
shapes can be removed instantly if one misfires by editing the env
string. New shapes never auto-promote — the operator opens new
shapes by editing the env value, the daemon never invents them.

Why the briefing-line literal match check: the agent's diagnosis may
be stale if the briefing was operator-edited between failure and
freeze handling (e.g. the operator hand-edited it trying to fix it
themselves). Verifying the `from` pattern is literally present on
the named line before patching closes the data-race window. A
mismatch emits `auto_unfreeze_skipped reason=briefing_mismatch` and
leaves the task Frozen — fail-safe.

**Focus rotation (TB-226 axis 4).** Three knobs gate the in-memory
focus-list pointer's advance. See `### Focus rotation (axis 4)` below
for the architecture + the `ap2 ack roadmap_complete` resume verb.

- `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default `3`, min `1`, max `20`) —
  heuristic-fallback threshold: when the active focus has NO
  explicit `Done when:` sub-block, the daemon advances after this
  many consecutive ideation cycles produced 0 proposals against
  the focus. Invalid (non-int / empty) values fall back to the
  default; values outside the clamp range are pinned to the
  nearest bound (so a typo `0` doesn't disable advance and `999`
  doesn't wedge it permanently).
- `AP2_FOCUS_AUTO_ADVANCE_DISABLED` — kill-switch. Set to `1` /
  `true` / `yes` / `on` (same convention as `AP2_IDEATION_DISABLED`)
  to prevent the daemon from auto-advancing even when criteria are
  met; the daemon surfaces a `## Decisions needed from operator`
  bullet instead so the operator can advance manually via
  `ap2 update-goal`. Default unset → auto-advance enabled.
- `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default `medium`) — effort
  level for the LLM-judge call that evaluates whether a focus's
  `Done when:` bullets are substantively met. Mirrors
  `AP2_JANITOR_JUDGE_EFFORT`'s shape: explicit value > fallback to
  `AP2_AGENT_EFFORT` > the `medium` default. Default `medium`
  (cheaper than the verifier's `high`) because the question is
  one-shot per advance attempt, not per-bullet-per-task.

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
- `AP2_PROJECT_NAME` (TB-280) — operator-facing project identity that
  leads the status-report Mattermost headline (`**[<project_name>]
  Autopilot Status Report** — <now>`). Defaults to `project_root.name`
  so a project at `/home/user/code/stoch` posts under `[stoch]`
  without configuration; override when the directory name is generic
  (`main`, `proj`) or carries a layout suffix the operator doesn't
  want surfaced. Hot-reloadable — a rename takes effect on the next
  tick without `ap2 stop && ap2 start`.
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

### Focus rotation (axis 4)

Closes goal.md L115-138's axis 4 design. The operator authors a
multi-`## Current focus:` heading list in `goal.md` (priority order,
top = active); the daemon's runtime pointer advances as each focus
exhausts, without operator-mediated rotation. The daemon never
mutates goal.md itself (goal.md L187-191 "Goal.md auto-rotation"
Non-goal); pointer state is in-memory only.

**Pointer file.**
`.cc-autopilot/focus_pointer.json` carries the runtime pointer
(`active_index`, `active_title`, `empty_cycles`, `exhausted_titles`,
`roadmap_complete_ack_idx`, `roadmap_complete_emitted`,
`updated_ts`, `schema`). Fenced from task agents
(`TASK_AGENT_FENCED_PATHS`) and gitignored so rollbacks (TB-111)
don't re-fire stale `focus_advanced` events. Schema-versioned via
the `schema: 1` field so a future migration can branch cleanly.

**Advance heuristic.**
Each tick, `_maybe_advance_focus(cfg, sdk)` runs as step 0.6 of
`_tick` (after the auto-unfreeze sweep, before cron / pipeline /
dispatch / ideation). The active focus's structural shape decides
the advance path:

1. *Explicit `Done when:` sub-block* — the daemon invokes a short
   SDK judge call (`_judge_done_when`) with the focus title, its
   Done-when bullets, the last ~10 task-complete titles + summaries,
   and the head of `ideation_state.md`. The judge replies on the
   first line with one of `yes` / `no` / `insufficient_evidence`;
   only `yes` triggers advance. Cost bounded by
   `AP2_FOCUS_DONE_WHEN_JUDGE_EFFORT` (default `medium`).
2. *No `Done when:` sub-block* — heuristic fallback. The daemon
   counts consecutive recent ideation cycles that produced 0
   proposals against the active focus
   (`ideation_empty_board` + `ideation_complete` events,
   reset by `ideation_proposal_recorded`). When the count reaches
   `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3, clamped to
   [1, 20]), advance.

On advance, the daemon emits `focus_advanced from=<old_title>
to=<new_title> trigger=<done_when_judge|empty_cycles_heuristic>
new_index=<i> total_foci=<n>` and writes the updated pointer.

**Kill-switch.**
`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` short-circuits the advance even
when criteria are met. The daemon surfaces a `## Decisions needed
from operator` bullet so the operator advances manually via
`ap2 update-goal`. The pointer doesn't move; the next tick re-emits
the bullet if criteria still trip — acceptable noise floor.

**Roadmap-complete: ideation parks (TB-275).**
When the pointer advances past the LAST `## Current focus:`
heading, the daemon emits `roadmap_complete exhausted_count=<n>
trigger=pointer_past_last` (once, suppressed via the pointer's
`roadmap_complete_emitted` flag) AND appends a `## Decisions
needed from operator` bullet to `ideation_state.md`. From then
on, the IDEATION TRIGGER skips: `_maybe_ideate` emits
`ideation_skipped reason=roadmap_complete` and bumps the cooldown
(TB-246), so a walk-away weekend that exhausts the roadmap stops
piling speculative proposals against an already-exhausted focus
list (without this gate, a 60-min cooldown × 48h weekend wastes
up to ~48 ideation SDK calls). The skip-gate is a sibling to
TB-174's focus-exhausted gate (same `ideation_skipped` event
shape with a different `reason` field; `force_ideate` bypasses
both so `ap2 ideate --force` works on the operator's recovery
path). TASK DISPATCH IS NOT AFFECTED (TB-275): already-queued
Backlog tasks — operator-added via `ap2 add`, operator-approved
via `ap2 approve`, or previously auto-approved by ideation —
continue to auto-promote and dispatch normally. Once ideation is
gated, no new speculative work can enter the Backlog anyway, so
everything in the queue is operator-originated or already-proposed
and should always drain. A genuine full-stop is `ap2 pause`, a
separate explicit mechanism. Operator clears the parked-ideation
notice via `ap2 update-goal` (extending the roadmap re-arms
ideation by resetting the pointer onto the new focus) OR via
`ap2 ack roadmap_complete --reason "..."` (dismisses the notice);
the daemon's events-jsonl scan detects an `operator_ack` event
whose `note` carries the `roadmap_complete` token AFTER the most
recent `roadmap_complete` event and clears the predicate. Same
shape TB-223's `auto_approve_unfreeze` / TB-224's
`auto_approve_window_resume` use.

**Status-report push surface (TB-244).**
Axis-4 events (`focus_advanced` / `roadmap_complete`) also
surface in the 2h status-report Mattermost cron post — the
operator's primary walk-away channel. The routine renders a
`## Focus rotation activity` sub-block (parallel to TB-228's
`## Automation loop activity` digest) listing one bullet per
event in the inter-report window, with the `ap2 ack
roadmap_complete` resume hint rendered verbatim on the halt
line so the operator can copy-paste it from the post. Closes
the push-surface gap TB-242 left open: the pull surfaces
(`ap2 status` text/JSON + web home) showed the active focus +
position + halt state on-demand, but a `roadmap_complete` halt
at 03:00Z used to wait for the operator's next manual `ap2
status` to surface. Now the next 2h cron post carries it.
Omit-on-empty: the sub-block is suppressed when no axis-4
events landed in the window, so quiet windows stay
byte-identical to the pre-TB-244 baseline. The
`_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` frozenset in
`ap2/status_report.py` also lists both event types so a lone
axis-4 event keeps the routine's skip-gate from firing —
operator never misses a rotation-state change because the
2h post coincided with an otherwise-quiet window.

**Why never auto-mutate goal.md.**
Goal.md L187-191 names goal.md auto-rotation as a Non-goal. The
operator owns the focus list; the daemon advances its pointer
based on exhaustion signals but never writes the file. Adding /
reordering / retiring foci stays `ap2 update-goal`-only. This
keeps the surface symmetric with the other operator-only paths
(cron mutation via `ap2 cron edit`, classify-verdict via
`ap2 classify`, ack via `ap2 ack`).

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
