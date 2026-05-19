"""Pipeline-pending sweep (TB-178) — verify long-running pipeline tasks
once all of their background subprocesses have died.

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) calls `_sweep_pipeline_pending` once per tick; this
module owns the walk-pending + dead-detection + verify-resolve logic.

The sweep depends on a few daemon-side helpers (`_run_verify`,
`_maybe_per_task_verify`, `_handle_failure`, `_append_progress`). To
avoid a circular import at module-load time AND to preserve the test
contract — `monkeypatch.setattr(daemon, "_sweep_pipeline_pending", ...)`
already takes effect via the re-export — we resolve those helpers
through `from . import daemon as _daemon` calls inside the sweep body.
Each call resolves through daemon's current attribute value so any
mid-test monkeypatches on `daemon._run_verify` (etc.) take effect.

Public surface:

  - `_sweep_pipeline_pending(cfg, sdk)`: the orchestrator entry point.
    Walks Pipeline Pending and verifies any task whose pipelines all
    died, routing the verdict through complete (move + progress
    append) or `_handle_failure(status="verification_failed")`.
  - `_pipeline_alive(pipeline)`: pid liveness + create-time
    cross-check (psutil when available, bare `os.kill(pid, 0)`
    otherwise).
"""
from __future__ import annotations

import os

from . import events, retry, tools
from .board import Board
from .config import Config
from .result import TaskResult
from .state_commit import _commit_state_files, _task_state_paths
from .tools import do_board_edit


async def _sweep_pipeline_pending(cfg: Config, sdk) -> None:
    """Walk Pipeline Pending and verify any task whose pipelines all died.

    For each task in `Pipeline Pending` we read events.jsonl backwards to
    find the most recent `task_pipeline_pending` event for that task — its
    `pipelines` field lists the (pid, started_at) tuples we need to check.
    A pipeline is "dead" if `os.kill(pid, 0)` raises ProcessLookupError
    (or if psutil reports the process create_time differs from the
    recorded `started_at`, defending against pid recycling). When every
    pipeline for the task is dead, re-run the verification harness:

      1. Project-wide gate (`_run_verify`) — same as the synchronous path.
      2. Per-task verification (`_maybe_per_task_verify`) — runs the
         briefing's `## Verification` bullets against the post-pipeline
         working tree.

    Pass → move to Complete, append progress, dispatch any cron directives
    captured at launch time. Fail → `_handle_failure(status="verification_failed")`
    routes through Backlog (with retry-counter bump) → Frozen at
    exhaustion. Tick continues; the next dispatch picks the (now-Backlog)
    task back up.
    """
    if not cfg.tasks_file.exists():
        return
    board = Board.load(cfg.tasks_file)
    pending = list(board.iter_tasks("Pipeline Pending"))
    if not pending:
        return

    # TB-263: late-bound daemon access so monkeypatches on
    # `daemon._run_verify` / `daemon._handle_failure` /
    # `daemon._maybe_per_task_verify` / `daemon._append_progress` (set in
    # downstream tests stubbing the verify harness) take effect through
    # daemon's current attribute values rather than this module's stale
    # snapshot at import time. The functions live in daemon.py; the sweep
    # logic lives here.
    from . import daemon as _daemon

    # Index task_pipeline_pending events by task id (newest wins).
    task_pipelines: dict[str, list[dict]] = {}
    summaries: dict[str, dict] = {}
    for evt in events.tail(cfg.events_file, n=2000):
        if evt.get("type") != "task_pipeline_pending":
            continue
        tid = evt.get("task")
        if not isinstance(tid, str):
            continue
        pls = evt.get("pipelines") or []
        if isinstance(pls, list):
            task_pipelines[tid] = [p for p in pls if isinstance(p, dict)]
            summaries[tid] = {
                "commit": evt.get("commit", "") or "",
                "summary": evt.get("summary", "") or "",
            }

    for task in pending:
        pipelines = task_pipelines.get(task.id) or []
        if not pipelines:
            # Defensive: no record of which pids gate this task. Skip —
            # don't auto-resolve without evidence the dispatcher knew
            # about. Operator can manually move the task off Pipeline
            # Pending if needed.
            continue
        alive = [p for p in pipelines if _pipeline_alive(p)]
        if alive:
            continue
        # All pipelines dead — verify and resolve.
        result_summary = summaries.get(task.id, {})
        final_status = "complete"
        verify_res = _daemon._run_verify(cfg, task)
        if verify_res is not None and not verify_res.passed:
            events.append(
                cfg.events_file,
                "verification_failed",
                task=task.id,
                source="pipeline_pending",
                command=verify_res.command,
                exit_code=verify_res.exit_code,
                stderr_tail=verify_res.stderr_tail,
                duration_s=round(verify_res.duration_s, 2),
            )
            _daemon._handle_failure(
                cfg, task,
                status="verification_failed",
                extras={
                    "kind": "project_wide",
                    "source": "pipeline_pending",
                    "verify_command": verify_res.command,
                    "exit_code": str(verify_res.exit_code),
                    "stderr_tail": verify_res.stderr_tail[:300],
                },
            )
            final_status = "verification_failed"
        else:
            # TB-252: same `verify_passed` audit emission as the
            # synchronous path above — gives the doctor's
            # `verify_timeout_audit` a duration signal from the
            # pipeline-pending verify path too. Carries `source` so
            # the audit can distinguish path-of-origin if needed
            # (today the audit aggregates both).
            if verify_res is not None:
                events.append(
                    cfg.events_file,
                    "verify_passed",
                    task=task.id,
                    source="pipeline_pending",
                    command=verify_res.command,
                    exit_code=verify_res.exit_code,
                    duration_s=round(verify_res.duration_s, 2),
                )
            per_verdict = await _daemon._maybe_per_task_verify(cfg, sdk, task)
            if per_verdict is not None and per_verdict.overall == "fail":
                events.append(
                    cfg.events_file,
                    "verification_failed",
                    task=task.id,
                    kind="per_task",
                    source="pipeline_pending",
                    overall=per_verdict.overall,
                    criteria=[
                        {"kind": c.kind, "status": c.status,
                         "bullet": c.bullet[:200], "notes": c.notes[:200]}
                        for c in per_verdict.criteria
                    ],
                    duration_s=round(per_verdict.duration_s, 2),
                )
                _daemon._handle_failure(
                    cfg, task,
                    status="verification_failed",
                    extras={
                        "kind": "per_task",
                        "source": "pipeline_pending",
                        "failed_criteria": "; ".join(
                            f"[{c.status}] {c.bullet[:120]}"
                            for c in per_verdict.criteria
                            if c.status == "fail"
                        )[:400] or "(no criteria captured)",
                    },
                )
                final_status = "verification_failed"
            else:
                if per_verdict is not None and per_verdict.overall == "partial":
                    events.append(
                        cfg.events_file,
                        "verification_partial",
                        task=task.id,
                        source="pipeline_pending",
                        criteria=[
                            {"kind": c.kind, "status": c.status,
                             "bullet": c.bullet[:200], "notes": c.notes[:200]}
                            for c in per_verdict.criteria
                        ],
                    )
                do_board_edit(cfg, {
                    "action": "move_to_complete", "task_id": task.id,
                })
                retry.reset_attempt(cfg.retry_state_file, task.id)
                synth = TaskResult(
                    status="complete",
                    commit=result_summary.get("commit", ""),
                    summary=result_summary.get("summary", "") or
                            f"pipelines completed ({len(pipelines)}); verification passed",
                    files_changed=[],
                    tests_passed=None,
                    cron=[],
                    raw="(pipeline_pending → complete)",
                )
                _daemon._append_progress(cfg, task, synth)
        events.append(
            cfg.events_file,
            "task_complete",
            task=task.id,
            status=final_status,
            source="pipeline_pending",
            commit=result_summary.get("commit", ""),
            summary=(result_summary.get("summary") or "")[:300],
        )
        # TB-188: terminal-event reconciliation, mirroring `run_task`.
        # The pipeline_pending sweep is the second of two task_complete
        # emission sites; both must reconcile so a proposal whose work
        # rode through the pipeline path doesn't end up with an empty
        # `outcome` block in its record.
        sweep_commit = result_summary.get("commit", "") or ""
        if final_status == "complete":
            tools.reconcile_proposal_outcome(
                cfg, task.id,
                decision_kind="completed",
                decision_actor="daemon",
                commit=sweep_commit[:8] or None,
            )
        elif final_status == "verification_failed":
            tools.reconcile_proposal_outcome(
                cfg, task.id,
                decision_kind="verification_failed",
                decision_actor="verifier",
                commit=sweep_commit[:8] or None,
            )
        board_after = Board.load(cfg.tasks_file)
        loc = board_after.find(task.id)
        dest = loc[0] if loc else "?"
        _commit_state_files(
            cfg, f"state: {task.id} → {dest}",
            paths=_task_state_paths(task),
        )


def _pipeline_alive(pipeline: dict) -> bool:
    """True if the pipeline subprocess identified by (pid, started_at) is
    still running. Defends against pid recycling by comparing
    `psutil.Process(pid).create_time()` to the recorded `started_at` when
    psutil is available; falls back to a bare `os.kill(pid, 0)` check.
    """
    pid = pipeline.get("pid")
    if not isinstance(pid, int):
        return False
    started_at = pipeline.get("started_at")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but isn't ours — treat as alive (we can't kill, so
        # we don't reliably know it's dead either). Won't happen in
        # practice since the daemon spawned it.
        return True
    if isinstance(started_at, (int, float)):
        try:
            import psutil

            ct = int(psutil.Process(pid).create_time())
            if abs(ct - int(started_at)) > 2:
                # PID recycled — the pid we recorded is gone, replaced
                # by an unrelated process.
                return False
        except Exception:  # noqa: BLE001
            pass
    return True
