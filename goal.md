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
public source-available cut).

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
  attention, janitor, cron, ideation, communication)
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
  `AP2_JANITOR_DISABLED`, etc. still work unchanged from
  the operator's perspective).
- A future "distribution" focus can be defined entirely in terms
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

## Current focus: cut a public source-available distribution

ap2 is structurally ready to ship outside the sandbox: the component model
is finished (every loop-level behavior is registry-loaded and independently
togglable), the operator manual is consolidated into cross-runtime skills,
and the codex backend is live. What remains for a public release is not
architecture — per the component-model `## Done when`, a distribution is
definable "entirely in terms of which components default to enabled plus
packaging extras, no further structural refactor required." This focus does
exactly that, plus the minimal release-hygiene a public checkout needs
(license wiring, identity scrubbing, an accurate README).

The release is **source-available and noncommercial** (PolyForm
Noncommercial 1.0.0), NOT OSI open source — the license withholds
commercial use. Naming and metadata must reflect that: it is a
"source-available distribution," not an "open-source release," and no
`License :: OSI Approved` classifier is claimed.

The split this focus preserves:
- **The publish and public identity stay operator-only.** The `git push`
  to the public remote (+ any package-registry upload) and the real repo
  URL / author identity are operator-CLI / operator-hand actions — the same
  rule that keeps goal mutations and pushes off the daemon path (they are
  irreversible / set public direction). Daemon tasks prepare everything
  else — **including dropping in the PolyForm Noncommercial 1.0.0 `LICENSE`
  text and setting the matching `pyproject` `license` field + classifiers**
  (a published, standard license — verbatim text, no operator legal
  authorship needed) — but never perform the push or invent the real public
  URL (a placeholder is fine; the operator sets the real value).
- **Conservative-by-default posture.** A fresh `ap2 init` keeps the loop
  whole but every operator-bypassing *behavior* off/inert: `auto_approve`
  disabled (the one component that defaults off), `attention.immediate_push`
  off (attention surfaces in status/web, never pushes unsolicited),
  `communication` active but with no channel configured (no external posting
  until the operator sets `AP2_MM_CHANNELS` + credentials), `auto_unfreeze`
  active but with no `fix_shapes` (no automatic unfreezing). This is the
  Non-goals "unconditional automation" rule applied to the default install;
  it is already the schema default, so the work is to assert and document
  it, not to disable whole components. (Mattermost is a channel adapter
  wrapped by `communication`, not a standalone component; the retired
  focus-auto-advance pointer is gone post-TB-342.)
- **No behavior removal.** Packaging and defaults change; no feature is
  deleted. The internal all-enabled install keeps doing everything it does
  today, by every observable signal.

Axes (coarse — ideation decomposes):

(1) **License wiring + identity scrub.** Replace the proprietary "All
Rights Reserved" `LICENSE` with the verbatim PolyForm Noncommercial 1.0.0
text, and set the `pyproject` `license` field + classifiers to match
(source-available, noncommercial — no `License :: OSI Approved`). Remove the
one internal absolute-path leak (the `ap2/json_extract.py` comment
referencing a local `~/repos/post-train/...` path), sweep source for any
other sandbox-identity string baked in as a non-overridable default (vs. a
documented overridable default like `AP2_SANDBOX_USER`), and make the
packaging metadata coherent for an outside consumer — author, repo-URL
placeholder, classifiers, and sdist contents (the committed `skills/` +
docs must ship in the source distribution, not just the wheel). Update the
README's License section to PolyForm Noncommercial 1.0.0 and add a note that
the committed `.cc-autopilot/` tree is ap2's own self-management state
(resettable via `ap2 init`), so an outside reader isn't confused by the
shipped task history. Delete-test: if the repo still declares "All Rights
Reserved", or a fresh install from a clean checkout leaks the sandbox's
local paths/identity, or the sdist omits the skills/docs — the wiring didn't
happen.

(2) **Default-config posture + extras.** The conservative posture above is
already the schema default; this axis pins it as a release gate rather than
changing it. Assert that the default config (loop whole, bypassing behaviors
off/inert) AND the all-components-disabled config both keep the test suite
green — the existing component-model invariant, now promoted to a release
gate — and confirm a fresh `ap2 init` writes that conservative default.
Confirm the install extras (`[codex]`, `[dev]`) resolve cleanly for someone
outside this sandbox, and add a `[mattermost]` extra only if the
communication/Mattermost path actually pulls a dependency beyond the base
set (it may not — the base deps carry no Mattermost-specific package).
Delete-test: if a fresh `ap2 init` install acts unattended on the operator's
behalf out of the box, or an extra fails to resolve for an outside user, the
posture isn't right.

The delete-test for the focus: does the work make a clean outside checkout
installable, safe-by-default, and accurately documented under the
noncommercial source-available license — without removing behavior or
performing the operator-only publish? Polishing internal-only ergonomics is
anti-work here.

Progress signals:
- A clean checkout installs and runs the test suite green with no
  sandbox-specific paths or identity baked into source.
- The `LICENSE` is the verbatim PolyForm Noncommercial 1.0.0 text and
  `pyproject` declares it (no "All Rights Reserved", no OSI-open-source
  claim).
- A fresh `ap2 init` keeps the loop whole with every operator-bypassing
  behavior off/inert (`auto_approve` disabled, `attention.immediate_push`
  off, no channels configured, no `fix_shapes`); the default and
  all-disabled configs both pass the suite.
- The only remaining steps to go public are the operator-only ones: set the
  real repo URL/author, and push.

Why now: the component model, the skills consolidation, and the codex
backend all landed — the structural prerequisites goal.md named for a
distribution cut are met, so the remaining work is packaging + defaults +
hygiene, exactly the shape goal.md said a distribution focus reduces to.
Doing it now converts "structurally ready" into "actually shippable."

## Shipped focus

Completed focus arcs (newest first). Durable criteria live on in "## Done
when" (component model, structured config) and "## Constraints" (pluggable
backend, per-backend auth); the entries below are provenance.

- **Consolidate the operator manual into cross-runtime skills (2026-06-11)**
  — retired `ap2/howto.md` as a separate operation manual, carving it into
  ~6–9 auto-triggered domain `SKILL.md` skills under `skills/` (board-ops,
  config, task-authoring, observability, failure-recovery, ideation-goals,
  status); retargeted the docs-drift config-knob-coverage gate and
  `sandbox.sync-assets` off howto onto the skills; made the deploy
  cross-runtime (`~/.claude/skills` + `~/.agents/skills` + an `AGENTS.md`)
  with a managed discovery pointer. `architecture.md` remained the
  standalone design doc; daemon-agent-relied briefing conventions stayed
  canonical in `ideation.default.md`.
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
- **Goal.md auto-rotation / auto-rewrite**: the operator owns the focus
  list. ap2 never mutates `goal.md` — adding, reordering, or retiring
  foci is operator-only via `ap2 update-goal`. When ideation exhausts a
  focus it parks and surfaces a notice rather than advancing itself.
  (The former `focus_advance/` auto-rotation pointer was retired in
  TB-342; goal direction is now an operator-only edit.)
- **Removing behavior during component extraction**: the component
  refactor moves modules into `ap2/components/` and gates them by
  env flag; it does not delete features. The internal install with
  all components enabled continues to do everything that works today,
  by every observable signal.
- **API stability commitments before the distribution cut**: the core
  surface stays fluid until the component-extraction focus is exhausted
  and the downstream distribution focus ships. We will not promise
  backwards compatibility on `ap2/core/` module signatures to
  internal callers during this refactor.
- **Relicensing to OSI open source as part of this cut**: the public
  distribution ships source-available and noncommercial (PolyForm
  Noncommercial 1.0.0) by operator decision. Converting to a permissive
  OSI license (allowing commercial use) is a separate operator call, not
  in scope for the distribution focus.

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
  default**: ideation proposals, retry-exhausted tasks, and other
  per-cycle operator gates default to requiring operator action.
  Relaxations are opt-in via components and their env knobs
  (`AP2_AUTO_APPROVE`, `AP2_ATTENTION_IMMEDIATE_PUSH`, future siblings)
  with documented safety gates (tag-based opt-out, cumulative-regression
  pause, ideation-exhaustion halt). Goal mutations (`goal.md` content —
  including the focus list itself), git pushes, and cron schedule
  changes remain operator-CLI-only by design — they're either
  irreversible or set direction for everything downstream.
