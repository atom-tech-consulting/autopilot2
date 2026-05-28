"""TB-321: TOML config schema + parser + `Config.from_toml` + janitor canary.

Pins the axis-(1) prerequisite slice of the **structured config (env →
TOML)** focus (goal.md L266-403):

  1. `ap2.config_loader.parse_toml` round-trips a fixture TOML written
     to `tmp_path` — proves the `tomllib` wrapper is wired correctly
     and `[core.*]` / `[components.<name>]` sectioning survives parse.
  2. `ConfigKey` is a typed dataclass with the briefing-pinned shape
     (`name`, `type`, `default`, `description`, `hot_reloadable`).
  3. `aggregate_schemas(default_registry())` surfaces the janitor's
     canary entry (`{"disabled": ConfigKey(...)}`) so the validator's
     walk has something to validate against — proves the manifest →
     registry → aggregator chain works end-to-end.
  4. `validate_config` rejects a bad-type value with a `ConfigSchemaError`
     whose message names the bad key path (the briefing's pinned
     shape: `[components.janitor] disabled = ...: expected bool, got
     <actual>`).
  5. `validate_config` rejects unknown component names + unknown keys
     within a known component.
  6. `Config.from_toml(path)` returns a shape-compatible `Config`
     dataclass with `[core.<key>]` overlaid on matching fields and
     `[components.<name>]` stashed on `cfg.components_config`.
  7. `Config.load()` with no `config.toml` present takes the env-only
     fallback path (zero behavior change for existing installs).
  8. `Config.load()` with `config.toml` present delegates to
     `from_toml` (the opt-in branch).

Why these pin the axis-1 cleavage: every subsequent axis (TB-322
per-component schemas, TB-323 env-override layer, axis-4 CLI, axis-5
per-knob migrations, axis-6 docs gate) builds on top of this slice.
If a refactor weakens any of the eight contracts above, the
follow-up axes silently lose their substrate — pinning at the
prerequisite layer is the cheapest place to catch the drift.
"""
from __future__ import annotations

import os

import pytest

from ap2.config import CONFIG_TOML_FILE, Config
from ap2.config_loader import (
    ConfigKey,
    ConfigSchemaError,
    aggregate_schemas,
    from_toml,
    parse_toml,
    validate_config,
)
from ap2.registry import Manifest, Registry, default_registry


# ---------------------------------------------------------------------
# 1. parse_toml round-trip
# ---------------------------------------------------------------------


def test_parse_toml_round_trips_core_and_components_sections(tmp_path):
    """`parse_toml` reads a fixture file and returns the expected dict
    shape — the `tomllib` wrapper preserves `[core.*]` / `[components.<name>]`
    sectioning verbatim."""
    p = tmp_path / "config.toml"
    p.write_text(
        "[core]\n"
        "tick_interval_s = 60\n"
        "\n"
        "[components.janitor]\n"
        "disabled = true\n"
    )
    raw = parse_toml(p)
    assert raw == {
        "core": {"tick_interval_s": 60},
        "components": {"janitor": {"disabled": True}},
    }


# ---------------------------------------------------------------------
# 2. ConfigKey shape
# ---------------------------------------------------------------------


def test_config_key_carries_briefing_pinned_fields():
    """`ConfigKey` declares the five fields the briefing pins on its
    shape: name, type, default, description, hot_reloadable."""
    key = ConfigKey(
        name="disabled",
        type=bool,
        default=False,
        description="Kill switch.",
        hot_reloadable=True,
    )
    assert key.name == "disabled"
    assert key.type is bool
    assert key.default is False
    assert key.description == "Kill switch."
    assert key.hot_reloadable is True


def test_config_key_hot_reloadable_defaults_false():
    """Conservative default — assume a knob needs a daemon restart
    unless the component explicitly opts in."""
    key = ConfigKey(
        name="x", type=int, default=0, description="d",
    )
    assert key.hot_reloadable is False


# ---------------------------------------------------------------------
# 3. aggregate_schemas surfaces janitor canary
# ---------------------------------------------------------------------


def test_aggregate_schemas_surfaces_janitor_disabled_canary():
    """`aggregate_schemas(default_registry())` returns a union with
    janitor's `disabled` ConfigKey present — proves the manifest →
    registry → aggregator chain works end-to-end."""
    schemas = aggregate_schemas(default_registry())
    assert "janitor" in schemas, (
        f"TB-321: janitor canary manifest must declare a config_schema; "
        f"got components with schemas: {sorted(schemas)}"
    )
    janitor_keys = schemas["janitor"]
    assert "disabled" in janitor_keys, (
        f"TB-321: janitor's config_schema must declare the `disabled` "
        f"knob; got {sorted(janitor_keys)}"
    )
    spec = janitor_keys["disabled"]
    assert isinstance(spec, ConfigKey)
    assert spec.type is bool
    # Default matches in-source default (`AP2_JANITOR_DISABLED` unset →
    # janitor on → disabled=False).
    assert spec.default is False
    # Description is non-empty (briefing prose check parity).
    assert spec.description.strip(), (
        "TB-321: ConfigKey description must be non-empty for axis-4 "
        "`ap2 config list` to surface a meaningful per-key annotation."
    )


# ---------------------------------------------------------------------
# 4. validate_config rejects bad types with named-path error
# ---------------------------------------------------------------------


def test_validate_config_rejects_bad_type_with_named_path():
    """The briefing's pinned error-message shape:
    `[components.janitor] disabled = 'yes': expected bool, got str`.
    Naming the path lets the operator grep their config file
    directly — operator-fix-first shape (goal.md L312-313).
    """
    loaded = {"components": {"janitor": {"disabled": "yes"}}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    msg = str(exc_info.value)
    assert "[components.janitor]" in msg, msg
    assert "disabled" in msg, msg
    assert "expected bool" in msg, msg
    assert "got str" in msg, msg


def test_validate_config_rejects_int_for_bool_key():
    """Python's `bool` is a subclass of `int`; the validator must
    NOT accept an int where a bool key is declared (or vice versa).
    Pinned because a naive `isinstance(v, bool)` is fine but a
    naive `isinstance(v, int)` admits booleans."""
    loaded = {"components": {"janitor": {"disabled": 1}}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    assert "expected bool, got int" in str(exc_info.value)


def test_validate_config_accepts_well_typed_value():
    """A correctly-typed value passes silently — no return value,
    no exception. The validator's contract is "raise on mismatch";
    the success path returns None."""
    loaded = {"components": {"janitor": {"disabled": True}}}
    result = validate_config(loaded, default_registry())
    assert result is None


# ---------------------------------------------------------------------
# 5. validate_config rejects unknown components / keys
# ---------------------------------------------------------------------


def test_validate_config_rejects_unknown_component():
    """A `[components.foo]` table for a component no manifest declares
    a schema for fails with a clear "unknown component" message —
    typo / stale-config defense."""
    loaded = {"components": {"nonexistent_component": {"x": 1}}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    msg = str(exc_info.value)
    assert "nonexistent_component" in msg
    assert "unknown component" in msg


def test_validate_config_rejects_unknown_key_within_known_component():
    """A typo in a key name within a known component fails — the
    component's schema is the closed set of known keys."""
    loaded = {"components": {"janitor": {"disabld": True}}}  # typo
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    msg = str(exc_info.value)
    assert "[components.janitor]" in msg
    assert "disabld" in msg
    assert "unknown key" in msg


def test_validate_config_rejects_non_table_components_section():
    """`[components]` must be a TOML table — a scalar at that path
    is a structural mistake."""
    loaded = {"components": "not a table"}
    with pytest.raises(ConfigSchemaError):
        validate_config(loaded, default_registry())


def test_validate_config_empty_components_section_passes():
    """No `[components]` section (or an empty one) is the no-op path
    — fresh installs writing only `[core.*]` should validate."""
    validate_config({}, default_registry())
    validate_config({"components": {}}, default_registry())
    validate_config({"core": {"tick_interval_s": 30}}, default_registry())


# ---------------------------------------------------------------------
# 6. Config.from_toml builds a shape-compatible Config
# ---------------------------------------------------------------------


def _write_toml(tmp_path, body):
    """Write `body` to `<tmp_path>/.cc-autopilot/config.toml` and return
    the path. Also creates an empty TASKS.md + .cc-autopilot/ scaffold
    so `Config._load_env_path`'s subsequent reads succeed."""
    (tmp_path / ".cc-autopilot").mkdir(exist_ok=True)
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    p = tmp_path / CONFIG_TOML_FILE
    p.write_text(body)
    return p


def _strip_ap2_env(monkeypatch):
    """Strip every `AP2_*` env knob the harness/CI might have set so the
    TOML overlay's behavior is observable in isolation.

    TB-323 (axis 2) made the env-override layer authoritative — `AP2_*`
    values in `os.environ` win over the loaded TOML by design (the
    operator's shell-export back-compat path). The TB-321 tests pin
    the TOML overlay's behavior specifically, so they need the env
    surface cleaned to avoid the daemon/CI environment's pre-set
    `AP2_TICK_S` / `AP2_AUTO_APPROVE` / etc. silently winning over the
    test's fixture TOML."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)


def test_config_from_toml_returns_config_dataclass(tmp_path, monkeypatch):
    """`Config.from_toml` returns an instance of `Config` — shape-
    compatible with `Config.load()`, same dataclass, same field
    names."""
    _strip_ap2_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    toml_path = _write_toml(tmp_path, "[core]\ntick_interval_s = 45\n")
    cfg = Config.from_toml(toml_path)
    assert isinstance(cfg, Config)
    # [core.*] overlay reached the dataclass field.
    assert cfg.tick_interval_s == 45


def test_config_from_toml_stashes_components_section(tmp_path, monkeypatch):
    """`[components.<name>]` sub-tables land on `cfg.components_config`
    verbatim — the dict shape axis-(5) per-component reads consume."""
    _strip_ap2_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    toml_path = _write_toml(
        tmp_path,
        "[components.janitor]\ndisabled = true\n",
    )
    cfg = Config.from_toml(toml_path)
    assert cfg.components_config == {"janitor": {"disabled": True}}


def test_config_from_toml_core_overlay_ignores_unknown_keys(tmp_path, monkeypatch):
    """A `[core.<key>]` for a name that is NOT a `Config` field is
    silently ignored (per the module docstring — core-schema
    validation is a future-axis concern; today's overlay only
    populates known fields)."""
    _strip_ap2_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    toml_path = _write_toml(
        tmp_path,
        "[core]\ntick_interval_s = 45\nbogus_unknown_key = 'whatever'\n",
    )
    cfg = Config.from_toml(toml_path)
    assert cfg.tick_interval_s == 45
    assert not hasattr(cfg, "bogus_unknown_key")


# ---------------------------------------------------------------------
# 7. Config.load() with no config.toml takes the env-only path
# ---------------------------------------------------------------------


def test_config_load_without_toml_takes_env_path(tmp_path, monkeypatch):
    """No `config.toml` present → `Config.load()` returns the env-path
    baseline directly (zero behavior change for existing installs).
    `components_config` is empty in the env-path branch — the field
    is always safe to read even when the operator hasn't opted in."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    assert isinstance(cfg, Config)
    assert cfg.components_config == {}


# ---------------------------------------------------------------------
# 8. Config.load() with config.toml delegates to from_toml
# ---------------------------------------------------------------------


def test_config_load_with_toml_prefers_toml_path(tmp_path, monkeypatch):
    """`config.toml` present → `Config.load()` delegates to
    `from_toml` — the opt-in branch in scope item (4)."""
    _strip_ap2_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    _write_toml(
        tmp_path,
        "[core]\ntick_interval_s = 77\n[components.janitor]\ndisabled = true\n",
    )
    cfg = Config.load(tmp_path)
    assert cfg.tick_interval_s == 77
    assert cfg.components_config == {"janitor": {"disabled": True}}


# ---------------------------------------------------------------------
# Synthetic-registry sanity: validator works against a hand-built registry
# ---------------------------------------------------------------------


def test_validate_config_works_against_synthetic_registry():
    """`validate_config` operates on any `Registry` — not just
    `default_registry()`. This is the test-isolation seam: a future
    test can hand-build a registry with synthetic schemas and
    exercise the validator without touching the live components
    package."""
    synthetic = Registry(
        [
            Manifest(
                name="synth",
                env_flag=None,
                default_enabled=True,
                hook_points={},
                config_schema={
                    "verbose": ConfigKey(
                        name="verbose",
                        type=bool,
                        default=False,
                        description="Synthetic verbose flag.",
                    ),
                    "count": ConfigKey(
                        name="count",
                        type=int,
                        default=0,
                        description="Synthetic count.",
                    ),
                },
            ),
        ]
    )
    # Well-typed values pass.
    validate_config(
        {"components": {"synth": {"verbose": True, "count": 5}}}, synthetic
    )
    # Bad-typed value raises.
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(
            {"components": {"synth": {"count": "five"}}}, synthetic
        )
    assert "expected int, got str" in str(exc_info.value)


# ---------------------------------------------------------------------
# Manifest carries the new field with the right default
# ---------------------------------------------------------------------


def test_manifest_config_schema_defaults_to_empty_dict():
    """A `Manifest` constructed without a `config_schema` argument
    gets an empty dict — the six non-canary manifests
    (mattermost / attention / focus_advance / auto_unfreeze /
    auto_approve / validator_judge) continue to load until TB-322
    fills them in."""
    m = Manifest(
        name="ephemeral",
        env_flag=None,
        default_enabled=True,
        hook_points={},
    )
    assert m.config_schema == {}
    assert isinstance(m.config_schema, dict)
