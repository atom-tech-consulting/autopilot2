# Cron component (scheduler + job-handler registry) + extended tick-phase vocabulary — the first tick-stage extraction (canary)

Tags: #autopilot #components #refactor #cron #job-handler #registry #tick-phase #axis-1 #canary

## Goal

This is **axis 1** of *Current focus: extract the remaining core subsystems
into components* — the first tick-stage extraction, and the one that
establishes the registry tick-phase vocabulary the later extractions reuse.
(Pipeline, the original canary candidate, was kept in core — its
`pipeline_task_start` tool is offered to the task agent and it drives core
board sections / post-agent disposition, so it isn't cleanly separable.)

Today the cron dispatch loop runs inline in `daemon._tick` (step 1):
`load_jobs` → for each due job → `run_cron`, where `run_cron` is a hardcoded
`if job.name == "status-report" / "janitor" / "real-sdk-smoke" / else` switch.
That switch is core-coupling by another name — exactly what the component
model should eliminate (the janitor branch already half-resolves its handler
via the registry).

Extract the cron *scheduler* into a component behind a registry tick hook, and
replace the `job.name` switch with a registered job-handler protocol.

Why now: cron is the most self-contained remaining tick subsystem, so it's the
right canary to pin the tick-stage extraction shape (new `Phase` members,
tick-hook wiring, the import-direction boundary) that the ideation extraction
(axis 3) then reuses.

## Scope

- **Extend the registry `Phase` vocabulary** (`ap2/registry.py`) with the
  phases the remaining tick stages need — at minimum a cron-dispatch phase
  (used here) and an ideation phase (reserved for axis 3) — and wire
  `daemon._tick` to walk `registry.tick_hooks(<phase>)` for them.
- **Relocate the cron scheduler** into `ap2/components/cron/` (manifest +
  impl): the `cron.yaml` / `cron_state.json` interval engine
  (`load_jobs`/`load_state`/`mark_run`), the due-check loop, the `cron_*`
  lifecycle events, and the `cron_propose` / `cron_edit` surface. Register it
  as a cron-dispatch tick hook.
- **Replace `run_cron`'s `if job.name == …` switch with a job-handler
  registry**: components and core contribute named handlers; the scheduler
  looks up the handler for a due job and dispatches to it, knowing nothing of
  what the job does. The `janitor` handler is the janitor component's; the
  `real-sdk-smoke` handler runs the smoke routine; the `status-report` handler
  stays a **core-registered** handler (its composition is baseline core); the
  generic LLM-cron handler calls back into the core `_run_control_agent`
  primitive (which stays in core, shared with ideation/mattermost).
- **Preserve behavior + env-knob names exactly** (same contract as the
  2026-05-27 component refactor) — purely structural; cron jobs fire on the
  same schedule and produce the same events.
- **Import-direction**: core must not statically import `ap2/components/cron/`;
  the daemon resolves the cron tick hook + job handlers via the registry. The
  CI import-direction gate must still pass.
- **Tests**: the cron component is registered + discoverable; a due job is
  dispatched to its registered handler (not a `job.name` switch); the
  all-components-disabled config still boots and runs a task (cron simply
  doesn't fire).

## Design

- **Scheduler vs handlers.** The component owns *when* jobs run (timing,
  due-detection, lifecycle events); *what* each job does is a registered
  handler contributed by whoever owns the work (janitor component,
  core status-report handler, smoke routine, generic LLM-cron → core
  `_run_control_agent`). This is the direct analog of, and replacement for,
  the `job.name` switch.
- **Canary role.** Being first, this task pays the cost of defining the
  tick-phase + tick-hook + import-direction shape once, so axis 3 (ideation)
  is a mechanical reuse.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including new cron-component + job-handler-registry tests and the all-disabled-config boot test.
- `test -f ap2/components/cron/manifest.py` — the cron component subpackage exists.
- `! grep -qE "if job\.name ==|job\.name == \"" ap2/daemon.py` — `run_cron`'s hardcoded job-name switch is gone (replaced by registry dispatch).
- `! grep -rqE "from ap2.components.cron|import ap2.components.cron" ap2/daemon.py ap2/cli*.py ap2/tools.py` — core does not statically import the cron component (import-direction gate).
- `ap2/registry.py` Prose: the `Phase` enum gains the cron-dispatch (and reserved ideation) tick phases, and `daemon._tick` walks `registry.tick_hooks(<phase>)` for them. Judge confirms via Read.
- `ap2/components/cron/` + `ap2/daemon.py` Prose: the cron scheduler (interval engine + `cron_*` events + `cron_propose`/`cron_edit`) runs as a registry tick-hook component; `run_cron`'s `job.name` switch is replaced by a registered job-handler protocol; `_run_control_agent` and the status-report handler stay in core; behavior and env-knob names are unchanged. Judge confirms via Read.

## Out of scope

- The **pipeline** subsystem — stays embedded in core by design (not separable; its tool is a task-agent/core tool).
- The **ideation** extraction (axis 3) — this task only *adds* the ideation tick phase to the registry; it does not move `ideation.py`.
- The auto-approve decouple (axis 2) and the prose-judge split (axis 4).
- Changing cron behavior, job schedules, or the `_run_control_agent` primitive.
