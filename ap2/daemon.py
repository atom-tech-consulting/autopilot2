"""Autopilot v2 daemon — the main loop.

This is a Python scheduler (not a Claude session). Each tick it:
  1. Pulls new mattermost messages → spawns a handler agent per message.
  2. Runs any due cron jobs.
  3. Picks the next Ready task off the board and runs it.

Each unit of work is a fresh SDK `query()` call, so contexts never accumulate.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from . import events, prompts
from .board import Board
from .config import Config
from .cron import CronJob, due_jobs, load_jobs, load_state, mark_run, save_state
from .mattermost import check_new_messages
from .result import TaskResult, parse as parse_result
from .tools import (
    CONTROL_AGENT_TOOLS,
    TASK_AGENT_TOOLS,
    build_mcp_server,
    do_board_edit,
)


RUNNING = True


def _handle_signal(signum, frame):  # noqa: ARG001
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def run_task(cfg: Config, sdk, task) -> None:
    """Execute a single Ready task in an isolated SDK query()."""
    prompt = prompts.build_task_prompt(cfg, task)
    events.append(cfg.events_file, "task_start", task=task.id, title=task.title)
    do_board_edit(cfg, {"action": "move_to_active", "task_id": task.id})

    result_text = ""
    commit_hash = ""
    try:
        async for msg in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                allowed_tools=TASK_AGENT_TOOLS,
                disallowed_tools=["Bash(git push*)", "Bash(rm -rf *)"],
                permission_mode="bypassPermissions",
                max_turns=int(os.environ.get("AP2_TASK_MAX_TURNS", 50)),
                setting_sources=["project"],
            ),
        ):
            text = _extract_text(msg)
            if text:
                result_text = text
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "task_error",
            task=task.id,
            error=f"{type(e).__name__}: {e}",
        )
        do_board_edit(cfg, {"action": "move_to_backlog", "task_id": task.id})
        return

    parsed = parse_result(result_text)
    commit_hash = parsed.commit
    if parsed.status == "complete":
        do_board_edit(cfg, {"action": "move_to_complete", "task_id": task.id})
        _append_progress(cfg, task.id, parsed)
    else:
        do_board_edit(cfg, {"action": "move_to_backlog", "task_id": task.id})
        _append_attempts(cfg, task, parsed)
    events.append(
        cfg.events_file,
        "task_complete",
        task=task.id,
        status=parsed.status,
        commit=commit_hash,
        summary=parsed.summary[:300],
    )


async def handle_message(cfg: Config, sdk, mcp_server, msg: dict) -> None:
    prompt = prompts.build_mattermost_prompt(cfg, msg)
    events.append(
        cfg.events_file,
        "mattermost",
        channel=msg.get("channel_name"),
        user=msg.get("user"),
        thread_id=msg.get("thread_id"),
        summary=(msg.get("text") or "")[:300],
    )
    try:
        async for _ in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=CONTROL_AGENT_TOOLS,
                permission_mode="bypassPermissions",
                max_turns=int(os.environ.get("AP2_CONTROL_MAX_TURNS", 15)),
                setting_sources=["project"],
            ),
        ):
            pass
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "mattermost_error",
            error=f"{type(e).__name__}: {e}",
        )


async def run_cron(cfg: Config, sdk, mcp_server, job: CronJob) -> None:
    prompt = prompts.build_cron_prompt(cfg, job.name, job.prompt)
    events.append(cfg.events_file, "cron_start", job=job.name)
    allowed = job.allowed_tools or CONTROL_AGENT_TOOLS
    try:
        async for _ in sdk.query(
            prompt=prompt,
            options=sdk.ClaudeAgentOptions(
                cwd=str(cfg.project_root),
                mcp_servers={"autopilot": mcp_server},
                allowed_tools=allowed,
                permission_mode="bypassPermissions",
                max_turns=job.max_turns,
                setting_sources=["project"],
            ),
        ):
            pass
    except Exception as e:  # noqa: BLE001
        events.append(
            cfg.events_file,
            "cron_error",
            job=job.name,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        mark_run(cfg.cron_state_file, job.name)
        events.append(cfg.events_file, "cron_complete", job=job.name)


def _extract_text(msg) -> str:
    """Best-effort extraction of an assistant message's final text block."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in reversed(content):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text
    result = getattr(msg, "result", None)
    if isinstance(result, str):
        return result
    return ""


def _append_progress(cfg: Config, task_id: str, r: TaskResult) -> None:
    cfg.progress_file.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {_today()}: {task_id} — {r.summary}"
    if r.commit:
        line += f" ({r.commit[:8]})"
    with cfg.progress_file.open("a") as f:
        f.write(line + "\n")


def _append_attempts(cfg: Config, task, r: TaskResult) -> None:
    if not task.briefing:
        return
    p = Path(task.briefing)
    full = p if p.is_absolute() else cfg.project_root / p
    if not full.exists():
        return
    text = full.read_text()
    header = "\n## Attempts\n"
    entry = (
        f"\n### {_today()} — {r.status}\n"
        f"{r.summary or '(no summary)'}\n"
    )
    if header in text:
        full.write_text(text.rstrip() + entry)
    else:
        full.write_text(text.rstrip() + header + entry)


def _today() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


async def main_loop(cfg: Config) -> None:
    cfg.ensure_dirs()
    _import_sdk_or_die()
    import claude_agent_sdk as sdk  # type: ignore

    mcp_server = build_mcp_server(cfg)
    events.append(cfg.events_file, "daemon_start", pid=os.getpid())
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text(str(os.getpid()))

    try:
        while RUNNING:
            if cfg.pause_flag.exists():
                await asyncio.sleep(cfg.tick_interval_s)
                continue
            await _tick(cfg, sdk, mcp_server)
            # Short sleep between ticks so we can shut down promptly.
            slept = 0
            while slept < cfg.tick_interval_s and RUNNING:
                await asyncio.sleep(1)
                slept += 1
    finally:
        events.append(cfg.events_file, "daemon_stop")
        try:
            cfg.pid_file.unlink()
        except OSError:
            pass


async def _tick(cfg: Config, sdk, mcp_server) -> None:
    # 1. Mattermost
    try:
        for msg in check_new_messages(cfg):
            await handle_message(cfg, sdk, mcp_server, msg)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "mm_poll_error", error=f"{type(e).__name__}: {e}")

    # 2. Cron
    try:
        jobs = load_jobs(cfg.cron_file)
        state = load_state(cfg.cron_state_file)
        for job in due_jobs(jobs, state, cfg.project_root):
            await run_cron(cfg, sdk, mcp_server, job)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "cron_error", error=f"{type(e).__name__}: {e}")

    # 3. Next Ready task
    try:
        board = Board.load(cfg.tasks_file)
        task = board.next_ready()
        if task:
            await run_task(cfg, sdk, task)
    except Exception as e:  # noqa: BLE001
        events.append(cfg.events_file, "task_error", error=f"{type(e).__name__}: {e}")


def _import_sdk_or_die() -> None:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print(
            "Error: claude-agent-sdk not installed. "
            "Install with: uv pip install claude-agent-sdk",
            file=sys.stderr,
        )
        sys.exit(1)


def run(project_root: str | None = None) -> None:
    cfg = Config.load(project_root)
    asyncio.run(main_loop(cfg))


if __name__ == "__main__":
    run()
