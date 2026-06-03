"""TB-369: backend-aware daemon-start codex-handle-availability gate.

The symmetric mirror of TB-368's Claude-SDK availability gate, for the codex
backend. `daemon.main_loop` already gates the Claude SDK import behind the
resolved per-kind backend set (`ap2.adapters.referenced_backends`), but the
codex side was missing: `CodexAdapter` lazily imports its handle (`import
openai_codex`) only at first dispatch, so a pure- or mixed-codex map with
`OPENAI_API_KEY` set but `openai_codex` NOT installed passed both existing
daemon-start gates, started cleanly, then hard-failed with a cryptic
`ImportError` deep in the first codex run.

`daemon._require_codex_handle_if_referenced(cfg)` closes that edge case:
resolve the per-kind backend map via the same shared `referenced_backends`
helper the Claude gate and the credential gate agree on, and probe the codex
handle (`ap2.adapters.load_codex_sdk` — the single relocated `import
openai_codex` point both the adapter and this gate resolve through) ONLY when at
least one kind resolves to `codex`. When codex is referenced but the handle is
not importable, the gate prints an actionable remediation and `sys.exit(1)`s.
An all-claude map skips the probe entirely — zero behavior change for current
operators.
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


def _make_codex_import_fail(monkeypatch) -> None:
    """Force `ap2.adapters.load_codex_sdk` — the single codex-handle-load point
    both the gate and `CodexAdapter._get_codex` resolve through (`from
    .adapters import load_codex_sdk`) — to raise `ImportError`, as if
    `openai_codex` were not installed."""
    def _boom():
        raise ImportError("No module named 'openai_codex'")

    monkeypatch.setattr("ap2.adapters.load_codex_sdk", _boom)


def test_pure_codex_map_dies_without_codex_handle(tmp_path: Path, monkeypatch):
    """Briefing core guarantee (mirror of
    `test_all_claude_default_still_dies_without_sdk`): a pure-codex backend map
    (every `AP2_AGENT_BACKEND_<KIND>=codex`) with the codex handle import forced
    to fail raises `SystemExit` with a non-zero code from the daemon-start
    codex-availability gate."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    for kind in AGENT_KINDS:
        monkeypatch.setenv(f"AP2_AGENT_BACKEND_{kind.upper()}", "codex")
    _make_codex_import_fail(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        daemon._require_codex_handle_if_referenced(cfg)
    assert exc.value.code != 0


def test_mixed_map_dies_without_codex_handle(tmp_path: Path, monkeypatch):
    """A mixed map (one codex kind, the rest claude) still references codex, so
    the codex handle is still required — the gate fires when codex is in the
    resolved set even alongside claude kinds."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    _make_codex_import_fail(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        daemon._require_codex_handle_if_referenced(cfg)
    assert exc.value.code != 0


def test_all_claude_default_unaffected_when_codex_unavailable(
    tmp_path: Path, monkeypatch
):
    """The all-claude default (no `AP2_AGENT_BACKEND_*` overrides) does NOT
    raise from the codex-availability gate even when the codex handle is
    unavailable — current operators are unaffected because the resolved backend
    set never contains `"codex"`, so the probe is skipped entirely."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    # Even with the codex handle forced to fail, the gate must short-circuit
    # before probing it (it never calls the failing load_codex_sdk).
    _make_codex_import_fail(monkeypatch)

    # Returns None without raising SystemExit.
    assert daemon._require_codex_handle_if_referenced(cfg) is None


def test_codex_referencing_map_with_handle_available_does_not_raise(
    tmp_path: Path, monkeypatch
):
    """Happy path: a codex-referencing map with the codex handle importable does
    NOT raise from the codex-availability gate — the probe succeeds and the
    daemon proceeds to the tick loop."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    for kind in AGENT_KINDS:
        monkeypatch.setenv(f"AP2_AGENT_BACKEND_{kind.upper()}", "codex")
    sentinel = object()
    monkeypatch.setattr("ap2.adapters.load_codex_sdk", lambda: sentinel)

    assert daemon._require_codex_handle_if_referenced(cfg) is None


def test_gate_keyed_off_resolved_backend_set(tmp_path: Path, monkeypatch):
    """The gate consults `ap2.adapters.referenced_backends(cfg)` to decide
    whether to probe — symmetric to the Claude-SDK gate. When codex is absent
    from the resolved set the codex-handle load is NEVER called (proving the
    probe is keyed off the backend set, not an unconditional import); when codex
    is present it IS called."""
    from ap2.adapters import referenced_backends

    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    assert "codex" not in referenced_backends(cfg)

    calls: list[int] = []

    def _tracking_load():
        calls.append(1)
        return object()

    monkeypatch.setattr("ap2.adapters.load_codex_sdk", _tracking_load)

    # All-claude: the probe is skipped, load_codex_sdk is never called.
    daemon._require_codex_handle_if_referenced(cfg)
    assert calls == []

    # Flip one kind to codex: now codex is in the resolved set, so the gate
    # probes the handle exactly once.
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    assert "codex" in referenced_backends(cfg)
    daemon._require_codex_handle_if_referenced(cfg)
    assert calls == [1]
