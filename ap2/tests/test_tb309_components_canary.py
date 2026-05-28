"""TB-309: component registry + manifest schema + `janitor/` canary.

Pins the structural cleavage axis-(1) of the **refactor features into
opt-in components** focus introduces:

  1. The registry's filesystem-driven discovery surfaces a component
     named "janitor" (it walks `ap2/components/*/manifest.py` via
     `pkgutil.iter_modules`; no hardcoded names anywhere).
  2. The janitor manifest declares a `tick_hook` hook-point bound to a
     callable.
  3. `registry.hook("tick_hook", component="janitor")` returns the
     SAME callable object as `run_janitor` reachable from the new
     `ap2.components.janitor` subpackage — proves the manifest's hook
     binding matches the canonical public entry point and the
     registry's lookup path is bit-equivalent to a direct import.
  4. The registry's discovery walk is filesystem-driven (not a
     hardcoded list of component names) — source-pin via
     `inspect.getsource(Registry.discover)` so a refactor that
     hardcoded "janitor" or any sibling component would flip this
     test.

Why this is the canary axis's load-bearing regression: every
subsequent migration (validator_judge, mattermost, attention,
focus_advance, auto_unfreeze, auto_approve — goal.md L176-201) drops a
component subpackage and expects the registry to pick it up without a
registry-side edit. If a future refactor accidentally re-coupled the
registry to a static list of names, this test (specifically the
source-pin assertion) breaks loudly at the same PR.
"""
from __future__ import annotations

import inspect

from ap2.components.janitor import run_janitor
from ap2.registry import Manifest, Registry, default_registry


def test_registry_discover_returns_janitor_component():
    """`Registry.discover()` walks the filesystem and surfaces a
    component named "janitor". Constructed fresh each call so the test
    isn't sensitive to a cached `default_registry()` from a prior test.
    """
    registry = Registry.discover()
    names = {m.name for m in registry.components}
    assert "janitor" in names, (
        f"TB-309: filesystem-driven discovery should find the janitor "
        f"canary at `ap2/components/janitor/manifest.py`; got {sorted(names)}"
    )


def test_janitor_manifest_exposes_callable_tick_hook():
    """The janitor manifest declares a `tick_hook` hook-point and its
    value is a callable (per goal.md L121 — hook-point values are
    live callables, not string indirection)."""
    registry = Registry.discover()
    janitor_manifest = registry.get("janitor")
    assert isinstance(janitor_manifest, Manifest)
    assert "tick_hook" in janitor_manifest.hook_points, (
        f"TB-309: janitor manifest must register a `tick_hook` hook-point; "
        f"got hook_points={list(janitor_manifest.hook_points)}"
    )
    hook = janitor_manifest.hook_points["tick_hook"]
    assert callable(hook), (
        f"TB-309: `tick_hook` value must be a live callable, not "
        f"a string indirection; got type={type(hook).__name__}"
    )


def test_registry_hook_lookup_returns_canonical_run_janitor():
    """`registry.hook("tick_hook", component="janitor")` returns the
    SAME callable object as `ap2.components.janitor.run_janitor` —
    the manifest's hook binding is bit-equivalent to a direct import
    (no wrapper indirection, no copy)."""
    registry = Registry.discover()
    via_registry = registry.hook("tick_hook", component="janitor")
    assert via_registry is run_janitor, (
        "TB-309: the manifest's `tick_hook` callable should be the "
        "EXACT object reachable from `ap2.components.janitor.run_janitor`"
        " — a wrapper or copy would defeat the "
        "monkeypatch-via-registry seam tests rely on."
    )


def test_registry_discovery_is_filesystem_driven_not_hardcoded():
    """Source-pin: `Registry.discover()` walks the filesystem via
    `pkgutil.iter_modules`; the function body never references
    "janitor" or any other component name as a string literal. If a
    future refactor hardcoded a name (or even a list of names), this
    assertion breaks loudly — preserving the contract every later
    migration depends on (goal.md L188-201).
    """
    src = inspect.getsource(Registry.discover)
    assert "pkgutil.iter_modules" in src, (
        "TB-309: Registry.discover should walk filesystem via "
        "`pkgutil.iter_modules`; got source without that reference"
    )
    # The function MUST NOT name-literal any component. If we ever
    # need to skip a component by name, that exemption belongs
    # outside `discover()` (e.g. an `enabled_components` filter).
    forbidden = ("janitor", "mattermost", "validator_judge", "attention",
                 "focus_advance", "auto_unfreeze", "auto_approve")
    for name in forbidden:
        assert name not in src, (
            f"TB-309: `Registry.discover` source must not reference "
            f"component name {name!r} — discovery is filesystem-"
            f"driven, not a hardcoded list (goal.md L188-201). "
            f"If a name-based exemption is genuinely needed it "
            f"belongs in `enabled_components` (env-flag-gated), "
            f"not in `discover`."
        )


def test_janitor_env_flag_is_disabled_polarity(monkeypatch):
    """The janitor manifest declares `env_flag="AP2_JANITOR_DISABLED"`
    with `default_enabled=True` — i.e. the env var is a KILL SWITCH,
    not an opt-in toggle. Backwards-compat per goal.md L64-67: existing
    operators don't need to flip a flag to keep TB-177/TB-178 behavior.

    Pins the polarity via `Registry._is_enabled` so a future drift
    (manifest typo flipping default_enabled, or the polarity rule in
    `_is_enabled` inverting) shows up as a loud test break."""
    registry = Registry.discover()
    janitor_manifest = registry.get("janitor")
    assert janitor_manifest.env_flag == "AP2_JANITOR_DISABLED", (
        f"TB-309: janitor's env_flag drifted from `AP2_JANITOR_DISABLED`; "
        f"got {janitor_manifest.env_flag!r}"
    )
    assert janitor_manifest.default_enabled is True, (
        "TB-309: janitor must remain default-enabled "
        "(backwards-compat with TB-177/TB-178 operators)"
    )

    # default-on when env var is absent
    monkeypatch.delenv("AP2_JANITOR_DISABLED", raising=False)
    assert Registry._is_enabled(janitor_manifest) is True

    # default-on when env var is empty / falsy
    for falsy in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("AP2_JANITOR_DISABLED", falsy)
        assert Registry._is_enabled(janitor_manifest) is True, falsy

    # disabled when env var is truthy
    for truthy in ("1", "true", "yes"):
        monkeypatch.setenv("AP2_JANITOR_DISABLED", truthy)
        assert Registry._is_enabled(janitor_manifest) is False, truthy


def test_default_registry_is_cached_and_resettable():
    """`default_registry()` returns the same instance across calls (cached
    module-level singleton); `_reset_default_registry()` clears it so
    a re-discovery pass after a manifest mutation sees the change."""
    from ap2.registry import _reset_default_registry

    _reset_default_registry()
    r1 = default_registry()
    r2 = default_registry()
    assert r1 is r2, "default_registry should cache the discovered instance"
    _reset_default_registry()
    r3 = default_registry()
    assert r3 is not r1, (
        "_reset_default_registry should force a fresh Registry.discover()"
    )
