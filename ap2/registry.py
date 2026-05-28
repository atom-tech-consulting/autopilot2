"""Component registry + manifest schema (TB-309, axis (1)).

This module is the structural prerequisite for the **refactor features into
opt-in components** focus (goal.md L116-201). Every subsequent axis-(5)
migration (`validator_judge/`, `mattermost/`, `attention/`,
`focus_advance/`, `auto_unfreeze/`, `auto_approve/`) drops a component
subpackage under `ap2/components/<name>/` with a `manifest.py`; the
registry discovers them at daemon startup, exposes a typed hook-point
API, and the daemon walks `registry.tick_hooks` (axis 2 — separate TB)
instead of importing each module directly.

Discovery is filesystem-driven (`pkgutil.iter_modules` over
`ap2/components/`); there is NO hardcoded list of component names here
— a future migration ships a subpackage and the registry picks it up
automatically. This is load-bearing per goal.md L188-201: "each
migration ships its own component subpackage; the registry must pick
them up without a registry-side edit."

Manifest contract (goal.md L121-125):

  name            — short identifier (e.g. "janitor", "mattermost").
  env_flag        — env var that toggles the component, or None for
                    always-on. The polarity is determined by
                    `default_enabled`:
                      default_enabled=True  → env_flag DISABLES when truthy
                      default_enabled=False → env_flag ENABLES when truthy
                    so the conventional shape for a default-on component
                    is a `*_DISABLED` kill switch (e.g.
                    `AP2_JANITOR_DISABLED`), and for a default-off
                    component a `*_ENABLED` opt-in toggle. Following the
                    existing `AP2_AUTO_UNFREEZE_DRY_RUN` / `AP2_AUTO_APPROVE`
                    naming family keeps the operator-facing surface
                    consistent.
  default_enabled — bool; the component's enabled state when env_flag
                    is unset (or env_flag is None).
  hook_points     — dict[str, Callable]; named hooks the component
                    registers. Hook-point names reserved for this and
                    later axes:
                      tick_hook              — axis (2), per-tick callable
                      validator_hook         — axis (4), briefing-validator
                      channel_adapter        — axis (3), channel delivery
                      status_report_section  — axis (3), digest renderer
                      cli_verb               — axis (5), `ap2 <verb>` impl
                      status_findings_counts — janitor-specific data accessor
                                               (used by cli_daemon + status_report)
                    Components register only the hooks they actually
                    provide; consumers look up via
                    `registry.hook(<name>, component=<component_name>)`.
  dependencies    — list[str] of component names this one depends on.
                    Reserved for axis (2) ordering; this TB doesn't act
                    on it yet.

For TB-309 only `tick_hook` and the janitor-specific
`status_findings_counts` are wired through to real call sites
(daemon.run_cron, cli_daemon.cmd_status, status_report._compute_state_snapshot).
Other hook-point names are reserved in the schema; the registry's
`hook()` method doesn't validate names — components can register any
hook name, and consumers know which name to ask for.
"""
from __future__ import annotations

import enum
import importlib
import os
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union


# Per-process cached registry built lazily by `default_registry()`. Tests
# that need to mutate manifests (e.g. patching a registered hook) should
# `_reset_default_registry()` between cases so a per-test monkeypatch
# doesn't leak across the file.
_DEFAULT_REGISTRY: "Registry | None" = None


# TB-310 (axis 2): typed callable signature for per-tick hooks the
# daemon dispatches by walking `registry.tick_hooks(phase)` instead of
# importing each component module directly. The signature is
# `(cfg, sdk) -> None | Awaitable[None]` — sync hooks return None, async
# hooks return a coroutine that the daemon awaits. The pattern mirrors
# the existing direct-call shapes in `daemon._tick` today:
#   - `_maybe_auto_unfreeze(cfg)`         — sync
#   - `await _maybe_advance_focus(cfg, sdk)` — async
#   - `_maybe_emit_attention_events(cfg)` — sync
# so the registry walk is a uniform iteration over both styles, with the
# daemon-side dispatch checking `iscoroutine()` on each return value.
#
# Each tick hook is expected to self-handle its own exception surface
# (matching the original per-call try/except blocks in `_tick` whose
# observable error events the briefing pins as bit-for-bit preserved
# behavior — e.g. `auto_unfreeze_skipped reason=sweep_error` for
# auto_unfreeze, stderr-print for focus_advance / attention). Failing to
# self-handle would surface as an uncaught exception in `_tick`, which
# already has an outer try/except per stage today; the contract is
# narrower than that fallback to keep observable behavior identical.
TickHook = Callable[[Any, Any], Union[None, Awaitable[None]]]


class Phase(enum.Enum):
    """TB-310: canonical tick-hook phases the daemon iterates per tick.

    Order of declaration matches the existing `daemon._tick` body
    (goal.md L138-141): the registry-walk refactor preserves observable
    behavior bit-for-bit by walking each phase in the same effective
    order today's direct calls fire in.

    - PRE_DISPATCH       — fires before the cron / task-dispatch stages
                           (today: `_maybe_auto_unfreeze`,
                           `_maybe_advance_focus`).
    - ATTENTION_EMISSION — proactive `attention_raised` event emission
                           (today: `_maybe_emit_attention_events`); a
                           dedicated phase because the status-report
                           cron's interesting-types skip-gate depends on
                           it firing before cron in the same tick.
    - POST_CRON          — fires during / after the cron stage (today:
                           the janitor cron job dispatches via
                           `registry.hook("tick_hook", component="janitor")`
                           inside `run_cron`). Listed here for the
                           manifest schema's completeness; daemon._tick
                           does not iterate POST_CRON itself — the cron
                           scheduler owns that invocation cadence.
    - POST_DISPATCH      — fires after task dispatch. Reserved for the
                           auto_approve gate logic when axis (5)
                           extracts it from the inline dispatch block;
                           today's stub-manifest registers a no-op on
                           this phase so the walk-everything contract
                           is uniform.
    """

    PRE_DISPATCH = "pre_dispatch"
    POST_DISPATCH = "post_dispatch"
    POST_CRON = "post_cron"
    ATTENTION_EMISSION = "attention_emission"


@dataclass(frozen=True)
class Manifest:
    """One component's declarative shape (goal.md L121-125).

    Frozen so a downstream consumer can't accidentally mutate the
    manifest after discovery; the mutable surface is `hook_points`
    (a plain dict, intentionally — tests monkeypatch entries to swap a
    hook for a stub without rebuilding the whole registry).

    TB-310 (axis 2) adds the `tick_hooks` field: a list of
    `(Phase, TickHook)` pairs declaring which phase(s) this component
    participates in. Multiple entries per phase are allowed (a
    component might register one PRE_DISPATCH hook and one POST_DISPATCH
    hook). The registry's `tick_hooks(phase)` method assembles the
    ordered list across all manifests for that phase — name-sorted by
    component, deterministic for tests. The TB-309-pinned
    `hook_points["tick_hook"]` lookup pattern is preserved alongside —
    `hook_points` indexes hooks by name regardless of phase, used by
    `run_cron`'s direct janitor lookup; `tick_hooks` indexes hooks by
    phase, used by `_tick`'s walk.
    """

    name: str
    env_flag: Optional[str]
    default_enabled: bool
    hook_points: dict[str, Callable]
    dependencies: list[str] = field(default_factory=list)
    tick_hooks: list[tuple[Phase, TickHook]] = field(default_factory=list)


class Registry:
    """Container of `Manifest`s with lookup + enabled-filtering helpers.

    Constructed from a list of manifests (one per component). Filesystem
    discovery lives in `Registry.discover()` — every test or production
    caller that wants the same set of components the daemon sees should
    use that classmethod (or `default_registry()` for the cached
    module-level singleton).
    """

    def __init__(self, components: list[Manifest]):
        self._by_name: dict[str, Manifest] = {m.name: m for m in components}

    @property
    def components(self) -> list[Manifest]:
        """All discovered manifests, in name-sorted order for stable iteration."""
        return [self._by_name[k] for k in sorted(self._by_name)]

    def get(self, component: str) -> Manifest:
        """Manifest by name. Raises KeyError if the component is unknown —
        consumer is expected to know what it's asking for.
        """
        return self._by_name[component]

    def enabled_components(self, cfg=None) -> list[Manifest]:
        """Components whose env_flag indicates enabled state (goal.md L121).

        Polarity rule:
          - `env_flag is None`                 → component is always on
                                                 (subject to `default_enabled`).
          - `env_flag set, default_enabled=True`  → truthy env var DISABLES.
          - `env_flag set, default_enabled=False` → truthy env var ENABLES.

        `cfg` is accepted for forward compatibility (axis 2 may want
        per-cfg overrides) but is unused today — we read directly from
        `os.environ` so a hot-reloaded env file (TB-271) takes effect on
        the next discovery pass.
        """
        out: list[Manifest] = []
        for m in self.components:
            if self._is_enabled(m):
                out.append(m)
        return out

    @staticmethod
    def _is_enabled(m: Manifest) -> bool:
        if m.env_flag is None:
            return m.default_enabled
        raw = os.environ.get(m.env_flag, "")
        is_truthy = raw.strip().lower() not in ("", "0", "false", "no", "off")
        if m.default_enabled:
            # env_flag DISABLES (kill switch).
            return not is_truthy
        # env_flag ENABLES (opt-in toggle).
        return is_truthy

    def hook(self, name: str, *, component: str) -> Callable:
        """Look up a single registered hook by hook-point name + component.

        Raises KeyError if the component is unknown or doesn't register
        that hook name. The caller is expected to know the contract —
        registry has no defaulting because a missing hook is a bug at
        the call site, not a runtime branch to silently skip.
        """
        manifest = self._by_name[component]
        return manifest.hook_points[name]

    def tick_hooks(self, phase: Phase) -> list[TickHook]:
        """Ordered list of tick hooks registered on `phase` (TB-310 axis 2).

        Walks every discovered manifest's `tick_hooks` field, filters
        entries whose phase matches, and returns the resulting hook
        callables in deterministic order: name-sorted by component name
        first, then by the registration order within a single manifest
        (a component may register multiple hooks on the same phase —
        rare, but the schema allows it).

        Determinism is load-bearing for the briefing's verification
        regression-pin (`uv run pytest -q
        ap2/tests/test_tb310_tick_hook_protocol.py`) and for
        observable-behavior preservation: today's `daemon._tick` fires
        `auto_unfreeze` (step 0.5) then `focus_advance` (step 0.6) on
        PRE_DISPATCH, and the alphabetical sort (`a` < `f`) preserves
        that order without an explicit ordering directive. Future
        components that need a non-alphabetical order will declare a
        `depends_on`-style constraint on the manifest (axis (2) leaves
        the topological-sort path as a stub — the `dependencies` field
        on Manifest is reserved for it; this method does not consult
        it yet because no current component needs it).
        """
        out: list[TickHook] = []
        for manifest in self.components:  # name-sorted iteration
            for entry_phase, hook in manifest.tick_hooks:
                if entry_phase is phase:
                    out.append(hook)
        return out

    @classmethod
    def discover(cls, *, components_pkg_name: str = "ap2.components") -> "Registry":
        """Walk `ap2/components/*/manifest.py` and build the registry.

        Filesystem-driven (`pkgutil.iter_modules` over the components
        package's `__path__`) — there is NO hardcoded list of component
        names; a future migration drops a subpackage and the registry
        picks it up automatically. This is load-bearing per goal.md
        L188-201.

        Each component's manifest module must expose a module-level
        `MANIFEST` attribute (a `Manifest` instance). Subpackages
        without a `manifest.py` or without a `MANIFEST` attribute are
        skipped silently — a half-converted component shouldn't crash
        the registry build during the migration cycle.
        """
        components_pkg = importlib.import_module(components_pkg_name)
        manifests: list[Manifest] = []
        for _finder, name, is_pkg in pkgutil.iter_modules(
            components_pkg.__path__
        ):
            if not is_pkg:
                continue
            try:
                manifest_mod = importlib.import_module(
                    f"{components_pkg_name}.{name}.manifest"
                )
            except ModuleNotFoundError:
                continue
            manifest = getattr(manifest_mod, "MANIFEST", None)
            if not isinstance(manifest, Manifest):
                continue
            manifests.append(manifest)
        return cls(manifests)


def default_registry() -> Registry:
    """Module-level cached `Registry.discover()` result.

    Lazy: built on first access so importing `ap2.registry` doesn't
    eagerly walk `ap2/components/` (keeps the import graph shallow for
    tests that don't touch components). Subsequent calls return the
    cached instance; `_reset_default_registry()` clears it.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = Registry.discover()
    return _DEFAULT_REGISTRY


def _reset_default_registry() -> None:
    """Clear the cached `default_registry()` instance.

    Tests use this after monkeypatching a manifest's hook_points so the
    next `default_registry()` call sees the patched value, or before a
    fresh discovery pass.
    """
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
