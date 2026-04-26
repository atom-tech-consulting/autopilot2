"""Fixtures for ap2 e2e tests."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from ap2 import cron as cron_mod
from ap2.board import Board
from ap2.config import Config


@pytest.fixture
def e2e_project(tmp_path: Path, monkeypatch) -> Callable[..., Config]:
    """Factory fixture: build a ready-to-tick project under tmp_path.

    Optional keyword arguments:
      ready_task:  (id, title) to seed in Ready.
      frozen_task: (id, title, blocked_on) to seed in Frozen.
      cron_jobs:   list of dicts passed straight to the cron.yaml `jobs:` list.

    Env is scrubbed so mattermost is opt-in per test. Returns the loaded
    `Config` with `ensure_dirs()` already called.
    """

    # Scrub mattermost env unless a test explicitly sets it.
    for k in (
        "AP2_MM_CHANNELS",
        "MATTERMOST_URL",
        "MATTERMOST_TOKEN",
        "AP2_MM_BOT_USER_ID",
        "AP2_MM_MENTION",
        # Scrub verify-gate env so unrelated e2e tests never accidentally run
        # the project-wide gate against a tmp_path that has no test target.
        "AP2_VERIFY_CMD",
        "AP2_VERIFY_TIMEOUT_S",
        # Scrub watchdog env so unrelated e2e tests don't fire diagnose posts.
        "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
        "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
    ):
        monkeypatch.delenv(k, raising=False)

    # Low retry/timeout so a misbehaving test fails fast rather than hanging.
    monkeypatch.setenv("AP2_TASK_TIMEOUT_S", "30")
    monkeypatch.setenv("AP2_CONTROL_TIMEOUT_S", "30")
    monkeypatch.setenv("AP2_MAX_RETRIES", "3")

    def build(
        *,
        ready_task: tuple[str, str] | None = None,
        frozen_task: tuple[str, str, str] | None = None,
        cron_jobs: list[dict] | None = None,
    ) -> Config:
        (tmp_path / "TASKS.md").write_text(
            "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
        )
        (tmp_path / "CLAUDE.md").write_text(
            "## Autopilot\n\n"
            "- Task list: `TASKS.md`\n"
            "- Next task ID: TB-10\n"
        )

        cfg = Config.load(tmp_path)
        cfg.ensure_dirs()

        # Seed board state via the real Board API so renders/ids match prod.
        board = Board.load(cfg.tasks_file)
        if ready_task:
            tid, title = ready_task
            board.add("Ready", task_id=tid, title=title)
        if frozen_task:
            tid, title, blocker = frozen_task
            board.add(
                "Frozen",
                task_id=tid,
                title=title,
                description=f"(blocked on: {blocker})",
            )
        if ready_task or frozen_task:
            board.save()

        if cron_jobs:
            jobs = [cron_mod.CronJob.from_dict(j) for j in cron_jobs]
            cron_mod.save_jobs(cfg.cron_file, jobs)

        return cfg

    return build


@pytest.fixture
def clock(monkeypatch):
    """Advance a fake wall clock used by `ap2.cron`.

    Returns a callable: `advance(seconds)` bumps the clock; `advance.now()`
    reads the current value.
    """
    import time as _real_time

    state = {"t": _real_time.time()}

    def now() -> float:
        return state["t"]

    monkeypatch.setattr("ap2.cron.time.time", now)

    def advance(seconds: float) -> float:
        state["t"] += seconds
        return state["t"]

    advance.now = lambda: state["t"]  # type: ignore[attr-defined]
    return advance
