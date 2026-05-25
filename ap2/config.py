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
# TB-260: per-daemon-lifetime runtime-introspection facts (currently
# `env_file_mtime_at_start` for the `.cc-autopilot/env` stale-detection
# surface). Separate from `auto_diagnose_state.json` because that file
# is dedicated to watchdog-cooldown bookkeeping; this one captures
# "facts pinned at daemon start, valid until daemon stop" so the CLI's
# `cmd_status` (a separate process) can compare current env mtime to
# the daemon's start-mtime without going through the daemon's PID.
DAEMON_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/daemon_state.json"
ENV_FILE = f"{AUTOPILOT_DIR_NAME}/env"

DEFAULT_TICK_INTERVAL_S = 30
# TB-122: Mattermost polling runs in its own loop (`_mm_loop`) at a faster
# tempo than the main tick. The handler is operator-facing — pause / add /
# delete commands shouldn't sit behind a 30s tick when the cheap part of
# the work is just an HTTP poll.
DEFAULT_MM_TICK_INTERVAL_S = 10
DEFAULT_EVENT_CONTEXT_SIZE = 50
DEFAULT_TASK_TIMEOUT_S = 1200  # 20 min per SDK query
# TB-278: bumped from 300s (5 min) to 1200s (20 min) — ideation / mattermost /
# cron agents under `xhigh` effort against a populated progress.md /
# operator_log.md / ideation_state.md routinely blew the old 5-min wall.
# This project's own `.cc-autopilot/env` overrides to 1800s; the bumped
# default just spares fresh projects from rediscovering the same ceiling.
DEFAULT_CONTROL_TIMEOUT_S = 1200  # 20 min for mattermost/cron agents
DEFAULT_MAX_RETRIES = 3
DEFAULT_VERIFY_TIMEOUT_S = 600  # 10 min for the project-wide verify gate
DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S = 10800  # 3h — TB-71 watchdog
DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S = 21600  # 6h — re-fire spam guard

# TB-282: proactive attention-raised detector knobs.
# `AP2_TASK_STUCK_THRESHOLD_S` defaults to 4h — long enough to skip a
# long-but-healthy task agent (TB-122/TB-255 pattern: real-world tasks
# at xhigh effort can sit 30-60 min inside `sdk.query` without being
# stuck), short enough that an actually-hung dispatch surfaces well
# before the next 2h status-report cron tick. `AP2_ATTENTION_DEBOUNCE_S`
# defaults to 6h so a still-stuck task re-fires roughly once per
# operator workday rather than every tick. Both knobs are read fresh
# from `os.environ` at detection-time inside `ap2/attention.py`
# (`_task_stuck_threshold_s` / `_attention_debounce_s`) and listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so an operator tightening either
# floor takes effect on the next tick without a daemon restart — they
# tune detection sensitivity, not lifecycle.
DEFAULT_TASK_STUCK_THRESHOLD_S = 14400  # 4h
DEFAULT_ATTENTION_DEBOUNCE_S = 21600  # 6h

# TB-278: max-turn caps promoted to named constants alongside the
# DEFAULT_*_TIMEOUT_S family above so every battle-tested default sits in
# one discoverable place. Defaults raised from the old inline literals
# (task 50, ideation 30) to values this project's `.cc-autopilot/env`
# already validated — TB-122 hit `error_max_turns` at 51 turns on a task,
# and a 2026-05-12 manual ideate hit 31 turns mid-goal-rewrite. Fresh
# projects start from those lessons rather than rediscovering the walls.
# DEFAULT_CONTROL_MAX_TURNS keeps its current value (15) — listed here for
# consistency so the env-template scaffold can document a single source
# of truth for every max-turn knob.
DEFAULT_TASK_MAX_TURNS = 200
DEFAULT_CONTROL_MAX_TURNS = 15
DEFAULT_IDEATION_MAX_TURNS = 100

# TB-284: model for `ap2/ideation_scrub.py`'s post-write filter that
# strips exhaustion-asserting sentences from `ideation_state.md` after
# each ideation cycle. Haiku-4.5 is the cost-target floor — sentence-
# level classification, not deep reasoning. Operator override:
# `AP2_IDEATION_SCRUB_MODEL`. Listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so an operator swapping the scrub
# model takes effect on the next ideation tick without a daemon
# restart. The runtime reads `AP2_IDEATION_SCRUB_MODEL` fresh from
# `os.environ` inside `ideation_scrub._resolved_model()` at call-time
# (parallel to `AP2_AGENT_MODEL`'s wiring), so this default lives
# here for discoverability — `Config.load` does NOT stash it on the
# dataclass because the call-site read is the source of truth.
DEFAULT_IDEATION_SCRUB_MODEL = "claude-haiku-4-5-20251001"


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
    # TB-260: stash for `env_file_mtime_at_start` (and any future
    # daemon-lifetime introspection facts) so the CLI's `cmd_status`
    # can compare the live env file mtime against the value captured
    # at daemon start without going through the daemon's PID.
    daemon_state_file: Path
    # TB-260: the `.cc-autopilot/env` source-of-truth path. Surfaced as
    # a Config attribute (not just the `ENV_FILE` module constant) so
    # both startup-capture (in `daemon._emit_daemon_start`) and the
    # cmd_status / status_report / diagnose stale-detection paths read
    # one canonical attribute — a refactor that moves the env file
    # ripples through the dataclass instead of every call site.
    env_file: Path
    next_task_id: int
    # TB-280: operator-facing project identity. Leads every status-
    # report Mattermost headline (`**[<project_name>] Autopilot Status
    # Report** — <now>`) so a multi-project operator monitoring 5+
    # daemons can identify a post's source project without alt-tabbing
    # to the repo. Default is `project_root.name`; override via
    # `AP2_PROJECT_NAME`. Surfaced on `Config` (not on a Routine-scoped
    # struct) so the same field is available to web home, `ap2 status`,
    # and any future push surface that wants to prefix the identity
    # uniformly.
    project_name: str
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
        applied = load_project_env(root)
        # TB-271: seed the env-reload tracker with the set of keys the
        # startup pass actually wrote into os.environ. The reload helper
        # uses this set to honor "shell export wins" on later ticks —
        # keys never file-sourced at startup keep shell-export precedence
        # even if the operator later adds them to the env file. Lazy
        # import to avoid the config↔env_reload module cycle (env_reload
        # imports Config for type signatures + defaults).
        from .env_reload import note_initial_applied
        note_initial_applied(root, applied)
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
            # TB-260: daemon-lifetime state stash (env_file_mtime_at_start).
            daemon_state_file=root / DAEMON_STATE_FILE,
            env_file=root / ENV_FILE,
            next_task_id=autopilot_section.get("next_task_id", 1),
            # TB-280: project identity for status-report headline. Env
            # override wins over the `project_root.name` default so a
            # daemon hosting the project under a generic-named root
            # (`/tmp/proj`, `/home/user/code/main`) can still post with
            # an operator-meaningful identifier. Whitespace-stripped so
            # an accidental `AP2_PROJECT_NAME=" foo"` doesn't render a
            # leading space in the bracketed headline.
            project_name=(
                os.environ.get("AP2_PROJECT_NAME", "").strip()
                or root.name
            ),
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
