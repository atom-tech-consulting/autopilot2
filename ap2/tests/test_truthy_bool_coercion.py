"""TB-428: config.toml bool coercion in component enablement gates.

A `config.toml` `enabled = true` (or `[core] ideation_disabled = true`) is
parsed by the TOML loader into Python's bool `True`, and
`cfg.get_component_value(...)` / `cfg.get_core_value(...)` hand that bool
back to the gate UNCHANGED. The pre-TB-428 gate copies stringified the
value first (`str(True)` → `"True"`) and then ran a lowercase-only
membership test (`raw.strip() in ("1", "true", "yes")`), so the capital-T
form silently fell out of the truthy set and the gate read `False` even
though the operator had set the documented key — the feature silently
no-oped.

TB-428 routes every component on/off gate through ONE canonical helper,
`ap2._shared.is_truthy`, which short-circuits a real bool and lowercases
string forms. This pins:

  - the `auto_approve` enable gate (`_is_auto_approve_enabled`) reads a
    TOML bool / string form correctly — TB-430 flipped it to the
    suppress-polarity `disabled` key (default-on / opt-out), so a truthy
    `disabled` value now reads as OFF;
  - the `ideation` disable gate (`_ideation_disabled`) does the same;
  - the canonical helper itself is bool-safe + case-insensitive while
    preserving the `1`/`true`/`yes` vocabulary and the unset→False default.
"""
from __future__ import annotations

import os

import pytest

from ap2._shared import is_truthy
from ap2.components.auto_approve.impl import _is_auto_approve_enabled
from ap2.components.ideation.impl import _ideation_disabled
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so the gate resolution lands on the
    config.toml snapshot tier deterministically (mirrors the TB-332
    cluster-pilot fixture shape)."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Per-test cfg over a fresh project root with a stripped env surface.

    `clean_env` runs FIRST so neither the process env nor the project's
    `.cc-autopilot/env` leaks an `AP2_AUTO_APPROVE*` / `AP2_*IDEATION*`
    value that would pre-empt the config.toml tier the gates resolve.
    """
    init_project(tmp_path)
    return Config.load(tmp_path)


# ---------------------------------------------------------------------------
# (1) auto_approve enable gate — the TB-428 headline defect.
# ---------------------------------------------------------------------------


def test_auto_approve_disabled_gate_reads_toml_bool_true(cfg):
    """`[components.auto_approve] disabled = true` parses to the Python bool
    `True`; the suppress-polarity gate (TB-430) must read that as DISABLED
    — the capital-T coercion path the lowercase-only membership test
    silently dropped pre-TB-428."""
    cfg.components_config = {"auto_approve": {"disabled": True}}
    assert _is_auto_approve_enabled(cfg) is False


def test_auto_approve_disabled_gate_reads_toml_bool_false(cfg):
    """The Python bool `False` (TOML `disabled = false`) reads as ENABLED
    (TB-430 default-on — opt-out not engaged)."""
    cfg.components_config = {"auto_approve": {"disabled": False}}
    assert _is_auto_approve_enabled(cfg) is True


@pytest.mark.parametrize("val", ["true", "True", "1", "yes"])
def test_auto_approve_disabled_gate_reads_string_truthy_case_insensitive(cfg, val):
    """String forms (incl. the capital-T `"True"` that the lowercase-only
    membership test silently dropped) all engage the suppress gate → OFF."""
    cfg.components_config = {"auto_approve": {"disabled": val}}
    assert _is_auto_approve_enabled(cfg) is False


# ---------------------------------------------------------------------------
# (2) ideation disable gate — same coercion defect at impl.py:184.
# ---------------------------------------------------------------------------


def test_ideation_disabled_gate_reads_toml_bool_true(cfg):
    """`[core] ideation_disabled = true` parses to the Python bool `True`;
    the gate must read that as disabled."""
    cfg.core_config = {"ideation_disabled": True}
    assert _ideation_disabled(cfg) is True


def test_ideation_disabled_gate_reads_capital_true_string(cfg):
    """The `str(True)` → `"True"` shape the pre-TB-428 case-sensitive
    membership test dropped now reads truthy."""
    cfg.core_config = {"ideation_disabled": "True"}
    assert _ideation_disabled(cfg) is True


def test_ideation_disabled_gate_reads_toml_bool_false(cfg):
    """The Python bool `False` reads as NOT disabled (gate off)."""
    cfg.core_config = {"ideation_disabled": False}
    assert _ideation_disabled(cfg) is False


# ---------------------------------------------------------------------------
# (3) the canonical helper — bool-safe + case-insensitive, unchanged
#     vocabulary + unset→False default.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [True, "true", "True", "TRUE", "1", "yes", "YES", " true "],
)
def test_canonical_is_truthy_accepts_truthy_forms(raw):
    assert is_truthy(raw) is True


@pytest.mark.parametrize(
    "raw",
    [False, "false", "False", "0", "no", "off", "", None, "maybe"],
)
def test_canonical_is_truthy_rejects_falsy_forms(raw):
    assert is_truthy(raw) is False
