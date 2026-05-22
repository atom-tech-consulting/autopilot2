# Project Goals

## Mission

ap2 is a meta-system: an autonomous development loop that drives a target
project toward its operator-stated goal with minimal human intervention.
The operator declares "this is what success looks like" once (in the target
project's `goal.md`); ap2 plans, dispatches, verifies, and recovers across
many task cycles until the target project actually achieves it.

Concretely: an operator should be able to point ap2 at a project, set the
goal, and walk away — returning to find the project measurably closer to
that goal. Failure modes (verification fails, retries exhaust, cron drifts,
etc.) are recovered automatically; only true ambiguity (genuine design
forks, scope decisions outside the declared goal) escalates to the
operator via Mattermost.

## Done when

ap2 itself is infrastructure — its value is continuous operation, not
retirement — but a target project's `goal.md` should always carry an
explicit `## Done when` section so ideation can tell "more work" from
"goal achieved" each cycle. Without one, the only done-signal is the
operator manually intervening, which defeats the walk-away promise.

For ap2's own infrastructure, the practical "done enough" thresholds are:

- An operator can point ap2 at a fresh project, paste a `goal.md` (with
  Mission + `## Done when`), and walk away for a week without intervention.
- Failure recovery (verification fails, retries exhaust, daemon restart,
  cron drift, agent timeouts) is fully automatic; only genuine design
  forks escalate.
- Ideation reliably proposes goal-aligned next steps that substantively
  advance the goal (not just goal-shaped pro-forma compliance), without
  drifting into ap2-meta polish or scope creep, and stops proposing when
  the target project's `## Done when` criteria are all met.

## Current focus: end-to-end automation

The code-quality consolidation focus has been substantively addressed:
the recent task arc (TB-203 → TB-220) closed the major gaps on testing
coverage (env-knob / MCP-tool / event-type / CLI-verb drift gates and
the test-presence mirror), operator-facing documentation (the
`## Authoring goal.md` section restructured, the CLI-verb reference
table landed, the howto-vs-goal.md content coupling decoupled), code
reusability (`_locked` / `_short` / `_now` / `_read_pid` /
`_collect_cli_verbs` extracted), and code cleanness (the verifier's
prose-vs-shell classifier tightened, the title-asterisk validation
gate added). The legibility-and-confidence foundation now exists.

The limiting factor on mission progress shifts to a different axis:
operator-in-the-loop bottlenecks. The mission says "walk away for a
week without intervention" — but today every ideation-proposed task
requires `ap2 approve`, every retry-exhausted task requires
`ap2 unfreeze`, and every focus rotation requires manual `ap2
update-goal`. A representative operator session in this codebase
approves 10-20 tasks; that's not walking away, that's approving
constantly. The Mission's walk-away promise is currently aspirational,
not deliverable.

This focus closes the gap by relaxing the operator-in-the-loop
defaults on surfaces where upstream gates already provide safety,
while keeping the operator-only path for surfaces where judgment
genuinely can't be automated (goal mutations, git pushes, focus
direction). The framing: every operator action that the current
codebase REQUIRES on every cycle is a candidate for opt-in
automation — automation that the operator can enable when they've
verified the upstream gates are trustworthy and disable when they
haven't.

Four axes form this focus. Each has its own failure mode to detect:

(1) **Manual-approval bottleneck**: every ideation-proposed task today
lands with `@blocked:review` and requires explicit `ap2 approve TB-N`
to dispatch. The upstream gates that ALREADY make this safe in
practice — briefing structural validation (TB-161 anchor check, TB-164
Why-now check, TB-171 manual-bullet reject), goal-alignment validation
(TB-161 anchor match), per-task verification (every briefing carries
auto-verifiable acceptance criteria), retry budget (3 attempts before
Frozen), and rollback (`ap2 rollback` walks back N tasks) — are not
deployed in concert because the operator-approve gate sits between
them and dispatch. Deliver: an opt-in auto-approve mode that bypasses
`@blocked:review` when the operator has verified the upstream gates,
with tag-based opt-out for high-risk shapes and a cumulative-regression
pause for safety. Delete-test: if this work didn't ship, the walk-away
promise stays fiction; with it, the cost-of-walking-away drops from
"approve constantly" to "set the knob, monitor occasionally." The
delete-test passes substantively.

(2) **Failure-recovery operator dependency**: retry-exhausted tasks
land in Frozen and require `ap2 unfreeze TB-N` to re-dispatch. In the
recent task arc, multiple Frozen states were resolved by trivial
briefing-shape edits the agent self-diagnosed in its blocked status
summary (TB-204's `grep -lE` → `grep -rlE`, TB-207's literal-backtick
in shell bullets). The daemon could apply agent-diagnosed briefing
patches automatically when the agent's `task_complete blocked` summary
names a concrete syntactic fix, with operator-curated guardrails on
which fix-shapes are auto-applicable. Delete-test: if this work didn't
ship, every briefing-shape regression cascades into operator-manual
unfreeze; with it, the loop self-heals on the recurring class.

(3) **Cost and blast-radius guards**: auto-approval shifts the
bottleneck from operator judgment to system safety. The current cost
accounting (token usage per SDK call, captured in `control_run_usage`
/ `task_run_usage` events) is observable but not gated. A bounded
auto-approve mode needs cost ceilings (cumulative-task budget per
window, per-task spend cap), regression pauses (N consecutive
verification-failed-to-Frozen → halt auto-promote until operator
acks), and unscheduled-failure detection (verifier returns "task_error"
not "verification_failed" → infrastructure issue, halt and surface).
Delete-test: if this work didn't ship, an auto-approve mode is
unbounded-blast-radius; with it, the safety floor catches the patterns
the operator's per-task review currently catches.

(4) **Multi-focus sequential execution**: `goal.md` supports a list of
`## Current focus:` headings in priority order (top = active). ap2
works the topmost focus until it's exhausted, then advances its
internal pointer to the next — without operator-mediated rotation.
The exhaustion gate is mixed: each focus can optionally carry a
`Done when:` sub-block listing concrete completion criteria, in which
case ideation gates advancement on those criteria being substantively
met; foci without an explicit `Done when:` fall back to a heuristic
(N consecutive 0-proposal cycles against the focus, configurable via
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`, default 3). The daemon advances by
updating an in-memory pointer + emitting `focus_advanced from=<old>
to=<new>` to the event log — it never mutates `goal.md` itself
(operator still owns the file; adding, reordering, or retiring foci
remains `ap2 update-goal`-only). When all foci exhaust, the daemon
emits a `roadmap_complete` decisions-needed entry and halts
auto-approval until the operator extends the roadmap. Failure mode
this closes: today's single-focus model caps walk-away time at the
focus's natural exhaustion point — when one focus's gaps are
addressed, ideation has nothing valuable to propose until the
operator manually rotates, forcing intervention at exactly the moment
the loop should be most productive. Delete-test: if this work didn't
ship, walk-away time is bounded by single-focus exhaustion (typically
days-to-weeks); with it, walk-away time scales with the
operator-declared roadmap length (weeks-to-months).

These four are mutually reinforcing. The auto-approval mode (#1) is
the most concrete deliverable and unblocks the others — once an
operator-trusted auto-approve loop exists, the failure-recovery
automation (#2) has a clear deployment target (the same opt-in mode),
the cost guards (#3) gate the same auto-approval surface, and the
multi-focus advance surface (#4) extends walk-away by removing
operator-mediated rotation from the per-epoch burden. The delete-test
for any proposed work in this focus: would the operator's
walk-away-time materially increase? If the answer is "no, the
operator still has to approve / unfreeze / update-goal manually,"
the work isn't paying rent — pro-forma automation that doesn't
deliver autonomy is its own failure mode.

Side note: continue capturing operator-decision signal as it surfaces
during this focus. Rejection reasons, classification verdicts,
briefing edits flow into `operator_log.md` passively via the
queue-routed verbs. Any auto-approve-mode pattern noticed (e.g., "the
auto-approved task hit a class of regression that should have gated
on tag X — pin tag X as auto-approve-gating") belongs in
`ideation_state.md`'s "Open questions for operator" or as an operator
log ack, so future auto-approve guard-tuning starts with fresh
evidence.

## Current focus: operator-legible reporting and monitoring

The end-to-end-automation focus is exhausted — auto-approve, auto-unfreeze,
cost guards, and multi-focus advance all shipped, so the operator *can* now
walk away. But "walk away and stay informed" has a second half the loop never
addressed: when the operator checks back, the reporting surface doesn't
actually inform them efficiently.

The limiting factor shifts from "can the operator leave?" to "when they glance
at an update, do they understand it?" Today's status-report cron fires on a
fixed clock, refers to work by bare `TB-N` and aggregate board counts (`3B`,
`1A`), and repeats near-identical content tick after tick. An operator running
several ap2 projects in parallel — who does not hold any one project's
TB-numbering in their head — has to ask follow-ups or open the project to
decode a report. It informs poorly and repeats noisily, so it gets tuned out —
defeating the monitoring half of the walk-away promise as surely as constant
approvals defeated the automation half.

This focus makes the operator-facing reporting/monitoring surface smart about
when to speak and rich enough to be understood cold. Framing: every report
should be reviewable by an operator who hasn't looked at this project since the
last report and is juggling others — self-contained, project-identified,
delta-focused, fired only when there's something worth saying.

(1) **Context-poor content**: reports identify work by bare `TB-N` + counts,
assuming the reader holds the board in their head. Deliver: reports that name
tasks by title + a one-line "what/why", lead with project identity, and frame
state so a cold reader understands it without opening the repo. Delete-test: if
not shipped, the operator must look up every TB-N and re-derive state from
counts — the report indexes, it doesn't inform.

(2) **Clock-driven repetition**: the cron fires on a fixed interval and
re-states unchanged content; the existing "skip if no activity" gate is too
coarse (suppresses only the fully-idle case, not near-duplicate repetition).
Deliver: significance-gated, delta-based reporting — fires on report-worthy
events (task completed / failed / frozen, focus advanced, decision needed,
anomaly, milestone), reports the change since last update rather than
re-stating the board, with explicit dedup so two consecutive reports never
repeat unchanged content. Delete-test: if not shipped, clock-driven noise
trains the operator to ignore the channel, burying the signal.

(3) **Shallow monitoring**: the periodic report is the only push surface;
attention-needing conditions (a task stuck / looping, repeated verification
failures, validator-judge noise, cost-cap approach, a pending decision) are
buried in it or only visible by pulling `ap2 status`. Deliver: proactive,
legible surfacing of those conditions so a multi-project operator can triage at
a glance which project needs them and why, in plain terms — not raw event types
or counts. Delete-test: if not shipped, monitoring stays pull-not-push; the
operator must poll each project to find problems.

Done when:
- A status report identifies tasks by title + one-line summary (never bare
  `TB-N` alone) and leads with the project name — a reader who hasn't seen the
  project since the last report understands it without a follow-up or opening
  the repo.
- Reports are significance-gated and delta-based: no two consecutive reports
  repeat unchanged content; a report fires on report-worthy change, not purely
  on the clock.
- Attention-needing conditions (stuck / failed / frozen tasks, decisions-needed,
  cost or validator-judge anomalies) are surfaced proactively in
  operator-legible terms, distinct from routine progress updates.

Scope guard: per-project legibility, NOT cross-project aggregation — each
daemon's reports stand alone and identify their own project (cross-project
orchestration stays a non-goal). Refinements change HOW the status /
decisions surfaces speak, not whether the skip-when-idle and escalation
behaviors exist.

## Non-goals

- **Generic task scheduler / project management tool**: ap2 is opinionated
  about agent-driven dev work. Don't add features whose primary use case
  is "a human tracking their own todos" — those compete with existing
  tools and dilute the loop.
- **Replacing operator judgment on goal definition**: the operator owns
  `goal.md`. ap2 doesn't propose new mission statements; it executes
  against the one it's given. Focus-rotation proposals (axis 4) surface
  recommendations for operator review; they do not auto-rewrite goal.md.
- **Multi-tenancy / shared sandbox**: one operator, one sandbox user,
  one daemon. Multi-tenant isolation is not on the path.
- **Real-time collaboration**: Mattermost is the human-loop channel, but
  the loop is async — operator nudges, daemon ticks, agent commits.
  Synchronous chat-driven editing (operator types, agent responds in
  real-time) is out of scope; the chat surface is for control, ack,
  approvals, and status — not for pair-programming.
- **Cross-project orchestration**: each project has its own ap2 daemon
  + state. ap2 doesn't aggregate across projects or propose work in one
  project based on activity in another.
- **Unconditional automation**: auto-approve, auto-unfreeze, and any
  other operator-in-the-loop relaxation are OPT-IN env knobs with
  conservative defaults. ap2 does not silently bypass the operator
  surface; relaxations are operator-curated trust upgrades.
- **Goal.md auto-rotation**: the operator owns the focus list. ap2
  advances its internal "topmost active focus" pointer based on
  exhaustion signals but never mutates `goal.md` itself — adding,
  reordering, or retiring foci is operator-only via `ap2 update-goal`.
  Auto-advance is a runtime pointer change, not a docs change.

## Constraints

- **Single-process daemon, file-state-only**: shared state lives on disk
  under `.cc-autopilot/`. No database, no message broker. Recovery is
  always "read files, resume."
- **Anthropic SDK + Claude Code CLI**: agent runs are `sdk.query()`
  invocations against the bundled Claude Code binary. Token cost is the
  operational constraint, not API rate limits.
- **OAuth auth (CLAUDE_CODE_OAUTH_TOKEN)**: not API-key. Features that
  require API-key (custom betas) are out of reach.
- **macOS + Linux POSIX shells**: no cross-platform Windows support.
- **No external mutation by task agents**: fenced files (the board, the
  goal, the daemon's state files) are agent-untouchable. Only the
  operator and the daemon mutate them.
- **Verification is gating**: every task lands with auto-verifiable
  acceptance criteria the daemon can evaluate unattended. No manual-step
  gating bullets.
- **Operator-in-the-loop is configurable per surface, conservative by
  default**: ideation proposals, retry-exhausted tasks, focus
  advancement, and other per-cycle operator gates default to requiring
  operator action. Relaxations are opt-in via env knobs
  (`AP2_AUTO_APPROVE`, `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`, future
  siblings) with documented safety gates (tag-based opt-out,
  cumulative-regression pause, all-foci-exhaust halt). Goal mutations
  (`goal.md` content — including the focus list itself), git pushes,
  and cron schedule changes remain operator-CLI-only by design —
  they're either irreversible or set direction for everything
  downstream.
