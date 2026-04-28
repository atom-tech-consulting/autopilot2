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

from . import doctor, events, retry, sandbox
from .board import Board, locked_board
from .config import Config
from .cron import load_jobs, load_state
from .init import init_project
from .tools import do_board_edit


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
    counts = {s: len(board.sections.get(s, [])) for s in ["Active", "Ready", "Backlog", "Complete", "Frozen"]}
    jobs = load_jobs(cfg.cron_file)
    state = load_state(cfg.cron_state_file)
    paused = cfg.pause_flag.exists()

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
        }
        print(json.dumps(out, indent=2))
        return 0

    print(f"daemon:   {'running' if running else 'stopped'} (pid {pid or '-'}){' [paused]' if paused else ''}")
    print(f"tick:     {cfg.tick_interval_s}s")
    print(f"board:    {counts['Active']}A / {counts['Ready']}R / {counts['Backlog']}B / {counts['Complete']}C / {counts['Frozen']}F")
    print(f"cron:     {len(jobs)} jobs ({', '.join(j.name for j in jobs) or '-'})")
    print(f"tasks:    {cfg.tasks_file}")
    print(f"events:   {cfg.events_file}")
    nxt = board.next_ready()
    if nxt:
        print(f"next:     {nxt.id} {nxt.title}")
    return 0


def cmd_add(cfg: Config, args: argparse.Namespace) -> int:
    title = args.title
    section = args.section.capitalize()
    section_map = {
        "Ready": "add_ready",
        "Backlog": "add_backlog",
        "Frozen": "add_frozen",
    }
    action = section_map.get(section)
    if not action:
        print(f"unknown section: {args.section}", file=sys.stderr)
        return 2
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
    res = do_board_edit(
        cfg,
        {
            "action": action,
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
    print(f"added {msg.get('task_id')} to {section}")
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
    """
    res = do_board_edit(cfg, {"action": "move_to_backlog", "task_id": args.task_id})
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print(f"moved {args.task_id} to Backlog")
    return 0


def cmd_unfreeze(cfg: Config, args: argparse.Namespace) -> int:
    """Move a Frozen task back to Backlog and clear its retry counter.

    Validation + move happen inside `locked_board()` so the section check is
    atomic w.r.t. the daemon. Retry reset and event emission run after lock
    release — they have their own fcntl-locked state files.
    """
    with locked_board(cfg.tasks_file) as board:
        loc = board.find(args.task_id)
        if loc is None:
            print(f"{args.task_id} not on board", file=sys.stderr)
            return 1
        section, _ = loc
        if section != "Frozen":
            print(
                f"{args.task_id} is in {section}, not Frozen — "
                f"use `ap2 backlog {args.task_id}` for non-frozen moves",
                file=sys.stderr,
            )
            return 1
        board.move(args.task_id, "Backlog", check=None)
    retry.reset_attempt(cfg.retry_state_file, args.task_id)
    events.append(cfg.events_file, "task_unfrozen", task=args.task_id)
    print(f"unfroze {args.task_id} → Backlog (retry counter reset)")
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
    """Read the installed `claude-automation` version. Single source of truth
    is `pyproject.toml`; we read it via importlib so the CLI tracks the
    installed build and we don't have to keep two version strings in sync.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("claude-automation")
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

    s = sub.add_parser("pause", help="pause the daemon (sets a flag)")
    s.add_argument("--reason", default="")
    s.set_defaults(func=cmd_pause)

    s = sub.add_parser("resume", help="clear the pause flag")
    s.set_defaults(func=cmd_resume)

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
