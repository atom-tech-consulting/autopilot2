"""Tests for per-agent-kind backend selection (TB-358 / goal.md axis 5).

Covers the selection surface axis 5 ships: `Config.get_agent_backend`
(env override > `[agent_backends]` table > all-`claude` default) and the
`ap2.adapters.select.select_adapter` resolver that maps a kind to a
concrete `ClaudeCodeAdapter` / `CodexAdapter` instance. The backend-aware
auth-gate half lives in `test_cli_daemon.py` (it imports the same
`_require_oauth_token` the TB-79 tests pin).
"""
from __future__ import annotations

import os
from pathlib import Path

from ap2.adapters import ClaudeCodeAdapter, CodexAdapter
from ap2.adapters.select import AGENT_KINDS, select_adapter
from ap2.config import CONFIG_TOML_FILE, DEFAULT_AGENT_BACKEND, Config
from ap2.tests.conftest import _project


def _clear_backend_env(monkeypatch) -> None:
    """Drop every `AP2_AGENT_BACKEND_<KIND>` override so a test reads the
    config-snapshot / default precedence cleanly regardless of the ambient
    shell env."""
    for kind in AGENT_KINDS:
        monkeypatch.delenv(f"AP2_AGENT_BACKEND_{kind.upper()}", raising=False)


def test_default_resolves_every_kind_to_claude(tmp_path: Path, monkeypatch):
    """With no `[agent_backends]` table and no env overrides, every agent
    kind resolves to the Claude backend — an all-default install behaves
    exactly as it did before the adapter layer existed."""
    _clear_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    assert DEFAULT_AGENT_BACKEND == "claude"
    for kind in AGENT_KINDS:
        assert cfg.get_agent_backend(kind) == "claude"
        adapter = select_adapter(kind, cfg)
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter.backend == "claude"


def test_env_override_resolves_task_to_codex(tmp_path: Path, monkeypatch):
    """`AP2_AGENT_BACKEND_TASK=codex` flips the `task` kind to the codex
    backend — `select_adapter` returns a `CodexAdapter` — while every other
    kind stays on claude (per-kind, not per-daemon, selection)."""
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    cfg = _project(tmp_path)

    assert cfg.get_agent_backend("task") == "codex"
    task_adapter = select_adapter("task", cfg)
    assert isinstance(task_adapter, CodexAdapter)
    assert task_adapter.backend == "codex"

    # Sibling kinds are untouched — selection is fixed per kind.
    for other in ("ideation", "status_report", "verifier_judge"):
        assert cfg.get_agent_backend(other) == "claude"
        assert isinstance(select_adapter(other, cfg), ClaudeCodeAdapter)


def test_unknown_kind_falls_back_to_claude(tmp_path: Path, monkeypatch):
    """A kind not in `AGENT_KINDS` (no env override, no table entry) resolves
    to claude via the `DEFAULT_AGENT_BACKEND` fallback — selection never
    hard-fails to a missing backend."""
    _clear_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    assert cfg.get_agent_backend("does_not_exist") == "claude"
    adapter = select_adapter("does_not_exist", cfg)
    assert isinstance(adapter, ClaudeCodeAdapter)


def test_config_snapshot_table_resolves_without_env(tmp_path: Path, monkeypatch):
    """The `[agent_backends]` snapshot (`cfg.agent_backends_config`) drives
    selection when no env override is live — the file-table path an operator
    who edits `config.toml` reads from."""
    _clear_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    cfg.agent_backends_config = {"ideation": "codex"}
    assert cfg.get_agent_backend("ideation") == "codex"
    assert isinstance(select_adapter("ideation", cfg), CodexAdapter)
    # Unmapped kind in a partial table still defaults to claude.
    assert isinstance(select_adapter("task", cfg), ClaudeCodeAdapter)


def test_env_override_wins_over_config_snapshot(tmp_path: Path, monkeypatch):
    """Env override beats the `[agent_backends]` snapshot — call-time
    env-first precedence, matching `get_core_value` / `get_component_value`."""
    _clear_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    cfg.agent_backends_config = {"task": "claude"}
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    assert cfg.get_agent_backend("task") == "codex"
    assert isinstance(select_adapter("task", cfg), CodexAdapter)


def test_unknown_backend_id_degrades_to_claude(tmp_path: Path, monkeypatch):
    """An unrecognized backend id (operator typo) resolves to the Claude
    adapter rather than crashing dispatch — `select_adapter`'s default."""
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "claud")  # typo
    cfg = _project(tmp_path)
    # The raw resolved id is returned verbatim by the config read...
    assert cfg.get_agent_backend("task") == "claud"
    # ...but the resolver degrades an unknown id to the Claude adapter.
    assert isinstance(select_adapter("task", cfg), ClaudeCodeAdapter)


def test_agent_backends_table_loads_from_config_toml(tmp_path: Path, monkeypatch):
    """The `[agent_backends]` TOML table is stashed on
    `cfg.agent_backends_config` by `config_loader.from_toml` and drives
    `get_agent_backend` / `select_adapter` end-to-end — the file-path the
    operator who edits `config.toml` exercises."""
    # Strip ambient AP2_* env so the TOML snapshot is observable in isolation.
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / ".cc-autopilot").mkdir(exist_ok=True)
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / CONFIG_TOML_FILE).write_text(
        '[agent_backends]\ntask = "codex"\nideation = "claude"\n'
    )
    cfg = Config.from_toml(tmp_path / CONFIG_TOML_FILE)
    assert cfg.agent_backends_config == {"task": "codex", "ideation": "claude"}
    assert cfg.get_agent_backend("task") == "codex"
    assert isinstance(select_adapter("task", cfg), CodexAdapter)
    assert isinstance(select_adapter("ideation", cfg), ClaudeCodeAdapter)
    # A kind absent from the partial table still defaults to claude.
    assert cfg.get_agent_backend("cron") == "claude"
