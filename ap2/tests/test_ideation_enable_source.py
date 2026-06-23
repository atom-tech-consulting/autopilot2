"""TB-429: a core-keyed component's registry view agrees with its gate.

TB-427 unified `Manifest.is_enabled` with the component's own self-gate
for every `[components.<name>]`-keyed component — both read the SAME
`Config.get_component_value` source. But ideation's enable/disable knob
is NOT in `[components.ideation]`: its `config_schema` is intentionally
empty and the knob is the CORE key `[core] ideation_disabled`, read by
the gate via `cfg.get_core_value("ideation_disabled")`
(`components/ideation/impl._ideation_disabled`). Before TB-429
`Manifest.is_enabled` still resolved its config tier from the absent
`[components.ideation].disabled` key, falling through to "enabled", so
after a `[core] ideation_disabled = true` config edit `ap2 status`
reported `ideation: on` while the gate had correctly stopped ideating —
the registry view disagreed with the gate.

TB-429 declares ideation's enablement source on the manifest
(`enable_core_key="ideation_disabled"`) and routes `is_enabled`'s config
tier to `cfg.get_core_value(enable_core_key)`. This module pins that the
registry view and the gate now read ONE signal for the core-keyed
component, while a `[components.*]`-keyed control (auto_unfreeze) stays
unified as before.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ap2.components.auto_unfreeze.impl import _is_auto_unfreeze_disabled
from ap2.components.ideation.impl import _ideation_disabled
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


# ---------------------------------------------------------------------------
# Ideation (core-keyed): is_enabled and the gate read `[core] ideation_disabled`.
# ---------------------------------------------------------------------------


def test_ideation_core_key_true_disables_both_views(tmp_path, clean_env):
    """`[core] ideation_disabled = true` (the TOML-bool shape) turns
    ideation OFF on BOTH the registry view and the self-gate."""
    cfg = _load(tmp_path, "[core]\nideation_disabled = true\n")
    # The gate sees the core key and reports disabled (True).
    assert _ideation_disabled(cfg) is True
    # The registry view (is_enabled) must AGREE: disabled → not enabled.
    assert _is_enabled(cfg, "ideation") is False
    # One signal: gate-disabled iff registry-not-enabled.
    assert _ideation_disabled(cfg) is (not _is_enabled(cfg, "ideation"))


def test_ideation_core_key_unset_enables_both_views(tmp_path, clean_env):
    """With the knob unset (schema default False) ideation is ON on both
    surfaces — the misreport TB-429 fixes only bit when the operator set
    the knob, but the unset baseline must stay aligned too."""
    cfg = _load(tmp_path, "")
    assert _ideation_disabled(cfg) is False
    assert _is_enabled(cfg, "ideation") is True
    assert _ideation_disabled(cfg) is (not _is_enabled(cfg, "ideation"))


def test_ideation_core_key_false_enables_both_views(tmp_path, clean_env):
    """`[core] ideation_disabled = false` keeps ideation ON on both
    surfaces (explicit-false is the same as unset for enablement)."""
    cfg = _load(tmp_path, "[core]\nideation_disabled = false\n")
    assert _ideation_disabled(cfg) is False
    assert _is_enabled(cfg, "ideation") is True
    assert _ideation_disabled(cfg) is (not _is_enabled(cfg, "ideation"))


# ---------------------------------------------------------------------------
# Control: a `[components.*]`-keyed component (auto_unfreeze) still unifies.
# ---------------------------------------------------------------------------


def test_auto_unfreeze_component_key_still_unifies(tmp_path, clean_env):
    """The component-keyed control resolves identically between is_enabled
    and its gate — TB-429's core-key routing must not regress the
    `[components.<name>].disabled` path."""
    # disabled via config.toml → both views OFF.
    cfg_off = _load(tmp_path, "[components.auto_unfreeze]\ndisabled = true\n")
    assert _is_auto_unfreeze_disabled(cfg_off) is True
    assert _is_enabled(cfg_off, "auto_unfreeze") is False
    assert _is_auto_unfreeze_disabled(cfg_off) is (
        not _is_enabled(cfg_off, "auto_unfreeze")
    )
    # default (unset) → both views ON (suppress polarity, default_enabled).
    cfg_on = _load(tmp_path, "")
    assert _is_auto_unfreeze_disabled(cfg_on) is False
    assert _is_enabled(cfg_on, "auto_unfreeze") is True
    assert _is_auto_unfreeze_disabled(cfg_on) is (
        not _is_enabled(cfg_on, "auto_unfreeze")
    )
