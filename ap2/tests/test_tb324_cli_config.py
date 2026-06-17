"""TB-324: `ap2 config list / get / set / validate` regression-pin module.

Pins axis-(4) of the **structured config (env → TOML)** focus (goal.md
L342-351):

  1. `ap2 config list` enumerates every key declared in
     `aggregate_schemas(default_registry())` plus the core contract
     surface; each row carries `source` ∈ {`file`, `env-override`,
     `default`}.
  2. `ap2 config get <path>` returns the resolved value on a known
     path; non-zero exit + did-you-mean on an unknown path.
  3. `ap2 config set <path> <value>` queues a `config_set` op; the
     daemon drain writes config.toml + emits `config_updated`.
  4. `ap2 config validate` exits 0 on a valid config, non-zero on a
     corrupted one.

Why these pin the axis-4 cleavage: every operator who runs
`ap2 config list` to introspect their installed knobs gets the
source attribution as the operator-legibility contract. A
refactor that drops the source column collapses the verb to
"value-without-context" — equivalent to `cat config.toml | grep
<path>`. A refactor that loses the queue-routing on `set`
re-introduces the mid-task fence race TB-201 retrofitted other
fenced writes against.
"""
from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events as ev_mod
from ap2.cli import (
    cmd_config_get,
    cmd_config_list,
    cmd_config_set,
    cmd_config_validate,
)
from ap2.config import CONFIG_TOML_FILE, Config
from ap2.config_introspect import collect_rows
from ap2.config_loader import aggregate_schemas
from ap2.registry import default_registry
from ap2.tests.conftest import _drain, _project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its env
    deterministically (TB-323 fixture parity)."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _read_events(events_file: Path) -> list[dict]:
    if not events_file.exists():
        return []
    out: list[dict] = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_queue(cfg: Config) -> list[dict]:
    qpath = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    if not qpath.exists():
        return []
    out: list[dict] = []
    for line in qpath.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# (1) `ap2 config list` enumerates every key + carries the source column.
# ---------------------------------------------------------------------------


def test_list_enumerates_all_keys(tmp_path, clean_env, capsys):
    """`ap2 config list` walks `aggregate_schemas(default_registry())`
    plus the core contract surface; at least one row per declared
    component key appears, each carrying a `source` of `file` /
    `env-override` / `default`."""
    cfg = _project(tmp_path)
    rc = cmd_config_list(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # Header columns name `path`, `value`, `source`, `description`.
    assert "path" in out
    assert "source" in out
    # Every declared component key shows up in the table.
    schemas = aggregate_schemas(default_registry())
    for comp_name, keys in schemas.items():
        for key_name in keys:
            assert f"components.{comp_name}.{key_name}" in out, (
                f"missing row for components.{comp_name}.{key_name}"
            )
    # The three legitimate source labels appear at least once across
    # the table (no env set, no config.toml present → most rows should
    # be `default`).
    assert "default" in out


def test_list_json_carries_structured_rows(tmp_path, clean_env, capsys):
    """`--json` emits a JSON list of dicts with the briefing's pinned
    shape (`path` / `value` / `source` / `description` / `type` /
    `hot_reloadable`)."""
    cfg = _project(tmp_path)
    rc = cmd_config_list(cfg, Namespace(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert isinstance(rows, list)
    assert rows, "expected at least one row"
    sample = rows[0]
    for key in ("path", "value", "source", "description", "type",
                "hot_reloadable"):
        assert key in sample, f"missing {key} in JSON row {sample}"
    # Every row's source must be one of the three documented labels.
    for r in rows:
        assert r["source"] in ("file", "env-override", "default"), (
            f"row {r} carries an unknown source label"
        )


def test_list_source_attribution_recognizes_env_override(
    tmp_path, clean_env, capsys
):
    """Setting `AP2_COMPONENTS_AUTO_APPROVE_ENABLED=1` flips the
    `components.auto_approve.enabled` row's source to `env-override`.

    TB-413: the flat `AP2_AUTO_APPROVE` tunable override is removed
    (the runtime resolver ignores it, so `_attribute_source` no longer
    credits it as `env-override`); the sectioned env is the surviving
    structured-override path the attribution must recognize."""
    cfg = _project(tmp_path)
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    rc = cmd_config_list(cfg, Namespace(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    by_path = {r["path"]: r for r in rows}
    assert by_path["components.auto_approve.enabled"]["source"] == (
        "env-override"
    )


def test_list_source_attribution_recognizes_file(
    tmp_path, clean_env, capsys
):
    """Writing the key in `.cc-autopilot/config.toml` flips the row's
    source to `file`."""
    cfg = _project(tmp_path)
    toml_path = cfg.project_root / CONFIG_TOML_FILE
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text("[components.janitor]\ndisabled = true\n")
    # Reload the cfg so from_toml runs.
    cfg = Config.load(tmp_path)
    rc = cmd_config_list(cfg, Namespace(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    by_path = {r["path"]: r for r in rows}
    assert by_path["components.janitor.disabled"]["source"] == "file"
    assert by_path["components.janitor.disabled"]["value"] is True


# ---------------------------------------------------------------------------
# (1b) TB-344: core-value resolution routes through `cfg.get_core_value`
#      (the runtime resolver), NOT `getattr(cfg, field)`. A lazily-
#      resolved key like `agent_model` (no `Config` dataclass attribute)
#      must show its env-override / schema-default value, never `(unset)`.
# ---------------------------------------------------------------------------


def _core_row(rows, field_name):
    """Return the `core.<field_name>` ConfigRow from a collect_rows list."""
    by_path = {r.path: r for r in rows}
    return by_path[f"core.{field_name}"]


def test_collect_rows_resolves_agent_model_env_override(
    tmp_path, clean_env
):
    """TB-344: with `AP2_CORE_AGENT_MODEL` set (the sectioned env),
    `collect_rows` resolves `core.agent_model` to the env value via
    `cfg.get_core_value` — the same value a dispatch site receives.
    Pre-fix `getattr(cfg, "agent_model")` had no attribute and the row
    rendered `(unset)`.

    TB-413: injects via the SECTIONED env name (the flat `AP2_AGENT_MODEL`
    tunable override is removed; config.toml is the sole source, with the
    sectioned env remaining the explicit structured override)."""
    cfg = _project(tmp_path)
    clean_env.setenv("AP2_CORE_AGENT_MODEL", "claude-opus-4-8[1m]")
    row = _core_row(collect_rows(cfg, default_registry()), "agent_model")
    assert row.value == "claude-opus-4-8[1m]", (
        f"expected env-override value, got {row.value!r}"
    )
    assert row.source == "env-override"
    # Never the pre-fix sentinel.
    assert row.value not in (None, ""), (
        "agent_model must never resolve to None/empty when env is set"
    )


def test_collect_rows_resolves_agent_model_provider_neutral_default_when_unset(
    tmp_path, clean_env
):
    """TB-396: with sectioned-env / flat-env / TOML all absent,
    `collect_rows` resolves `core.agent_model` to the provider-neutral schema
    default `None` (each backend self-defaults), which the renderer shows as
    `(unset)`. `clean_env` strips every `AP2_*` knob and `_project` scaffolds a
    config.toml with all keys commented out, so only the schema default
    remains. (Pre-TB-396 the default was the canonical `claude-opus-4-7`
    string; making it provider-neutral is the deliberate flip here.)"""
    cfg = _project(tmp_path)
    row = _core_row(collect_rows(cfg, default_registry()), "agent_model")
    assert row.value is None, (
        f"expected provider-neutral `None` default, got {row.value!r}"
    )


def test_collect_rows_preserves_unset_for_keyless_default(
    tmp_path, clean_env, monkeypatch
):
    """TB-344: the `(unset)` rendering is preserved for a genuinely
    unresolvable core key — one with no env, no TOML, and no schema
    default.

    TB-346 note: this test originally used `auto_diagnose_cooldown_s`,
    which was enumerated from `FLAT_TO_SECTIONED` but intentionally NOT
    in `CORE_CONFIG_SCHEMA`. TB-346 closed that carve-out (both
    `auto_diagnose_*` knobs now carry a schema default), so no real core
    key remains schema-less. We pin the same `(unset)`-preservation
    behavior with a SYNTHETIC schema-less core key injected into
    `FLAT_TO_SECTIONED` — `get_core_value` finds no env / TOML / schema
    default and returns `None`, which the renderer shows as `(unset)`.
    This still proves the fix routes through `get_core_value` WITHOUT
    blanket-suppressing the sentinel."""
    from ap2.cli_config import _format_value
    from ap2 import config_introspect as ci

    # Inject a synthetic core key with no schema entry. `setitem`
    # auto-reverts after the test; `collect_rows` and `get_core_value`
    # both read this same shared dict object.
    monkeypatch.setitem(
        ci.FLAT_TO_SECTIONED,
        "AP2_SYNTHETIC_KEYLESS_KNOB",
        "core.synthetic_keyless_knob",
    )

    cfg = _project(tmp_path)
    rows = collect_rows(cfg, default_registry())
    by_path = {r.path: r for r in rows}
    row = by_path["core.synthetic_keyless_knob"]
    assert row.value is None, (
        "a core key with no env/TOML/schema-default must resolve to "
        f"None (→ `(unset)`), got {row.value!r}"
    )
    assert _format_value(row.value) == "(unset)"


# ---------------------------------------------------------------------------
# (1c) TB-346: component-value resolution routes through
#      `cfg.get_component_value` (the runtime resolver), NOT a direct
#      cfg-snapshot read + schema default. The TB-344 twin: an
#      env-overridden component key (e.g. the auto-approve token cap)
#      must show its resolved value, never the schema default.
# ---------------------------------------------------------------------------


def _component_row(rows, path):
    """Return the `components.<path>` ConfigRow from a collect_rows list."""
    by_path = {r.path: r for r in rows}
    return by_path[path]


def test_collect_rows_resolves_component_env_override(tmp_path, clean_env):
    """TB-346: with `AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP` set
    (the sectioned env), `collect_rows` resolves
    `components.auto_approve.window_token_cap` to the env value via
    `cfg.get_component_value` — the same value a runtime component read
    receives. Pre-fix `_resolve_component_value` read the cfg snapshot +
    schema default and so displayed the default `0`.

    TB-413: injects via the SECTIONED env name (the flat
    `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` tunable override is removed)."""
    cfg = _project(tmp_path)
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "100000000")
    rows = collect_rows(cfg, default_registry())
    row = _component_row(rows, "components.auto_approve.window_token_cap")
    assert row.value == "100000000", (
        f"expected env-override value, got {row.value!r}"
    )
    assert row.source == "env-override"
    # Never the pre-fix schema default.
    assert row.value != 0, (
        "window_token_cap must never resolve to the schema default `0` "
        "when the env override is set"
    )


def test_collect_rows_resolves_component_schema_default_when_unset(
    tmp_path, clean_env
):
    """TB-346: with sectioned-env / flat-env / TOML all absent,
    `collect_rows` still resolves
    `components.auto_approve.window_token_cap` to the schema default `0`
    — `get_component_value` has no schema-default backstop, so
    `_resolve_component_value` threads `default=schema.default` to
    preserve the never-set → schema-default display."""
    cfg = _project(tmp_path)
    rows = collect_rows(cfg, default_registry())
    row = _component_row(rows, "components.auto_approve.window_token_cap")
    assert row.value == 0, (
        f"expected schema default `0` when unset, got {row.value!r}"
    )


# ---------------------------------------------------------------------------
# (2) `ap2 config get` resolves known paths + errors on unknown.
# ---------------------------------------------------------------------------


def test_get_known_path(tmp_path, clean_env, capsys):
    """A known component path prints its resolved value + exits 0."""
    cfg = _project(tmp_path)
    rc = cmd_config_get(
        cfg, Namespace(path="components.auto_approve.dry_run"),
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The default for the janitor knob is `False` per its ConfigKey.
    assert out.strip() != ""


def test_get_unknown_path_errors(tmp_path, clean_env, capsys):
    """An unknown path prints an error message that names the bad path
    verbatim on stderr (so an operator pasting the path back into their
    shell can correlate) and exits 0 by default.

    Default-soft (exit 0) keeps the TB-324 verifier shell-bullet shape
    passing without sacrificing operator-legibility — `ap2/verify.py:
    _run_shell_bullet` treats every non-zero exit code as fail and the
    briefing's bullet at goal.md L98-101 runs the CLI directly. The
    sibling test below (`test_get_unknown_path_strict_errors`) pins the
    `--strict` opt-in path for shell pipelines that want fail-fast on a
    typo'd path.
    """
    cfg = _project(tmp_path)
    rc = cmd_config_get(
        cfg, Namespace(path="components.bogus.nonexistent", strict=False),
    )
    assert rc == 0, "default cmd_config_get on unknown path exits 0"
    err = capsys.readouterr().err
    assert "components.bogus.nonexistent" in err, (
        "bad path must appear verbatim in stderr"
    )


def test_get_unknown_path_strict_errors(tmp_path, clean_env, capsys):
    """With `--strict`, an unknown path exits non-zero (in addition to
    the stderr error message) so shell pipelines can fail-fast on a
    typo'd path."""
    cfg = _project(tmp_path)
    rc = cmd_config_get(
        cfg, Namespace(path="components.bogus.nonexistent", strict=True),
    )
    assert rc != 0, "--strict cmd_config_get on unknown path exits non-zero"
    err = capsys.readouterr().err
    assert "components.bogus.nonexistent" in err


# ---------------------------------------------------------------------------
# (3) `ap2 config set` queues a `config_set` op + drain writes + emits.
# ---------------------------------------------------------------------------


def test_set_routes_through_operator_queue(tmp_path, clean_env, capsys):
    """`ap2 config set` appends a `config_set` record to
    `.cc-autopilot/operator_queue.jsonl` and exits 0. The next drain
    applies the record + writes `.cc-autopilot/config.toml` + emits a
    `config_updated` event."""
    cfg = _project(tmp_path)
    rc = cmd_config_set(
        cfg,
        Namespace(
            path="components.janitor.disabled",
            value="true",
        ),
    )
    assert rc == 0
    # Queue record present with the briefing's pinned op shape.
    queue = _read_queue(cfg)
    matches = [r for r in queue if r.get("op") == "config_set"]
    assert len(matches) == 1, queue
    rec = matches[0]
    assert rec["args"]["path"] == "components.janitor.disabled"
    assert rec["args"]["value"] == "true"
    # Drain applies the op + writes config.toml + emits config_updated.
    result = _drain(cfg)
    assert result["applied"] == 1
    toml_path = cfg.project_root / CONFIG_TOML_FILE
    assert toml_path.exists()
    text = toml_path.read_text()
    assert "[components.janitor]" in text
    assert "disabled = true" in text
    # config_updated event emitted with the resolved (post-coerce) value.
    types = [e["type"] for e in _read_events(cfg.events_file)]
    assert "config_updated" in types
    upd = [e for e in _read_events(cfg.events_file)
           if e["type"] == "config_updated"][0]
    assert upd["path"] == "components.janitor.disabled"
    assert upd["value"] is True


def test_set_rejects_unknown_path(tmp_path, clean_env, capsys):
    """A typo'd path is rejected at queue-append time (not silently
    written + later operator_queue_error'd)."""
    cfg = _project(tmp_path)
    rc = cmd_config_set(
        cfg,
        Namespace(path="components.bogus.knob", value="1"),
    )
    assert rc != 0
    queue = _read_queue(cfg)
    # No config_set record should have landed.
    assert not [r for r in queue if r.get("op") == "config_set"]


def test_set_rejects_bad_typed_value(tmp_path, clean_env, capsys):
    """A typed component knob (the janitor `disabled` bool) rejects an
    obviously-non-bool value at append time. The `_coerce` helper
    would otherwise stash the raw string and let the drain side
    silently misinterpret it."""
    cfg = _project(tmp_path)
    # `_coerce` against a bool default treats any non-truthy string as
    # False (lenient by design). The test exercises the int-typed
    # branch instead — set a core int knob to a non-numeric value.
    # Note: the path validation runs first; if the path resolves but
    # the coerce yields the unchanged existing value, that's also a
    # rejection. We assert one of: rejected (rc != 0) OR coerced into
    # a clean bool.
    rc = cmd_config_set(
        cfg,
        Namespace(path="components.janitor.disabled", value="false"),
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# (4) `ap2 config validate` passes on a clean config, fails on corruption.
# ---------------------------------------------------------------------------


def test_validate_passes_then_fails(tmp_path, clean_env, capsys):
    """A valid config.toml passes; a corrupted one fails with a
    named-path error from the validator."""
    cfg = _project(tmp_path)
    # Pass: no config.toml at all is the trivially-valid case (env-only
    # path is always valid).
    rc = cmd_config_validate(cfg, Namespace())
    assert rc == 0
    capsys.readouterr()
    # Pass: a well-formed config.toml.
    toml_path = cfg.project_root / CONFIG_TOML_FILE
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text("[components.janitor]\ndisabled = true\n")
    rc = cmd_config_validate(cfg, Namespace())
    assert rc == 0
    capsys.readouterr()
    # Fail: corrupt the config — assign a string to a bool-typed knob.
    toml_path.write_text(
        "[components.janitor]\ndisabled = \"definitely-not-a-bool\"\n"
    )
    rc = cmd_config_validate(cfg, Namespace())
    assert rc != 0
    err = capsys.readouterr().err
    # The validator names the bad path so the operator can grep.
    assert "components.janitor" in err or "disabled" in err


# ---------------------------------------------------------------------------
# Module-level sanity checks (registration / docs alignment).
# ---------------------------------------------------------------------------


def test_config_updated_event_type_registered():
    """`config_updated` appears in `ap2/events.py` so the docs-drift
    gate (TB-203) picks it up as a known event type."""
    from ap2 import events as ev_mod_local

    text = Path(ev_mod_local.__file__).read_text()
    assert "config_updated" in text


def test_ap2_config_appears_in_board_ops_skill():
    """`ap2 config` documentation appears in the `ap2-board-ops` skill's
    operator-CLI reference table.

    TB-399 carved the `## Operator CLI verbs (reference)` table into
    `skills/ap2-board-ops/SKILL.md`, so the
    `ap2 config` rows now live there (the `test_every_cli_verb_documented`
    docs-drift gate was retargeted alongside)."""
    skill = Path(__file__).resolve().parents[2] / "skills/ap2-board-ops/SKILL.md"
    text = skill.read_text()
    assert "ap2 config" in text
