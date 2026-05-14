"""Internal helpers shared across `ap2/` modules.

Currently exposes two fcntl-based file-locking context managers. They share
99% of their body but differ in *what* file the lock fd points at — a
distinction with real on-disk consequences:

- `locked_inplace(path)` opens an fd on `path` itself and holds the
  exclusive flock on that fd. Use this when the file you want to lock IS
  the file you want serialized access to. The caller is responsible for
  not truncating or replacing `path` under the lock — doing so would
  invalidate the fd-bound lock for any future opener.

- `locked_sidecar(path)` opens an fd on a sibling
  `path.with_suffix(path.suffix + ".lock")` file instead, and locks
  *that*. The locked file (`path`) itself is never opened by the helper,
  so the body of the `with` block is free to rewrite/truncate/replace
  `path` atomically (e.g. write-to-temp + os.replace) without disturbing
  the lock. Use this when the protected resource is a file you mutate
  via whole-file rewrite under the lock — `.cc-autopilot/cron.yaml`,
  `.cc-autopilot/retry_state.json`, etc.

Both helpers create parent directories of the lock-fd path with
`mkdir(parents=True, exist_ok=True)` before opening, so the lock file
materialises on first use without a separate setup step.

Two named functions (not one helper with a `sidecar=` flag) because the
two locking modes are semantically distinct and every current caller picks
one variant and sticks with it; forcing the choice at the import site
makes the distinction visible at the call site.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def locked_inplace(path: Path) -> Iterator[int]:
    """Acquire an exclusive fcntl lock on `path` itself.

    Opens (creating if absent) an fd on `path` and holds `LOCK_EX` on it
    for the duration of the `with` block. The caller must not truncate
    or replace `path` under the lock — see module docstring for the
    sidecar variant when whole-file rewrite is needed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def locked_sidecar(path: Path) -> Iterator[int]:
    """Acquire an exclusive fcntl lock on a `.lock` sidecar next to `path`.

    Opens (creating if absent) an fd on `path.with_suffix(path.suffix +
    ".lock")` and holds `LOCK_EX` on that. `path` itself is never opened
    by this helper, so the `with` body is free to truncate, rewrite, or
    atomic-replace `path` without invalidating the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
