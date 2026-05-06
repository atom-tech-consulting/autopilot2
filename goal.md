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

## Current focus: ideation quality signal collection

Ideation is the engine that turns "what's the goal" into "what's the next
task" — and it's still the weakest link in the walk-away promise. Even
when proposals satisfy every structural validator, they often don't
substantively advance the goal. The currently-shipped mechanical guards
stop malformed proposals but not goal-shaped pro-forma ones — proposals
that satisfy every validator and still don't advance the goal in
substance. Improving prompt-side reasoning quality from here without
empirical data on what "advances the goal" looks like in practice is
gut-feel iteration.

The bottleneck is signal volume, not prompt-language craft. Before
further prompt-shape work, accumulate the operator-decision and
proposal-outcome data that lets the prompt be tuned against measurable
behavior rather than intuition. The specific shape of that
instrumentation is for ideation to derive — what to capture, where it
lives, how it surfaces back into the next cycle.

These signals are dual-purpose. In the near term they support evaluation:
the operator and the prompt-author tune ideation against evidence rather
than intuition. In the longer term they are themselves agent context —
ideation should be able to read its own track record and adapt its process
and prompt dynamically based on what has and hasn't worked. Design the
instrumentation with both audiences in mind from the start: structured,
agent-readable, and persistent across cycles, not just human-readable
metrics buried in a dashboard.

The failure mode signal collection exists to detect:
**goal-shaped pro-forma compliance** — proposals that cite the right
anchors, articulate a plausible rationale, satisfy every structural
validator, and still don't move the goal forward in substance. The
diagnostic is the delete-test: if you delete the proposal, does the goal
still ship unchanged? If yes, it was pro-forma. The shape varies (polish
of meta-system surfaces unrelated to the project's outcome; wack-a-mole
fixes that address one case without generalizing; safe tiny steps when
compounding moves are available) but the underlying problem is one:
ideation is being ambitious in motion without being ambitious in
substance.

## Non-goals

- **Generic task scheduler / project management tool**: ap2 is opinionated
  about agent-driven dev work. Don't add features whose primary use case
  is "a human tracking their own todos" — those compete with existing
  tools and dilute the loop.
- **Replacing operator judgment on goal definition**: the operator owns
  `goal.md`. ap2 doesn't propose new mission statements; it executes
  against the one it's given.
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
- **Operator-in-the-loop where work is irreversible**: ideation proposals
  require operator approval before dispatch; cron schedule changes are
  operator-CLI-only; git pushes are not automated.
