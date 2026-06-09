# Docs: refresh architecture.md to the current component model (registry + ap2/components layout + tick phases + contributions(point) + communication + judges-as-adapters)

Tags: #autopilot #docs #architecture #components #registry

## Goal

`ap2/architecture.md` is broadly stale relative to the component refactor
(2026-05-27) and the boundary-refinement focus that just shipped (TB-386→389
+ TB-391). It still describes the pre-component world: the "## Module map"
lists `auto_approve.py` / `attention.py` / `janitor.py` etc. as flat top-level
modules (they moved to `ap2/components/<name>/`), the "## The daemon loop"
section shows the old 5-step tick with no registry tick phases, and there is
no description of the component registry, the single generic
`contributions(point)` accessor, the communication component, or the
loop-level component boundary. The doc has been spot-patched (a few TB-386
judge references) but never refreshed. Bring it current.

Doc-surface split (do not violate): `architecture.md` = technical design /
component model; `howto.md` = operation manual (CLI / knobs / events) and is
NOT to receive component-model design prose; `README.md` = quickstart. This
task edits `architecture.md` (and only moves design prose OUT of howto.md into
architecture.md if any is found there — it does not add new design prose to
howto.md). Meta-infra documentation, no focus anchor.

## Scope

- **Module map** — replace the flat-layout listing with the real tree: the
  components under `ap2/components/<name>/` (attention, auto_approve,
  auto_unfreeze, janitor, cron, communication, ideation) each as a
  manifest+impl subpackage walked via the registry, and the core modules that
  remain flat. Explicitly note the things that are NOT components: the LLM
  judges (verification prose-judge in `verify.py`, briefing dep-coherence in
  `briefing_validators.py`) are core sub-steps reached via
  `select_adapter(...)`, and the pipeline subsystem stays in core.
- **Daemon loop / tick** — replace the 5-step description with the current
  phase-walked tick: the registry tick `Phase` vocabulary
  (`PRE_DISPATCH`, `ATTENTION_EMISSION`, `CRON_DISPATCH`, `POST_CRON`,
  `IDEATION`), interleaved with the core steps (operator-queue drain,
  pipeline-pending sweep, dispatch). Note that `POST_DISPATCH` was removed
  (TB-388) and why.
- **New "## Component model" section** — the registry (`registry.py`):
  `Manifest`, `env_flag` polarity / enable-disable, the import-direction CI
  gate (core never imports `ap2/components/`), and the single generic
  `contributions(point)` accessor — fan-out only, keying stays consumer-local
  (the registry never does keyed dispatch). State the loop-level boundary
  principle: a component is a top-level loop participant (a tick phase or
  coarse loop surface); sub-step leaves (the judges) are adapters, not
  components; internal multiplicity (cron jobs, comm channels) lives inside its
  owning component, not on the core surface.
- **Cron** — document the two layers: the scheduler component
  (`Phase.CRON_DISPATCH`) and the cross-component job-handler surface
  (contributed by janitor + core, resolved cron-locally) — replacing the old
  `job.name` switch.
- **Communication** — document the communication component owning inbound +
  outbound, wrapping its channel adapters (mattermost; future slack/email)
  internally; note that `channel_adapters()` / `inbound_poll` are gone from the
  core surface (TB-389).
- **Judges-as-adapters** — make explicit (extending the existing TB-386 spot
  references) that neither `validator_judge` nor `verifier_judge` is a
  component: both are optional LLM layers a deterministic core runner
  (briefing-validation / task-verification) calls via `select_adapter`, with a
  config off-switch (structural-only / shell-only).
- Do NOT (re)write the agent-backend layer here — TB-393 (predecessor) owns the
  "## Agent backends" section; this task reads it as already-present and does
  not duplicate or conflict with it.
- Documentation only — no code changes.

## Design

- Source of truth: `ap2/registry.py` (Manifest, `Phase`, `contributions`),
  `ap2/components/<name>/` (the real subpackage layout), `ap2/daemon.py`
  `_tick` (the phase walks), `ap2/verify.py` + `ap2/briefing_validators.py`
  (the judges as `select_adapter` sub-steps). Describe what the code IS; do not
  invent structure.
- Sequenced after TB-393 (backend section) and TB-391 (ideation component) so
  the doc reflects the final state — ideation is a component, the backend
  section already exists.

## Verification

- `grep -qE 'ap2/components/' ap2/architecture.md` — the module map reflects the components subpackage layout.
- `grep -qE 'CRON_DISPATCH|PRE_DISPATCH|tick phase' ap2/architecture.md` — the tick is documented in terms of registry phases.
- `grep -qE 'contributions\(' ap2/architecture.md` — the generic registry accessor is documented.
- `grep -qiE 'communication' ap2/architecture.md` — the communication component is documented.
- `ap2/howto.md` Prose: confirm this task added NO new component-model design prose to howto.md — howto stays an operation manual (CLI / knobs / events); any stray design prose found there was moved into architecture.md, not added. Judge confirms via Read.
- `ap2/architecture.md` Prose: the module map lists `ap2/components/<name>/` subpackages (not the flat layout), the daemon loop is described via registry tick phases (with `POST_DISPATCH` noted as removed), a "## Component model" section documents the registry + `contributions(point)` fan-out accessor + the loop-level boundary, and the LLM judges are described as `select_adapter` sub-steps (not components). Judge confirms via Read.

## Out of scope

- The "## Agent backends" / codex backend section (TB-393, predecessor).
- Any code change; documentation only.
- `howto.md` operational content beyond removing any stray component-model design prose into architecture.md.
