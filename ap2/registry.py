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
                      cron_job_handlers      — TB-381 axis 1: a
                                               dict[str, JobHandler] a
                                               component contributes to the
                                               cron job-handler registry. The
                                               cron scheduler aggregates these
                                               (via `registry.cron_job_handlers()`)
                                               with the core-registered
                                               handlers and dispatches a due
                                               job to its named handler —
                                               replacing the pre-TB-381
                                               `if job.name == …` switch. The
                                               janitor component contributes
                                               `{"janitor": <handler>}` here.
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

# TB-321: per-component config-schema declarations live on the
# manifest as `config_schema: dict[str, ConfigKey]`. The ConfigKey
# type and the daemon-start `validate_config` helper live in
# `ap2.config_loader` (axis-1 of the structured-config focus). The
# import is top-level rather than TYPE_CHECKING-guarded so a
# manifest body referencing `ConfigKey(...)` at module load time
# resolves without a forward-string-annotation dance. There is no
# cycle: `config_loader.py` does not import `registry.py` at module
# scope (it lazy-imports `Config` only inside `from_toml`).
from .config_loader import ConfigKey


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
                           inside the cron scheduler). Listed here for
                           the manifest schema's completeness; daemon._tick
                           does not iterate POST_CRON itself — the cron
                           scheduler owns that invocation cadence.
    - POST_DISPATCH      — fires after task dispatch. Reserved for the
                           auto_approve gate logic when axis (5)
                           extracts it from the inline dispatch block;
                           today's stub-manifest registers a no-op on
                           this phase so the walk-everything contract
                           is uniform.
    - CRON_DISPATCH      — TB-381 axis 1: the cron *scheduler* phase. The
                           cron component (`ap2/components/cron/`)
                           registers its due-check-loop + per-job
                           handler-dispatch tick hook here; `daemon._tick`
                           walks `registry.tick_hooks(Phase.CRON_DISPATCH)`
                           at step 1 instead of running the cron loop
                           inline (the pre-TB-381 `load_jobs` → `due_jobs`
                           → `run_cron` block). The scheduler self-gates
                           on `AP2_CRON_DISABLED` and owns the `cron_*`
                           lifecycle events. This is the first tick-stage
                           extraction; it pins the tick-phase + tick-hook +
                           import-direction shape the later extractions
                           (axis 3 — ideation) reuse mechanically.
    - IDEATION           — TB-381 axis 1: reserved for the ideation
                           extraction (axis 3). The phase is added now so
                           axis 3 only needs to ship the ideation
                           subpackage + register its tick hook here — no
                           registry-side edit. `daemon._tick` already walks
                           `registry.tick_hooks(Phase.IDEATION)` (empty
                           today — `ideation._maybe_ideate` stays a direct
                           core call until axis 3 moves it), so the walk is
                           in place the moment a hook is registered.
    """

    PRE_DISPATCH = "pre_dispatch"
    POST_DISPATCH = "post_dispatch"
    POST_CRON = "post_cron"
    ATTENTION_EMISSION = "attention_emission"
    CRON_DISPATCH = "cron_dispatch"
    IDEATION = "ideation"


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
    # TB-321 (axis 1): per-component config-schema declarations keyed
    # by the bare TOML key name (e.g. `"disabled"` for the janitor's
    # `[components.janitor] disabled = true` knob). The registry
    # aggregates the union across all manifests in
    # `config_loader.aggregate_schemas(registry)`; the daemon-start
    # `validate_config` walks `[components.<name>]` sub-tables against
    # the union. Default empty so the six non-canary manifests
    # (mattermost, attention, focus_advance, auto_unfreeze,
    # auto_approve, validator_judge) continue to load until TB-322
    # fills in their per-component schemas (axis 3).
    config_schema: dict[str, ConfigKey] = field(default_factory=dict)

    def is_enabled(self, env: Optional[dict] = None) -> bool:
        """Resolve this manifest's enabled state against `env` (TB-319).

        Single source of truth for the polarity convention codified at
        the top of this module (`env_flag is None` → `default_enabled`;
        `default_enabled=True` → env_flag is suppress-polarity / kill
        switch; `default_enabled=False` → env_flag is require-polarity
        / opt-in toggle). `Registry._is_enabled` delegates here so the
        registry's enabled-walk and the `ap2 status` `## Components`
        enumeration (TB-319) share one implementation — a future
        polarity edit ripples through both surfaces without drift.

        `env` defaults to `os.environ` when None so callers can pass a
        synthetic mapping in tests without monkeypatching the process
        env. Reads via `.get(name, "")` so a missing key is the same
        empty-string falsy as `os.environ.get(name, "")` in the
        original `Registry._is_enabled` body.
        """
        if env is None:
            env = os.environ
        if self.env_flag is None:
            return self.default_enabled
        raw = env.get(self.env_flag, "")
        is_truthy = raw.strip().lower() not in ("", "0", "false", "no", "off")
        if self.default_enabled:
            # env_flag DISABLES (kill switch / suppress polarity).
            return not is_truthy
        # env_flag ENABLES (opt-in toggle / require polarity).
        return is_truthy

    def env_flag_description(self, env: Optional[dict] = None) -> str:
        """Human-readable env-flag state for `ap2 status` (TB-319).

        Shape matches the briefing's text-render contract:
          - `env_flag=None`                  — always-on manifests
                                              (post-TB-320: `attention/`
                                              is the only such manifest
                                              — operator decision per
                                              2026-05-28).
          - `<NAME> unset`                   — env var absent or empty.
          - `<NAME>=<value>`                 — env var set to a non-empty
                                              value (value is truncated
                                              at 32 chars with an
                                              ellipsis so a long
                                              channel-id list / opaque
                                              token doesn't blow up the
                                              status block width).

        Pure read-layer; no polarity decision here — pair with
        `is_enabled(env)` for the on/off bit.
        """
        if env is None:
            env = os.environ
        if self.env_flag is None:
            return "env_flag=None"
        raw = env.get(self.env_flag, "")
        if not raw:
            return f"{self.env_flag} unset"
        # Truncate the value so a long AP2_MM_CHANNELS=channel-id,channel-id,...
        # list doesn't wrap the status block. 32 chars + ellipsis is wide
        # enough to keep a single channel-id readable for the common case.
        if len(raw) > 32:
            raw = raw[:29] + "..."
        return f"{self.env_flag}={raw}"


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
        """Polarity-respecting enabled check (delegates to `Manifest.is_enabled`).

        TB-319: the polarity rule's body moved onto `Manifest.is_enabled`
        so both the registry walk (here) and the new `ap2 status`
        `## Components` enumeration share one implementation. The
        existing TB-309 canary tests still call
        `Registry._is_enabled(janitor_manifest)` directly — preserved
        as a thin staticmethod shim so the call shape is unchanged.
        """
        return m.is_enabled()

    def hook(self, name: str, *, component: str) -> Callable:
        """Look up a single registered hook by hook-point name + component.

        Raises KeyError if the component is unknown or doesn't register
        that hook name. The caller is expected to know the contract —
        registry has no defaulting because a missing hook is a bug at
        the call site, not a runtime branch to silently skip.
        """
        manifest = self._by_name[component]
        return manifest.hook_points[name]

    def channel_adapters(self, cfg=None) -> list:
        """Ordered list of `ChannelAdapter` instances from enabled components
        (TB-312 axis 3).

        Walks every enabled manifest, instantiates its
        `hook_points["channel_adapter"]` factory (or treats the entry as an
        already-built adapter when it's not callable — components may
        register either a class or a module-level singleton), and returns
        the resulting list in deterministic component-name-sorted order.

        Determinism is load-bearing: today's three call sites (the
        attention immediate-push at `daemon.py`, the watchdog's
        no-destination + main-fire paths at `watchdog.py`, and the
        status-report delivery path) iterate the list and best-effort
        post to each adapter. Stable iteration order means a future
        component (e.g. `slack/`) joins the list in a predictable spot —
        adapter logs / dedup state files don't shuffle on each daemon
        restart.

        Empty list is a legitimate return — when no component declares
        a `channel_adapter` hook point (or all such components are
        disabled by their env_flag), the caller's `_deliver(...)`
        helper observes "no destination" and emits the
        `*_no_destination` audit event family that pre-TB-312 watchdog
        used. The behavior preserves goal.md L156-157's "the digest's
        default destination is non-null when Mattermost is disabled"
        only when at least one core sibling adapter (Stdout, FileAppend,
        Webhook) is wired into a component manifest — TB-312 ships the
        core ABC + adapters; downstream TBs may register them on
        component manifests as project conventions evolve.

        `cfg` is accepted for forward compatibility (an adapter factory
        may want per-cfg knobs); unused today — adapters that read env
        do so lazily inside `.post()` so a hot-reloaded env (TB-271)
        applies on the next dispatch pass.
        """
        out: list = []
        for manifest in self.enabled_components(cfg):
            factory = manifest.hook_points.get("channel_adapter")
            if factory is None:
                continue
            try:
                adapter = factory() if callable(factory) else factory
            except Exception:  # noqa: BLE001
                # A factory raise must not abort the walk — other
                # adapters may still be deliverable. The caller's
                # per-adapter try/except in `_deliver(...)` would catch
                # a post-side raise; here we swallow construction
                # failures and continue so the registry stays usable
                # even on a half-broken component install.
                continue
            out.append(adapter)
        return out

    def briefing_validators(self, cfg=None) -> list[Callable]:
        """Ordered list of `BriefingValidator` callables registered on
        enabled components (TB-316 axis 4).

        Walks every enabled manifest's
        `hook_points["briefing_validator"]` and returns the resulting
        callables in deterministic component-name-sorted order. Mirrors
        the structural shape of `tick_hooks(phase)` and
        `channel_adapters(cfg)`: name-sorted walk over enabled
        manifests, skip entries that don't carry the named hook point.

        Determinism is load-bearing for
        `_validate_briefing_structure`'s pipeline-as-list orchestrator
        (TB-316). The five deterministic structural checks (sections,
        goal-anchor, why-now, no-manual-bullets, no-fenced-paths-in-
        scope) live in core and always run first; the registry-walked
        list is appended after them, so a future component that
        registers an additional `briefing_validator` hook (validator-
        chain extension is the explicit forward-compatibility point of
        the refactor) slots in at the end without rewriting core.

        Empty list is a legitimate return — when no enabled component
        declares a `briefing_validator` hook point, the validator's
        orchestrator simply walks the core list. This is the path
        unit tests take when the entire components surface is shielded
        via env flag.

        `cfg` is accepted for forward compatibility (a validator
        callable may want per-cfg knobs); unused today — validators
        that read env do so lazily inside their own body so a hot-
        reloaded env (TB-271) applies on the next call.
        """
        out: list[Callable] = []
        for manifest in self.enabled_components(cfg):
            validator = manifest.hook_points.get("briefing_validator")
            if validator is None:
                continue
            out.append(validator)
        return out

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
        `auto_unfreeze` (the sole PRE_DISPATCH registry hook post-TB-345)
        and then calls the core `ideation_halt.maybe_halt_on_exhaustion`
        directly (step 0.6 — no longer a registry hook). Future
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

    def cron_job_handlers(self) -> dict[str, Callable]:
        """Aggregated cron job-handler map contributed by components (TB-381).

        Walks every discovered manifest's
        `hook_points.get("cron_job_handlers")` (a `dict[str, handler]`)
        and merges them into a single name→handler map. The cron
        scheduler (`ap2/components/cron/`) overlays this on top of the
        core-registered handlers (`ap2.cron_handlers.CORE_CRON_HANDLERS`)
        and dispatches a due job to `handlers.get(job.name,
        DEFAULT_CRON_HANDLER)` — the direct replacement for `run_cron`'s
        pre-TB-381 `if job.name == …` switch. Each handler is a
        self-contained `async (cfg, sdk, mcp_server, job) -> None`
        callable that owns its own `cron_*` lifecycle events + `mark_run`
        (so the scheduler "knows nothing of what the job does").

        Walks ALL components (not enabled-filtered) to preserve the
        pre-TB-381 behavior where the janitor cron job dispatched its
        handler unconditionally via `registry.hook("tick_hook",
        component="janitor")` regardless of the `AP2_JANITOR_DISABLED`
        kill switch. Today only the `janitor` component contributes a
        handler (`{"janitor": run_janitor_cron}`); there are no key
        collisions with the core handler names (`status-report`,
        `real-sdk-smoke`).
        """
        out: dict[str, Callable] = {}
        for manifest in self.components:  # name-sorted iteration
            handlers = manifest.hook_points.get("cron_job_handlers")
            if not handlers:
                continue
            out.update(handlers)
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
