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
- A docs-drift gate enforces that every config schema key is
  documented in the operator-facing reference (the config/knobs
  operator skill — formerly `ap2/howto.md`'s `## Configuration
  knobs` section, retired into skills).

## Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills

ap2's operator-facing knowledge is split across two surfaces with mismatched
ergonomics: a ~184 KB `ap2/howto.md` (the operation manual — CLI verbs, env
knobs, event schema, fix-shapes) that an agent reads only when pointed at it
via a hand-maintained `~/.claude/CLAUDE.md` pointer, and a handful of
auto-discovered skills (`/ap2`, `/ap2-task`). Now that the agentskills.io
`SKILL.md` standard — `name`/`description` frontmatter, progressive
disclosure, **description-based implicit (auto) invocation** — is supported by
**both Claude Code and Codex**, the monolithic howto is the weaker form: a
skill surfaces the right slice on a task match and is portable across runtimes;
howto cannot.

This focus retires `ap2/howto.md` as a separate surface, consolidates the
operator manual into a set of **domain skills**, retargets the docs guards and
the deploy onto those skills, and makes the deploy **cross-runtime** (Claude +
Codex). It is a docs/tooling restructure — no daemon behavior change.

The split it preserves:
- **`architecture.md` stays the standalone design doc** — contributor-facing
  "why it's shaped this way," read on a deep-dive, NOT operational. It is not
  merged into skills (that would put design prose into the operator's
  always-loaded skill budget).
- **Skills are operator-session tooling only.** The daemon's task/ideation
  agents run with `setting_sources=["project"]` + inlined prompts and do not
  read skills or howto. So nothing a *daemon* agent relies on moves into a
  skill — briefing-authoring conventions the **ideation** agent follows stay
  canonical in `ideation.default.md` (an operator authoring skill may mirror
  them, but the daemon's copy is the source of truth).
- **Group by operator task/domain, not per CLI subcommand.** Skill summaries
  are always loaded up front (~8 KB each under progressive disclosure), so
  over-fragmentation bloats the always-on budget and blurs trigger boundaries.
  Aim for ~6–9 coherent domain skills with tight descriptions.

Axes (coarse — ideation decomposes):

(1) **Carve `howto.md` into domain skills.** Break the operation manual into
~6–9 task/domain `SKILL.md` skills (e.g. monitoring/status, task + briefing /
verification-bullet authoring, board ops, ideation + goal/focus management,
event-schema / observability / diagnostics, config knobs + backend (codex)
setup, failure-recovery / operator-playbook), each with a tight auto-trigger
`description` and its reference material in the body. Retire `ap2/howto.md` as
a separate file. Delete-test: if `howto.md` survives as the canonical
operation manual, the consolidation didn't happen.

(2) **Retarget the docs guards + deploy.** Repoint the `test_docs_drift.py`
config-knob-coverage gate from `ap2/howto.md` to the skills; drop the
`ap2-howto.md` target from `sandbox.sync-assets`; fix the skills'
cross-references to resolve at their deployed paths (not repo-relative
`ap2/howto.md`). Delete-test: if the drift gate still asserts coverage in
`howto.md`, or `sync-assets` still deploys it, the surface wasn't retired.

(3) **Cross-runtime deploy + managed pointer.** `sync-assets` gains a
Codex/standard target (`~/.agents/skills`) alongside `~/.claude/skills`, so the
same skills serve a Codex operator session; add the `AGENTS.md` analog of the
operator `CLAUDE.md`, and have the deploy *manage* the discovery pointer
(closing the current gap where the file is deployed but the `CLAUDE.md` pointer
is hand-maintained). Delete-test: if deploying the skills still leaves an
operator runtime unable to discover them without a manual edit, the deploy
story isn't done.

The delete-test for the focus: does the work move operator-operation knowledge
into an auto-triggered domain skill (retiring the howto surface), retarget a
guard/deploy from howto to skills, or make the skills cross-runtime? Polishing
`ap2/howto.md` in place is anti-work — it's being retired.

Progress signals:
- `ap2/howto.md` no longer exists as a separate operation manual; its content
  lives in ~6–9 domain `SKILL.md` skills under `skills/`.
- `architecture.md` remains the standalone design doc.
- The docs-drift gate enforces config-knob coverage in the skills, not howto.
- `sync-assets` deploys the skills to both `~/.claude/skills` and
  `~/.agents/skills` (+ an `AGENTS.md`) and manages the discovery pointer;
  nothing references `ap2/howto.md`.
- Daemon-agent-relied conventions (ideation briefing authoring) remain in the
  daemon prompts, not skills.

Why now: the agentskills.io standard just converged across Claude + Codex
(implicit invocation + progressive disclosure), and the codex backend just
shipped — so consolidating into portable skills both fixes the howto-discovery
gaps and delivers a clean, cross-runtime operator-onboarding surface, a
prerequisite for the OSS cut.

## Shipped focus

Completed focus arcs (newest first). Durable criteria live on in "## Done
when" (component model, structured config) and "## Constraints" (pluggable
backend, per-backend auth); the entries below are provenance.

- **Component boundary = loop-level participants only (2026-06-09)** —
  finished the component model: extracted cron + ideation into
  `ap2/components/` behind registry tick phases, introduced a single generic
  `contributions(point)` accessor (retiring the bespoke `channel_adapters()` /
  `cron_job_handlers()` / `briefing_validators()` methods and the
  `hook_points[...]` symbol-pull blocks), folded the channel surface into a
  `communication` component, decoupled auto-approve from `board_edit` (removing
  the dead `POST_DISPATCH` phase), and demoted both LLM judges
  (verifier / validator) out of component-hood into `select_adapter` layers.
  Minimal-kernel e2e green with every component disabled.
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
  gate); each is env-togglable. The 2026-06-09 boundary focus refined this —
  finishing the loop subsystems (cron, ideation, communication) and demoting
  the LLM judges back out to adapters.

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
