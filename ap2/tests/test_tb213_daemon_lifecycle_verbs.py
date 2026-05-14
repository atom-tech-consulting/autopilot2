"""TB-213: happy + error path coverage for the four daemon-lifecycle CLI
verbs that TB-209's `test_coverage_drift.py` docstring (prior to this
change, L407-408 + L417-418 of the comment-block shim) tags as TB-205-shape
coverage debt.

The four verbs — `ap2 pause`, `ap2 resume`, `ap2 stop`, `ap2 unfreeze` —
are the operator's primary daemon-control surface, but prior to TB-213 had
ZERO real test references — only the substring drift gate's comment-block
enumeration kept the gate green. A future refactor of `cli.cmd_pause` /
`cli.cmd_resume` / `cli.cmd_stop` / `cli.cmd_unfreeze` (or the
daemon-control IPC they wrap) could silently break the operator UX while
the drift gate stays green via the shim.

This module mirrors TB-205's `test_env_knobs.py` / TB-210's
`test_tb210_env_knobs.py` shape on the CLI-verb axis: per-verb default
behavior + override + at-least-one error-path test, calls go through the
public CLI handler (`cli.cmd_<verb>`) rather than reaching into
implementation internals, side-effects asserted on filesystem state +
events.

  1. `ap2 pause`    — `cli.cmd_pause`: writes `cfg.pause_flag` with the
                      operator-supplied `--reason`, emits a `daemon_pause`
                      event. The handler has no real error branch; the
                      "non-happy" path pinned here is idempotency
                      (re-pausing overrides the prior reason, doesn't
                      crash). Pause is deliberately decoupled from daemon
                      liveness — operators can pre-pause before `ap2 start`
                      and the daemon honors the flag at first tick.

  2. `ap2 resume`   — `cli.cmd_resume`: unlinks `cfg.pause_flag` if
                      present, emits `daemon_resume`. Error-path:
                      resume-when-not-paused is intentionally a no-op
                      (no exception, event still emitted) — operators
                      treat the verb as a "force-resume" idempotent
                      escape hatch. Pinning the no-op behavior catches
                      a refactor that adds a "refuse if not paused"
                      check (which would silently break the recovery
                      flow when the flag file is stale-deleted).

  3. `ap2 stop`     — `cli.cmd_stop`: reads pid from `cfg.pid_file`, sends
                      SIGTERM (or SIGKILL with `-f/--force`) to the
                      running daemon. Happy path: live pid → signal sent.
                      Error paths: missing pid file → prints "not running"
                      and returns 0; stale pid file (pid not running) →
                      cleans up the stale file AND prints "not running".

  4. `ap2 unfreeze` — `cli.cmd_unfreeze`: routes through
                      `tools.do_operator_queue_append` (TB-131) with
                      `op="unfreeze"`. Happy path: Frozen TB-N is queued;
                      drain moves it to Backlog and resets the retry
                      counter. Error paths: non-Frozen TB-N (returns 1,
                      stderr names the section + nudges to `ap2 backlog`);
                      unknown TB-N (returns 1, stderr says "not on
                      board"). The existing `test_cli.py::test_unfreeze_*`
                      tests cover this handler's full lifecycle, but
                      none of those tests carry the literal substring
                      `"ap2 unfreeze"` — the drift gate's `name in blob`
                      check matches the verb name, not the handler
                      symbol, so this module adds the literal-string
                      reference alongside fresh focused tests.

Test-function names follow the convention `test_cmd_<verb>_<aspect>`
(e.g. `test_cmd_pause_happy_path`, `test_cmd_pause_daemon_not_running`).
The auto-verifier bullets in the briefing grep for
`def test_cmd_(pause|resume|stop|unfreeze)` across this file and
`test_cli.py`; the minimum is ≥4 happy-path tests (one per verb), which
this module satisfies on its own.

Removing the four matching rows from `test_coverage_drift.py`'s
discovered-at-landing comment block (`#   - ap2 pause` / `resume` /
`stop` / `unfreeze`) is paired with this file landing — the
comment-block shim was a "test mention waiting to happen" entry,
redundant once a real test references the verb name. The 8 sandbox
rows (install-* + audit/setup) stay in the shim until sibling TBs
close them.

CLI-verb substring pins (one per verb, kept here so the drift gate's
`name in blob` resolves against THIS module rather than the deleted
comment-block shim):

    "ap2 pause", "ap2 resume", "ap2 stop", "ap2 unfreeze"
"""
from __future__ import annotations

import os
import signal
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from ap2 import events, retry, tools
from ap2.board import Board
from ap2.cli import (
    cmd_pause,
    cmd_resume,
    cmd_stop,
    cmd_unfreeze,
)
from ap2.config import Config
from ap2.init import init_project


# ---------------------------------------------------------------------------
# Shared fixture: a minimal ap2 project with init_project() scaffolding.
# Mirrors test_cli.py::_project — the daemon-control handlers all need a
# real Config (events_file, pause_flag, pid_file, tasks_file).
# ---------------------------------------------------------------------------


def _project(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drain(cfg: Config) -> dict:
    """Apply pending operator-queue ops as the daemon's `_tick` would.

    `cmd_unfreeze` (TB-131) stages the unfreeze op via
    `tools.do_operator_queue_append`; the actual board move + retry-reset
    happens on the daemon's next drain step. Tests that assert on
    post-drain state must call this helper after the CLI invocation.
    """
    return tools.drain_operator_queue(cfg)


# ===========================================================================
# (1) `ap2 pause` — `cli.cmd_pause`
#
# Handler at `ap2/cli.py:1383-1388`:
#     cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
#     cfg.pause_flag.write_text((args.reason or "") + "\n")
#     events.append(cfg.events_file, "daemon_pause", reason=args.reason or "")
#     print("paused (flag written)")
#     return 0
#
# Pause is a flag-only operation; no daemon-liveness check, no IPC. The
# daemon honors the flag at its next tick. We pin the flag content + the
# emitted event + the return code.
# ===========================================================================


def test_cmd_pause_happy_path(tmp_path: Path, capsys):
    """`ap2 pause --reason maintenance` writes the pause flag with the
    reason and emits a `daemon_pause` event. Pins the public contract:
    flag content carries the operator's reason, event records it for the
    audit trail, return code is 0, stdout confirms the write."""
    cfg = _project(tmp_path)
    rc = cmd_pause(cfg, Namespace(reason="maintenance"))

    assert rc == 0
    assert cfg.pause_flag.exists()
    assert cfg.pause_flag.read_text() == "maintenance\n"
    assert "paused" in capsys.readouterr().out

    evts = events.tail(cfg.events_file, 5)
    pauses = [e for e in evts if e["type"] == "daemon_pause"]
    assert len(pauses) == 1
    assert pauses[0]["reason"] == "maintenance"


def test_cmd_pause_no_reason_writes_empty_flag(tmp_path: Path):
    """`ap2 pause` with no `--reason` (argparse default `""`): flag is
    written but contains only the trailing newline; the emitted event
    carries `reason=""`. Pins the empty-reason default branch — a
    refactor that switches to "reason required" would flip this test."""
    cfg = _project(tmp_path)
    rc = cmd_pause(cfg, Namespace(reason=""))

    assert rc == 0
    assert cfg.pause_flag.read_text() == "\n"
    evts = events.tail(cfg.events_file, 5)
    pauses = [e for e in evts if e["type"] == "daemon_pause"]
    assert len(pauses) == 1
    assert pauses[0]["reason"] == ""


def test_cmd_pause_idempotent_when_already_paused(tmp_path: Path):
    """Non-happy contract: `ap2 pause` while already paused is idempotent
    (no exception, the new reason overrides the prior flag content, a
    second `daemon_pause` event is emitted).

    The handler has no real error branch — pinning idempotency here
    catches a refactor that adds a "refuse if already paused" check.
    That check would silently break the operator-clobber flow where a
    second pause with a fresher reason is the natural recovery from a
    stale flag. The audit trail is the secondary `daemon_pause` event,
    so we pin two events (not one) at the end."""
    cfg = _project(tmp_path)
    cmd_pause(cfg, Namespace(reason="first"))
    rc = cmd_pause(cfg, Namespace(reason="second"))

    assert rc == 0
    # Reason override: second pause replaces the flag content.
    assert cfg.pause_flag.read_text() == "second\n"
    evts = events.tail(cfg.events_file, 5)
    pauses = [e for e in evts if e["type"] == "daemon_pause"]
    assert len(pauses) == 2
    assert [p["reason"] for p in pauses] == ["first", "second"]


# ===========================================================================
# (2) `ap2 resume` — `cli.cmd_resume`
#
# Handler at `ap2/cli.py:1391-1396`:
#     if cfg.pause_flag.exists():
#         cfg.pause_flag.unlink()
#     events.append(cfg.events_file, "daemon_resume")
#     print("resumed")
#     return 0
#
# `resume` is the dual of `pause`. The handler is also flag-only and
# intentionally idempotent — operator's "force-resume" escape hatch when
# the flag file might be stale-deleted underfoot. We pin BOTH branches
# (flag exists → unlinked; flag absent → no-op + event still emitted).
# ===========================================================================


def test_cmd_resume_happy_path(tmp_path: Path, capsys):
    """`ap2 resume` while paused: removes the pause flag and emits a
    `daemon_resume` event. Pins the public contract: flag is gone after,
    return code is 0, audit event records the resume."""
    cfg = _project(tmp_path)
    # Pre-pause so resume has something to clear.
    cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_flag.write_text("paused for a reason\n")

    rc = cmd_resume(cfg, Namespace())

    assert rc == 0
    assert not cfg.pause_flag.exists()
    assert "resumed" in capsys.readouterr().out

    evts = events.tail(cfg.events_file, 5)
    resumes = [e for e in evts if e["type"] == "daemon_resume"]
    assert len(resumes) == 1


def test_cmd_resume_when_not_paused_is_noop(tmp_path: Path):
    """Error-shape contract: `ap2 resume` when no pause flag exists is a
    no-op (no exception, no flag write/unlink), but the `daemon_resume`
    event is STILL emitted. The handler treats the verb as an idempotent
    "force-resume" — an operator may legitimately invoke it after a
    crash or after manually deleting the flag.

    Pinning the still-emit catches a refactor that gates the event on
    `pause_flag.exists()` — silently breaking the audit-trail invariant
    that every `ap2 resume` invocation appears in events.jsonl."""
    cfg = _project(tmp_path)
    assert not cfg.pause_flag.exists()  # precondition

    rc = cmd_resume(cfg, Namespace())

    assert rc == 0
    assert not cfg.pause_flag.exists()
    evts = events.tail(cfg.events_file, 5)
    resumes = [e for e in evts if e["type"] == "daemon_resume"]
    assert len(resumes) == 1, (
        "daemon_resume must be emitted even when resume is a no-op — "
        "audit trail invariant"
    )


# ===========================================================================
# (3) `ap2 stop` — `cli.cmd_stop`
#
# Handler at `ap2/cli.py:101-111`:
#     pid = _read_pid(cfg)
#     if not pid or not _is_running(pid):
#         print("not running")
#         if cfg.pid_file.exists():
#             cfg.pid_file.unlink()
#         return 0
#     sig = signal.SIGKILL if args.force else signal.SIGTERM
#     os.kill(pid, sig)
#     print(f"sent {sig.name} to pid {pid}")
#     return 0
#
# We mock `os.kill` so the test never actually signals anything: the
# liveness probe `_is_running` uses `os.kill(pid, 0)` (signal 0 = no-op
# probe), and the actual stop uses `os.kill(pid, SIGTERM)`. The mock
# tolerates both calls; we assert the SIGTERM/SIGKILL signal was sent to
# the right pid.
# ===========================================================================


def test_cmd_stop_happy_path(tmp_path: Path, capsys):
    """`ap2 stop` against a live daemon pid: sends SIGTERM, prints the
    confirmation line, returns 0. The pid file is intentionally NOT
    unlinked on the happy path — the daemon's own shutdown handler is
    responsible for that, so a crashed daemon leaves a stale pid file
    that the next `ap2 stop` (error path below) cleans up."""
    cfg = _project(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("12345\n")

    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        # `_is_running` calls `os.kill(pid, 0)` first as a liveness probe;
        # we let that succeed silently. The "real" stop call sends SIGTERM.
        calls.append((pid, sig))

    with patch("ap2.cli.os.kill", side_effect=fake_kill):
        rc = cmd_stop(cfg, Namespace(force=False))

    assert rc == 0
    # Two calls expected: one liveness probe (sig=0) + one SIGTERM.
    assert (12345, 0) in calls, f"liveness probe missing: {calls}"
    assert (12345, signal.SIGTERM) in calls, (
        f"SIGTERM not delivered to pid 12345: {calls}"
    )
    out = capsys.readouterr().out
    assert "SIGTERM" in out and "12345" in out


def test_cmd_stop_force_sends_sigkill(tmp_path: Path, capsys):
    """`ap2 stop --force` swaps SIGTERM → SIGKILL. Pin the force-branch
    behavior: a refactor that drops `-f/--force` or flips the default
    signal would surface here. SIGKILL is the operator's last-resort
    escape hatch for a wedged daemon (no clean-shutdown chance)."""
    cfg = _project(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("12345\n")

    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    with patch("ap2.cli.os.kill", side_effect=fake_kill):
        rc = cmd_stop(cfg, Namespace(force=True))

    assert rc == 0
    assert (12345, signal.SIGKILL) in calls, (
        f"SIGKILL not delivered under --force: {calls}"
    )
    assert "SIGKILL" in capsys.readouterr().out


def test_cmd_stop_daemon_not_running_no_pid_file(tmp_path: Path, capsys):
    """Error path: `ap2 stop` with no pid file (`_read_pid` returns None)
    prints "not running" and exits 0. No `os.kill` should fire (avoiding
    the accidental-kill-by-stale-pid hazard). Pin this branch so a
    refactor that swaps to "raise if no pid file" surfaces — operators
    rely on `ap2 stop` being idempotent in CI/teardown scripts."""
    cfg = _project(tmp_path)
    assert not cfg.pid_file.exists()  # precondition

    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    with patch("ap2.cli.os.kill", side_effect=fake_kill):
        rc = cmd_stop(cfg, Namespace(force=False))

    assert rc == 0
    assert calls == [], (
        f"os.kill must not fire when no pid file exists: {calls}"
    )
    assert "not running" in capsys.readouterr().out


def test_cmd_stop_stale_pid_file_is_cleaned_up(tmp_path: Path, capsys):
    """Error path: `ap2 stop` with a pid file whose pid is dead
    (`_is_running` returns False) prints "not running", UNLINKS the
    stale pid file, and exits 0. Pin the stale-file cleanup so a
    refactor that leaves the file in place would surface — leftover
    stale pid files trip `_is_running` checks elsewhere and the
    operator's next `ap2 start` would refuse with "already running"."""
    cfg = _project(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("99999\n")

    def fake_kill(pid: int, sig: int) -> None:
        # `_is_running`'s liveness probe (sig=0) raises
        # ProcessLookupError on a dead pid — the handler interprets that
        # as "not running" and proceeds to clean up.
        if sig == 0:
            raise ProcessLookupError(f"no such pid {pid}")
        raise AssertionError(
            f"unexpected non-probe os.kill: pid={pid} sig={sig} "
            "— handler should short-circuit before signal delivery"
        )

    with patch("ap2.cli.os.kill", side_effect=fake_kill):
        rc = cmd_stop(cfg, Namespace(force=False))

    assert rc == 0
    assert not cfg.pid_file.exists(), (
        "stale pid file must be unlinked so a future `ap2 start` "
        "doesn't trip the already-running check"
    )
    assert "not running" in capsys.readouterr().out


# ===========================================================================
# (4) `ap2 unfreeze` — `cli.cmd_unfreeze`
#
# Handler at `ap2/cli.py:905-923`:
#     res = tools.do_operator_queue_append(
#         cfg, {"op": "unfreeze", "task_id": args.task_id}
#     )
#     if res.get("isError"):
#         print(res["content"][0]["text"], file=sys.stderr)
#         return 1
#     print(f"queued unfreeze {args.task_id} → Backlog ...")
#     return 0
#
# The validation (TB-131) happens inside `tools.do_operator_queue_append`:
# unfreeze on a non-Frozen task fails immediately; unknown task fails
# immediately. The actual board move + retry-reset happens on drain.
# We pin BOTH validation rejections (error paths) AND the queue→drain→
# Backlog flow (happy path).
#
# Existing `test_cli.py::test_unfreeze_*` tests exercise the same
# handler; the additions here carry the literal `"ap2 unfreeze"` for
# the drift gate's `name in blob` check (the existing tests use the
# `cmd_unfreeze` symbol but never the verb-string the gate looks for)
# AND mirror the per-verb-shape pattern (cmd_pause / cmd_resume /
# cmd_stop) for naming consistency.
# ===========================================================================


def test_cmd_unfreeze_happy_path(tmp_path: Path, capsys):
    """`ap2 unfreeze TB-N` on a Frozen task: queues the unfreeze op;
    after drain, the task is in Backlog with a reset retry counter and
    a `task_unfrozen` event recorded. Pins the full
    cmd_unfreeze → operator_queue → drain → Backlog flow that TB-131
    introduced."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-200", title="tb213 happy unfreeze")
    board.save()
    # Bump the retry counter so we can assert reset post-drain.
    retry.bump_attempt(cfg.retry_state_file, "TB-200")
    retry.bump_attempt(cfg.retry_state_file, "TB-200")
    assert retry.attempt_count(cfg.retry_state_file, "TB-200") == 2

    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-200"))
    assert rc == 0
    assert "queued unfreeze TB-200" in capsys.readouterr().out

    _drain(cfg)

    assert Board.load(cfg.tasks_file).find("TB-200")[0] == "Backlog"
    assert retry.attempt_count(cfg.retry_state_file, "TB-200") == 0
    evts = events.tail(cfg.events_file, 10)
    unfrozen = [e for e in evts if e["type"] == "task_unfrozen"]
    assert len(unfrozen) == 1
    assert unfrozen[0]["task"] == "TB-200"


def test_cmd_unfreeze_not_frozen(tmp_path: Path, capsys):
    """Error path: `ap2 unfreeze TB-N` on a non-Frozen task (here,
    Backlog) is rejected synchronously at queue-append time. Returns 1;
    stderr names the current section AND nudges the operator at
    `ap2 backlog` (the verb that handles non-Frozen moves). Pin the
    nudge text so a refactor that drops the operator-self-service hint
    surfaces — the value of the verb-separation IS the nudge."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-201", title="tb213 not frozen")
    board.save()

    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-201"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "not Frozen" in err
    assert "ap2 backlog" in err  # nudge to the right command
    # Task didn't move — validation was synchronous, not deferred.
    assert Board.load(cfg.tasks_file).find("TB-201")[0] == "Backlog"


def test_cmd_unfreeze_unknown_task(tmp_path: Path, capsys):
    """Error path: `ap2 unfreeze TB-999` with no such task on the board
    returns 1; stderr says "not on board". Pin so a refactor that
    silently no-ops on unknown ids (or returns 0) surfaces — operators
    rely on the non-zero exit to script around typos."""
    cfg = _project(tmp_path)
    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-999"))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err
