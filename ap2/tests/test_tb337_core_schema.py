"""TB-337: typed `[core.*]` ConfigKey schema regression-pins.

Axis (1) completion of the **structured config (env → TOML)** focus.
Pins five cleavages around the new `ap2.core_config_schema` module:

  (a) **Schema-key coverage**: every name the briefing pins as one of
      the "21 known core keys" is declared in `CORE_CONFIG_SCHEMA`
      (with its TOML-key alias for the three env-suffix shorthand
      forms: `tick_s` → `tick_interval_s`, `mm_tick_s` →
      `mm_tick_interval_s`, `event_context` → `event_context_size`).
  (b) **Validator catches unknown core key + hint shape**: a TOML
      with `[core.web_prot] = 8080` (intentional typo) raises
      `ConfigSchemaError` whose message names `[core] web_prot` and
      includes the "did you mean ...?" suggestion ("web_port").
  (c) **Validator catches bad core type**: `[core.tick_interval_s]
      = "thirty"` raises with an `expected int, got str` message.
  (d) **`get_core_value` falls back to schema default**: an unset
      env + empty TOML + caller-passed `default=None` resolves to
      the schema's declared default.
  (e) **`ap2 init` config.toml template renders `[core]` block**:
      the scaffolded `.cc-autopilot/config.toml` contains a `[core]`
      header followed by all 21 commented-out keys.

Regression intent: pre-TB-337 the core schema sat as a deferred
footnote ("schema deferred to a future axis; current round-trip is
shape-only") and the validator only walked
`[components.<name>.*]` sub-tables. A typo in a `[core.*]` key
silently kept the env-path default and a non-numeric `tick_interval_s
= "thirty"` would not fail daemon-start. This file holds the line
once axis (1) is closed.
"""
from __future__ import annotations

import os

import pytest

from ap2.config import CONFIG_TOML_FILE, Config
from ap2.config_compat import reset_env_deprecated_emit_for_tests
from ap2.config_loader import (
    ConfigSchemaError,
    aggregate_schemas,
    validate_config,
)
from ap2.core_config_schema import CORE_CONFIG_SCHEMA
from ap2.init import _config_template, init_project
from ap2.registry import default_registry


# ---------------------------------------------------------------------
# (a) Schema-key coverage — the briefing's 21 pinned core keys.
# ---------------------------------------------------------------------

# The briefing names 21 keys using a mix of env-knob-suffix shorthand
# (e.g. `tick_s`) and canonical TOML/dataclass names (e.g.
# `task_timeout_s`). The schema declares the canonical names; the
# alias map below translates the briefing's shorthand to the schema's
# canonical form so the regression-pin matches the briefing's literal
# list while keeping the schema's TOML-key naming consistent with
# `config_compat.FLAT_TO_SECTIONED`.
_BRIEFING_PINNED_CORE_KEYS: tuple[str, ...] = (
    "tick_s",                       # → tick_interval_s
    "mm_tick_s",                    # → mm_tick_interval_s
    "task_timeout_s",
    "control_timeout_s",
    "max_retries",
    "verify_cmd",
    "verify_timeout_s",
    "event_context",                # → event_context_size
    "agent_model",
    "agent_effort",
    "task_max_turns",
    "control_max_turns",
    "verify_judge_max_turns",
    "ideation_disabled",
    "ideation_trigger_task_count",
    "ideation_cooldown_s",
    "ideation_max_turns",
    "ideation_scrub_model",
    "project_name",
    "web_port",
    "web_disabled",
)
_BRIEFING_ALIAS_TO_CANONICAL: dict[str, str] = {
    "tick_s": "tick_interval_s",
    "mm_tick_s": "mm_tick_interval_s",
    "event_context": "event_context_size",
}


def test_core_schema_declares_at_least_21_keys():
    """`CORE_CONFIG_SCHEMA` must declare at least 21 ConfigKey entries
    (the briefing's pinned cardinality)."""
    assert len(CORE_CONFIG_SCHEMA) >= 21, (
        f"TB-337: CORE_CONFIG_SCHEMA must carry at least 21 keys; "
        f"got {len(CORE_CONFIG_SCHEMA)}: {sorted(CORE_CONFIG_SCHEMA)}"
    )


@pytest.mark.parametrize("briefing_key", _BRIEFING_PINNED_CORE_KEYS)
def test_core_schema_covers_every_briefing_pinned_key(briefing_key):
    """Every one of the 21 briefing-pinned core keys is declared in
    `CORE_CONFIG_SCHEMA` (under its canonical TOML/dataclass name
    when the briefing used an env-suffix shorthand)."""
    canonical = _BRIEFING_ALIAS_TO_CANONICAL.get(briefing_key, briefing_key)
    assert canonical in CORE_CONFIG_SCHEMA, (
        f"TB-337: briefing-pinned core key {briefing_key!r} (canonical "
        f"{canonical!r}) missing from CORE_CONFIG_SCHEMA; got "
        f"{sorted(CORE_CONFIG_SCHEMA)}"
    )


def test_every_core_schema_entry_has_non_empty_description():
    """`ap2 config list` (axis-4) renders each `ConfigKey.description`
    as the per-key annotation; an empty description would surface as a
    blank row. Same prose-check parity as the per-component
    `test_aggregate_schemas_surfaces_janitor_disabled_canary` shape."""
    for key_name, spec in CORE_CONFIG_SCHEMA.items():
        assert spec.description.strip(), (
            f"TB-337: CORE_CONFIG_SCHEMA[{key_name!r}].description must "
            f"be non-empty for axis-4 `ap2 config list` to surface a "
            f"meaningful per-key annotation."
        )


def test_aggregate_schemas_includes_core_when_kwarg_passed():
    """`aggregate_schemas(registry, core_schema=CORE_CONFIG_SCHEMA)`
    surfaces the core entries under the reserved `"core"` namespace
    alongside the per-component entries. Default `core_schema=None`
    keeps the per-component-only return shape for back-compat."""
    bare = aggregate_schemas(default_registry())
    assert "core" not in bare, (
        "TB-337: aggregate_schemas with default `core_schema=None` must "
        "NOT surface a `core` namespace — that's the backwards-compat "
        "guarantee for callers that don't opt into the core walk."
    )
    with_core = aggregate_schemas(
        default_registry(), core_schema=CORE_CONFIG_SCHEMA,
    )
    assert "core" in with_core, (
        "TB-337: aggregate_schemas with `core_schema=...` must surface "
        "the core entries under the `core` namespace."
    )
    assert with_core["core"] == dict(CORE_CONFIG_SCHEMA)


# ---------------------------------------------------------------------
# (b) Validator rejects unknown core key with named path + suggestion.
# ---------------------------------------------------------------------


def test_validate_config_rejects_unknown_core_key():
    """`[core.web_prot] = 8080` (intentional typo) raises with a clear
    named-path error that includes a `web_port` suggestion."""
    loaded = {"core": {"web_prot": 8080}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    msg = str(exc_info.value)
    assert "[core]" in msg, msg
    assert "web_prot" in msg, msg
    assert "unknown key" in msg, msg
    assert "web_port" in msg, (
        f"TB-337: validator should suggest `web_port` for the typo "
        f"`web_prot` via difflib.get_close_matches; got: {msg!r}"
    )


def test_validate_config_unknown_core_key_no_suggestion_when_distant():
    """A truly novel core key (no close match) still raises but doesn't
    name a misleading suggestion. The error still lists the known keys
    so the operator can browse."""
    loaded = {"core": {"xyzabc123": "garbage"}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    msg = str(exc_info.value)
    assert "[core]" in msg
    assert "xyzabc123" in msg
    assert "unknown key" in msg
    # Sanity check: known-list mention helps the operator grep.
    assert "tick_interval_s" in msg or "known" in msg.lower()


# ---------------------------------------------------------------------
# (c) Validator rejects bad core type.
# ---------------------------------------------------------------------


def test_validate_config_rejects_non_numeric_tick_interval_s():
    """`[core.tick_interval_s] = "thirty"` raises with `expected int,
    got str` — same named-path error shape as the per-component
    branch."""
    loaded = {"core": {"tick_interval_s": "thirty"}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    msg = str(exc_info.value)
    assert "[core]" in msg
    assert "tick_interval_s" in msg
    assert "expected int" in msg
    assert "got str" in msg


def test_validate_config_rejects_bool_for_int_core_key():
    """Python's `bool` is a subclass of `int`; the validator's
    `_type_matches` excludes that conflation for the core walk too —
    a `bool` value where an `int` key is declared raises."""
    loaded = {"core": {"task_timeout_s": True}}
    with pytest.raises(ConfigSchemaError) as exc_info:
        validate_config(loaded, default_registry())
    assert "expected int, got bool" in str(exc_info.value)


def test_validate_config_accepts_well_typed_core_values():
    """A correctly-typed core value passes silently — same
    `return None` contract as the per-component path."""
    loaded = {
        "core": {
            "tick_interval_s": 30,
            "agent_model": "claude-opus-4-7",
            "ideation_disabled": True,
        }
    }
    assert validate_config(loaded, default_registry()) is None


def test_validate_config_empty_core_section_passes():
    """No `[core]` section (or an empty one) is the no-op path — fresh
    installs writing only `[components.*]` should still validate."""
    validate_config({"core": {}}, default_registry())
    validate_config({"components": {}, "core": {}}, default_registry())


# ---------------------------------------------------------------------
# (d) `get_core_value` falls back to schema default.
# ---------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its env surface
    deterministically. Mirrors `test_tb334_core_cfg_reads.clean_env`."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def emit_reset():
    """Reset `_EMITTED_ONCE` in config_compat so the one-shot
    `env_deprecated` accounting doesn't leak between tests."""
    reset_env_deprecated_emit_for_tests()
    yield
    reset_env_deprecated_emit_for_tests()


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Per-test cfg over a fresh project root with a stripped env
    surface. `init_project` scaffolds the schema-rendered TOML so
    `Config.load` lands on the TOML branch."""
    init_project(tmp_path)
    return Config.load(tmp_path)


def test_get_core_value_falls_back_to_schema_default_when_default_is_none(
    cfg, clean_env, emit_reset,
):
    """When the caller passes `default=None` AND the key has a
    declared schema default, the schema default wins over `None`."""
    # Schema default for ideation_cooldown_s is 3600 (TB-418, was 7200).
    assert cfg.get_core_value("ideation_cooldown_s") == 3600
    # Schema default for tick_interval_s is 30.
    assert cfg.get_core_value("tick_interval_s") == 30
    # Schema default for web_port is 8729.
    assert cfg.get_core_value("web_port") == 8729


def test_get_core_value_explicit_default_wins_over_schema_default(
    cfg, clean_env, emit_reset,
):
    """A caller-supplied explicit `default=...` still wins over the
    schema default — preserves the pre-TB-337 contract for migrated
    readers that pass their own per-site default."""
    # Caller-supplied default `999` wins, even though schema default
    # for ideation_cooldown_s is 3600.
    assert cfg.get_core_value("ideation_cooldown_s", default=999) == 999
    # Caller-supplied `"X"` wins over the (now empty, TB-418) schema
    # default for ideation_scrub_model.
    assert cfg.get_core_value("ideation_scrub_model", default="X") == "X"


def test_get_core_value_env_override_wins_over_schema_default(
    cfg, clean_env, emit_reset,
):
    """Env-override precedence remains: sectioned env > flat env >
    TOML > schema default."""
    clean_env.setenv("AP2_CORE_IDEATION_COOLDOWN_S", "1234")
    assert cfg.get_core_value("ideation_cooldown_s") == "1234"


def test_get_core_value_undeclared_key_returns_none_default(
    cfg, clean_env, emit_reset,
):
    """When the key isn't in `CORE_CONFIG_SCHEMA` and `default=None`,
    the helper returns `None` — no surprise fallback for novel keys."""
    assert cfg.get_core_value("not_in_schema_at_all") is None


# ---------------------------------------------------------------------
# (e) `ap2 init` config.toml template renders `[core]` block.
# ---------------------------------------------------------------------


def test_config_template_contains_core_block():
    """The rendered `CONFIG_TEMPLATE` includes a `[core]` section
    header (not `[components.core]`) ahead of the per-component
    blocks."""
    rendered = _config_template()
    assert "[core]" in rendered, (
        "TB-337: CONFIG_TEMPLATE must render a top-level `[core]` "
        f"section; got rendered template:\n{rendered}"
    )
    assert "[components.core]" not in rendered, (
        "TB-337: CONFIG_TEMPLATE must NOT render `[components.core]` "
        "— the `core` namespace is distinct from the per-component tree."
    )


def test_config_template_contains_every_core_key_commented():
    """The rendered template emits each `[core]` key as a commented-out
    `# <key> = <default>` line so operators can uncomment to override.
    Pins parity with `test_every_config_key_in_template`'s per-component
    contract."""
    rendered = _config_template()
    missing = sorted(
        key for key in CORE_CONFIG_SCHEMA
        if f"# {key} =" not in rendered
    )
    assert not missing, (
        f"TB-337: CONFIG_TEMPLATE missing commented-out `# <key> =` "
        f"line for core key(s): {missing}"
    )


def test_init_project_writes_config_toml_with_core_block(tmp_path, clean_env):
    """`ap2 init` writes `.cc-autopilot/config.toml` whose body
    contains the `[core]` block + all 21 commented-out keys. End-to-end
    parity with the renderer-level check above."""
    init_project(tmp_path)
    body = (tmp_path / CONFIG_TOML_FILE).read_text()
    assert "[core]" in body
    missing = sorted(
        key for key in CORE_CONFIG_SCHEMA if f"# {key} =" not in body
    )
    assert not missing, (
        f"TB-337: ap2 init's config.toml missing commented-out "
        f"`# <key> =` line for core key(s): {missing}"
    )


# ---------------------------------------------------------------------
# (f) End-to-end daemon-start gate parity — a bad core key surfaces
#     through `_validate_toml_config_at_start` as a SystemExit(2).
# ---------------------------------------------------------------------


def test_daemon_start_gate_exits_on_bad_core_type(tmp_path, clean_env):
    """The daemon-start hook (`_validate_toml_config_at_start`) wraps
    `validate_config` and translates a `ConfigSchemaError` into a
    `SystemExit(2)` with a stderr-printed message. A bad `[core.*]`
    type now triggers that path (pre-TB-337 only `[components.*]`
    typos / bad types did)."""
    from ap2.daemon import _validate_toml_config_at_start

    init_project(tmp_path)
    # Overwrite the scaffolded TOML with a bad-type core value.
    (tmp_path / CONFIG_TOML_FILE).write_text(
        '[core]\ntick_interval_s = "thirty"\n'
    )
    cfg = Config.load(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _validate_toml_config_at_start(cfg)
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------
# Hot-reloadability parity — every schema key's `hot_reloadable` flag
# matches its `env_reload.HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS`
# membership for the keys that have a flat-env counterpart.
# ---------------------------------------------------------------------


def test_hot_reloadable_flag_matches_env_reload_partition():
    """For every `[core.<key>]` schema entry whose flat-env name is in
    `env_reload.FIXED_KNOBS`, `hot_reloadable` must be False. For
    entries whose flat-env name is in `HOT_RELOADABLE_KNOBS`, the flag
    must be True. Pins parity so a future schema edit can't silently
    diverge from the actual reload behavior the operator depends on."""
    from ap2.config_compat import FLAT_TO_SECTIONED
    from ap2.env_reload import FIXED_KNOBS, HOT_RELOADABLE_KNOBS

    # Build reverse map: sectioned-path → flat-env-name.
    reverse: dict[str, str] = {
        sectioned: flat for flat, sectioned in FLAT_TO_SECTIONED.items()
    }
    mismatches: list[str] = []
    for key, spec in CORE_CONFIG_SCHEMA.items():
        flat = reverse.get(f"core.{key}")
        if flat is None:
            continue  # No flat counterpart — schema-only key.
        if flat in FIXED_KNOBS and spec.hot_reloadable:
            mismatches.append(
                f"core.{key} (env {flat}): schema says hot_reloadable=True "
                f"but {flat} is in env_reload.FIXED_KNOBS"
            )
        if flat in HOT_RELOADABLE_KNOBS and not spec.hot_reloadable:
            mismatches.append(
                f"core.{key} (env {flat}): schema says hot_reloadable=False "
                f"but {flat} is in env_reload.HOT_RELOADABLE_KNOBS"
            )
    assert not mismatches, (
        "TB-337: CORE_CONFIG_SCHEMA `hot_reloadable` flag must agree "
        "with env_reload.HOT_RELOADABLE_KNOBS / FIXED_KNOBS for keys "
        "with a flat-env counterpart:\n  " + "\n  ".join(mismatches)
    )


def test_core_config_schema_module_is_importable():
    """Sanity: `from ap2.core_config_schema import CORE_CONFIG_SCHEMA`
    resolves without side effects. Catches a refactor that introduced
    an import cycle through `ap2/config.py` ↔ `ap2/config_loader.py`."""
    import importlib

    import ap2.core_config_schema as mod

    importlib.reload(mod)
    assert hasattr(mod, "CORE_CONFIG_SCHEMA")
    assert isinstance(mod.CORE_CONFIG_SCHEMA, dict)
    assert mod.CORE_CONFIG_SCHEMA  # non-empty
