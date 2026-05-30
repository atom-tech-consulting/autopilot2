"""Source-attribution helper for `ap2 config list / get` (TB-324, axis 4).

Walks the registry's aggregated component schemas + the FLAT_TO_SECTIONED
contract surface for `core.*` keys, looks up each key's resolved value
on the loaded `Config`, and tags each row with where the value came
from (`file` / `env-override` / `default`).

Layered against the same precedence pipeline `config_loader.from_toml`
runs at daemon-start:

    sectioned env > flat env (back-compat) > TOML file > in-source default

Source detection re-reads `os.environ` and the raw TOML file (when
present) rather than mutating the loaded Config — the introspection
walk must be a pure read so an operator running `ap2 config list`
during an in-flight daemon doesn't perturb live state.

Design notes:

- Component keys come from `aggregate_schemas(registry)` — those are
  the typed schema declarations TB-321/322 land. Each row carries the
  ConfigKey's `description` + `type` for the rendering layer.
- Core keys come from the unique `core.<field>` paths in
  `FLAT_TO_SECTIONED` — that's the documented operator-facing contract
  surface for non-component tunables (until axis-(5) per-cluster
  migrations promote each to a typed core schema, this is the
  best-available enumeration).
- A flat env hit (`AP2_<flat>`) AND a sectioned env hit
  (`AP2_<SECTION>_<KEY>`) both count as `env-override`. The
  `apply_env_overrides` precedence already resolves which one wins —
  we just report "env beat the file" without re-deriving precedence
  inside the introspect layer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG_TOML_FILE, Config
from .config_compat import FLAT_TO_SECTIONED
from .config_loader import ConfigKey, aggregate_schemas, parse_toml
from .registry import Registry


@dataclass(frozen=True)
class ConfigRow:
    """One row in the `ap2 config list` enumeration.

    Fields:
      path           — full dotted path (e.g. `components.janitor.disabled`
                       or `core.tick_interval_s`).
      value          — current resolved value on the loaded `Config`.
      source         — one of `file` / `env-override` / `default`.
      description    — human-readable description (from the ConfigKey
                       when available; empty string for core keys
                       without a typed schema declaration).
      type_name      — declared type name (`bool`, `int`, `str`, etc.);
                       empty string for core keys without a schema.
      hot_reloadable — whether the daemon picks up changes without a
                       restart (declared on the ConfigKey).
    """

    path: str
    value: Any
    source: str
    description: str
    type_name: str
    hot_reloadable: bool


def _build_core_paths_from_flat_map() -> dict[str, str]:
    """Return `{core_field_name: flat_env_name}` for every `core.<field>`
    entry in `FLAT_TO_SECTIONED` — the back-compat map is the canonical
    enumeration of core-side tunable knobs.

    Note: multiple flat names CAN map to the same sectioned target in
    principle (none today, but a future migration window might), so the
    last-write-wins semantics here are a defensive simplification —
    the introspect layer's source attribution doesn't need a complete
    flat-name list, just one to probe.
    """
    out: dict[str, str] = {}
    for flat, sectioned in FLAT_TO_SECTIONED.items():
        parts = sectioned.split(".")
        if len(parts) == 2 and parts[0] == "core":
            out[parts[1]] = flat
    return out


def _toml_has_core_key(raw_toml: dict, field_name: str) -> bool:
    core = raw_toml.get("core")
    return isinstance(core, dict) and field_name in core


def _toml_has_component_key(
    raw_toml: dict, comp_name: str, key_name: str,
) -> bool:
    components = raw_toml.get("components")
    if not isinstance(components, dict):
        return False
    section = components.get(comp_name)
    return isinstance(section, dict) and key_name in section


def _attribute_source(
    *,
    has_toml: bool,
    sectioned_env: str,
    flat_env: str | None,
) -> str:
    """Pick the source label given the three input signals.

    Precedence mirrors `apply_env_overrides`: env (either kind) wins
    over the TOML file, file wins over default. The display label
    flattens both env paths to `env-override` — the operator can grep
    `os.environ` directly to see which knob fired.
    """
    if sectioned_env in os.environ:
        return "env-override"
    if flat_env and flat_env in os.environ:
        return "env-override"
    if has_toml:
        return "file"
    return "default"


def collect_rows(cfg: Config, registry: Registry) -> list[ConfigRow]:
    """Return one `ConfigRow` per known config key on the loaded `cfg`.

    Walked surfaces:
      - `aggregate_schemas(registry)` — typed component schemas
        (TB-321/322).
      - `core.<field>` paths derived from `FLAT_TO_SECTIONED` — the
        contract surface for core-side tunables.

    Rows are sorted by path so the rendered table / JSON output is
    stable across runs (no surprise diff in operator-script consumers).
    """
    # Read the raw TOML file once for source attribution. Missing file
    # is fine — every key falls through to `default` or `env-override`.
    toml_path = cfg.project_root / CONFIG_TOML_FILE
    if toml_path.exists():
        try:
            raw_toml = parse_toml(toml_path)
        except Exception:  # noqa: BLE001
            # A corrupted TOML can't be a source of truth — treat every
            # key as if the file were absent. `ap2 config validate`
            # surfaces the decode error separately.
            raw_toml = {}
    else:
        raw_toml = {}

    rows: list[ConfigRow] = []

    # Component schemas — each entry carries a ConfigKey with description.
    component_schemas = aggregate_schemas(registry)
    for comp_name in sorted(component_schemas):
        for key_name in sorted(component_schemas[comp_name]):
            spec = component_schemas[comp_name][key_name]
            sectioned_env = f"AP2_COMPONENTS_{comp_name.upper()}_{key_name.upper()}"
            flat_env = _flat_for_sectioned(
                f"components.{comp_name}.{key_name}"
            )
            source = _attribute_source(
                has_toml=_toml_has_component_key(raw_toml, comp_name, key_name),
                sectioned_env=sectioned_env,
                flat_env=flat_env,
            )
            value = _resolve_component_value(cfg, comp_name, key_name, spec)
            rows.append(
                ConfigRow(
                    path=f"components.{comp_name}.{key_name}",
                    value=value,
                    source=source,
                    description=spec.description,
                    type_name=getattr(spec.type, "__name__", str(spec.type)),
                    hot_reloadable=spec.hot_reloadable,
                )
            )

    # Core fields — enumerated from FLAT_TO_SECTIONED's core entries.
    core_fields = _build_core_paths_from_flat_map()
    for field_name in sorted(core_fields):
        flat_env = core_fields[field_name]
        sectioned_env = f"AP2_CORE_{field_name.upper()}"
        source = _attribute_source(
            has_toml=_toml_has_core_key(raw_toml, field_name),
            sectioned_env=sectioned_env,
            flat_env=flat_env,
        )
        # TB-344: resolve through the SAME runtime path the daemon uses
        # (`cfg.get_core_value`: env → TOML → schema default) rather than
        # a bare `getattr` on the loaded Config. The dataclass-attribute
        # read only worked for the ~11 keys overlaid onto `Config`
        # (`control_timeout_s`, `verify_cmd`, …); lazily-resolved keys
        # (`agent_model`, `agent_effort`) have no attribute and showed
        # `(unset)` even when env / schema default supplied a value. Now
        # the displayed value matches what a dispatch site receives.
        # A genuinely-unset key (no env, no TOML, None/absent schema
        # default) still resolves to `None` → `(unset)` in the renderer.
        value = cfg.get_core_value(field_name)
        rows.append(
            ConfigRow(
                path=f"core.{field_name}",
                value=value,
                source=source,
                description="",
                type_name=type(value).__name__ if value is not None else "",
                hot_reloadable=False,
            )
        )

    return rows


def _flat_for_sectioned(sectioned_path: str) -> str | None:
    """Inverse lookup of `FLAT_TO_SECTIONED`. Returns the first matching
    flat name (or None if no flat back-compat exists for the path).
    """
    for flat, target in FLAT_TO_SECTIONED.items():
        if target == sectioned_path:
            return flat
    return None


def _resolve_component_value(
    cfg: Config, comp_name: str, key_name: str, spec: ConfigKey,
) -> Any:
    """Look up the component key's value on the loaded `cfg`.

    Falls back to the schema's declared default when the component's
    sub-table doesn't carry the key (a fresh config.toml without that
    knob, no env overlay applied). The fallback preserves the
    declarative-default contract operators rely on for `ap2 config get`
    on a never-set knob.
    """
    comp = (cfg.components_config or {}).get(comp_name)
    if isinstance(comp, dict) and key_name in comp:
        return comp[key_name]
    return spec.default


# ---------------------------------------------------------------------------
# Schema lookup for the `set` verb. Used by the operator-queue drain to
# parse the operator's string-shaped value against the declared type.
# ---------------------------------------------------------------------------


def lookup_spec(path: str, registry: Registry) -> ConfigKey | None:
    """Return the `ConfigKey` declared for `path`, or None for paths the
    registry has no typed schema for (today: every `core.*` path).

    Used by the drain-side `do_config_set` handler to decide how to
    parse the operator's `value` string; core paths fall through to the
    Config dataclass field's existing type as the authoritative signal.
    """
    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "components":
        comp_name, key_name = parts[1], parts[2]
        component_schemas = aggregate_schemas(registry)
        return component_schemas.get(comp_name, {}).get(key_name)
    return None


def list_known_paths(cfg: Config, registry: Registry) -> list[str]:
    """Flat list of every known config path. Used by the `set` verb's
    did-you-mean suggestion."""
    return [r.path for r in collect_rows(cfg, registry)]
