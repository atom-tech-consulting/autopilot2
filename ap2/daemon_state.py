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

import datetime as _dt
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


# ---------------------------------------------------------------------------
# TB-379: effective-config snapshot — the daemon publishes what IT actually
# resolved so `ap2 status` (a separate process) reports the daemon's live
# config instead of re-resolving env locally.
# ---------------------------------------------------------------------------


def build_effective_config_snapshot(cfg: Config, *, registry=None) -> dict:
    """Build the daemon's effective-config snapshot dict (TB-379).

    Resolves every discovered component's enabled-state + env-flag value
    against the daemon's LIVE process env (`os.environ`) — the same env
    the in-daemon web UI reads correctly. This is what makes the snapshot
    truthful where a CLI-local re-resolution diverges: the daemon's
    `os.environ` carries any shell-pinned knob (`AP2_AUTO_APPROVE=1`
    exported into the daemon's launch shell) that a later
    `.cc-autopilot/env` edit could NOT override (env_reload's "existing
    env vars win" rule). A `ap2 status` running from a clean shell would
    re-resolve the file's `=0` and misreport; reading this snapshot fixes
    that.

    Shape (stable for the CLI reader + JSON consumers):

        {
          "pid": <int>,            # writing daemon's pid (liveness probe)
          "ts": "<iso8601 Z>",     # write timestamp
          "version": "<str>",      # running source version
          "components": [
            {
              "name": str,
              "enabled": bool,                # daemon-resolved on/off
              "env_flag": str | None,
              "env_flag_description": str,     # daemon-resolved knob value
              "default_enabled": bool,
            },
            ...
          ],
        }

    `registry` defaults to `default_registry()`; injectable for tests.
    """
    if registry is None:
        from .registry import default_registry

        registry = default_registry()
    from . import get_version

    components = [
        {
            "name": m.name,
            # Resolve against the daemon's live os.environ (env=None) —
            # the truthful effective state the web UI already shows.
            "enabled": m.is_enabled(),
            "env_flag": m.env_flag,
            "env_flag_description": m.env_flag_description(),
            "default_enabled": m.default_enabled,
        }
        for m in registry.components
    ]
    return {
        "pid": os.getpid(),
        "ts": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "version": get_version(),
        "components": components,
    }


def write_effective_config_snapshot(cfg: Config, *, registry=None) -> dict:
    """Atomically write the effective-config snapshot (TB-379).

    Called from the daemon tick (after `env_reload` so the snapshot
    reflects the freshly-reloaded env) and once at daemon start. Writes
    via a temp file + `os.replace` so a `ap2 status` read can never see a
    half-written JSON. Best-effort: a file-system hiccup is swallowed by
    the caller's try/except — a missing/stale snapshot just sends
    `ap2 status` down the labelled local-fallback path, never crashes the
    daemon.

    Returns the snapshot dict it wrote (for caller logging / tests).
    """
    snapshot = build_effective_config_snapshot(cfg, registry=registry)
    cfg.effective_config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.effective_config_file.with_suffix(
        cfg.effective_config_file.suffix + ".tmp"
    )
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    os.replace(tmp, cfg.effective_config_file)
    return snapshot


def load_effective_config_snapshot(cfg: Config) -> dict:
    """Read `effective_config.json` (TB-379) → dict, or `{}` if missing /
    malformed.

    Defensive parse (mirrors `_load_daemon_state`): a truncated or
    hand-edited file returns `{}` so `cmd_status` falls through to the
    labelled local-resolution fallback rather than raising.
    """
    if not cfg.effective_config_file.exists():
        return {}
    try:
        data = json.loads(cfg.effective_config_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}
