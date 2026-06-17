"""TB-418: ideation default-tuning + provider-aware scrub model.

Gate-runnable (no real SDK / credential) regression pin for the
operator-directed defaults pass. Two cleavages:

  (1) The three numeric ideation schema defaults are bumped to their new
      baseline — trigger-task-count 3 → 10, cooldown 7200 → 3600s,
      max-turns 100 → 200. Pinned both at the schema layer
      (`CORE_CONFIG_SCHEMA[...].default`) and end-to-end through a default
      config's `get_core_value` resolution.

  (2) `ideation_scrub._resolved_model` is provider-aware: with no operator
      override the unset fallback resolves by the `ideation_scrub` kind's
      backend (`get_agent_backend("ideation_scrub")` — the SAME selector the
      scrub dispatcher uses) — `claude-haiku-4-5-20251001` under a
      Claude-backed kind, `gpt-5.4-mini` under a Codex-backed one. The scrub
      is a cost-floor canary, so each branch is the cheap model for its
      provider (not the backend's full default). An explicit operator value
      still wins regardless of backend; the provider-aware fallback only
      governs the UNSET case.

Why this matters: pre-TB-418 the scrub default was a fixed Claude string, so
a Codex-routed project handed `claude-haiku-4-5` to Codex and had to override
it by hand. The provider-aware default fixes that for every Codex project out
of the box.
"""
from __future__ import annotations

import os

import pytest

from ap2 import ideation_scrub
from ap2.config import Config
from ap2.core_config_schema import CORE_CONFIG_SCHEMA
from ap2.ideation_scrub import DEFAULT_SCRUB_MODEL, DEFAULT_SCRUB_MODEL_CODEX
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


# --- (1) numeric defaults ---------------------------------------------------


def test_numeric_schema_defaults_are_the_new_baseline():
    """The three numeric ideation schema defaults are the TB-418 baseline."""
    assert CORE_CONFIG_SCHEMA["ideation_trigger_task_count"].default == 10
    assert CORE_CONFIG_SCHEMA["ideation_cooldown_s"].default == 3600
    assert CORE_CONFIG_SCHEMA["ideation_max_turns"].default == 200


def test_numeric_defaults_resolve_via_cfg_under_default_config(cfg):
    """A default config (no env / TOML override) resolves each numeric knob to
    its new baseline through `get_core_value` — the path `ap2 config get`
    and the runtime readers take."""
    assert cfg.get_core_value("ideation_trigger_task_count") == 10
    assert cfg.get_core_value("ideation_cooldown_s") == 3600
    assert cfg.get_core_value("ideation_max_turns") == 200


# --- (2) provider-aware scrub model -----------------------------------------


def test_scrub_schema_default_is_unset():
    """The scrub-model schema default is empty (`""`), NOT a fixed `claude-*`
    string — the empty default is what hands resolution to the provider-aware
    fallback in `_resolved_model`."""
    spec = CORE_CONFIG_SCHEMA["ideation_scrub_model"]
    assert spec.default == ""
    assert not (
        isinstance(spec.default, str) and spec.default.startswith("claude")
    )


def test_scrub_model_resolves_to_haiku_under_claude_backend(cfg, clean_env):
    """Unset override + Claude-backed `ideation_scrub` kind → the cheap Claude
    scrub model (`claude-haiku-4-5-20251001`)."""
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "claude")
    assert cfg.get_agent_backend("ideation_scrub") == "claude"
    assert ideation_scrub._resolved_model(cfg) == DEFAULT_SCRUB_MODEL
    assert ideation_scrub._resolved_model(cfg) == "claude-haiku-4-5-20251001"


def test_scrub_model_defaults_to_haiku_when_backend_unset(cfg, clean_env):
    """No `[agent_backends]` mapping at all → the all-claude default backend →
    claude-haiku. Provider-awareness isn't a codex-only special case."""
    assert cfg.get_agent_backend("ideation_scrub") == "claude"
    assert ideation_scrub._resolved_model(cfg) == DEFAULT_SCRUB_MODEL


def test_scrub_model_resolves_to_gpt_mini_under_codex_backend(cfg, clean_env):
    """Unset override + Codex-backed `ideation_scrub` kind → the cheap Codex
    scrub model (`gpt-5.4-mini`), NOT the Claude id — the leak the
    provider-aware default avoids out of the box."""
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "codex")
    assert cfg.get_agent_backend("ideation_scrub") == "codex"
    resolved = ideation_scrub._resolved_model(cfg)
    assert resolved == DEFAULT_SCRUB_MODEL_CODEX
    assert resolved == "gpt-5.4-mini"
    assert not resolved.startswith("claude"), (
        f"a claude-* id leaked into a codex-routed scrub: {resolved!r}"
    )


def test_explicit_override_wins_over_provider_aware_default_claude(cfg, clean_env):
    """An explicit operator value (sectioned env) wins over the provider-aware
    fallback under a Claude backend."""
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "claude")
    clean_env.setenv("AP2_CORE_IDEATION_SCRUB_MODEL", "operator-pinned-model")
    assert ideation_scrub._resolved_model(cfg) == "operator-pinned-model"


def test_explicit_override_wins_over_provider_aware_default_codex(cfg, clean_env):
    """An explicit operator value wins even under a Codex backend — the
    provider-aware fallback only governs the UNSET case, so a project that
    pinned a specific scrub model keeps it (e.g. gpu-bidder's config.toml)."""
    clean_env.setenv("AP2_AGENT_BACKEND_IDEATION_SCRUB", "codex")
    clean_env.setenv("AP2_CORE_IDEATION_SCRUB_MODEL", "operator-pinned-model")
    assert ideation_scrub._resolved_model(cfg) == "operator-pinned-model"
