"""TB-427: component enablement resolves from ONE config-aware source.

Before TB-427 the registry layer (`Manifest.is_enabled`, and through it
`ap2 status` `## Components`, `ap2 doctor`, `Registry.enabled_components`)
read ONLY the env flag and could not see `config.toml`, while the
component's own gate (`_is_auto_approve_enabled` →
`cfg.get_component_value(...)`) read sectioned-env + config.toml. The two
layers read DISJOINT sources, so `[components.auto_approve] enabled = true`
turned the gate on but left `ap2 status` reporting it off (and the
reverse for the flat env flag). They could disagree.

This module pins the post-TB-427 contract: with a `Config` supplied,
`Manifest.is_enabled(cfg=...)` resolves through the SAME accessor the gate
uses (`Config.get_component_value`), so for every component:

  - `is_enabled(cfg)`, `enabled_components(cfg)` membership, and
    `get_component_value` agree, and
  - the component's gate view can never disagree with `is_enabled`.

Covered across the env-only / config-only / neither / both matrix for two
suppress-polarity components (`auto_approve` and `janitor`, both
`default_enabled=True`, `disabled` key). "Env master flag" here is the
sectioned `AP2_COMPONENTS_<NAME>_<KEY>` name — the spelling
`get_component_value` honors, so the registry layer and the config
accessor are exercised against ONE knob.

TB-430: `auto_approve` flipped from require-polarity (opt-in,
`default_enabled=False`, `enabled` key) to suppress-polarity (default-on /
opt-out, `default_enabled=True`, `disabled` key). After the flip no
require-polarity component remains, so the require-polarity branch of
`Manifest.is_enabled` is covered by the synthetic manifests in
`test_tb319_status_components.py`; this file pins the cross-surface
agreement invariant for the real suppress-polarity components.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ap2.components.auto_approve.impl import _is_auto_approve_enabled
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import default_registry


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` knob so each test owns its env surface
    deterministically (the known env-knob verifier-leak failure mode)."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _load(tmp_path: Path, body: str = "") -> Config:
    """Scaffold a project, overwrite config.toml with `body`, and load."""
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


def _is_enabled(cfg: Config, name: str) -> bool:
    return default_registry().get(name).is_enabled(cfg=cfg)


def _in_enabled_components(cfg: Config, name: str) -> bool:
    return name in {m.name for m in default_registry().enabled_components(cfg)}


def _truthy(raw) -> bool:
    """The registry's falsy-enumeration parse (mirrors
    `Manifest.is_enabled`): empty / 0 / false / no / off → off."""
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Helpers asserting the cross-surface invariant for one component.
# ---------------------------------------------------------------------------


def _assert_all_agree(cfg: Config, name: str, key: str, expected: bool):
    """is_enabled / enabled_components membership / the config accessor
    all report the SAME on/off for `name`."""
    enabled = _is_enabled(cfg, name)
    assert enabled is expected, (
        f"{name}: is_enabled(cfg) expected {expected}, got {enabled}"
    )
    assert _in_enabled_components(cfg, name) is expected, (
        f"{name}: enabled_components(cfg) membership disagrees with "
        f"is_enabled ({expected})"
    )
    # The config accessor's raw signal, run through the polarity
    # convention, must reproduce is_enabled — proving they share a source.
    # Polarity is read from the manifest's `default_enabled`: suppress
    # (default_enabled=True, `disabled` key) → truthy signal means OFF;
    # require (default_enabled=False, `enabled` key) → truthy means ON.
    raw = cfg.get_component_value(name, key, default="")
    signal_truthy = _truthy(raw)
    suppress_polarity = default_registry().get(name).default_enabled
    derived = (not signal_truthy) if suppress_polarity else signal_truthy
    assert derived is expected, (
        f"{name}: get_component_value({key!r})={raw!r} → {derived}, "
        f"expected {expected}"
    )


# ---------------------------------------------------------------------------
# (a) config.toml key ALONE turns the component on/off on every surface.
# ---------------------------------------------------------------------------


def test_auto_approve_config_toml_key_alone_disables(tmp_path, clean_env):
    """`[components.auto_approve] disabled = true` in config.toml (no env)
    turns auto_approve OFF across is_enabled / enabled_components /
    get_component_value — the headline split TB-427 fixes, exercised on
    the TB-430 suppress-polarity (`disabled`) key."""
    cfg = _load(tmp_path, "[components.auto_approve]\ndisabled = true\n")
    _assert_all_agree(cfg, "auto_approve", "disabled", expected=False)
    # …and the gate agrees with the registry view.
    assert _is_auto_approve_enabled(cfg) is False
    assert _is_auto_approve_enabled(cfg) is _is_enabled(cfg, "auto_approve")


def test_janitor_config_toml_key_alone_disables(tmp_path, clean_env):
    """`[components.janitor] disabled = true` in config.toml (no env)
    turns the suppress-polarity janitor OFF across every surface."""
    cfg = _load(tmp_path, "[components.janitor]\ndisabled = true\n")
    _assert_all_agree(cfg, "janitor", "disabled", expected=False)


# ---------------------------------------------------------------------------
# (b) the env master flag (sectioned) ALONE does the same.
# ---------------------------------------------------------------------------


def test_auto_approve_sectioned_env_alone_disables(tmp_path, clean_env):
    """`AP2_COMPONENTS_AUTO_APPROVE_DISABLED=1` (no config.toml key) turns
    auto_approve OFF across every surface — and the gate agrees."""
    cfg = _load(tmp_path, "")
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", "1")
    _assert_all_agree(cfg, "auto_approve", "disabled", expected=False)
    assert _is_auto_approve_enabled(cfg) is False
    assert _is_auto_approve_enabled(cfg) is _is_enabled(cfg, "auto_approve")


def test_janitor_sectioned_env_alone_disables(tmp_path, clean_env):
    """`AP2_COMPONENTS_JANITOR_DISABLED=1` (no config.toml key) turns the
    janitor OFF across every surface."""
    cfg = _load(tmp_path, "")
    clean_env.setenv("AP2_COMPONENTS_JANITOR_DISABLED", "1")
    _assert_all_agree(cfg, "janitor", "disabled", expected=False)


# ---------------------------------------------------------------------------
# (c) neither set → schema/polarity defaults (require off, suppress on).
# ---------------------------------------------------------------------------


def test_defaults_when_neither_env_nor_config_set(tmp_path, clean_env):
    cfg = _load(tmp_path, "")
    # auto_approve (suppress polarity, TB-430 default-on) defaults ON.
    _assert_all_agree(cfg, "auto_approve", "disabled", expected=True)
    assert _is_auto_approve_enabled(cfg) is True
    # janitor (suppress polarity) defaults ON.
    _assert_all_agree(cfg, "janitor", "disabled", expected=True)


# ---------------------------------------------------------------------------
# (d) both set → sectioned env wins over config.toml, still all-agree.
# ---------------------------------------------------------------------------


def test_sectioned_env_overrides_config_toml_and_all_surfaces_agree(
    tmp_path, clean_env,
):
    """With config.toml saying ON (`disabled = false`) and the sectioned
    env saying OFF (`...DISABLED=1`), the env wins (precedence) and every
    surface — including the gate — follows it in lockstep."""
    cfg = _load(tmp_path, "[components.auto_approve]\ndisabled = false\n")
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", "1")
    _assert_all_agree(cfg, "auto_approve", "disabled", expected=False)
    assert _is_auto_approve_enabled(cfg) is False


# ---------------------------------------------------------------------------
# (e) gate and is_enabled never disagree across the full matrix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "toml_body,env",
    [
        ("", None),                                               # neither
        ("[components.auto_approve]\ndisabled = true\n", None),   # config only (off)
        ("", "1"),                                                # env only (off)
        ("[components.auto_approve]\ndisabled = false\n", "1"),   # both (env wins off)
        ("[components.auto_approve]\ndisabled = true\n", "0"),    # both (env wins on)
    ],
)
def test_auto_approve_gate_never_disagrees_with_is_enabled(
    tmp_path, clean_env, toml_body, env,
):
    cfg = _load(tmp_path, toml_body)
    if env is not None:
        clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", env)
    assert _is_auto_approve_enabled(cfg) is _is_enabled(cfg, "auto_approve"), (
        "the auto_approve gate and Manifest.is_enabled must resolve to the "
        f"same value (toml={toml_body!r}, env={env!r})"
    )
