"""TB-339: pin the drained ``_PENDING_MIGRATION_KNOBS`` debt set to empty.

Axis (5) final cleanup of the structured-config focus. TB-338 landed
the cut-line gate (`test_tb338_env_only_cut_line.py`) carrying a
2-entry pending-migration debt set as a documented exception. TB-339
drained those last two entries — `AP2_VERIFY_JUDGE_EFFORT` (at
`ap2/verify.py` L588) and `AP2_STATUS_REPORT_EFFORT` (at
`ap2/status_report.py` L2028) — by adding matching `ConfigKey`
entries to `CORE_CONFIG_SCHEMA` and swapping each direct env read
to a chained-``or`` `cfg.get_core_value(...)` shape.

This module pins the drained state across five regression cleavages
so a future regression that re-introduces a direct env read can't
quietly satisfy `test_tb338_env_only_cut_line` by re-adding an entry
to `_PENDING_MIGRATION_KNOBS`:

  (1) **Empty-pin**: ``_PENDING_MIGRATION_KNOBS`` is now ``frozenset()``.
      Without this pin, a future PR could in principle add a new entry
      to satisfy the cut-line gate; this assertion is the
      defensive twin of TB-338's `test_pending_migration_knobs_still_referenced`
      (which catches stale entries) — together they pin both
      directions.
  (2) **Schema declarations**: `verify_judge_effort` and
      `status_report_effort` both appear in `CORE_CONFIG_SCHEMA` with
      `type=str` and `default=""`. The empty-string default is
      load-bearing: the read-site `or`-chain collapses it to
      `agent_effort`, preserving the pre-TB-339 fallback contract.
  (3) **Grep-absence**: zero direct ``os.environ.get(<knob>)`` reads
      of the AP2_VERIFY_JUDGE_EFFORT and AP2_STATUS_REPORT_EFFORT
      knobs across ``ap2/`` (excluding ``ap2/tests/``). A textual
      regex twin to TB-338's AST-walker gate, kept here so a per-
      knob regression surfaces in the named TB rather than only via
      the broader cut-line gate.
  (4) **Per-site env precedence**: with the flat env knob set, the
      back-compat path through `FLAT_TO_SECTIONED` (config_compat.py
      L105-106) routes it to the matching `core.<key>` and
      `cfg.get_core_value(<key>)` returns the env value. Same shape
      as TB-334's `test_get_core_value_flat_env_parity` pinned for
      these two specific keys.
  (5) **Fallback chain**: with neither per-site env nor TOML override,
      the call-site `or`-chain falls back to `agent_effort` (and from
      there to the hardcoded per-site default — `high` for verify-
      judge, `medium` for status-report). The functional contract of
      the `or`-chain is exercised here without going through the
      live SDK setup at the read site.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.core_config_schema import CORE_CONFIG_SCHEMA
from ap2.init import init_project
from ap2.tests.test_tb338_env_only_cut_line import _PENDING_MIGRATION_KNOBS


# Repository root, derived from this file's location:
# ap2/tests/test_tb339_pending_migration_drained.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AP2_DIR = _REPO_ROOT / "ap2"


# The two knobs TB-339 migrated. The matching schema key name (without
# the `AP2_` prefix and lowercased) is the `core.<key>` form an operator
# would type into config.toml.
_TB339_MIGRATED_KEYS: tuple[tuple[str, str, str], ...] = (
    # (flat env, schema/core key, per-site hardcoded fallback default)
    ("AP2_VERIFY_JUDGE_EFFORT", "verify_judge_effort", "high"),
    ("AP2_STATUS_REPORT_EFFORT", "status_report_effort", "medium"),
)


# ---------------------------------------------------------------------------
# Fixtures (mirror the TB-334 cluster-pilot vocabulary).
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically.
    """
    import os

    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Per-test cfg over a fresh project root with a stripped env surface."""
    init_project(tmp_path)
    return Config.load(tmp_path)


@pytest.fixture
def emit_reset():
    """Reset `_EMITTED_ONCE` in config_compat so the one-shot
    `env_deprecated` accounting doesn't leak between tests.
    """
    reset_env_deprecated_emit_for_tests()
    yield
    reset_env_deprecated_emit_for_tests()


# ---------------------------------------------------------------------------
# (1) Empty-pin — the debt set is now empty by construction.
# ---------------------------------------------------------------------------


def test_pending_migration_knobs_is_empty():
    """TB-339: ``_PENDING_MIGRATION_KNOBS`` is now ``frozenset()``.

    Defensive twin to TB-338's
    ``test_pending_migration_knobs_still_referenced`` — that test catches
    stale entries (no longer read directly); this one catches a
    re-introduced direct env read that tries to satisfy the cut-line gate
    by adding a new entry here. With both pins in place the debt set is
    fixed at empty: adding an entry trips this test; an entry going stale
    trips TB-338's referential-integrity pin.
    """
    assert _PENDING_MIGRATION_KNOBS == frozenset(), (
        "TB-339 drained `_PENDING_MIGRATION_KNOBS` to empty "
        "(`frozenset()`). The set is intentionally pinned empty so a "
        "future regression that re-introduces a direct "
        "`os.environ.get(\"AP2_*\")` read can't quietly satisfy the "
        "TB-338 cut-line gate by adding the knob back here. If you "
        "need to defer a migration, route the read through "
        "`cfg.get_core_value(...)` or `cfg.get_component_value(...)` "
        "instead — the axis-5 migration's default path. "
        f"Current set: {sorted(_PENDING_MIGRATION_KNOBS)}"
    )


# ---------------------------------------------------------------------------
# (2) Schema declarations — both keys are typed in CORE_CONFIG_SCHEMA.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flat, key, _default", _TB339_MIGRATED_KEYS)
def test_migrated_key_declared_in_core_config_schema(flat, key, _default):
    """Both `verify_judge_effort` and `status_report_effort` appear in
    `CORE_CONFIG_SCHEMA`. Without these declarations, an operator who
    authors `[core] verify_judge_effort = "low"` in `config.toml` would
    hit `ConfigSchemaError: unknown key` at daemon-start
    (validate_config L302-310).
    """
    assert key in CORE_CONFIG_SCHEMA, (
        f"TB-339: `{key}` (migrated from `{flat}`) must be declared in "
        f"`CORE_CONFIG_SCHEMA`; got {sorted(CORE_CONFIG_SCHEMA)}"
    )


@pytest.mark.parametrize("flat, key, _default", _TB339_MIGRATED_KEYS)
def test_migrated_key_schema_default_is_empty_string(flat, key, _default):
    """Schema default is the empty string for both keys. The empty
    default is load-bearing: the read-site `or`-chain collapses it to
    `agent_effort`, preserving the pre-TB-339 fallback contract. Same
    convention `agent_effort` itself uses (core_config_schema.py
    L245-257 — "Empty default = no extra_args sent").
    """
    spec = CORE_CONFIG_SCHEMA[key]
    assert spec.type is str, (
        f"TB-339: `core.{key}` schema type must be `str`; got {spec.type}"
    )
    assert spec.default == "", (
        f"TB-339: `core.{key}` schema default must be `\"\"` so the "
        f"read-site `or`-chain collapses it to `agent_effort`; got "
        f"{spec.default!r}"
    )


# ---------------------------------------------------------------------------
# (3) Grep-absence — zero direct env reads of the migrated knobs.
# ---------------------------------------------------------------------------


def _iter_source_files() -> list[pathlib.Path]:
    """Every `*.py` under `ap2/` excluding `ap2/tests/` and
    `__pycache__/`. Mirrors the source-walk shape in
    `test_tb338_env_only_cut_line._iter_source_files`.
    """
    out: list[pathlib.Path] = []
    for path in sorted(_AP2_DIR.rglob("*.py")):
        rel = path.relative_to(_AP2_DIR)
        parts = rel.parts
        if parts and parts[0] == "tests":
            continue
        if "__pycache__" in parts:
            continue
        out.append(path)
    return out


@pytest.mark.parametrize("flat, _key, _default", _TB339_MIGRATED_KEYS)
def test_no_direct_env_read_of_migrated_knob(flat, _key, _default):
    """Zero `os.environ.get("<FLAT>"` call sites across `ap2/` source
    (excluding tests + `__pycache__`). Textual regex twin to TB-338's
    AST-walker gate, kept here so a per-knob regression surfaces in the
    named TB.
    """
    pattern = re.compile(rf"os\.environ\.get\([\"']{re.escape(flat)}[\"']")
    violations: list[str] = []
    for path in _iter_source_files():
        src = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        f"TB-339: `{flat}` must be read via "
        f"`cfg.get_core_value(...)`; found {len(violations)} "
        f"direct `os.environ.get(\"{flat}\", ...)` violation(s):\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# (4) Per-site env precedence — flat env routes through FLAT_TO_SECTIONED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flat, key, _default", _TB339_MIGRATED_KEYS)
def test_flat_to_sectioned_maps_migrated_knob(flat, key, _default):
    """`FLAT_TO_SECTIONED` maps the flat env knob to `core.<key>`. This
    is what makes the back-compat path light up for an operator whose
    `.cc-autopilot/env` still carries the flat name. Pin separately so
    a refactor that drops the mapping fails here loudly.
    """
    assert FLAT_TO_SECTIONED.get(flat) == f"core.{key}", (
        f"TB-339: `FLAT_TO_SECTIONED[{flat!r}]` must be "
        f"{f'core.{key}'!r}; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


@pytest.mark.parametrize("flat, key, _default", _TB339_MIGRATED_KEYS)
def test_flat_env_resolves_via_get_core_value(
    cfg, clean_env, emit_reset, flat, key, _default,
):
    """With the flat env knob set, `cfg.get_core_value(<key>)` returns
    the env value — the back-compat path through FLAT_TO_SECTIONED. Same
    parity shape as TB-334's `test_get_core_value_flat_env_parity`,
    pinned here for the two TB-339 keys so a per-knob regression
    surfaces in the named TB.
    """
    clean_env.setenv(flat, "low")
    assert cfg.get_core_value(key, default="") == "low", (
        f"TB-339: flat env `{flat}=low` should resolve to `\"low\"` "
        f"via `cfg.get_core_value({key!r})`."
    )


# ---------------------------------------------------------------------------
# (5) Fallback chain — the read-site `or` collapses empty to agent_effort.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, key, per_site_default",
    _TB339_MIGRATED_KEYS,
)
def test_or_chain_falls_through_to_agent_effort(
    cfg, clean_env, emit_reset, flat, key, per_site_default,
):
    """With neither per-site env nor TOML override, the call-site
    `or`-chain falls back to `cfg.get_core_value("agent_effort",
    default=<per_site_default>)`. Exercises the functional contract
    of the `or`-chain shape at `ap2/verify.py` L588 + `ap2/status_report.py`
    L2028 without going through the live SDK setup.

    First branch: per-site read returns `""` (no env, no TOML, schema
    default `""`). Second branch: global `agent_effort` is unset, so the
    caller's per-site hardcoded default (`high` / `medium`) wins.
    """
    per_site = cfg.get_core_value(key, default="")
    assert per_site == "", (
        f"TB-339: unset per-site `{key}` should resolve to `\"\"`; "
        f"got {per_site!r}"
    )
    effort = per_site or cfg.get_core_value(
        "agent_effort", default=per_site_default,
    )
    assert effort == per_site_default, (
        f"TB-339: `or`-chain should fall through to `agent_effort` "
        f"default {per_site_default!r}; got {effort!r}"
    )


@pytest.mark.parametrize(
    "flat, key, per_site_default",
    _TB339_MIGRATED_KEYS,
)
def test_or_chain_uses_agent_effort_when_per_site_empty(
    cfg, clean_env, emit_reset, flat, key, per_site_default,
):
    """When `agent_effort` IS set (globally) but the per-site knob isn't,
    the `or`-chain picks up the global. Pins the precedence layer
    between the per-site default ("") and the global cfg-read default.
    """
    clean_env.setenv("AP2_AGENT_EFFORT", "xhigh")
    per_site = cfg.get_core_value(key, default="")
    assert per_site == "", (
        f"TB-339: per-site `{key}` should remain `\"\"` when only "
        f"`AP2_AGENT_EFFORT` is set; got {per_site!r}"
    )
    effort = per_site or cfg.get_core_value(
        "agent_effort", default=per_site_default,
    )
    assert effort == "xhigh", (
        f"TB-339: `or`-chain should pick up global `agent_effort=xhigh` "
        f"when per-site `{key}` is empty; got {effort!r}"
    )


@pytest.mark.parametrize(
    "flat, key, per_site_default",
    _TB339_MIGRATED_KEYS,
)
def test_or_chain_per_site_wins_over_agent_effort(
    cfg, clean_env, emit_reset, flat, key, per_site_default,
):
    """When BOTH per-site and global are set, the per-site value wins —
    the very precedence the original `os.environ.get(<flat>,
    cfg.get_core_value("agent_effort", ...))` shape preserved.
    """
    clean_env.setenv("AP2_AGENT_EFFORT", "xhigh")
    clean_env.setenv(flat, "low")
    per_site = cfg.get_core_value(key, default="")
    assert per_site == "low"
    effort = per_site or cfg.get_core_value(
        "agent_effort", default=per_site_default,
    )
    assert effort == "low", (
        f"TB-339: per-site `{flat}=low` should win over global "
        f"`AP2_AGENT_EFFORT=xhigh`; got {effort!r}"
    )
