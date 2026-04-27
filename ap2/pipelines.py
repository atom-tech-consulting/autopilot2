"""Pipeline-blocker helpers (TB-81).

A `pid:<N>@<TS>` blocker is satisfied iff EITHER the process at PID N no
longer exists, OR its OS-reported `create_time` has shifted from the recorded
TS (PID was recycled to an unrelated process). The recycling check is what
keeps a long-lived daemon's validation task from auto-unblocking the moment
the kernel hands the dead pipeline's PID to a fresh shell.
"""
from __future__ import annotations

import os
import re

_PID_BLOCKER_RE = re.compile(r"^pid:(\d+)(?:@(\d+))?$")

# Tolerance window for create_time comparison. psutil reports seconds-since-epoch
# from the kernel; our recorded TS is the int(time) we captured in the same
# second. Anything beyond a few seconds of drift means the kernel handed the
# PID to a fresh process — that's PID recycling, treat as unblocked.
_RECYCLE_TOLERANCE_S = 5


def is_blocking(blocker: str) -> bool:
    """Return True iff the `pid:<N>@<TS>` blocker should still gate dispatch.

    Returns False (unblocked) on:
      - malformed token
      - PID gone (`os.kill(pid, 0)` raises ProcessLookupError)
      - PID exists but the recorded create_time differs (recycled PID)
      - PermissionError — almost always means the PID was recycled to a
        process owned by another user (e.g. root); treat as unblocked

    Returns True (still blocking) when the process is alive and either no TS
    was recorded, or the recorded TS matches the OS-reported create_time
    within `_RECYCLE_TOLERANCE_S`. If psutil isn't importable, we degrade
    gracefully: alive PID = blocking.
    """
    m = _PID_BLOCKER_RE.match(blocker)
    if not m:
        return False
    pid = int(m.group(1))
    recorded_ts = int(m.group(2)) if m.group(2) else None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False

    try:
        import psutil

        p = psutil.Process(pid)
        # Zombie = child process that exited but hasn't been reaped by its
        # parent. `os.kill(pid, 0)` still succeeds for zombies (the entry is
        # in the process table), but the work is done — treat as unblocked
        # so the validation task can run. This is the common case for the
        # daemon spawning detached pipelines and then forgetting the Popen
        # handle.
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        if recorded_ts is not None:
            current_ts = int(p.create_time())
            if abs(current_ts - recorded_ts) > _RECYCLE_TOLERANCE_S:
                return False
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        # Process metadata read failed (NoSuchProcess if it died between
        # `os.kill` and now, etc.). Be conservative — fall through to
        # "still blocking" so we don't promote on transient errors.
        pass

    return True
