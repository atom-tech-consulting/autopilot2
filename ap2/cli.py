"""`autopilot` CLI — start/stop/status/add/logs/skip.

Intended to be run as `python -m ap2` or via the console_scripts entrypoint.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import doctor, events, rollback, sandbox
from .board import Board, board_file_lock
from .config import Config
from .cron import load_jobs, load_state
from .init import init_project
from . import tools


def _read_pid(cfg: Config) -> int | None:
    if not cfg.pid_file.exists():
        return None
    try:
        return int(cfg.pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _require_oauth_token() -> int:
    """Refuse to start the daemon when CLAUDE_CODE_OAUTH_TOKEN isn't in env (TB-79).

    Without the token the SDK control protocol times out on handshake and the
    daemon idles through `Control request timeout: initialize` events — the
    failure mode is silent because `claude` exits before printing anything to
    stderr. Returns 1 + prints remediation; the source-of-truth for env
    delivery is operator policy (login shell, sudoers env_keep, project env
    file), so ap2 stays out of guessing.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return 0
    print(
        "ap2: refusing to start — CLAUDE_CODE_OAUTH_TOKEN is not in the env.\n"
        "Without it the SDK control protocol will silently time out at\n"
        "initialize. Pick one:\n"
        "  - launch via login shell:  sudo -u <user> -i ap2 start\n"
        "  - install token first:     ap2 sandbox install-token <user>\n"
        "                             (then re-launch via -i, or set\n"
        "                             CLAUDE_CODE_OAUTH_TOKEN explicitly)\n"
        "  - one-off env pass:        sudo --preserve-env=CLAUDE_CODE_OAUTH_TOKEN \\\n"
        "                                 -u <user> ap2 start",
        file=sys.stderr,
    )
    return 1


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    pid = _read_pid(cfg)
    if _is_running(pid):
        print(f"already running (pid {pid})")
        return 0
    # stale pid file
    if cfg.pid_file.exists():
        cfg.pid_file.unlink()
    rc = _require_oauth_token()
    if rc != 0:
        return rc
    if args.foreground:
        from .daemon import run

        run(str(cfg.project_root))
        return 0
    # Fork into background via `python -m ap2 _run`.
    log = cfg.project_root / ".cc-autopilot" / "daemon.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "ap2", "--project", str(cfg.project_root), "_run"]
    with log.open("a") as f:
        proc = subprocess.Popen(
            cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    time.sleep(0.5)
    print(f"started (pid {proc.pid}), logs: {log}")
    return 0


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    pid = _read_pid(cfg)
    if not pid or not _is_running(pid):
        print("not running")
        if cfg.pid_file.exists():
            cfg.pid_file.unlink()
        return 0
    sig = signal.SIGKILL if args.force else signal.SIGTERM
    os.kill(pid, sig)
    print(f"sent {sig.name} to pid {pid}")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    pid = _read_pid(cfg)
    running = _is_running(pid)
    board = Board.load(cfg.tasks_file)
    counts = {s: len(board.sections.get(s, [])) for s in
              ["Active", "Ready", "Backlog", "Pipeline Pending", "Complete", "Frozen"]}
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    paused = cfg.pause_flag.exists()
    # TB-131: pending operator-queued ops (CLI / MM-handler appends that
    # haven't been drained by the daemon's tick yet). Visible here so an
    # operator can spot a stalled queue (depth > 0 with the daemon down
    # ⇒ ops will sit until the daemon comes back up).
    queue_pending = tools.operator_queue_pending_count(cfg)
    # TB-130: when the daemon is up and the web UI wasn't disabled, surface
    # the URL so operators don't have to remember to run `ap2 web`
    # separately. Resolution mirrors the daemon's own — same env vars, same
    # default — so what we print is the URL the daemon is actually serving.
    web_url = _resolve_web_url(cfg) if running else None

    if args.json:
        out = {
            "running": running,
            "pid": pid,
            "paused": paused,
            "tick_interval_s": cfg.tick_interval_s,
            "board": counts,
            "cron_jobs": [j.name for j in jobs],
            "cron_last_run": state,
            "tasks_file": str(cfg.tasks_file),
            "events_file": str(cfg.events_file),
            "web_url": web_url,
            "operator_queue_pending": queue_pending,
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"daemon:   {'running' if running else 'stopped'} (pid {pid or '-'}){' [paused]' if paused else ''}")
    print(f"tick:     {cfg.tick_interval_s}s")
    print(
        f"board:    {counts['Active']}A / {counts['Ready']}R / "
        f"{counts['Backlog']}B / {counts['Pipeline Pending']}P / "
        f"{counts['Complete']}C / {counts['Frozen']}F"
    )
    print(f"cron:     {len(jobs)} jobs ({', '.join(j.name for j in jobs) or '-'})")
    print(f"tasks:    {cfg.tasks_file}")
    print(f"events:   {cfg.events_file}")
    if web_url:
        print(f"web:      {web_url}")
    if queue_pending:
        print(
            f"pending:  {queue_pending} operator op"
            f"{'s' if queue_pending != 1 else ''}"
        )
    nxt = board.next_ready()
    if nxt:
        print(f"next:     {nxt.id} {nxt.title}")
    return 0


def _resolve_web_url(cfg: Config) -> str | None:
    """The URL the daemon-spawned web UI is serving on, or `None` when off.

    Returns `None` when `AP2_WEB_DISABLED` is set (the operator opted out
    of the bundled UI for this daemon process). Otherwise resolves
    host/port the same way `ap2.daemon._web_loop_for_daemon` does, so the
    printed URL matches reality. We don't grep events.jsonl for a
    `web_start` line — the daemon writes the same URL we'd compute, and
    spinning up file IO for status is wasteful.
    """
    from . import web as _web

    if _web.is_web_disabled():
        return None
    port = _web.daemon_web_port()
    return f"http://127.0.0.1:{port}/"


def cmd_add(cfg: Config, args: argparse.Namespace) -> int:
    title = args.title
    section = args.section.capitalize()
    section_map = {
        "Ready": "add_ready",
        "Backlog": "add_backlog",
        "Frozen": "add_frozen",
    }
    op = section_map.get(section)
    if not op:
        print(f"unknown section: {args.section}", file=sys.stderr)
        return 2
    # TB-134: reject multi-line title / description / tags before we
    # touch the operator queue. The same gate fires inside
    # `do_operator_queue_append`, but failing fast at the CLI keeps the
    # error message obviously CLI-shaped (no "ERROR:" prefix) and avoids
    # any half-written briefing-file state.
    for field_name, value in (
        ("title", title),
        ("description", args.description or ""),
    ):
        err = tools._validate_single_line(field_name, value)
        if err:
            print(f"ap2 add: {err}", file=sys.stderr)
            return 1
    for tag in args.tags or []:
        err = tools._validate_single_line("tag", tag)
        if err:
            print(f"ap2 add: {err}", file=sys.stderr)
            return 1
    briefing = None
    if args.briefing_file:
        briefing = Path(args.briefing_file).read_text()
    tags = list(args.tags or [])
    # --no-verify becomes a `#no-verify` tag on the task line. The daemon
    # checks for this tag in `_run_verify` to skip the project-wide gate
    # for tasks the operator already knows can't be meaningfully verified
    # by AP2_VERIFY_CMD (docs, infra, etc.). Tags survive the round-trip
    # through TASK_LINE_RE so the marker persists across daemon restarts.
    if getattr(args, "no_verify", False) and "#no-verify" not in tags:
        tags.append("#no-verify")
    # TB-131: stage through the operator queue rather than mutate TASKS.md
    # directly. The TB-N is pre-allocated synchronously (so we can print
    # it immediately), the briefing file is pre-written, and only the
    # TASKS.md insertion is deferred until the daemon's next tick.
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": op,
            "title": title,
            "tags": tags,
            "description": args.description or "",
            "briefing": briefing,
        },
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    msg = json.loads(res["content"][0]["text"])
    print(f"{msg.get('task_id')} (queued; will land at next tick)")
    return 0


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
    rep = doctor.diagnose(cfg.project_root, user)
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
        extras = {k: v for k, v in e.items() if k not in ("ts", "type")}
        extra = " ".join(f"{k}={_short(v)}" for k, v in extras.items())
        print(f"{ts} {typ:16s} {extra}")
    return 0


def cmd_backlog(cfg: Config, args: argparse.Namespace) -> int:
    """Move a task to Backlog from any section.

    Replaces the older `cmd_skip` (TB-77) — same code path, name now matches
    the underlying `move_to_backlog` action instead of the historical
    "skip in queue" use case.

    TB-131: routes through the operator queue. Snapshot validation runs at
    queue-append time (rejects unknown TB-N immediately); the actual
    move lands on the daemon's next tick.
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "move_to_backlog", "task_id": args.task_id}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(f"queued move {args.task_id} → Backlog (will land at next tick)")
    return 0


def cmd_unfreeze(cfg: Config, args: argparse.Namespace) -> int:
    """Move a Frozen task back to Backlog and clear its retry counter.

    TB-131: routes through the operator queue. Snapshot validation runs at
    queue-append time so an unfreeze on a non-Frozen task is rejected
    immediately — exactly as before. The retry-counter reset + the
    `task_unfrozen` event are emitted by the daemon's drain step.
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "unfreeze", "task_id": args.task_id}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(
        f"queued unfreeze {args.task_id} → Backlog "
        f"(retry counter will reset on drain)"
    )
    return 0


def cmd_delete(cfg: Config, args: argparse.Namespace) -> int:
    """Permanently remove a task from the board.

    Refuses to delete from Active (in-flight) or Ready (about to dispatch)
    by default — the daemon's orphan-recovery and dispatch invariants
    assume those sections aren't out from under it. Use `ap2 backlog
    <TB-N>` first to move the task somewhere safe, OR pass `--force` if
    you really mean it.

    TB-131: routes through the operator queue. The Active/Ready/Pipeline
    Pending refusal happens at queue-append time so the operator gets
    immediate feedback. The `task_deleted` event is emitted by the
    daemon's drain step (after the briefing under `.cc-autopilot/tasks/`
    is preserved on disk — git history preserves the briefing if it was
    committed; the ghost file is harmless).
    """
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "delete",
            "task_id": args.task_id,
            "force": bool(args.force),
        },
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(f"queued delete {args.task_id} (will land at next tick)")
    return 0


def cmd_rollback(cfg: Config, args: argparse.Namespace) -> int:
    """Linear rollback (TB-111).

    Walk back along first-parent history to a boundary commit and
    `git reset --hard` to it. Atomic via `locked_board()`. Mid-history
    rollback (revert TB-X while keeping TB-Y after) is explicitly out of
    scope — operators do that by hand with `git revert`.
    """
    if not (cfg.project_root / ".git").exists():
        print("ap2 rollback: project is not a git repo — nothing to roll back",
              file=sys.stderr)
        return 1

    # Pre-flight: refuse a dirty working tree. Rollback isn't a stash.
    porcelain = subprocess.run(
        ["git", "-C", str(cfg.project_root), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if porcelain.returncode != 0:
        print(f"ap2 rollback: `git status --porcelain` failed: "
              f"{porcelain.stderr.strip()}", file=sys.stderr)
        return 1
    if porcelain.stdout.strip() and not args.force:
        print(
            "ap2 rollback: working tree is dirty — refusing.\n"
            "  Commit, stash, or `git checkout -- .` your changes first,\n"
            "  or pass --force to bypass (the dirt will be discarded).",
            file=sys.stderr,
        )
        return 1

    # Resolve boundary from -n / --task / --to (mutually exclusive; default -n 1).
    boundary: str | None = None
    if args.to:
        # Explicit ancestor sha. Refuse if not an ancestor (no rebases mid-rollback).
        if not rollback.is_ancestor(cfg, args.to):
            print(f"ap2 rollback: {args.to} is not an ancestor of HEAD — refusing",
                  file=sys.stderr)
            return 1
        # Resolve to a full SHA so the print is unambiguous.
        rp = subprocess.run(
            ["git", "-C", str(cfg.project_root), "rev-parse", args.to],
            capture_output=True, text=True,
        )
        boundary = rp.stdout.strip() if rp.returncode == 0 else args.to
    elif args.task:
        boundary = rollback.resolve_boundary_by_task(cfg, args.task)
        if boundary is None:
            print(
                f"ap2 rollback: {args.task} not found in HEAD's first-parent "
                f"history.\n  Try `git log --grep={args.task} --oneline` — "
                f"the task may be too far back, or it shipped on a side branch.",
                file=sys.stderr,
            )
            return 1
    else:
        n = args.n if args.n is not None else 1
        if n <= 0:
            print("ap2 rollback: -n must be ≥ 1", file=sys.stderr)
            return 2
        boundary = rollback.resolve_boundary_by_n(cfg, n)
        if boundary is None:
            print(f"ap2 rollback: history doesn't have {n} task-completions "
                  f"to roll back", file=sys.stderr)
            return 1

    affected = rollback.list_affected_commits(cfg, boundary)
    if not affected:
        print(f"ap2 rollback: nothing to roll back "
              f"(boundary {boundary[:8]} == HEAD)")
        return 0
    affected_tasks = rollback.affected_task_ids(affected)
    pipeline_warnings = rollback.list_alive_pipelines_in_range(cfg, boundary)

    # Print plan.
    print("Rollback plan:")
    print(f"  Boundary: {boundary[:8]}")
    print(f"  Affected commits ({len(affected)}):")
    for sha, subject in affected:
        print(f"    - {sha[:8]} {subject}")
    if affected_tasks:
        print(f"  Affected tasks: {', '.join(affected_tasks)}")
    if pipeline_warnings:
        print("  Pipelines still running (NOT auto-killed):")
        for w in pipeline_warnings:
            print(f"    pid {w['pid']} ({w['name'] or '?'}) "
                  f"— log: {w['log']}")
    if not args.yes:
        try:
            reply = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("ap2 rollback: aborted (no changes made)")
            return 0

    # Execute under board lock for atomicity vs. the daemon. We use the
    # save-less variant because `git reset --hard` already wrote the
    # post-reset TASKS.md; locked_board's save-on-exit would clobber it.
    with board_file_lock(cfg.tasks_file):
        try:
            rollback.linear_rollback_to(cfg, boundary)
        except Exception as exc:  # noqa: BLE001
            events.append(
                cfg.events_file, "rollback_error",
                boundary=boundary, error=f"{type(exc).__name__}: {exc}",
            )
            print(f"ap2 rollback: failed: {exc}", file=sys.stderr)
            return 1

    events.append(
        cfg.events_file,
        "task_rollback",
        boundary_sha=boundary,
        reverted_commits=[
            {"sha": sha, "subject": subject} for sha, subject in affected
        ],
        affected_tasks=affected_tasks,
        pipeline_warnings=pipeline_warnings,
    )
    print(f"ap2 rollback: reset to {boundary[:8]} "
          f"({len(affected)} commit(s) reverted, "
          f"{len(affected_tasks)} task(s) affected)")
    if pipeline_warnings:
        print(f"  warning: {len(pipeline_warnings)} pipeline subprocess(es) "
              f"still running — terminate manually if rerunning")
    return 0


def cmd_pause(cfg: Config, args: argparse.Namespace) -> int:
    cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_flag.write_text((args.reason or "") + "\n")
    events.append(cfg.events_file, "daemon_pause", reason=args.reason or "")
    print("paused (flag written)")
    return 0


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    if cfg.pause_flag.exists():
        cfg.pause_flag.unlink()
    events.append(cfg.events_file, "daemon_resume")
    print("resumed")
    return 0


def cmd_cron_list(cfg: Config, args: argparse.Namespace) -> int:
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    for j in jobs:
        last = state.get(j.name, 0)
        last_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last)) if last else "never"
        print(f"{j.name:30s} every {j.interval_s}s  last={last_str}  cond={j.active_when or '-'}")
    return 0


def cmd_ack(cfg: Config, args: argparse.Namespace) -> int:
    """Append an operator-decision line to .cc-autopilot/operator_log.md
    (TB-106). Used to communicate "I did X" / "I decided Y" back to ap2
    so ideation stops re-proposing actions whose effects aren't visible
    on the filesystem (e.g. "considered FRAGILE plist retention,
    decided to keep them"). Optional `-t TB-N` ties the ack to a task.
    """
    res = tools.do_operator_log_append(
        cfg,
        {"note": args.note, "task_id": args.task or ""},
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(json.loads(res["content"][0]["text"])["line"])
    return 0


def cmd_web(cfg: Config, args: argparse.Namespace) -> int:
    """Start the local read-only web UI for daemon state and event log.

    Defaults to 127.0.0.1 so the (no-auth) page can't leak full event
    payloads — briefings, prompt-dump paths, Mattermost message bodies —
    off the box. Override with --host at your own risk.
    """
    from . import web

    web.serve(cfg, host=args.host, port=args.port)
    return 0


def _short(v, limit=120) -> str:
    s = str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _add_mm_url_token_args(p: argparse.ArgumentParser) -> None:
    """Shared --mm-* flags for user-setup / install-mm.

    Precedence (resolved in sandbox._resolve_mm_url_token): explicit --mm-url/
    --mm-token, then --mm-url-env/--mm-token-env env-var names, then the
    caller's own MATTERMOST_URL/MATTERMOST_TOKEN from the environment.
    """
    p.add_argument("--mm-url", metavar="URL",
                   help="MATTERMOST_URL to install into ~user/.zshenv")
    p.add_argument("--mm-token", metavar="TOKEN",
                   help="MATTERMOST_TOKEN to install (prefer --mm-token-env)")
    p.add_argument("--mm-url-env", metavar="VAR",
                   help="read MATTERMOST_URL from this env var instead")
    p.add_argument("--mm-token-env", metavar="VAR",
                   help="read MATTERMOST_TOKEN from this env var instead")


def _version_string() -> str:
    """Read the installed `autopilot2` version. Single source of truth is
    `pyproject.toml`; we read it via importlib so the CLI tracks the installed
    build and we don't have to keep two version strings in sync.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("autopilot2")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description="Autopilot v2 CLI.")
    p.add_argument(
        "--version",
        action="version",
        version=f"ap2 {_version_string()}",
    )
    p.add_argument("--project", default=None, help="project root (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start the daemon (backgrounded)")
    s.add_argument("--foreground", action="store_true", help="run in foreground")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("_run", help=argparse.SUPPRESS)
    s.set_defaults(func=lambda cfg, a: (__import__("ap2.daemon", fromlist=["run"]).run(str(cfg.project_root)) or 0))

    s = sub.add_parser("stop", help="stop the daemon")
    s.add_argument("-f", "--force", action="store_true")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", help="show daemon + board status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("add", help="add a task")
    s.add_argument("title")
    s.add_argument("-s", "--section", default="Ready", help="Ready|Backlog|Frozen")
    s.add_argument("-t", "--tags", nargs="*")
    s.add_argument("-d", "--description", default="")
    s.add_argument("--briefing-file", help="path to a briefing markdown file")
    s.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the AP2_VERIFY_CMD project-wide test gate for this task "
             "(adds `#no-verify` to its tags)",
    )
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("init", help="scaffold gitignores + .cc-autopilot/tasks/ (idempotent)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="check ap2 readiness (project skeleton + sandbox)")
    s.add_argument("--user", default=None, help="sandbox user (default: claude-agent)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser(
        "check",
        help="check on-disk state-file integrity: TASKS.md shape, "
             "briefing-link resolution, cron.yaml schema, JSON state "
             "parseability, insights front matter (TB-108). Exits 1 on "
             "errors; warnings don't fail.",
    )
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("logs", help="show recent events")
    s.add_argument("-n", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("backlog", help="move a task to Backlog from any section")
    s.add_argument("task_id")
    s.set_defaults(func=cmd_backlog)

    s = sub.add_parser(
        "unfreeze",
        help="move a Frozen task to Backlog + clear its retry counter "
             "(refuses if the task isn't currently in Frozen)",
    )
    s.add_argument("task_id")
    s.set_defaults(func=cmd_unfreeze)

    s = sub.add_parser(
        "delete",
        help="permanently remove a task from the board (refuses Active/"
             "Ready without --force; emits task_deleted event for audit)",
    )
    s.add_argument("task_id")
    s.add_argument("-f", "--force", action="store_true",
                   help="allow deletion from Active or Ready (use with care)")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser(
        "rollback",
        help="linear rollback (TB-111): walk back from HEAD by N tasks "
             "(or to a specific TB-N / sha) and `git reset --hard`. "
             "Restores TASKS.md + every committed state file coherently. "
             "Refuses dirty working tree by default.",
    )
    grp = s.add_mutually_exclusive_group()
    grp.add_argument("-n", type=int, default=None,
                     help="roll back the last N task-completions (default: 1)")
    grp.add_argument("--task", metavar="TB-N",
                     help="roll back to before TB-N (linear: undoes everything "
                          "between HEAD and TB-N too)")
    grp.add_argument("--to", metavar="SHA",
                     help="reset to an explicit ancestor sha")
    s.add_argument("-y", "--yes", action="store_true",
                   help="skip the interactive confirm prompt")
    s.add_argument("--force", action="store_true",
                   help="proceed even with a dirty working tree (will discard)")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("pause", help="pause the daemon (sets a flag)")
    s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_pause)

    s = sub.add_parser("resume", help="clear the pause flag")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser(
        "ack",
        help="record an operator-decision in .cc-autopilot/operator_log.md "
             "(TB-106) so ideation stops re-proposing actions whose effects "
             "aren't filesystem-visible",
    )
    s.add_argument("note", help="the decision or action to record (one sentence)")
    s.add_argument("-t", "--task", default=None,
                   help="optional TB-N this ack relates to")
    s.set_defaults(func=cmd_ack)

    s = sub.add_parser(
        "web",
        help="start a local read-only web UI for status + events "
             "(127.0.0.1 by default; no auth — local-only)",
    )
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1)")
    s.add_argument("--port", type=int, default=7820,
                   help="bind port (default: 7820)")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("cron", help="cron utilities")
    sub_cron = s.add_subparsers(dest="cron_cmd", required=True)
    sc = sub_cron.add_parser("list", help="list cron jobs")
    sc.set_defaults(func=cmd_cron_list)

    s = sub.add_parser("sandbox", help="OS-level sandbox user + project helpers")
    s.set_defaults(func=lambda cfg, a: (s.print_help() or 0))
    sub_sbx = s.add_subparsers(dest="sbx_cmd")

    sc = sub_sbx.add_parser("user-audit", help="verify sandbox user has no creds")
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_user_audit)

    sc = sub_sbx.add_parser("user-setup", help="create sandbox user (prompts before running sudo)")
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    sc.add_argument("--skip-token", action="store_true",
                    help="don't prompt for CLAUDE_CODE_OAUTH_TOKEN post-creation")
    sc.add_argument("--skip-statusline", action="store_true",
                    help="don't install the project's statusline into ~user/.claude/")
    _add_mm_url_token_args(sc)
    sc.set_defaults(func=sandbox.cmd_user_setup)

    sc = sub_sbx.add_parser(
        "install-token",
        help="install CLAUDE_CODE_OAUTH_TOKEN into ~<user>/.zshenv "
             "(obtain via `claude setup-token`)",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.add_argument("--token-env", metavar="VAR",
                    help="read token from this env var instead of prompting")
    sc.set_defaults(func=sandbox.cmd_install_token)

    sc = sub_sbx.add_parser(
        "install-statusline",
        help="copy hooks/statusline-command.sh into ~<user>/.claude/ + "
             "wire it into ~<user>/.claude/settings.json",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_install_statusline)

    sc = sub_sbx.add_parser(
        "install-howto",
        help="copy ap2/howto.md to ~<user>/.claude/ap2-howto.md so a Claude "
             "session running as the sandbox user can read it for context",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_install_howto)

    sc = sub_sbx.add_parser(
        "install-mm",
        help="install MATTERMOST_URL + MATTERMOST_TOKEN into ~<user>/.zshenv",
    )
    sc.add_argument("user", nargs="?", default=sandbox.DEFAULT_USER)
    _add_mm_url_token_args(sc)
    sc.set_defaults(func=sandbox.cmd_install_mm)

    sc = sub_sbx.add_parser("project-setup", help="clone <source> into ~<user>/repos/")
    sc.add_argument("source", help="path to the source repo (human's clone)")
    sc.add_argument("--user", default=sandbox.DEFAULT_USER)
    sc.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    sc.add_argument("--mm-channel", metavar="NAME",
                    help="resolve #NAME via MATTERMOST_URL/TOKEN in current env and "
                         "write AP2_MM_CHANNELS=<id> into <project>/.cc-autopilot/env")
    sc.add_argument("--git-name", default=sandbox.DEFAULT_GIT_NAME,
                    help=f"repo-local git user.name (default: {sandbox.DEFAULT_GIT_NAME!r})")
    sc.add_argument("--git-email", default=sandbox.DEFAULT_GIT_EMAIL,
                    help=f"repo-local git user.email (default: {sandbox.DEFAULT_GIT_EMAIL!r})")
    sc.set_defaults(func=sandbox.cmd_project_setup)

    sc = sub_sbx.add_parser(
        "install-channel",
        help="resolve a MM channel name to an ID and write "
             "AP2_MM_CHANNELS into <project>/.cc-autopilot/env",
    )
    sc.add_argument("project", help="path to an existing ap2 project clone")
    sc.add_argument("channel", help="channel name (with or without leading #)")
    sc.add_argument("--user", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_install_channel)

    sc = sub_sbx.add_parser("project-audit", help="verify isolated project clone")
    sc.add_argument("path")
    sc.add_argument("--user", default=sandbox.DEFAULT_USER)
    sc.set_defaults(func=sandbox.cmd_project_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load(args.project)
    return args.func(cfg, args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
