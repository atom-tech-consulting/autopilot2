Goal: keep Backlog populated with high-leverage, goal-aligned tasks.

Read these files in order:
1. .cc-autopilot/ideation_state.md — last cycle's progress assessment
   (if it exists). This is YOUR cross-cycle memory — what was
   considered, what was deferred, where prior cycles thought the
   current focus stood.
2. .cc-autopilot/operator_log.md — operator decisions and action
   acknowledgements (if it exists). Each line is authoritative: do
   NOT re-propose actions or decisions logged here, even if your
   prior assessment surfaced them as "Decisions needed from operator"
   (or under the pre-TB-191 section name on a state file from before
   the rename).
   Treat this file as the operator-owned ground truth on what's been
   done, abandoned, or decided. The `## Recent operator rejections`
   block in this prompt's `## Current state` header (TB-163) is a
   pre-extracted view of the most recent `rejected ideation proposal`
   lines from the same file — consult it BEFORE ranking and call out
   any pattern (recurring topic, framing, or scope shape the operator
   keeps vetoing) in your `## Considered & deferred` section so the
   pattern compounds across cycles instead of getting re-discovered.
3. goal.md — project mission, current focus, non-goals, constraints.
   If absent OR all sections are still placeholders, infer goals from
   CLAUDE.md + progress.md instead.
4. TASKS.md — current board state (do not duplicate existing tasks).
5. progress.md — recent completed work, for follow-up discovery.
6. CLAUDE.md — project conventions and any Autopilot config.

Propose at most N new tasks this cycle, where N is the
`proposal slots this cycle` value in the `## Current state` block above.
If N is 0, do not propose any tasks (the queue is already at the
operator's configured threshold).

## Step 0: write the progress assessment FIRST (TB-87)
Before proposing anything, call the `ideation_state_write` MCP tool
with the full assessment as `content` — that overwrites
`.cc-autopilot/ideation_state.md` atomically (TB-90). This is
load-bearing: the file's structure forces you to ground proposals in
cited evidence and gives the next cycle's ideation memory of what
you considered.

The tool is the ONLY way to write the assessment file. You don't
have Write/Edit access to `.cc-autopilot/`, and you don't have Bash
either (TB-109) — neither `tee` nor `> path` is available to you.
The MCP tool handles atomic write + event emission. Reads stay
through the regular `Read` tool.

Use this exact schema for the `content` argument:

    # Ideation State

    _Last updated: <UTC ISO-8601 timestamp> by ideation cron_

    ## Mission alignment
    One paragraph: are recent completes still serving goal.md's
    Mission? Cite the 3-5 most recent Completes you considered
    (TB-N + one-line summary).

    ## Current focus assessment
    For each item in goal.md's "Current focus" section:

    - **<focus item verbatim from goal.md>**
      - Progress so far: <what shipped against this — cite TB-N for
        EVERY claim; vague claims like "good progress" are forbidden>
      - Gaps: <what's not yet addressed; concrete, actionable>
      - Status: `in-progress` | `exhausted-needs-operator` | `deferred`
      - Reasoning: <one sentence on why that status>

    If a focus item has no Complete TB-Ns yet, status MUST be
    `in-progress`. If every reasonable next step has shipped and you
    can't identify a non-trivial gap, status is
    `exhausted-needs-operator` — write a one-liner about what the
    operator should decide next.

    ## Non-goal risk check
    Quick scan of in-flight + recent work: is anything drifting into
    goal.md's Non-goals? List specific concerns or write "none".

    ## Considered & deferred this cycle
    - **<task idea title>**: <why you didn't propose it this cycle —
      e.g., "covered by TB-95 still in flight", "lower impact than
      the 3 ranked ahead of it", "would conflict with non-goal X">

    ## Cycle observations
    Agent-internal working notes (TB-191). NOT forwarded to
    operator-facing surfaces (`ap2 status`, the Mattermost cron
    status-report). Use this for observations that informed THIS
    cycle's assessment but don't fit a structured section.

    Triage discipline (when writing this section):
    1. Read the prior cycle's `## Cycle observations` bullets FIRST.
    2. For each prior bullet, decide:
       - Situation changed? → drop (mention the change in
         Mission alignment if it's cross-cutting).
       - Now fits a structured section? → promote there
         (Mission alignment, Current focus > Gaps, Considered &
         deferred); do NOT also keep here.
       - Stale / no longer informs current reasoning? → drop.
       - Still actively informing reasoning AND no better home?
         → carry, with a one-sentence re-justification of why
         this cycle still needs it.
    3. Default disposition: DROP. Only carry if explicitly
       re-justified.

    Hard cap: 10 bullets max. If you can't triage to 10, the
    discipline is slipping.

    Hard prohibitions:
    - NEVER an item the operator should act on (use
      `## Decisions needed from operator`).
    - NEVER pure status reporting (events.jsonl covers that).
    - NEVER recurring "no X events" / "no operator activity"
      type bullets.

    ## Decisions needed from operator
    Operator-facing escalation channel (TB-191). Each bullet MUST
    satisfy ALL of:
    - Direct question (`?`-terminated) OR explicit prefix
      `Decision needed:` / `Operator input required:`.
    - Names the specific operator action (`ap2 approve TB-N` /
      "edit goal.md to add Y" / "decide between approach A vs B").
    - Names the unblock-condition (what changes about the next
      ideation cycle if the operator engages).

    DO NOT include:
    - Status observations ("No X events", "Cadence is steady").
    - Pattern-tracking notes ("n=3 retries on bullet kind Y").
    - Behavioral commentary about the operator.
    - Metric updates without a corresponding decision.
    - Items where the operator would need no input even if they
      read the bullet.
    - Tasks-awaiting-review bullets (TB-182). Pending-review TB-Ns
      are surfaced mechanically by `ap2 status` (CLI) and the cron
      status-report's snapshot block (per TB-151 / TB-173) from
      current board state every run, so duplicating that list here
      creates contradiction risk when the gap between ideation
      cycles diverges from current board state (e.g. an
      `ap2 approve` lands in the gap and the stale list disagrees
      with the fresh one in the same Mattermost post).

    Misplaced content redirect: if a noticed gap doesn't match the
    actionable-decision shape above, route it to `## Cycle
    observations` (agent-internal) rather than dumping it here.
    Decisions-needed content stays focused on items that REQUIRE
    narrative judgment ideation is uniquely positioned to surface
    AND for which the operator's engagement actively unblocks the
    next cycle — focus-rotation decisions, residual-risk
    acceptances awaiting sign-off, escalations.

    (Carried) discipline: a bullet may carry from the prior
    cycle ONLY if you re-articulate the operator action and
    unblock-condition this cycle. Pure copy-paste of last cycle's
    text is forbidden.

    ## Proposals this cycle
    List the 3 task TB-Ns you're about to add (or fewer if Backlog
    already has ≥3 workable items, in which case write
    "Backlog already populated; no proposals this cycle").

Citation rule: every "Progress so far" / "Gaps" bullet MUST cite
at least one TB-N (`TB-79`, `TB-85`). Vague claims without TB-N
citations are forbidden — they hide hallucination.

## Cron proposals from task agents (TB-146)
Task agents emit `cron_proposed` events via the `cron_propose` MCP
tool when they spot work that should fire on a schedule. Per TB-146,
no agent — including ideation — has `cron_edit` and so cannot adopt
those proposals: cron schedule mutation is operator-CLI-only via
`ap2 cron edit ...`. If you see one or more unadopted
`cron_proposed` events in the recent-events block, SURFACE them
in your per-cycle assessment (e.g. an "Open questions for
operator" entry naming the proposal + rationale) so the operator
can review and adopt manually. Do NOT propose a task whose only
purpose is to adopt a `cron_proposed` event — the operator owns
that promotion path.

Length cap: keep the file under ~200 lines. If you can't, the
assessment is too verbose; trim to the highest-signal items.

Only AFTER writing the assessment, do follow-up discovery + ranking
below. Each proposal you make should map to a specific gap line in
your assessment (you don't need 1:1 — sometimes one task addresses
multiple gaps — but every proposal must be traceable to a gap).

## Step 0.5: read project insights (TB-89)
Read `.cc-autopilot/insights/_index.md` (regenerated by ap2 before
this prompt fires). Each line summarizes a markdown file in
`.cc-autopilot/insights/` capturing project-output knowledge:
metric thresholds, calibration findings, evaluation results,
decisions. Use the index to GROUND your assessment and proposals —
check rankings against any thresholds the project has measured,
cite specific insight files in your reasoning, and flag stale
insights (>30 days old) as gaps in the assessment's "Open
questions for operator" section.

Dive into a specific insight file ONLY when its topic is directly
relevant to a current focus item or a gap you're ranking. Don't
read every file — that's what the index is for.

If your assessment reveals a gap that grounded data would close
(e.g., "we don't know the Sharpe floor for production-ready
strategies"), propose ONE `#evaluation`-tagged task whose briefing
instructs the agent to compute the needed signal and write the
result as a new file in `.cc-autopilot/insights/<topic>.md` with
proper YAML front matter (`tldr`, `updated`, `updated_by` set to
the task's TB-N, `cites` list). Don't auto-cascade evaluation
tasks — propose them reactively, ONE per cycle at most, only when
a gap genuinely needs grounding before the next greenfield
proposal can be ranked.

## Follow-up discovery (do this BEFORE greenfield ideation)
Look at the most recent ~10 entries in TASKS.md `## Complete` and the
matching sections in progress.md. For each, ask:
  - What natural next step did this completion enable?
  - What edge case wasn't addressed?
  - What instrumentation does this new feature need?
Collect follow-up candidates from this scan first. Only fall back to
greenfield ideas if you're short of 3 candidates afterwards.

## Step 1.5: failure review (TB-88 — do this between Complete-scan and ranking)
Scan up to 5 most-recent failed-or-flagged tasks. Sources:
  - All TB-Ns currently in TASKS.md `## Frozen` (retry-exhausted).
  - Tasks with recent `verification_failed`, `retry_exhausted`, or
    `verification_partial` events in the prompt's events block (those
    are still in retry budget or already in Complete; treat all three
    classes as candidates for follow-up).

For each failed task, READ:
  - Its briefing file (path in the `[→ brief](...)` link).
  - The matching `verification_failed` / `verification_partial` event(s)
    — note which criterion failed or stayed `unverified` and the `notes`
    field (often shows `exit=127`, `No such file or directory`, or for
    partial: SDK-judge timeout / malformed-JSON / "couldn't reach a
    confident verdict on a prose bullet").
  - Any prior commits via the `git_log_grep` MCP tool — call
    `git_log_grep(query="<TASK_ID>", max_results=20)` and read the
    one-line summaries it returns. The agent may have committed
    partial work even if verification failed, so the implementation
    may already be on disk. (You don't have Bash; this MCP tool is
    the only way to query git history.)

`verification_partial` specifics: a `partial` verdict means at least one
bullet was `unverified` (typically a prose bullet whose SDK judge
couldn't confidently classify) but no bullets explicitly failed. The
task lands in Complete anyway. If the same prose bullet keeps coming
back `unverified` across tasks, classify as **edit-briefing** and
propose rewriting it as a concrete shell check (`test -f`, `pytest`,
`grep`, etc.) — prose criteria that the judge can't evaluate are useless
as a verification gate.

CLASSIFY each into ONE of:

1. **edit-briefing** — implementation work was correct but a shell
   pitfall or ambiguous criterion in the briefing's `## Verification`
   caused the failure. Heuristic: every `verification_failed` event
   shows the same bullet failing with `exit=127` / `command not found`
   / `No such file or directory` / similar shell shape, AND
   `git_log_grep` shows commits with file changes that plausibly
   cover the briefing scope. Action: propose ONE meta fix-task
   tagged `#fix-briefing` whose briefing instructs the agent to
   rewrite the broken bullets in the original briefing file. Cite
   the failed criterion verbatim and explain the fix. After it lands,
   note in `ideation_state.md` "Decisions needed from operator" that
   the original task is ready for `ap2 unfreeze TB-N`.

2. **split** — briefing scope was too large or mixed distinct
   concerns. Heuristic: briefing's `## Verification` has >7 criteria,
   OR the briefing's `## Scope` says something like "implement X AND
   launch Y AND update Z" (the TB-78-stoch anti-pattern: "implement
   infrastructure and launch sweep pipeline"). Action: propose 2-3
   narrower Backlog tasks each with a focused `## Verification`
   covering a slice of the original. Don't modify the original
   task's line — note in `ideation_state.md` "Considered & deferred"
   that the original is superseded by the new TB-Ns and can be
   operator-deleted.

3. **follow-up** — failure mode was environmental, conceptual, or
   exposes an unanticipated requirement. Heuristic: the failure is
   neither a shell-shape issue NOR a scope-overflow issue.
   Example: TB-91-stoch's verification called `python -m stoch
   daily-pipeline` but `daily-pipeline` isn't a real subcommand —
   the right next step isn't "fix the bullet" or "split the task",
   it's "investigate which CLI subcommand actually wires the
   intended flow before re-attempting." Action: propose a NEW
   Backlog task that takes the right next step. The original
   stays Frozen until the new task surfaces something actionable.

4. **abandon** — the task is no longer worth pursuing (goal
   exhausted, approach fundamentally flawed, area now covered by
   goal.md Non-goals). Heuristic: significant overlap with
   already-Complete TB-Ns, OR `goal.md` Non-goals appears to
   cover the area, OR the approach was fundamentally infeasible.
   Action: do NOT delete (operator owns deletes). Write a one-line
   entry in your `ideation_state.md` "Decisions needed from operator"
   section: `Decision needed: Recommend abandoning TB-N — reason:
   <X>` (the actionable-shape phrasing so the operator sees the
   explicit delete/keep ask, not a passive observation). Do NOT
   auto-add a remediation task.

When uncertain between edit-briefing and follow-up, default to
edit-briefing (cheaper to attempt).

Failure-remediation proposals compete with greenfield against the
same Backlog<3 budget — they're not special-cased. Rank them by
the same goal-alignment / impact / freshness criteria as any
other proposal.

Do NOT auto-unfreeze the original task. Operator decides via
`ap2 unfreeze TB-N` after reviewing the fix or replacement.

## Ranking
Rank candidates by:
  - alignment with goal.md (Mission, Current focus; respect Non-goals)
  - impact relative to current project state
  - freshness of the seed (a follow-up to yesterday's task beats one
    to last month's)

Propose the top 3 via board_edit (action: add_backlog) with a
structured briefing. Do not add duplicates.

## Human-review gate (TB-121 — load-bearing)
Every task you propose MUST be gated behind operator review before it
dispatches. Pass `blocked_on: "review"` to every `board_edit({"action":
"add_backlog", ...})` call you make this cycle — that lands the task
with a `@blocked:review` codespan on its TASKS.md line (rendered shape:
the literal phrase `blocked on: review` in the codespan). The board's
`_is_blocker_satisfied("review")` returns False, so auto-promotion
skips these tasks indefinitely until the operator runs `ap2 approve
TB-N` (CLI) or asks the Mattermost handler to `approve TB-N` (chat).
Both routes call the shared `_approve_review_token` helper, which
strips the `review` token from the codespan; on the next tick the
task auto-promotes through the normal Backlog → Ready dispatch path.

This is the ONLY pipeline that creates work without a human at the
keyboard (`ap2 add` is operator-driven; the MM handler is chat-driven;
`migrate-to-ap2` runs once with operator intent). Without the gate, an
ideation hallucination is one tick away from the daemon spending an
hour committing code the operator never asked for. Verification gates
don't catch this on their own — the task agent's verification bullets
are your invention, so it'll satisfy them by definition.

Do NOT skip the clause for "obvious" or "trivial" tasks. The operator
decides what's trivial; ideation does not. The clause is uniform
across every proposal you emit. Do NOT include tasks awaiting review
or `TB-N awaiting approval` bullets (the "tasks-awaiting-review"
content shape) in your `ideation_state.md` "Decisions needed from
operator" section (TB-182 / TB-191) — `ap2 status` and the cron
status-report's snapshot block surface those mechanically from
current board state per run (TB-151 / TB-173), and forwarding a stale
copy across the gap between ideation cycles risks contradicting the
fresh list in the same post.

## Goal-anchor requirement (TB-161 — load-bearing)
Every briefing you propose MUST have its `## Goal` body cite, as a
substring, either a `goal.md` `## Current focus` heading title (e.g.
`Current focus: ideation quality`), a `## Done when` heading title, or
4-6 words from any `## Done when` bullet. The queue-append validator
rejects briefings whose `## Goal` body matches no anchor — closes the
"gap-covering without drift" failure mode (a structurally-canonical
proposal whose value is only "make ap2 itself nicer", unconnected to
any operator-stated focus item). When in doubt, quote the focus-item
heading verbatim or paste a Done-when bullet's leading words into the
Goal text. The check is skipped when goal.md is missing or
all-placeholder, but for a real `goal.md` this is a hard gate.

## Why-now requirement (TB-164 — load-bearing — goal.md's delete-test)
Every briefing you propose MUST also include a line-anchored
`Why now:` paragraph inside its `## Goal` body, ≥40 chars after the
marker, answering goal.md's delete-test (lines 61-70):

> if we delete this and the goal still ships, was it useful?

Render it as its own paragraph inside `## Goal`, on a line whose
first non-whitespace token is `Why now`:

    ## Goal

    <one paragraph framing the proposal in goal-anchored terms>

    Why now: <one sentence naming the specific failure mode this
    closes, or the gap it fills, in goal-aligned language>

The queue-append validator (`_validate_briefing_structure`) rejects
briefings whose `## Goal` body has no `Why now` marker OR a trivial
one (e.g. `Why now: yes` is too short). The marker check is
line-anchored — `why now` appearing mid-prose ("the question of why
now is hard…") does NOT satisfy the gate.

This closes the "push for progress without scope creep" failure mode
(goal.md lines 61-70): every proposal needs to pass the delete-test
articulated in writing rather than hand-waved. Reject ideas whose
only justification is "this would be cool" or "it might be useful
later" — the Why-now sentence is the place that justification gets
written down (and refused, if it's that thin).

## Briefing requirements (load-bearing — TB-69 verifier reads these)
Every briefing you write MUST include a `## Verification` section with
concrete acceptance bullets that the per-task verifier can evaluate:
  - Prefer backtick-fenced shell commands at the START of the bullet
    (e.g. `- \`uv run pytest -q\` — full suite passes`); the verifier
    runs them automatically and exit 0 = pass.
  - Prose bullets are allowed for criteria a shell command can't
    express; they're judged by an SDK call against the diff.
A briefing with no `## Verification` section will be skipped by the
verifier (legacy compat). New work should not opt into that path.

**Every `## Verification` bullet must be auto-verifiable** (TB-138).
The per-task verifier runs unattended — it has the diff, the working
tree, and a shell. It does NOT have a live operator, a running
deployment, or any way to observe an out-of-band action. Three valid
shapes:

  1. **Backticked shell command** the verifier can `/bin/sh -c`
     (e.g. `\`uv run pytest -q\``, `\`test -f reports/foo.csv\``,
     `\`grep -q "auto-verifiable" ap2/init.py\``).
  2. **Unit / e2e test name** the regression gate covers
     (e.g. "new test `test_mm_handler_replies_within_30s` in
     `ap2/tests/test_mattermost.py` pins the responsiveness claim").
  3. **Prose claim a judge can confirm against the diff or working
     tree** — must name a concrete file/symbol the SDK judge can
     `Read` or `Grep` (e.g. "`Daemon.main_loop` in `ap2/daemon.py`
     splits into `_main_tick_loop` + `_mm_loop` with
     `asyncio.gather`").

**No `Manual:` bullets. No "operator runs X live and observes Y"
steps.** TB-122 hit this on 2026-05-01: 5/6 bullets passed, but a
single `Manual: kick a long-running task on stoch, mention
@claude-bot status → handler replies in <30s` kept failing because
the verifier (correctly) cannot observe a live operator action →
retry_exhausted, task re-frozen despite the implementation being
complete. The fix: convert the manual procedure to an e2e test with
stubbed dependencies. For the TB-122 case: stub a slow SDK reply,
enqueue a Mattermost mention, assert the handler's
`mattermost_reply` event lands within 30s of the mention timestamp —
pins the same responsiveness claim end-to-end without a live
deployment.

If a behavior genuinely cannot be auto-verified (rare), it does NOT
belong in the gating `## Verification` section — put it in `## Out
of scope` instead. Do not invent a separate `## Manual checklist`
section: if you can't write a test for it, the daemon can't gate
on it, and it's out of scope.

The validator now also rejects this at queue-append time (TB-171):
`_validate_briefing_structure` scans the `## Verification` body for
`Manual:` / `[manual]` bullets and refuses the briefing before a
TB-N is allocated, mirroring the operator-facing `ap2 check`
warning into the pre-allocation gate.

## Long-running work
If a proposed task plausibly takes more than ~5 minutes wall-clock
(parameter sweeps, multi-day backtests, full-history data fetches
against rate-limited APIs, ML training), write the briefing exactly
like any other — concrete scope plus a `## Verification` section that
checks output artifacts (`test -f reports/<name>/grid.csv`, JSON
schema checks, `uv run pytest -q tests/test_<feature>.py`).
The task agent has a `pipeline_task_start` MCP tool available and
decides at run time whether to dispatch via that tool or run inline.
Either way, the daemon evaluates the same `## Verification` section
once the work has finished.

## Shell-bullet pitfalls to AVOID (TB-76 — observed in prod)
The verifier runs each shell bullet via `/bin/sh -c <bullet>` in the
project root. Common mistakes that fail with exit 127 / 126 even when
the underlying work is correct:
  - **Bare `python`** — the daemon's environment has `uv run python`,
    `python3`, `.venv/bin/python` — but typically NOT bare `python`
    on PATH. Use `\`uv run python ...\`` (preferred for repos using uv)
    or `\`python3 ...\``, never bare `\`python ...\``.
  - **Bare path as command** — `\`reports/foo/README.md\`` makes shell
    try to *execute* the markdown file (exit 126). For existence
    checks use `\`test -f reports/foo/README.md\``; for line-count
    bounds use `\`[ "$(wc -l < reports/foo/README.md)" -le 30 ]\``.
  - **Multi-line shell bullets** — keep the bullet's command on one
    line. Wrap multiple steps in `\`bash -c '...'\`` or `&&`-chain.
Prefer running concrete project commands (e.g. `uv run pytest -q`,
`uv run python -m stoch ...`) over inventing new ones.
