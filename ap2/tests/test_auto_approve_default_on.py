"""TB-430: `auto_approve` is default-ON (autonomous-by-default; opt-OUT).

TB-430 flipped the `auto_approve` component from require-polarity
(opt-in, default-off, `env_flag=AP2_AUTO_APPROVE`, config key `enabled`)
to **suppress-polarity** (default-on, `env_flag=AP2_AUTO_APPROVE_DISABLED`,
config key `disabled`) — matching the `*_DISABLED` kill-switch convention
of janitor / cron / ideation / auto_unfreeze. A bare install is now
autonomous; operators opt OUT.

This module is the forward-looking pin for the new posture (the sibling
`test_tb320_auto_approve_independent_disable` in `test_components_disabled.py`
pins the manifest fields). It covers:

  * the manifest polarity fields + the `disabled` config key;
  * `is_enabled` resolving ON from a bare env and OFF from each opt-out
    surface (flat kill switch / sectioned env / config.toml);
  * the legacy `AP2_AUTO_APPROVE` back-compat tier honored in BOTH
    polarities (`=1` keeps on, `=0` opts out) but ALWAYS shadowed by a
    modern knob;
  * `should_auto_approve` defaulting True (and the gate-tag escape hatch
    + opt-out still suppressing it);
  * the one-time `DeprecationWarning` that fires when (and only when) the
    legacy flag is explicitly set.

Fixtures mirror the TB-326 / TB-227 shape — `init_project` + an
`AP2_*`-stripping `clean_env`; no SDK / network / freezegun dependence.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from ap2.components.auto_approve import _is_auto_approve_enabled, should_auto_approve
from ap2.components.auto_approve import impl as _auto_approve_impl
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import Registry


# ===========================================================================
# Fixtures + helpers.
# ===========================================================================


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically — the bare-default baseline TB-430's
    flip is asserted against. Returns the `monkeypatch` so a test can
    `setenv` an opt-out knob AFTER the strip."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def warn_reset():
    """Reset the module-level one-shot `_LEGACY_FLAG_DEPRECATION_WARNED`
    latch in `auto_approve.impl` so the deprecation-warning accounting
    doesn't leak in from an earlier test in the session (many tests set
    `AP2_AUTO_APPROVE` and would have tripped the latch already)."""
    saved = _auto_approve_impl._LEGACY_FLAG_DEPRECATION_WARNED
    _auto_approve_impl._LEGACY_FLAG_DEPRECATION_WARNED = False
    yield
    _auto_approve_impl._LEGACY_FLAG_DEPRECATION_WARNED = saved


def _manifest():
    """Fresh registry discovery → the `auto_approve` manifest. Hermetic
    (no cached `default_registry`), so the env-param `is_enabled` reads
    below don't depend on process state."""
    return Registry.discover().get("auto_approve")


class _StubCfg:
    """Duck-typed cfg exposing only `get_component_value` — the tier-3
    config.toml surface `Manifest.is_enabled` consults. Lets the
    config.toml opt-out tier be exercised without filesystem plumbing
    (the registry documents this duck-typing explicitly)."""

    def __init__(self, disabled_value: str):
        self._disabled = disabled_value

    def get_component_value(self, component, key, default=""):
        if component == "auto_approve" and key == "disabled":
            return self._disabled
        return default


# ===========================================================================
# Manifest polarity fields.
# ===========================================================================


def test_manifest_is_suppress_polarity():
    """The manifest carries the TB-430 suppress-polarity shape: default-on,
    `AP2_AUTO_APPROVE_DISABLED` flat flag, `AP2_AUTO_APPROVE` retained as
    the legacy back-compat flag, and a `disabled` config key (default
    `false`)."""
    m = _manifest()
    assert m.default_enabled is True
    assert m.env_flag == "AP2_AUTO_APPROVE_DISABLED"
    assert m.legacy_env_flag == "AP2_AUTO_APPROVE"
    # The config key is the suppress-polarity `disabled` (not the old
    # require-polarity `enabled`); default keeps auto-approve ON.
    assert "disabled" in m.config_schema
    assert "enabled" not in m.config_schema
    assert m.config_schema["disabled"].default is False
    # The registry's enable-key accessor agrees (`disabled` for a
    # default-on component).
    assert m._enable_config_key() == "disabled"


# ===========================================================================
# is_enabled — default ON, opt-out OFF.
# ===========================================================================


def test_bare_env_resolves_enabled():
    """A bare env (no knobs) resolves ON — the autonomous-by-default
    posture TB-430 ships."""
    assert _manifest().is_enabled(env={}) is True


@pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on"])
def test_flat_kill_switch_opts_out(truthy):
    """The flat suppress-polarity kill switch `AP2_AUTO_APPROVE_DISABLED`
    set truthy → OFF."""
    assert _manifest().is_enabled(env={"AP2_AUTO_APPROVE_DISABLED": truthy}) is False


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
def test_flat_kill_switch_falsy_stays_on(falsy):
    """A falsy kill switch is a no-op — auto-approve stays ON (a kill
    switch only disables when truthy)."""
    assert _manifest().is_enabled(env={"AP2_AUTO_APPROVE_DISABLED": falsy}) is True


def test_sectioned_env_opts_out():
    """The sectioned spelling `AP2_COMPONENTS_AUTO_APPROVE_DISABLED=1`
    (tier 1) opts out."""
    assert (
        _manifest().is_enabled(env={"AP2_COMPONENTS_AUTO_APPROVE_DISABLED": "1"})
        is False
    )


def test_config_toml_disabled_opts_out():
    """`[components.auto_approve] disabled = true` (tier 3) opts out; an
    empty/false config value leaves auto-approve ON."""
    m = _manifest()
    assert m.is_enabled(env={}, cfg=_StubCfg("true")) is False
    assert m.is_enabled(env={}, cfg=_StubCfg("")) is True


# ===========================================================================
# Legacy AP2_AUTO_APPROVE back-compat tier (TB-430).
# ===========================================================================


def test_legacy_flag_honored_both_polarities():
    """The deprecated require-polarity `AP2_AUTO_APPROVE` is honored as a
    transitional override when every modern knob is silent: `=1` keeps
    auto-approve ON, a falsy value opts OUT."""
    m = _manifest()
    assert m.is_enabled(env={"AP2_AUTO_APPROVE": "1"}) is True
    assert m.is_enabled(env={"AP2_AUTO_APPROVE": "0"}) is False
    assert m.is_enabled(env={"AP2_AUTO_APPROVE": "off"}) is False


def test_modern_knob_shadows_legacy_flag():
    """A modern knob ALWAYS wins over the legacy flag — the legacy tier is
    consulted only when tiers 1–3 are silent. A `disabled` kill switch
    set alongside a truthy legacy `AP2_AUTO_APPROVE=1` still resolves
    OFF."""
    m = _manifest()
    assert (
        m.is_enabled(
            env={"AP2_AUTO_APPROVE_DISABLED": "1", "AP2_AUTO_APPROVE": "1"},
        )
        is False
    )
    assert (
        m.is_enabled(
            env={
                "AP2_COMPONENTS_AUTO_APPROVE_DISABLED": "1",
                "AP2_AUTO_APPROVE": "1",
            },
        )
        is False
    )


# ===========================================================================
# should_auto_approve — default-on gate, tag escape hatch, opt-out.
# ===========================================================================


def test_should_auto_approve_default_on(clean_env, warn_reset):
    """With a bare env, `should_auto_approve` returns True for non-gated
    tags (and for `None` / empty tags) — the loop pass strips
    `@blocked:review` by default. A gate-tag carrier retains review."""
    assert should_auto_approve(["#cli"]) is True
    assert should_auto_approve(None) is True
    assert should_auto_approve([]) is True
    # Gate-tag escape hatch unchanged by TB-430 — a #breaking-change
    # proposal still retains manual review even with auto-approve on.
    assert should_auto_approve(["#breaking-change"]) is False


def test_should_auto_approve_suppressed_when_opted_out(clean_env, warn_reset):
    """Opting out via the kill switch suppresses auto-approve for every
    tag shape — even the non-gated ones that default True."""
    clean_env.setenv("AP2_AUTO_APPROVE_DISABLED", "1")
    assert should_auto_approve(["#cli"]) is False
    assert should_auto_approve(None) is False
    assert should_auto_approve([]) is False


def test_should_auto_approve_legacy_opt_out(clean_env, warn_reset):
    """The legacy `AP2_AUTO_APPROVE=0` opt-out still suppresses the loop
    pass (back-compat for an un-migrated deployment)."""
    clean_env.setenv("AP2_AUTO_APPROVE", "0")
    assert should_auto_approve(["#cli"]) is False


# ===========================================================================
# Deprecation warning — fires once, only when the legacy flag is set.
# ===========================================================================


def test_legacy_flag_emits_deprecation_warning_once(clean_env, warn_reset):
    """Setting the legacy `AP2_AUTO_APPROVE` flag emits exactly one
    `DeprecationWarning` (steering at `AP2_AUTO_APPROVE_DISABLED`); a
    second resolve in the same process is silent (one-shot latch)."""
    clean_env.setenv("AP2_AUTO_APPROVE", "1")

    with warnings.catch_warnings(record=True) as first:
        warnings.simplefilter("always")
        _is_auto_approve_enabled()
    deprecations = [w for w in first if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, [str(w.message) for w in first]
    msg = str(deprecations[0].message)
    assert "AP2_AUTO_APPROVE" in msg
    assert "AP2_AUTO_APPROVE_DISABLED" in msg

    # Second resolve: latch is set, no further warning.
    with warnings.catch_warnings(record=True) as second:
        warnings.simplefilter("always")
        _is_auto_approve_enabled()
    assert not [
        w for w in second if issubclass(w.category, DeprecationWarning)
    ]


def test_no_deprecation_warning_when_legacy_unset(clean_env, warn_reset):
    """A resolve with the legacy flag unset emits NO deprecation warning —
    the steer fires only for deployments still carrying the old flag."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _is_auto_approve_enabled()
    assert not [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
