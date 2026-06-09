"""TB-381 (axis 1): cron scheduler component + job-handler registry + extended
tick-phase vocabulary — the first tick-stage extraction (canary).

Pins the structural cleavage required by the **extract the remaining core
subsystems into components** focus:

  - The cron *scheduler* (due-check loop + per-job dispatch + `cron_*`
    lifecycle events) runs as a registry tick-hook component
    (`ap2/components/cron/`) registered on the new `Phase.CRON_DISPATCH`,
    instead of inline in `daemon._tick`.
  - `run_cron`'s hardcoded `if job.name == …` switch is replaced by a
    job-handler registry: components contribute named handlers (the
    janitor component contributes `{"janitor": …}`); core contributes the
    `status-report` / `real-sdk-smoke` handlers + the generic LLM-cron
    default. The scheduler resolves `job.name` to a handler and
    dispatches, knowing nothing of what the job does.
  - The `Phase` enum gains `CRON_DISPATCH` (used here) and `IDEATION`
    (reserved for axis 3); `daemon._tick` walks both.
  - Core never statically imports `ap2/components/cron/` — the daemon
    resolves the scheduler + handlers via the registry. The import-
    direction CI gate (`test_core_import_direction.py`) still passes.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from ap2 import cron_handlers, daemon, events, ideation, tools
from ap2.components import janitor as janitor_component
from ap2.components.cron import (
    impl as cron_impl,
    resolve_cron_handler,
    run_cron,
    run_cron_scheduler,
)
from ap2.config import Config
from ap2.cron import CronJob, save_jobs
from ap2.init import init_project
from ap2.registry import Phase, Registry, default_registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_cfg(tmp_path: Path) -> Config:
    """Fresh ap2 project under `tmp_path`."""
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


# ---------------------------------------------------------------------------
# (1) Component is registered + discoverable
# ---------------------------------------------------------------------------


def test_cron_component_discoverable_and_registered():
    """`Registry.discover()` surfaces the `cron` component with a
    `tick_hook` registered on `Phase.CRON_DISPATCH` (the manifest +
    impl land at `ap2/components/cron/`)."""
    registry = Registry.discover()
    names = {m.name for m in registry.components}
    assert "cron" in names, (
        f"TB-381: the cron component should be discoverable via the "
        f"filesystem walk of `ap2/components/*/manifest.py`; "
        f"discovered={sorted(names)}"
    )
    manifest = registry.get("cron")
    assert "tick_hook" in manifest.hook_points, manifest.hook_points
    assert callable(manifest.hook_points["tick_hook"])
    phases = [p for p, _ in manifest.tick_hooks]
    assert Phase.CRON_DISPATCH in phases, (
        f"TB-381: cron must register a tick hook on CRON_DISPATCH; "
        f"got phases={phases}"
    )
    # The scheduler is the registered CRON_DISPATCH hook.
    cron_hooks = registry.tick_hooks(Phase.CRON_DISPATCH)
    assert cron_impl.run_cron_scheduler in cron_hooks


def test_cron_component_has_kill_switch_default_on():
    """The cron component is a default-on toggle-able component with the
    `AP2_CRON_DISABLED` kill switch (mirrors the janitor /
    auto_unfreeze kill-switch family)."""
    manifest = default_registry().get("cron")
    assert manifest.env_flag == "AP2_CRON_DISABLED", manifest.env_flag
    assert manifest.default_enabled is True


def test_manifest_file_exists():
    """Verification bullet `test -f ap2/components/cron/manifest.py`."""
    here = Path(__file__).resolve().parents[1]  # ap2/
    assert (here / "components" / "cron" / "manifest.py").is_file()


# ---------------------------------------------------------------------------
# (2) Extended tick-phase vocabulary + daemon walk
# ---------------------------------------------------------------------------


def test_phase_enum_gains_cron_dispatch_and_ideation():
    """The `Phase` enum gains CRON_DISPATCH (used here) and IDEATION
    (reserved for axis 3)."""
    assert hasattr(Phase, "CRON_DISPATCH")
    assert hasattr(Phase, "IDEATION")


def test_tick_walks_cron_dispatch_and_ideation_phases():
    """Source-pin: `daemon._tick` walks both new phases via
    `registry.tick_hooks(...)`."""
    src = inspect.getsource(daemon._tick)
    assert "tick_hooks(Phase.CRON_DISPATCH)" in src, (
        "TB-381: `_tick` must walk `registry.tick_hooks(Phase.CRON_DISPATCH)` "
        "(replacing the inline cron loop)."
    )
    assert "tick_hooks(Phase.IDEATION)" in src, (
        "TB-381: `_tick` must walk `registry.tick_hooks(Phase.IDEATION)` "
        "(reserved for axis 3)."
    )


def test_ideation_phase_is_empty_today():
    """The IDEATION phase has no registered hooks yet — axis 3 ships the
    ideation subpackage; this task only adds the phase."""
    assert default_registry().tick_hooks(Phase.IDEATION) == []


def test_daemon_has_no_job_name_switch():
    """Verification bullet: `run_cron`'s hardcoded `if job.name == …`
    switch is gone from daemon.py (replaced by registry dispatch)."""
    daemon_src = (Path(__file__).resolve().parents[1] / "daemon.py").read_text()
    # Mirror of `! grep -qE "if job\.name ==|job\.name == \"" ap2/daemon.py`.
    assert "if job.name ==" not in daemon_src, (
        "TB-381: daemon.py must not contain an `if job.name ==` cron switch."
    )
    assert 'job.name == "' not in daemon_src


def test_core_does_not_statically_import_cron_component():
    """Verification bullet (import-direction): daemon.py / cli*.py /
    tools.py do not statically import the cron component."""
    ap2_root = Path(__file__).resolve().parents[1]
    targets = [ap2_root / "daemon.py", ap2_root / "tools.py"]
    targets += list(ap2_root.glob("cli*.py"))
    for path in targets:
        src = path.read_text()
        assert "from ap2.components.cron" not in src, path
        assert "import ap2.components.cron" not in src, path


# ---------------------------------------------------------------------------
# (3) Job-handler registry: resolution + dispatch
# ---------------------------------------------------------------------------


def test_resolve_cron_handler_routes_by_name():
    """The job-handler registry resolves a job name to its registered
    handler — core handlers for `status-report` / `real-sdk-smoke`, the
    janitor component's handler for `janitor`, and the generic LLM-cron
    default for anything else."""
    assert resolve_cron_handler("status-report") is cron_handlers.status_report_handler
    assert resolve_cron_handler("real-sdk-smoke") is cron_handlers.smoke_handler
    assert resolve_cron_handler("janitor") is janitor_component.run_janitor_cron
    assert resolve_cron_handler("some-llm-cron") is cron_handlers.DEFAULT_CRON_HANDLER


def test_janitor_handler_contributed_via_registry():
    """The `janitor` handler comes from the janitor component's manifest
    (`hook_points["cron_job_handlers"]`), aggregated by the registry's
    generic `contributions("cron_job_handlers")` accessor — not hardcoded
    in the scheduler."""
    handlers = default_registry().contributions("cron_job_handlers")
    assert handlers.get("janitor") is janitor_component.run_janitor_cron


def test_due_job_dispatched_to_registered_handler(project_cfg, monkeypatch):
    """A due job is dispatched to its registered handler — proving the
    scheduler resolves via the job-handler registry, not a `job.name`
    switch. We register a recording handler for a custom job name and
    assert the scheduler calls it with `(cfg, sdk, mcp_server, job)`."""
    cfg = project_cfg
    calls: list[tuple] = []

    async def _recording_handler(c, sdk, mcp_server, job):
        calls.append((c, sdk, mcp_server, job.name))

    # Contribute the handler to the CORE handler map (the scheduler
    # overlays component handlers on top of these).
    monkeypatch.setitem(cron_handlers.CORE_CRON_HANDLERS, "demo-job", _recording_handler)

    # A due job (empty cron_state → last_run defaults to 0 → due now).
    save_jobs(cfg.cron_file, [CronJob(name="demo-job", interval_s=60, prompt="x")])

    asyncio.run(run_cron_scheduler(cfg, sdk="SDK"))

    assert len(calls) == 1, calls
    assert calls[0][1] == "SDK"
    assert calls[0][3] == "demo-job"


def test_run_cron_dispatcher_resolves_handler(project_cfg, monkeypatch):
    """`run_cron(cfg, sdk, mcp_server, job)` (the per-job dispatcher
    tests previously imported from `daemon`) resolves + awaits the
    registered handler."""
    cfg = project_cfg
    seen: list[str] = []

    async def _h(c, sdk, mcp_server, job):
        seen.append(job.name)

    monkeypatch.setitem(cron_handlers.CORE_CRON_HANDLERS, "solo", _h)
    job = CronJob(name="solo", interval_s=60, prompt="")
    asyncio.run(run_cron(cfg, sdk=None, mcp_server=None, job=job))
    assert seen == ["solo"]


# ---------------------------------------------------------------------------
# (4) Self-gate on AP2_CRON_DISABLED — "cron simply doesn't fire"
# ---------------------------------------------------------------------------


def test_scheduler_self_gates_when_disabled(project_cfg, monkeypatch):
    """With `AP2_CRON_DISABLED` truthy the scheduler no-ops — a due job
    is NOT dispatched and no `cron_*` event fires."""
    cfg = project_cfg
    calls: list[str] = []

    async def _recording_handler(c, sdk, mcp_server, job):
        calls.append(job.name)

    monkeypatch.setitem(cron_handlers.CORE_CRON_HANDLERS, "demo-job", _recording_handler)
    save_jobs(cfg.cron_file, [CronJob(name="demo-job", interval_s=60, prompt="x")])

    monkeypatch.setenv("AP2_CRON_DISABLED", "1")
    asyncio.run(run_cron_scheduler(cfg, sdk="SDK"))

    assert calls == [], "TB-381: disabled cron scheduler must not dispatch"
    cron_events = [
        e for e in events.tail(cfg.events_file, 50)
        if str(e.get("type", "")).startswith("cron_")
    ]
    assert cron_events == [], cron_events


# ---------------------------------------------------------------------------
# (5) All-components-disabled config still boots + runs a task (cron silent)
# ---------------------------------------------------------------------------


def test_all_disabled_config_boots_runs_task_cron_silent(project_cfg, monkeypatch):
    """End-to-end on `daemon._tick`: with every component disabled
    (including cron) and a due cron job present plus a Ready task, the
    tick still dispatches the Ready task while the cron stage stays
    silent ("cron simply doesn't fire"). Pins the briefing's all-
    components-disabled boot test."""
    cfg = project_cfg

    # Disable every env-flag-bearing component, including cron.
    for manifest in Registry.discover().components:
        if manifest.env_flag is None:
            continue
        if manifest.default_enabled:
            monkeypatch.setenv(manifest.env_flag, "1")
        else:
            monkeypatch.delenv(manifest.env_flag, raising=False)

    # A due cron job that, were the scheduler enabled, would dispatch.
    save_jobs(cfg.cron_file, [CronJob(name="demo-job", interval_s=60, prompt="x")])
    # A Ready task to dispatch.
    cfg.tasks_file.write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-900** **Smoke task** `#x` — desc.\n\n"
        "## Backlog\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )

    dispatched: list[str] = []

    async def _record_run_task(c, sdk, mcp_server, task):
        dispatched.append(task.id)

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    # Stub the heavy / SDK-bound stages; leave the CRON_DISPATCH walk +
    # task-dispatch stage real so we observe both facts.
    monkeypatch.setattr(daemon, "run_task", _record_run_task)
    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_async)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda c: None)
    monkeypatch.setattr(ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(ideation, "force_ideate", _noop_async)
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda c: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    asyncio.run(daemon._tick(cfg, sdk=None, mcp_server=None))

    # The Ready task was dispatched — core boots + runs a task.
    assert dispatched == ["TB-900"], dispatched
    # Cron stayed silent — no cron_start (or any cron_*) event.
    cron_events = [
        e for e in events.tail(cfg.events_file, 100)
        if str(e.get("type", "")).startswith("cron_")
    ]
    assert cron_events == [], cron_events
