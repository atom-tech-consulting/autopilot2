"""Daemon startup state file (TB-139 / TB-260).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split.
The orchestrator (`main_loop`) calls `_emit_daemon_start` once at boot;
this module owns the per-process state stash the CLI reads from a
separate process via `cmd_status`.

  - `_emit_daemon_start`: append the `daemon_start` event with version
    + pid, AND capture the `.cc-autopilot/env` mtime baseline.
  - `_capture_env_mtime_at_start`: stash env-file mtime into
    `daemon_state.json` so a CLI in a separate process can WARN when
    the operator bumped a knob since the daemon last loaded the file.
  - `_load_daemon_state` / `_save_daemon_state`: defensive JSON
    read/write for `daemon_state.json`.

Re-exported from `ap2/daemon.py` so existing test paths
(`daemon._emit_daemon_start`, `daemon._capture_env_mtime_at_start`)
continue to resolve unchanged.
"""
from __future__ import annotations

import json
import os

from . import events
from .config import Config


def _emit_daemon_start(cfg: Config) -> dict:
    """Append the `daemon_start` event with the current source version (TB-139).

    Stamps the running source revision so a post-mortem can correlate
    state-file mutations with the exact commit the daemon was loading.
    Editable installs (the common case here) get `<base>+<sha>.<ts>`;
    released wheels get just the base version.

    Extracted so the daemon's startup-event shape is unit-testable without
    spinning up the full `main_loop` (which is async + needs the SDK).

    TB-260: also captures the `.cc-autopilot/env` mtime at startup into
    `daemon_state.json` so the CLI's `cmd_status` (a separate process)
    can compare the live env file mtime against the value pinned at
    startup and emit a WARN line when the operator has bumped a knob
    since the daemon last loaded the file. The capture lives here (not
    in `main_loop`) so the unit-testable startup hook is the single
    source-of-truth for "what state was pinned at boot"; the value
    survives across the daemon's lifetime until the next `daemon_start`
    overwrites it on restart.
    """
    from . import get_version

    _capture_env_mtime_at_start(cfg)
    return events.append(
        cfg.events_file, "daemon_start",
        pid=os.getpid(), version=get_version(),
    )


def _capture_env_mtime_at_start(cfg: Config) -> None:
    """Stash the `.cc-autopilot/env` mtime at daemon-start time into
    `daemon_state.json` (TB-260).

    On a fresh project where the env file doesn't exist yet, we still
    write `env_file_mtime_at_start: null` so the read-side helper can
    distinguish "daemon never captured" from "captured but env file
    absent". The CLI's stale-detection treats a null at-start mtime as
    "never stale" — there's nothing to compare against, so the WARN
    line stays silent until an env file is created AND the daemon is
    restarted to pin a baseline.

    File errors (read-only filesystem, etc.) are best-effort silent so
    a startup hiccup on the state file can't take the daemon down. The
    surface stays silent if the capture fails; an operator notices the
    missing WARN line on a knob bump and can re-trigger a restart.
    """
    try:
        mtime: float | None = (
            cfg.env_file.stat().st_mtime if cfg.env_file.exists() else None
        )
    except OSError:
        mtime = None
    state = _load_daemon_state(cfg)
    state["env_file_mtime_at_start"] = mtime
    try:
        _save_daemon_state(cfg, state)
    except OSError:
        # Best-effort — the daemon still starts; surfaces stay silent
        # for this lifetime.
        pass


def _load_daemon_state(cfg: Config) -> dict:
    """Read `daemon_state.json` (TB-260) → dict, or `{}` if missing /
    malformed.

    Parallel to `_load_diagnose_state` — both are per-process state
    stashes the daemon writes at lifecycle hooks and the CLI reads from
    a separate process. Defensive parse: a truncated or hand-edited file
    returns `{}` rather than raising so `cmd_status` never fails on a
    state-file blip.
    """
    if not cfg.daemon_state_file.exists():
        return {}
    try:
        data = json.loads(cfg.daemon_state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_daemon_state(cfg: Config, state: dict) -> None:
    """Write `daemon_state.json` (TB-260). Parents created on demand
    (parallel to `_save_diagnose_state`)."""
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_state_file.write_text(
        json.dumps(state, indent=2, sort_keys=True),
    )
