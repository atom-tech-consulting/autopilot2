"""TB-419: adapter-provided default model tiers (heavy + light per backend).

Each agent backend adapter declares two default model tiers at the provider
boundary: a HEAVY tier (for the primary agents) and a LIGHT tier (for the
cost-sensitive sub-calls). The call sites route through the SELECTED adapter's
tier when their own model config is unset, instead of hard-coding a
provider-specific model string or relying on the backend's opaque native
default:

  - `ClaudeCodeAdapter` → heavy `claude-opus-4-8` / light `claude-sonnet-4-6`.
  - `CodexAdapter`      → heavy `gpt-5.5`        / light `gpt-5.4-mini`.

  - Primary-agent dispatch (task / ideation / cron / status_report /
    mattermost) builds `cfg.get_core_value("agent_model") or
    select_adapter(kind, cfg).default_model_heavy` — unset → the resolved
    adapter's HEAVY tier; an explicit `agent_model` override still wins.
  - The validator judge and ideation scrub resolve to the selected adapter's
    LIGHT tier (replacing the hard-coded `_VALIDATOR_JUDGE_MODEL =
    "claude-haiku-4-5"` and TB-418's scrub call-site backend-string-match).

Gate-runnable (no real SDK / credential): pins the tier declarations and the
per-call-site resolution with no live backend.
"""
from __future__ import annotations

import os

import pytest

from ap2 import ideation_scrub
from ap2.adapters import ClaudeCodeAdapter, CodexAdapter, select_adapter
from ap2.briefing_validators import _validator_judge_model
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` knob so each test owns its env surface — a true
    DEFAULT config where only the schema default governs the resolution."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Fresh project + stripped env → a default config. `clean_env` runs first
    (it's a dependency) so neither the host env nor the scaffolded
    `.cc-autopilot/env` template leaks an override into the resolution."""
    init_project(tmp_path)
    return Config.load(tmp_path)


def _dispatch_heavy_model(kind: str, cfg) -> str:
    """The model the PRIMARY-agent dispatch sites resolve, built the way the
    daemon's `run_task` / `_run_control_agent` build it post-TB-419:
    `cfg.get_core_value("agent_model") or select_adapter(kind, cfg)
    .default_model_heavy`. Pins the REAL resolution expression, not a literal."""
    return cfg.get_core_value("agent_model") or select_adapter(
        kind, cfg
    ).default_model_heavy


# --- (1) adapter tier declarations ------------------------------------------


def test_claude_adapter_declares_heavy_and_light_tiers():
    a = ClaudeCodeAdapter()
    assert a.default_model_heavy == "claude-opus-4-8"
    assert a.default_model_light == "claude-sonnet-4-6"


def test_codex_adapter_declares_heavy_and_light_tiers():
    a = CodexAdapter()
    assert a.default_model_heavy == "gpt-5.5"
    assert a.default_model_light == "gpt-5.4-mini"


def test_tiers_are_distinct_per_adapter():
    """Heavy and light are different ids within each backend (the whole point —
    a primary agent and a cost-sensitive sub-call run on different models)."""
    for a in (ClaudeCodeAdapter(), CodexAdapter()):
        assert a.default_model_heavy != a.default_model_light


# --- (2) primary-agent dispatch resolves to the selected adapter's heavy ----


def test_primary_dispatch_unset_model_resolves_to_claude_heavy(cfg, clean_env):
    """Default (claude) backend + unset `agent_model` → the Claude adapter's
    HEAVY tier (`claude-opus-4-8`)."""
    clean_env.setenv("AP2_AGENT_BACKEND_TASK", "claude")
    assert cfg.get_core_value("agent_model") is None
    assert _dispatch_heavy_model("task", cfg) == "claude-opus-4-8"
    assert _dispatch_heavy_model("task", cfg) == ClaudeCodeAdapter().default_model_heavy


def test_primary_dispatch_unset_model_resolves_to_codex_heavy(cfg, clean_env):
    """Codex-routed `task` kind + unset `agent_model` → the Codex adapter's
    HEAVY tier (`gpt-5.5`), NOT a Claude id and NOT an omitted kwarg."""
    clean_env.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    assert isinstance(select_adapter("task", cfg), CodexAdapter)
    resolved = _dispatch_heavy_model("task", cfg)
    assert resolved == "gpt-5.5"
    assert not resolved.startswith("claude")


def test_control_surfaces_resolve_to_heavy(cfg, clean_env):
    """The shared control-agent kinds (ideation / cron / status_report /
    mattermost) are PRIMARY agents → heavy tier under the default map."""
    for kind in ("ideation", "cron", "status_report", "mattermost"):
        assert _dispatch_heavy_model(kind, cfg) == "claude-opus-4-8"


def test_explicit_agent_model_override_wins_over_heavy_tier(cfg, clean_env):
    """An explicit `agent_model` (sectioned env) wins over the adapter heavy
    tier — even pinning a Claude id onto a codex-routed kind (the operator-pin
    caveat). The tier is only the UNSET fallback."""
    clean_env.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    clean_env.setenv("AP2_CORE_AGENT_MODEL", "operator-pinned-model")
    assert _dispatch_heavy_model("task", cfg) == "operator-pinned-model"


# --- (3) validator judge + ideation scrub resolve to the selected light -----


def test_validator_judge_resolves_to_claude_light(cfg, clean_env):
    clean_env.setenv("AP2_AGENT_BACKEND_VALIDATOR_JUDGE", "claude")
    assert _validator_judge_model(cfg) == "claude-sonnet-4-6"
    assert _validator_judge_model(cfg) == ClaudeCodeAdapter().default_model_light


def test_validator_judge_resolves_to_codex_light(cfg, clean_env):
    """The leak TB-419 fixes: a codex-routed judge runs on `gpt-5.4-mini`, not
    the old hard-coded `claude-haiku-4-5` it would hand to codex verbatim."""
    clean_env.setenv("AP2_AGENT_BACKEND_VALIDATOR_JUDGE", "codex")
    resolved = _validator_judge_model(cfg)
    assert resolved == "gpt-5.4-mini"
    assert not resolved.startswith("claude")


def test_validator_judge_cfg_less_seam_uses_default_claude_light():
    """The `_judge_dep_coherence_default` cfg-less seam falls back to the
    default Claude adapter's light tier (matching the all-`claude` default)."""
    assert _validator_judge_model(None) == ClaudeCodeAdapter().default_model_light


def test_ideation_scrub_resolves_to_claude_light(cfg, clean_env):
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "claude")
    assert ideation_scrub._resolved_model(cfg) == "claude-sonnet-4-6"
    assert (
        ideation_scrub._resolved_model(cfg)
        == ClaudeCodeAdapter().default_model_light
    )


def test_ideation_scrub_resolves_to_codex_light(cfg, clean_env):
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "codex")
    resolved = ideation_scrub._resolved_model(cfg)
    assert resolved == "gpt-5.4-mini"
    assert not resolved.startswith("claude")


def test_explicit_scrub_model_override_wins_over_light_tier(cfg, clean_env):
    """An explicit `ideation_scrub_model` override wins over the adapter light
    tier — the tier governs only the UNSET case."""
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "codex")
    clean_env.setenv("AP2_CORE_IDEATION_SCRUB_MODEL", "operator-pinned-scrub")
    assert ideation_scrub._resolved_model(cfg) == "operator-pinned-scrub"
