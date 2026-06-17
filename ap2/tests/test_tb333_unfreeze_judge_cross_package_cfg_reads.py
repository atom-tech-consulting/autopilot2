"""TB-333: cross-package `auto_unfreeze` + `validator_judge` knob reads
via `cfg.get_component_value` (axis-5 cross-package consumer migration).

Sibling to TB-332's auto_approve cross-package sweep and to the
component-body pilots (TB-327 auto_unfreeze, TB-331 validator_judge).
TB-327 closed the auto_unfreeze knob reads inside
`ap2/components/auto_unfreeze/`; TB-331 closed the validator_judge
knob reads inside `ap2/components/validator_judge/`. The cross-package
consumers (`ap2/automation_status.py`, `ap2/doctor.py`) still held
~7 hand-rolled `os.environ.get(AP2_AUTO_UNFREEZE_…)` /
`os.environ.get(AP2_VALIDATOR_JUDGE_…)` reads. Post-TB-333 they read via
`Config.get_component_value("auto_unfreeze"|"validator_judge", <key>)`
which evaluates sectioned-env > flat-env > cfg snapshot > default at
call-time and keeps `monkeypatch.setenv(...)` + flat-env back-compat
working for the shell-export operator.

Five regression cleavages this pin holds (mirrors the TB-332 template):

  (1) **Grep-shape**: zero remaining `os.environ.get("AP2_AUTO_UNFREEZE_"` /
      `os.environ.get("AP2_VALIDATOR_JUDGE_"` call sites in the four
      cross-package files (`ap2/automation_status.py`, `ap2/doctor.py`,
      `ap2/_shared.py`, `ap2/briefing_validators.py`). A refactor that
      re-introduces a direct env read in any of these files surfaces
      here instead of only via the briefing-level grep gate.
  (2) **Per-knob cfg-read parity**: for each migrated knob, a
      `monkeypatch.setenv(...)` value reaches the cfg-based helper
      identical to what the legacy env-read shape would return.
  (3) **Default-None back-compat**: each helper that gained a
      `cfg: Config | None = None` kwarg preserves the legacy env-read
      shape when called with `cfg=None` (pre-TB-333 callers — TB-227's
      `test_is_auto_unfreeze_dry_run_helper_directly`, TB-239's
      `auto_unfreeze_audit()` direct calls, TB-243's
      `test_validator_judge_noisy_threshold_parse` — see bit-for-bit
      identical behavior).
  (4) **TypeError-on-non-Config**: passing a non-Config positional
      raises TypeError (closes the "stray positional silently masked
      as cfg" hazard the TB-327 design pattern names).
  (5) **FLAT_TO_SECTIONED entries pinned**: the nine migrated knob
      entries in `FLAT_TO_SECTIONED` (5 auto_unfreeze + 4 validator_judge
      consumed by the cross-package consumers; AP2_VALIDATOR_JUDGE_MAX_
      TOKENS is the TB-249 deprecated alias consumed only inside the
      component subpackage and isn't a cross-package concern) are the
      contract the helper's reverse-lookup back-compat depends on.

Conftest exemption (briefing § Out of scope): `ap2/tests/conftest.py`
still reads `AP2_VALIDATOR_JUDGE_DISABLED` directly because the
test-suite-wide shield runs at conftest import time, before pytest has
constructed any project's `Config`. The conftest.py call site is
deliberately excluded from the grep-shape pin below; the comment block
above the shield (TB-333) documents the exemption rationale at source.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2 import automation_status, doctor
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The four files the briefing's Verification grep covers. Notably
# excludes `ap2/tests/conftest.py` — see module docstring's
# "Conftest exemption" note for the rationale (the shield runs at
# import time, before any project Config exists).
_CROSS_PACKAGE_FILES: tuple[str, ...] = (
    "ap2/automation_status.py",
    "ap2/doctor.py",
    "ap2/_shared.py",
    "ap2/briefing_validators.py",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically. Mirrors the TB-326 / TB-327 / TB-332
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
    the project's own `.cc-autopilot/env` doesn't leak the operator-
    tuned AP2_AUTO_UNFREEZE_* / AP2_VALIDATOR_JUDGE_* values via
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
# (1) Grep-shape — zero remaining `os.environ.get("AP2_AUTO_UNFREEZE_…"` /
#     `os.environ.get("AP2_VALIDATOR_JUDGE_…"` call sites in the four
#     cross-package files. The pattern matches
#     `os.environ.get("AP2_AUTO_UNFREEZE` / `os.environ.get("AP2_VALIDATOR_JUDGE`
#     (any quote, any suffix), so the legacy `os.getenv(...)` fallback shape
#     that the TB-333 design uses is NOT a violation. Comments / docstrings
#     citing the old call-site shape for historical context are allowed
#     iff they don't form a literal call statement (the pattern's
#     `os\.environ\.get\([\"']AP2_...` anchor only matches the bare-call
#     shape).
# ---------------------------------------------------------------------------


def test_no_direct_auto_unfreeze_env_reads_in_cross_package_files():
    """The briefing's primary Verification bullet pinned to source.

    Each of the four cross-package files must read `AP2_AUTO_UNFREEZE_*`
    values via `cfg.get_component_value("auto_unfreeze", <key>)` (or
    the legacy `os.getenv` fallback on the default-None back-compat
    branch), NOT via direct `os.environ.get("AP2_AUTO_UNFREEZE_…")`.

    A refactor that re-introduces a direct `os.environ.get(...)` read
    surfaces here instead of only via the briefing-level grep gate.
    """
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_AUTO_UNFREEZE_")
    violations: list[str] = []
    for rel in _CROSS_PACKAGE_FILES:
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-333: the cross-package consumers must read "
        "`AP2_AUTO_UNFREEZE_*` via "
        "`cfg.get_component_value('auto_unfreeze', <key>)`, not via "
        "direct `os.environ.get('AP2_AUTO_UNFREEZE_…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_no_direct_validator_judge_env_reads_in_cross_package_files():
    """Sibling pin for the validator_judge cluster. Same shape as the
    auto_unfreeze grep above; both share the TB-333 cross-package
    consumer scope.
    """
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_VALIDATOR_JUDGE_")
    violations: list[str] = []
    for rel in _CROSS_PACKAGE_FILES:
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-333: the cross-package consumers must read "
        "`AP2_VALIDATOR_JUDGE_*` via "
        "`cfg.get_component_value('validator_judge', <key>)`, not via "
        "direct `os.environ.get('AP2_VALIDATOR_JUDGE_…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_cfg_get_component_value_path_present_in_primary_consumers():
    """Positive form of the grep-shape pins: the two primary
    cross-package consumers (`automation_status.py`, `doctor.py`) call
    `cfg.get_component_value("auto_unfreeze", ...)` AND
    `cfg.get_component_value("validator_judge", ...)` at least once
    each. The briefing's secondary Verification bullets
    (`grep -rE "get_component_value\\(.auto_unfreeze."` and
    `... .validator_judge."`) require the resolved-config read path
    to be present in each.
    """
    # Pattern allows the call's first argument to land on the same
    # line OR on the next line — `doctor.auto_unfreeze_audit` wraps
    # the call across lines for the four-knob block, while
    # `automation_status` inlines on one line. Both are valid; the
    # grep just asserts the resolved-config read path is present.
    pattern_unfreeze = re.compile(
        r"get_component_value\(\s*[\"']auto_unfreeze[\"']",
    )
    pattern_judge = re.compile(
        r"get_component_value\(\s*[\"']validator_judge[\"']",
    )
    for rel in ("ap2/automation_status.py", "ap2/doctor.py"):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert pattern_unfreeze.search(src), (
            f"TB-333: {rel} should call "
            "`cfg.get_component_value(\"auto_unfreeze\", <key>)` for "
            "the TB-333 cross-package migration."
        )
        assert pattern_judge.search(src), (
            f"TB-333: {rel} should call "
            "`cfg.get_component_value(\"validator_judge\", <key>)` for "
            "the TB-333 cross-package migration."
        )


# ---------------------------------------------------------------------------
# (2) Per-knob cfg-read parity — for each migrated knob, a
#     `monkeypatch.setenv(...)` value reaches the cfg-based helper
#     identical to what the legacy env-read shape would return.
# ---------------------------------------------------------------------------


def test_is_auto_unfreeze_dry_run_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`automation_status._is_auto_unfreeze_dry_run(cfg)` returns True
    iff the env value is truthy — same shape the legacy env-only
    helper returned for the same value. Exercises the cfg-read path
    via the flat-env back-compat reverse-lookup.
    """
    for val in ("1", "true", "yes"):
        clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", val)
        assert automation_status._is_auto_unfreeze_dry_run(cfg) is True, val
    for val in ("0", "false", "no", ""):
        clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", val)
        assert automation_status._is_auto_unfreeze_dry_run(cfg) is False, val


def test_validator_judge_noisy_threshold_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`automation_status.validator_judge_noisy_threshold(cfg)` parses
    the flat-env int value identical to the pre-TB-333 helper.
    Defaults to 5 on unset / non-int / non-positive; honors operator-
    set positive values.
    """
    clean_env.delenv(
        "AP2_COMPONENTS_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False,
    )
    assert automation_status.validator_judge_noisy_threshold(cfg) == 5
    clean_env.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_NOISY_THRESHOLD", "7")
    assert automation_status.validator_judge_noisy_threshold(cfg) == 7
    clean_env.setenv(
        "AP2_COMPONENTS_VALIDATOR_JUDGE_NOISY_THRESHOLD", "not-a-number",
    )
    assert automation_status.validator_judge_noisy_threshold(cfg) == 5
    clean_env.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_NOISY_THRESHOLD", "0")
    assert automation_status.validator_judge_noisy_threshold(cfg) == 5
    clean_env.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_NOISY_THRESHOLD", "-3")
    assert automation_status.validator_judge_noisy_threshold(cfg) == 5


def test_collect_auto_approve_state_threads_cfg_through_unfreeze_dry_run(
    cfg, clean_env, emit_reset,
):
    """`automation_status.collect_auto_approve_state(cfg)` surfaces
    `auto_unfreeze_dry_run_enabled` via the cfg-routed helper so a
    `monkeypatch.setenv(AP2_AUTO_UNFREEZE_DRY_RUN=1)` flips the state
    key end-to-end. Drives the `ap2 status` text/JSON + web home
    automation card.
    """
    clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", "1")
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_unfreeze_dry_run_enabled"] is True

    clean_env.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", raising=False)
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_unfreeze_dry_run_enabled"] is False


def test_doctor_auto_unfreeze_audit_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`doctor.auto_unfreeze_audit(cfg)` reads via cfg. With both
    knobs unset → single INFO "disabled (allowlist unset)" line; with
    dry-run set + allowlist unset → WARN "silent no-op" line; with
    allowlist non-empty → INFO summary naming shape count + caps.
    Same shape the TB-239 env-only tests pin on
    `auto_unfreeze_audit()`.
    """
    # Branch 1: default-off → INFO only.
    res = doctor.auto_unfreeze_audit(cfg)
    levels = [lvl for lvl, _ in res.messages]
    assert levels == ["INFO"]
    assert "disabled" in res.messages[0][1].lower()

    # Branch 2: dry-run armed without allowlist → WARN.
    clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DRY_RUN", "1")
    res = doctor.auto_unfreeze_audit(cfg)
    levels = [lvl for lvl, _ in res.messages]
    assert levels.count("WARN") == 1, levels

    # Branch 3: dry-run armed WITH allowlist → INFO summary.
    clean_env.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    res = doctor.auto_unfreeze_audit(cfg)
    levels = [lvl for lvl, _ in res.messages]
    assert levels == ["INFO"], levels
    msg = res.messages[0][1]
    assert "dry-run armed" in msg
    assert "1 shapes" in msg


def test_doctor_auto_unfreeze_audit_honors_custom_caps_via_cfg(
    cfg, clean_env, emit_reset,
):
    """`AP2_AUTO_UNFREEZE_MAX_PER_TASK` / `AP2_AUTO_UNFREEZE_MAX_PER_DAY`
    surface in the INFO summary when set, mirroring the TB-239 env-only
    contract.
    """
    clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "shape_a")
    clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "5")
    clean_env.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "10")
    res = doctor.auto_unfreeze_audit(cfg)
    msg = res.messages[0][1]
    assert "per-task cap 5" in msg
    assert "per-day cap 10" in msg


def test_doctor_validator_judge_timeout_audit_cfg_reads_match_env(
    cfg, clean_env, emit_reset, tmp_path,
):
    """`doctor.validator_judge_timeout_audit(state_dir, cfg)` resolves
    `AP2_VALIDATOR_JUDGE_TIMEOUT_S` via cfg. With <3 sample events the
    INFO "insufficient data" branch fires regardless of timeout value
    (the parser still runs; we exercise the value-flow through cfg via
    the message body, which echoes the env knob name).
    """
    clean_env.setenv("AP2_COMPONENTS_VALIDATOR_JUDGE_TIMEOUT_S", "60")
    # With no validator_judge_passed events seeded, the audit short-
    # circuits to INFO "insufficient data" — that confirms the cfg
    # path didn't trip an exception during parse.
    res = doctor.validator_judge_timeout_audit(tmp_path, cfg)
    levels = [lvl for lvl, _ in res.messages]
    assert "WARN" not in levels, levels
    assert levels == ["INFO"]
    assert "AP2_VALIDATOR_JUDGE_TIMEOUT_S" in res.messages[0][1]


# ---------------------------------------------------------------------------
# (3) Default-None back-compat — each helper preserves the legacy
#     env-read shape when called with `cfg=None`. Pre-TB-333 callers
#     see bit-for-bit identical behavior.
# ---------------------------------------------------------------------------


def test_is_auto_unfreeze_dry_run_default_none_falls_back_to_env(clean_env):
    """`_is_auto_unfreeze_dry_run()` (no cfg) reads
    `AP2_AUTO_UNFREEZE_DRY_RUN` via the env-fallback branch. Pins the
    pre-TB-333 contract the TB-227
    `test_is_auto_unfreeze_dry_run_helper_directly` test relies on.
    """
    clean_env.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    assert automation_status._is_auto_unfreeze_dry_run() is False
    clean_env.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    assert automation_status._is_auto_unfreeze_dry_run() is True
    clean_env.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "no")
    assert automation_status._is_auto_unfreeze_dry_run() is False


def test_validator_judge_noisy_threshold_default_none_falls_back_to_env(
    clean_env,
):
    """`validator_judge_noisy_threshold()` (no cfg) reads
    `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` via the env-fallback branch.
    Pins the TB-243 `test_validator_judge_noisy_threshold_parse` and
    TB-288 `test_threshold_override_lowers_trip_point` contracts.
    """
    clean_env.delenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", raising=False)
    assert automation_status.validator_judge_noisy_threshold() == 5
    clean_env.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "1")
    assert automation_status.validator_judge_noisy_threshold() == 1
    clean_env.setenv("AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD", "garbage")
    assert automation_status.validator_judge_noisy_threshold() == 5


def test_doctor_auto_unfreeze_audit_default_none_falls_back_to_env(clean_env):
    """`doctor.auto_unfreeze_audit()` (no cfg) reads env directly via
    the back-compat branch. Pins the TB-239 contract — every test in
    `test_tb239_doctor_auto_unfreeze_audit.py` calls without cfg.
    """
    clean_env.delenv("AP2_AUTO_UNFREEZE_FIX_SHAPES", raising=False)
    clean_env.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    res = doctor.auto_unfreeze_audit()
    assert [lvl for lvl, _ in res.messages] == ["INFO"]

    clean_env.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    res = doctor.auto_unfreeze_audit()
    levels = [lvl for lvl, _ in res.messages]
    assert "WARN" in levels


# ---------------------------------------------------------------------------
# (4) TypeError-on-non-Config — each migrated helper raises TypeError
#     when called with a non-Config positional value. Closes the
#     "stray positional silently masked as cfg" hazard the briefing's
#     design explicitly names.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper",
    [
        automation_status._is_auto_unfreeze_dry_run,
        automation_status.validator_judge_noisy_threshold,
        doctor.auto_unfreeze_audit,
    ],
)
def test_helper_rejects_non_config_positional(helper):
    """Each helper that grew a `cfg: Config | None = None` kwarg must
    raise TypeError when called with a non-Config positional value.
    Closes the hazard the TB-327 design pattern names: a stray
    positional from a pre-migration caller would otherwise pass
    silently through as `cfg`.
    """
    with pytest.raises(TypeError):
        helper("not-a-config")


# ---------------------------------------------------------------------------
# (5) FLAT_TO_SECTIONED entries pinned — the migrated knobs are
#     listed in the back-compat map. A refactor that drops any entry
#     would silently break the cfg-based helper's reverse-lookup.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        # auto_unfreeze cluster (5 knobs).
        ("AP2_AUTO_UNFREEZE_DISABLED", "components.auto_unfreeze.disabled"),
        (
            "AP2_AUTO_UNFREEZE_FIX_SHAPES",
            "components.auto_unfreeze.fix_shapes",
        ),
        ("AP2_AUTO_UNFREEZE_DRY_RUN", "components.auto_unfreeze.dry_run"),
        (
            "AP2_AUTO_UNFREEZE_MAX_PER_TASK",
            "components.auto_unfreeze.max_per_task",
        ),
        (
            "AP2_AUTO_UNFREEZE_MAX_PER_DAY",
            "components.auto_unfreeze.max_per_day",
        ),
        # validator_judge cluster (4 knobs consumed cross-package; the
        # 5th `max_tokens` mapping is the TB-249 deprecated alias and
        # is consumed only inside the component subpackage).
        (
            "AP2_VALIDATOR_JUDGE_DISABLED",
            "components.validator_judge.disabled",
        ),
        (
            "AP2_VALIDATOR_JUDGE_TIMEOUT_S",
            "components.validator_judge.timeout_s",
        ),
        (
            "AP2_VALIDATOR_JUDGE_MAX_TURNS",
            "components.validator_judge.max_turns",
        ),
        (
            "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
            "components.validator_judge.noisy_threshold",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_migrated_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops any of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-333: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the auto_unfreeze / validator_judge "
        f"cross-package reverse-lookup back-compat path; got "
        f"{FLAT_TO_SECTIONED.get(flat)!r}"
    )


# ---------------------------------------------------------------------------
# Conftest exemption sanity — the test-suite-wide
# `AP2_VALIDATOR_JUDGE_DISABLED` shield in `ap2/tests/conftest.py` is
# documented as an intentional cross-package exemption (the shield runs
# at conftest import time, before any project's `Config` is constructed).
# Pin that the conftest still carries the comment block documenting the
# carve-out so a future cleanup pass doesn't strip the rationale.
# ---------------------------------------------------------------------------


def test_conftest_documents_tb333_validator_judge_disabled_exemption():
    """`ap2/tests/conftest.py`'s `AP2_VALIDATOR_JUDGE_DISABLED` shield
    is the documented carve-out: read at conftest import time, before
    any `Config` exists, so the cfg-routed read path doesn't apply.
    The TB-333 comment block above the shield names the exemption; a
    cleanup pass that strips the rationale would surface here.
    """
    src = (_REPO_ROOT / "ap2/tests/conftest.py").read_text(encoding="utf-8")
    assert "TB-333" in src, (
        "TB-333: `ap2/tests/conftest.py` should carry the TB-333 "
        "exemption rationale for the `AP2_VALIDATOR_JUDGE_DISABLED` "
        "shield (the only intentional cross-package env read of the "
        "validator_judge cluster post-TB-333)."
    )
