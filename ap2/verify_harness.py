"""Project-wide + per-task verification harness (TB-66 / TB-69 / TB-127).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`run_task`, `_sweep_pipeline_pending`) calls these helpers
on every task-completion path; this module owns the regression-gate
command execution + per-task verifier dispatch.

Public surface (re-exported from `ap2/daemon.py` so existing call sites
and tests resolve through one name):

  - `VerifyResult`: dataclass returned by `_run_verify`.
  - `_run_verify(cfg, task)`: execute `cfg.verify_cmd` against the
    current working tree. Returns None on skip (gate unconfigured or
    task tagged `#no-verify`).
  - `_maybe_per_task_verify(cfg, sdk, task)`: dispatch the per-task
    briefing verifier (TB-69). Returns None when the briefing has no
    `## Verification` section.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import verify
from .config import Config


@dataclass
class VerifyResult:
    """Outcome of running the project-wide AP2_VERIFY_CMD against HEAD.

    Returned by `_run_verify` when the gate is configured. `exit_code=None`
    means the command exceeded `AP2_VERIFY_TIMEOUT_S`. stderr/stdout are
    tail-truncated to 2k chars to keep events.jsonl entries bounded.
    """

    passed: bool
    command: str
    exit_code: int | None
    stderr_tail: str
    stdout_tail: str
    duration_s: float


def _run_verify(cfg: Config, task) -> "VerifyResult | None":
    """Execute the project-wide regression gate, returning a result or None.

    Returns None (skip path) when:
      - AP2_VERIFY_CMD is unset or blank — the default; preserves pre-TB-66
        behavior so projects that haven't opted in see no change.
      - The task carries `#no-verify` — operator opt-out for tasks the gate
        can't meaningfully check (docs-only, infra changes the project's
        test command can't see, etc.).

    Otherwise runs `cfg.verify_cmd` via `shell=True` in `cfg.project_root`
    and returns a `VerifyResult`. Note `shell=True` is intentional: the
    command is operator-supplied configuration (not agent-supplied input),
    so shell parsing of forms like `uv run pytest -q` is the desired
    behavior, not an injection risk.
    """
    if not cfg.verify_cmd:
        return None
    if "#no-verify" in (task.tags or []):
        return None
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cfg.verify_cmd,
            shell=True,
            cwd=str(cfg.project_root),
            capture_output=True,
            text=True,
            timeout=cfg.verify_timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        # `e.stderr` and `e.stdout` may be bytes or str depending on Python
        # version + capture path. Normalize to str so callers don't care.
        def _to_str(x) -> str:
            if x is None:
                return ""
            if isinstance(x, bytes):
                return x.decode("utf-8", errors="replace")
            return x

        return VerifyResult(
            passed=False,
            command=cfg.verify_cmd,
            exit_code=None,
            stderr_tail=_to_str(e.stderr)[-2000:],
            stdout_tail=_to_str(e.stdout)[-2000:],
            duration_s=time.monotonic() - t0,
        )
    return VerifyResult(
        passed=proc.returncode == 0,
        command=cfg.verify_cmd,
        exit_code=proc.returncode,
        stderr_tail=proc.stderr[-2000:],
        stdout_tail=proc.stdout[-2000:],
        duration_s=time.monotonic() - t0,
    )


async def _maybe_per_task_verify(cfg: Config, sdk, task) -> "verify.VerifyVerdict | None":
    """Run the per-task verifier (TB-69) for `task` if its briefing has a
    `## Verification` section. Returns None to mean "skip" (legacy task or
    no concrete criteria) — the caller proceeds to move_to_complete unchanged.
    """
    if not task.briefing:
        return None
    p = Path(task.briefing)
    full = p if p.is_absolute() else cfg.project_root / p
    if not full.exists():
        return None
    text = full.read_text()
    return await verify.verify_task(
        briefing_text=text,
        project_root=cfg.project_root,
        timeout_s=cfg.verify_timeout_s,
        sdk=sdk,
        # TB-127: hand the verifier the task id so prose-bullet judging
        # can locate the task's actual implementation commit (subject
        # `<task.id>: ...`) instead of HEAD. On retries of an
        # already-committed task, HEAD is a daemon state-bookkeeping
        # commit; without `task_id` the prose judge sees only that and
        # hallucinates "no changes to file X".
        task_id=task.id,
        # TB-157: thread events_file through so per-judge `judge_call`
        # events land on the canonical aggregation surface. The judge
        # path bypasses the daemon's `_log_message` (its own SDK loop),
        # so this is the only capture point for prose-judge cost.
        events_file=cfg.events_file,
        # TB-334 (axis 5 core cluster): thread cfg so the prose-judge
        # path resolves agent-runtime knobs (`agent_model`,
        # `agent_effort`, `verify_judge_max_turns`) through
        # `Config.get_core_value` rather than the pre-migration direct
        # `os.environ.get` reads. Same precedence at call time.
        cfg=cfg,
    )
