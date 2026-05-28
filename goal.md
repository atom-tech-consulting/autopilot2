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

## Current focus: refactor features into opt-in components

The walk-away-automation and operator-legible-reporting foci shipped
(roadmap_complete halt 2026-05-27 — auto-approve, auto-unfreeze,
multi-focus rotation, cost guards, attention detectors, smart-cadence
+ context-rich digests are all live and stable). The limiting factor
on mission progress shifts to a structural axis: every autonomous
behavior in the codebase — auto-approve, auto-unfreeze, attention
detectors, multi-focus + focus auto-advance, janitor, validator-judge
LLM dep-coherence, Mattermost integration — is wired directly into
`daemon._tick` (or via direct imports from other core modules) and
cross-references across the tree. There is no notion of "this feature
is optional"; every behavior runs on every tick, gated only by
per-feature env knobs the operator has to discover and tune
individually. Adding a new autonomous behavior means touching daemon
control-flow; turning one off requires reading source to find which
env flag suppresses it.

This focus refactors the codebase so every autonomous behavior lives
in `ap2/components/<name>/` as a self-contained module that registers
its hooks (tick, validator, channel adapter, CLI verb extension) via
a shared registry. Core (daemon, dispatch, verify, briefing
validators, status-report digest composition, audit trail, CLI
scaffold) never imports any component directly; all wiring flows
through the registry. The work is purely structural — no behavior
changes for the operator, no env-knob renaming. By the time this
focus is exhausted, every existing feature should be a component, and
turning any of them on or off should be a one-line env change with
no code touched.

This focus does NOT decide what a public-facing OSS distribution
includes. That's a separate downstream focus: once components are
cleanly togglable, choosing which ones default to enabled in OSS
is a one-line policy decision plus README work, not a code refactor.

Why now: the recent focus arcs left the codebase rich in autonomous
behaviors but flat in structure — `daemon._tick` carries direct calls
into eight modules, and every new behavior compounds the coupling.
Without a registry contract, the next focus (whatever it is) builds
on a foundation that already resists composition. The refactor is
cheap now (well-tested behaviors, clear cleavage candidates) and
gets more expensive every cycle.

Six axes form this focus. Each has its own failure mode to detect:

(1) **Component manifest + registry shape** — define the file-tree
layout and the manifest contract. Each component lives in
`ap2/components/<name>/` with at minimum a `manifest.py` declaring:
the env-flag that enables it, the hook points it registers
(`tick_hook`, `validator_hook`, `channel_adapter`,
`status_report_section`, `cli_verb`, etc.), its dependencies on
other components, and its default-enabled state. The registry
(`ap2/registry.py` or similar core module) discovers components at
daemon startup, reads manifests, and exposes a typed interface the
daemon walks. Deliver: the registry module, the manifest schema,
and one converted component (the canary — pick the least entangled,
likely `janitor/`). Delete-test: if the shape isn't pinned in one
converted component, every subsequent migration re-invents the
protocol.

(2) **Daemon tick hook protocol** — `daemon._tick` today calls into
`auto_approve.maybe_apply()`, `auto_unfreeze.sweep()`,
`attention._maybe_emit_attention_events()`,
`focus_advance.advance_if_exhausted()`, `janitor.run_janitor()`,
and several others, all by direct import. Deliver: the daemon walks
`registry.tick_hooks` instead, calling each registered hook in a
canonical phase order (pre-dispatch, post-dispatch, post-cron,
attention-emission, etc.). Hooks are typed callables with a
`(cfg, events_file)` signature. Components that need to fire on
specific events (e.g. attention push on `attention_raised`) register
event-driven hooks via the same registry. Delete-test: if not
shipped, every component migration still requires editing
`daemon._tick` — defeats the cleavage.

(3) **Channel adapter abstraction** — reporting outputs (status-report
digest delivery, attention push, future channels) are routed through
a `ChannelAdapter` ABC with `post(message, **metadata) -> Result`.
The status-report digest *composition* stays in core
(`status_report.py` builds the post body from board state, events,
focus rotation activity, etc. — operator-legible reporting is a
baseline value, not optional). The status-report *delivery channel*
becomes pluggable: the Mattermost channel becomes a
`MattermostChannelAdapter` registered by the `mattermost/` component
when its env flag is set; sibling adapters (`StdoutChannelAdapter`,
`FileAppendChannelAdapter`, `WebhookChannelAdapter`) ship in core so
the digest has a non-null default destination. Operator configures
which adapter(s) receive the digest via env (`AP2_CHANNELS=...` or
similar — exact knob name TBD; preserve back-compat with
`AP2_MM_CHANNELS`). Delete-test: if not shipped, "report to anything
other than Mattermost" requires editing `_mm_post` call sites.

(4) **Validator pipeline as a list** — `_validate_briefing_structure`
in `ap2/briefing_validators.py` currently calls TB-154 / TB-161 /
TB-164 / TB-171 / TB-235 / TB-308 checks inline. Deliver: the
pipeline becomes a list of `BriefingValidator` callables; the
deterministic structural checks (TB-154 sections, TB-161 goal-anchor,
TB-164 why-now, TB-171 no-manual, TB-308 no-fenced-paths-in-scope)
live in core and always run; the TB-235 LLM dep-coherence judge is
registered by the `validator_judge/` component when its env flag is
on. Components can register additional validators via the registry.
Delete-test: if not shipped, the LLM dep-coherence check stays
hardcoded in core's validator path and pays an SDK call on every
queue-append regardless of whether the operator wants it.

(5) **Component migrations** — convert existing modules to the
component shape, one at a time, preserving env-knob names exactly.
Target migration order (sequenced to limit blast radius):

  - `janitor/` (canary; isolated — most self-contained autonomous
    behavior in the tree)
  - `validator_judge/` (only the briefing-validator pipeline touches
    it; lands with axis 4)
  - `mattermost/` (channel adapter; lands with axis 3 — Mattermost
    HTTP client, channel/team/bot env knobs, and the `mattermost_reply`
    MCP tool all move together)
  - `attention/` (detectors + immediate-push hooks; publishes via the
    channel-adapter abstraction landed in axis 3)
  - `focus_advance/` (multi-focus + focus auto-advance — reads
    `goal.md` headings, runs the empty-cycles counter, advances the
    in-memory focus pointer, emits `focus_advanced` / `roadmap_complete`
    events. Reads goal.md but never mutates it; the autonomous
    advancement is what makes it a component.)
  - `auto_unfreeze/` (registers tick hook; depends on operator-queue
    which stays in core)
  - `auto_approve/` (largest blast radius — touches ideation,
    proposal labeling, retry semantics, cost guards; migrate last)

Each migration is one TB-N. Delete-test: if not shipped, the
registry stays a shell with one component in it — value isn't
delivered.

(6) **Toggle-correctness tests + CI gate** — add
`tests/test_components_disabled.py` (or distribute per-component)
that runs the full test suite in the "every component disabled"
configuration and confirms core behavior (dispatch, verify, briefing
validation, operator queue, basic ideation, status-report digest
composition + channel-adapter routing) still passes. Add the
import-direction CI gate: `test_core_does_not_import_from_components`
walks every `.py` outside `ap2/components/` and asserts no `from
ap2.components.` or `import ap2.components.` statements. Delete-test:
if not shipped, the cleavage erodes silently — a refactor accidentally
re-couples core to a component and nobody notices until a downstream
distribution attempt.

These six axes are sequenced: (1) is the prerequisite for everything
else. (2) and (3) are independent of each other and unblock the
component migrations in (5). (4) gates on (5)'s `validator_judge`
migration. (6) lands incrementally — the disabled-config test gets
re-run after each migration; the import-direction gate lands once
the first component is in `ap2/components/`.

The delete-test for any proposed work in this focus: does this make
the registry strictly more capable, OR move a previously-hardcoded
behavior into a component without changing its observable behavior?
Refactors that touch components without exercising the registry
shape, or that "improve" component internals while leaving the core
coupling intact, aren't paying focus rent.

Progress signals:
- `ap2/components/<name>/` subpackages exist for every formerly-
  hardcoded autonomous behavior.
- `daemon._tick` and other core modules carry zero direct imports
  from `ap2/components/`.
- The registry's tick-hook list is the canonical source of "what
  runs each tick"; `ap2 status` could in principle enumerate
  active components from it.
- The full test suite passes in the all-components-disabled
  configuration.
- The import-direction CI gate exists and passes.
- The status-report digest is composed by core and routed through
  whichever channel adapter(s) the operator configured — no
  hardcoded Mattermost dependency in the composition path.

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
  operator action. Relaxations are opt-in via components and their
  env knobs (`AP2_AUTO_APPROVE`, `AP2_FOCUS_AUTO_ADVANCE_DISABLED`,
  `AP2_ATTENTION_IMMEDIATE_PUSH`, future siblings) with documented
  safety gates (tag-based opt-out, cumulative-regression pause,
  all-foci-exhaust halt). Goal mutations (`goal.md` content —
  including the focus list itself), git pushes, and cron schedule
  changes remain operator-CLI-only by design — they're either
  irreversible or set direction for everything downstream.
