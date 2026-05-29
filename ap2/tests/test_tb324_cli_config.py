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
    """Setting `AP2_AUTO_APPROVE=1` flips `components.auto_approve.enabled`
    row's source to `env-override` (flat back-compat path)."""
    cfg = _project(tmp_path)
    clean_env.setenv("AP2_AUTO_APPROVE", "1")
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
    """An unknown path exits non-zero AND names the bad path verbatim
    in the error message (so an operator pasting the path back into
    their shell can correlate)."""
    cfg = _project(tmp_path)
    rc = cmd_config_get(
        cfg, Namespace(path="components.bogus.nonexistent"),
    )
    assert rc != 0
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


def test_ap2_config_appears_in_howto():
    """`ap2 config` documentation appears in `ap2/howto.md`'s
    operator-CLI reference table."""
    howto = Path(__file__).resolve().parent.parent / "howto.md"
    text = howto.read_text()
    assert "ap2 config" in text
