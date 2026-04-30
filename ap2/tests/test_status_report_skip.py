"""TB-128: skip-if-idle gate for the status-report cron.

The status-report cron has historically posted reports with stale headline
timestamps when nothing changed between runs (the agent re-rendered text
from a prior context's cache). The fix has two layers:

1. The prompt builder injects a deterministic `## Current state` block
   with a fresh UTC `now:` timestamp and binds the status-report job to
   "use that value verbatim" (covered in `test_prompts.py`).
2. The daemon's `run_cron` short-circuits the agent invocation entirely
   when no events of interest have happened since the last
   `cron_complete job=status-report` — covered here.

Both layers are belt-and-braces: the agent could still ignore the prompt
contract, but the daemon-level gate prevents an SDK turn from being
burned in the first place when there's nothing new to report.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events
from ap2.config import Config
from ap2.cron import CronJob
from ap2.daemon import _status_report_should_skip, run_cron


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_skip_returns_false_when_never_run(tmp_path):
    """First run ever (or last run rolled out of the tail) — never skip."""
    cfg = _cfg(tmp_path)
    # Empty events log: no prior cron_complete to anchor against.
    assert _status_report_should_skip(cfg) is False


def test_skip_returns_true_when_only_self_events_after_last_complete(tmp_path):
    """Back-to-back run: previous cron_complete is in the tail and the only
    events since are self-bookkeeping (cron_start/cron_complete for
    status-report itself, the agent's own status_report log_event, and the
    outbound mattermost_reply that quoted the report headline). Nothing of
    substance happened — skip.
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_start", job="status-report")
    events.append(
        cfg.events_file, "mattermost_reply",
        channel="ap2",
        summary="**Autopilot Status Report** — 2026-04-30T10:00Z\n• ...",
    )
    events.append(
        cfg.events_file, "status_report",
        summary="Posted to #ap2: idle since last report.",
    )
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # Nothing else happens before the next run fires.
    assert _status_report_should_skip(cfg) is True


def test_skip_returns_false_when_task_completed_since_last_run(tmp_path):
    """A `task_complete` event between runs is exactly the kind of activity
    the status report is supposed to surface — must not skip."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_start", job="status-report")
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-99", status="complete", commit="deadbee",
        summary="did the thing",
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_returns_false_when_pipeline_event_since_last_run(tmp_path):
    """Pipeline activity is interesting — must not skip."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "pipeline_complete",
        task="TB-50", name="data-fetch", pid=12345,
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_returns_false_when_verification_failed_since_last_run(tmp_path):
    """Verification failures are interesting — must not skip."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "verification_failed",
        task="TB-77", overall="fail",
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_filters_self_mattermost_reply_by_summary(tmp_path):
    """The cron's own outbound `mattermost_reply` (summary starts with
    "**Autopilot Status Report**") is self-noise — must not count as
    activity. A non-self mattermost_reply (e.g. handler answering the
    operator) IS activity.
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # Self-noise: the cron's outbound headline post.
    events.append(
        cfg.events_file, "mattermost_reply",
        channel="ap2",
        summary="**Autopilot Status Report** — 2026-04-30T10:00Z",
    )
    assert _status_report_should_skip(cfg) is True

    # Now a non-self mattermost_reply (handler responding to operator) —
    # this IS interesting activity.
    events.append(
        cfg.events_file, "mattermost_reply",
        channel="ap2",
        summary="Pausing the daemon as requested.",
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_filters_other_status_report_log_events(tmp_path):
    """Self-emitted `status_report` log_events between cron_completes (e.g.
    a prior skipped run) must not register as activity."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "status_report",
        summary="skipped: no activity since last report",
    )
    events.append(cfg.events_file, "cron_skipped",
                  job="status-report", reason="no_activity_since_last_report")
    assert _status_report_should_skip(cfg) is True


# ---------------------------------------------------------------------------
# Integration: run_cron honors the skip gate end-to-end.


class _NoopSDK:
    """SDK stub that records whether `query` was called."""

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


def test_run_cron_skips_status_report_when_idle(tmp_path):
    """run_cron must short-circuit (no SDK call, but `cron_skipped` event
    + cron_state mark) when the gate says skip."""
    cfg = _cfg(tmp_path)
    # Seed a prior cron_complete with no follow-up activity so the gate
    # returns True.
    events.append(cfg.events_file, "cron_complete", job="status-report")

    sdk = _NoopSDK()
    job = CronJob(
        name="status-report", interval_s=60, prompt="post a report",
        max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    assert sdk.called is False, "skipped run must not invoke the SDK"

    # Skip event landed; no cron_start / cron_complete from this aborted run.
    evts = events.tail(cfg.events_file, 50)
    skipped = [e for e in evts if e.get("type") == "cron_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["job"] == "status-report"
    assert skipped[0]["reason"] == "no_activity_since_last_report"

    # cron_state was advanced so the daemon doesn't re-fire every tick.
    import json

    state = json.loads(cfg.cron_state_file.read_text())
    assert "status-report" in state and state["status-report"] > 0


def test_run_cron_does_not_skip_when_activity_present(tmp_path, monkeypatch):
    """run_cron must NOT skip when meaningful activity has happened since
    the last status report — and must reach the SDK invocation path.

    We stub the SDK with a no-op generator (returns immediately) so the
    test doesn't depend on real Claude wiring; the assertion is that
    `query` was reached at all. We also patch the prompt builder out so
    the test doesn't need a real `Bash` for `git log` (the helper handles
    a non-git tmp_path, but the safe.directory subprocess invocation in
    a CI sandbox is not worth the surface area for this assertion).
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    monkeypatch.setattr(
        "ap2.daemon.prompts.build_control_prompt",
        lambda cfg, name, body: "stub prompt",
    )

    sdk = _NoopSDK()
    job = CronJob(
        name="status-report", interval_s=60, prompt="post a report",
        max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    assert sdk.called is True, "active state should reach the SDK"
    evts = events.tail(cfg.events_file, 50)
    kinds = [e["type"] for e in evts]
    assert "cron_start" in kinds
    assert "cron_complete" in kinds
    assert "cron_skipped" not in kinds


# ---------------------------------------------------------------------------
# cron.default.yaml prompt content must encode the freshness contract.


def test_default_status_report_prompt_pins_freshness_contract():
    """The bootstrap default for `status-report` must spell out the
    headline-timestamp / fresh-read / skip-if-idle rules so new projects
    don't inherit the old stale-text behavior. Operators with
    pre-existing cron.yaml files keep their copy until they re-bootstrap;
    the daemon-side `## Current state` block + skip gate cover them
    regardless.
    """
    from ap2.cron import load_jobs

    default = (
        Path(__file__).resolve().parent.parent / "cron.default.yaml"
    )
    jobs = {j.name: j for j in load_jobs(default)}
    sr = jobs["status-report"]
    body = sr.prompt
    # Headline timestamp pin.
    assert "Freshness contract" in body
    assert "`now:` value" in body
    # Re-read pin.
    assert "events.jsonl" in body
    assert "TASKS.md" in body
    # Skip-if-idle pin.
    assert "SKIP" in body
    assert "status_report" in body
