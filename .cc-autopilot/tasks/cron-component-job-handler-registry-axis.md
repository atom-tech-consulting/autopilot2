## Goal

Advance `Current focus: extract the remaining core subsystems into components`
by relocating the cron scheduler into `ap2/components/cron/` and replacing
`run_cron`'s hardcoded `if job.name ==` switch (daemon.py L1376-1440:
status-report L1387, janitor L1405, real-sdk-smoke L1440) with a registered
job-handler protocol. goal.md axis (2): the scheduler (the `cron.yaml` /
`cron_state.json` interval engine, the `cron_*` lifecycle events, the
`cron_propose` / `cron_edit` surface) moves into the component; components and
core contribute named handlers and the scheduler dispatches by name knowing
nothing of what a job does. The shared `_run_control_agent` primitive stays in
core (the generic LLM-cron handler calls back into it); the status-report job
stays a core-registered handler (its composition is baseline core). This builds
on the new tick-phase vocabulary added by TB-381 (axis 1) — hence the
`@blocked:TB-381` dependency. Purely structural — schedule semantics, `cron_*`
events, and `cron_propose`/`cron_edit` preserved bit-for-bit.

Why now: goal.md's axis-(2) delete-test says "if the `job.name` switch survives,
the coupling just moves into a new folder" — replacing it with a job-handler
registry is what actually decouples the scheduler from job semantics, and the
new tick phases from TB-381 are the seam it dispatches against.

## Scope

- Create `ap2/components/cron/` (`impl.py` holding the relocated interval-engine
  + `run_cron` body, `manifest.py` exposing a `MANIFEST`, thin `__init__.py`)
  following the `impl.py` / `manifest.py` / `__init__.py` subpackage shape the
  existing components under `ap2/components/` already use.
- Define a job-handler registry: handlers register a `name -> async callable`;
  `run_cron` resolves the handler for a `CronJob` by name and dispatches to it
  instead of the `if/elif` switch. The status-report handler is registered by
  core; the janitor handler keeps its existing `registry.hook("tick_hook",
  component="janitor")` path; real-sdk-smoke registers its handler.
- Route the cron stage through the cron-dispatch tick phase added in TB-381 so
  `daemon._tick` walks the registry rather than calling `run_cron` via a
  hardcoded core path.
- Preserve `cron.yaml` / `cron_state.json` semantics, all `cron_*` events, and
  the `cron_propose` / `cron_edit` surface verbatim; keep `_run_control_agent`
  in core.

## Design

Two separable moves bundled because they share the `run_cron` body. (a)
Relocation: the interval engine + lifecycle-event emission move into the
component subpackage, discovered filesystem-side by the registry. (b)
Handler-protocol: replace the `if job.name == "..."` chain with a
`dict[str, JobHandler]` the scheduler consults by name — core registers
`status-report` and `real-sdk-smoke` handlers, the janitor component already
exposes its handler via `hook_points["tick_hook"]`, and the generic LLM-cron
handler calls back into the core `_run_control_agent` primitive (which stays in
core so the component never owns agent dispatch). The scheduler becomes
job-agnostic: adding a job means registering a handler, not editing a switch.
Dispatch flows through the cron-dispatch `Phase` member from TB-381 so `_tick`
walks the registry uniformly. `cron_propose` / `cron_edit` keep their current MCP
+ CLI surfaces and `cron_*` event shapes so the operator-facing contract and the
events allowlist are untouched.

## Verification

- `uv run pytest -q` — full suite passes.
- `test -f ap2/components/cron/manifest.py` — the cron component subpackage
  exists with a registry manifest.
- `! grep -nE 'if job\.name ==' ap2/daemon.py` — the hardcoded job-name switch is
  gone from core.
- `! grep -rnE 'if job\.name ==' ap2/components/cron/` — the relocated scheduler
  does NOT reintroduce the hardcoded switch; it dispatches via the job-handler
  registry.
- `ap2/daemon.py` Prose: `run_cron` (or its relocated equivalent) resolves a
  `CronJob`'s handler by name from a registered job-handler protocol
  (status-report registered by core, janitor via its component) rather than an
  `if/elif` chain; judge confirms via Read.
- Prose: a new regression test pins that registering a named job handler causes
  the scheduler to dispatch to it, and that `cron_propose` / `cron_edit` + the
  `cron_*` events are unchanged; judge confirms via Read.

## Out of scope

- Extracting pipeline (axis 1), ideation (axis 4), the prose-judge (axis 5), or
  decoupling auto-approve (axis 3) — separate tasks.
- Any change to cron schedule semantics, the `cron_propose` / `cron_edit`
  surface, or moving `_run_control_agent` out of core.
- Adopting any pending `cron_proposed` event onto a schedule — schedule mutation
  is operator-CLI-only via `ap2 cron edit`.