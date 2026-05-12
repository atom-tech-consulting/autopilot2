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

## Current focus: code quality

The data-collection infrastructure for evaluating ideation quality is
now in place (operator-decision capture via the reject/classify/update-goal
verbs, proposal-outcome records, token-cost accounting on every SDK call
site, rejection-reason injection into the prompt header). Future
iteration tuning ideation against this data depends on the underlying
codebase remaining legible and confidently modifiable. The focus shifts
from accumulating signal to consolidating the foundation that lets us
act on it: tests, docs, reusable helpers, and code cleanness.

Without consolidation, every prompt-shape tweak or behavior change risks
silent regression (no test net catches it), drift (no docs name the
invariant), parallel-bug-surface duplication (the same logic copied
across call sites), or cognitive load that slows future iteration
(undifferentiated multi-thousand-line modules). The signal-collection
work is only worthwhile if it feeds into changes we can confidently make.

Four axes form this focus. Each has its own failure mode to detect:

(1) **Testing coverage**: every shipped CLI verb, MCP tool, control-agent
path, and env-knob-flagged behavior has automated tests pinning the happy
path AND at least one error path. Delete-test for a proposed test: if
this test were deleted, would a regression risk become invisible? If no,
the test isn't paying rent — pro-forma test coverage that exercises
nothing real is its own failure mode.

(2) **Operator-facing documentation**: every operator surface (CLI
verb, env knob, MCP tool, fenced-file role, board section semantic) is
documented where an operator looks first — `ap2/howto.md` for workflow,
`ap2 <verb> --help` for usage, `ap2/architecture.md` for internals. The
failure mode is an operator who can't understand a surface from its
documented description and has to read source. Docs that paraphrase the
source without explaining the WHY are pro-forma documentation — same
delete-test applies.

(3) **Code reusability**: when a piece of logic appears at three or
more call sites with structural similarity, extract to a shared helper.
Threshold is three (not two) — premature abstraction is its own failure
mode. The failure mode for the OTHER direction is the same bug appearing
at multiple call sites because logic was copy-pasted instead of shared.

(4) **Code cleanness**: naming clarity, dead-code removal, comment
hygiene per project conventions (no `# TB-N:` prefix tags rotting in
source as TB-N references age, no multi-paragraph docstrings duplicating
what well-named identifiers already communicate, no comments narrating
what well-named code already shows). Long-running modules (`ap2/tools.py`
past 3700 lines, `ap2/daemon.py` past 2500, `ap2/cli.py` past 1700) get
decomposed along natural domain boundaries when the boundary becomes
clear from reading — not via speculative refactor.

These four are mutually reinforcing. Tests give confidence to refactor
for reusability and cleanness. Docs capture the invariants tests pin.
Reusable helpers reduce the volume of code that needs testing and
documenting in the first place. The delete-test for any proposed
code-quality work: would the codebase get noticeably less confidently
modifiable if this work didn't ship? If the answer is "no, things would
be about the same," it's pro-forma consolidation — the same shape of
goal-shaped pro-forma compliance the prior focus was set up to detect,
just applied to a different axis.

Side note: continue capturing operator-decision signal as it surfaces
during this focus. Rejection reasons, classification verdicts, briefing
edits flow into `operator_log.md` passively via the queue-routed verbs
without active iteration. Any prompt-shape pattern noticed during
code-quality work (e.g., "this proposal got rejected because X — that's
a class of failure mode worth pinning later") belongs in
`ideation_state.md`'s "Open questions for operator" or as an operator
log ack, so the eventual prompt-tuning iteration starts with fresh
evidence rather than gaps.

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
