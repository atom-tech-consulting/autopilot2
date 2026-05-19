"""Diagnostic / introspection CLI handlers (TB-264 split from `ap2/cli.py`).

Owns the inspection / one-shot integrity / cron-management verbs:

  - `cmd_doctor`     — environment-readiness check (project skeleton +
                        sandbox + CLI presence).
  - `cmd_check`      — on-disk state-file integrity (TASKS.md shape,
                        briefing-link resolution, cron.yaml schema,
                        JSON state parseability, insights front matter
                        — TB-108).
  - `cmd_logs`       — render recent events with the TB-158
                        verification_failed pretty-printer and TB-180
                        usage-event compaction.
  - `cmd_cron_list`  — list cron jobs + last-fire timestamps.
  - `cmd_cron_edit`  — operator-CLI-only add/remove/update for
                        `.cc-autopilot/cron.yaml` (TB-146 + TB-202;
                        refuses mid-task).
  - `cmd_init`       — idempotent project scaffolding (gitignore +
                        `.cc-autopilot/tasks/`).

`_format_verification_failed_row` lives here because it's used only by
`cmd_logs`. `_active_task_id` is imported from `cli_review` rather than
re-defined — one canonical implementation across the two refuse-if-active
call sites (`cli_review.cmd_backfill_proposals` and this module's
`cmd_cron_edit`).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from ap2._shared import short
from . import doctor, events, sandbox, tools
from .cli_review import _active_task_id
from .config import Config
from .cron import load_jobs, load_state
from .init import init_project


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    """Idempotent project scaffolding: gitignore entries + tasks dir.

    `cfg.project_root` is already resolved by Config.load() — we don't take a
    DIR argument because every other ap2 subcommand operates on the same root.
    """
    report = init_project(cfg.project_root)
    print(f"ap2 init: {report.project_root}")
    report.print()
    return 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    """One-shot integrity check on TASKS.md, cron.yaml, JSON state files,
    insights front matter, and briefing-link resolution (TB-108).

    Sibling of `ap2 doctor` (which checks the environment — sandbox user,
    OAuth token, project clone, CLI presence). `check` checks the data on
    disk. Exit nonzero on any error; warnings don't fail.
    """
    from . import check

    report = check.check_project(cfg)
    print(check.render_json(report) if args.json else check.render_text(report))
    return 0 if report.ok else 1


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    """One-shot environment-readiness check (project skeleton + sandbox + CLI)."""
    user = args.user or sandbox.DEFAULT_USER
    # TB-252: thread cfg through so `verify_timeout_audit` can read
    # `cfg.verify_timeout_s` (the resolved env value) without
    # re-loading the project env from inside doctor.
    rep = doctor.diagnose(cfg.project_root, user, cfg=cfg)
    rep.print()
    return 0 if rep.ok else 1


def cmd_logs(cfg: Config, args: argparse.Namespace) -> int:
    n = args.n
    evts = events.tail(cfg.events_file, n=n)
    if args.json:
        for e in evts:
            print(json.dumps(e))
        return 0
    for e in evts:
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        # TB-158: dedicated rendering for `verification_failed` rows so the
        # operator sees N/M passed + the failing bullet headlines without
        # opening events.jsonl in an editor or expanding raw json.
        if typ == "verification_failed":
            print(_format_verification_failed_row(ts, e))
            continue
        # TB-180: compact one-line rendering for the three usage-carrying
        # event types (`judge_call`, `task_run_usage`, `control_run_usage`)
        # — the verbose `usage` / `model_usage` / `server_tool_use` blobs
        # otherwise wrap the row across several lines and drown the
        # at-a-glance signal. Same shared helper as TB-179's web rendering
        # so the CLI and `/events` page stay symmetric. Operators wanting
        # the raw payload pass `--json` (regression-pinned).
        if typ in ("judge_call", "task_run_usage", "control_run_usage"):
            compact = events.summarize_usage_event(e)
            if compact:
                print(f"{ts} {typ:16s} {compact}")
                continue
        extras = {k: v for k, v in e.items() if k not in ("ts", "type")}
        extra = " ".join(f"{k}={short(v, 120)}" for k, v in extras.items())
        print(f"{ts} {typ:16s} {extra}")
    return 0


def _format_verification_failed_row(ts: str, e: dict) -> str:
    """TB-158: pretty-print a `verification_failed` event for `ap2 logs`.

    Shape:
        <ts>  verification_failed  <task>  <pass>/<total> passed, <f> failed, <u> unverified
          ✗ [<kind>]  <bullet, truncated to ~120>
                     ↳ <judge note, truncated to ~200>

    Passing / unverified bullets are NOT individually rendered (they live
    in the counter only) — the briefing's `## Out of scope` calls this out
    explicitly to keep the noise/signal ratio in the operator's favor.
    Operators wanting the raw payload pass `--json` (regression-pinned).
    """
    summary = events.summarize_verification_failed(
        e, max_bullet=120, max_note=200,
    )
    task = str(e.get("task") or "").strip() or "?"
    lines = [
        f"{ts} verification_failed {task}  {summary['summary_line']}"
    ]
    for fb in summary["failed_bullets"]:
        kind = fb.get("kind") or "?"
        bullet = fb.get("bullet") or ""
        notes = fb.get("notes") or ""
        lines.append(f"  ✗ [{kind}]  {bullet}")
        if notes:
            lines.append(f"            ↳ {notes}")
    return "\n".join(lines)


def cmd_cron_list(cfg: Config, args: argparse.Namespace) -> int:
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    for j in jobs:
        last = state.get(j.name, 0)
        last_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last)) if last else "never"
        print(f"{j.name:30s} every {j.interval_s}s  last={last_str}  cond={j.active_when or '-'}")
    return 0


def cmd_cron_edit(cfg: Config, args: argparse.Namespace) -> int:
    """Operator CLI for mutating `.cc-autopilot/cron.yaml` (TB-146 +
    TB-202).

    TB-146 retired `cron_edit` from every agent toolset — cron schedule
    mutation is operator-CLI-only. The handler under the hood is
    `tools.do_cron_edit`; this command is the operator-facing wrapper
    invoked as `ap2 cron edit <action> <name> [...flags]`.

    TB-202: pre-flight refuse-if-active gate — cron.yaml is fenced
    and not exempt from the TB-110 post-hoc snapshot check, so a
    mid-task `ap2 cron edit` would trigger a false-positive rollback.
    The refuse-if-active gate is the cheap mitigation; queue-routing
    is overkill for an operation that fires during project setup or
    cadence-tuning, not routinely.
    """
    active_id = _active_task_id(cfg)
    if active_id is not None:
        print(
            f"ap2 cron edit: a task is currently active "
            f"({active_id}) — refusing.\n"
            f"  cron edit writes to fenced `.cc-autopilot/cron.yaml` and "
            f"racing the active task would trigger a state_violation "
            f"rollback.\n"
            f"  Wait for the task to complete (see `ap2 status`) or pause "
            f"the daemon, then retry. Note: `ap2 pause` halts dispatch of "
            f"new tasks but does NOT abort the in-flight one; pause helps "
            f"only for the NEXT slot.",
            file=sys.stderr,
        )
        return 1

    payload: dict = {"action": args.action, "name": args.name}
    if args.interval is not None:
        payload["interval"] = args.interval
    if args.prompt is not None:
        payload["prompt"] = args.prompt
    if args.active_when is not None:
        payload["active_when"] = args.active_when
    if args.max_turns is not None:
        payload["max_turns"] = args.max_turns
    res = tools.do_cron_edit(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(res["content"][0]["text"])
    return 0
