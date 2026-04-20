"""Per-task retry counter backed by `.cc-autopilot/retry_state.json`.

Used by the daemon to bound how many times a failing task is re-attempted before
it gets shelved to Frozen for human review.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    with _locked(state_file):
        state = _load(state_file)
        state[task_id] = state.get(task_id, 0) + 1
        _save(state_file, state)
        return state[task_id]


def reset_attempt(state_file: Path, task_id: str) -> None:
    with _locked(state_file):
        state = _load(state_file)
        if task_id in state:
            del state[task_id]
            _save(state_file, state)
