# Project Goals

## Mission

ap2 is a meta-system: an autonomous development loop that drives a target
project toward its operator-stated goal. The operator declares "this is
what success looks like" once (in the target project's `goal.md`); ap2
plans, dispatches, verifies, and reports across many task cycles until
the target project achieves it.

Architecturally, ap2 is structured as a small **core** (the dispatch-
verify-report loop, the deterministic baseline runners — shell-bullet
verification, briefing-structure validation, status-report composition —
the audit trail, and the CLI surface) plus a set of **components** that
each plug into the core loop via a shared registry. A component is a
*loop-level participant*: it registers on a tick phase or a coarse loop
surface (auto-approve, auto-unfreeze, attention, focus auto-advance,
janitor, cron, ideation, communication, ...). Things invoked only as an
internal sub-step of a core runner — the LLM judges over verification and
briefing-validation — are NOT components; they are swappable adapters the
runner calls. Every component can be turned on or off independently via
env flag; the model makes features composable without code surgery and is
the structural prerequisite for future distribution shapes (including a
public OSS cut).

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

- Every loop-level autonomous behavior (auto-approve, auto-unfreeze,
  attention, focus-advance, janitor, cron, ideation, communication)
  lives under `ap2/components/<name>/` and is loaded via the component
  registry, not via direct import from `ap2/daemon.py` or other core
  modules. The LLM judges (verification prose-judge, briefing
  dep-coherence) are NOT components — they are `select_adapter` layers
  the core runners call, disable-able via config.
- A CI gate fails the build if any core module directly imports
  from `ap2/components/<name>/`. All cross-references flow through the
  registry's single generic accessor — no per-kind registration
  methods, and no core→component `hook_points[...]` symbol lookups.
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

## Current focus: get the component boundary right — loop-level participants only

The 2026-05-27 refactor extracted the opt-in autonomous behaviors behind
the registry. The follow-on work exposed that the *component boundary
itself* was drawn loosely. Three problems: genuine loop subsystems (cron,
ideation) were still welded into `daemon._tick`; other things were modeled
as components that aren't loop participants at all (the LLM judges, invoked
deep inside core runners); and the registry grew a bespoke registration
method per extension kind (`channel_adapters()`, `briefing_validators()`,
`cron_job_handlers()`) plus blocks where core reaches *backwards* into
component internals via `hook_points[...]` symbol lookups.

This focus draws one clean boundary and makes the registry contract
uniform. It is structural — no behavior change, env-knob names preserved.

**The boundary: a component is a top-level participant in the core loop**
— it registers on a tick phase or a coarse loop-level surface. Anything
invoked only as an internal sub-step of a core operation (an LLM judge
inside a verify/validation runner) is NOT a component; it's a swappable
adapter the runner calls. So the work runs *both* directions: extract
genuine loop subsystems *in*, and demote mis-modeled leaves *out*.

Two things stay in core, unchanged: the **pipeline subsystem** (its tool
is a task-agent/core tool; it drives core board sections + post-agent
disposition) and the **deterministic baseline runners** — shell-bullet
verification, briefing-structure validation, and status-report
composition (verification/validation are gating; reporting is baseline).

Why now: cron just landed as the canary (axis 1), proving the tick-phase
+ two-layer job-handler shape; the core is otherwise stable post-codex.
This is the cheapest moment to finish the boundary before more features
compound the coupling — and a clean, minimal kernel with a uniform
registry is the prerequisite for any OSS distribution shape.

Axes (each has its failure mode):

(1) **Cron component — LANDED (the canary).** The cron scheduler now runs
as a `Phase.CRON_DISPATCH` tick-hook component (`ap2/components/cron/`);
the `if job.name == …` switch is replaced by job handlers contributed
through the registry and keyed *cron-locally* — the `janitor` handler is
the janitor component's; status-report/smoke/LLM-cron are core-registered.
This pinned the new tick-phase vocabulary (`CRON_DISPATCH`, reserved
`IDEATION`) and the two-layer pattern (scheduler ≠ jobs) the rest of the
focus reuses. Residual: its bespoke `cron_job_handlers()` registry method
folds into the generic accessor in axis 5.

(2) **Communication component (inbound + outbound) wrapping the channel
adapters.** Today the channel surface is split across a bespoke
`registry.channel_adapters()` (outbound) and a one-off
`hook_points["inbound_poll"]` (inbound), with `mattermost` as a top-level
component. Introduce a single `communication` component that owns both
directions as tick-phase work and holds its channel adapters (mattermost,
future slack/email) in an *internal* registry — invisible to core. There
may be multiple channels; that multiplicity is the component's concern,
not the kernel's. Outbound becomes event-driven (the component delivers
undelivered notification events on its tick pass; the synchronous
`_deliver` call goes away). `mattermost` demotes to a channel adapter
under it; `AP2_MM_CHANNELS` becomes channel-level config. Delete-test: if
core still walks `channel_adapters()` or `inbound_poll`, channel
multiplicity has leaked into the kernel.

(3) **Decouple auto-approve from `board_edit` into a loop pass; remove the
dead `POST_DISPATCH` phase.** Today the auto-approve strip is evaluated
*inside* `board_edit`'s `add_backlog` branch (approval policy embedded in a
mutation tool, mid-agent-run), and the tags policy (`should_auto_approve`)
squats in `ideation.py` and is reached from core — the cross-boundary knot
that blocks axis 4. Make `board_edit` policy-free (proposals always born
`@blocked:review`) and move the gate chain + tags policy into the
`auto_approve` component as a discrete `PRE_DISPATCH` loop pass that runs
after ideation and before dispatch, stripping `@blocked:review` from
Backlog tasks that clear the gates. While here, delete the `POST_DISPATCH`
phase — its only registrant is auto_approve's no-op placeholder, and
auto-approve's real pass runs at `PRE_DISPATCH`. Delete-test: if the strip
stays in `board_edit`, ideation can't be extracted without core→component
import violations; if `POST_DISPATCH` survives, a dead phase is still
walked every tick.

(4) **Ideation component.** Extract `ap2/ideation.py` (the `_maybe_ideate`
trigger gate, the roadmap-exhaustion halt, proposal records, scrub
coordination) behind the reserved `Phase.IDEATION` tick hook + the halt
hook; owns the `AP2_IDEATION_*` knob cluster, all `ideation_*` events, and
`AP2_IDEATION_DISABLED` as its `env_flag`. Sequenced after axis 3 unties
the auto-approve coupling (largest blast radius). Delete-test: if ideation
stays in core, the kernel still hard-depends on the proposal engine.

(5) **Judges are adapters, not components; one generic registry verb.**
Two moves sharing a thesis — stop modeling sub-step leaves as components,
and stop growing a bespoke registration method per extension kind:
  - **Both LLM judges become adapter layers, not components.**
    `verifier_judge` stays the `select_adapter("verifier_judge")` kind it
    already is (revert any extraction of it into a component).
    `validator_judge` demotes from a component to a
    `select_adapter("validator_judge")` layer the core briefing-validation
    runner calls — dissolving `ap2/components/validator_judge/`, deleting
    `registry.briefing_validators()` (it has no other contributor) and the
    component's `hook_points` symbol-exposure block. Both runners keep a
    config off-switch (shell-only / structural-only);
    `AP2_VALIDATOR_JUDGE_DISABLED` survives as a plain knob.
  - **One generic `contributions(point)` registry accessor subsuming
    `Phase`.** Collapse the bespoke `channel_adapters()` /
    `cron_job_handlers()` methods into a single typed-extension-point
    accessor (fan-out only; keying stays consumer-local — the registry
    never does keyed dispatch). Delete the core→component `hook_points[...]`
    symbol-pull blocks (auto_approve, attention, validator_judge) — those
    are wrong-direction imports, not extension points.
  Delete-test: if a judge is still a component, or the registry still
  carries a per-kind registration method or a symbol-pull block, the
  boundary/contract isn't clean.

Sequencing: cron (1) landed. (5) judge-demotion + registry-verb is largely
independent and can go early (it removes the most clutter). (2)
communication is independent. (3) decouples auto-approve and removes
`POST_DISPATCH`, unblocking (4); ideation (4) lands last (largest blast
radius).

The delete-test for any work in this focus: does it move a genuine loop
subsystem behind the registry, demote a non-loop leaf out of
component-hood, make the registry contract uniform (one accessor, no
symbol-pull), or wrap internal multiplicity inside its owning component?
Polishing internals while the boundary stays wrong is not paying focus
rent.

Progress signals:
- `daemon.py` carries no cron loop or ideation gate inline; each is a
  registry-walked `ap2/components/<name>/` subpackage.
- A single `communication` component owns inbound + outbound; core never
  references channels; `channel_adapters()` and `inbound_poll` are gone.
- `board_edit` carries no auto-approve policy; the `POST_DISPATCH` phase is
  removed.
- Neither LLM judge is a component; both are `select_adapter` layers over
  core runners; `registry.briefing_validators()` is gone.
- The registry exposes one generic `contributions(point)` accessor; no
  per-kind registration methods; no `hook_points[...]` symbol-pull blocks
  in core.
- The import-direction CI gate still passes; the full suite passes with
  every component disabled, and a task dispatches → verifies (shell) →
  reports in that minimal-kernel config.

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
  gate); each is env-togglable. The current focus refines this boundary —
  finishing the genuine loop subsystems (cron, ideation, communication) and
  demoting the LLM judges back out to adapters.

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
- **Real-time collaboration**: chat channels (when the communication
  component is enabled) are the human-loop surface, but the loop is async
  — operator nudges, daemon ticks, agent commits. Synchronous chat-driven
  editing (operator types, agent responds in real-time) is out of scope;
  the chat surface is for control, ack, approvals, and status — not
  pair-programming.
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
