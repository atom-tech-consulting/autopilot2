"""Tests for ap2.pipelines.is_blocking — pid:<N>@<TS> liveness check (TB-81)."""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from ap2 import pipelines


@pytest.fixture
def alive_proc():
    """Spawn `sleep 30`, yield (pid, started_at), reap on teardown."""
    proc = subprocess.Popen(
        ["sleep", "30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import psutil

        started_at = int(psutil.Process(proc.pid).create_time())
    except Exception:
        started_at = int(time.time())
    try:
        yield proc.pid, started_at
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)


def test_alive_with_matching_ts_is_blocking(alive_proc):
    pid, ts = alive_proc
    assert pipelines.is_blocking(f"pid:{pid}@{ts}") is True


def test_alive_without_ts_is_blocking(alive_proc):
    pid, _ = alive_proc
    # Old format / TS-less blocker: alive PID is enough to block.
    assert pipelines.is_blocking(f"pid:{pid}") is True


def test_dead_process_is_unblocked():
    proc = subprocess.Popen(
        ["true"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=5)
    # Reap zombie via wait() above; PID slot may be recycled but kill(0) will
    # raise ProcessLookupError as long as the slot isn't immediately reused.
    pid = proc.pid
    # Loop a tiny bit to make sure the kernel has fully torn down the entry.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    assert pipelines.is_blocking(f"pid:{pid}@{int(time.time())}") is False


def test_recycled_pid_is_unblocked(alive_proc, monkeypatch):
    """Live PID, but recorded TS is far older than the OS-reported create_time
    — that's PID recycling; the original pipeline is gone, so unblock.
    """
    pid, ts = alive_proc
    # Pretend the recorded start was much earlier than the actual one.
    stale_ts = ts - 3600
    assert pipelines.is_blocking(f"pid:{pid}@{stale_ts}") is False


def test_tolerance_window_treats_minor_drift_as_blocking(alive_proc):
    """Within ±_RECYCLE_TOLERANCE_S of the OS create_time, still blocking."""
    pid, ts = alive_proc
    # 1-second drift is well inside the 5s tolerance.
    assert pipelines.is_blocking(f"pid:{pid}@{ts - 1}") is True


def test_malformed_blocker_is_unblocked():
    assert pipelines.is_blocking("pid:") is False
    assert pipelines.is_blocking("pid:notanint") is False
    assert pipelines.is_blocking("pid:123@notanint") is False
    assert pipelines.is_blocking("garbage") is False
    assert pipelines.is_blocking("") is False


def test_zombie_process_is_unblocked():
    """Zombie = child exited but parent (this test process) hasn't reaped.
    `os.kill(pid, 0)` still succeeds, but the work is done — is_blocking
    must consult psutil status and treat zombies as unblocked, otherwise a
    detached pipeline that finished but wasn't waited on would block its
    validation task forever.
    """
    proc = subprocess.Popen(
        ["true"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid

    import psutil

    # Wait for the child to exit AND become a zombie (no .wait() call here).
    deadline = time.time() + 2.0
    saw_zombie = False
    while time.time() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                saw_zombie = True
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)

    if saw_zombie:
        # `os.kill(pid, 0)` succeeds — entry still in process table.
        os.kill(pid, 0)
        # But is_blocking knows it's done.
        assert pipelines.is_blocking(f"pid:{pid}@{int(time.time())}") is False
    # Cleanup: reap so we don't leak a zombie out of the test.
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def test_permission_error_is_unblocked(monkeypatch):
    """If the PID exists but belongs to another user (kill(0) raises EPERM),
    almost always means a recycled PID owned by root/system. Treat as
    unblocked rather than wedging the validation task forever.
    """
    def fake_kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", fake_kill)
    assert pipelines.is_blocking("pid:99999@1700000000") is False
