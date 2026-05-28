"""TB-212: happy + error path coverage for the three mattermost-emitted event
types tagged as TB-205-shape coverage debt in TB-208's `test_coverage_drift.py`
docstring (L391-399, mattermost subset).

The three events — `mattermost_timeout`, `mattermost_error`, `mm_poll_error` —
fire from the daemon's MM-handler / MM-poll path:

  - `daemon.handle_message` (daemon.py:744-756): wraps the per-mention
    `_run_control_agent` call. On `(timed_out=True, error=None, ...)` the
    helper emits `mattermost_timeout` carrying `timeout_s` +
    `thread_id`. On `(timed_out=False, error="<Type>: <msg>", ...)` it
    emits `mattermost_error` carrying the `error` string in the same
    `<type>: <msg>` shape `cron_error` uses (architecture.md L240).
  - `daemon._mm_loop` (daemon.py:2337-2351): the standalone polling loop
    wraps each `check_new_messages(cfg)` call in a try/except; on raise
    it emits `mm_poll_error` with `error=f"{type(e).__name__}: {e}"` —
    once per iteration that errors, NOT once per retry within an
    iteration (the loop has no in-iteration retry).

Prior to TB-212 these three events had ZERO real assertion coverage under
`ap2/tests/`; the only substring reference was the comment-block shim in
`test_coverage_drift.py`. A future refactor of `handle_message`'s
post-`_run_control_agent` switch (e.g. collapsing timeout + error into a
single event), or `_mm_loop`'s try/except wrap (e.g. moving emit into
`check_new_messages` itself), could silently drop the events while the
substring drift gate stayed green via the shim.

This module mirrors TB-211's `test_tb211_event_types.py` shape — one
focused test function per (event, aspect) pair, source-pinned to the
production emit site via `inspect.getsource(...)` so a refactor flips
the source-grep AND the runtime behavior assertion simultaneously, and
exercised through the real daemon seams (`daemon.handle_message`,
`daemon._mm_loop`) with the underlying SDK / poll calls stubbed:

  1. mattermost_timeout — `_run_control_agent` returns timeout-shaped
     tuple → `handle_message` emits with `timeout_s=cfg.control_timeout_s`
     and `thread_id=msg.get("thread_id")`. Two tests pin the emit and the
     payload-field shape.
  2. mattermost_error — `_run_control_agent` returns error-shaped tuple
     → `handle_message` emits with `error=<Type>: <msg>` payload, and
     does NOT also emit `mattermost_timeout` (the elif branch is
     mutually exclusive). Two tests pin happy emit + branch exclusivity.
  3. mm_poll_error — `check_new_messages(cfg)` raises → `_mm_loop`'s
     try/except emits with `error=<Type>: <msg>`. Two tests pin happy
     emit + the once-per-iteration invariant (no double-emit on retry).
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from ap2 import daemon, events
from ap2.config import Config


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    """Minimal Config with the required board sections present so a fresh
    `Board.load` doesn't trip on missing headings. Mirrors `_cfg` in
    `test_tb211_event_types.py` / `test_tb210_env_knobs.py`.
    """
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _base_msg(thread_id: str = "thread-abc") -> dict:
    """Mirrors `test_concurrent_mm._base_msg`. The thread_id field is the
    one `mattermost_timeout` carries through to its event payload — set a
    distinct value so the assertion can check propagation, not just
    presence.
    """
    return {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot status",
        "thread_id": thread_id,
    }


class _NoopSDK:
    """SDK stub used by `_mm_loop`. The seam under test (`check_new_messages`
    raising) never reaches the SDK boundary, so a no-behavior stub
    suffices. Mirrors the shape used in `test_tb211_event_types.py`."""

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        async def _gen():
            if False:
                yield None

        return _gen()


# ===========================================================================
# (1) mattermost_timeout — `daemon.handle_message` (daemon.py:744-750).
# Emitted when `_run_control_agent` returns `(timed_out=True, ...)`.
# Payload: `timeout_s=cfg.control_timeout_s`, `thread_id=msg.get("thread_id")`.
# ===========================================================================


_MATTERMOST_TIMEOUT_EMIT_FRAGMENT = '"mattermost_timeout"'


def test_mattermost_timeout_fires_on_handler_timeout(tmp_path, monkeypatch):
    """Happy emit-site (daemon.py:745-750): when `_run_control_agent`
    returns `(timed_out=True, error=None, ...)`, `handle_message` emits
    `mattermost_timeout`. Source-pin proves the emit lives on the
    `if timed_out:` branch in production; runtime check confirms the
    branch fires when the helper actually returns the timeout shape.

    Drives the real `daemon.handle_message` seam with the
    `_run_control_agent` helper monkeypatched to return the documented
    timeout-shape tuple. No SDK / network IO involved.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon.handle_message)
    assert _MATTERMOST_TIMEOUT_EMIT_FRAGMENT in src, (
        "regression: `daemon.handle_message` no longer emits "
        "mattermost_timeout — the per-handler timeout signal operators "
        "rely on when @claude-bot stops responding has been dropped"
    )
    assert "if timed_out:" in src, (
        "regression: `daemon.handle_message` no longer gates the "
        "mattermost_timeout emit on the `_run_control_agent` "
        "timed_out return — the documented branch contract is broken"
    )

    async def _stub_timeout(
        cfg, sdk, mcp_server, *,  # noqa: ARG001
        label, prompt, allowed_tools, max_turns, effort=None,  # noqa: ARG001
    ):
        return True, None, "stderr tail line\n", Path("/tmp/prompt.md")

    monkeypatch.setattr(daemon, "_run_control_agent", _stub_timeout)

    asyncio.run(
        daemon.handle_message(cfg, sdk=_NoopSDK(), mcp_server=None,
                              msg=_base_msg(thread_id="thread-T1"))
    )

    evts = events.tail(cfg.events_file, 50)
    timeouts = [e for e in evts if e["type"] == "mattermost_timeout"]
    assert len(timeouts) == 1, evts
    # The error branch must NOT also fire — `if/elif` is mutually exclusive.
    assert [e for e in evts if e["type"] == "mattermost_error"] == [], (
        "regression: timeout branch must not also emit mattermost_error "
        "— `if timed_out: ... elif error is not None:` is mutually "
        "exclusive in production"
    )


def test_mattermost_timeout_carries_timeout_s_and_thread_id(tmp_path, monkeypatch):
    """Payload-shape pin (daemon.py:746-750): the `mattermost_timeout`
    event carries `timeout_s=cfg.control_timeout_s` and
    `thread_id=msg.get("thread_id")`. A refactor that drops either
    field, renames them, or hardcodes a literal would trip this test.

    The two fields are operator-load-bearing: `timeout_s` lets the
    operator distinguish a fast-fail timeout from a tuned-too-low one
    without reading source; `thread_id` is what the operator uses to
    correlate the timeout back to the chat thread that triggered it.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon.handle_message)
    assert "timeout_s=cfg.control_timeout_s" in src, (
        "regression: `mattermost_timeout` no longer carries "
        "`timeout_s=cfg.control_timeout_s` — operators lose the "
        "fast-fail-vs-tuned-too-low signal"
    )
    assert 'thread_id=msg.get("thread_id")' in src, (
        "regression: `mattermost_timeout` no longer carries "
        "`thread_id=msg.get(\"thread_id\")` — operators lose the "
        "chat-thread correlation"
    )

    async def _stub_timeout(
        cfg, sdk, mcp_server, *,  # noqa: ARG001
        label, prompt, allowed_tools, max_turns, effort=None,  # noqa: ARG001
    ):
        return True, None, "", Path("/tmp/prompt.md")

    monkeypatch.setattr(daemon, "_run_control_agent", _stub_timeout)

    msg = _base_msg(thread_id="thread-PAYLOAD-1")
    asyncio.run(
        daemon.handle_message(cfg, sdk=_NoopSDK(), mcp_server=None, msg=msg)
    )

    evts = events.tail(cfg.events_file, 50)
    timeouts = [e for e in evts if e["type"] == "mattermost_timeout"]
    assert len(timeouts) == 1, evts
    evt = timeouts[0]
    assert evt["timeout_s"] == cfg.control_timeout_s, (
        f"timeout_s payload should equal cfg.control_timeout_s "
        f"({cfg.control_timeout_s}); got {evt.get('timeout_s')!r}"
    )
    assert evt["thread_id"] == "thread-PAYLOAD-1", (
        f"thread_id should propagate from msg['thread_id']; got "
        f"{evt.get('thread_id')!r}"
    )


# ===========================================================================
# (2) mattermost_error — `daemon.handle_message` (daemon.py:751-756).
# Emitted when `_run_control_agent` returns `(timed_out=False,
# error="<Type>: <msg>", ...)`. Payload: `error=<Type>: <msg>` (same
# shape as `cron_error`'s `error` field per architecture.md L240).
# ===========================================================================


def test_mattermost_error_carries_error_field(tmp_path, monkeypatch):
    """Happy emit-site (daemon.py:751-756): when `_run_control_agent`
    returns `(timed_out=False, error="<Type>: <msg>", ...)`,
    `handle_message` emits `mattermost_error` with the helper-formatted
    `error` string passed straight through.

    Source-pin confirms the emit lives on the `elif error is not None:`
    branch and uses the helper's `error` return verbatim — a refactor
    that re-formats the string (or splits it into separate type/msg
    fields) would break operator events.jsonl greps that match on
    `error="<Type>: ..."`.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon.handle_message)
    assert '"mattermost_error"' in src, (
        "regression: `daemon.handle_message` no longer emits "
        "mattermost_error — the per-handler exception signal operators "
        "rely on for SDK-side failures has been dropped"
    )
    assert "elif error is not None:" in src, (
        "regression: `daemon.handle_message` no longer gates the "
        "mattermost_error emit on the `_run_control_agent` `error` "
        "return — the documented branch contract is broken"
    )
    assert "error=error" in src, (
        "regression: `mattermost_error` no longer passes the helper's "
        "`error` string through verbatim — operators lose the "
        "<Type>: <msg> shape that mirrors cron_error"
    )

    err_str = "RuntimeError: SDK subprocess crashed"

    async def _stub_error(
        cfg, sdk, mcp_server, *,  # noqa: ARG001
        label, prompt, allowed_tools, max_turns, effort=None,  # noqa: ARG001
    ):
        return False, err_str, "stderr tail\n", Path("/tmp/prompt.md")

    monkeypatch.setattr(daemon, "_run_control_agent", _stub_error)

    asyncio.run(
        daemon.handle_message(cfg, sdk=_NoopSDK(), mcp_server=None,
                              msg=_base_msg())
    )

    evts = events.tail(cfg.events_file, 50)
    errors = [e for e in evts if e["type"] == "mattermost_error"]
    assert len(errors) == 1, evts
    assert errors[0]["error"] == err_str, (
        f"mattermost_error payload `error` should pass the helper return "
        f"through verbatim; got {errors[0].get('error')!r}"
    )


def test_mattermost_error_does_not_double_emit_with_timeout(tmp_path, monkeypatch):
    """Branch exclusivity (daemon.py:744-756): `if timed_out: ... elif
    error is not None:` is mutually exclusive — when `_run_control_agent`
    returns the error shape, ONLY `mattermost_error` fires (not also
    `mattermost_timeout`). A refactor that swaps `elif` for `if` would
    double-emit and trip this test.
    """
    cfg = _cfg(tmp_path)

    async def _stub_error(
        cfg, sdk, mcp_server, *,  # noqa: ARG001
        label, prompt, allowed_tools, max_turns, effort=None,  # noqa: ARG001
    ):
        return False, "ValueError: bad payload", "", Path("/tmp/prompt.md")

    monkeypatch.setattr(daemon, "_run_control_agent", _stub_error)

    asyncio.run(
        daemon.handle_message(cfg, sdk=_NoopSDK(), mcp_server=None,
                              msg=_base_msg())
    )

    evts = events.tail(cfg.events_file, 50)
    assert [e["type"] for e in evts if e["type"] in {
        "mattermost_timeout", "mattermost_error",
    }] == ["mattermost_error"], (
        "regression: error path should emit mattermost_error only; "
        f"got {[e['type'] for e in evts]}"
    )


def test_handle_message_success_emits_neither_timeout_nor_error(tmp_path, monkeypatch):
    """Branch exclusivity (negative case, daemon.py:744-756): when
    `_run_control_agent` returns the success shape `(False, None, "",
    path)`, NEITHER `mattermost_timeout` NOR `mattermost_error` fires.
    Pins the negative branch: a refactor that always-emits one of the
    two (e.g. on a missing-payload guard) would trip here.
    """
    cfg = _cfg(tmp_path)

    async def _stub_success(
        cfg, sdk, mcp_server, *,  # noqa: ARG001
        label, prompt, allowed_tools, max_turns, effort=None,  # noqa: ARG001
    ):
        return False, None, "", Path("/tmp/prompt.md")

    monkeypatch.setattr(daemon, "_run_control_agent", _stub_success)

    asyncio.run(
        daemon.handle_message(cfg, sdk=_NoopSDK(), mcp_server=None,
                              msg=_base_msg())
    )

    evts = events.tail(cfg.events_file, 50)
    failure_evts = [
        e for e in evts
        if e["type"] in {"mattermost_timeout", "mattermost_error"}
    ]
    assert failure_evts == [], (
        f"success path must not emit any failure event; got {failure_evts}"
    )


# ===========================================================================
# (3) mm_poll_error — `daemon._mm_loop` (daemon.py:2337-2351). Emitted
# when `check_new_messages(cfg)` raises inside the polling loop. Payload:
# `error=f"{type(e).__name__}: {e}"`. Once per iteration that errors —
# the loop has no in-iteration retry.
# ===========================================================================


def _stop_loop_after(n_iterations: int, monkeypatch) -> list[int]:
    """Replace `daemon._interruptible_sleep` with a no-IO stub that flips
    `daemon.RUNNING = False` after `n_iterations` calls. Returns the
    counter list (caller can read `[0]` after the run to assert the
    actual number of sleeps that fired).
    """
    counter = [0]

    async def _stub_sleep(total_s):  # noqa: ARG001
        counter[0] += 1
        if counter[0] >= n_iterations:
            daemon.RUNNING = False

    monkeypatch.setattr(daemon, "_interruptible_sleep", _stub_sleep)
    return counter


def test_mm_poll_error_on_check_new_messages_exception(tmp_path, monkeypatch):
    """Happy emit-site (daemon.py:2347-2351): when `check_new_messages`
    raises inside `_mm_loop`, the surrounding try/except catches the
    exception and emits `mm_poll_error` with `error="<Type>: <msg>"`
    payload.

    Drives the real `daemon._mm_loop` for one iteration with
    `check_new_messages` monkeypatched to raise. Source-pin proves the
    emit pattern matches production; runtime check confirms the wrap
    actually fires the documented event.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon._mm_loop)
    assert '"mm_poll_error"' in src, (
        "regression: `daemon._mm_loop` no longer emits mm_poll_error "
        "around the check_new_messages call — the MM-poll-side error "
        "signal operators rely on has been dropped"
    )
    assert 'error=f"{type(e).__name__}: {e}"' in src, (
        "regression: mm_poll_error `error` field formatting drifted "
        "from `<type>: <msg>` — operators lose the type-vs-message "
        "split that matches cron_error / mattermost_error"
    )
    # TB-312: `_mm_loop` now routes through `_check_inbound_messages(cfg)`
    # which walks the registry's `inbound_poll` hook points. The
    # pre-TB-312 `check_new_messages(cfg)` call site was renamed; the
    # structural invariant (MM polling fires per iteration) is preserved
    # at the new symbol name.
    assert "_check_inbound_messages(cfg)" in src, (
        "regression: `_mm_loop` no longer calls "
        "`_check_inbound_messages(cfg)` — MM polling is structurally broken"
    )

    def _boom(cfg):  # noqa: ARG001
        raise RuntimeError("MM poll API down")

    monkeypatch.setattr(daemon, "_check_inbound_messages", _boom)
    monkeypatch.setattr(daemon, "RUNNING", True)
    _stop_loop_after(1, monkeypatch)

    asyncio.run(
        daemon._mm_loop(cfg, sdk=_NoopSDK(), mcp_server=None,
                        handler_tasks=set())
    )

    evts = events.tail(cfg.events_file, 50)
    poll_errs = [e for e in evts if e["type"] == "mm_poll_error"]
    assert len(poll_errs) == 1, evts
    err = poll_errs[0]
    assert "RuntimeError" in err["error"]
    assert "MM poll API down" in err["error"]


def test_mm_poll_error_emits_once_per_iteration_no_double_on_retry(
    tmp_path, monkeypatch,
):
    """Branch invariant (daemon.py:2337-2352): the try/except sits
    INSIDE the `while RUNNING:` loop body but OUTSIDE any in-iteration
    retry — so a raising poll produces exactly one `mm_poll_error`
    per iteration, never two.

    Drives `_mm_loop` for two iterations with a monkeypatched
    raise-every-call `_check_inbound_messages` (TB-312: the
    registry-walked poll layer that replaced the pre-TB-312 direct
    `check_new_messages` import). Asserts the event count is exactly
    equal to the iteration count (2), not 4 — proves the wrap is
    per-iteration, not per-call within a hypothetical retry loop.
    A refactor that bolted retry-with-backoff inside the iteration
    without dedup would emit multiple events per loop pass and trip
    this test.
    """
    cfg = _cfg(tmp_path)

    call_count = [0]

    def _boom(cfg):  # noqa: ARG001
        call_count[0] += 1
        raise RuntimeError(f"poll #{call_count[0]} failed")

    monkeypatch.setattr(daemon, "_check_inbound_messages", _boom)
    monkeypatch.setattr(daemon, "RUNNING", True)
    sleep_counter = _stop_loop_after(2, monkeypatch)

    asyncio.run(
        daemon._mm_loop(cfg, sdk=_NoopSDK(), mcp_server=None,
                        handler_tasks=set())
    )

    # Sanity: the loop body actually ran twice (once per scheduled sleep).
    assert sleep_counter[0] == 2, (
        f"loop should have run exactly 2 iterations; sleep stub fired "
        f"{sleep_counter[0]} times"
    )

    evts = events.tail(cfg.events_file, 50)
    poll_errs = [e for e in evts if e["type"] == "mm_poll_error"]
    assert len(poll_errs) == 2, (
        f"expected exactly one mm_poll_error per iteration (2 total); "
        f"got {len(poll_errs)} — possible double-emit regression: {poll_errs}"
    )
    # Each iteration's payload should reflect the distinct call-site message,
    # confirming the events are from separate iterations rather than a
    # spurious double-emit within one iteration.
    assert "poll #1 failed" in poll_errs[0]["error"]
    assert "poll #2 failed" in poll_errs[1]["error"]
