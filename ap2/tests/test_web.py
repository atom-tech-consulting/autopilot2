"""Tests for `ap2.web` — app construction / daemon lifecycle / port binding.

Post-TB-267: this module retains only the cross-cutting tests that aren't
tied to a single route group — the asyncio `serve_async` entry point,
the `_bind_with_enumeration` helper that papers over single-port
conflicts (TB-155), and the env-knob plumbing (`is_web_disabled`,
`daemon_web_port`). Every route-specific test was relocated to a
web-prefixed sibling that mirrors the TB-265 source split:

  - `test_web_home.py` — `/` page + cards (pending queue, operator
    decisions, ideation status, env-stale warning).
  - `test_web_events.py` — `/events`, `/tasks`, `/task/<id>`,
    `/pipelines`, `/ideation_state`, `/commits`.
  - `test_web_tasks.py` — `/task-run/<id>` live page + stream JSON.
  - `test_web_insights.py` — `/insights` + `/insight/<n>`.
  - `test_web_usage.py` — `/usage` token-cost dashboard.
  - `test_web_chrome.py` — chrome helpers (`_row_class`, `_event_extra`,
    `_find_run_id_for_event`, `_terminal_event_for_run`, `_read_jsonl`,
    `_events_table` compact-usage rendering).
  - `test_web_stats.py` — `/stats` mirror placeholder (no coverage in
    this batch; the `/stats` tests live in `test_stats_dashboard.py`).

The shared `project` fixture moved to `ap2/tests/conftest.py` so every
sibling can pick it up via pytest auto-discovery without sibling-to-
sibling imports.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import web
from ap2.config import Config


# --------- TB-130: daemon-bundled web lifecycle ---------


def test_is_web_disabled_truthy_values(monkeypatch):
    """The `AP2_WEB_DISABLED` env knob accepts the standard ap2 truthy
    strings (1/true/yes/on, case-insensitive). Anything else (including
    unset) keeps the UI on so daemon-spawned mode is the default."""
    for val in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv("AP2_WEB_DISABLED", val)
        assert web.is_web_disabled() is True, val
    for val in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("AP2_WEB_DISABLED", val)
        assert web.is_web_disabled() is False, val
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)
    assert web.is_web_disabled() is False


def test_daemon_web_port_default_and_override(monkeypatch):
    """Default 8729 (TB-130 spec); `AP2_WEB_PORT` overrides; malformed
    values fall back to the default rather than crashing the daemon at
    startup."""
    monkeypatch.delenv("AP2_WEB_PORT", raising=False)
    assert web.daemon_web_port() == web.DEFAULT_DAEMON_WEB_PORT == 8729

    monkeypatch.setenv("AP2_WEB_PORT", "9999")
    assert web.daemon_web_port() == 9999

    # A typo shouldn't kill the daemon — fall back rather than ValueError.
    monkeypatch.setenv("AP2_WEB_PORT", "not-a-number")
    assert web.daemon_web_port() == web.DEFAULT_DAEMON_WEB_PORT


def _free_port() -> int:
    """Bind 0 to grab a kernel-assigned port, then release it. Cheap; the
    test grabs the same port a moment later for the real bind."""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def test_serve_async_serves_and_cancels_cleanly(project: Config):
    """`serve_async` is the daemon-managed entry point: bind a real socket,
    confirm a request lands, then cancel the task and confirm the port
    frees up. End-to-end check that cancellation actually shuts down the
    HTTP server thread (otherwise restarting the daemon would EADDRINUSE)."""
    import asyncio
    import urllib.request

    port = _free_port()

    async def _exercise() -> tuple[int, str]:
        task = asyncio.create_task(
            web.serve_async(project, host="127.0.0.1", port=port)
        )
        # The bind happens synchronously inside `serve_async` before it
        # parks on Event.wait, but the thread's first accept() takes a
        # tick — yield control once so the listener is ready.
        for _ in range(50):
            await asyncio.sleep(0.02)
            try:
                resp = await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"http://127.0.0.1:{port}/", None, 2.0,
                )
                body = resp.read().decode()
                status = resp.status
                resp.close()
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError("server never accepted a request")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return status, body

    status, body = asyncio.run(_exercise())
    assert status == 200
    assert "<!DOCTYPE html>" in body
    # And the port is releasable — otherwise the next daemon restart trips
    # EADDRINUSE. `_free_port` will throw or return a different port if
    # this one is still bound; a fresh bind on the same port should work.
    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_serve_async_propagates_bind_error(project: Config):
    """A port collision should surface as `OSError` from `serve_async` so
    the daemon can log a `web_error` event instead of crashing the loop.

    TB-155: auto-enumeration is opt-out via `max_attempts=1` — pre-TB-155
    callers that want the original "first failure raises" behavior keep
    that contract by passing `max_attempts=1`. The daemon path now sets
    `max_attempts=10` and only raises after exhausting the range; that's
    covered by `test_serve_async_range_exhausted_raises` below.
    """
    import asyncio
    import socket

    port = _free_port()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)

    async def _go():
        with pytest.raises(OSError):
            await web.serve_async(
                project, host="127.0.0.1", start_port=port, max_attempts=1,
            )

    try:
        asyncio.run(_go())
    finally:
        blocker.close()


# --------- TB-155: web port auto-enumerate on conflict ---------


def test_bind_with_enumeration_no_conflict_returns_start_port():
    """Happy path: when `start_port` is free, the helper binds it and
    returns it untouched — no enumeration churn, no offset added.
    Establishes the baseline contract before testing the conflict cases."""
    port = _free_port()
    sock, bound = web._bind_with_enumeration(
        "127.0.0.1", port, web.DEFAULT_WEB_PORT_MAX_ATTEMPTS,
    )
    try:
        assert bound == port
        # The returned socket is actually bound to that port (so the caller
        # can hand it to `socketserver` and start serving without re-binding).
        assert sock.getsockname()[1] == port
    finally:
        sock.close()


def test_bind_with_enumeration_skips_busy_port():
    """When `start_port` is already bound, the helper walks forward and
    binds the next free port. This is the core TB-155 behavior — silently
    paper over a single-port collision (typically a stale daemon or an
    `ap2 web` standalone) instead of failing the whole web UI."""
    import socket as _sock

    port = _free_port()
    # Block `port`; leave `port+1` open. The helper should pick `port+1`.
    blocker = _sock.socket()
    blocker.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        sock, bound = web._bind_with_enumeration("127.0.0.1", port, 10)
        try:
            assert bound == port + 1, (
                f"expected port+1={port + 1}, got {bound}"
            )
            assert sock.getsockname()[1] == port + 1
        finally:
            sock.close()
    finally:
        blocker.close()


def test_bind_with_enumeration_exhausts_range_and_raises():
    """When ALL ports in the enumerated range are bound, the helper
    raises a single `OSError` whose message names the range — no infinite
    loop, no climb into the ephemeral range. The error message is the
    operator's only handle on the conflict, so it must include the
    boundaries they need to investigate."""
    import socket as _sock

    # Grab a contiguous range of free ports first (kernel-assigned ports
    # aren't guaranteed contiguous, so probe upward from a free start).
    start = _free_port()
    blockers = []
    n = 4  # tight range so the test is cheap.
    try:
        for offset in range(n):
            s = _sock.socket()
            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", start + offset))
            except OSError:
                # Skip this run — port races with another process.
                pytest.skip("contiguous port range unavailable")
            s.listen(1)
            blockers.append(s)
        with pytest.raises(OSError) as exc:
            web._bind_with_enumeration("127.0.0.1", start, n)
        msg = str(exc.value)
        # The range must be in the message — that's the audit trail the
        # operator gets in the `web_error` event payload.
        assert f"{start}..{start + n - 1}" in msg, msg
    finally:
        for s in blockers:
            s.close()


def test_bind_with_enumeration_non_eaddrinuse_propagates_immediately():
    """Errors other than EADDRINUSE (e.g. permission denied on a privileged
    port) shouldn't trigger enumeration — walking forward wouldn't help and
    would just produce N noisy retries. The first non-EADDRINUSE OSError
    propagates as-is."""
    # Privileged port on a non-root user trips EACCES, not EADDRINUSE.
    # Skip when we happen to have privilege (e.g. running as root in CI).
    import os as _os
    if _os.geteuid() == 0:
        pytest.skip("test requires non-root euid to trip EACCES")
    with pytest.raises(OSError) as exc:
        web._bind_with_enumeration("127.0.0.1", 1, 10)
    # Either EACCES (Linux) or EPERM (macOS) — both are non-EADDRINUSE,
    # which is the contract we're checking. Concretely: the message must
    # NOT contain the "no free port in range" wording, because that would
    # mean the helper enumerated through privileged ports instead of
    # raising on the first failure.
    assert "no free port in range" not in str(exc.value), (
        "non-EADDRINUSE errors must propagate without enumeration"
    )


def test_serve_async_no_conflict_binds_start_port(project: Config):
    """TB-155 baseline: with no port collision, `serve_async(start_port=X)`
    binds X exactly — no enumeration churn, the `on_bind` callback fires
    with the requested port. The daemon wrapper relies on this equality
    to decide whether to omit the `requested_port` field from the
    `web_start` event (the audit signal that a silent enumeration
    happened); if `serve_async` ever drifted off the requested port on
    the happy path, every daemon startup would emit a spurious
    `requested_port` and the audit signal would lose its meaning."""
    import asyncio

    start_port = _free_port()
    bound_holder: dict = {}

    def _on_bind(host: str, port: int) -> None:
        bound_holder["host"] = host
        bound_holder["port"] = port

    async def _exercise() -> None:
        task = asyncio.create_task(
            web.serve_async(
                project,
                host="127.0.0.1",
                start_port=start_port,
                max_attempts=10,
                on_bind=_on_bind,
            )
        )
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "port" in bound_holder:
                    break
            assert "port" in bound_holder, "on_bind never fired"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_exercise())

    # Resolved port equals requested — no enumeration, no offset.
    assert bound_holder["host"] == "127.0.0.1"
    assert bound_holder["port"] == start_port, (
        f"expected bound=={start_port} on the no-conflict path, "
        f"got {bound_holder['port']}"
    )


def test_serve_async_auto_enumerates_on_conflict(project: Config):
    """End-to-end: with `start_port` already bound, `serve_async` quietly
    binds the next free port and the `on_bind` callback fires with the
    resolved port. Mirrors what `_web_loop_for_daemon` relies on for its
    `web_start` event payload."""
    import asyncio
    import socket as _sock
    import urllib.request

    start_port = _free_port()
    blocker = _sock.socket()
    blocker.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", start_port))
    blocker.listen(1)

    bound_holder: dict = {}

    def _on_bind(host: str, port: int) -> None:
        bound_holder["host"] = host
        bound_holder["port"] = port

    async def _exercise() -> None:
        task = asyncio.create_task(
            web.serve_async(
                project,
                host="127.0.0.1",
                start_port=start_port,
                max_attempts=10,
                on_bind=_on_bind,
            )
        )
        # Wait for `on_bind` to fire and a request to land on the
        # auto-enumerated port — confirms the server is actually listening
        # on the resolved port, not the requested one.
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "port" in bound_holder:
                    break
            assert "port" in bound_holder, "on_bind never fired"
            assert bound_holder["port"] != start_port, (
                "should have enumerated past the busy port"
            )
            resp = await asyncio.to_thread(
                urllib.request.urlopen,
                f"http://127.0.0.1:{bound_holder['port']}/", None, 2.0,
            )
            assert resp.status == 200
            resp.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(_exercise())
    finally:
        blocker.close()

    assert bound_holder["host"] == "127.0.0.1"
    assert bound_holder["port"] == start_port + 1


def test_serve_async_range_exhausted_raises(project: Config):
    """When the entire enumeration range is bound, `serve_async` re-raises
    the helper's `OSError` so the daemon can log a single `web_error`
    naming the range — the operator's hunt for the offending pid starts
    there. No silent fall-through, no climb past `max_attempts`."""
    import asyncio
    import socket as _sock

    start = _free_port()
    n = 3
    blockers = []
    try:
        for offset in range(n):
            s = _sock.socket()
            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", start + offset))
            except OSError:
                pytest.skip("contiguous port range unavailable")
            s.listen(1)
            blockers.append(s)

        async def _go():
            with pytest.raises(OSError) as exc:
                await web.serve_async(
                    project,
                    host="127.0.0.1",
                    start_port=start,
                    max_attempts=n,
                )
            assert f"{start}..{start + n - 1}" in str(exc.value)

        asyncio.run(_go())
    finally:
        for s in blockers:
            s.close()


def test_serve_calls_through_to_enumeration(project: Config, monkeypatch):
    """The standalone `ap2 web` path (`web.serve`) routes the bind through
    `_bind_with_enumeration` too, so an operator with a stale standalone
    on 7820 still gets a working UI on 7821 instead of an OSError. We
    don't run `serve()` to completion (it blocks on serve_forever), so
    this is a focused white-box: assert `_build_server` is called with
    the standalone start port and the helper's enumeration kicks in."""
    import socket as _sock

    # Block port 7820 with a real socket so enumeration has something to
    # walk past. Use a kernel-assigned proxy port (`_free_port` then bind
    # at the helper level) is not possible here because `serve()`'s
    # default is the literal 7820. Skip when 7820 is unbindable for
    # unrelated reasons (an actual standalone running, e.g.).
    blocker = _sock.socket()
    blocker.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    try:
        try:
            blocker.bind(("127.0.0.1", web.DEFAULT_STANDALONE_WEB_PORT))
        except OSError:
            pytest.skip(
                f"standalone default port "
                f"{web.DEFAULT_STANDALONE_WEB_PORT} not bindable in this env"
            )
        blocker.listen(1)

        # Stub `serve_forever` so `serve()` returns instead of blocking.
        # We just want to confirm the bind path picked port+1.
        captured: dict = {}
        real_build = web._build_server

        def _spy_build(cfg, host, start_port, max_attempts=10):
            srv, bound = real_build(
                cfg, host, start_port, max_attempts=max_attempts,
            )
            captured["bound"] = bound

            # Make `serve_forever` a no-op so the test doesn't hang.
            def _noop_serve_forever():
                return None

            srv.serve_forever = _noop_serve_forever  # type: ignore[method-assign]
            return srv, bound

        monkeypatch.setattr(web, "_build_server", _spy_build)
        web.serve(project)  # uses defaults: host=127.0.0.1, port=7820

        assert captured["bound"] == web.DEFAULT_STANDALONE_WEB_PORT + 1, (
            f"expected enumeration to {web.DEFAULT_STANDALONE_WEB_PORT + 1}, "
            f"got {captured.get('bound')}"
        )
    finally:
        blocker.close()
