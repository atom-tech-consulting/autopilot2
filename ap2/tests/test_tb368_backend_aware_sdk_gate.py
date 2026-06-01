"""TB-368: backend-aware daemon-start SDK-availability gate.

`daemon.main_loop` no longer imports `claude_agent_sdk` unconditionally at
startup. It resolves the per-kind backend map (via the shared
`ap2.adapters.referenced_backends` helper) and only requires/loads the Claude
SDK when at least one kind resolves to `claude` — mirroring the credential
gate (`cli_daemon._require_oauth_token`). A pure-codex map therefore starts
without `claude_agent_sdk` installed, while the all-claude default still
hard-fails when the SDK is missing (behavior parity for every current
operator).

The gate lives in `daemon._load_claude_sdk_if_referenced(cfg)`, which
`main_loop` calls in place of the old unconditional `_import_sdk_or_die()` +
`load_claude_sdk()` pair.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import daemon
from ap2.adapters.select import AGENT_KINDS
from ap2.tests.conftest import _project


def _clear_agent_backend_env(monkeypatch) -> None:
    """Drop every `AP2_AGENT_BACKEND_<KIND>` override so a test's cfg reads the
    all-claude default unless it sets an override explicitly."""
    for kind in AGENT_KINDS:
        monkeypatch.delenv(f"AP2_AGENT_BACKEND_{kind.upper()}", raising=False)


def _make_sdk_import_fail(monkeypatch) -> None:
    """Force `ap2.adapters.load_claude_sdk` — the single SDK-load point both the
    gate and `_import_sdk_or_die` resolve through (`from .adapters import
    load_claude_sdk`) — to raise `ImportError`, as if `claude_agent_sdk` were
    not installed."""
    def _boom():
        raise ImportError("No module named 'claude_agent_sdk'")

    monkeypatch.setattr("ap2.adapters.load_claude_sdk", _boom)


def test_pure_codex_map_skips_sdk_import(tmp_path: Path, monkeypatch):
    """Briefing core guarantee: with every kind resolving to codex, the gate
    skips the Claude SDK import entirely and returns None — even when
    `load_claude_sdk` would raise `ImportError`. No `SystemExit`; a pure-codex
    install starts cleanly."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    for kind in AGENT_KINDS:
        monkeypatch.setenv(f"AP2_AGENT_BACKEND_{kind.upper()}", "codex")
    _make_sdk_import_fail(monkeypatch)

    # Proceeds WITHOUT raising SystemExit (and never even calls the failing
    # load_claude_sdk — that would have surfaced as SystemExit via the gate's
    # _import_sdk_or_die fallthrough).
    sdk = daemon._load_claude_sdk_if_referenced(cfg)
    assert sdk is None


def test_all_claude_default_still_dies_without_sdk(tmp_path: Path, monkeypatch):
    """The all-claude default still hard-fails (`sys.exit(1)`) when the SDK is
    missing — zero observable change for current operators."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    _make_sdk_import_fail(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        daemon._load_claude_sdk_if_referenced(cfg)
    assert exc.value.code != 0


def test_all_claude_default_loads_sdk_handle(tmp_path: Path, monkeypatch):
    """When claude is referenced and the SDK imports fine, the gate returns the
    loaded handle — the injected-SDK seam threaded to `run_task` /
    `_run_control_agent` / `status_report.configure`."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    sentinel = object()
    monkeypatch.setattr("ap2.adapters.load_claude_sdk", lambda: sentinel)

    assert daemon._load_claude_sdk_if_referenced(cfg) is sentinel


def test_mixed_map_loads_sdk_handle(tmp_path: Path, monkeypatch):
    """A mixed map (one codex kind, the rest claude) still references claude, so
    the SDK is still imported — the hermetic injected-SDK seam keeps working for
    mixed/all-claude configs."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    sentinel = object()
    monkeypatch.setattr("ap2.adapters.load_claude_sdk", lambda: sentinel)

    assert daemon._load_claude_sdk_if_referenced(cfg) is sentinel


def test_referenced_backends_resolves_per_kind_set(tmp_path: Path, monkeypatch):
    """The shared helper the gate and credential gate agree on: all-claude →
    {"claude"}, all-codex → {"codex"}, mixed → both."""
    from ap2.adapters import referenced_backends

    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    assert referenced_backends(cfg) == {"claude"}

    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    assert referenced_backends(cfg) == {"claude", "codex"}

    for kind in AGENT_KINDS:
        monkeypatch.setenv(f"AP2_AGENT_BACKEND_{kind.upper()}", "codex")
    assert referenced_backends(cfg) == {"codex"}
