"""TB-211: happy + error path coverage for the five daemon-emitted event
types that landed TB-208's `test_coverage_drift.py` L391-399 coverage-debt
comment block.

The five events — `auto_diagnose_error`, `classify_record_unreadable`,
`cron_bootstrap`, `cron_error`, `pipeline_pending_sweep_error` — each fire
from one or more emitter sites in `ap2/daemon.py` (lines 961/996/2169/2401/
2411/2494) and `ap2/tools.py` (line 2659/2667), but prior to TB-211 had
ZERO real test references under `ap2/tests/` — only the substring drift
gate's comment-block enumeration kept them green. A future refactor of
`daemon._run_cron_job` (`cron_error` / `cron_bootstrap`), `daemon._tick`'s
post-sweep / post-watchdog try/except wrappers (`cron_error` 2401 /
`pipeline_pending_sweep_error` 2411 / `auto_diagnose_error` 2494), or
`tools._apply_operator_op`'s classify branch (`classify_record_unreadable`)
could silently drop the wrap, rename the event, or invert a payload field
without any test signal.

This module mirrors TB-210's `test_tb210_env_knobs.py` shape — one or more
focused per-name test functions per emitter, source-pinned to the
production call site via `inspect.getsource(...)` so a refactor flips the
source-grep AND the runtime behavior assertion simultaneously, and
exercised through real daemon/tools seams (`daemon.run_cron`,
`daemon.bootstrap_cron`, `tools._apply_operator_op`) wherever possible.

For the three `_tick`-wrapped emitters (`cron_error` 2401,
`pipeline_pending_sweep_error` 2411, `auto_diagnose_error` 2494), driving
`_tick` end-to-end with selectively-stubbed internals exercises the
documented stub-points the daemon actually runs at tick boundary — the
exact try/except wrap and `events.append(...)` call live in `_tick`'s
source, not in test code.

  1. auto_diagnose_error            — `_tick` outer try/except around
                                      `_maybe_auto_diagnose`. Stub
                                      `_maybe_auto_diagnose` to raise; tick
                                      catches + emits with
                                      `error="<type>: <msg>"` payload.
  2. classify_record_unreadable     — `tools._apply_operator_op`'s
                                      `classify` branch when the proposal
                                      record exists but `json.loads` raises
                                      (or the parsed payload is not a
                                      dict). Two branches → two tests.
  3. cron_bootstrap                 — `daemon.bootstrap_cron` returns True
                                      on missing cron.yaml; `main_loop`
                                      emits with `path=<cron.yaml path>`.
                                      No emit when the file already exists.
  4. cron_error                     — three emitter sites:
                                      (a) janitor branch in `run_cron`
                                          (line 961) when the registry's
                                          janitor `tick_hook` raises
                                          (TB-309 reroute);
                                      (b) `_tick`'s outer wrap (line 2401)
                                          when `load_jobs` raises. Both
                                          payloads carry `error` formatted
                                          as `"<type>: <msg>"`.
  5. pipeline_pending_sweep_error   — `_tick` outer try/except around
                                      `_sweep_pipeline_pending`. Stub the
                                      sweep to raise; tick catches + emits.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import daemon, events, ideation, tools, web
from ap2.board import Board
from ap2.config import Config
from ap2.cron import CronJob


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    """Minimal Config with the required board sections present so a fresh
    `Board.load` / `_tick` doesn't trip on missing headings. Mirrors the
    `_cfg` helper in `test_env_knobs.py` / `test_tb210_env_knobs.py`.
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


class _NoopSDK:
    """SDK stub with no behavior — used where the seam under test never
    reaches the SDK boundary (e.g. `run_cron` with a stubbed
    `run_janitor` that raises before any SDK call)."""

    def __init__(self) -> None:
        self.called = False

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


def _stub_main_loop_internals(monkeypatch) -> None:
    """Stub every `daemon.main_loop` heavy/blocking dependency to a no-op
    so the real `main_loop` coroutine runs to completion in-process.

    The only `main_loop` behavior we leave un-stubbed is the line under
    test: the `if bootstrap_cron(cfg.cron_file): events.append("cron_bootstrap", ...)`
    pair near the top (L2167-2169). Everything downstream (SDK import,
    MCP server build, daemon_start emit, the two infinite tick loops, the
    web task spawn, the `daemon_stop` finally block) is no-op or harmless
    so `await daemon.main_loop(cfg)` returns within the test's runtime.

    Stubbed:
      - `daemon._recover_orphans` → sync no-op (skips reading orphan
        sentinels under `tmp_path` that the test doesn't seed).
      - `daemon._import_sdk_or_die` → sync no-op (we don't need the real
        SDK for the bootstrap-emit path; the import line at L2172 still
        runs and reads the installed module).
      - `daemon.build_mcp_server` → returns None (the MCP server is never
        actually consumed because the two tick loops are also stubbed).
      - `daemon._main_tick_loop` / `daemon._mm_loop` → async no-op so the
        `asyncio.gather(...)` at L2201 completes immediately instead of
        looping until SIGTERM.
      - `AP2_WEB_DISABLED=1` env so the `web.is_web_disabled()` check at
        L2197 short-circuits the web task spawn.
    """
    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_recover_orphans", lambda cfg: None)
    monkeypatch.setattr(daemon, "_import_sdk_or_die", lambda: None)
    monkeypatch.setattr(daemon, "build_mcp_server", lambda cfg: None)
    monkeypatch.setattr(daemon, "_main_tick_loop", _noop_async)
    monkeypatch.setattr(daemon, "_mm_loop", _noop_async)
    monkeypatch.setenv("AP2_WEB_DISABLED", "1")
    # Sanity: the env-knob parser must read this back as True, otherwise
    # `main_loop` spawns the web task and the test would hang. Pinning the
    # parser contract here prevents a silent env-name rename from breaking
    # this test in non-obvious ways.
    assert web.is_web_disabled() is True


def _stub_tick_quiet(monkeypatch) -> None:
    """Stub every `_tick` internal to a no-op success so a single
    targeted raise (in a follow-up `monkeypatch.setattr`) is the ONLY
    error path exercised. Caller decides which one raises by overriding
    the relevant attribute AFTER this helper runs.

    Stubbed:
      - `tools.drain_operator_queue` → returns empty applied dict.
      - `daemon._sweep_pipeline_pending` → async no-op.
      - `daemon._maybe_auto_diagnose` → sync no-op.
      - `ideation._maybe_ideate` / `ideation.force_ideate` → async no-op.
      - `daemon.load_jobs` is left alone (default reads cron.yaml which
        we leave non-existent → `load_jobs` returns []; load_jobs-raise
        tests monkeypatch this explicitly).
    """
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda cfg: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    async def _noop_sweep(cfg, sdk):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_sweep)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    monkeypatch.setattr(ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(ideation, "force_ideate", _noop_async)


# ===========================================================================
# (1) cron_bootstrap — emitted from `daemon.main_loop` at L2168-2169 when
# `bootstrap_cron(cfg.cron_file)` returns True (first-run cron.yaml seed).
#
# The bootstrap helper (`cron.bootstrap` re-exported as `daemon.bootstrap_cron`)
# copies the packaged default cron.yaml into place if missing and returns
# True; returns False if the file already exists. The emit pattern in
# `main_loop` is the canonical `if bootstrap_cron(...): events.append(...)`
# shape — source-pinning the pattern proves the emit is wired to the
# return value, not a side channel.
# ===========================================================================


_CRON_BOOTSTRAP_EMIT = (
    'events.append(cfg.events_file, "cron_bootstrap", path=str(cfg.cron_file))'
)


def test_cron_bootstrap_fires_on_first_run(tmp_path, monkeypatch):
    """Happy path: missing cron.yaml → `daemon.main_loop` calls
    `bootstrap_cron(cfg.cron_file)`, which returns True (the seed is
    written), and `main_loop`'s next line emits `cron_bootstrap` with
    `path=<cron.yaml path>` payload.

    Source-pin the `if bootstrap_cron(...): events.append(...)` pattern
    so a refactor that drops the emit (or renames the event type /
    payload field) trips this test. Then drive the REAL `main_loop`
    coroutine end-to-end with heavy/blocking internals stubbed to no-op
    so the bootstrap-emit pair (lines 2168-2169) actually fires from
    production code — not from a synthetic mirror in test code.
    """
    cfg = _cfg(tmp_path)
    src = inspect.getsource(daemon.main_loop)
    assert "bootstrap_cron(cfg.cron_file)" in src, (
        "regression: `daemon.main_loop` no longer calls `bootstrap_cron` — "
        "the first-run cron.yaml seed contract is broken"
    )
    assert _CRON_BOOTSTRAP_EMIT in src, (
        "regression: `daemon.main_loop` no longer emits `cron_bootstrap` "
        "with `path=str(cfg.cron_file)` payload"
    )

    # Sanity: pre-bootstrap, the cron.yaml does NOT exist.
    assert not cfg.cron_file.exists()

    _stub_main_loop_internals(monkeypatch)

    # Real seam — `daemon.main_loop` itself. The two tick loops are
    # stubbed to async no-ops so `await asyncio.gather(...)` completes
    # immediately; everything before the gather (`ensure_dirs`,
    # `bootstrap_cron(...)` + the conditional emit, SDK import,
    # `_emit_daemon_start`, pid-file write) runs unmodified, so the
    # `cron_bootstrap` event we assert on below comes from production
    # code's `events.append(...)` call at L2169.
    asyncio.run(daemon.main_loop(cfg))

    # Production code's bootstrap_cron wrote the seed.
    assert cfg.cron_file.exists()

    evts = events.tail(cfg.events_file, 50)
    bootstraps = [e for e in evts if e["type"] == "cron_bootstrap"]
    assert len(bootstraps) == 1, evts
    assert bootstraps[0]["path"] == str(cfg.cron_file)


def test_cron_bootstrap_no_emit_when_cron_yaml_exists(tmp_path, monkeypatch):
    """Branch coverage: when cron.yaml already exists, `bootstrap_cron`
    returns False and the `if` gate in `main_loop` short-circuits — no
    `cron_bootstrap` event fires. Pins the negative branch so a refactor
    that always-emits (regardless of bootstrap return) trips here.

    Drives the real `daemon.main_loop` end-to-end (with heavy internals
    stubbed) on a pre-seeded cron.yaml; the absence of the event is the
    contract, verified against production code's actual run — not a
    side-channel inference.
    """
    cfg = _cfg(tmp_path)
    cfg.cron_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.cron_file.write_text("jobs: []\n")

    src = inspect.getsource(daemon.main_loop)
    assert "if bootstrap_cron(cfg.cron_file):" in src, (
        "regression: `daemon.main_loop` no longer gates the cron_bootstrap "
        "emit on the bootstrap_cron return value — would re-emit on every "
        "daemon start instead of only first run"
    )

    _stub_main_loop_internals(monkeypatch)

    asyncio.run(daemon.main_loop(cfg))

    # cron.yaml is unchanged — `bootstrap_cron` returned False because
    # the file existed, so the seed wasn't overwritten.
    assert cfg.cron_file.read_text() == "jobs: []\n"

    # The `if` gate short-circuits → no `cron_bootstrap` event in the
    # production-side emit log.
    evts = events.tail(cfg.events_file, 50)
    assert [e for e in evts if e["type"] == "cron_bootstrap"] == [], evts


# ===========================================================================
# (2) cron_error — three emitter sites (daemon.py:961, 996, 2401). All
# three carry `error=f"{type(e).__name__}: {e}"`; the janitor and
# control-agent sites also carry `job=<name>`. We pin two of the three
# (janitor + tick wrap) — the control-agent path at L996 shares the
# same `error` formatting contract and is a structural sibling.
# ===========================================================================


def test_cron_error_carries_error_field_when_janitor_raises(tmp_path, monkeypatch):
    """Happy emit-site (janitor branch, daemon.py:961): when
    `janitor.run_janitor` raises, `run_cron`'s try/except catches the
    exception and emits `cron_error` with `job="janitor"` plus
    `error="<exc_type>: <msg>"`.

    Drives the real `daemon.run_cron` seam with a stubbed-to-raise
    `janitor.run_janitor`. Source-pin proves the emit pattern is the
    exact one in production.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon.run_cron)
    assert '"cron_error"' in src, (
        "regression: `daemon.run_cron` no longer emits cron_error on "
        "janitor exception"
    )
    assert 'error=f"{type(e).__name__}: {e}"' in src, (
        "regression: cron_error `error` field formatting drifted from "
        "`<type>: <msg>` — operators reading the event lose the "
        "type-vs-message split"
    )

    async def _boom(cfg, sdk):  # noqa: ARG001
        raise RuntimeError("janitor exploded mid-run")

    # TB-309: janitor is now a registry-discovered component. `run_cron`
    # looks up the tick_hook via `default_registry().hook(...)` at
    # call-time, so the patching point is the manifest's `hook_points`
    # dict (a regular dict — intentionally mutable so a test can swap
    # one hook for a stub without rebuilding the whole registry).
    from ap2.registry import default_registry
    janitor_manifest = default_registry().get("janitor")
    monkeypatch.setitem(janitor_manifest.hook_points, "tick_hook", _boom)

    sdk = _NoopSDK()
    job = CronJob(
        name="janitor", interval_s=300, prompt="ignored", max_turns=5,
    )
    asyncio.run(daemon.run_cron(cfg, sdk, mcp_server=None, job=job))

    evts = events.tail(cfg.events_file, 50)
    cron_errs = [e for e in evts if e["type"] == "cron_error"]
    assert len(cron_errs) == 1, evts
    err = cron_errs[0]
    assert err["job"] == "janitor"
    assert "RuntimeError" in err["error"]
    assert "janitor exploded mid-run" in err["error"]
    # cron_complete still fires after the error path — the run_cron contract
    # bookends every job regardless of inner failure.
    assert any(
        e["type"] == "cron_complete" and e["job"] == "janitor"
        for e in evts
    )


def test_cron_error_wraps_load_jobs_failure_in_tick(tmp_path, monkeypatch):
    """Branch (tick wrap, daemon.py:2401): when `load_jobs(cfg.cron_file)`
    raises inside `_tick`, the surrounding try/except catches it and
    emits `cron_error` with just an `error` field (no `job=` field —
    no specific job was in flight).

    Drives `daemon._tick` end-to-end with the load_jobs path stubbed to
    raise and every other tick stage stubbed to a no-op success, so the
    `cron_error` emit is the ONLY error event produced by the tick.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon._tick)
    assert '"cron_error"' in src, (
        "regression: `daemon._tick` no longer emits cron_error around the "
        "cron stage's load_jobs/run_cron block"
    )

    _stub_tick_quiet(monkeypatch)

    def _boom_load_jobs(*a, **kw):  # noqa: ARG001
        raise RuntimeError("cron.yaml unreadable")

    monkeypatch.setattr(daemon, "load_jobs", _boom_load_jobs)

    sdk = _NoopSDK()
    asyncio.run(daemon._tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 50)
    cron_errs = [e for e in evts if e["type"] == "cron_error"]
    assert len(cron_errs) == 1, evts
    err = cron_errs[0]
    # The tick-wrap emitter has no `job` field — only the janitor /
    # control-agent emitters carry it.
    assert "job" not in err, (
        "regression: tick-wrap cron_error must NOT carry job= field; "
        f"the load_jobs failure happens before any specific job is in "
        f"flight. got: {err}"
    )
    assert "RuntimeError" in err["error"]
    assert "cron.yaml unreadable" in err["error"]


# ===========================================================================
# (3) pipeline_pending_sweep_error — emitted from `daemon._tick`'s outer
# try/except (line 2410-2413) around `_sweep_pipeline_pending`. Stub the
# sweep to raise and drive `_tick`; the wrap catches + emits.
# ===========================================================================


def test_pipeline_pending_sweep_error_fires_when_sweep_raises(tmp_path, monkeypatch):
    """Branch emit-site (tick wrap, daemon.py:2411): when
    `_sweep_pipeline_pending` raises, `_tick`'s try/except catches the
    exception and emits `pipeline_pending_sweep_error` with
    `error="<type>: <msg>"` payload.

    Drives `_tick` end-to-end with the sweep stubbed to raise (the
    documented stub-point — `_sweep_pipeline_pending` is a daemon-module
    attribute). All other tick stages are stubbed to no-op success.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon._tick)
    assert '"pipeline_pending_sweep_error"' in src, (
        "regression: `daemon._tick` no longer emits "
        "pipeline_pending_sweep_error around the sweep stage"
    )
    assert "_sweep_pipeline_pending" in src, (
        "regression: `daemon._tick` no longer calls "
        "`_sweep_pipeline_pending` — pipeline-pending verification is "
        "structurally broken"
    )

    _stub_tick_quiet(monkeypatch)

    async def _boom_sweep(cfg, sdk):  # noqa: ARG001
        raise RuntimeError("sweep failed reading events.jsonl")

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _boom_sweep)

    sdk = _NoopSDK()
    asyncio.run(daemon._tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 50)
    errs = [e for e in evts if e["type"] == "pipeline_pending_sweep_error"]
    assert len(errs) == 1, evts
    err = errs[0]
    assert "RuntimeError" in err["error"]
    assert "sweep failed reading events.jsonl" in err["error"]


# ===========================================================================
# (4) auto_diagnose_error — emitted from `daemon._tick`'s outer try/except
# (line 2491-2495) around `_maybe_auto_diagnose`. Stub the watchdog to
# raise and drive `_tick`; the wrap catches + emits.
# ===========================================================================


def test_auto_diagnose_error_fires_when_maybe_auto_diagnose_raises(
    tmp_path, monkeypatch,
):
    """Branch emit-site (tick wrap, daemon.py:2494): when
    `_maybe_auto_diagnose` raises, `_tick`'s try/except catches the
    exception and emits `auto_diagnose_error` with
    `error="<type>: <msg>"` payload.

    Drives `_tick` end-to-end with the watchdog stubbed to raise (the
    documented stub-point — `_maybe_auto_diagnose` is a daemon-module
    attribute). All other tick stages stubbed to no-op success so the
    `auto_diagnose_error` is the ONLY error event the tick produces.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(daemon._tick)
    assert '"auto_diagnose_error"' in src, (
        "regression: `daemon._tick` no longer emits auto_diagnose_error "
        "around the idle-watchdog stage"
    )

    _stub_tick_quiet(monkeypatch)

    def _boom_diag(cfg, **kw):  # noqa: ARG001
        raise RuntimeError("watchdog failed building diagnose report")

    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", _boom_diag)

    sdk = _NoopSDK()
    asyncio.run(daemon._tick(cfg, sdk, mcp_server=None))

    evts = events.tail(cfg.events_file, 50)
    errs = [e for e in evts if e["type"] == "auto_diagnose_error"]
    assert len(errs) == 1, evts
    err = errs[0]
    assert "RuntimeError" in err["error"]
    assert "watchdog failed building diagnose report" in err["error"]


# ===========================================================================
# (5) classify_record_unreadable — emitted from `tools._apply_operator_op`'s
# `classify` branch (tools.py:2659 + 2667) when the proposal record file
# exists but `json.loads` raises (malformed JSON) OR the parsed payload
# is not a dict. Two branches → two tests.
# ===========================================================================


def _make_classify_args(tb_id: str, *, verdict: str = "advanced-goal") -> dict:
    """Build the queue record shape `_apply_operator_op` consumes for a
    classify op. Mirrors `do_operator_queue_append`'s record shape."""
    return {
        "op": "classify",
        "args": {
            "task_id": tb_id,
            "verdict": verdict,
            "reason": "test reason",
        },
    }


def test_classify_record_unreadable_on_malformed_json(tmp_path):
    """Happy emit-site (tools.py:2659): when the per-proposal record file
    EXISTS but `json.loads(...)` raises, `_apply_operator_op`'s classify
    branch catches the (OSError | JSONDecodeError) and emits
    `classify_record_unreadable` carrying `task` + `verdict` — then
    returns gracefully (no exception escapes to the caller).

    Real seam: `tools._apply_operator_op` is the daemon's drain-side
    dispatcher; this is the EXACT function the tick loop runs at L2339.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(tools._apply_operator_op)
    assert '"classify_record_unreadable"' in src, (
        "regression: tools._apply_operator_op no longer emits "
        "classify_record_unreadable on JSON parse failure"
    )

    tb_id = "TB-1900"
    # Seed the board so `_apply_operator_op` can find the task. The
    # classify branch doesn't actually move the task (verb is
    # metadata-only) but the queue-append handler validated existence
    # earlier — we mirror that precondition here.
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id=tb_id, title="shipped proposal")
    board.save()
    board = Board.load(cfg.tasks_file)

    # Write malformed JSON to the proposal record path so `json.loads`
    # raises JSONDecodeError on the read.
    record = tools.proposal_record_path(cfg, tb_id)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{not valid json")
    assert record.exists()

    # Real seam — drives the classify branch. Must NOT raise; the
    # try/except in tools.py:2654-2663 swallows the JSONDecodeError and
    # emits classify_record_unreadable instead.
    tools._apply_operator_op(cfg, board, _make_classify_args(tb_id))

    evts = events.tail(cfg.events_file, 50)
    unreadable = [e for e in evts if e["type"] == "classify_record_unreadable"]
    assert len(unreadable) == 1, evts
    evt = unreadable[0]
    assert evt["task"] == tb_id
    assert evt["verdict"] == "advanced-goal"
    # task_classified also fired (line 2636-2642 emits it BEFORE the
    # record amend attempts the read). The drain-side caller can rely
    # on task_classified being authoritative even when the per-proposal
    # record amend fails.
    classified = [e for e in evts if e["type"] == "task_classified"]
    assert len(classified) == 1 and classified[0]["task"] == tb_id


def test_classify_record_unreadable_on_non_dict_payload(tmp_path):
    """Branch emit-site (tools.py:2667): when the per-proposal record file
    exists AND parses as valid JSON but the top-level value is not a dict
    (e.g. a stray list or scalar written by a hand-edit), the classify
    branch emits `classify_record_unreadable` rather than trying to
    `record["impact"] = ...` against a non-dict (which would crash).

    Pins the defensive isinstance check. A refactor that drops the check
    would raise TypeError mid-drain — bad ergonomics for the operator.
    """
    cfg = _cfg(tmp_path)

    src = inspect.getsource(tools._apply_operator_op)
    assert "isinstance(record, dict)" in src, (
        "regression: tools._apply_operator_op no longer guards against "
        "non-dict proposal record payloads; a list/scalar JSON file "
        "would crash mid-drain instead of emitting "
        "classify_record_unreadable"
    )

    tb_id = "TB-1901"
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id=tb_id, title="shipped proposal")
    board.save()
    board = Board.load(cfg.tasks_file)

    # Write valid JSON whose top-level value is a list, not a dict.
    record = tools.proposal_record_path(cfg, tb_id)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(["not", "a", "dict"]))

    tools._apply_operator_op(cfg, board, _make_classify_args(tb_id))

    evts = events.tail(cfg.events_file, 50)
    unreadable = [e for e in evts if e["type"] == "classify_record_unreadable"]
    assert len(unreadable) == 1, evts
    evt = unreadable[0]
    assert evt["task"] == tb_id
    assert evt["verdict"] == "advanced-goal"
