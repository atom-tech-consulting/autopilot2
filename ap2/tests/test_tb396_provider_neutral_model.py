"""TB-396: provider-neutral default model — `agent_model` default → `None` so
both backends self-default, and a `claude-*` model never leaks to a codex-routed
kind.

Gate-runnable (no real SDK) regression pin for the model-resolution contract the
real codex smokes (`ap2/tests/smoke/`) exercise out-of-band on a 6h cron. The
smokes make paid live calls and are descoped from the per-task gate, so this
pins the same contract on every `pytest` run with no SDK / credential:

  - Under a DEFAULT config (no `AP2_AGENT_MODEL`), the resolved `agent_model` is
    `None`. The four production dispatch sites build
    `cfg.get_core_value("agent_model") or None`, so an unset key (schema default
    `None`) AND an explicit empty-string env both reach `AgentOptions.model` as
    `None`.
  - A codex-routed dispatch (`select_adapter("task", cfg)` → `CodexAdapter`)
    maps that `None` through `normalize_options` to an OMITTED `model` kwarg, so
    codex self-defaults — a Claude id is never handed to a codex turn (which
    would reject it).
  - The change is provider-neutral: a claude-routed dispatch under the default
    config ALSO omits the kwarg (Claude's CLI self-default), so this isn't a
    codex-only special case.
  - The guard has teeth: a `claude-*` `agent_model` (here an operator pin — the
    same mechanism a default regression would trip) DOES reach a codex-routed
    kind, forwarding `model="claude-opus-4-7"` onto codex's native kwarg. That
    is the leak the provider-neutral default avoids out of the box (and the
    operator-pin caveat the `ap2-config` skill documents).
"""
from __future__ import annotations

import os

import pytest

from ap2.adapters import (
    AgentOptions,
    ClaudeCodeAdapter,
    CodexAdapter,
    select_adapter,
)
from ap2.config import Config
from ap2.core_config_schema import CORE_CONFIG_SCHEMA
from ap2.init import init_project


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` knob so each test owns its env surface — a true
    DEFAULT config where only the schema default governs `agent_model`."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Fresh project + stripped env → a default config. `clean_env` runs first
    (it's a dependency) so neither the host env nor the scaffolded
    `.cc-autopilot/env` template leaks an `AP2_AGENT_MODEL` pin into the
    resolution."""
    init_project(tmp_path)
    return Config.load(tmp_path)


def _production_shape_options(cfg) -> AgentOptions:
    """`AgentOptions` built the way the four production dispatch sites build it
    — `model=cfg.get_core_value("agent_model") or None` — so the test pins the
    REAL resolution path, not a hand-picked literal `None`."""
    return AgentOptions(
        cwd=str(cfg.project_root),
        permission_mode="bypassPermissions",
        max_turns=5,
        setting_sources=["project"],
        model=cfg.get_core_value("agent_model") or None,
    )


# --- schema -----------------------------------------------------------------


def test_schema_default_is_provider_neutral_none():
    """The `agent_model` schema default is provider-neutral `None`, never a
    `claude-*` string or `""` (both forward a non-None model — `""` because
    `"" is not None` is `True`)."""
    spec = CORE_CONFIG_SCHEMA["agent_model"]
    assert spec.default is None, (
        f"agent_model schema default must be provider-neutral None, got "
        f"{spec.default!r}"
    )
    assert spec.default != ""
    assert not (
        isinstance(spec.default, str) and spec.default.startswith("claude")
    )


# --- resolution under a default config --------------------------------------


def test_get_core_value_agent_model_is_none_under_default_config(cfg):
    """No env / TOML override → `get_core_value` falls back to the schema
    default, which is now `None`."""
    assert cfg.get_core_value("agent_model") is None


def test_empty_string_env_coerces_to_none(cfg, clean_env):
    """`AP2_CORE_AGENT_MODEL=""` resolves to a literal `""` from
    `get_core_value`, which the dispatch-site `or None` coercion folds to
    `None` — the load-bearing distinction the schema comment calls out.

    TB-413: injects via the SECTIONED env (the flat `AP2_AGENT_MODEL`
    tunable override is removed; config.toml is the sole source, the
    sectioned env remaining the explicit structured override)."""
    clean_env.setenv("AP2_CORE_AGENT_MODEL", "")
    assert cfg.get_core_value("agent_model") == ""
    assert (cfg.get_core_value("agent_model") or None) is None


# --- codex routing omits the model kwarg ------------------------------------


def test_codex_routed_dispatch_omits_model_under_default_config(cfg, clean_env):
    """The core pin: a codex-routed kind under the default config resolves
    `model=None` and `CodexAdapter.normalize_options` OMITS the `model` kwarg,
    so codex self-defaults — no Claude id reaches a codex turn."""
    clean_env.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    adapter = select_adapter("task", cfg)
    assert isinstance(adapter, CodexAdapter)
    assert adapter.backend == "codex"

    options = _production_shape_options(cfg)
    assert options.model is None, "default config must resolve model to None"

    kwargs = adapter.normalize_options(options)
    assert "model" not in kwargs, (
        f"a codex-routed dispatch under the provider-neutral default must omit "
        f"the model kwarg (codex self-defaults); got "
        f"model={kwargs.get('model')!r}"
    )
    # Defensive: no claude-* id leaked into ANY codex kwarg.
    assert not any(
        isinstance(v, str) and v.startswith("claude") for v in kwargs.values()
    ), f"a claude-* id leaked into a codex kwarg: {kwargs!r}"


def test_claude_routed_dispatch_also_omits_model_under_default_config(
    cfg, clean_env,
):
    """Provider-neutrality, not a codex special case: the claude kind ALSO omits
    the kwarg under the default config, so each backend self-defaults."""
    clean_env.setenv("AP2_AGENT_BACKEND_TASK", "claude")
    adapter = select_adapter("task", cfg)
    assert isinstance(adapter, ClaudeCodeAdapter)
    kwargs = adapter.normalize_options(_production_shape_options(cfg))
    assert "model" not in kwargs


# --- the guard has teeth ----------------------------------------------------


def test_a_claude_model_reaches_a_codex_kind_when_resolved(cfg, clean_env):
    """The pin isn't green-by-construction: a `claude-*` `agent_model` (here an
    operator pin via the top-precedence env layer — the same mechanism a default
    regression would trip, but order-independent) DOES reach a codex-routed
    kind. The dispatch resolves through the REAL path
    (`select_adapter("task", cfg)` → `CodexAdapter`, production options shape),
    and `normalize_options` forwards the claude model onto codex's native
    `model` kwarg — the leak TB-396's provider-neutral default avoids out of the
    box and the operator-pin caveat documents. (Contrast
    `test_codex_routed_dispatch_omits_model_under_default_config`, which shows
    the default config omits it entirely.)"""
    clean_env.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    clean_env.setenv("AP2_CORE_AGENT_MODEL", "claude-opus-4-7")
    adapter = select_adapter("task", cfg)
    assert isinstance(adapter, CodexAdapter)

    options = _production_shape_options(cfg)
    assert options.model == "claude-opus-4-7", (
        "a pinned claude-* agent_model must resolve through the production path"
    )
    kwargs = adapter.normalize_options(options)
    assert kwargs.get("model") == "claude-opus-4-7", (
        "a claude-* agent_model REACHES the codex kind's native model kwarg — "
        "the leak TB-396's provider-neutral default avoids and the operator-pin "
        "caveat documents."
    )
