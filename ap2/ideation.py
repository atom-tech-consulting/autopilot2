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
- Cooldown: `AP2_IDEATION_COOLDOWN_S` (default 7200 — 2h).
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
from .cron import load_state, mark_run


IDEATION_NAME = "ideation"
IDEATION_MAX_TURNS_DEFAULT = 30
IDEATION_COOLDOWN_DEFAULT_S = 7200  # 2h between fires when board stays empty

_DEFAULT_PROMPT_PATH = Path(__file__).parent / "ideation.default.md"
_PROJECT_PROMPT_REL = ".cc-autopilot/ideation_prompt.md"


def load_prompt(cfg: Config) -> str:
    """Return the ideation prompt — project override if present, else default."""
    override = cfg.project_root / _PROJECT_PROMPT_REL
    if override.is_file():
        return override.read_text()
    return _DEFAULT_PROMPT_PATH.read_text()


def _cooldown_s() -> int:
    """Effective cooldown (seconds), env-overridable."""
    v = os.environ.get("AP2_IDEATION_COOLDOWN_S")
    if v:
        try:
            return int(v)
        except ValueError:
            pass
    return IDEATION_COOLDOWN_DEFAULT_S


async def _maybe_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Fire ideation when the board is fully empty and the cooldown elapsed.

    Reuses `daemon._run_control_agent` for SDK plumbing (prompt-dump,
    stderr capture, MCP wiring) but owns its own event vocabulary —
    `ideation_empty_board` on entry, `ideation_error` / `ideation_timeout`
    on failure, and the agent's own `ideation_complete` log_event call as
    the success-end marker. Cooldown is still tracked under the
    `ideation` key in cron_state.json so the TB-95 migration from the
    cron-yaml-driven design is unaffected.

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
    # Refresh the insights index — ideation Step 0.5 reads
    # `.cc-autopilot/insights/_index.md` for grounding (TB-89). Lazy:
    # no-op when nothing changed. A failure here must NOT block the run.
    try:
        from . import insights

        insights.maybe_regenerate_index(cfg)
    except Exception:  # noqa: BLE001
        pass
    # Lazy imports to avoid daemon ↔ ideation circular dependency.
    from . import daemon as _daemon
    from . import prompts
    from .tools import CONTROL_AGENT_TOOLS

    full_prompt = prompts.build_control_prompt(cfg, IDEATION_NAME, load_prompt(cfg))
    max_turns = int(os.environ.get("AP2_IDEATION_MAX_TURNS", IDEATION_MAX_TURNS_DEFAULT))
    # TB-126: snapshot the state surface before ideation runs so the post-
    # run state commit only stages paths ideation actually touched (new
    # briefings, ideation_state.md, TASKS.md / CLAUDE.md from add_backlog,
    # any insights). Briefings already in the working tree from a prior op
    # do NOT ride along.
    pre_snapshot = _daemon._snapshot_state_paths(cfg)
    timed_out, error, stderr_tail, prompt_dump = await _daemon._run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label="ideation",
        prompt=full_prompt,
        allowed_tools=CONTROL_AGENT_TOOLS,
        max_turns=max_turns,
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "ideation_timeout",
            timeout_s=cfg.control_timeout_s,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "ideation_error",
            error=error,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    # Always advance the cooldown — even on failure — so a broken
    # ideation agent doesn't get hammered every tick.
    mark_run(cfg.cron_state_file, IDEATION_NAME)
    touched = _daemon._changed_state_paths(
        pre_snapshot, _daemon._snapshot_state_paths(cfg)
    )
    if touched:
        _daemon._commit_state_files(cfg, "state: ideation", paths=touched)
