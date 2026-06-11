---
name: ap2-ideation-goals
description: "Use when authoring or revising an ap2 project's `goal.md` (Mission / Done-when / Current focus / Non-goals / Constraints, the delete-test, and the TB-161/TB-164/TB-235 queue-time validators) or running the retrospective `ap2 audit` review of what shipped after a walk-away window."
---

# ap2 ideation & goal/focus management — authoring goal.md + the retrospective audit

The operator-facing reference for steering an ap2 loop's direction: how to
write the `goal.md` that ideation reads every cycle, and how to review what
the loop shipped once you come back from a window of unattended operation.
An operator should never have to hunt through the ap2 source to learn what each
goal.md section is for or how the retrospective `ap2 audit` walk works. Two
self-contained surfaces:

- **Authoring goal.md** — what each of the five operator-curated sections
  (Mission / Done when / Current focus / Non-goals / Constraints) is for,
  how ideation reads them, which queue-time validators (TB-161 anchor,
  TB-164 Why-now, TB-235 dependency-coherence) key off the content, and the
  **delete-test** that keeps `## Done when` bullets honest.
- **Retrospective audit workflow** — the `ap2 audit` verb: the "I just came
  back from a week of unattended operation, what shipped and what's worth a
  verdict?" review surface, its `--interactive` walkthrough, the filter
  flags, and the natural-cadence return surfaces that nudge the count.

This is operator-session tooling. The daemon ideation agent's *own*
briefing-authoring conventions (how a proposed task's briefing must be
shaped) stay canonical in `ap2/ideation.default.md` — this skill references
them but `ap2/ideation.default.md` is the source of truth for those.

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
  bucket distinct from `pro-forma`'s neutral-no-impact — see the
  **ap2-task** skill's `## Classify verdicts` reference) + optional reason; queues
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
