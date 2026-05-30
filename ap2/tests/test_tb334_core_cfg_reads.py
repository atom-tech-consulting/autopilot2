"""TB-334: core-cluster agent-runtime knob reads via `cfg.get_core_value`
(axis-5 core cluster migration).

Sibling to the per-component cluster pilots (TB-326..331) and the
cross-package consumer sweeps (TB-332 auto_approve, TB-333
auto_unfreeze + validator_judge). Where those covered `components.<name>.*`
reads via `Config.get_component_value`, this TB closes the analogous
`[core.*]` gap: the ~10 agent-runtime tunables
(``AP2_AGENT_MODEL``, ``AP2_AGENT_EFFORT``, ``AP2_TASK_MAX_TURNS``,
``AP2_CONTROL_MAX_TURNS``, ``AP2_VERIFY_JUDGE_MAX_TURNS``) that fire
on every task / verify / janitor / status-report dispatch.

Post-TB-334 the four consumer files (``ap2/daemon.py``,
``ap2/verify.py``, ``ap2/status_report.py``, ``ap2/components/janitor/``)
no longer call ``os.environ.get("AP2_AGENT_*")`` /
``os.environ.get("AP2_TASK_MAX_TURNS"`` / etc. directly; they read
via the new ``Config.get_core_value(<key>, default=<x>)`` helper which
evaluates a call-time env-first precedence (sectioned env
``AP2_CORE_<KEY>`` > flat env via reverse-``FLAT_TO_SECTIONED`` >
``cfg.core_config`` TOML snapshot > default).

Five regression cleavages this pin holds (mirrors the TB-326 / TB-332
templates):

  (1) **Grep-shape**: zero remaining ``os.environ.get("AP2_AGENT_…"`` /
      ``os.environ.get("AP2_TASK_MAX_TURNS"`` /
      ``os.environ.get("AP2_CONTROL_MAX_TURNS"`` /
      ``os.environ.get("AP2_VERIFY_JUDGE_MAX_TURNS"`` call sites in
      the four migrated files. A refactor that re-introduces a direct
      env read in any of these files surfaces here instead of only via
      the briefing-level grep gate.
  (2) **Per-knob cfg-read parity (flat env)**: for each migrated knob,
      a ``monkeypatch.setenv(<flat>, …)`` value reaches
      ``cfg.get_core_value(<key>)`` identical to what the legacy
      ``os.environ.get(<flat>, default)`` shape would return.
  (3) **Per-knob cfg-read parity (sectioned env)**: same parity for
      the sectioned-env name ``AP2_CORE_<KEY>`` — the canonical
      naming under the TB-323 sectioned regime, which the helper
      consults first.
  (4) **TOML snapshot read**: a ``[core.<key>] = <value>`` TOML entry
      populates ``cfg.core_config`` (via the TB-334 extension to
      ``config_loader.from_toml``) and surfaces through the helper at
      the cfg-snapshot precedence layer.
  (5) **Helper presence + chosen-shape pin**: ``Config.get_core_value``
      exists with the documented option-2 signature (parallel to
      ``get_component_value``); ``FLAT_TO_SECTIONED`` carries the
      five agent-runtime mappings this TB depends on.

Out of scope (per the briefing): ideation cluster knobs
(``AP2_IDEATION_*`` — sibling TB-335), ``AP2_WEB_*``,
``AP2_AUTO_DIAGNOSE_*``, and the dataclass-attribute core knobs
(``AP2_TICK_S``, ``AP2_TASK_TIMEOUT_S``, ``AP2_VERIFY_CMD``,
``AP2_PROJECT_NAME``, &c.) that already flow through named
``Config`` fields.
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
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb334_core_cfg_reads.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# The four files the briefing's Verification grep covers. The janitor
# subpackage is a directory (everything under
# `ap2/components/janitor/` qualifies); the other three are single-
# file consumers.
_MIGRATED_FILES: tuple[str, ...] = (
    "ap2/daemon.py",
    "ap2/verify.py",
    "ap2/status_report.py",
    # TB-343: the janitor body (with its get_core_value calls) moved to impl.py.
    "ap2/components/janitor/impl.py",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically. Mirrors the TB-326 / TB-332 / TB-333
    cluster-pilot fixture shape so the per-cluster regression-pin
    files share the same setup vocabulary.
    """
    import os

    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Per-test cfg over a fresh project root with a stripped env
    surface. `init_project` scaffolds the schema-rendered TOML so
    `Config.load` lands on the TOML branch; `clean_env` runs FIRST so
    the project's own `.cc-autopilot/env` doesn't leak operator-tuned
    AP2_AGENT_* / AP2_TASK_MAX_TURNS / &c. values via
    `apply_env_overrides`.
    """
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
# (1) Grep-shape — zero remaining direct env reads in the migrated files.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    _MIGRATED_FILES,
)
def test_no_direct_agent_env_reads_in_migrated_files(rel_path):
    """Per-file grep-shape pin: no `os.environ.get("AP2_AGENT_…"` call
    sites in any of the four migrated files. Matches the briefing-level
    grep gate.

    Comments / docstrings that QUOTE the old call-site shape for
    historical context are allowed iff they don't form a literal call
    statement (the regex anchor `os\\.environ\\.get\\([\"']AP2_AGENT_`
    only matches the bare-call shape).
    """
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_AGENT_")
    src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    violations: list[str] = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if pattern.search(line):
            violations.append(f"L{lineno}: {line.strip()}")
    assert not violations, (
        f"TB-334: {rel_path} must read `AP2_AGENT_*` via "
        "`cfg.get_core_value(<key>, default=<x>)`, not via direct "
        "`os.environ.get('AP2_AGENT_…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_no_direct_task_max_turns_env_read_in_daemon():
    """`AP2_TASK_MAX_TURNS` no longer read directly in daemon.py."""
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_TASK_MAX_TURNS")
    src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    violations = [
        f"L{lineno}: {line.strip()}"
        for lineno, line in enumerate(src.splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not violations, (
        "TB-334: daemon.py must read `AP2_TASK_MAX_TURNS` via "
        "`cfg.get_core_value('task_max_turns', default=…)`. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_no_direct_control_max_turns_env_read_in_daemon():
    """`AP2_CONTROL_MAX_TURNS` no longer read directly in daemon.py."""
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_CONTROL_MAX_TURNS")
    src = (_REPO_ROOT / "ap2/daemon.py").read_text(encoding="utf-8")
    violations = [
        f"L{lineno}: {line.strip()}"
        for lineno, line in enumerate(src.splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not violations, (
        "TB-334: daemon.py must read `AP2_CONTROL_MAX_TURNS` via "
        "`cfg.get_core_value('control_max_turns', default=…)`. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_no_direct_verify_judge_max_turns_env_read_in_verify():
    """`AP2_VERIFY_JUDGE_MAX_TURNS` no longer read directly in verify.py."""
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_VERIFY_JUDGE_MAX_TURNS")
    src = (_REPO_ROOT / "ap2/verify.py").read_text(encoding="utf-8")
    violations = [
        f"L{lineno}: {line.strip()}"
        for lineno, line in enumerate(src.splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not violations, (
        "TB-334: verify.py must read `AP2_VERIFY_JUDGE_MAX_TURNS` via "
        "`cfg.get_core_value('verify_judge_max_turns', default=…)`. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_get_core_value_path_present_in_migrated_files():
    """Positive form of the grep-shape pins: each migrated file calls
    `cfg.get_core_value(...)` at least once. The briefing's secondary
    Verification bullet (`grep -rE "get_core_value\\("`) requires the
    resolved-config read path to be present in every migrated file.
    """
    pattern = re.compile(r"get_core_value\(")
    for rel_path in _MIGRATED_FILES:
        src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert pattern.search(src), (
            f"TB-334: {rel_path} should call `cfg.get_core_value(…)` "
            "for the TB-334 core-cluster migration."
        )


# ---------------------------------------------------------------------------
# (2) Per-knob cfg-read parity (flat env) — `cfg.get_core_value(<key>)`
#     returns the same value as `os.environ.get(<flat>, default)` would.
# ---------------------------------------------------------------------------


# (key, flat env name, sample value) tuples for the five migrated knobs.
_FLAT_PARITY_CASES = [
    ("agent_model", "AP2_AGENT_MODEL", "claude-sonnet-4-7"),
    ("agent_effort", "AP2_AGENT_EFFORT", "medium"),
    ("task_max_turns", "AP2_TASK_MAX_TURNS", "77"),
    ("control_max_turns", "AP2_CONTROL_MAX_TURNS", "22"),
    ("verify_judge_max_turns", "AP2_VERIFY_JUDGE_MAX_TURNS", "33"),
]


@pytest.mark.parametrize("key, flat, sample", _FLAT_PARITY_CASES)
def test_get_core_value_flat_env_parity(
    cfg, clean_env, emit_reset, key, flat, sample,
):
    """For each migrated agent-runtime knob, a
    `monkeypatch.setenv(<flat>, <sample>)` value reaches
    `cfg.get_core_value(<key>)` identical to what
    `os.environ.get(<flat>, default)` would have returned pre-TB-334.

    Drives the back-compat path the shell-export operator depends on
    via the helper's reverse-`FLAT_TO_SECTIONED` lookup.
    """
    clean_env.setenv(flat, sample)
    assert cfg.get_core_value(key, default="UNSET") == sample, (
        f"TB-334: flat env `{flat}={sample}` should resolve to {sample!r} "
        f"via `cfg.get_core_value({key!r})`."
    )


@pytest.mark.parametrize("key, flat, sample", _FLAT_PARITY_CASES)
def test_get_core_value_sectioned_env_parity(
    cfg, clean_env, emit_reset, key, flat, sample,
):
    """Same parity for the sectioned-env name `AP2_CORE_<KEY>` — the
    canonical naming under the TB-323 sectioned regime. The helper
    consults sectioned env FIRST so an operator who has migrated their
    env file to the new naming sees their value land.
    """
    sectioned = f"AP2_CORE_{key.upper()}"
    clean_env.setenv(sectioned, sample)
    assert cfg.get_core_value(key, default="UNSET") == sample, (
        f"TB-334: sectioned env `{sectioned}={sample}` should resolve to "
        f"{sample!r} via `cfg.get_core_value({key!r})`."
    )


@pytest.mark.parametrize("key, flat, sample", _FLAT_PARITY_CASES)
def test_get_core_value_sectioned_env_wins_over_flat_env(
    cfg, clean_env, emit_reset, key, flat, sample,
):
    """Sectioned env (`AP2_CORE_<KEY>`) wins over flat env (`AP2_<FLAT>`)
    — the head-of-list position the helper enforces at call time mirrors
    `_apply_sectioned_env_overrides`'s load-time precedence.
    """
    clean_env.setenv(flat, "FLAT-VAL")
    sectioned = f"AP2_CORE_{key.upper()}"
    clean_env.setenv(sectioned, sample)
    assert cfg.get_core_value(key, default="UNSET") == sample, (
        f"TB-334: sectioned env `{sectioned}` should win over flat "
        f"env `{flat}`; got "
        f"{cfg.get_core_value(key, default='UNSET')!r}, expected {sample!r}."
    )


# ---------------------------------------------------------------------------
# (3) Default-on-unset semantics — when no env / TOML carries a value, the
#     helper returns the caller's default. Preserves the pre-migration
#     `os.environ.get(name, default)` shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, default",
    [
        ("agent_model", "claude-opus-4-7"),
        ("agent_effort", "xhigh"),
        ("task_max_turns", 500),
        ("control_max_turns", 15),
        ("verify_judge_max_turns", 20),
    ],
)
def test_get_core_value_unset_returns_default(
    cfg, clean_env, emit_reset, key, default,
):
    """Unset env + empty TOML → caller's default. Bit-for-bit identical
    to the pre-migration `os.environ.get(<flat>, <default>)` return value.
    """
    assert cfg.get_core_value(key, default=default) == default


# ---------------------------------------------------------------------------
# (4) TOML snapshot read — `[core.<key>]` populates `cfg.core_config`
#     and surfaces through the helper at the cfg-snapshot layer.
# ---------------------------------------------------------------------------


def _load_toml_cfg(tmp_path, body: str) -> Config:
    """Helper that writes `body` to `.cc-autopilot/config.toml` and
    returns the corresponding `Config.load` result (TOML branch).
    """
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


def test_get_core_value_reads_from_toml_snapshot(tmp_path, clean_env, emit_reset):
    """A `[core.agent_model] = "foo"` TOML entry populates
    `cfg.core_config["agent_model"]`, which `cfg.get_core_value` reads
    at the cfg-snapshot precedence layer when no env override is live.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        '[core]\nagent_model = "claude-sonnet-toml"\n'
        'task_max_turns = 88\n',
    )
    # Snapshot landed.
    assert cfg.core_config["agent_model"] == "claude-sonnet-toml"
    assert cfg.core_config["task_max_turns"] == 88
    # Helper surfaces it.
    assert cfg.get_core_value("agent_model", default="X") == "claude-sonnet-toml"
    assert cfg.get_core_value("task_max_turns", default=999) == 88


def test_flat_env_wins_over_toml_snapshot(tmp_path, clean_env, emit_reset):
    """Precedence pin: flat env (back-compat layer) wins over the TOML
    snapshot — the operator who hasn't migrated their env file still
    sees their env value override the TOML default.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        '[core]\nagent_model = "claude-sonnet-toml"\n',
    )
    clean_env.setenv("AP2_AGENT_MODEL", "claude-flat-env")
    assert cfg.get_core_value("agent_model", default="X") == "claude-flat-env"


def test_sectioned_env_wins_over_toml_snapshot(tmp_path, clean_env, emit_reset):
    """Sectioned env beats the TOML snapshot too — the canonical
    sectioned naming is the highest-precedence runtime override.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        '[core]\nagent_model = "claude-sonnet-toml"\n',
    )
    clean_env.setenv("AP2_CORE_AGENT_MODEL", "claude-sectioned-env")
    assert cfg.get_core_value("agent_model", default="X") == "claude-sectioned-env"


# ---------------------------------------------------------------------------
# (5) Helper presence + chosen-shape pin — `Config.get_core_value` exists
#     with the documented signature; `FLAT_TO_SECTIONED` carries the
#     migrated knob mappings.
# ---------------------------------------------------------------------------


def test_get_core_value_helper_present_on_config():
    """`Config.get_core_value` must exist as a method (the briefing's
    primary deliverable) so the migrated read sites in daemon.py /
    verify.py / status_report.py / janitor resolve correctly.
    """
    assert hasattr(Config, "get_core_value"), (
        "TB-334: `Config.get_core_value` must be defined — the migrated "
        "call sites depend on it."
    )
    # Light shape check: the helper takes `key` positional and a
    # `default` keyword-only arg (parallel to `get_component_value`).
    import inspect
    sig = inspect.signature(Config.get_core_value)
    params = list(sig.parameters.values())
    # `self` + `key` + `default` (keyword-only).
    assert params[1].name == "key", params
    assert "default" in sig.parameters, sig
    assert sig.parameters["default"].kind == inspect.Parameter.KEYWORD_ONLY


def test_config_carries_core_config_field():
    """The `Config` dataclass must expose a `core_config` attribute
    (the TB-334 plumbing addition) for the helper's cfg-snapshot layer
    to consult. Defaults to empty dict on the env-path branch.
    """
    cfg = Config._load_env_path(_REPO_ROOT)
    assert isinstance(getattr(cfg, "core_config", None), dict), (
        "TB-334: `Config.core_config` must be a dict (default empty) so "
        "the helper's cfg-snapshot layer reads safely on the env-path "
        "branch with no `config.toml`."
    )


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        ("AP2_AGENT_MODEL", "core.agent_model"),
        ("AP2_AGENT_EFFORT", "core.agent_effort"),
        ("AP2_TASK_MAX_TURNS", "core.task_max_turns"),
        ("AP2_CONTROL_MAX_TURNS", "core.control_max_turns"),
        ("AP2_VERIFY_JUDGE_MAX_TURNS", "core.verify_judge_max_turns"),
    ],
)
def test_flat_to_sectioned_pins_migrated_core_knobs(flat, sectioned):
    """`FLAT_TO_SECTIONED` (TB-323) must carry the five migrated
    agent-runtime knob mappings. A refactor that drops one would
    silently break the flat-env back-compat path for that knob; this
    pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-334: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the core-cluster reverse-lookup back-compat "
        f"path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )
