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

## Current focus: codex support through an agent adaptor layer

Every agent run in ap2 is a `claude_agent_sdk.query()` call against the
bundled Claude Code binary, across distinct dispatch sites: `run_task`
(task agents), the shared `_run_control_agent` (ideation, status-report,
cron, mattermost-handler), `_judge_prose_bullet` (the verifier
prose-bullet judge), `_run_scrub` (ideation-scrub), and the
validator-judge / janitor-judge component calls. The SDK imports sit in
only two files (`daemon.py`, `tools.py`), but its concepts —
`ClaudeAgentOptions` (model / effort / max_turns / timeout),
`create_sdk_mcp_server` (ap2's custom tools), the `AssistantMessage` /
`ResultMessage` stream shape, usage / cost parsing, OAuth-only auth —
leak across ~10 modules. ap2 is structurally single-backend, which the
Constraints section pins explicitly.

This focus introduces an **agent adaptor layer**: a backend-agnostic
`AgentAdapter` interface every dispatch flows through —
`run(prompt, tools, options)` yields a normalized event stream and an
`AgentResult(usage, commit, ...)`. Today's Claude path becomes a
`ClaudeCodeAdapter` that wraps the current `sdk.query` behavior
bit-for-bit; a new `CodexAdapter` drives OpenAI's Codex agent through
OpenAI's official `openai-codex` Python SDK behind the same interface. Crucially, the backend is selected **per
agent kind**, not per daemon: an `[agent_backends]` config table (with
`AP2_AGENT_BACKEND_<KIND>` env overrides, every kind defaulting to
`claude`) lets an operator run, for example, `ideation=claude` while
`task=codex`. ap2's custom MCP tools register through the adapter so
either backend exposes the same toolset.

The focus migrates every agent kind through the adapter one at a time:
each dispatch site becomes a single TB that moves it behind the
interface, preserving its exact tool policy and behavior on Claude
before Codex is offered for it. By the time the focus is exhausted,
every agent kind is adapter-routed and independently backend-selectable.

This focus does NOT add a third backend, and does NOT do per-message or
in-task backend routing — selection is fixed per kind at dispatch time.
It does not change any agent's prompt, tool policy, or verification
semantics; it relocates dispatch behind an interface and adds Codex as a
selectable alternative.

Why now: the two preceding structural focuses (component refactor,
structured config) just made internal seams cheap to introduce, and the
SDK coupling is still concentrated enough to factor cleanly — every
future feature that assumes the Claude stream shape makes the adaptor
more expensive to retrofit. Per-kind selection also lets the operator
route by cost and capability (a cheap backend for scrub / judges, the
strongest for task or ideation). Landing the abstraction before the
downstream OSS-distribution focus means OSS ships with a
backend-pluggable core, which materially widens its audience.

Seven axes form this focus. Each has its own failure mode to detect:

(1) **AgentAdapter interface + ClaudeCodeAdapter** — define the ABC
(`run`, options normalization, MCP-tool registration, the result/usage
shape) and move today's `sdk.query` path behind it with zero behavior
change. Delete-test: if the Claude path isn't behind the interface, the
Codex adapter has no contract to conform to.

(2) **Options + result/usage normalization** — a backend-neutral options
struct (model, effort / reasoning, max_turns, timeout) and a normalized
`AgentResult` / usage record, so the cost guards, the `task_run_usage`
emission, and `ap2 status` read one shape regardless of backend.
Delete-test: if not normalized, every consumer of usage / result
branches per-backend.

(3) **MCP / tool exposure through the adapter** — ap2's custom tools
(report_result, cron_propose, pipeline_task_start, the prose judge)
register through the adapter so both backends see the same toolset.
Delete-test: if tools stay Claude-MCP-specific, a Codex agent can't
report results and the loop breaks.

(4) **CodexAdapter** — implement the interface against OpenAI's official `openai-codex` Python SDK (which bundles the codex CLI):
prompt assembly, tool wiring, streaming, result / commit extraction, and
timeout / turn bounding. Delete-test: an abstraction with one
implementation is no actual Codex support.

(5) **Per-agent-kind selection + auth gate** — the `[agent_backends]`
map plus `AP2_AGENT_BACKEND_<KIND>` overrides select the adapter for
each kind; the daemon-start credential check becomes backend-aware,
requiring creds for each backend the map references (OAuth for
claude-backed kinds, OpenAI credentials for codex-backed kinds).
Delete-test: if not shipped, switching a kind's backend needs a code
edit and codex hard-fails the OAuth-only gate.

(6) **Agent-kind migrations (one TB each)** — convert each dispatch site
to adapter-routed and per-kind-selectable, sequenced least-entangled
first: `ideation-scrub` (canary — smallest, one-shot, no MCP tools) then
the verifier prose-judge, then validator-judge + janitor-judge, then
`run_task`, then the shared `_run_control_agent` (which unlocks per-kind
selection for ideation, status-report, cron, and the
mattermost-handler). Each migration preserves the site's tool policy and
behavior on Claude. Delete-test: migrate none and the adapter is a shell
with no caller.

(7) **Parity tests + per-backend smokes** — an adapter-contract test
suite both adapters satisfy, plus a Codex real-SDK smoke that
round-trips a tool call (gated like the Claude smokes, run via the 6h
`real-sdk-smoke` cron). Delete-test: without it, Codex regressions are
invisible.

The axes are sequenced: (1) is the prerequisite for everything else. (2)
and (3) land against the interface from (1). (4) implements it for
Codex. (5) makes the choice operator-facing. (6) is the long tail — one
TB per dispatch site — and (7) lands incrementally alongside (6).

The delete-test for any proposed work in this focus: does it move a
dispatch site behind the adapter, or let a second backend actually drive
an agent kind? Polishing Claude-path internals without exercising the
abstraction, or adding adapter scaffolding with no migrated caller, is
not paying focus rent.

Progress signals:
- `claude_agent_sdk` is imported only inside `ClaudeCodeAdapter`, not
  across `daemon.py` / `tools.py` / `verify.py` / `ideation_scrub.py`.
- Every dispatch site (task, control, verifier-judge, ideation-scrub,
  validator-judge, janitor-judge) runs through the adapter.
- A mixed configuration (`ideation=claude`, `task=codex`) runs an agent
  of each kind end-to-end: dispatch then tool calls then report_result
  then verify.
- usage / cost / `ap2 status` read one normalized result shape across
  backends.
- The adapter-contract test suite passes for both the Claude and Codex
  adapters.

## Shipped focus: refactor features into opt-in components (shipped 2026-05-27)

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

## Shipped focus: structured config (env → TOML) (shipped 2026-05-29)

The just-shipped component refactor (TB-309 → TB-320) closes the
"every feature is a togglable component" structural axis but leaves
runtime configuration on its pre-refactor footing: ~52 `AP2_*` env
vars in a flat namespace, each documented across three places
(`config.py` defaults, `env_reload.HOT_RELOADABLE_KNOBS`,
`ap2/howto.md ## Configuration knobs`), discovery via grep, schema
validation via "is this knob in TB-305's docs-drift gate?" Adding
a new component now means adding 1-6 new env knobs and threading
each through three documentation surfaces. The flat namespace
isn't broken, but it's drifted past the threshold where it scales
gracefully — TB-320 alone added one new knob plus wired three more
into component manifests.

This focus migrates ap2's runtime configuration to a structured
TOML config file (`.cc-autopilot/config.toml`), with env-var
overrides preserved as a 12-factor escape hatch. Each component
declares its config schema in its `manifest.py` (matching the
`config_schema` pattern Claude Code's plugin manifest already
ships); the registry validates the merged config (file + env
overrides + defaults) at startup with a clear error message on
schema mismatch. Operators get a single discoverable surface
(`ap2 config list / get / set`); fresh-project onboarding becomes
"edit one file" instead of "discover 50+ knobs across howto.md".

Why now: the env-knob count just bumped past 50, the component
refactor gives a natural per-component grouping for the new
schema, and the downstream OSS-distribution focus benefits if it
starts with a clean operator-facing surface. The work is cheaper
now (per-component knob counts are fresh) than later (more knobs
accumulate, more documentation drifts, the migration's blast
radius grows). Independently surfaced by operator audit on
2026-05-28T20:00Z immediately after TB-320 wired the last set of
`env_flag=None` component manifests.

Six axes form this focus. Each has its own failure mode to detect:

(1) **TOML schema + parser** — Extend `ap2/config.py` (or add a
sibling `ap2/config_loader.py`) with the file's canonical schema
shape, a parser, and a startup-time schema validator. The schema
is sectioned: a `[core.*]` group for non-component tunables
(verifier, ideation, control-agent, cron) and `[components.<name>]`
sub-tables for each component-owned knob. Validation at
daemon-start fails fast with a clear error naming the bad key
path; the daemon does NOT auto-correct typos (operator-fix-first
shape). Deliver: parser, schema registry, validator, plus a
`Config.from_toml(path)` constructor that returns the same
`Config` dataclass the daemon already consumes. Delete-test: if
not shipped, every subsequent axis has nothing to read against.

(2) **Env-var override layer** — Mapping rule:
`AP2_<SECTION>_<KEY>` overrides `[<section>.<subsection>] key =
...`. e.g., `AP2_COMPONENTS_AUTO_APPROVE_ENABLED=1` overrides
`[components.auto_approve] enabled = true`. Same hot-reload
semantics as today — `env_reload.py`'s mtime trick still works,
extended to also watch `config.toml`. Existing flat `AP2_FOO`
names get a back-compat map in `ap2/config_compat.py` with a
one-shot `env_deprecated` event emission on first use per
process. Deliver: the override layer plus the back-compat map
for every migrated knob. Delete-test: if not shipped, OSS users
get a new file but can no longer override per-shell-session;
existing CI / sandbox setups break.

(3) **Per-component config schemas** — Each existing component
(auto_approve, auto_unfreeze, attention, focus_advance, janitor,
mattermost, validator_judge) gains a `config_schema` field on
its `Manifest` (a `@dataclass` or `TypedDict`) declaring its
tunable knobs with types, defaults, descriptions, and a
hot-reloadability flag. The registry validates the loaded config
against the union of all components' schemas. Existing internal
`os.environ.get("AP2_FOO")` calls in component bodies migrate
to reading from `cfg.<path>.<key>`. Delete-test: if not shipped,
the migration is half-done — operators have a new config file
but components still consult env directly.

(4) **CLI surface** — `ap2 config list` (enumerates every config
key with current value + source: file / env-override / default),
`ap2 config get <path>` (single-key lookup), `ap2 config set
<path> <value>` (writes to file via the operator queue for
audit; emits `config_updated` event), `ap2 config validate`
(dry-run schema check). Each verb mirrors the existing
operator-CLI shape (operator-queue-routed for the write paths,
fcntl-locked, audit events). Delete-test: if not shipped, the
new config surface is present but not operator-discoverable —
every operator has to read the toml file directly.

(5) **Migration of existing knobs** — Walk the ~52 known
`AP2_*` knobs (per TB-305's `_collect_env_knobs()`
source-of-truth), map each to a config-file home, and migrate
the in-source readers. Some knobs explicitly stay env-only —
true 12-factor secrets (Mattermost auth tokens, sandbox-user
identity, OAuth tokens) and deployment-environment knobs
(`AP2_DIR`, `AP2_REAL_SDK`). The migration list lives in a
comment block in `ap2/config_compat.py` so the cut-line is
auditable. One TB-N per logical cluster of knobs (auto_approve,
auto_unfreeze, attention, etc.). Delete-test: if not shipped,
the new file exists but holds nothing — operators still tune
via env.

(6) **Docs + drift-gate parity** — `ap2/howto.md`'s
`## Configuration knobs` section gets a tree-rendered table of
config paths (replacing the flat env-var list). `ap2/init.py`'s
`ENV_TEMPLATE` gets a `CONFIG_TEMPLATE` sibling for the
fresh-init TOML file. The TB-305 docs-drift gate
(`test_every_env_knob_documented`) gets a sibling
(`test_every_config_key_documented`) that walks the component
schemas and asserts each is mentioned in howto.md. The
`_TEMPLATE_EXEMPT_KNOBS` exemption set from TB-305 gets a
config analogue. Delete-test: if not shipped, the docs-drift
class TB-305 closed reopens against the new surface.

The six axes are mutually reinforcing. (1) is the prerequisite
for everything else. (2) and (3) are parallelizable once (1)
lands. (4) gates on (1) for the read paths. (5) is the long
tail — one TB-N per knob cluster. (6) lands incrementally
alongside (5).

The delete-test for any proposed work in this focus: does this
make the config surface strictly more discoverable / validatable
/ structured, OR migrate a previously-env-only knob into the
config schema without losing back-compat? Refactors that touch
config plumbing without exercising the schema validator or
moving a knob aren't paying focus rent.

Progress signals:
- `.cc-autopilot/config.toml` exists as the fresh-project
  default, written by `ap2 init`.
- `ap2 config list` enumerates every tunable knob with its
  source (file / env-override / default).
- Each component's `manifest.py` carries a `config_schema`
  field that the registry validates against the loaded config.
- ≥80% of source-side `os.environ.get("AP2_*")` calls migrated
  to `cfg.<path>.<key>` reads.
- A TB-305-style docs-drift gate exists for config keys.
- The set of true 12-factor env-only knobs (secrets, deployment
  identity) is documented in a single comment block in
  `ap2/config_compat.py` and is clearly minimal.

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
  through an `AgentAdapter` layer (the active "codex support" focus); the
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
