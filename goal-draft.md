# Project Goals (draft — propose into goal.md)

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
- Ideation reliably proposes goal-aligned next steps without drifting
  into ap2-meta polish or scope creep, and stops proposing when the
  target project's `## Done when` criteria are all met.

## Current focus: ideation quality

Ideation is the engine that turns "what's the goal" into "what's the next
task." Today it's still the weakest link, even though the structural
guards are now in place: TB-121's review gate ships proposals as
`@blocked:review` so nothing dispatches without operator approval,
TB-138's auto-verifiable-bullet rule is pinned into the briefing prompt,
and TB-154 validates the canonical Goal/Scope/Design/Verification/
Out-of-scope shape at queue-append time so a malformed proposal is
rejected before TB-N is allocated. What remains is prompt-shape work —
the two failure modes we still want it to avoid:

(1) **Gap-covering without drift**: ideation should fill in work the
operator didn't think to enumerate, but every proposal must reduce to a
visible step toward the declared goal. Concretely, the prompt needs to:
- Re-read `goal.md` (when filled) and `progress.md` each cycle to know
  what the goal is and how far we've come.
- Reject proposals whose value is only "make ap2 itself nicer" unless
  ap2-improvement is on the project's goal path. Bias toward the target
  project's outcomes, not the meta-system's polish.
- Surface uncovered axes (test coverage gaps, missing error paths, doc
  staleness, performance regressions) but only when they connect to a
  concrete goal-relevant outcome.

(2) **Push for progress without scope creep**: ideation should be
ambitious — propose the *next meaningful chunk*, not the safest tiniest
step — but every proposal needs to pass an "if we delete this and the
goal still ships, was it useful?" test. Concretely:
- Reject feature additions whose only justification is "this would be
  cool" or "it might be useful later."
- Prefer compounding changes (a refactor that unblocks 3 future tasks)
  over isolated polish.
- When two paths to the same outcome exist, pick the one that creates
  fewer follow-up tasks, not the one that's faster to execute.

TB-121 (review gate), TB-138 (auto-verifiable-bullet rule), and TB-154
(canonical Goal/Scope/Design/Verification/Out-of-scope structural
validator at queue-append time) gave us the mechanical scaffolding;
the open work is folding the goal-relevance and scope-creep guards
above into the ideation prompt itself so proposals arrive already
filtered, not just gated after the fact.

## Non-goals

- **Generic task scheduler / project management tool**: ap2 is opinionated
  about agent-driven dev work. Don't add features whose primary use case
  is "a human tracking their own todos" — those compete with existing
  tools and dilute the loop.
- **Replacing operator judgment on goal definition**: the operator owns
  `goal.md`. ap2 doesn't propose new mission statements; it executes
  against the one it's given.
- **Multi-tenancy / shared sandbox**: one operator, one sandbox user,
  one daemon. TB-120 (kernel-level fence via split users) is frozen
  precisely because the multi-tenant case isn't on the path.
- **Real-time collaboration**: Mattermost is the human-loop channel,
  but the loop is async — operator nudges, daemon ticks, agent commits.
  Synchronous chat-driven editing (operator types, agent responds in
  real-time) is out of scope; the chat surface is for control, ack,
  approvals, and status — not for pair-programming.
- **Cross-project orchestration**: each project has its own ap2 daemon
  + state. ap2 doesn't aggregate across projects or propose work in
  one project based on activity in another.

## Constraints

- **Single-process daemon, file-state-only**: shared state lives on disk
  under `.cc-autopilot/`. No database, no message broker. Recovery is
  always "read files, resume."
- **Anthropic SDK + Claude Code CLI**: agent runs are `sdk.query()`
  invocations against the bundled Claude Code binary. Model choice
  configurable (today: Opus 4.7); token cost is the operational
  constraint, not API rate limits.
- **OAuth auth (CLAUDE_CODE_OAUTH_TOKEN)**: not API-key. 1M-context
  beta is engaged for Opus 4.7 under this auth (TB-139-era probe);
  features that require API-key (custom betas) are out of reach.
- **macOS + Linux POSIX shells**: shell bullets run via `/bin/bash`
  (TB-147); no cross-platform Windows support.
- **No external mutation by task agents**: fenced files (TASKS.md,
  CLAUDE.md, goal.md, .cc-autopilot/{progress,events,ideation_state,
  cron,operator_log}.md, operator_queue_state.json,
  operator_queue.jsonl) are agent-untouchable. Operator/daemon-only.
- **Verification is gating**: every task lands with a real
  `## Verification` section (TB-135 enforced) of auto-verifiable
  bullets only (TB-138 pins this in the briefing prompt; TB-154
  validates the canonical Goal/Scope/Design/Verification/Out-of-scope
  shape at queue-append time so non-canonical section names —
  `## Acceptance`, `## Tests` — are rejected before TB-N is
  allocated). No manual-step bullets; if a behavior isn't
  auto-checkable it's out-of-scope.
- **Operator-in-the-loop where work is irreversible**: ideation
  proposals require approval (TB-121 — landed; proposals carry
  `@blocked:review` until `ap2 approve TB-N`), cron schedule
  changes are operator-CLI-only (TB-146 — landed; `cron_edit` is
  hidden from every agent toolset, mutation goes through
  `ap2 cron edit`), git pushes are not automated.

## Design decisions for the ideation iteration

With TB-121 (review gate), TB-138 (auto-verifiable bullets), and TB-154
(canonical-structure validator) landed, the design questions for the
next ideation prompt iteration have been resolved as follows:

- **Done-signal lives in `goal.md`.** Every project's `goal.md` carries
  an explicit `## Done when` section (see this file's section above for
  ap2's own version); ideation reads it each cycle and treats all-met
  criteria as "stop proposing here." Without it the only done-signal is
  manual operator intervention, which defeats the walk-away promise.

- **Cadence is already per-project tunable — no change needed.**
  `AP2_IDEATION_COOLDOWN_S` (default 7200s = 2h) lets an operator set
  responsive or conservative cadence per project via the daemon env;
  no global-default rework required.

- **Ideation calibrates from prior-cycle outcomes — both approvals and
  rejections.** Approvals AND deletions of ideation-proposed tasks
  already land in `.cc-autopilot/operator_log.md` (every queued op
  writes an audit line — `applied operator-queued approve → TB-N`,
  `applied operator-queued delete → TB-N`), and ideation Step 0 reads
  that file as authoritative ground truth on what's been decided.
  Re-proposal of decided items isn't the live gap. The narrower
  load-bearing work is **capturing operator REASONS for rejection**:
  today `delete` records the action without a reason field, so
  ideation can't learn from "why this proposal didn't fly." TB-152
  (in Backlog, awaiting approval) addresses this with a dedicated
  `reject` verb that writes
  `<ts> — rejected ideation proposal → TB-N (<title>): <reason>`
  lines that ideation will pick up alongside the existing
  operator-decision audit trail.

- **Multi-task plans are allowed.** Nothing in the current prompt forbids
  a coherent N-task arc toward a milestone — the prompt's "top 3" framing
  reads as independent ranking, and the per-cycle cap (3 proposals when
  Backlog<3) is set in the prompt, not in code, so there's no hard
  numeric limit beyond what the prompt requests. The real friction is
  TB-121's per-task review gate: each task in an arc lands
  `@blocked:review` and needs its own `ap2 approve TB-N`, which costs
  arc cohesion. The prompt iteration should either accept that cost
  (each task in the arc must justify itself standalone) or design a
  plan-level approve flow — capturing that decision is part of the
  prompt-iteration work, not blocking it.
