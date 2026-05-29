"""TB-323: env-var override layer + back-compat map for flat AP2_* knobs.

Axis (2) of the **structured config (env → TOML)** focus (goal.md
L317-329). This module ships three load-bearing surfaces:

  1. `FLAT_TO_SECTIONED` — the operator-facing contract mapping every
     existing flat `AP2_*` env knob (as audited 2026-05-28) to its
     sectioned counterpart (e.g. `AP2_AUTO_APPROVE` →
     `components.auto_approve.enabled`, `AP2_TICK_S` →
     `core.tick_interval_s`). Anything in this map gets a back-compat
     read path with a deprecation event; anything in
     `_KNOBS_STAYING_ENV_ONLY` is documented-permanent env-only. The
     two sets partition the existing `AP2_*` namespace; a regression-pin
     test asserts the partition is total against
     `ap2.init._TEMPLATE_EXEMPT_KNOBS` (TB-305's source-of-truth set).
  2. `apply_env_overrides(cfg)` — called from `config_loader.from_toml`
     after the TOML overlay. For each `AP2_<SECTION>_<KEY>` env name
     present in `os.environ` that matches a key already populated on
     the loaded config (sectioned-env path), override the value. For
     each flat `AP2_*` key in `FLAT_TO_SECTIONED` present in
     `os.environ` (back-compat path), apply the override to the
     matching sectioned path AND emit a one-shot `env_deprecated` event
     per process per knob.
  3. `_KNOBS_STAYING_ENV_ONLY` — frozenset of true 12-factor knobs
     (Mattermost auth / channel identity, integration secrets,
     deployment-environment paths) that never migrate to TOML per
     goal.md L356-358. A single comment block above the frozenset
     documents the cut-line for auditability per goal.md L361.

Precedence order (high → low):
  sectioned env > flat env (back-compat) > TOML file > in-source defaults.

Same shape as today's "shell export wins over `.cc-autopilot/env`"
rule, extended one precedence level lower.

Import-direction (TB-311 parity): this module must NOT statically
import from `ap2.components`. Schema declarations live on component
manifests; the registry walk (`aggregate_schemas`) is the cross-
reference path, but it's not used here — the back-compat layer
operates purely against the loaded `Config` instance's
`components_config` dict and the dataclass fields. A `! grep -qE
"^from ap2\\.components"` check pins this invariant
(`grep -q` exit-zero on absence).

Why a separate module (not folded into `config_loader.py`): the
back-compat surface is a vocabulary unto itself — a public map an
operator can read, a public partition that pins the migration
boundary, and a one-shot emission helper. `config_loader.py` owns the
schema/parser/validator; this module owns the env-layer plumbing
+ deprecation accounting. Keeping them separate keeps each focused.
"""
from __future__ import annotations

import os
import threading
from dataclasses import fields as _dataclass_fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from .config import Config


# ---------------------------------------------------------------------------
# FLAT_TO_SECTIONED — the operator-facing back-compat map.
#
# Every flat `AP2_*` knob currently read from `os.environ` (per the 2026-05-28
# `_collect_env_knobs()` audit — 50+ entries) is either listed here OR in
# `_KNOBS_STAYING_ENV_ONLY` below. The two sets partition the existing
# `AP2_*` namespace.
#
# Sectioned path convention (matches goal.md L307-310):
#   - `core.<key>` for non-component tunables (verifier, ideation, control-
#     agent, cron, watchdog, project identity).
#   - `components.<name>.<key>` for component-owned knobs, where `<name>`
#     matches the component package directory under `ap2/components/`.
#
# Key naming on the sectioned side follows snake_case in the Python convention
# (NOT the upper-cased env shape) — `[core.tick_interval_s] = 30`, not
# `[core.TICK_INTERVAL_S]`. This is the contract the
# `Config.from_toml` overlay reads (`[core.<key>]` overlays onto
# `Config.<key>` by name) and the contract a future `ap2 config list`
# (axis-4) will render.
# ---------------------------------------------------------------------------
FLAT_TO_SECTIONED: dict[str, str] = {
    # --- Core / non-component tunables (Config dataclass fields) -----------
    "AP2_TICK_S": "core.tick_interval_s",
    "AP2_MM_TICK_S": "core.mm_tick_interval_s",
    "AP2_EVENT_CONTEXT": "core.event_context_size",
    "AP2_TASK_TIMEOUT_S": "core.task_timeout_s",
    "AP2_CONTROL_TIMEOUT_S": "core.control_timeout_s",
    "AP2_MAX_RETRIES": "core.max_retries",
    "AP2_VERIFY_CMD": "core.verify_cmd",
    "AP2_VERIFY_TIMEOUT_S": "core.verify_timeout_s",
    "AP2_AUTO_DIAGNOSE_COOLDOWN_S": "core.auto_diagnose_cooldown_s",
    "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S": "core.auto_diagnose_idle_threshold_s",
    "AP2_PROJECT_NAME": "core.project_name",
    # --- Core / agent shape (read fresh from os.environ at SDK call time) ---
    "AP2_AGENT_MODEL": "core.agent_model",
    "AP2_AGENT_EFFORT": "core.agent_effort",
    "AP2_TASK_MAX_TURNS": "core.task_max_turns",
    "AP2_CONTROL_MAX_TURNS": "core.control_max_turns",
    "AP2_IDEATION_MAX_TURNS": "core.ideation_max_turns",
    "AP2_STATUS_REPORT_EFFORT": "core.status_report_effort",
    "AP2_VERIFY_JUDGE_EFFORT": "core.verify_judge_effort",
    "AP2_VERIFY_JUDGE_MAX_TURNS": "core.verify_judge_max_turns",
    # --- Core / lifecycle (FIXED_KNOBS — restart required even after TOML) -
    "AP2_WEB_PORT": "core.web_port",
    "AP2_WEB_DISABLED": "core.web_disabled",
    # --- Core / ideation -----------------------------------------------------
    "AP2_IDEATION_DISABLED": "core.ideation_disabled",
    "AP2_IDEATION_TRIGGER_TASK_COUNT": "core.ideation_trigger_task_count",
    "AP2_IDEATION_COOLDOWN_S": "core.ideation_cooldown_s",
    "AP2_IDEATION_SCRUB_MODEL": "core.ideation_scrub_model",
    # --- auto_approve component ---------------------------------------------
    "AP2_AUTO_APPROVE": "components.auto_approve.enabled",
    "AP2_AUTO_APPROVE_DRY_RUN": "components.auto_approve.dry_run",
    "AP2_AUTO_APPROVE_GATE_TAGS": "components.auto_approve.gate_tags",
    "AP2_AUTO_APPROVE_FREEZE_THRESHOLD": "components.auto_approve.freeze_threshold",
    "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP": "components.auto_approve.per_task_token_cap",
    "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP": "components.auto_approve.window_token_cap",
    "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED": "components.auto_approve.noisy_pause_disabled",
    "AP2_AUTO_APPROVE_COST_APPROACH_PCT": "components.auto_approve.cost_approach_pct",
    # --- auto_unfreeze component --------------------------------------------
    "AP2_AUTO_UNFREEZE_DISABLED": "components.auto_unfreeze.disabled",
    "AP2_AUTO_UNFREEZE_FIX_SHAPES": "components.auto_unfreeze.fix_shapes",
    "AP2_AUTO_UNFREEZE_DRY_RUN": "components.auto_unfreeze.dry_run",
    "AP2_AUTO_UNFREEZE_MAX_PER_TASK": "components.auto_unfreeze.max_per_task",
    "AP2_AUTO_UNFREEZE_MAX_PER_DAY": "components.auto_unfreeze.max_per_day",
    # --- attention component (proactive detectors) --------------------------
    "AP2_TASK_STUCK_THRESHOLD_S": "components.attention.task_stuck_threshold_s",
    "AP2_TASK_FROZEN_RECENCY_S": "components.attention.task_frozen_recency_s",
    "AP2_ATTENTION_DEBOUNCE_S": "components.attention.debounce_s",
    "AP2_ATTENTION_IMMEDIATE_PUSH": "components.attention.immediate_push",
    # --- focus_advance component --------------------------------------------
    # TB-329: the sectioned target on the left side of `AP2_FOCUS_AUTO_ADVANCE_DISABLED`
    # was originally `components.focus_advance.disabled` (TB-323
    # initial map) but TB-322's manifest schema named the key
    # `auto_advance_disabled` (see
    # `ap2/components/focus_advance/manifest.py` config_schema) and
    # `ap2/howto.md` documents `components.focus_advance.auto_advance_disabled`
    # to the operator. The bare `disabled` form was a latent bug — under
    # it, a flat `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` would populate
    # `cfg.components_config["focus_advance"]["disabled"]` (per the
    # `apply_env_overrides` write path) but the TB-329 read-site
    # migration calls `cfg.get_component_value("focus_advance",
    # "auto_advance_disabled")`, whose reverse-`FLAT_TO_SECTIONED` lookup
    # would miss the legacy flat env and the cfg-snapshot fallback would
    # miss the wrongly-keyed write — net effect: the operator's flat env
    # value would silently disappear once the read site swapped. Align
    # the back-compat map with the schema + docs so the three surfaces
    # (TB-322 schema, TB-323 back-compat map, TB-329 read site) agree.
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED": "components.focus_advance.auto_advance_disabled",
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES": "components.focus_advance.empty_cycles",
    # --- janitor component --------------------------------------------------
    "AP2_JANITOR_DISABLED": "components.janitor.disabled",
    "AP2_JANITOR_JUDGE_EFFORT": "components.janitor.judge_effort",
    "AP2_JANITOR_JUDGE_MAX_TURNS": "components.janitor.judge_max_turns",
    "AP2_JANITOR_MAX_FINDINGS_LLM": "components.janitor.max_findings_llm",
    # --- validator_judge component ------------------------------------------
    "AP2_VALIDATOR_JUDGE_DISABLED": "components.validator_judge.disabled",
    "AP2_VALIDATOR_JUDGE_MAX_TOKENS": "components.validator_judge.max_tokens",
    "AP2_VALIDATOR_JUDGE_MAX_TURNS": "components.validator_judge.max_turns",
    "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD": "components.validator_judge.noisy_threshold",
    "AP2_VALIDATOR_JUDGE_TIMEOUT_S": "components.validator_judge.timeout_s",
}


# ---------------------------------------------------------------------------
# _KNOBS_STAYING_ENV_ONLY — the documented-permanent 12-factor exemption set.
#
# Per goal.md L356-358, these knobs NEVER migrate to TOML. The cut-line is
# operator-auditable here in a single comment block — a future reader asking
# "should this knob graduate to TOML?" reads this comment + the partition
# test (`ap2/tests/test_tb323_config_compat.py::
# test_template_exempt_partition_is_total`) to see why each one stays.
#
# TB-338 enforcement gate: `ap2/tests/test_tb338_env_only_cut_line.py`
# closes goal.md progress signal 6 (L401-403, "clearly minimal") by
# asserting (a) the disjointness of FLAT_TO_SECTIONED and this set, and
# (b) that every direct `os.environ.get("AP2_…")` AST call node under
# `ap2/` reads a knob that is EITHER in this set, in the
# bootstrap allowlist (`ap2/config.py` / `ap2/env_reload.py`), or in the
# test module's documented `_PENDING_MIGRATION_KNOBS` debt set. A future
# PR that adds a new direct env read outside those carve-outs fails CI
# until the author picks one of the four remediation paths the test's
# failure message enumerates.
#
# Cut-line rationale (per knob):
#   - Integration secrets / auth tokens: Mattermost bot identity and team
#     identity are auth-bearing — they belong in shell-exported env (or a
#     12-factor secrets manager) where they can be rotated independently of
#     project-tracked config files. Same shape as the long-standing
#     Mattermost-prefixed envs in `.cc-autopilot/env` that this project
#     never commits.
#   - Webhook destinations: `AP2_WEBHOOK_URL` is the same shape — a per-
#     deployment integration secret with no project-tracked counterpart.
#   - Deployment-environment paths: `AP2_CHANNEL_FILE_PATH` (the channel
#     adapter's file destination) is a sandbox-/deployment-specific path,
#     not a project-tracked tunable; an operator running multiple daemons
#     against the same project would have different values per daemon and
#     committing one to TOML would force per-deployment forks.
#   - Lifecycle / channel-subscription identity: `AP2_MM_CHANNELS` is the
#     daemon-start MM subscription set — read once at startup, never
#     re-applied without a daemon restart (FIXED_KNOBS). Treating it as
#     an env-only knob keeps it out of the TOML migration's hot-reload
#     story and avoids the "operator edits config.toml; daemon doesn't
#     pick up the channel change" footgun.
#   - Future placeholders (`AP2_DIR`, `AP2_REAL_SDK`) named in goal.md
#     L358 are listed for forward-compatibility — neither is currently
#     read in source, but the goal.md cut-line documents them as
#     env-only so a future addition stays on the right side of the
#     partition without an architectural debate.
# ---------------------------------------------------------------------------
_KNOBS_STAYING_ENV_ONLY: frozenset[str] = frozenset({
    # Mattermost integration secrets / auth identity.
    "AP2_MM_BOT_USER_ID",
    "AP2_MM_TEAM_ID",
    "AP2_MM_REPORT_CHANNEL",
    "AP2_MM_MENTION",
    # Mattermost channel-subscription identity (lifecycle / FIXED_KNOBS).
    "AP2_MM_CHANNELS",
    # Webhook integration secret (per-deployment URL).
    "AP2_WEBHOOK_URL",
    # Deployment-environment path (sandbox-specific channel-file
    # destination, TB-312).
    "AP2_CHANNEL_FILE_PATH",
    # Forward-compatibility placeholders per goal.md L358 (sandbox user
    # identity + SDK-mode escape hatch). Not currently read in source;
    # listed here so a future addition stays env-only by default.
    "AP2_DIR",
    "AP2_REAL_SDK",
})


# ---------------------------------------------------------------------------
# One-shot env_deprecated emission bookkeeping.
#
# Mirrors the `_emitted_attention_keys` accounting in `ap2/watchdog.py` —
# a module-level set guarded by a lock, populated on first emit, never
# trimmed during the process. One `env_deprecated` event per (flat-knob,
# process) pair so the operator's audit trail in `events.jsonl` carries a
# single discoverable signal per process per migrated knob, not a per-tick
# repeat.
# ---------------------------------------------------------------------------
_EMITTED_LOCK = threading.Lock()
_EMITTED_ONCE: set[str] = set()


def reset_env_deprecated_emit_for_tests() -> None:
    """Reset the module-level one-shot emission set. Tests call this in
    setup so each test starts from a known-empty state — production never
    calls this (the set is reset only on process restart by design).
    """
    with _EMITTED_LOCK:
        _EMITTED_ONCE.clear()


def _emit_env_deprecated(
    events_file: "Path | None",
    flat: str,
    sectioned: str,
) -> bool:
    """Emit a one-shot `env_deprecated` event for `flat → sectioned`.

    Returns True if the event was actually emitted (first read of `flat`
    this process), False if it was suppressed (already emitted).

    Payload (per briefing): `flat` (the deprecated knob name), `sectioned`
    (its replacement path), `process_pid` (so a multi-daemon operator
    setup can tell which daemon process emitted the event). `ts` is
    added by `events.append`.

    `events_file=None` is a defensive no-op — the caller (Config build
    path) may not have an events surface wired up yet during unit-test
    construction, and the deprecation event is best-effort audit
    signal, not a hard contract.
    """
    if events_file is None:
        return False
    with _EMITTED_LOCK:
        if flat in _EMITTED_ONCE:
            return False
        _EMITTED_ONCE.add(flat)
    # Lazy import keeps the config_compat.py ↔ events.py cycle broken at
    # import time. `events` itself is tiny (no transitive Config import)
    # so this is a hot-path-safe pattern.
    from . import events
    events.append(
        events_file,
        "env_deprecated",
        flat=flat,
        sectioned=sectioned,
        process_pid=os.getpid(),
    )
    return True


# ---------------------------------------------------------------------------
# Value coercion: env vars are always strings; the loaded Config (and the
# parsed TOML) carry typed values. The coercion helper normalizes the env
# string into the expected type, preferring the existing value's type as the
# authoritative signal (a key already populated as `bool false` should
# coerce `"1"` to `True`, not int `1`).
# ---------------------------------------------------------------------------
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSY: frozenset[str] = frozenset({"0", "false", "no", "off"})


def _coerce(env_val: str, *, existing: Any = None) -> Any:
    """Coerce an env-var string into the type of `existing` when possible.

    Behavior matrix:
      - existing is bool → parse `1` / `true` / `yes` / `on` (truthy,
        case-insensitive) → True; everything else → False. Mirrors the
        sibling `AP2_FOCUS_AUTO_ADVANCE_DISABLED` bool-parse shape across
        `ap2/components/*` so an operator who learned the truthy
        vocabulary from one knob carries it to all.
      - existing is int (NOT bool — Python's bool subclasses int, so the
        bool branch comes first) → `int(env_val)`; on ValueError, return
        the existing value unchanged (don't corrupt typed state with a
        garbage env value).
      - existing is float → `float(env_val)`; on ValueError, return
        existing unchanged.
      - existing is str → strip whitespace from `env_val`. If the
        stripped value is empty, return `existing` (a whitespace-only
        env override is treated as "unset" — matches the
        `_load_env_path` strip-and-fallback contract for
        `AP2_PROJECT_NAME` / `AP2_VERIFY_CMD`, so an accidental
        whitespace-only env value doesn't override a deliberate
        baseline with a leading-space string). Tests around the
        project-name fallback path (TB-280) pin this contract.
      - existing is None → heuristic cascade: truthy/falsy bool literals
        first, then int, then float, then raw string. The no-existing
        case fires on the flat back-compat path when the TOML omits the
        key; the heuristic mirrors how an operator typing `1` or `true`
        expects the daemon to interpret their intent.
      - existing is any other type (e.g. Path, list, dict) → return
        `env_val` verbatim; we don't try to coerce into structural types
        from a single string.
    """
    if isinstance(existing, bool):
        return env_val.strip().lower() in _TRUTHY
    if isinstance(existing, int):
        try:
            return int(env_val)
        except ValueError:
            return existing
    if isinstance(existing, float):
        try:
            return float(env_val)
        except ValueError:
            return existing
    if isinstance(existing, str):
        # Strip-and-fallback semantics matching `_load_env_path` for
        # `AP2_PROJECT_NAME` (TB-280) and `AP2_VERIFY_CMD`: a
        # whitespace-only env value is "unset", not an override that
        # paints a leading-space string into a deliberate baseline. A
        # non-empty stripped value is applied stripped — the
        # `_load_env_path` body for project_name calls out the strip
        # explicitly (a leading-space env value shouldn't render a
        # leading space in the bracketed status headline); the same
        # intent applies uniformly here.
        stripped = env_val.strip()
        if not stripped:
            return existing
        return stripped
    if existing is not None:
        # Unknown / structural existing type — don't try to coerce.
        return env_val
    # No existing — heuristic cascade.
    low = env_val.strip().lower()
    if low in _TRUTHY:
        return True
    if low in _FALSY:
        return False
    try:
        return int(env_val)
    except ValueError:
        pass
    try:
        return float(env_val)
    except ValueError:
        pass
    return env_val


# ---------------------------------------------------------------------------
# Path-addressed get/set on the loaded Config instance.
#
# `core.<field>` → `cfg.<field>` (dataclass attribute).
# `components.<name>.<key>` → `cfg.components_config[<name>][<key>]`.
# ---------------------------------------------------------------------------


def _get_path(cfg: "Config", path: list[str]) -> Any:
    """Read the value at `path` on `cfg`. Returns None when the path
    doesn't resolve (no such field / no such component entry / no such
    key). The caller treats None as "no existing value to type-match
    against" — coercion falls back to the heuristic branch.
    """
    if len(path) == 2 and path[0] == "core":
        # TB-334: non-dataclass core knobs (`agent_model`,
        # `task_max_turns`, &c.) live in `cfg.core_config` rather
        # than as named dataclass attributes. Probe both — dataclass
        # attr first (existing tunables that ARE Config fields), then
        # `core_config` dict (the axis-5 core-cluster overlay).
        if hasattr(cfg, path[1]):
            return getattr(cfg, path[1], None)
        core_cfg = getattr(cfg, "core_config", None)
        if isinstance(core_cfg, dict):
            return core_cfg.get(path[1])
        return None
    if len(path) == 3 and path[0] == "components":
        comp = (cfg.components_config or {}).get(path[1])
        if isinstance(comp, dict):
            return comp.get(path[2])
    return None


def _set_path(cfg: "Config", path: list[str], value: Any) -> bool:
    """Write `value` at `path` on `cfg`. Returns True if the write
    landed, False if the path didn't resolve to a writable surface
    (unknown core field — the dataclass has no slot for it, so we skip
    rather than silently grow the instance dict).

    Component writes ALWAYS land — a `components.<new>.<key>` write
    creates the per-component dict on the fly (mirroring `dict.setdefault`).
    This matches the "back-compat shim populates the structured config
    even when the TOML omits the section" contract.
    """
    if len(path) == 2 and path[0] == "core":
        if hasattr(cfg, path[1]):
            setattr(cfg, path[1], value)
            return True
        # TB-334: non-dataclass core knobs (the axis-5 agent-runtime
        # cluster: `agent_model`, `task_max_turns`, &c.) land in
        # `cfg.core_config` so flat-env / sectioned-env overrides
        # still apply for keys the dataclass doesn't carry as named
        # attributes. Mirrors the `components.<name>.<key>` write path
        # below — the back-compat shim populates the structured
        # config even when the dataclass has no slot for the key.
        if getattr(cfg, "core_config", None) is None:
            cfg.core_config = {}
        cfg.core_config[path[1]] = value
        return True
    if len(path) == 3 and path[0] == "components":
        if cfg.components_config is None:
            cfg.components_config = {}
        comp = cfg.components_config.setdefault(path[1], {})
        if not isinstance(comp, dict):
            return False
        comp[path[2]] = value
        return True
    return False


# ---------------------------------------------------------------------------
# Sectioned-env override layer.
# ---------------------------------------------------------------------------


def _apply_sectioned_env_overrides(cfg: "Config") -> dict[str, Any]:
    """For each `AP2_<SECTION>_<KEY>` env name matching a path already
    populated on `cfg`, override the value.

    Two cleavages walked here:

      - `components.<name>.<key>` paths: enumerate
        `cfg.components_config` (dict-of-dicts) and probe
        `AP2_COMPONENTS_<NAME>_<KEY>` (upper-cased) in `os.environ`.
        Override on hit.
      - `core.<field>` paths: enumerate `Config`'s dataclass fields and
        probe `AP2_CORE_<FIELD>` in `os.environ`. Override on hit.

    Sectioned env overrides do NOT emit `env_deprecated` events — they
    use the new canonical naming and are forward-compatible by design.
    Returns the dict of overrides applied (path → coerced value) for
    callers that want to log / audit; the primary effect is the
    in-place mutation.

    No regex match on `os.environ` keys here — we walk the structured
    config and probe specific names. That avoids the
    `AP2_<flat>` false-positive shape (`AP2_AUTO_APPROVE` matching the
    `^AP2_[A-Z][A-Z0-9_]+$` regex but NOT being a sectioned override).
    The regex-anchored detection the briefing names is satisfied by
    the structured probe — every probed name conforms to the anchor.
    """
    overrides: dict[str, Any] = {}
    # Components sub-tables.
    components = cfg.components_config or {}
    for comp_name, knobs in list(components.items()):
        if not isinstance(knobs, dict):
            continue
        comp_upper = comp_name.upper()
        for key in list(knobs.keys()):
            env_name = f"AP2_COMPONENTS_{comp_upper}_{key.upper()}"
            if env_name in os.environ:
                existing = knobs[key]
                coerced = _coerce(os.environ[env_name], existing=existing)
                knobs[key] = coerced
                overrides[f"components.{comp_name}.{key}"] = coerced
    # Core dataclass fields.
    for f in _dataclass_fields(cfg):
        env_name = f"AP2_CORE_{f.name.upper()}"
        if env_name in os.environ:
            existing = getattr(cfg, f.name, None)
            coerced = _coerce(os.environ[env_name], existing=existing)
            if _set_path(cfg, ["core", f.name], coerced):
                overrides[f"core.{f.name}"] = coerced
    return overrides


# ---------------------------------------------------------------------------
# Flat-name back-compat shim.
# ---------------------------------------------------------------------------


def _apply_flat_back_compat(cfg: "Config") -> dict[str, Any]:
    """For each flat `AP2_*` knob in `FLAT_TO_SECTIONED` present in
    `os.environ`, apply the override to the matching sectioned path AND
    emit a one-shot `env_deprecated` event per process per knob.

    Returns the dict of overrides applied (flat-name → coerced value).
    Skips entries in `_KNOBS_STAYING_ENV_ONLY` — the partition is
    enforced at construction time (the partition-coverage test pins
    no overlap), but the runtime double-check makes the contract
    explicit at the call site.
    """
    overrides: dict[str, Any] = {}
    events_file = getattr(cfg, "events_file", None)
    for flat, sectioned in FLAT_TO_SECTIONED.items():
        if flat in _KNOBS_STAYING_ENV_ONLY:
            # Belt-and-suspenders: a future edit that listed a knob in
            # BOTH sets would silently fire deprecation events for an
            # explicitly-env-only knob. Skip here to make the
            # _KNOBS_STAYING_ENV_ONLY contract authoritative.
            continue
        if flat not in os.environ:
            continue
        path = sectioned.split(".")
        existing = _get_path(cfg, path)
        coerced = _coerce(os.environ[flat], existing=existing)
        if _set_path(cfg, path, coerced):
            overrides[flat] = coerced
        # Emit the deprecation event regardless of whether _set_path
        # landed — even an unknown-path write (refactor mid-flight, a
        # knob whose sectioned target the dataclass hasn't grown yet)
        # is still a flat-knob hit worth surfacing for the operator.
        _emit_env_deprecated(events_file, flat, sectioned)
    return overrides


# ---------------------------------------------------------------------------
# Public entry point — called from `config_loader.from_toml`.
# ---------------------------------------------------------------------------


def apply_env_overrides(cfg: "Config") -> dict[str, Any]:
    """Apply the env-override layer + back-compat shim to a loaded Config.

    Called from `ap2.config_loader.from_toml` after the TOML overlay
    but before the Config is returned. Applies overrides in
    precedence-defined order (high → low):

      1. Sectioned env (`AP2_<SECTION>_<KEY>`) → wins over TOML.
      2. Flat env (`AP2_<FLAT>`, back-compat) → wins over TOML but loses
         to a same-target sectioned-env override (applied first; flat-
         path overrides on the same sectioned target then overwrite,
         making flat last-wins by iteration order).

    Returns a dict mapping every applied-override path to the coerced
    value (sectioned keys: `"components.x.y"`; flat-name keys preserve
    the flat name for forensic traceability). Callers can use the
    return value for logging / metrics; the primary effect is the
    in-place mutation of `cfg`.

    The flat shim's `env_deprecated` emission is one-shot per (knob,
    process) — a daemon-restart resets the accounting, mirroring the
    `note_initial_applied` startup pass in `env_reload`. Tests that
    want clean state call `reset_env_deprecated_emit_for_tests()` in
    setup.
    """
    sectioned_applied = _apply_sectioned_env_overrides(cfg)
    flat_applied = _apply_flat_back_compat(cfg)
    # Re-apply sectioned overrides AFTER flat so the precedence
    # contract holds: a same-target sectioned-env hit wins over a
    # flat-env hit even when the flat one was iterated last.
    if sectioned_applied:
        _apply_sectioned_env_overrides(cfg)
    return {**flat_applied, **sectioned_applied}
