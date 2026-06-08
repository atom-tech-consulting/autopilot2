# Project Goals

## Mission

ap2 is a meta-system: an autonomous development loop that drives a target
project toward its operator-stated goal. The operator declares "this is
what success looks like" once (in the target project's `goal.md`); ap2
plans, dispatches, verifies, and reports across many task cycles until
the target project achieves it.

Architecturally, ap2 is structured as a small **core** (dispatch-verify-
report loop, briefing validators, audit trail, CLI surface, status-report
digest composition) plus a set of **components** (auto-approve,
auto-unfreeze, attention detectors, multi-focus + focus auto-advance,
validator-judge LLM dep-coherence, Mattermost channel adapter, janitor,
...) that each opt into the loop via a shared registry. Every component
can be turned on or off independently via env flag; the component model
makes features composable without code surgery and is the structural
prerequisite for future distribution shapes (including a public OSS
cut).

Concretely: an operator should be able to point ap2 at a project, set
the goal, and walk away — returning to find the project measurably
closer to that goal. Failure modes (verification fails, retries
exhaust, cron drifts, etc.) are recovered automatically; only true
ambiguity (genuine design forks, scope decisions outside the declared
goal) escalates to the operator. Which surfaces escalate vs auto-
resolve is determined by which components are enabled.

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

For the component model specifically:

- Every existing module that wraps an autonomous behavior
  (auto-approve, auto-unfreeze, attention, focus-advance, janitor,
  validator-judge, Mattermost channel) lives under
  `ap2/components/<name>/` and is loaded via the component registry,
  not via direct import from `ap2/daemon.py` or other core modules.
- A CI gate fails the build if any core module directly imports
  from `ap2/components/<name>/`. All cross-references flow through
  the registry's hook protocol.
- Every component can be independently disabled via its env flag; the
  full test suite passes in the default configuration AND in an
  "every component disabled" configuration. (Component-specific tests
  may be marked `requires_component=<name>` and skip when off.)
- Existing env-knob names are preserved for backwards compatibility
  (`AP2_AUTO_APPROVE`, `AP2_ATTENTION_IMMEDIATE_PUSH`,
  `AP2_FOCUS_AUTO_ADVANCE_DISABLED`, etc. still work unchanged from
  the operator's perspective).
- A future "OSS distribution" focus can be defined entirely in terms
  of "which components default to enabled" plus packaging extras —
  no further structural refactor required.

For the structured-config focus specifically:

- A fresh `ap2 init` writes a `.cc-autopilot/config.toml` with
  every tunable knob present as a commented default; operators
  discover knobs by reading one file, not by grepping howto.md.
- Each component's `manifest.py` declares a `config_schema` field;
  the registry validates the merged config (file + env overrides
  + defaults) at daemon-start with clear error messages on
  schema mismatch.
- ≥80% of `os.environ.get("AP2_*")` calls in source migrate to
  `cfg.<path>.<key>` reads. The exception list is true 12-factor
  knobs (secrets, deployment identity, etc.) documented in a
  single comment block.
- Existing `AP2_*` env names continue to work as overrides for
  one full release cycle (back-compat shim emits a one-shot
  `env_deprecated` event per process on first use of a
  deprecated name).
- A TB-305-style docs-drift gate enforces "every config schema
  key is mentioned in `ap2/howto.md`'s `## Configuration knobs`
  section."

## Current focus: extract the remaining core subsystems into components

The component refactor (shipped 2026-05-27) extracted the *opt-in autonomous
behaviors* — auto-approve, auto-unfreeze, attention, focus-advance, janitor,
validator-judge, mattermost. But the **core still carries cohesive,
tick-resident subsystems** wired directly into `daemon._tick`: the cron
dispatch loop and ideation (the `_maybe_ideate` gate + roadmap-exhaustion
halt). Each has the full component shape — a tick stage or hook, an owned
`AP2_*` knob cluster, its own events — yet lives in core, so `daemon.py` is
still a ~3,000-line monolith.

This focus extracts those tick subsystems (cron, ideation) into
`ap2/components/<name>/` behind the registry, shrinking core toward a minimal
kernel. It is purely structural — no behavior change, env-knob names
preserved exactly (same contract as the 2026-05-27 refactor). It does NOT
change dispatch/verify semantics, ideation logic, or any agent's behavior; it
relocates subsystems behind the registry. One axis is not an extraction but a
prerequisite *decoupling*: the auto-approve strip must move out of `board_edit`
into the `auto_approve` component before ideation can cleanly leave core.

Two things are deliberately **kept in core**, not extracted:
- **The pipeline subsystem** (`pipeline_task_start` + the Pipeline-Pending
  sweep). Its tool is offered directly to the *task agent* (core), and the
  board sections and post-agent task disposition it drives are core concerns,
  so it is not cleanly separable — extracting it would leave the coupling in
  core anyway.
- **Verification (shell-bullet path) and status-report composition** stay
  baseline core (verification is gating; reporting is baseline); only the
  optional LLM prose-judge is split out (axis 4).

(A public OSS distribution — packaging extras, a defaults-enabled policy, a
quickstart — is a separate downstream focus that this extraction unblocks but
does not itself deliver.)

Why now: codex support just shipped, so the backend layer is pluggable and the
core is otherwise stable — the cheapest moment to factor the last tick
subsystems before more features compound the coupling. A minimal
componentized core is the prerequisite for any future distribution shape: you
can't ship "ap2 + pick your components" while ideation / cron are welded into
the daemon.

Axes (each has its failure mode):

(1) **Cron component (scheduler + job-handler registry) + extended tick-phase
vocabulary** — the first tick-stage extraction, and the one that establishes
the new registry phases (e.g. `CRON_DISPATCH`, `IDEATION`) the later
extractions reuse. Relocate the cron *scheduler* (the `cron.yaml` /
`cron_state.json` interval engine, the `cron_*` lifecycle events, and the
`cron_propose` / `cron_edit` surface) into `ap2/components/cron/` behind a
cron-dispatch tick hook, and replace `run_cron`'s hardcoded `if job.name == …`
switch with a registered job-handler protocol: components and core contribute
named handlers, and the scheduler dispatches to them while knowing nothing of
what a job does. The shared `_run_control_agent` primitive stays in core (the
generic LLM-cron handler calls back into it); the status-report job stays a
**core-registered** handler (its composition is baseline core — see axis 4),
not a separate component. Delete-test: if the `job.name` switch survives — or
the phase vocabulary isn't pinned by this first extraction — every later
extraction re-invents it.

(2) **Decouple auto-approve from `board_edit` into a loop pass** — today the
auto-approve strip is evaluated *inside* `board_edit`'s `add_backlog` branch
(approval policy embedded in a mutation tool, evaluated mid-agent-run), and
the tags policy (`should_auto_approve` and friends) squats in `ideation.py`
and is reached from core — the cross-boundary knot that blocks axis 3. Make
`board_edit` policy-free (proposals are always born `@blocked:review`) and
move the gate chain + tags policy into the `auto_approve` component as a
discrete loop pass that runs after ideation and before dispatch, stripping
`@blocked:review` from Backlog tasks that clear the gates (the daemon
auto-running `ap2 approve`). Delete-test: if the strip stays in `board_edit`,
ideation can't be extracted without core→component import violations.

(3) **Ideation component** — extract `ap2/ideation.py` (the `_maybe_ideate`
trigger gate, the roadmap-exhaustion halt, proposal records, scrub
coordination) behind an ideation tick hook + the halt hook; owns the
`AP2_IDEATION_*` knob cluster, all `ideation_*` events, and
`AP2_IDEATION_DISABLED` as its `env_flag`. Sequenced after axis 1 establishes
the tick-stage shape and axis 2 unties the auto-approve coupling (largest
blast radius). Delete-test: if ideation stays in core, the kernel still
hard-depends on the proposal engine.

(4) **Extract the prose-judge into a `verifier_judge` component** — the
per-task verify runner (`verify.py:verify_task`) parses the Verification
section, runs shell bullets, and dispatches prose bullets to an LLM judge
(`_judge_prose_bullet`). Verification is gating, so the runner + the
deterministic shell-bullet path + aggregation stay in **core**; only the
optional LLM prose-judge moves into a `verifier_judge` component the runner
calls via the registry — mirroring the existing `validator_judge` component
(today `verifier_judge` is an agent kind welded into `verify.py`, not a
component). A deployment can then verify with shell bullets alone, prose-judge
disabled. Status-report composition is likewise baseline and stays a core
rendering library (scheduling is cron, delivery is the channel component) — it
is NOT extracted. Delete-test: if the prose-judge stays inside the verify
runner, the LLM verification layer can't be disabled independently of the
gating shell-bullet path.

Sequencing: (1) is the first extraction and pins the tick-phase shape. (4) is
independent (it mirrors the existing `validator_judge` component, no new tick
phase needed). (2) decouples auto-approve and unblocks (3); ideation (3) lands
last (largest blast radius).

The delete-test for any work in this focus: does it move a core subsystem
behind the registry (preserving observable behavior), untangle a cross-
boundary coupling that blocks such a move, split an optional LLM layer out of
a baseline-core path, or extend the registry to express a new subsystem shape?
Polishing a subsystem's internals while it stays welded to `daemon._tick` is
not paying focus rent.

Progress signals:
- `daemon.py` no longer contains the cron loop or the ideation gate inline;
  each is an `ap2/components/<name>/` subpackage walked via the registry.
- `board_edit` carries no auto-approve policy; the strip runs as an
  `auto_approve` component loop pass.
- The prose-judge runs as a `verifier_judge` component (mirroring
  `validator_judge`); the verify runner + shell-bullet path stay in core.
- The pipeline subsystem and status-report composition remain in core (by
  design — not regressions).
- The import-direction CI gate (core never imports `ap2/components/`) still
  passes with the new components.
- The full suite passes with every component disabled, and a task
  dispatches → verifies (shell bullets) → reports in that minimal-kernel
  config.

## Shipped focus

Completed focus arcs (newest first). Durable criteria live on in "## Done
when" (component model, structured config) and "## Constraints" (pluggable
backend, per-backend auth); the entries below are provenance.

- **Codex support via an agent adaptor layer (2026-06-06)** — every agent
  dispatch flows through a backend-agnostic `AgentAdapter`; a
  `ClaudeCodeAdapter` (default) and a `CodexAdapter` (OpenAI `openai-codex`
  SDK, ChatGPT-login auth) are selectable per agent kind via
  `[agent_backends]` / `AP2_AGENT_BACKEND_<KIND>`. Validated 31/31 real-SDK
  smokes on both backends across all 9 agent kinds (dispatch, tool
  round-trips incl. `report_result` over a stdio-MCP bridge, judge verdicts,
  control agents, real file-edit+commit work).
- **Structured config: env → TOML (2026-05-29)** — runtime config moved to
  `.cc-autopilot/config.toml` with per-component `config_schema` validation,
  `AP2_*` env overrides preserved as a back-compat escape hatch, and
  `ap2 config list / get / set`.
- **Refactor features into opt-in components (2026-05-27)** — every
  autonomous behavior (auto-approve, auto-unfreeze, attention, focus-advance,
  janitor, validator-judge, mattermost) lives under `ap2/components/<name>/`
  behind the registry; core never imports components (CI import-direction
  gate); each is env-togglable. The current focus extends this to the
  remaining core subsystems.

## Non-goals

- **Generic task scheduler / project management tool**: ap2 is opinionated
  about agent-driven dev work. Don't add features whose primary use case
  is "a human tracking their own todos" — those compete with existing
  tools and dilute the loop.
- **Replacing operator judgment on goal definition**: the operator owns
  `goal.md`. ap2 doesn't propose new mission statements; it executes
  against the one it's given. Focus-rotation proposals (multi-focus
  component) surface recommendations for operator review; they do not
  auto-rewrite goal.md.
- **Multi-tenancy / shared sandbox**: one operator, one sandbox user,
  one daemon. Multi-tenant isolation is not on the path.
- **Real-time collaboration**: chat channels (when the relevant
  channel-adapter component is enabled) are the human-loop surface,
  but the loop is async — operator nudges, daemon ticks, agent
  commits. Synchronous chat-driven editing (operator types, agent
  responds in real-time) is out of scope; the chat surface is for
  control, ack, approvals, and status — not pair-programming.
- **Cross-project orchestration**: each project has its own ap2 daemon
  + state. ap2 doesn't aggregate across projects or propose work in one
  project based on activity in another.
- **Unconditional automation**: every component that bypasses an
  operator gate (auto-approve, auto-unfreeze, attention immediate-push,
  focus auto-advance, ...) is OPT-IN with conservative defaults. ap2
  does not silently bypass the operator surface; relaxations are
  operator-curated trust upgrades.
- **Goal.md auto-rotation**: the operator owns the focus list. The
  `focus_advance/` component advances its internal "topmost active
  focus" pointer based on exhaustion signals but never mutates
  `goal.md` itself — adding, reordering, or retiring foci is
  operator-only via `ap2 update-goal`. Auto-advance is a runtime
  pointer change, not a docs change.
- **Removing behavior during component extraction**: the component
  refactor moves modules into `ap2/components/` and gates them by
  env flag; it does not delete features. The internal install with
  all components enabled continues to do everything that works today,
  by every observable signal.
- **API stability commitments before the OSS cut**: the core surface
  stays fluid until the component-extraction focus is exhausted and
  the downstream OSS-distribution focus ships. We will not promise
  backwards compatibility on `ap2/core/` module signatures to
  internal callers during this refactor.

## Constraints

- **Single-process daemon, file-state-only**: shared state lives on disk
  under `.cc-autopilot/`. No database, no message broker. Recovery is
  always "read files, resume."
- **Pluggable agent backend (default Claude Code)**: agent runs dispatch
  through an `AgentAdapter` layer (shipped via the codex-support focus); the
  default and behaviour-reference adapter is `sdk.query()` against the
  bundled Claude Code binary, with a Codex adapter selectable per agent
  kind. Token cost is the operational constraint, not API rate limits.
- **Per-backend auth, OAuth for the Claude adapter**: the Claude adapter
  uses `CLAUDE_CODE_OAUTH_TOKEN` (not API-key — API-key-only betas are out
  of reach); a Codex adapter brings its own OpenAI credentials. The
  daemon-start gate requires creds for each backend the agent-backend map
  references.
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
  operator action. Relaxations are opt-in via components and their
  env knobs (`AP2_AUTO_APPROVE`, `AP2_FOCUS_AUTO_ADVANCE_DISABLED`,
  `AP2_ATTENTION_IMMEDIATE_PUSH`, future siblings) with documented
  safety gates (tag-based opt-out, cumulative-regression pause,
  all-foci-exhaust halt). Goal mutations (`goal.md` content —
  including the focus list itself), git pushes, and cron schedule
  changes remain operator-CLI-only by design — they're either
  irreversible or set direction for everything downstream.
