"""TB-350: real-SDK smoke-check routine + cron dispatch.

`ap2.smoke_runner.run_smoke_check` runs the live-API smoke suite
(`ap2/tests/smoke/`) on a 6h cron schedule instead of on every task — the
per-task verification gate dropped the smokes on 2026-05-30 because
transient live-service blips false-failed unrelated tasks (TB-345/346).

These tests stub the subprocess boundary (NO real SDK call is made here)
and the Mattermost `_mm_post` shim, then pin:

  (a) exit 0            → `smoke_check_passed` event + ZERO Mattermost posts.
  (b) non-zero exit     → `smoke_check_failed` event carrying the failure
                          tail + EXACTLY ONE Mattermost alert that carries
                          the same tail.
  (c) `AP2_REAL_SDK` unset → `smoke_check_skipped` event + NO subprocess
                          spawned (inert-by-default, never run paid calls
                          on installs that haven't opted in).

Plus a dispatch test that `daemon.run_cron` routes a
`job.name == "real-sdk-smoke"` job to `run_smoke_check` (bookended by
`cron_start` / `cron_complete` and advancing `cron_state`), mirroring the
janitor / status-report branches rather than the generic agent-prompt
path.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from ap2 import events, smoke_runner, tools
from ap2.config import Config
from ap2.cron import CronJob
# TB-381: `run_cron` (the cron per-job dispatcher that routes
# `real-sdk-smoke` to the smoke routine) moved into the cron scheduler
# component (`ap2/components/cron/`).
from ap2.components.cron import run_cron
# TB-389: the smoke runner's failure alert is now event-driven — it
# enqueues onto the `ap2.notify` queue, and the communication
# component's outbound tick delivers it. Tests drain the queue via
# `run_outbound_tick` then assert on the captured `_mm_post` calls.
from ap2.components.communication import run_outbound_tick


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _project(tmp_path: Path) -> Config:
    """Minimal ap2 project scaffold — no git needed (the smoke routine
    shells out to pytest, not git)."""
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


class _FakeCompleted:
    """Stand-in for `subprocess.CompletedProcess`."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_subprocess(monkeypatch, *, result=None, raises=None):
    """Replace `smoke_runner.subprocess.run` with a recorder.

    Returns the `calls` list (one entry — the captured kwargs — per
    invocation) so tests can assert spawn count + cwd + the command.
    """
    calls: list[dict] = []

    def _fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(smoke_runner.subprocess, "run", _fake_run)
    return calls


def _patch_mm_post(monkeypatch):
    """Record `tools._mm_post` calls. The `MattermostChannelAdapter`
    routes through it, so enabling the component (`AP2_MM_CHANNELS` set)
    + this stub captures every outbound alert without a live HTTP call."""
    posts: list[dict] = []

    def _fake_mm_post(channel: str, text: str, thread_id: str = "") -> str:
        posts.append({"channel": channel, "text": text, "thread_id": thread_id})
        return "post-123"

    monkeypatch.setattr(tools, "_mm_post", _fake_mm_post)
    return posts


def _types_of(cfg: Config) -> list[str]:
    return [e.get("type") for e in events.tail(cfg.events_file, 50)]


def _events_of(cfg: Config, typ: str) -> list[dict]:
    return [e for e in events.tail(cfg.events_file, 50) if e.get("type") == typ]


# ---------------------------------------------------------------------------
# (c) inert-by-default: AP2_REAL_SDK unset → skip, no subprocess.
# ---------------------------------------------------------------------------


def test_skips_and_spawns_nothing_when_real_sdk_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("AP2_REAL_SDK", raising=False)
    cfg = _project(tmp_path)
    calls = _patch_subprocess(monkeypatch, result=_FakeCompleted(0))
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))

    assert len(_events_of(cfg, "smoke_check_skipped")) == 1
    assert _events_of(cfg, "smoke_check_passed") == []
    assert _events_of(cfg, "smoke_check_failed") == []
    # The whole point of inert-by-default: NO paid subprocess spawned.
    assert calls == []
    assert posts == []


def test_skips_for_falsey_real_sdk_value(tmp_path, monkeypatch):
    """An operator who wrote `AP2_REAL_SDK=0` to disable the job gets the
    inert path, not a paid run."""
    monkeypatch.setenv("AP2_REAL_SDK", "0")
    cfg = _project(tmp_path)
    calls = _patch_subprocess(monkeypatch, result=_FakeCompleted(0))

    asyncio.run(smoke_runner.run_smoke_check(cfg))

    assert len(_events_of(cfg, "smoke_check_skipped")) == 1
    assert calls == []


# ---------------------------------------------------------------------------
# (a) exit 0 → smoke_check_passed, no Mattermost post.
# ---------------------------------------------------------------------------


def test_pass_emits_passed_event_and_no_post(tmp_path, monkeypatch):
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")
    cfg = _project(tmp_path)
    calls = _patch_subprocess(
        monkeypatch,
        result=_FakeCompleted(0, stdout="5 passed in 12.3s\n"),
    )
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))

    passed = _events_of(cfg, "smoke_check_passed")
    assert len(passed) == 1
    assert "duration_s" in passed[0]
    assert _events_of(cfg, "smoke_check_failed") == []
    # No "smokes OK" noise — the pass record lives in events.jsonl only.
    assert posts == []
    # The subprocess ran in the project root with the documented command.
    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "uv", "run", "--extra", "dev", "pytest", "-q", "ap2/tests/smoke/",
    ]
    assert calls[0]["cwd"] == str(cfg.project_root)
    # Bounded by the verify timeout so a hung SDK can't stall the tick.
    assert calls[0]["timeout"] == cfg.verify_timeout_s


# ---------------------------------------------------------------------------
# (b) non-zero exit → smoke_check_failed + exactly one MM alert w/ tail.
# ---------------------------------------------------------------------------


def test_failure_emits_failed_event_and_one_alert_with_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    cfg = _project(tmp_path)
    failure_text = (
        "FAILED ap2/tests/smoke/test_prose_judge_real_sdk.py::test_judge "
        "- AssertionError: verdict mismatch\n1 failed, 4 passed in 30.1s\n"
    )
    _patch_subprocess(
        monkeypatch,
        result=_FakeCompleted(1, stdout=failure_text),
    )
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))
    # TB-389: the alert is enqueued; deliver it via the communication tick.
    run_outbound_tick(cfg)

    failed = _events_of(cfg, "smoke_check_failed")
    assert len(failed) == 1
    assert failed[0]["exit_code"] == 1
    assert failed[0]["reason"] == "nonzero_exit"
    # The failure tail names the failing test.
    assert "test_prose_judge_real_sdk" in failed[0]["failure_tail"]
    assert _events_of(cfg, "smoke_check_passed") == []

    # EXACTLY ONE Mattermost alert, carrying the same failure tail.
    assert len(posts) == 1
    assert posts[0]["channel"] == "test-channel-id"
    assert "test_prose_judge_real_sdk" in posts[0]["text"]
    assert "FAILED" in posts[0]["text"]


def test_failure_alert_prefers_report_channel(tmp_path, monkeypatch):
    """The alert routes to `AP2_MM_REPORT_CHANNEL` when set, mirroring the
    status-report routine's channel preference."""
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "poll-channel")
    monkeypatch.setenv("AP2_MM_REPORT_CHANNEL", "report-channel")
    cfg = _project(tmp_path)
    _patch_subprocess(monkeypatch, result=_FakeCompleted(2, stdout="boom\n"))
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))
    run_outbound_tick(cfg)  # TB-389: deliver the enqueued alert.

    assert len(posts) == 1
    assert posts[0]["channel"] == "report-channel"


def test_timeout_emits_failed_event_with_timeout_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("AP2_REAL_SDK", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "test-channel-id")
    cfg = _project(tmp_path)
    _patch_subprocess(
        monkeypatch,
        raises=subprocess.TimeoutExpired(
            cmd=smoke_runner._SMOKE_CMD,
            timeout=cfg.verify_timeout_s,
            output="partial output before hang\n",
        ),
    )
    posts = _patch_mm_post(monkeypatch)

    asyncio.run(smoke_runner.run_smoke_check(cfg))
    run_outbound_tick(cfg)  # TB-389: deliver the enqueued alert.

    failed = _events_of(cfg, "smoke_check_failed")
    assert len(failed) == 1
    assert failed[0]["reason"] == "timeout"
    # One alert fires on the timeout path too.
    assert len(posts) == 1
    assert "TIMED OUT" in posts[0]["text"]


# ---------------------------------------------------------------------------
# Cron dispatch: run_cron routes `real-sdk-smoke` to the routine.
# ---------------------------------------------------------------------------


def test_cron_dispatch_routes_real_sdk_smoke_to_routine(tmp_path, monkeypatch):
    """`run_cron` with a `real-sdk-smoke` job must call
    `smoke_runner.run_smoke_check` (NOT `_run_control_agent`), bookend the
    run with `cron_start` / `cron_complete` (job=real-sdk-smoke), and
    advance `cron_state[real-sdk-smoke].last_run`."""
    cfg = _project(tmp_path)

    called = {"n": 0, "cfg": None}

    async def _fake_run_smoke_check(passed_cfg):
        called["n"] += 1
        called["cfg"] = passed_cfg

    monkeypatch.setattr(smoke_runner, "run_smoke_check", _fake_run_smoke_check)

    # If the generic agent path were taken instead, this sentinel SDK's
    # `query` would be invoked — assert it never is.
    class _SentinelSDK:
        def __init__(self):
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

    sdk = _SentinelSDK()
    job = CronJob(
        name="real-sdk-smoke", interval_s=6 * 3600, prompt="(stub)", max_turns=1,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    assert called["n"] == 1
    assert called["cfg"] is cfg
    assert sdk.called is False

    import json

    types = _types_of(cfg)
    assert ("cron_start" in types) and ("cron_complete" in types)
    bookends = [
        e for e in events.tail(cfg.events_file, 50)
        if e.get("type") in ("cron_start", "cron_complete")
        and e.get("job") == "real-sdk-smoke"
    ]
    assert len(bookends) == 2
    state = json.loads(cfg.cron_state_file.read_text())
    assert "real-sdk-smoke" in state and state["real-sdk-smoke"] > 0
