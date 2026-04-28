"""Ideation: a first-class autopilot mechanism, not a cron job.

Ideation fires when the working board (Active+Ready+Backlog) is fully empty,
throttled by a per-project cooldown. Its prompt instructs the agent to
propose new tasks based on goal.md, TASKS.md, progress.md, the insights
index, and recent failures (see `ideation.default.md`).

Why a dedicated module rather than a cron job: ideation is the only
mechanism that creates new work, so it needs to evolve faster than the
generic cron infrastructure — its prompt structure (assessment, failure
review, insights grounding, two-tier verification) is load-bearing and
changes often. Splitting it out also lets projects override just the
prompt without touching cron.yaml.

Configuration:
- Default prompt: `ap2/ideation.default.md` shipped with the package.
- Project override (optional): `.cc-autopilot/ideation_prompt.md` in the
  project root — when present, it replaces the default verbatim.
- Cooldown: `AP2_IDEATION_COOLDOWN_S` (default 3600), with the legacy
  `AP2_EMPTY_BOARD_IDEATION_COOLDOWN_S` and intermediate
  `AP2_EMPTY_BOARD_COOLDOWN_S` env vars honored as fallbacks.
- Max turns: `AP2_IDEATION_MAX_TURNS` (default 30 — bumped from the legacy
  cron-default 15 because the assessment + failure-review + proposal flow
  routinely needs ~10-15 turns and 15 was running close to the wire).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import events
from .board import Board
from .config import Config
from .cron import CronJob, load_state


IDEATION_NAME = "ideation"
IDEATION_MAX_TURNS_DEFAULT = 30

_DEFAULT_PROMPT_PATH = Path(__file__).parent / "ideation.default.md"
_PROJECT_PROMPT_REL = ".cc-autopilot/ideation_prompt.md"


def load_prompt(cfg: Config) -> str:
    """Return the ideation prompt — project override if present, else default."""
    override = cfg.project_root / _PROJECT_PROMPT_REL
    if override.is_file():
        return override.read_text()
    return _DEFAULT_PROMPT_PATH.read_text()


def _cooldown_s() -> int:
    """Effective cooldown (seconds). Honors the new env var first, then the
    transitional name from the empty-board generalization, then the legacy
    name from the original empty-board ideation hook."""
    for var in (
        "AP2_IDEATION_COOLDOWN_S",
        "AP2_EMPTY_BOARD_COOLDOWN_S",
        "AP2_EMPTY_BOARD_IDEATION_COOLDOWN_S",
    ):
        v = os.environ.get(var)
        if v:
            try:
                return int(v)
            except ValueError:
                continue
    return 3600


async def _maybe_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Fire ideation when the board is fully empty and the cooldown elapsed.

    Reuses `daemon.run_cron` so prompt-dump, stderr capture, and event
    logging are identical to scheduled cron jobs. The job name is
    `ideation`, so cron_state.json's `ideation` key tracks the cooldown
    just as it did under the old cron-yaml-driven design (and migrations
    from that design preserve the cooldown across the cutover).

    Set `AP2_IDEATION_DISABLED=1` to opt out entirely (the tests use this
    by default; it's also useful for projects that want to drive ideation
    manually rather than on empty-board).
    """
    if os.environ.get("AP2_IDEATION_DISABLED", "").strip() in ("1", "true", "yes"):
        return
    board = Board.load(cfg.tasks_file)
    has_work = any(
        next(board.iter_tasks(section=s), None) is not None
        for s in ("Active", "Ready", "Backlog")
    )
    if has_work:
        return
    state = load_state(cfg.cron_state_file)
    last = state.get(IDEATION_NAME, 0.0)
    cooldown = _cooldown_s()
    now = time.time()
    if now - last < cooldown:
        return
    events.append(
        cfg.events_file,
        "ideation_empty_board",
        cooldown_s=cooldown,
        seconds_since_last=int(now - last) if last else None,
    )
    job = CronJob(
        name=IDEATION_NAME,
        interval_s=0,  # not on a schedule — this code path is the only firer
        prompt=load_prompt(cfg),
        max_turns=int(os.environ.get("AP2_IDEATION_MAX_TURNS", IDEATION_MAX_TURNS_DEFAULT)),
    )
    # Lazy import to avoid daemon ↔ ideation circular dependency at module load.
    from . import daemon as _daemon

    await _daemon.run_cron(cfg, sdk, mcp_server, job)
