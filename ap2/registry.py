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
                                               (via
                                               `registry.contributions("cron_job_handlers")`)
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

    TB-388 removed the `POST_DISPATCH` member (and its `daemon._tick` walk).
    It was a placeholder for the auto_approve gate, but TB-383 moved that
    gate to a real `PRE_DISPATCH` loop pass, leaving the phase with zero
    registrants walked every tick. The promote-time gate stays inline in
    `daemon._tick`'s dispatch block.
    """

    PRE_DISPATCH = "pre_dispatch"
    POST_CRON = "post_cron"
    ATTENTION_EMISSION = "attention_emission"
    CRON_DISPATCH = "cron_dispatch"
    IDEATION = "ideation"
    # TB-389: the communication component's OUTBOUND delivery phase. The
    # `communication` component registers `run_outbound_tick` here; the
    # daemon walks `registry.tick_hooks(Phase.COMMUNICATION)` each tick to
    # drain the `ap2.notify` queue and deliver to the component's internal
    # channels. Channel multiplicity (mattermost today, slack/email later)
    # is fully behind this one phase — core no longer walks a
    # channel-adapter list (the `channel_adapters()` accessor was removed)
    # nor polls inbound via a one-off `inbound_poll` hook_point.
    COMMUNICATION = "communication"


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
    component might register one PRE_DISPATCH hook and one POST_CRON
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
    # TB-429: optional override declaring that this component's
    # enable/disable signal lives in the CORE config cluster
    # (`[core] <enable_core_key>`, read via `cfg.get_core_value`) rather
    # than the default per-component `[components.<name>]`
    # `enabled`/`disabled` key. Defaults to None → the component-scoped
    # `_enable_config_key()` key is used (the existing, unchanged path
    # for every `[components.*]`-keyed component). Ideation sets it to
    # `"ideation_disabled"` so `is_enabled`'s config tier reads the SAME
    # core key its self-gate (`_ideation_disabled` →
    # `cfg.get_core_value("ideation_disabled")`) reads — the registry
    # view (`ap2 status` / `ap2 doctor`) can no longer disagree with the
    # gate for a core-keyed component. Polarity is still carried by
    # `default_enabled` (suppress for ideation), so the core key name is
    # a suppress-polarity `*_disabled` knob, consistent with the
    # convention at the top of this module.
    enable_core_key: Optional[str] = None
    # TB-430: optional DEPRECATED flat env flag from a PRIOR polarity of
    # this manifest, honored as a transitional back-compat override of
    # the current default so an existing deployment that still sets the
    # old name does not silently change behavior on upgrade. Carries the
    # OPPOSITE polarity of `env_flag` from the manifest's previous life:
    # auto_approve flipped default-off→default-on (require→suppress), so
    # its `legacy_env_flag` (`AP2_AUTO_APPROVE`) is REQUIRE-polarity
    # (`=1` enables, `=0`/falsy disables). Resolved as a final tier in
    # `is_enabled`, consulted ONLY when none of the current knobs (tiers
    # 1–3: sectioned env / flat `env_flag` / config.toml) are set, so the
    # modern knobs always win and the legacy flag merely overrides the
    # bare default. Defaults to None → no legacy alias (every other
    # manifest). The owning component is responsible for emitting a
    # one-time deprecation note when it observes the legacy flag set.
    legacy_env_flag: Optional[str] = None

    def _enable_config_key(self) -> str:
        """The per-component config key carrying this manifest's
        enable/disable signal, matching the polarity convention
        (TB-427): suppress-polarity (`default_enabled=True`) reads the
        `disabled` key; require-polarity (`default_enabled=False`) reads
        the `enabled` key. This is the same key the component's own
        config-aware self-gate (`cfg.get_component_value(name, key)`)
        consults, so the registry view and the gate read ONE key.
        """
        return "disabled" if self.default_enabled else "enabled"

    def is_enabled(self, env: Optional[dict] = None, cfg=None) -> bool:
        """Resolve this manifest's enabled state (TB-319 / TB-427).

        Single source of truth for the polarity convention codified at
        the top of this module (`env_flag is None` → `default_enabled`;
        `default_enabled=True` → env_flag is suppress-polarity / kill
        switch; `default_enabled=False` → env_flag is require-polarity
        / opt-in toggle). `Registry._is_enabled` delegates here so the
        registry's enabled-walk and the `ap2 status` `## Components`
        enumeration (TB-319) share one implementation — a future
        polarity edit ripples through both surfaces without drift.

        TB-427 — ONE config-aware precedence, resolved in three tiers so
        the registry layer (status / doctor / `enabled_components`), the
        TB-379 shell-pin snapshot, AND the component's own self-gate all
        read the SAME enable/disable signal and can never disagree:

          1. sectioned env `AP2_COMPONENTS_<NAME>_<KEY>` — the spelling
             `Config.get_component_value` honors as its top tier.
          2. the flat operational env flag (`self.env_flag`, e.g.
             `AP2_AUTO_APPROVE` / `AP2_JANITOR_DISABLED`), read DIRECTLY
             from `env`. TB-413 keeps these flat tunable names out of
             `ENV_PERMITTED_KEYS`, so `get_component_value` would ignore
             them; this tier re-permits the specific on/off master flags
             for the *enablement* read (briefing option A) — a
             shell-pinned flat kill switch / opt-in toggle still flips
             status/doctor/registry, preserving pre-TB-427 behavior.
          3. config.toml — only when env tiers 1+2 are silent, via the
             gate's accessor (which at this point resolves to its
             config.toml snapshot → default, env having been ruled out
             above). The accessor is chosen by the component's DECLARED
             enablement source (TB-429): a `[components.*]`-keyed
             component reads `cfg.get_component_value(name, key)`
             (`[components.<name>] enabled`/`disabled`); a core-keyed
             component (one that sets `enable_core_key`, e.g. ideation →
             `ideation_disabled`) reads `cfg.get_core_value(
             enable_core_key)` (`[core] <enable_core_key>`) — the SAME
             core key its own self-gate reads, so the two can never
             disagree. TB-427 added the `[components.<name>]` surface;
             TB-429 extends the unification to core-keyed components.

        Env beats config.toml (tiers 1+2 before tier 3), matching the
        accessor's own precedence so both layers agree.

        TB-430 — a FINAL legacy tier follows tiers 1–3: if a manifest
        declares `legacy_env_flag` (a deprecated flat flag from a PRIOR
        polarity, e.g. auto_approve's `AP2_AUTO_APPROVE`) and that flag
        is EXPLICITLY present in `env` while every current knob is silent,
        its require-polarity truthy value is returned directly (`=1`
        enables, `=0`/falsy disables). This honors an un-migrated
        deployment's old flag as a transitional override of the bare
        default WITHOUT letting it shadow the modern knobs. A
        not-present legacy flag falls through to the manifest default.

        `cfg` is duck-typed (`hasattr(cfg, "get_component_value")`) so a
        dummy `cfg` (some tick-hook unit tests pass `object()`) or
        `cfg=None` cleanly skips tier 3 and resolves from env alone —
        the env-only path the TB-319 synthetic-env unit tests and the
        TB-379 shell-pin snapshot depend on. All env reads use
        `.get(name, "")` so a missing key is the same empty-string falsy
        as `os.environ.get(name, "")`.
        """
        if self.env_flag is None:
            return self.default_enabled
        if env is None:
            env = os.environ
        key = self._enable_config_key()
        # Tier 1: sectioned env (the name get_component_value honors).
        raw = env.get(f"AP2_COMPONENTS_{self.name.upper()}_{key.upper()}", "")
        # Tier 2: the flat operational master flag, read directly.
        if raw == "":
            raw = env.get(self.env_flag, "")
        # Tier 3: config.toml (env silent) via the gate's accessor. The
        # accessor is routed to the component's DECLARED enablement
        # source (TB-429): a core-keyed component (`enable_core_key` set,
        # e.g. ideation) reads `cfg.get_core_value(enable_core_key)`; the
        # default `[components.<name>]` path reads
        # `cfg.get_component_value(name, key)`.
        if raw == "" and cfg is not None:
            val = None
            if self.enable_core_key is not None and hasattr(cfg, "get_core_value"):
                val = cfg.get_core_value(self.enable_core_key, default="")
            elif hasattr(cfg, "get_component_value"):
                val = cfg.get_component_value(self.name, key, default="")
            raw = "" if val is None else val
        # Tier 4 (TB-430): a DEPRECATED legacy flat flag from a prior
        # polarity, consulted only when tiers 1–3 are all silent. It is
        # require-polarity regardless of the current `default_enabled`
        # (auto_approve was default-off/opt-in before the flip), so when
        # it is EXPLICITLY present in env we resolve it here and return
        # directly rather than feeding the current-polarity branch below.
        if raw == "" and self.legacy_env_flag is not None:
            legacy_raw = env.get(self.legacy_env_flag, None)
            if legacy_raw is not None:
                return str(legacy_raw).strip().lower() not in (
                    "", "0", "false", "no", "off",
                )
        is_truthy = str(raw).strip().lower() not in (
            "", "0", "false", "no", "off",
        )
        if self.default_enabled:
            # env_flag / `disabled` key DISABLES (kill switch / suppress).
            return not is_truthy
        # env_flag / `enabled` key ENABLES (opt-in toggle / require).
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
        """Components whose enabled-state is on (goal.md L121).

        Polarity rule:
          - `env_flag is None`                 → component is always on
                                                 (subject to `default_enabled`).
          - `env_flag set, default_enabled=True`  → truthy signal DISABLES.
          - `env_flag set, default_enabled=False` → truthy signal ENABLES.

        TB-427: `cfg` is now threaded through to `Manifest.is_enabled`
        so the enabled-walk is config-aware — a `[components.<name>]`
        config.toml key (or sectioned env) turns a component on/off, and
        this filter agrees with the component's own gate. With `cfg=None`
        the walk reads `os.environ` directly (env-only), so a
        hot-reloaded env file (TB-271) takes effect on the next
        discovery pass exactly as before.
        """
        out: list[Manifest] = []
        for m in self.components:
            if self._is_enabled(m, cfg):
                out.append(m)
        return out

    @staticmethod
    def _is_enabled(m: Manifest, cfg=None) -> bool:
        """Polarity-respecting enabled check (delegates to `Manifest.is_enabled`).

        TB-319: the polarity rule's body moved onto `Manifest.is_enabled`
        so both the registry walk (here) and the new `ap2 status`
        `## Components` enumeration share one implementation. TB-427
        threads an optional `cfg` so the check is config-aware when a
        Config is available. The existing TB-309 canary tests still call
        `Registry._is_enabled(janitor_manifest)` directly (no `cfg`) —
        preserved as a thin staticmethod shim so the call shape is
        unchanged.
        """
        return m.is_enabled(cfg=cfg)

    def hook(self, name: str, *, component: str) -> Callable:
        """Look up a single registered hook by hook-point name + component.

        Raises KeyError if the component is unknown or doesn't register
        that hook name. The caller is expected to know the contract —
        registry has no defaulting because a missing hook is a bug at
        the call site, not a runtime branch to silently skip.
        """
        manifest = self._by_name[component]
        return manifest.hook_points[name]

    # TB-389: the `channel_adapters(cfg)` accessor was REMOVED. Channel
    # multiplicity (mattermost today, slack/email later) is no longer a
    # kernel concern — the `communication` component
    # (`ap2/components/communication/`) owns the channel surface in both
    # directions and holds its channel adapters in an INTERNAL registry
    # (`communication.channels.channel_registry`) that core cannot see.
    # Core's outbound path is now event-driven: a call site appends to the
    # `ap2.notify` queue and the communication component's
    # `Phase.COMMUNICATION` tick hook delivers. There is therefore no
    # `channel_adapter` hook_point and no registry-level channel walk —
    # `contributions("channel_adapters")` is NOT a core extension point
    # (channels are owned wholly by one component; see `contributions`'s
    # docstring note).

    # TB-386 (axis 5a): the per-kind `briefing_validators()` and
    # `verifier_judge()` accessors were removed. Both LLM judges they
    # resolved (the dep-coherence briefing judge and the prose-bullet verify
    # judge) are internal sub-steps of core runners, not loop-level
    # participants, so TB-386 demoted them out of `ap2/components/` back into
    # core (`ap2/briefing_validators.py` / `ap2/verify.py`), each still
    # resolving its backend via `select_adapter(<kind>, cfg)`. With them gone
    # the registry has no remaining per-kind accessor for either judge.

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

    def contributions(self, point: str, cfg=None):
        """Generic fan-out accessor for a named hook point (TB-387).

        The single replacement for the registry's bespoke per-kind
        fan-out methods: walk every manifest's `hook_points.get(point)`
        in name-sorted order and merge the contributions into one
        aggregate. The aggregate shape follows the contributions
        themselves — dict-shaped points (e.g. cron's
        `dict[str, JobHandler]`) dict-merge via `.update()` (later
        manifests win on a key collision); list-shaped points list-merge
        via `.extend()`; a scalar (non-dict, non-list) contribution is
        appended to the list. A point no manifest contributes to yields
        an empty list.

        This is FAN-OUT ONLY: the registry assembles and returns the
        merged contributions and performs NO keyed dispatch. Keying stays
        consumer-local — the cron scheduler still does
        `handlers.get(job.name, DEFAULT)` itself. A surface earns a
        generic `contributions(point)` only when multiple owners feed it
        AND it stays in core (cron job handlers — contributed by the
        janitor component AND core); a surface owned wholly by one
        component is internal to that component, not a core point.
        `channel_adapters()` is therefore NOT routed here — channels are
        owned wholly by the communication component (TB-389) and never
        become a core extension point.

        Walk scope preserves the per-point "walk-all vs walk-enabled"
        semantics via `cfg`: with `cfg is None` (the cron job-handler
        case) the accessor walks ALL discovered manifests — a contributed
        handler dispatches regardless of the component's env_flag kill
        switch; with a `cfg` supplied it walks only
        `enabled_components(cfg)`. Name-sorted iteration order is
        load-bearing: the merge is deterministic (later manifests
        overwrite earlier ones on a dict key collision, and list order is
        stable across daemon restarts).
        """
        manifests = self.components if cfg is None else self.enabled_components(cfg)
        merged_dict: dict = {}
        merged_list: list = []
        is_dict = False
        for manifest in manifests:  # name-sorted iteration
            contribution = manifest.hook_points.get(point)
            if not contribution:
                continue
            if isinstance(contribution, dict):
                is_dict = True
                merged_dict.update(contribution)
            elif isinstance(contribution, (list, tuple)):
                merged_list.extend(contribution)
            else:
                merged_list.append(contribution)
        return merged_dict if is_dict else merged_list

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
