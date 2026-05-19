"""TB-260: behavioral pinning for the stale-`.cc-autopilot/env` surface
on `ap2 status` (text + JSON), the cron status-report digest, and the
watchdog `auto_diagnose_fired` summary.

TB-255 hit a `verification_failed` at `duration_s=600.01s` on
2026-05-18T17:38Z against the old 600s default, ~26h after
`AP2_VERIFY_TIMEOUT_S` had been bumped to 1800s in the env file. The
daemon hadn't restarted in between, so the in-memory `Config` still
held the old 600s ceiling. `retry_exhausted` → Frozen → operator
manually unfroze → re-ran cleanly.

This module pins the operator-surface fix:

  (a) fresh daemon-start with env unchanged → no WARN line in `ap2 status`
      text output, `env_stale: false` in JSON.
  (b) env file touched after daemon start → WARN line present in text
      output naming both timestamps + the restart remediation.
  (c) same condition surfaces `env_stale: true` field in `ap2 status
      --json` output, plus `env_file_mtime` / `env_file_mtime_at_start`
      iso strings.
  (d) `collect_env_staleness(cfg)` returns the documented shape across
      the three stale-conditions.
  (e) `render_env_staleness_section(state)` omits the sub-block when
      not stale.
  (f) `render_env_staleness_section(state)` emits the header + bullet
      with both timestamps + `ap2 stop && ap2 start` nudge when stale.
  (g) the cron status-report routine threads the rendered sub-block
      through `state_extras` so the agent forwards it verbatim
      (parallel to TB-228 / TB-244 / TB-245 / TB-258 / TB-259 wiring
      tests).
  (h) the watchdog `diagnose.render_markdown` emits a one-line
      `env-stale: yes (modified <ts>)` reminder when applicable
      (omitted on a healthy daemon — pre-TB-260 byte-identical).
  (i) the daemon's `_capture_env_mtime_at_start` writes the mtime
      into `daemon_state.json` so a fresh process (the CLI) reads
      the same value.
  (j) `_STATUS_REPORT_CONTRACT` in `ap2/prompts.py` enumerates the new
      env-staleness sub-block (verbatim-forwarding contract pin).
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import time
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import automation_status, diagnose, events
from ap2.config import Config
from ap2.init import init_project
from ap2.status_report import (
    render_env_staleness_section,
    run_status_report,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Initialized project. The env file is NOT created by `init_project`;
    individual tests create it explicitly so the missing-file branch
    can be exercised separately."""
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _write_env_file(cfg: Config, *, content: str = "AP2_VERIFY_TIMEOUT_S=1800\n") -> None:
    cfg.env_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.env_file.write_text(content)


def _pin_env_mtime_at_start(cfg: Config, *, mtime: float) -> None:
    """Write `daemon_state.json` with a specific `env_file_mtime_at_start`.

    Mirrors what `daemon._capture_env_mtime_at_start` does at boot, but
    with a caller-controlled mtime so tests can drive the
    `current > at_start` comparison deterministically without sleeping
    or mutating the env file's timestamp via `os.utime` (which is
    flaky across filesystems with low-resolution mtimes).
    """
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_state_file.write_text(
        _json.dumps({"env_file_mtime_at_start": mtime}, indent=2),
    )


class _NoopSDK:
    """SDK stub: records `query` was called, returns an empty async gen.

    Mirrors TB-258's `_NoopSDK`. The routine still needs
    `ClaudeAgentOptions` on the instance even though these tests
    assert against `state_extras` rather than the SDK call site.
    """

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


# ===========================================================================
# (a) Fresh daemon-start with env unchanged → no WARN line; JSON env_stale false.
# ===========================================================================


def test_status_text_omits_warn_line_when_env_not_stale(
    cfg: Config, capsys, monkeypatch,
):
    """Daemon captured the env file mtime at startup and the file
    hasn't been modified since → the `WARN:` line MUST NOT appear in
    `ap2 status` text. Pin the omit-on-empty rule so a healthy daemon
    stays byte-identical to the pre-TB-260 output (load-bearing
    default-off regression pin — mirrors TB-258's `audit:` line omit
    rule).
    """
    from ap2.cli import cmd_status

    for name in (
        "AP2_AUTO_APPROVE",
        "AP2_AUTO_APPROVE_DRY_RUN",
        "AP2_AUTO_UNFREEZE_DRY_RUN",
        "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    # Daemon captures the SAME mtime → at-start == current → not stale.
    _pin_env_mtime_at_start(cfg, mtime=env_mtime)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" not in out, (
        f"WARN: line must be omitted on a healthy (non-stale) daemon; out={out!r}"
    )
    assert ".cc-autopilot/env modified" not in out, out


def test_status_json_carries_env_stale_false_when_fresh(
    cfg: Config, capsys, monkeypatch,
):
    """`ap2 status --json` ALWAYS carries the `env_stale` /
    `env_file_mtime` / `env_file_mtime_at_start` keys (parser stability
    contract — mirrors `auto_approve` / `audit` promise). On a healthy
    daemon `env_stale` is False and both iso timestamps reflect the
    same value (file unchanged since capture)."""
    from ap2.cli import cmd_status

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime)

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert "env_stale" in payload, payload
    assert payload["env_stale"] is False
    assert "env_file_mtime" in payload, payload
    assert "env_file_mtime_at_start" in payload, payload
    # Both should be iso-formatted strings (not None / not float).
    assert isinstance(payload["env_file_mtime"], str)
    assert isinstance(payload["env_file_mtime_at_start"], str)


def test_status_json_carries_env_stale_false_when_no_baseline(
    cfg: Config, capsys, monkeypatch,
):
    """Fresh project: daemon hasn't run yet, so `daemon_state.json`
    is absent and there's no baseline to compare against. `env_stale`
    MUST be False (we can't know if the file is stale relative to a
    non-existent baseline — surfaces stay silent). Both iso fields
    are None. Pin so a cold-start daemon doesn't trip a spurious
    warn."""
    from ap2.cli import cmd_status

    # No env file, no daemon_state.json → both surfaces None / False.
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["env_stale"] is False
    assert payload["env_file_mtime"] is None
    assert payload["env_file_mtime_at_start"] is None


# ===========================================================================
# (b) Env file touched after daemon start → WARN line in text output.
# ===========================================================================


def test_status_text_emits_warn_line_when_env_stale(
    cfg: Config, capsys, monkeypatch,
):
    """Env file mtime > daemon-start mtime → the `WARN:` line MUST
    appear in `ap2 status` text output. The line must carry: both
    timestamps (current modified-at + daemon-start at), the project-
    relative path `.cc-autopilot/env`, and the restart remediation
    `ap2 stop && ap2 start` so the operator can copy-paste the verb.
    Pins the load-bearing message shape from the briefing."""
    from ap2.cli import cmd_status

    for name in (
        "AP2_AUTO_APPROVE",
        "AP2_AUTO_APPROVE_DRY_RUN",
        "AP2_AUTO_UNFREEZE_DRY_RUN",
        "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    # Pin the daemon-start mtime to 10s BEFORE the current file mtime —
    # simulates the operator bumping a knob 10s after daemon start.
    _pin_env_mtime_at_start(cfg, mtime=env_mtime - 10.0)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN:" in out, out
    assert ".cc-autopilot/env modified" in out, out
    assert "after daemon start at" in out, out
    assert "ap2 stop && ap2 start" in out, out


# ===========================================================================
# (c) JSON surfaces env_stale: true + both timestamps.
# ===========================================================================


def test_status_json_surfaces_env_stale_true_when_stale(
    cfg: Config, capsys, monkeypatch,
):
    """Env file modified after daemon-start mtime → `ap2 status --json`
    carries `env_stale: true`. Both iso fields are populated; the live
    `env_file_mtime` is strictly later than `env_file_mtime_at_start`
    when compared as iso strings (same lexicographic order as epoch
    arithmetic for the daemon's `%Y-%m-%dT%H:%M:%SZ` format)."""
    from ap2.cli import cmd_status

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime - 60.0)

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["env_stale"] is True
    assert payload["env_file_mtime"] is not None
    assert payload["env_file_mtime_at_start"] is not None
    # Lexical order matches iso ordering for the daemon's iso format.
    assert payload["env_file_mtime"] > payload["env_file_mtime_at_start"]


# ===========================================================================
# (d) `collect_env_staleness` returns the documented shape.
# ===========================================================================


def test_collect_env_staleness_shape_fresh_project(cfg: Config):
    """No env file + no daemon_state.json → helper returns all-None /
    False shape with the three documented keys. Pin the exact key set
    so a refactor that drops a key blows the renderer + JSON wiring
    up at runtime (not silently)."""
    state = automation_status.collect_env_staleness(cfg)
    assert set(state.keys()) == {
        "env_stale",
        "env_file_mtime",
        "env_file_mtime_at_start",
    }
    assert state["env_stale"] is False
    assert state["env_file_mtime"] is None
    assert state["env_file_mtime_at_start"] is None


def test_collect_env_staleness_shape_healthy_daemon(cfg: Config):
    """Env file present + daemon-state mtime == current mtime →
    `env_stale` False, both iso timestamps populated and equal."""
    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime)

    state = automation_status.collect_env_staleness(cfg)
    assert state["env_stale"] is False
    assert state["env_file_mtime"] == state["env_file_mtime_at_start"]


def test_collect_env_staleness_shape_stale(cfg: Config):
    """Env file mtime > daemon-state mtime → `env_stale` True,
    both timestamps populated, current strictly later than at-start."""
    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime - 60.0)

    state = automation_status.collect_env_staleness(cfg)
    assert state["env_stale"] is True
    assert state["env_file_mtime"] > state["env_file_mtime_at_start"]


def test_collect_env_staleness_not_stale_when_no_baseline(cfg: Config):
    """Env file exists but daemon_state.json is missing →
    `env_stale` MUST stay False (no baseline to compare against). Pin
    the cold-start safety: the operator restarting a daemon for the
    first time on an existing env file shouldn't see a spurious WARN."""
    _write_env_file(cfg)
    # Don't write daemon_state.json — daemon never captured.
    state = automation_status.collect_env_staleness(cfg)
    assert state["env_stale"] is False
    assert state["env_file_mtime"] is not None  # the env file exists
    assert state["env_file_mtime_at_start"] is None  # no baseline


# ===========================================================================
# (e) renderer omits sub-block when not stale.
# ===========================================================================


def test_renderer_returns_empty_list_when_env_not_stale():
    """Non-stale env → renderer returns `[]` (omit-on-empty rule
    pinned at the source). Load-bearing default-off byte-identical
    pin so the pre-TB-260 digest stays unchanged on healthy daemons."""
    state = {
        "env_stale": False,
        "env_file_mtime": "2026-05-18T12:00:00Z",
        "env_file_mtime_at_start": "2026-05-18T12:00:00Z",
    }
    lines = render_env_staleness_section(state)
    assert lines == [], (
        f"section must be omitted when env not stale; got: {lines!r}"
    )


def test_renderer_returns_empty_list_when_no_baseline():
    """No baseline (daemon_state.json absent) → `env_stale` False →
    renderer returns `[]`. Pin the cold-start digest safety."""
    state = {
        "env_stale": False,
        "env_file_mtime": "2026-05-18T12:00:00Z",
        "env_file_mtime_at_start": None,
    }
    lines = render_env_staleness_section(state)
    assert lines == []


# ===========================================================================
# (f) renderer happy-path emits header + bullet with both timestamps.
# ===========================================================================


def test_renderer_emits_header_and_bullet_when_env_stale():
    """`env_stale: True` → renderer emits `[heading, bullet]` with
    the live mtime, the daemon-start mtime, and the `ap2 stop && ap2
    start` nudge. Pin the exact shape so the agent's verbatim-
    forwarding contract holds (parallel to TB-258's renderer shape
    pin)."""
    state = {
        "env_stale": True,
        "env_file_mtime": "2026-05-18T17:38:00Z",
        "env_file_mtime_at_start": "2026-05-17T15:12:00Z",
    }
    lines = render_env_staleness_section(state)
    assert len(lines) == 2, lines
    assert "*Daemon env file stale (restart required):*" == lines[0]
    assert "2026-05-18T17:38:00Z" in lines[1]
    assert "2026-05-17T15:12:00Z" in lines[1]
    assert "ap2 stop && ap2 start" in lines[1]
    assert ".cc-autopilot/env modified" in lines[1]


# ===========================================================================
# (g) cron status-report threads sub-block through state_extras.
# ===========================================================================


def test_run_status_report_injects_env_staleness_into_state_extras(
    tmp_path, monkeypatch,
):
    """Stale env → the routine appends the rendered sub-block to
    `state_extras` so the rendered prompt's `## Current state` block
    carries it for the agent to forward verbatim. Pin the wiring
    path so a refactor that drops the call site (or threads it
    through a different parameter) trips here (parallel to TB-258
    wiring test).
    """
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime - 60.0)

    # task_complete so the skip-gate doesn't fire on the routine entry.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "*Daemon env file stale (restart required):*" in joined, joined
    assert ".cc-autopilot/env modified" in joined, joined
    assert "ap2 stop && ap2 start" in joined, joined


def test_run_status_report_omits_env_staleness_section_when_healthy(
    tmp_path, monkeypatch,
):
    """No stale env (current mtime == at-start) → the routine does
    NOT append the sub-block to `state_extras`. Pins the omit-on-empty
    rule at the wiring level so env-staleness stays as quiet as
    TB-258 does on a fully-reviewed window. Load-bearing default-off
    byte-identical regression pin."""
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime)  # healthy

    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture)

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    joined = "\n".join(captured["extras"])
    assert "Daemon env file stale" not in joined, (
        f"env-staleness sub-block must not appear on a healthy daemon; "
        f"extras={captured['extras']!r}"
    )


# ===========================================================================
# (h) watchdog `diagnose.render_markdown` surfaces stale-env state.
# ===========================================================================


def test_diagnose_render_markdown_emits_env_stale_line_when_applicable(
    cfg: Config,
):
    """`build_report` collects env-staleness state into the
    DiagnoseReport; `render_markdown` emits a one-line `env-stale: yes
    (modified <ts>)` reminder when applicable. Pin the load-bearing
    line shape so the `auto_diagnose_fired` event's `report_summary`
    carries the restart nudge."""
    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime - 60.0)

    report = diagnose.build_report(cfg)
    md = diagnose.render_markdown(report)
    assert "env-stale: yes" in md, md
    assert "ap2 stop && ap2 start" in md, md


def test_diagnose_render_markdown_omits_env_stale_line_when_healthy(
    cfg: Config,
):
    """Healthy daemon (no stale env) → the env-stale line MUST NOT
    appear in the watchdog summary. Pre-TB-260 byte-identical
    regression pin so a healthy daemon's diagnose stays unchanged."""
    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime
    _pin_env_mtime_at_start(cfg, mtime=env_mtime)  # healthy

    report = diagnose.build_report(cfg)
    md = diagnose.render_markdown(report)
    assert "env-stale" not in md, md


# ===========================================================================
# (i) daemon `_capture_env_mtime_at_start` writes to daemon_state.json.
# ===========================================================================


def test_capture_env_mtime_at_start_writes_to_daemon_state(cfg: Config):
    """Daemon helper writes the env file's current mtime into
    `daemon_state.json` under `env_file_mtime_at_start`. The CLI
    (a separate process) reads the same value via
    `collect_env_staleness`. Pin the cross-process contract — without
    this, the CLI never sees the baseline and `env_stale` would always
    return False even after operator edits."""
    from ap2.daemon import _capture_env_mtime_at_start

    _write_env_file(cfg)
    env_mtime = cfg.env_file.stat().st_mtime

    _capture_env_mtime_at_start(cfg)
    assert cfg.daemon_state_file.exists()
    data = _json.loads(cfg.daemon_state_file.read_text())
    assert "env_file_mtime_at_start" in data
    # Should match the file's mtime exactly (epoch float).
    assert data["env_file_mtime_at_start"] == pytest.approx(env_mtime)


def test_capture_env_mtime_at_start_writes_null_when_no_env_file(
    cfg: Config,
):
    """No env file at startup → daemon helper writes `null` under
    `env_file_mtime_at_start` so the read-side can distinguish
    'daemon captured but file absent' from 'daemon never captured'."""
    from ap2.daemon import _capture_env_mtime_at_start

    _capture_env_mtime_at_start(cfg)
    data = _json.loads(cfg.daemon_state_file.read_text())
    assert data["env_file_mtime_at_start"] is None


# ===========================================================================
# (j) `_STATUS_REPORT_CONTRACT` carries the new env-staleness clause.
# ===========================================================================


def test_status_report_contract_in_prompts_carries_env_staleness_clause():
    """The `_STATUS_REPORT_CONTRACT` addendum in `ap2/prompts.py`
    teaches the agent to forward the `*Daemon env file stale (restart
    required):*` sub-block VERBATIM. Pin the load-bearing markers so
    a paraphrase that drops the contract trips here (parallel to
    TB-228 / TB-244 / TB-245 / TB-258 / TB-259 prompt-contract pins).
    """
    import inspect

    from ap2 import prompts
    src = inspect.getsource(prompts)
    assert "Daemon env file stale (restart required)" in src
    assert "TB-260" in src
    assert "VERBATIM" in src
    # The literal `"env_stale"` token must appear so machine consumers
    # parsing the contract see the JSON-side field name.
    assert "env_stale" in src


# ===========================================================================
# Structural pins — implementation-symbol grep (briefing verifier).
# ===========================================================================


def test_implementation_symbol_lives_in_non_test_code():
    """Briefing verifier:
    `grep -rE 'env_file_mtime|env_stale' ap2/ --include='*.py' | grep -v test_`
    must match at least one line. Pin that both implementation symbols
    live in non-test code so a refactor that accidentally moves them
    into tests-only doesn't pass the verifier silently."""
    ap2_root = Path(__file__).resolve().parent.parent
    found_in_non_test = False
    for py_file in ap2_root.rglob("*.py"):
        if py_file.name.startswith("test_") or "tests/" in str(py_file):
            continue
        text = py_file.read_text()
        if "env_file_mtime" in text or "env_stale" in text:
            found_in_non_test = True
            break
    assert found_in_non_test, (
        "expected at least one non-test .py file under ap2/ to reference "
        "env_file_mtime or env_stale"
    )


def test_automation_status_declares_collect_env_staleness():
    """`grep -q "def collect_env_staleness" ap2/automation_status.py`
    structural pin: the collector is declared at module level so
    `from ap2 import automation_status; automation_status.collect_env_staleness`
    works for the CLI + cron wiring + tests."""
    from ap2 import automation_status as _mod
    src = Path(_mod.__file__).read_text()
    assert "def collect_env_staleness(" in src


def test_status_report_declares_render_env_staleness_section():
    """`grep -q "def render_env_staleness_section" ap2/status_report.py`
    structural pin: the renderer is declared at module level so the
    wiring + tests can import it directly."""
    from ap2 import status_report as _mod
    src = Path(_mod.__file__).read_text()
    assert "def render_env_staleness_section(" in src
