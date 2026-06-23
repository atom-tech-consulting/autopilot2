"""TB-412: fresh-install conservative-posture release gate (axis 2).

Promotes ap2's conservative-by-default posture to a release gate under
goal.md's **Current focus: cut a public source-available distribution**
(axis 2, "Default-config posture + extras"). The posture is ALREADY the
schema default — this test asserts and documents it so a future config
change can't silently ship a public install that acts unattended on the
operator's behalf out of the box (the axis-2 delete-test failure).

The conservative posture, pinned here against the default merged config
(no env overrides): every operator-bypassing behavior is off/inert while
the loop stays whole —

  1. `attention.immediate_push` is OFF — the status-report cron stays the
     routine push surface; the daemon does not push to a channel
     unprompted on every `attention_raised`.
  2. No communication channel is configured — `AP2_MM_CHANNELS` unset →
     the communication component's channel registry is empty (no
     outbound destination).
  3. `auto_unfreeze` carries no `fix_shapes` allowlist — the
     briefing-shape auto-heal sweep is a no-op until the operator opts in
     by naming trusted shapes.

TB-430 deliberately removed `auto_approve` from this gate: it flipped to
default-ON (autonomous-by-default; operators opt OUT via
`AP2_AUTO_APPROVE_DISABLED`), so an enabled auto_approve is now the
shipped default rather than an operator-bypassing surprise. The gate
below covers only the behaviors that remain off-by-default.

The same file also asserts that an all-components-disabled config (via the
shared `enumerate_disabled_env_flags()` helper the minimal-kernel e2e
uses) still LOADS without error — the loop stays whole in the minimal
kernel — and that a fresh `ap2 init` writes a config whose resolved
posture matches the conservative default above.

This test does NOT disable or change any default value; it reads the
posture the schema already ships and locks it as a gate. The posture
reads route through each component's canonical resolver
(`_is_attention_immediate_push_enabled`, `channel_registry`,
`_auto_unfreeze_allowlist`) so a drift in any resolver's default trips
this gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2.components.attention.impl import _is_attention_immediate_push_enabled
from ap2.components.auto_unfreeze.impl import _auto_unfreeze_allowlist
from ap2.components.communication import channel_registry
from ap2.config import Config
from ap2.init import init_project
from ap2.registry import (
    Registry,
    _reset_default_registry,
    default_registry,
)
from ap2.tests.test_components_disabled import enumerate_disabled_env_flags


# Every env knob (flat + sectioned) that could override one of the three
# posture surfaces. Cleared by the `clean_posture_env` fixture so the
# assertions exercise the DEFAULT merged config ("no env overrides") even
# when the test runner's environment carries an operator-tuned value.
_POSTURE_ENV_KNOBS: tuple[str, ...] = (
    # attention immediate-push toggle (flat + sectioned).
    "AP2_ATTENTION_IMMEDIATE_PUSH",
    "AP2_COMPONENTS_ATTENTION_IMMEDIATE_PUSH",
    # communication channel activation.
    "AP2_MM_CHANNELS",
    # auto_unfreeze fix-shape allowlist (flat + sectioned).
    "AP2_AUTO_UNFREEZE_FIX_SHAPES",
    "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES",
)


@pytest.fixture
def clean_posture_env(monkeypatch):
    """Clear every posture-relevant env override so the posture reads see
    the in-source defaults — the faithful "default merged config (no env
    overrides)" baseline the briefing's first scope bullet names.

    `get_component_value` (and `channel_registry`) read `os.environ`
    first, so an operator-exported `AP2_AUTO_APPROVE=1` in the test
    runner's environment would otherwise leak into the assertions. The
    `delenv(..., raising=False)` loop makes the gate hermetic regardless
    of what the runner carries.
    """
    for key in _POSTURE_ENV_KNOBS:
        monkeypatch.delenv(key, raising=False)
    yield


def _assert_conservative_posture(cfg: Config) -> None:
    """Assert `cfg` resolves the three operator-bypassing behaviors to
    off/inert — the conservative default a fresh install must ship.

    Shared between the default-merged-config gate and the fresh-`ap2
    init` gate so both pin the identical posture contract from one place.

    TB-430 dropped `auto_approve` from this gate: it is now default-ON
    (operators opt OUT via `AP2_AUTO_APPROVE_DISABLED`), so an enabled
    auto_approve is the shipped default, not a posture regression.
    """
    # 1. attention.immediate_push OFF — no unprompted channel push.
    assert _is_attention_immediate_push_enabled(cfg) is False, (
        "default posture must leave attention.immediate_push off — the "
        "status-report cron stays the routine push surface for fresh "
        "installs."
    )

    # 2. No communication channel configured — empty channel registry.
    assert channel_registry(cfg) == [], (
        "default posture must configure no communication channel "
        "(AP2_MM_CHANNELS unset → empty channel registry → no outbound "
        "destination)."
    )

    # 3. auto_unfreeze carries no fix_shapes allowlist — sweep is inert.
    assert _auto_unfreeze_allowlist(cfg) == frozenset(), (
        "default posture must ship no auto_unfreeze fix_shapes — the "
        "briefing-shape auto-heal sweep stays a no-op until the operator "
        "opts in by naming trusted shapes."
    )


def test_default_merged_config_conservative_posture(
    tmp_path: Path, clean_posture_env,
):
    """The default merged config (no env overrides, no config.toml) leaves
    every operator-bypassing behavior off/inert while the loop stays
    whole.

    Loads a `Config` for a bare project root — no `.cc-autopilot/env`,
    no `config.toml` — so the env-only resolution path returns the
    in-source defaults, then asserts the four-point conservative posture.
    """
    cfg = Config.load(tmp_path)
    _assert_conservative_posture(cfg)


def test_all_components_disabled_config_loads(tmp_path: Path, monkeypatch):
    """An all-components-disabled config still LOADS without error — the
    loop stays whole in the minimal kernel.

    Applies the shared `enumerate_disabled_env_flags()` helper (the same
    registry-driven disable set the minimal-kernel e2e uses) to the
    process env, forces a fresh registry discovery, then confirms a fresh
    project's `Config.load()` succeeds and every env-flag-bearing
    component is dropped from `enabled_components()`.
    """
    flags = enumerate_disabled_env_flags(Registry.discover())
    for key, val in flags.items():
        if val:
            monkeypatch.setenv(key, val)
        else:
            monkeypatch.delenv(key, raising=False)
    # Post-TB-391 the helper already maps `AP2_IDEATION_DISABLED -> "1"`,
    # but set it explicitly so the minimal-kernel config is robust to
    # ordering / any future helper change (mirrors the e2e pin).
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    _reset_default_registry()
    try:
        init_project(tmp_path)
        # The load itself must not raise under the all-disabled env — the
        # config layer doesn't depend on any component being enabled.
        cfg = Config.load(tmp_path)
        assert isinstance(cfg, Config)

        # Sanity: the kernel really is minimal — every env-flag-bearing
        # component dropped out; only always-on (env_flag=None) remain.
        registry = default_registry()
        enabled = {m.name for m in registry.enabled_components()}
        for manifest in registry.components:
            if manifest.env_flag is not None:
                assert manifest.name not in enabled, (
                    f"{manifest.name!r} should be disabled in the all-"
                    f"disabled config; enabled={sorted(enabled)}"
                )
    finally:
        # Drop the cached registry so a sibling test gets a clean
        # discovery pass against the (monkeypatch-reverted) env state.
        _reset_default_registry()


def test_fresh_init_writes_conservative_default(
    tmp_path: Path, clean_posture_env,
):
    """A fresh `ap2 init` writes a config whose resolved posture matches
    the conservative default.

    `init_project` scaffolds `.cc-autopilot/config.toml` (every key
    commented out) + `.cc-autopilot/env` (every knob commented out), so
    `Config.load()` resolves through the TOML branch with no overrides
    live — the same conservative posture the default merged config
    carries.
    """
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    _assert_conservative_posture(cfg)
