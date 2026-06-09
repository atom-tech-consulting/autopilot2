"""Core-registered cron job handlers (TB-381 axis 1).

The cron *scheduler* moved into a registry tick-hook component at
`ap2/components/cron/` (TB-381). The scheduler owns *when* jobs run
(timing, due-detection, the per-job dispatch loop); *what* each job does
is a registered handler contributed by whoever owns the work. This
module is the **core** side of that job-handler registry — the handlers
whose composition is baseline core (status-report) or that call back
into a shared core primitive (`_run_control_agent` for the generic
LLM-cron path; the smoke routine for `real-sdk-smoke`).

The `janitor` handler is NOT here — it's the janitor component's
(`ap2/components/janitor/`), contributed via that manifest's
`hook_points["cron_job_handlers"]`. The cron scheduler aggregates the
component-contributed handlers (via
`registry.contributions("cron_job_handlers")`) on
top of the core handlers below and dispatches a due job to its named
handler, replacing the pre-TB-381 `if job.name == …` switch in
`run_cron`.

Handler contract
----------------
Each handler is a self-contained
`async (cfg, sdk, mcp_server, job) -> None` callable. It owns its own
`cron_*` lifecycle events + `cron_state` advance (`mark_run`) so the
scheduler "knows nothing of what the job does". The three handlers below
are lifted verbatim (behavior-preserving) from the pre-TB-381
`daemon.run_cron` branches:

  - `status_report_handler`  ← the `job.name == "status-report"` branch.
  - `smoke_handler`          ← the `job.name == "real-sdk-smoke"` branch.
  - `generic_llm_handler`    ← the fall-through LLM-cron branch (default).

Import-direction note
---------------------
This is a core module, so it must NOT statically import from
`ap2/components/` (the TB-311 gate). It imports only sibling core
modules. `_run_control_agent` lives in `ap2/daemon.py`; to avoid a
`daemon ↔ cron_handlers` import cycle it is reached via a lazy
`from . import daemon` inside the handler body — the same pattern
`ap2/status_report.py` uses. Env-knob names are unchanged from the
pre-TB-381 `run_cron`.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from . import events, prompts
from . import status_report as _status_report_mod
from .config import Config
from .cron import CronJob, mark_run
from .state_commit import (
    _changed_state_paths,
    _commit_state_files,
    _snapshot_state_paths,
)


# Signature of every cron job handler (core- or component-contributed).
JobHandler = Callable[[Config, object, object, CronJob], Awaitable[None]]


async def status_report_handler(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    """`status-report` cron handler (was `run_cron`'s status-report branch).

    Delegates to the shared routine in `ap2.status_report` so the cron
    path and the chat-trigger MCP tool (`mcp__autopilot__status_report_run`)
    share one prompt, one skip-gate (TB-128), and one event vocabulary.
    The cron path passes `trigger="cron"` so `cron_state[status-report]`
    advances (the chat path explicitly does NOT advance it — operator-
    triggered reports must not silence the next scheduled cron). `job.prompt`
    is ignored: the routine uses `STATUS_REPORT_PROMPT` verbatim so an
    operator's stale `cron.yaml` doesn't drift from the canonical contract.
    The routine owns its own lifecycle events + `mark_run`.
    """
    await _status_report_mod.run_status_report(
        cfg, sdk, mcp_server,
        trigger="cron",
        max_turns=job.max_turns,
    )


async def smoke_handler(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    """`real-sdk-smoke` cron handler (was `run_cron`'s smoke branch).

    Runs the live-API smoke suite as a timeout-bounded subprocess via
    `ap2.smoke_runner.run_smoke_check`. The work is a deterministic shell
    action (running pytest), not an LLM task, so this dispatches a Python
    routine rather than building a control prompt. The routine itself
    emits the `smoke_check_skipped` / `smoke_check_passed` /
    `smoke_check_failed` outcome events + posts the failure-only Mattermost
    alert; we bookend with `cron_start` / `cron_complete` (job=real-sdk-smoke)
    and advance `cron_state[real-sdk-smoke].last_run`. `job.prompt` is an
    ignored stub.
    """
    from . import smoke_runner as _smoke_runner_mod

    events.append(cfg.events_file, "cron_start", job=job.name)
    try:
        await _smoke_runner_mod.run_smoke_check(cfg)
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "cron_error",
            job=job.name,
            error=f"{type(e).__name__}: {e}",
        )
    mark_run(cfg.cron_state_file, job.name)
    events.append(cfg.events_file, "cron_complete", job=job.name)


async def generic_llm_handler(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    """Generic LLM-cron handler (was `run_cron`'s fall-through branch).

    The default handler for any cron job without a more specific
    registered handler. Builds a control prompt and dispatches it through
    the shared core `_run_control_agent` primitive (which stays in core,
    shared with ideation / the Mattermost handler), emitting the
    `cron_start` / `cron_timeout` / `cron_error` / `cron_complete` events
    and committing only the state paths the cron actually mutated.
    """
    from . import daemon as _daemon
    from .tools import CONTROL_AGENT_TOOLS

    prompt = prompts.build_control_prompt(cfg, job.name, job.prompt)
    events.append(cfg.events_file, "cron_start", job=job.name)
    # TB-126: snapshot the state surface before the cron runs so we can
    # commit ONLY paths the cron actually mutated. Without this, a leftover
    # dirty briefing from a prior op rides along with the next cron commit.
    pre_snapshot = _snapshot_state_paths(cfg)
    timed_out, error, stderr_tail, prompt_dump = await _daemon._run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label=f"cron-{job.name}",
        prompt=prompt,
        allowed_tools=job.allowed_tools or CONTROL_AGENT_TOOLS,
        max_turns=job.max_turns,
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "cron_timeout",
            job=job.name,
            timeout_s=cfg.control_timeout_s,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "cron_error",
            job=job.name,
            error=error,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    mark_run(cfg.cron_state_file, job.name)
    events.append(cfg.events_file, "cron_complete", job=job.name)
    # No-op for crons that didn't touch the board (e.g. status-report).
    touched = _changed_state_paths(pre_snapshot, _snapshot_state_paths(cfg))
    if touched:
        _commit_state_files(cfg, f"state: cron {job.name}", paths=touched)


# Core-registered cron handler map (TB-381). The cron scheduler overlays
# the component-contributed handlers
# (`registry.contributions("cron_job_handlers")`) on
# top of this; any job name not present in either map falls through to
# `DEFAULT_CRON_HANDLER`. `status-report`'s composition is baseline core;
# `real-sdk-smoke` runs the core smoke routine.
CORE_CRON_HANDLERS: dict[str, JobHandler] = {
    "status-report": status_report_handler,
    "real-sdk-smoke": smoke_handler,
}

# Default handler for an unrecognized cron job — the generic LLM-cron
# path that calls back into the core `_run_control_agent` primitive.
DEFAULT_CRON_HANDLER: JobHandler = generic_llm_handler
