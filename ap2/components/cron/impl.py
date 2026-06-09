"""Cron scheduler component — the first tick-stage extraction (TB-381 axis 1).

Pre-TB-381 the cron dispatch loop ran inline in `daemon._tick` (step 1):

    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    for job in due_jobs(jobs, state, cfg.project_root):
        await run_cron(cfg, sdk, mcp_server, job)

…where `run_cron` was a hardcoded `if job.name == "status-report" /
"janitor" / "real-sdk-smoke" / else` switch. That switch is core-coupling
by another name — exactly what the component model eliminates.

This component relocates the cron **scheduler** behind a registry tick
hook (`Phase.CRON_DISPATCH`): the due-check loop, the `cron_*` lifecycle
events, and the per-job dispatch. The scheduler owns *when* jobs run;
*what* each job does is a registered handler (the job-handler registry),
contributed by whoever owns the work:

  - ``status-report`` → core (`cron_handlers.status_report_handler`).
  - ``real-sdk-smoke`` → core smoke routine (`cron_handlers.smoke_handler`).
  - ``janitor``        → the janitor component
                         (`hook_points["cron_job_handlers"]`).
  - any other job      → core generic LLM-cron handler
                         (`cron_handlers.generic_llm_handler` →
                         `_run_control_agent`).

The scheduler "knows nothing of what the job does": it resolves
`job.name` to a handler and dispatches. This is the direct analog of, and
replacement for, the `job.name` switch.

Interval engine vs scheduler
----------------------------
The reusable interval-engine *primitives* (`CronJob`, `parse_interval`,
`load_jobs` / `load_state` / `save_state` / `mark_run` / `due_jobs`,
`evaluate_condition`, `bootstrap`, `update_job`) stay in the core
`ap2/cron.py` library, because core consumers — `status_report`,
`smoke_runner`, `tools` (the `cron_propose` / `cron_edit` MCP write-path),
`cli_*` — depend on them and the TB-311 import-direction gate forbids
core from importing `ap2/components/`. This component is the *scheduler*
that drives that engine on the daemon's tick: it reads `cron.yaml` /
`cron_state.json` through the core library and runs due jobs. A component
can import core freely (component → core); only core → component is
gated.

cron_propose / cron_edit surface
--------------------------------
The agent/operator write-path into `cron.yaml` — the `cron_propose` MCP
tool (task agents emit `cron_proposed` events; operator promotes via
review) and the operator-only `cron_edit` mutation — lives in
`ap2/tools.py` because that module composes the MCP server, which must be
in core. Those tools mutate the same `cron.yaml` this scheduler reads, so
the propose → review → schedule flow is unchanged; the scheduler simply
picks up the operator-promoted job on its next due-check pass.

Import-direction: core resolves this scheduler via
`default_registry().tick_hooks(Phase.CRON_DISPATCH)` — it never statically
imports `ap2/components/cron/`. The CI import-direction gate
(`test_core_import_direction.py`) stays green.
"""
from __future__ import annotations

from typing import Callable

from ap2 import cron_handlers, events
from ap2.config import Config
from ap2.cron import CronJob, due_jobs, load_jobs, load_state
from ap2.registry import default_registry


# Module-level component name so the self-gate + handler resolution read
# from one source.
COMPONENT_NAME = "cron"


def resolve_cron_handler(name: str) -> cron_handlers.JobHandler:
    """Resolve a cron job name to its registered handler (TB-381).

    Overlays the component-contributed handlers
    (`registry.contributions("cron_job_handlers")` — today only the
    janitor component's `{"janitor": …}`) on top of the core-registered
    handlers (`cron_handlers.CORE_CRON_HANDLERS`). An unrecognized job
    name falls through to `cron_handlers.DEFAULT_CRON_HANDLER` (the
    generic LLM-cron path). This is the data-driven replacement for
    `run_cron`'s pre-TB-381 `if job.name == …` switch — the scheduler
    knows nothing of what each job does.

    The registry's `contributions(point)` accessor is fan-out only — it
    merges every manifest's `hook_points["cron_job_handlers"]` dict and
    returns the aggregate; the keyed dispatch (`handlers.get(name, …)`)
    stays here, local to the scheduler.
    """
    handlers: dict[str, Callable] = dict(cron_handlers.CORE_CRON_HANDLERS)
    handlers.update(default_registry().contributions("cron_job_handlers"))
    return handlers.get(name, cron_handlers.DEFAULT_CRON_HANDLER)


async def run_cron(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    """Dispatch a single due cron `job` to its registered handler (TB-381).

    The behavior-preserving replacement for `daemon.run_cron`: resolve the
    handler for `job.name` and await it. Each handler is self-contained
    (owns its own `cron_*` lifecycle events + `mark_run`), so this
    dispatcher carries no per-job-type knowledge. Tests that previously
    drove `daemon.run_cron(cfg, sdk, mcp_server=None, job=job)` import this
    function instead (`from ap2.components.cron import run_cron`).
    """
    handler = resolve_cron_handler(job.name)
    await handler(cfg, sdk, mcp_server, job)


def _resolve_mcp_server():
    """Best-effort fetch of the daemon's MCP server reference.

    The `Phase.CRON_DISPATCH` tick hook signature is the uniform
    `(cfg, sdk)` every registry tick hook uses, but the status-report and
    generic LLM-cron handlers need the daemon's `mcp_server` to dispatch
    a sub-agent. The daemon stashes `(sdk, mcp_server)` on
    `status_report._SDK_REF` at startup (`status_report.configure(...)`,
    called from `main_loop`); we read `mcp_server` from there — the same
    process-wide singleton the `mcp__autopilot__status_report_run` tool
    already consumes. Returns `None` when `configure(...)` hasn't run (no
    daemon — e.g. a unit test driving the scheduler directly), which is
    the same null `mcp_server` those handlers tolerate.
    """
    from ap2 import status_report as _status_report_mod

    return _status_report_mod._SDK_REF.get("mcp_server")


async def run_cron_scheduler(cfg: Config, sdk) -> None:
    """Cron scheduler tick hook (TB-381 — `Phase.CRON_DISPATCH`).

    The behavior-preserving replacement for `daemon._tick`'s inline cron
    stage: load jobs, find the due ones, and dispatch each to its
    registered handler. Self-gates on the `AP2_CRON_DISABLED` kill switch
    (resolved via `Manifest.is_enabled()` in `ap2/registry.py`, matching
    the family convention where the daemon walks every tick hook
    regardless of env_flag and the hook self-gates).

    No outer try/except here: per-job errors are caught inside each
    handler (they emit `cron_error` WITH a `job=` field), while a
    whole-stage failure (e.g. `load_jobs` raising on an unreadable
    `cron.yaml`) propagates to `daemon._tick`'s wrap around the
    `Phase.CRON_DISPATCH` walk, which emits the tick-level `cron_error`
    (no `job=` field) — preserving the TB-211 event split bit-for-bit.
    """
    if not default_registry().get(COMPONENT_NAME).is_enabled():
        return
    mcp_server = _resolve_mcp_server()
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    for job in due_jobs(jobs, state, cfg.project_root):
        await run_cron(cfg, sdk, mcp_server, job)
