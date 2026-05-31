"""Per-task retry state backed by `.cc-autopilot/retry_state.json`.

Used by the daemon to bound how many times a failing task is re-attempted before
it gets shelved to Frozen for human review.

TB-356 adds a second per-task quantity to the SAME file: an effort-downshift
level (keyed under a reserved `<task_id>::downshift` suffix so it never
collides with the bare `<task_id>` attempt-counter entry). It drives graceful
degradation for the bundled-CLI thinking-block-immutability 400 failure class
— see `bump_downshift` / `downshift_level` below and `daemon._step_down_effort`.
"""
from __future__ import annotations

import json
from pathlib import Path

from ap2._shared import locked_sidecar


def _load(state_file: Path) -> dict[str, int]:
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _save(state_file: Path, state: dict[str, int]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True))


def attempt_count(state_file: Path, task_id: str) -> int:
    return _load(state_file).get(task_id, 0)


def bump_attempt(state_file: Path, task_id: str) -> int:
    """Increment the attempt counter for `task_id` and return the new value."""
    with locked_sidecar(state_file):
        state = _load(state_file)
        state[task_id] = state.get(task_id, 0) + 1
        _save(state_file, state)
        return state[task_id]


def reset_attempt(state_file: Path, task_id: str) -> None:
    """Clear ALL per-task retry state for `task_id` on success.

    TB-356: this now drops both the attempt counter (the bare `task_id`
    key) AND the effort-downshift level (the `<task_id>::downshift` key),
    so a clean run wipes the slate — a later, unrelated failure starts
    from full effort with a zeroed counter.
    """
    with locked_sidecar(state_file):
        state = _load(state_file)
        changed = False
        for key in (task_id, task_id + _DOWNSHIFT_SUFFIX):
            if key in state:
                del state[key]
                changed = True
        if changed:
            _save(state_file, state)


# ---------------------------------------------------------------------------
# TB-356: per-task effort-downshift level.
#
# Persisted alongside the attempt counter in the SAME `retry_state.json`,
# under a reserved-suffix key (`<task_id>::downshift`) so it never collides
# with the bare `<task_id>` attempt-counter entry. The daemon bumps it ONLY
# when a failure classifies as the bundled-CLI thinking-block-immutability
# 400 (`daemon._is_thinking_block_corruption`); every other failure class
# leaves it untouched. `reset_attempt` clears it on success. The level drives
# `daemon._step_down_effort(base, level)` at the next task dispatch
# (xhigh→high→medium→low, floored), so a task that repeatedly trips the bug
# walks its effort down to a tier whose thinking blocks are small enough to
# avoid the corruption path.
# ---------------------------------------------------------------------------
_DOWNSHIFT_SUFFIX = "::downshift"


def downshift_level(state_file: Path, task_id: str) -> int:
    """Current effort-downshift level for `task_id` (0 = base effort)."""
    return _load(state_file).get(task_id + _DOWNSHIFT_SUFFIX, 0)


def bump_downshift(state_file: Path, task_id: str) -> int:
    """Increment the downshift level for `task_id`; return the new value."""
    with locked_sidecar(state_file):
        state = _load(state_file)
        key = task_id + _DOWNSHIFT_SUFFIX
        state[key] = state.get(key, 0) + 1
        _save(state_file, state)
        return state[key]


def reset_downshift(state_file: Path, task_id: str) -> None:
    """Clear just the effort-downshift level for `task_id` (leaves the
    attempt counter intact). Not on the daemon's hot path — `reset_attempt`
    already clears both on success — but exposed for symmetry / tooling."""
    with locked_sidecar(state_file):
        state = _load(state_file)
        key = task_id + _DOWNSHIFT_SUFFIX
        if key in state:
            del state[key]
            _save(state_file, state)
