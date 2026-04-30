"""Paths, constants, and project configuration for autopilot v2.

All shared state lives under `.cc-autopilot/` (the v1 directory — v2 reuses it so
projects don't need a migration). Paths can be overridden by the project's
CLAUDE.md `## Autopilot` section.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


AUTOPILOT_DIR_NAME = ".cc-autopilot"
DEFAULT_TASKS_FILE = "TASKS.md"
DEFAULT_PROGRESS_FILE = f"{AUTOPILOT_DIR_NAME}/progress.md"
DEFAULT_TASKS_DIR = f"{AUTOPILOT_DIR_NAME}/tasks"
EVENTS_FILE = f"{AUTOPILOT_DIR_NAME}/events.jsonl"
CRON_FILE = f"{AUTOPILOT_DIR_NAME}/cron.yaml"
PID_FILE = f"{AUTOPILOT_DIR_NAME}/daemon.pid"
PAUSE_FLAG = f"{AUTOPILOT_DIR_NAME}/paused"
CRON_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/cron_state.json"
MM_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/mm_state.json"
RETRY_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/retry_state.json"
AUTO_DIAGNOSE_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/auto_diagnose_state.json"
ENV_FILE = f"{AUTOPILOT_DIR_NAME}/env"

DEFAULT_TICK_INTERVAL_S = 30
# TB-122: Mattermost polling runs in its own loop (`_mm_loop`) at a faster
# tempo than the main tick. The handler is operator-facing — pause / add /
# delete commands shouldn't sit behind a 30s tick when the cheap part of
# the work is just an HTTP poll.
DEFAULT_MM_TICK_INTERVAL_S = 10
DEFAULT_EVENT_CONTEXT_SIZE = 50
DEFAULT_TASK_TIMEOUT_S = 1200  # 20 min per SDK query
DEFAULT_CONTROL_TIMEOUT_S = 300  # 5 min for mattermost/cron agents
DEFAULT_MAX_RETRIES = 3
DEFAULT_VERIFY_TIMEOUT_S = 600  # 10 min for the project-wide verify gate
DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S = 10800  # 3h — TB-71 watchdog
DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S = 21600  # 6h — re-fire spam guard


@dataclass
class Config:
    """Resolved per-project configuration."""

    project_root: Path
    tasks_file: Path
    progress_file: Path
    tasks_dir: Path
    events_file: Path
    cron_file: Path
    pid_file: Path
    pause_flag: Path
    cron_state_file: Path
    mm_state_file: Path
    retry_state_file: Path
    auto_diagnose_state_file: Path
    next_task_id: int
    tick_interval_s: int
    mm_tick_interval_s: int
    event_context_size: int
    task_timeout_s: int
    control_timeout_s: int
    max_retries: int
    verify_cmd: str
    verify_timeout_s: int
    auto_diagnose_idle_threshold_s: int
    auto_diagnose_cooldown_s: int

    @classmethod
    def load(cls, project_root: str | Path | None = None) -> "Config":
        root = Path(project_root or os.getcwd()).resolve()
        load_project_env(root)
        autopilot_section = _read_autopilot_section(root / "CLAUDE.md")

        tasks_file = _resolve(root, autopilot_section.get("task_list"), DEFAULT_TASKS_FILE)
        progress_file = _resolve(
            root, autopilot_section.get("progress_log"), DEFAULT_PROGRESS_FILE
        )
        tasks_dir = _resolve(root, autopilot_section.get("task_briefings"), DEFAULT_TASKS_DIR)

        return cls(
            project_root=root,
            tasks_file=tasks_file,
            progress_file=progress_file,
            tasks_dir=tasks_dir,
            events_file=root / EVENTS_FILE,
            cron_file=root / CRON_FILE,
            pid_file=root / PID_FILE,
            pause_flag=root / PAUSE_FLAG,
            cron_state_file=root / CRON_STATE_FILE,
            mm_state_file=root / MM_STATE_FILE,
            retry_state_file=root / RETRY_STATE_FILE,
            auto_diagnose_state_file=root / AUTO_DIAGNOSE_STATE_FILE,
            next_task_id=autopilot_section.get("next_task_id", 1),
            tick_interval_s=int(os.environ.get("AP2_TICK_S", DEFAULT_TICK_INTERVAL_S)),
            mm_tick_interval_s=int(
                os.environ.get("AP2_MM_TICK_S", DEFAULT_MM_TICK_INTERVAL_S)
            ),
            event_context_size=int(
                os.environ.get("AP2_EVENT_CONTEXT", DEFAULT_EVENT_CONTEXT_SIZE)
            ),
            task_timeout_s=int(
                os.environ.get("AP2_TASK_TIMEOUT_S", DEFAULT_TASK_TIMEOUT_S)
            ),
            control_timeout_s=int(
                os.environ.get("AP2_CONTROL_TIMEOUT_S", DEFAULT_CONTROL_TIMEOUT_S)
            ),
            max_retries=int(os.environ.get("AP2_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            verify_cmd=os.environ.get("AP2_VERIFY_CMD", "").strip(),
            verify_timeout_s=int(
                os.environ.get("AP2_VERIFY_TIMEOUT_S", DEFAULT_VERIFY_TIMEOUT_S)
            ),
            auto_diagnose_idle_threshold_s=int(
                os.environ.get(
                    "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
                    DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S,
                )
            ),
            auto_diagnose_cooldown_s=int(
                os.environ.get(
                    "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
                    DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S,
                )
            ),
        )

    def ensure_dirs(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)


def load_project_env(project_root: Path) -> dict[str, str]:
    """Read `.cc-autopilot/env` (KEY=VALUE lines) and merge into `os.environ`.

    Existing env vars win — the file only fills in keys not already set, so a
    shell export still overrides the file (useful for one-off runs).
    Blank lines and `#`-comments are ignored. Values may be wrapped in single
    or double quotes. Returns the dict of keys that were actually applied.
    """
    env_file = project_root / ENV_FILE
    if not env_file.exists():
        return {}
    applied: dict[str, str] = {}
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = val
        applied[key] = val
    return applied


def _resolve(root: Path, configured: str | None, default: str) -> Path:
    p = Path(configured or default)
    return p if p.is_absolute() else root / p


def _read_autopilot_section(claude_md: Path) -> dict:
    """Parse the `## Autopilot` section of CLAUDE.md into a dict."""
    if not claude_md.exists():
        return {}
    text = claude_md.read_text()
    # `\b[^\n]*$` matches `## Autopilot` with or without trailing
    # disambiguators (e.g. `## Autopilot (per-project)`). Same brittleness
    # pattern as TB-91's verifier regex; eliminating proactively (TB-102).
    m = re.search(r"^##\s+Autopilot\b[^\n]*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return {}
    body = m.group(1)
    result: dict = {}
    for label, key in [
        ("Task list", "task_list"),
        ("Task briefings", "task_briefings"),
        ("Progress log", "progress_log"),
    ]:
        mm = re.search(rf"- {re.escape(label)}:\s*`?([^`\n]+?)`?\s*$", body, re.M)
        if mm:
            result[key] = mm.group(1).strip()
    mm = re.search(r"- Next task ID:\s*TB-(\d+)", body)
    if mm:
        result["next_task_id"] = int(mm.group(1))
    return result


def bump_next_task_id(claude_md: Path, new_next: int) -> None:
    """Update the `- Next task ID: TB-N` line in CLAUDE.md."""
    text = claude_md.read_text()
    new_text, n = re.subn(
        r"(- Next task ID:\s*TB-)(\d+)",
        lambda _: f"- Next task ID: TB-{new_next}",
        text,
    )
    if n:
        claude_md.write_text(new_text)
