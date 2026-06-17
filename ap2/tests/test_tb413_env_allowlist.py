"""TB-413: config.toml is the SOLE source for behavioral tunables.

`.cc-autopilot/config.toml` is the single source for behavioral tunables;
`.cc-autopilot/env` is reserved for a secrets + deployment-identity
allowlist (`config.ENV_PERMITTED_KEYS`). The flat `AP2_<knob>` override
path (the reverse-`FLAT_TO_SECTIONED` lookup) is removed for every
behavioral tunable: a flat tunable env name no longer overrides the TOML
value — it is IGNORED. Only the env-permitted (secret /
deployment-identity / runtime-fixed) flat names still win.

The pins below hold the new contract:

  (1) **Flat tunable env ignored** — a behavioral-tunable flat env var
      SET (e.g. `AP2_ATTENTION_IMMEDIATE_PUSH=1`) with the opposing value
      in `config.toml` resolves to the config.toml/schema value, NOT the
      env value. Covers both a component knob and a core knob.
  (2) **Allowlisted env still wins** — a deployment-identity / runtime-fixed
      flat name on `ENV_PERMITTED_KEYS` (`AP2_WEB_PORT`, `AP2_PROJECT_NAME`,
      `AP2_TICK_S`) still overrides, so the 12-factor escape hatch for the
      genuinely-per-deployment knobs is untouched.
  (3) **Allowlist structure** — `ENV_PERMITTED_KEYS` is an explicit
      frozenset; behavioral-tunable flat names are absent; the
      FLAT_TO_SECTIONED ∩ ENV_PERMITTED_KEYS members are exactly the five
      deployment-identity / runtime-fixed core knobs that keep their flat
      env.
  (4) **No env_deprecated for an ignored tunable** — the retired emission
      means a flat tunable env name produces no `env_deprecated` event.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ap2.config import ENV_PERMITTED_KEYS, Config
from ap2.config_compat import FLAT_TO_SECTIONED
from ap2.init import init_project


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically (the known env-knob verifier-leak failure
    mode). Tests set/unset via this monkeypatch so nothing leaks across
    tests."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _load_toml_cfg(tmp_path: Path, body: str) -> Config:
    """Scaffold a project at `tmp_path`, overwrite its config.toml with
    `body`, and return the `Config.load` result (TOML branch)."""
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


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


# ---------------------------------------------------------------------------
# (1) Flat tunable env is ignored — config.toml / schema wins.
# ---------------------------------------------------------------------------


def test_flat_component_tunable_env_is_ignored(tmp_path, clean_env):
    """The briefing's canonical case: `AP2_ATTENTION_IMMEDIATE_PUSH=1`
    SET with the opposing `[components.attention] immediate_push = false`
    in config.toml resolves to the config.toml value (False), NOT the env
    value — the flat tunable override is ignored."""
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.attention]\nimmediate_push = false\n",
    )
    clean_env.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "1")
    assert cfg.get_component_value("attention", "immediate_push") is False
    assert cfg.components_config["attention"]["immediate_push"] is False


def test_flat_core_tunable_env_is_ignored(tmp_path, clean_env):
    """A core behavioral tunable (`AP2_TASK_MAX_TURNS`) SET with the
    opposing `[core] task_max_turns = 222` in config.toml resolves to the
    config.toml value, NOT the env value."""
    cfg = _load_toml_cfg(tmp_path, "[core]\ntask_max_turns = 222\n")
    clean_env.setenv("AP2_TASK_MAX_TURNS", "999")
    assert cfg.get_core_value("task_max_turns") == 222


def test_flat_tunable_env_ignored_even_when_toml_omits_key(tmp_path, clean_env):
    """When config.toml omits the key entirely, a flat tunable env name
    still does NOT resolve through the helper — the schema default wins,
    not the env value (config.toml -> schema default are the only
    sources)."""
    cfg = _load_toml_cfg(tmp_path, "[core]\ntick_interval_s = 30\n")
    clean_env.setenv("AP2_TASK_MAX_TURNS", "999")
    from ap2.config import DEFAULT_TASK_MAX_TURNS

    assert cfg.get_core_value("task_max_turns") == DEFAULT_TASK_MAX_TURNS


# ---------------------------------------------------------------------------
# (2) Allowlisted env still wins (deployment-identity / runtime-fixed).
# ---------------------------------------------------------------------------


def test_allowlisted_web_port_env_still_wins(tmp_path, clean_env):
    """`AP2_WEB_PORT` is deployment-identity (on `ENV_PERMITTED_KEYS`), so
    its flat env still resolves through `get_core_value` even with a
    config.toml value present."""
    cfg = _load_toml_cfg(tmp_path, "[core]\nweb_port = 8000\n")
    clean_env.setenv("AP2_WEB_PORT", "9999")
    assert str(cfg.get_core_value("web_port")) == "9999"


def test_allowlisted_project_name_env_still_wins(tmp_path, clean_env):
    """`AP2_PROJECT_NAME` is deployment-identity, so its flat env still
    overrides the config.toml/default project name."""
    clean_env.setenv("AP2_PROJECT_NAME", "deploy-identity")
    cfg = _load_toml_cfg(tmp_path, "[core]\ntick_interval_s = 30\n")
    assert cfg.project_name == "deploy-identity"


# ---------------------------------------------------------------------------
# (3) Allowlist structure.
# ---------------------------------------------------------------------------


def test_env_permitted_keys_is_frozenset():
    assert isinstance(ENV_PERMITTED_KEYS, frozenset)
    assert ENV_PERMITTED_KEYS, "allowlist must be non-empty"


def test_behavioral_tunable_flat_names_absent_from_allowlist():
    """Representative behavioral-tunable flat names must NOT be on the
    allowlist — they resolve from config.toml only."""
    for tunable in (
        "AP2_ATTENTION_IMMEDIATE_PUSH",
        "AP2_TASK_MAX_TURNS",
        "AP2_AGENT_MODEL",
        "AP2_AUTO_APPROVE",
        "AP2_JANITOR_DISABLED",
        "AP2_IDEATION_DISABLED",
        "AP2_VALIDATOR_JUDGE_MAX_TURNS",
        "AP2_TASK_TIMEOUT_S",
    ):
        assert tunable not in ENV_PERMITTED_KEYS, (
            f"{tunable} is a behavioral tunable and must not be "
            f"env-permitted (config.toml is its sole source)"
        )


def test_flat_to_sectioned_allowlist_intersection_is_exactly_the_deployment_knobs():
    """The flat names that BOTH appear in FLAT_TO_SECTIONED AND remain
    env-permitted are exactly the five deployment-identity / runtime-fixed
    core knobs that keep their flat env. A future edit that adds a tunable
    to the allowlist (or drops a deployment knob) lights up here."""
    intersection = set(FLAT_TO_SECTIONED) & ENV_PERMITTED_KEYS
    assert intersection == {
        "AP2_TICK_S",
        "AP2_MM_TICK_S",
        "AP2_WEB_PORT",
        "AP2_WEB_DISABLED",
        "AP2_PROJECT_NAME",
    }


# ---------------------------------------------------------------------------
# (4) No env_deprecated emission for an ignored flat tunable.
# ---------------------------------------------------------------------------


def test_ignored_flat_tunable_emits_no_env_deprecated(tmp_path, clean_env):
    """A flat tunable env name no longer fires `env_deprecated` — the
    emission is retired (an ignored override has nothing to deprecate)."""
    clean_env.setenv("AP2_AUTO_APPROVE", "1")
    clean_env.setenv("AP2_TASK_MAX_TURNS", "999")
    cfg = _load_toml_cfg(tmp_path, "[core]\ntick_interval_s = 30\n")
    deprecations = [
        e for e in _read_events(cfg.events_file)
        if e.get("type") == "env_deprecated"
    ]
    assert deprecations == [], (
        f"flat tunable env must not emit env_deprecated (retired in "
        f"TB-413); got: {deprecations}"
    )
