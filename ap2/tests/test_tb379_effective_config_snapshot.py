"""TB-379: `ap2 status` must report the daemon's LIVE effective config,
not a locally re-resolved one.

Before TB-379, `ap2 status` rendered the `## Components` on/off + knob
lines by re-resolving config in the CLI process (this shell's env layered
over `.cc-autopilot/env`), NOT by reading what the running daemon
actually resolved. So status diverged from the daemon whenever the two
processes' environments differed.

Observed live 2026-06-08: a daemon launched from a shell that exported
`AP2_AUTO_APPROVE=1` kept auto-approve ARMED (env_reload's "existing env
vars win" rule means a later `.cc-autopilot/env` edit to `=0` can't
override the shell-pinned value). But `ap2 status`, run from a clean
shell, re-resolved the knob from the file and printed
`auto_approve: off (AP2_AUTO_APPROVE=0)` — flatly wrong about the daemon.

The fix: the daemon publishes a per-tick effective-config snapshot
(`effective_config.json`) recording its actually-resolved component/knob
state + its pid + a timestamp; `ap2 status` reads that snapshot for the
component/knob lines when the daemon is live, and falls back to a
clearly-labelled local re-resolution when it is not.

Pinned behavior:

  (a) Given a snapshot declaring `auto_approve` ENABLED with a LIVE pid,
      `ap2 status` reports auto-approve ON even when the local env/file
      resolves to OFF — proving status reads the daemon snapshot, not a
      local re-resolution (the exact divergence observed 2026-06-08).
  (b) With no snapshot, `ap2 status` falls back to local resolution and
      emits the "(daemon not running)" label.
  (c) With a snapshot whose pid is NOT running, same fallback + label.
  (d) The snapshot write/read round-trips the daemon's live resolution.
"""
from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import cli_daemon, daemon_state
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import default_registry


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Fresh ap2 project scaffold — same shape as TB-319."""
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _dead_pid() -> int:
    """A pid that is guaranteed NOT to be a running process."""
    candidate = 2_147_483_647
    while cli_daemon._is_running(candidate):
        candidate -= 1
    return candidate


def _components_from_text(out: str, known: set[str]) -> dict[str, bool]:
    """Parse the `## Components` text block → {name: enabled}."""
    enabled: dict[str, bool] = {}
    for line in out.splitlines():
        if line.startswith("  ") and ": " in line:
            head, rest = line.split(":", 1)
            name = head.strip()
            if name in known:
                token = rest.strip().split(" ", 1)[0]
                enabled[name] = token == "on"
    return enabled


# ===========================================================================
# (a) Live snapshot wins over local re-resolution — the core divergence.
# ===========================================================================


def test_status_reports_daemon_snapshot_over_local_env(
    cfg: Config, capsys, monkeypatch,
):
    """A live snapshot declaring `auto_approve` ON makes `ap2 status`
    report ON even though the LOCAL env resolves to OFF — proving status
    reads the daemon's snapshot, not a CLI-local env re-resolution.

    This is the exact 2026-06-08 divergence: the daemon was shell-pinned
    to `AP2_AUTO_APPROVE=1` while a clean-shell `ap2 status` re-resolved
    the file's `=0` and misreported `off`.
    """
    # Simulate the shell-pinned daemon: build + write the snapshot with
    # AP2_AUTO_APPROVE=1 (pid = this live test process). The snapshot
    # records auto_approve ENABLED.
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    snap = daemon_state.write_effective_config_snapshot(cfg)
    assert snap["pid"] == os.getpid()
    aa_snap = next(c for c in snap["components"] if c["name"] == "auto_approve")
    assert aa_snap["enabled"] is True

    # Now the operator's clean shell resolves the knob to OFF.
    monkeypatch.setenv("AP2_AUTO_APPROVE", "0")
    assert (
        default_registry().get("auto_approve").is_enabled() is False
    ), "local re-resolution must be OFF for the divergence to be meaningful"

    # JSON branch: reads the snapshot → auto_approve ON, source=daemon.
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    aa = next(c for c in payload["components"] if c["name"] == "auto_approve")
    assert aa["enabled"] is True, (
        "status must report the DAEMON's auto_approve=ON, not the local "
        f"env's OFF: {aa}"
    )
    assert payload["effective_config_source"] == "daemon"
    assert "auto_approve" in payload["effective_config_divergences"], (
        "the shell-pinned divergence should be surfaced"
    )

    # Text branch: `auto_approve: on (...)`.
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    known = {m.name for m in default_registry().components}
    text_enabled = _components_from_text(out, known)
    assert text_enabled.get("auto_approve") is True, out
    # No spurious "(daemon not running)" label on the live-snapshot path.
    assert "(daemon not running" not in out, out
    # The divergence hint is surfaced on the diverging component's line.
    aa_line = next(
        line for line in out.splitlines()
        if line.startswith("  auto_approve:")
    )
    assert "[daemon env pinned; local env disagrees]" in aa_line, aa_line


# ===========================================================================
# (b) No snapshot → local fallback + label.
# ===========================================================================


def test_no_snapshot_falls_back_to_local_with_label(
    cfg: Config, capsys, monkeypatch,
):
    """With no `effective_config.json`, `ap2 status` re-resolves locally
    and emits the `(daemon not running ...)` label so the divergence can
    never silently mislead."""
    assert not cfg.effective_config_file.exists()
    # Local resolution: auto_approve OFF.
    monkeypatch.setenv("AP2_AUTO_APPROVE", "0")

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Components" in out
    assert "(daemon not running" in out, out
    # The header line itself stays the bare `## Components` (so the
    # `grep -q "^## Components"` pin holds); the label is a separate line.
    assert any(line == "## Components" for line in out.splitlines()), out

    # JSON branch carries the provenance flag.
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective_config_source"] == "local"
    assert payload["effective_config_divergences"] == []


# ===========================================================================
# (c) Dead-pid snapshot → local fallback + label.
# ===========================================================================


def test_dead_pid_snapshot_falls_back_to_local_with_label(
    cfg: Config, capsys, monkeypatch,
):
    """A snapshot whose `pid` is not a running process is treated as
    stale — `ap2 status` falls back to local resolution + the label,
    NOT the snapshot's (possibly stale) component state."""
    # Hand-write a snapshot claiming auto_approve ON but under a DEAD pid.
    stale = {
        "pid": _dead_pid(),
        "ts": "2026-06-08T00:00:00Z",
        "version": "test",
        "components": [
            {
                "name": "auto_approve",
                "enabled": True,
                "env_flag": "AP2_AUTO_APPROVE",
                "env_flag_description": "AP2_AUTO_APPROVE=1",
                "default_enabled": False,
            }
        ],
    }
    cfg.effective_config_file.write_text(json.dumps(stale))

    # Local resolution says OFF; the dead-pid snapshot must NOT win.
    monkeypatch.setenv("AP2_AUTO_APPROVE", "0")

    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective_config_source"] == "local"
    aa = next(c for c in payload["components"] if c["name"] == "auto_approve")
    assert aa["enabled"] is False, (
        "a dead-pid snapshot must not override local resolution"
    )

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "(daemon not running" in out, out


# ===========================================================================
# (d) Snapshot helpers round-trip the daemon's live resolution.
# ===========================================================================


def test_snapshot_round_trips_live_resolution(cfg: Config, monkeypatch):
    """`write_effective_config_snapshot` records every discovered
    component with the daemon's live resolution; `load_*` reads it back
    intact, and the pid/ts/version envelope is present."""
    monkeypatch.setenv("AP2_JANITOR_DISABLED", "1")
    written = daemon_state.write_effective_config_snapshot(cfg)

    loaded = daemon_state.load_effective_config_snapshot(cfg)
    assert loaded == written
    assert loaded["pid"] == os.getpid()
    assert isinstance(loaded["ts"], str) and loaded["ts"].endswith("Z")
    assert "version" in loaded

    names = {c["name"] for c in loaded["components"]}
    assert names == {m.name for m in default_registry().components}
    janitor = next(c for c in loaded["components"] if c["name"] == "janitor")
    assert janitor["enabled"] is False, "AP2_JANITOR_DISABLED=1 → off"
    assert set(janitor.keys()) == {
        "name",
        "enabled",
        "env_flag",
        "env_flag_description",
        "default_enabled",
    }


def test_load_missing_or_malformed_snapshot_is_empty(cfg: Config):
    """A missing or corrupt snapshot reads back as `{}` (defensive parse)
    so `cmd_status` falls through to the labelled local fallback rather
    than raising."""
    assert daemon_state.load_effective_config_snapshot(cfg) == {}
    cfg.effective_config_file.write_text("{not json")
    assert daemon_state.load_effective_config_snapshot(cfg) == {}


# ===========================================================================
# Defensive shape — the per-tick + startup snapshot writes are wrapped so
# a write hiccup surfaces as an `effective_config_write_error` event
# rather than taking the daemon down. Pinned at the source level (mirrors
# TB-271's `test_tick_swallows_env_reload_exception`) — invoking `_tick`
# end-to-end would need the asyncio + SDK harness.
# ===========================================================================


def test_tick_writes_snapshot_and_swallows_write_error():
    """`_tick` writes the effective-config snapshot right after the env
    reload and wraps it in a try/except that emits
    `effective_config_write_error` (never re-raises) so a filesystem
    hiccup can't crash the tick."""
    import inspect

    from ap2 import daemon

    src = inspect.getsource(daemon._tick)
    assert "write_effective_config_snapshot(cfg)" in src
    assert "effective_config_write_error" in src
    pos = src.find("write_effective_config_snapshot(cfg)")
    after = src[pos:pos + 800]
    assert "except Exception" in after
    assert "events.append" in after


def test_daemon_start_writes_snapshot():
    """`main_loop` publishes the snapshot once at startup (before the
    first tick) so `ap2 status` is truthful from the moment the pid file
    exists, with the same defensive `effective_config_write_error`
    swallow."""
    import inspect

    from ap2 import daemon

    src = inspect.getsource(daemon.main_loop)
    assert "write_effective_config_snapshot(cfg)" in src
    assert "effective_config_write_error" in src
