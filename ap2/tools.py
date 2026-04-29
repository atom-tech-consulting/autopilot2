"""Custom SDK MCP tools for control agents.

The mattermost handler and cron agents call these to mutate the board, the cron
registry, and send Mattermost replies. Task agents do NOT get these tools — they
just code, commit, and exit.

Tools close over a Config so the daemon can wire paths at startup without the
agent having to know them.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from . import events
from .board import Board, locked_board
from .config import Config, bump_next_task_id
from .cron import update_job
from .init import render_briefing


def slugify(text: str, max_len: int = 40) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def _ok(text: str, **fields: Any) -> dict:
    body = {"message": text}
    body.update(fields)
    return {
        "content": [{"type": "text", "text": json.dumps(body)}],
    }


def _err(text: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
        "isError": True,
    }


# ---------------- implementations (SDK-free, directly testable) ----------------


def _allocate_id(board: Board, cfg: Config) -> str:
    """Pick the next TB-N, using max(board max + 1, CLAUDE.md next_task_id)."""
    candidate = max(board.max_id() + 1, cfg.next_task_id)
    cfg.next_task_id = candidate + 1
    claude_md = cfg.project_root / "CLAUDE.md"
    if claude_md.exists():
        bump_next_task_id(claude_md, cfg.next_task_id)
    return f"TB-{candidate}"


def do_board_edit(cfg: Config, args: dict) -> dict:
    action = args.get("action", "")
    task_id = args.get("task_id")
    title = (args.get("title") or "").strip()
    tags = args.get("tags") or []
    briefing = args.get("briefing")
    description = (args.get("description") or "").strip()
    blocked_on = (args.get("blocked_on") or "").strip()

    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }
    move_map = {
        "move_to_ready": "Ready",
        "move_to_active": "Active",
        "move_to_frozen": "Frozen",
        "move_to_complete": "Complete",
        "move_to_backlog": "Backlog",
    }

    try:
        with locked_board(cfg.tasks_file) as board:
            if action in add_map:
                if not title:
                    return _err("title is required for add actions")
                new_id = _allocate_id(board, cfg)
                # TB-69: when add_backlog is called without an explicit briefing
                # payload, auto-fill the briefing with the standard template so
                # every newly-discovered task lands with a load-bearing
                # `## Verification` section. add_ready / add_frozen still pass
                # through — those are for cases where the briefing is being
                # explicitly managed by the caller.
                if action == "add_backlog" and not (briefing or "").strip():
                    briefing = render_briefing(
                        task_id=new_id, title=title, description=description,
                    )
                briefing_rel = None
                if briefing:
                    slug = slugify(title)
                    brief_path = cfg.tasks_dir / f"{slug}.md"
                    # collision avoidance
                    n = 2
                    while brief_path.exists():
                        brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                        n += 1
                    brief_path.parent.mkdir(parents=True, exist_ok=True)
                    brief_path.write_text(briefing)
                    briefing_rel = str(brief_path.relative_to(cfg.project_root))
                desc = description
                if blocked_on:
                    desc = (desc + " " if desc else "") + f"(blocked on: {blocked_on})"
                board.add(
                    add_map[action],
                    task_id=new_id,
                    title=title,
                    tags=tags,
                    description=desc,
                    briefing=briefing_rel,
                )
                return _ok(
                    f"{action} {new_id} {title!r}",
                    task_id=new_id,
                    briefing_path=briefing_rel,
                )

            if action in move_map:
                if not task_id:
                    return _err("task_id is required for move actions")
                to_section = move_map[action]
                checked = True if to_section == "Complete" else None
                try:
                    t = board.move(task_id, to_section, check=checked)
                except KeyError:
                    return _err(f"{task_id} not on board")
                return _ok(f"{action} {t.id}", task_id=t.id, section=t.section)

            if action == "remove":
                if not task_id:
                    return _err("task_id is required for remove")
                removed = board.remove(task_id)
                if removed is None:
                    return _err(f"{task_id} not on board")
                return _ok(f"removed {removed.id}", task_id=removed.id)

            return _err(f"unknown action {action!r}")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def do_pipeline_task_start(cfg: Config, args: dict) -> dict:
    """Atomically launch a long-running pipeline + create a Backlog validation
    task gated on the pipeline's PID liveness (TB-81).

    The launch agent's responsibility collapses from the TB-80 8-step recipe
    (launch + Frozen stub + Backlog validation + monitor cron) to ONE call.
    The validation task auto-promotes when the OS process dies, via
    `Board.next_dispatchable` consulting `pipelines.is_blocking`.
    """
    name = (args.get("name") or "").strip()
    command = (args.get("command") or "").strip()
    validation_title = (args.get("validation_title") or "").strip()
    validation_briefing = args.get("validation_briefing") or ""
    if not name or not command or not validation_title:
        return _err("name, command, and validation_title are required")

    log_dir = cfg.project_root / ".cc-autopilot" / "pipelines"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_tmp = log_dir / f"{name}.log.tmp"
    log_handle = log_tmp.open("a")
    try:
        # `start_new_session=True` puts the child in its own session/process
        # group so a parent (daemon) exit doesn't take it down.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cfg.project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    try:
        import psutil

        started_at = int(psutil.Process(proc.pid).create_time())
    except Exception:  # noqa: BLE001
        # Process may have died instantly, or psutil isn't importable. Fall
        # back to wall clock so we still record SOMETHING — the validation
        # task may unblock erroneously on PID recycling, but this branch is
        # rare and operator-debuggable from the log file.
        started_at = int(time.time())

    log_path = log_dir / f"{name}-{proc.pid}.log"
    try:
        log_tmp.rename(log_path)
    except OSError:
        log_path = log_tmp

    blocker = f"pid:{proc.pid}@{started_at}"
    description = f"(blocked on: {blocker})"

    # Mirror add_backlog briefing-write logic: one file per task, slug-named,
    # collision-suffixed. Briefing payloads from launch agents are typically
    # full markdown; pass them through as-is.
    slug = slugify(validation_title)
    brief_path = cfg.tasks_dir / f"{slug}.md"
    n = 2
    while brief_path.exists():
        brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
        n += 1
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(validation_briefing)
    briefing_rel = str(brief_path.relative_to(cfg.project_root))

    with locked_board(cfg.tasks_file) as board:
        val_id = _allocate_id(board, cfg)
        board.add(
            "Backlog",
            task_id=val_id,
            title=validation_title,
            description=description,
            briefing=briefing_rel,
        )

    events.append(
        cfg.events_file,
        "pipeline_start",
        name=name,
        pid=proc.pid,
        started_at=started_at,
        command=command,
        validation=val_id,
        log=str(log_path),
    )
    return _ok(
        f"pipeline {name!r} started (pid {proc.pid})",
        pid=proc.pid,
        started_at=started_at,
        validation_id=val_id,
        log=str(log_path),
    )


def do_cron_edit(cfg: Config, args: dict) -> dict:
    action = args.get("action", "")
    name = args.get("name")
    if not name:
        return _err("name is required")
    try:
        msg, jobs = update_job(
            cfg.cron_file,
            action,
            name=name,
            interval=args.get("interval"),
            prompt=args.get("prompt"),
            active_when=args.get("active_when"),
            max_turns=args.get("max_turns"),
        )
        return _ok(msg, jobs=[j.name for j in jobs])
    except (KeyError, ValueError) as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def do_task_complete(cfg: Config, args: dict) -> dict:
    """Acknowledge a `task_complete` tool call from a task agent (TB-101).

    The structured payload (status / commit / summary / files_changed /
    tests_passed / cron) is captured by `daemon.run_task` walking the
    SDK message stream — this handler exists only to give the SDK a
    valid response so the agent doesn't loop or treat the call as
    failed. No state mutation here; the daemon owns the routing
    decision after the query returns.

    Replaces the `RESULT:\\n status: ...` free-text contract that
    `result.py` parsed via regex.
    """
    status = args.get("status", "")
    if not isinstance(status, str) or not status.strip():
        return _err("status is required")
    return _ok(f"task_complete acknowledged (status={status})")


def do_git_log_grep(cfg: Config, args: dict) -> dict:
    """Search the project's git log for commits whose message matches `query`.

    Replaces the ad-hoc `Bash("git log --grep=...")` that ideation Step
    1.5 used to call (TB-109). Narrow MCP tool means control agents
    don't need shell access for this — `Bash` was the only legitimate
    dependency in CONTROL_AGENT_TOOLS, and dropping it closes the
    shell-redirect-into-fenced-file corruption surface (TB-108 case).

    Returns one line per match: `<short-sha> <subject>`. Capped at 100.
    Subprocess runs git with arg-list (no `shell=True`), so the query
    is shell-safe — it's a single argument to `--grep`, not interpolated.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    try:
        max_results = int(args.get("max_results") or 20)
    except (TypeError, ValueError):
        max_results = 20
    max_results = max(1, min(max_results, 100))

    if not (cfg.project_root / ".git").exists():
        return _ok("not a git repo", matches=[], count=0)

    try:
        proc = subprocess.run(
            [
                "git",
                "-c", "safe.directory=*",
                "-C", str(cfg.project_root),
                "log",
                "--grep", query,
                "--oneline",
                "-n", str(max_results),
            ],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _err("git log timed out after 10s")
    except FileNotFoundError:
        return _err("git not on PATH")
    if proc.returncode != 0:
        return _err(f"git log failed: {proc.stderr.strip()[-300:]}")

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return _ok(
        f"{len(lines)} commit(s) matched {query!r}",
        matches=lines,
        count=len(lines),
    )


def do_operator_log_append(cfg: Config, args: dict) -> dict:
    """Append a timestamped operator-decision line to
    `.cc-autopilot/operator_log.md` (TB-106).

    Operator-owned channel for decisions ideation can't observe via the
    filesystem (e.g. "decided to keep FRAGILE plists as references" or
    "considered the universe-expansion question, deferred"). Ideation
    reads the log in Step 0 and treats logged items as authoritative —
    won't re-propose them in subsequent cycles.

    Two write paths share this handler:
      - operator-side: `ap2 ack [-t TB-N] "<note>"` (CLI)
      - mattermost-handler-side: `operator_log_append` MCP tool when the
        operator sends `@claude-bot done: ...` style messages.

    Each call appends one bullet line. The file is created with a
    short header on first append. `operator_ack` event emitted for
    auditability.
    """
    note = str(args.get("note") or "").strip()
    if not note:
        return _err("note is required")
    task_id = str(args.get("task_id") or "").strip()

    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements. Append-only.\n"
            "Ideation reads this in Step 0; logged items are authoritative —\n"
            "ideation won't re-propose decisions logged here._\n\n"
        )

    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tb_tag = f" [{task_id}]" if task_id else ""
    line = f"- {ts}{tb_tag} — {note}\n"
    with log_path.open("a") as f:
        f.write(line)

    payload: dict = {"note": note[:500]}
    if task_id:
        payload["task"] = task_id
    events.append(cfg.events_file, "operator_ack", **payload)
    return _ok(f"appended to {log_path.name}", line=line.strip())


def do_ideation_state_write(cfg: Config, args: dict) -> dict:
    """Overwrite `.cc-autopilot/ideation_state.md` with a fresh assessment (TB-90).

    Called by the ideation cron in Step 0 to land the per-cycle progress
    assessment introduced by TB-87. The content is written verbatim — schema
    correctness is the prompt's responsibility, not the tool's. Atomic write
    (tmpfile + rename) so a concurrent reader can't observe a partial file.

    Reads stay through the existing `Read` tool — this tool only wraps the
    write path. Same pattern as `board_edit` / `cron_edit`: broad reads,
    narrow writes.
    """
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return _err("content is required")
    # Soft cap to surface runaway prompts. The TB-87 schema aims for ~200
    # lines (~10-20KB); 50KB leaves headroom for legitimate verbose
    # assessments without letting the file grow unbounded.
    if len(content) > 50_000:
        return _err(
            f"content too long ({len(content)} bytes); aim for <50KB. "
            "Trim to highest-signal items per the prompt's length cap."
        )
    target = (
        cfg.project_root
        / ".cc-autopilot"
        / "ideation_state.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(target)
    events.append(
        cfg.events_file,
        "ideation_state_updated",
        bytes=len(content),
    )
    return _ok(
        f"wrote {len(content)} bytes to ideation_state.md",
        bytes=len(content),
    )


def do_log_event(cfg: Config, args: dict) -> dict:
    typ = args.get("type") or "info"
    summary = args.get("summary") or ""
    evt = events.append(cfg.events_file, typ, summary=summary)
    return _ok(f"logged {typ}", event=evt)


def do_daemon_control(cfg: Config, args: dict) -> dict:
    action = args.get("action")
    reason = args.get("reason") or ""
    if action == "pause":
        cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
        cfg.pause_flag.write_text(reason + "\n")
        events.append(cfg.events_file, "daemon_pause", reason=reason)
        return _ok("daemon paused")
    if action == "resume":
        if cfg.pause_flag.exists():
            cfg.pause_flag.unlink()
        events.append(cfg.events_file, "daemon_resume", reason=reason)
        return _ok("daemon resumed")
    return _err(f"unknown action {action!r}")


def do_mattermost_reply(cfg: Config, args: dict) -> dict:
    channel = args.get("channel") or ""
    text = args.get("text") or ""
    thread_id = args.get("thread_id") or ""
    if not channel or not text:
        return _err("channel and text are required")
    try:
        post_id = _mm_post(channel, text, thread_id)
        events.append(
            cfg.events_file,
            "mattermost_reply",
            channel=channel,
            thread_id=thread_id,
            post_id=post_id,
            summary=text[:200],
        )
        return _ok(f"posted to {channel}", post_id=post_id)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def _mm_post(channel: str, text: str, thread_id: str = "") -> str:
    url = os.environ.get("MATTERMOST_URL")
    token = os.environ.get("MATTERMOST_TOKEN")
    if not url or not token:
        raise RuntimeError("MATTERMOST_URL and MATTERMOST_TOKEN must be set")
    # Resolve channel name → id if needed (names start without alnum restriction,
    # but IDs are 26-char base32). Best-effort: treat 26-char as id.
    channel_id = channel if len(channel) == 26 and channel.isalnum() else _mm_lookup_channel(url, token, channel)
    body = {"channel_id": channel_id, "message": text}
    if thread_id:
        body["root_id"] = thread_id
    req = urllib.request.Request(
        f"{url}/api/v4/posts",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("id", "")


def _mm_lookup_channel(url: str, token: str, name: str) -> str:
    name = name.lstrip("#")
    # Need a team id; we pick the user's first team as a default.
    team_id = _mm_user_team(url, token)
    req = urllib.request.Request(
        f"{url}/api/v4/teams/{team_id}/channels/name/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read())["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"channel {name!r} not found: {e}") from e


_TEAM_CACHE: str | None = None


def _mm_user_team(url: str, token: str) -> str:
    global _TEAM_CACHE
    if _TEAM_CACHE:
        return _TEAM_CACHE
    req = urllib.request.Request(
        f"{url}/api/v4/users/me/teams",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        teams = json.loads(resp.read())
    if not teams:
        raise RuntimeError("user has no mattermost teams")
    _TEAM_CACHE = teams[0]["id"]
    return _TEAM_CACHE


# ---------------- SDK wiring ----------------


def build_mcp_server(cfg: Config):
    """Build the in-process MCP server exposing the custom tools.

    Imported lazily so unit tests don't need the SDK.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "board_edit",
        "Add, move, or remove tasks on the TASKS.md board.",
        {
            "action": str,
            "task_id": str,
            "title": str,
            "tags": list,
            "briefing": str,
            "description": str,
            "blocked_on": str,
        },
    )
    async def board_edit(args):
        return do_board_edit(cfg, args)

    @tool(
        "cron_edit",
        "Add, remove, or update a scheduled cron job.",
        {
            "action": str,
            "name": str,
            "interval": str,
            "prompt": str,
            "active_when": str,
            "max_turns": int,
        },
    )
    async def cron_edit(args):
        return do_cron_edit(cfg, args)

    @tool(
        "mattermost_reply",
        "Send a message to a Mattermost channel or thread.",
        {"channel": str, "text": str, "thread_id": str},
    )
    async def mattermost_reply(args):
        return do_mattermost_reply(cfg, args)

    @tool(
        "log_event",
        "Append an event to the autopilot event log.",
        {"type": str, "summary": str},
    )
    async def log_event(args):
        return do_log_event(cfg, args)

    @tool(
        "daemon_control",
        "Pause or resume the autopilot daemon.",
        {"action": str, "reason": str},
    )
    async def daemon_control(args):
        return do_daemon_control(cfg, args)

    @tool(
        "ideation_state_write",
        "Overwrite .cc-autopilot/ideation_state.md with a fresh per-cycle "
        "progress assessment (TB-87 Step 0). Body is written verbatim — the "
        "ideation prompt is responsible for schema correctness. Returns the "
        "byte count written. Path is fixed; no path arg.",
        {"content": str},
    )
    async def ideation_state_write(args):
        return do_ideation_state_write(cfg, args)

    # Tool name avoids the `task_*` prefix because Claude Code reserves that
    # namespace for its built-in TaskCreate/TaskUpdate/TaskList/TaskGet
    # subagent dispatch tools. Real-SDK smoke runs against `task_complete`
    # showed Claude Code's tool surface filtered the name out — `ToolSearch`
    # returned 0 results for `mcp__autopilot__task_complete` even though the
    # MCP server registered it. Renamed to `report_result` (no `task_`
    # prefix) so the namespace doesn't collide.
    @tool(
        "git_log_grep",
        "Search the project's git log for commits whose message matches "
        "`query` (passed verbatim to `git log --grep=...`). Returns up to "
        "`max_results` (default 20, capped at 100) one-line summaries. "
        "Replaces the ad-hoc `Bash('git log --grep=...')` pattern — "
        "control agents do not have Bash (TB-109).",
        {"query": str, "max_results": int},
    )
    async def git_log_grep(args):
        return do_git_log_grep(cfg, args)

    @tool(
        "operator_log_append",
        "Append a timestamped operator-decision line to "
        ".cc-autopilot/operator_log.md (TB-106). Use ONLY for "
        "operator-mediated messages — e.g. when an operator says "
        "`@claude-bot done: <action>` or `@claude-bot decided: <choice>`. "
        "Args: note (required, one sentence), task_id (optional TB-N). "
        "Ideation reads this log in Step 0 and treats entries as "
        "authoritative; logged decisions are not re-proposed.",
        {"note": str, "task_id": str},
    )
    async def operator_log_append(args):
        return do_operator_log_append(cfg, args)

    @tool(
        "report_result",
        "Report task completion to the autopilot daemon. Call this ONCE at "
        "the end of your run instead of emitting a `RESULT:` text block. "
        "Args: status='complete'|'incomplete'|'blocked'|'failed' (required); "
        "commit=<7-40 char sha or empty>; summary=<one sentence>; "
        "files_changed=<comma-separated paths>; tests_passed='true'|'false'; "
        "cron=<JSON list of {action,name,interval,prompt} dicts, or empty>.",
        # All-string schema — every other MCP tool in this server uses str-
        # only fields. `list` / `bool` types in the schema correlated with
        # Claude Code refusing to surface the tool in earlier smoke runs;
        # strings round-trip cleanly and the daemon-side capture parses
        # `tests_passed` / `files_changed` / `cron` from their string forms.
        {
            "status": str,
            "commit": str,
            "summary": str,
            "files_changed": str,
            "tests_passed": str,
            "cron": str,
        },
    )
    async def report_result(args):
        return do_task_complete(cfg, args)

    @tool(
        "pipeline_task_start",
        "Atomically launch a long-running pipeline as a detached OS process "
        "and create a single Backlog validation task gated on the process's "
        "liveness. The validation task auto-promotes when the process dies. "
        "Use for work that would take >10 minutes as a single agent dispatch.",
        {
            "name": str,
            "command": str,
            "validation_title": str,
            "validation_briefing": str,
        },
    )
    async def pipeline_task_start(args):
        return do_pipeline_task_start(cfg, args)

    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        version = _pkg_version("claude-automation")
    except PackageNotFoundError:
        version = "unknown"

    return create_sdk_mcp_server(
        name="autopilot",
        version=version,
        tools=[
            board_edit,
            cron_edit,
            mattermost_reply,
            log_event,
            daemon_control,
            ideation_state_write,
            git_log_grep,
            operator_log_append,
            report_result,
            pipeline_task_start,
        ],
    )


# Control agents (cron, ideation, mattermost handler) read project state
# via `Read`/`Glob`/`Grep` and mutate it via narrow MCP tools. They do
# NOT get `Bash` (TB-109) — the only legitimate use was ideation's
# `git log --grep=<TASK_ID>` in Step 1.5, replaced by the `git_log_grep`
# MCP tool. Dropping shell access closes the corruption surface that bit
# stoch's TASKS.md (TB-108): a control agent's `Bash("echo > TASKS.md")`
# bypassed every fence we'd built for task agents.
CONTROL_AGENT_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "mcp__autopilot__board_edit",
    "mcp__autopilot__cron_edit",
    "mcp__autopilot__mattermost_reply",
    "mcp__autopilot__log_event",
    "mcp__autopilot__daemon_control",
    "mcp__autopilot__ideation_state_write",
    "mcp__autopilot__git_log_grep",
    "mcp__autopilot__operator_log_append",
]

# `pipeline_task_start` is the first MCP tool task agents can call directly
# (TB-81). The privilege increase is narrow: one tool, atomic, well-scoped to
# launching long-running work that the daemon can't host inside a single
# `await sdk.query(...)` slot. Keep this list otherwise minimal — task agents
# are not control agents and shouldn't gain blanket access to `board_edit`,
# `cron_edit`, etc. via this list.
TASK_AGENT_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "mcp__autopilot__pipeline_task_start",
    "mcp__autopilot__report_result",
]


# Files the task agent must NOT edit. Two enforcement layers wrap each
# entry: (1) `prompts._TASK_HEADER` lists each file with a one-line
# explanation so a well-behaved agent skips them, (2) `daemon.run_task`
# adds `Edit(<path>)` + `Write(<path>)` to `disallowed_tools` so the SDK
# rejects direct calls if the agent tries anyway.
#
# Defense-in-depth, not airtight: a determined agent could still write
# via `Bash` (`echo > path`, `sed -i`, `python -c "open(...).write(...)"`).
# Those rely on prompt compliance — globbing every shell shape that
# touches a fenced file is a losing arms race.
#
# Categories:
#   - Daemon-owned state: TASKS.md, progress.md, events.jsonl,
#     ideation_state.md, CLAUDE.md (the daemon bumps Next task ID).
#   - Daemon-owned config: cron.yaml (control agents edit via cron_edit).
#   - Operator-curated: goal.md — the project mission. Ideation reads it
#     for grounding; if a task could rewrite it, ideation would
#     effectively rewrite its own constraints. Tasks that *want* to update
#     goal.md should surface the recommendation in their RESULT summary
#     instead, leaving the operator to apply.
TASK_AGENT_FENCED_PATHS = (
    "TASKS.md",
    "CLAUDE.md",
    "goal.md",
    ".cc-autopilot/progress.md",
    ".cc-autopilot/events.jsonl",
    ".cc-autopilot/ideation_state.md",
    ".cc-autopilot/cron.yaml",
    ".cc-autopilot/operator_log.md",
)
