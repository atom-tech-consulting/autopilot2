"""Tests for the daemon-bundled web UI lifecycle (TB-130).

`ap2 start` now spawns the read-only web server in the daemon process so
operators don't have to remember a second `ap2 web` invocation (and don't
end up with the UI pointing at a stale events.jsonl after a daemon
restart). This module covers the wiring that wasn't already covered by
`test_web.py::test_serve_async_*`:

- `_web_loop_for_daemon` emits `web_start` then `web_stop` around the run.
- A bind clash on the port surfaces as a `web_error` event but doesn't
  propagate (the daemon's main loops must keep ticking).
- `AP2_WEB_DISABLED` short-circuits the spawn entirely from `main_loop`.
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from ap2 import events as ev_mod, web
from ap2.config import Config
from ap2.daemon import _web_loop_for_daemon


def _project(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _events_of_type(cfg: Config, typ: str) -> list[dict]:
    return [e for e in ev_mod.tail(cfg.events_file, n=200) if e.get("type") == typ]


def test_web_loop_emits_start_and_stop(tmp_path: Path, monkeypatch):
    """Happy path: `_web_loop_for_daemon` writes `web_start` when the
    server comes up, `web_stop` when cancellation tears it down.
    Operators reading events.jsonl can pair the two and confirm the
    web UI's lifetime matches the daemon's."""
    cfg = _project(tmp_path)
    port = _free_port()
    monkeypatch.setenv("AP2_WEB_PORT", str(port))
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)

    async def _go() -> None:
        task = asyncio.create_task(_web_loop_for_daemon(cfg))
        # Yield long enough for the bind + first `web_start` write.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if _events_of_type(cfg, "web_start"):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_go())

    starts = _events_of_type(cfg, "web_start")
    stops = _events_of_type(cfg, "web_stop")
    assert len(starts) == 1, starts
    assert starts[0]["port"] == port
    assert starts[0]["url"] == f"http://127.0.0.1:{port}/"
    # TB-155: when the bound port matches the requested port (the
    # no-conflict happy path), `requested_port` must NOT appear in the
    # `web_start` payload — its presence is the audit signal of a
    # silent enumeration. Operators grepping `requested_port` should
    # only see hits when something actually clashed.
    assert "requested_port" not in starts[0], (
        "`requested_port` must be omitted on the no-conflict path"
    )
    # `web_stop` always fires on the way out — the daemon-stop summary in
    # operator reports relies on its presence even if the bind failed.
    assert len(stops) == 1, stops
    # No `web_error` on the happy path.
    assert _events_of_type(cfg, "web_error") == []


def test_web_loop_auto_enumerates_on_single_port_clash(
    tmp_path: Path, monkeypatch,
):
    """TB-155: a single-port collision is no longer fatal. When the
    requested port is bound (typically a stale daemon or an `ap2 web`
    standalone), `_web_loop_for_daemon` quietly walks forward to the
    next free port and emits a `web_start` carrying both `port` (the
    bound one) and `requested_port` (the one we asked for). No
    `web_error` — the UI is up, just on a different port."""
    cfg = _project(tmp_path)
    requested = _free_port()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", requested))
    blocker.listen(1)
    monkeypatch.setenv("AP2_WEB_PORT", str(requested))
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)

    async def _go() -> None:
        task = asyncio.create_task(_web_loop_for_daemon(cfg))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if _events_of_type(cfg, "web_start"):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(_go())
    finally:
        blocker.close()

    starts = _events_of_type(cfg, "web_start")
    assert len(starts) == 1, starts
    bound = starts[0]["port"]
    assert bound != requested, "should have enumerated past the busy port"
    assert starts[0]["requested_port"] == requested, (
        "the audit field naming the silent enumeration must be present"
    )
    # URL must reflect the BOUND port, not the requested one — otherwise
    # the operator clicks through to a port nothing is listening on.
    assert starts[0]["url"] == f"http://127.0.0.1:{bound}/"
    # No web_error: a single-port collision is now a routine startup
    # condition the daemon papers over.
    assert _events_of_type(cfg, "web_error") == []
    # `web_stop` records the bound port (matches `web_start`'s `port`)
    # so paired-grep lookups stay consistent.
    stops = _events_of_type(cfg, "web_stop")
    assert len(stops) == 1
    assert stops[0]["port"] == bound


def test_web_loop_logs_web_error_when_range_exhausted(
    tmp_path: Path, monkeypatch,
):
    """TB-155: when all 10 candidate ports in the enumeration range are
    bound, the daemon falls back to the pre-TB-155 behavior: log a
    `web_error` naming the range and return cleanly so the rest of the
    daemon (tick loop, MM loop) keeps running. The web UI is a
    convenience — its absence still must not take the daemon down."""
    cfg = _project(tmp_path)
    start = _free_port()
    n = web.DEFAULT_WEB_PORT_MAX_ATTEMPTS  # 10
    blockers = []
    try:
        for offset in range(n):
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", start + offset))
            except OSError:
                pytest.skip("contiguous port range unavailable")
            s.listen(1)
            blockers.append(s)
        monkeypatch.setenv("AP2_WEB_PORT", str(start))
        monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)

        async def _go() -> None:
            # Should return cleanly — NOT raise OSError out to the caller.
            await _web_loop_for_daemon(cfg)

        asyncio.run(_go())
    finally:
        for s in blockers:
            s.close()

    errs = _events_of_type(cfg, "web_error")
    assert len(errs) == 1, errs
    assert errs[0]["port"] == start
    # The range must be in the error message — that's the operator's
    # only audit trail when post-mortem'ing a bind failure.
    assert f"{start}..{start + n - 1}" in errs[0]["error"], errs[0]["error"]
    # `web_stop` still fires for symmetry — operators grepping for
    # `web_start.*web_stop` don't need a special case for the failed bind.
    assert _events_of_type(cfg, "web_stop")


def test_main_loop_skips_web_when_disabled(monkeypatch):
    """`AP2_WEB_DISABLED=1` short-circuits `is_web_disabled` so `main_loop`
    never schedules `_web_loop_for_daemon` at all. White-box: the env-knob
    parser is the daemon's only gate, so testing the parser covers the
    skip path without spinning up the full main_loop."""
    monkeypatch.setenv("AP2_WEB_DISABLED", "1")
    assert web.is_web_disabled() is True

    monkeypatch.setenv("AP2_WEB_DISABLED", "true")
    assert web.is_web_disabled() is True

    monkeypatch.setenv("AP2_WEB_DISABLED", "0")
    assert web.is_web_disabled() is False
