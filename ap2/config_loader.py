"""TB-321: TOML config schema + parser + daemon-start validator (axis 1).

Axis-(1) of the **structured config (env → TOML)** focus (goal.md
L266-403). This module ships the prerequisite slice every subsequent
axis builds on:

  - `ConfigKey` — typed schema declaration a component carries on its
    `Manifest.config_schema` for each tunable knob the component owns.
    Fields: name, type, default, description, hot_reloadable.
  - `parse_toml(path)` — thin `tomllib`-based reader (Python 3.11
    stdlib; no new dependency); returns the raw dict.
  - `aggregate_schemas(registry)` — walks `default_registry().components`,
    aggregates each manifest's `config_schema` into one
    `{component_name: {key_name: ConfigKey}}` union.
  - `validate_config(loaded_toml, registry)` — daemon-start validator.
    Walks `[components.<name>]` sub-tables in the loaded TOML and
    asserts every key is declared in some manifest's `config_schema`
    with a matching type. Raises `ConfigSchemaError` on the first
    mismatch — message names the bad key path (e.g.
    `[components.janitor] disabled = "yes": expected bool, got str`)
    so the operator can grep their config file directly. Fail-fast
    shape; the daemon does NOT auto-correct typos (goal.md L312-313).
  - `from_toml(toml_path)` — `Config.from_toml(path)` constructor.
    Returns a `Config` shape-compatible with `Config.load()`: walks
    the env-path baseline first (so any non-TOML knob keeps today's
    env-resolution behavior), then overlays `[core.<key>]` onto
    matching `Config` dataclass fields by name, and stashes
    `[components.<name>]` sub-tables on `Config.components_config`
    for axis-(5) per-component read-path migrations to consume.

Per-component reads from `cfg.<path>.<key>` are explicitly axis-(5)
follow-ups — this TB only lays the read paths (the dict shape on
Config). Migrating any actual `os.environ.get("AP2_*")` call to
read from `Config.components_config` is the per-knob long tail
(see goal.md L353-364).

Import-direction (TB-311 parity): this module must NOT statically
import from `ap2.components`. Schema declarations live on component
manifests; the registry walk is the cross-reference path. `Config`
is imported lazily inside `from_toml` to avoid a `config.py`↔
`config_loader.py` cycle (config.py needs to call from_toml from
inside `Config.load`; this module needs `Config` to build the
return value).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config
    from .registry import Registry


class ConfigSchemaError(ValueError):
    """Raised by `validate_config` on TOML schema mismatch.

    Subclass of ValueError so a caller that wants to catch only schema
    issues can do `except ConfigSchemaError`; a caller that only cares
    about "anything wrong with config" can do `except ValueError`.
    The daemon-start hook prints the message and exits — operator-fix
    shape, no auto-correction.
    """


@dataclass(frozen=True)
class ConfigKey:
    """One config key's typed schema declaration.

    Lives on `Manifest.config_schema` keyed by the bare key name
    (e.g. `"disabled"` for `[components.janitor] disabled = true`).
    The component owns the truth for its own knobs; the registry
    aggregates the union and the validator walks it.

    Fields:
      name           — the bare key name (`"disabled"`), redundant
                       with the dict key on the manifest but pinned
                       here so a future flat-list rendering surface
                       (axis-4 `ap2 config list`) carries it
                       without rebuilding from the surrounding dict.
      type           — the Python type the value must be an instance
                       of (`bool`, `int`, `str`, `float`). Bool/int
                       are intentionally distinguished — Python's
                       `bool` is a subclass of `int`, so a naive
                       `isinstance(v, int)` admits booleans; the
                       validator's `_type_matches` excludes that
                       conflation.
      default        — the value used when the operator omits the
                       key from `config.toml`. Mirrored from the
                       in-source default (e.g. `False` for
                       `AP2_JANITOR_DISABLED` unset → janitor on).
      description    — human-readable one-line explanation, surfaced
                       by axis-4's `ap2 config list` as the
                       per-key annotation.
      hot_reloadable — bool; True when the daemon picks up changes
                       to this key without a restart (axis-2's
                       env_reload mtime trick extended to TOML).
                       Defaults to False (conservative — assume a
                       restart is needed unless explicitly opted in).
    """

    name: str
    type: type
    default: Any
    description: str
    hot_reloadable: bool = False


def parse_toml(path: Path) -> dict[str, Any]:
    """Read `path` and return the raw parsed TOML dict.

    Wraps `tomllib.loads` so a malformed file surfaces a
    `tomllib.TOMLDecodeError` with the path in the call stack. The
    daemon-start hook catches the decode error and prints the path +
    line for the operator. Path can be any TOML file; the conventional
    location is `<project_root>/.cc-autopilot/config.toml`.
    """
    text = path.read_text(encoding="utf-8")
    return tomllib.loads(text)


def aggregate_schemas(registry: "Registry") -> dict[str, dict[str, ConfigKey]]:
    """Walk `registry.components` and return the union of per-component schemas.

    Shape: `{component_name: {key_name: ConfigKey}}`. Components
    whose manifest carries an empty `config_schema` (or no schema
    attribute at all — defensive against a future manifest written
    against a pre-TB-321 codebase) contribute nothing — the empty
    dict is the safe default so the six non-canary manifests
    continue to load until TB-322 fills them in.

    Used by `validate_config` (this module) and by axis-4's
    `ap2 config list` (a future TB). The function is deliberately
    pure — no env reads, no side effects — so it can be exercised
    from a unit test with a synthetic registry.
    """
    out: dict[str, dict[str, ConfigKey]] = {}
    for manifest in registry.components:
        schema = getattr(manifest, "config_schema", None) or {}
        if schema:
            out[manifest.name] = dict(schema)
    return out


def _type_matches(value: Any, expected: type) -> bool:
    """Type-check a TOML value against a declared `ConfigKey.type`.

    Special-cases bool vs int — Python's `bool` is a subclass of
    `int`, so a naive `isinstance(value, int)` returns True for a
    bool value too. The validator's contract is "the type the
    operator declared" — a key declared `int` should reject a bool
    value (and vice versa). Other types delegate to plain
    `isinstance`.
    """
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def validate_config(loaded_toml: dict[str, Any], registry: "Registry") -> None:
    """Validate a parsed TOML config against the registry's aggregated schemas.

    Walks `[components.<name>]` sub-tables in `loaded_toml` and
    asserts every key is declared in some manifest's `config_schema`
    with a matching type. Raises `ConfigSchemaError` on the first
    mismatch; the message names the bad key path so the operator can
    grep their config file directly:

        [components.janitor] disabled = 'yes': expected bool, got str

    Fail-fast shape; the daemon does NOT auto-correct typos
    (goal.md L312-313 — operator-fix-first). The daemon-start hook
    in `daemon.main_loop` catches the raise, prints the message, and
    refuses to start.

    `[core.*]` keys are NOT validated here — TB-321 ships per-
    component schema declarations on manifests; core knobs (verifier,
    ideation, cron, etc.) move to a dedicated `[core.*]` schema in a
    later axis. Today they round-trip through `from_toml` as
    free-form keys mapped to existing `Config` dataclass fields by
    name — a typo silently keeps the env-path default.

    Reject-unknown-component path: if `[components.foo]` appears in
    the loaded TOML but no manifest declares a `config_schema` for
    `foo`, that's a typo or a stale config — fail with a clear
    "unknown component" message. The operator either spelled the
    name wrong or is on an older daemon that doesn't know the new
    component yet.
    """
    if not isinstance(loaded_toml, dict):
        raise ConfigSchemaError(
            f"Loaded TOML must be a dict (table); got "
            f"{type(loaded_toml).__name__}"
        )
    components_section = loaded_toml.get("components") or {}
    if not isinstance(components_section, dict):
        raise ConfigSchemaError(
            f"[components] must be a TOML table; got "
            f"{type(components_section).__name__}"
        )
    schemas = aggregate_schemas(registry)
    for component_name, knobs in components_section.items():
        if not isinstance(knobs, dict):
            raise ConfigSchemaError(
                f"[components.{component_name}] must be a TOML table; got "
                f"{type(knobs).__name__}"
            )
        schema = schemas.get(component_name)
        if schema is None:
            known = sorted(schemas) or ["(no components declare a config_schema yet)"]
            raise ConfigSchemaError(
                f"[components.{component_name}]: unknown component (no "
                f"manifest declares a `config_schema` for "
                f"{component_name!r}). Known: {known}"
            )
        for key_name, value in knobs.items():
            spec = schema.get(key_name)
            if spec is None:
                raise ConfigSchemaError(
                    f"[components.{component_name}] {key_name} = {value!r}: "
                    f"unknown key (known: {sorted(schema)})"
                )
            if not _type_matches(value, spec.type):
                raise ConfigSchemaError(
                    f"[components.{component_name}] {key_name} = {value!r}: "
                    f"expected {spec.type.__name__}, got "
                    f"{type(value).__name__}"
                )


def from_toml(toml_path: Path) -> "Config":
    """Build a `Config` from `.cc-autopilot/config.toml` at `toml_path`.

    The returned `Config` is shape-compatible with `Config.load()`:
    same dataclass, same field names, same in-process consumers
    work unchanged. The only structural addition is the
    `components_config` attribute — a `{component_name: {key: value}}`
    dict holding every `[components.<name>]` sub-table verbatim,
    ready for axis-(5) per-component read paths to consume.

    Layering: the env-path `Config.load()` runs first as the baseline
    (so any knob NOT migrated to TOML keeps today's env-resolution
    behavior bit-for-bit). Then `[core.<key>]` entries overlay onto
    matching `Config` dataclass fields by name — a TOML value for
    `tick_interval_s` becomes `cfg.tick_interval_s`. Fields that
    don't exist on `Config` are silently ignored at this layer (the
    validator already rejected unknown component keys; core-schema
    validation is a future-axis concern). `[components.*]` sub-
    tables are stashed wholesale on `cfg.components_config`.

    The opt-in branch in `Config.load()` (`if config.toml exists,
    prefer it`) calls THIS function. Existing installs with no
    config.toml see zero behavior change — `Config.load()` returns
    the env-path baseline directly.
    """
    # Lazy import to avoid the config.py ↔ config_loader.py cycle —
    # config.py needs to call into us from inside Config.load, and
    # we need Config to build the return value.
    from .config import Config

    raw = parse_toml(toml_path)
    # Convention: config.toml lives at <project_root>/.cc-autopilot/config.toml,
    # so project_root is the parent of the file's parent.
    project_root = toml_path.parent.parent
    # Env-path baseline. Passing `_skip_toml=True` short-circuits the
    # TOML branch on `Config.load` so we don't recurse — see config.py.
    cfg = Config._load_env_path(project_root)
    core_section = raw.get("core") or {}
    if not isinstance(core_section, dict):
        raise ConfigSchemaError(
            f"[core] must be a TOML table; got {type(core_section).__name__}"
        )
    for key, value in core_section.items():
        # Only overlay keys that name an existing Config field; a typo
        # in a [core.*] key silently keeps the env-path default. Core-
        # schema validation is a future-axis concern (per the module
        # docstring's note on [core.*]).
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    # TB-334 (axis 5 core cluster): also stash every [core.<key>] entry
    # verbatim on `cfg.core_config` so `Config.get_core_value` can read
    # the TOML snapshot for non-dataclass core knobs (`agent_model`,
    # `agent_effort`, `task_max_turns`, &c. — the agent-runtime
    # tunables whose pre-migration call sites read `os.environ.get`).
    # The setattr overlay above remains the back-compat path for
    # readers that access `cfg.<field>` directly (e.g.
    # `cfg.tick_interval_s`); the dict snapshot here is the helper's
    # input. Deep-copy not needed — values are scalars (int/bool/str)
    # not nested tables.
    cfg.core_config = dict(core_section)
    components_section = raw.get("components") or {}
    if not isinstance(components_section, dict):
        raise ConfigSchemaError(
            f"[components] must be a TOML table; got "
            f"{type(components_section).__name__}"
        )
    # Deep-copy the sub-tables so per-component dicts are mutable and
    # the env-override layer can write into them without aliasing the
    # parsed TOML structure. `dict(components_section)` alone would
    # reuse the inner dicts.
    cfg.components_config = {
        name: dict(knobs) if isinstance(knobs, dict) else knobs
        for name, knobs in components_section.items()
    }
    # TB-323 (axis 2): apply the env-override layer + flat-name back-
    # compat shim. Sectioned envs (`AP2_<SECTION>_<KEY>`) win over the
    # TOML value; flat envs (`AP2_<FLAT>` listed in
    # `config_compat.FLAT_TO_SECTIONED`) also override + emit a
    # one-shot `env_deprecated` event per process per knob. Lazy
    # import keeps the config_loader.py ↔ config_compat.py boundary
    # tidy and avoids any import-time cycle through the events module.
    from .config_compat import apply_env_overrides
    apply_env_overrides(cfg)
    return cfg
