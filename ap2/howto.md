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

**Optional `Progress signals:` sub-block (TB-285).** A focus heading
MAY carry a `Progress signals:` sub-block (either an inline
`Progress signals:` paragraph leading a bulleted list, or a nested
`### Progress signals` sub-heading with bullets underneath). The
bullets are advisory ideation-prompt context — concrete examples of
what visible progress against the focus looks like — and feed the
ideation agent's per-cycle assessment so proposals lean toward
movement on the named signals. They are NOT a gating criterion: focus
advancement is driven solely by the empty-cycles heuristic
(`AP2_IDEATION_HALT_EMPTY_CYCLES`; see `### Focus rotation (axis 4)`),
which fires for foci with AND without a `Progress signals:` block.
Authors who want to give ideation explicit "what good looks like"
hints can include the sub-block; authors who'd rather let the focus's
prose carry the framing can omit it without changing how the daemon
advances.

> ## Current focus: webhook reliability
>
> The broker webhook is the bot's single ingestion path — alerts dropped
> here never reach the summary. The focus is on retry semantics, dead-
> letter handling, and observability of webhook delivery.
>
> Progress signals:
> - Missed alerts surface within 5 minutes via an observable counter.
> - A killed webhook subscription auto-recovers without operator action.

The bullets give ideation concrete shapes to favor — they do NOT
auto-fire focus advancement when satisfied (operator authorship of
the next focus, or the empty-cycles heuristic, drives that). The
sub-block's historical name (the one renamed in TB-285) was
dropped to clear the gating connotation it carried; the legacy
heading does not parse (hard cut — `ap2 update-goal` to migrate
any pre-TB-285 goal.md to the `Progress signals:` heading).

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

For prose-judge parse-failure diagnostics — length signals,
`parse_error` categories, and the on-disk response dumps — see the
**ap2-observability** skill (`skills/ap2-observability/SKILL.md`).

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

## Operator board-ops reference — see the ap2-board-ops skill

The operator-action reference — the `autopilot` custom MCP tool catalogue
(the `report_result` / `board_edit` / `operator_queue_append` /
`cron_propose` pools, partitioned by agent toolset) and the full
`ap2 <verb>` operator-CLI verb table with its WHY / when-to-use companion —
was carved out of this manual into the auto-triggered **ap2-board-ops**
skill (`skills/ap2-board-ops/SKILL.md`, TB-399). The
`test_every_cli_verb_documented` drift gate in
`ap2/tests/test_docs_drift.py` now reads the skill, and
`test_every_mcp_tool_documented` accepts the skill alongside
`ap2/architecture.md`'s `CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` literal
enumeration — so an operator looks up a CLI verb or an agent's MCP tool
surface by reading that one skill, not by grepping this file.

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
mirror of the `auto_approve` block); (c) the status-report
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

## Components enumeration (`ap2 status`)

TB-319 closes the goal.md L235-237 Progress signal that named
`ap2 status` as the natural surface for discovering which components
are wired into the daemon. Before TB-319 the only way to find out was
to `ls ap2/components/` and read each manifest by hand; after, every
`ap2 status` invocation appends a `## Components` block listing every
discovered manifest with its on/off state and the env-flag string
that controls it.

**Text-mode rendering.** After the operator-attention cluster +
`auto-approve:` block + `next:` line, `ap2 status` prints:

    ## Components
      attention: on (env_flag=None)
      auto_approve: on (env_flag=None)
      auto_unfreeze: on (env_flag=None)
      focus_advance: on (env_flag=None)
      janitor: on (AP2_JANITOR_DISABLED unset)
      mattermost: on (AP2_MM_CHANNELS=channel-id)

Entries are alphabetic by manifest name (matching
`default_registry().components` iteration so a reader's mental model
of "in what order do hooks fire?" lines up with what they see in
status). Always emitted — unlike the operator-attention cluster's
omit-on-empty rule, the registry walk is deterministic and the same
set of components ships on every project, so suppressing the section
would itself be a regression worth surfacing.

**`<state>` is `on` or `off`** — resolved via `Manifest.is_enabled()`
against the live process env, so a hot-reloaded `.cc-autopilot/env`
takes effect on the next invocation. The polarity convention
(`Registry._is_enabled` since TB-309, now consolidated on
`Manifest.is_enabled` by TB-319):

- **`env_flag=None`** (attention, auto_approve, auto_unfreeze,
  focus_advance) — always-on per `default_enabled=True`. There's no
  master kill switch for these four; whether to add one is the
  open ideation question logged at the `## Decisions needed` surface.
- **Suppress polarity** (`*_DISABLED` env_flag, `default_enabled=True`
  — janitor / `AP2_JANITOR_DISABLED`) — the env var is a kill switch:
  truthy disables, unset/empty/falsy keeps the component on. (TB-386
  demoted the validator_judge / verifier_judge LLM judges out of
  `ap2/components/`; their `AP2_VALIDATOR_JUDGE_DISABLED` /
  `AP2_VERIFY_JUDGE_DISABLED` off-switches survive as plain config knobs
  read directly by the core briefing-validation / verify runners, NOT as
  component env_flags, so they no longer appear in this list.)
- **Require polarity** (opt-in env_flag, `default_enabled=False`
  — mattermost / `AP2_MM_CHANNELS`) — the env var is an opt-in
  toggle: unset/empty disables, non-empty enables.

**`<env_flag_desc>`** renders the env-flag state in operator-legible
form: `env_flag=None` for always-on manifests, `<NAME> unset` when
the env var is absent or empty, or `<NAME>=<value>` when set
(truncated at 32 chars with an ellipsis so a long channel-id list /
opaque token doesn't blow up the status block width).

**JSON parity (`--json`).** A top-level `components` key carries
one entry per discovered manifest, each with the four documented
keys (`name`, `enabled`, `env_flag`, `default_enabled`). ALWAYS
present (parser-stability mirror of the `auto_approve` / `audit` /
`attention` blocks), and the text + JSON branches walk the same
`default_registry().components` snapshot inside one `cmd_status`
call so they can never disagree about a component's enabled state.

**Out of scope** (deferred to follow-up TBs if operator asks):

- A `/components` web pull page parallel to TB-296's `/attention`.
- Per-component diagnostic info (tick counts, last-fired timestamps,
  recent events).
- New env knobs to filter the enumeration; the walk shows every
  discovered manifest unconditionally. Master kill-switch flags for
  the four `env_flag=None` manifests are the open ideation question
  named above.

## Classify verdicts

`ap2 classify TB-N --impact <verdict>` accepts one of four values from
`IMPACT_VERDICTS` (single source of truth at `ap2/briefing_validators.py`; still importable via `ap2.tools.IMPACT_VERDICTS` thanks to TB-262's re-export). The four
buckets form a gradient — substantive-positive → compliance-neutral →
actively-harmful — with `unclear` as the explicit "can't tell yet"
bucket. Pick the verdict by running two delete-tests in sequence:

- **`advanced-goal`** — substantively advanced the goal (positive).
  Passes the base delete-test: "if we deleted this task, would the
  goal still ship?" Answer: no — the goal would be visibly worse off
  without this work. Use when the task moved the active focus's
  progress signals closer (or the top-level `## Done when` criteria,
  if the work cuts across foci), unblocked a downstream task, or
  shipped a user-visible capability the goal names.

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

## Configuration reference — see the ap2-config skill

The operator-facing configuration reference — the full `AP2_*`
environment-knob catalogue, the typed `.cc-autopilot/config.toml` key
reference (`[core]` / `[components.*]` / `[agent_backends]`), and the Codex
agent-backend install + auth setup — was carved out of this manual into the
auto-triggered **ap2-config** skill (`skills/ap2-config/SKILL.md`, TB-398).
The `test_every_env_knob_documented` and `test_every_config_key_documented`
drift gates in `ap2/tests/test_docs_drift.py` now enforce that every `AP2_*`
source knob and every `ConfigKey` schema key is documented there — so an
operator discovers or tunes any knob by reading that one skill, not by
grepping this file. The `.cc-autopilot/env` and `.cc-autopilot/config.toml`
scaffolds `ap2 init` writes point at it too.

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
