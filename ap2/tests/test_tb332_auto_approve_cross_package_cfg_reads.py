"""TB-332: cross-package `auto_approve` knob reads via
`cfg.get_component_value` (axis-5 cross-package consumer migration).

Sibling to TB-326's component-body pilot: TB-326 closed the
component-internal call sites; TB-332 closes the ~10 cross-package
consumer call sites in `ap2/automation_status.py`, `ap2/board_edits.py`,
`ap2/operator_queue.py`, `ap2/doctor.py`, and `ap2/ideation.py`. These
files all read `AP2_AUTO_APPROVE*` env vars at call-time pre-TB-332;
post-TB-332 they read via `Config.get_component_value("auto_approve",
<key>)` which evaluates sectioned-env > flat-env > cfg snapshot >
default at call-time and keeps `monkeypatch.setenv(...)` + flat-env
back-compat working for the shell-export operator.

Five regression cleavages this pin holds (mirrors the TB-326 / TB-327 /
TB-328 / TB-330 / TB-331 cluster-pilot template):

  (1) **Grep-shape**: zero remaining `os.environ.get("AP2_AUTO_APPROVE…"`
      call sites in the six cross-package files. A refactor that
      re-introduces a direct env read in any of these files surfaces
      here instead of only via the briefing-level grep gate.
  (2) **Per-knob cfg-read parity**: for each migrated knob, a
      `monkeypatch.setenv(...)` value reaches the cfg-based helper
      identical to what the legacy env-read shape would return.
  (3) **Default-None back-compat**: each helper that gained a
      `cfg: Config | None = None` kwarg preserves the legacy env-read
      shape when called with `cfg=None` (pre-TB-332 callers see
      bit-for-bit identical behavior).
  (4) **TypeError-on-non-Config**: passing a non-Config positional
      raises TypeError (the briefing's design contract — closes the
      "stray positional silently masked as cfg" hazard).
  (5) **FLAT_TO_SECTIONED entries pinned**: the seven auto_approve
      knob entries in `FLAT_TO_SECTIONED` (per `config_compat.py`
      L117-124) are the contract the helper's reverse-lookup back-compat
      depends on; a refactor that drops one would silently break the
      flat-env path.
"""
from __future__ import annotations

import pathlib
import re

import pytest

# Import order matters: `board_edits` ↔ `operator_queue` ↔ `tools` form
# a partial-import resolution triangle that breaks if the test module
# pulls them in at module load. Pull `automation_status` / `doctor` /
# `ideation` (the migration-helper-owning modules) at module scope;
# defer `board_edits` / `operator_queue` to lazy imports inside the
# tests that need them (only the grep-shape pin reads their source
# text, which is filesystem-level — no Python import required).
from ap2 import automation_status, doctor, ideation  # noqa: F401
from ap2.components import auto_approve
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    _KNOBS_STAYING_ENV_ONLY,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The six files the briefing's Verification grep covers. cli_daemon.py is
# included even though it has no AP2_AUTO_APPROVE reads today — keeping
# it on the absence-check list pins it on the migrated side so a future
# read added there surfaces here.
_CROSS_PACKAGE_FILES: tuple[str, ...] = (
    "ap2/automation_status.py",
    "ap2/board_edits.py",
    "ap2/operator_queue.py",
    "ap2/doctor.py",
    "ap2/ideation.py",
    "ap2/cli_daemon.py",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically. Mirrors the TB-326 / TB-327 cluster-pilot
    fixture shape so the per-cluster regression-pin files share the
    same setup vocabulary.
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
    tuned AP2_AUTO_APPROVE_* values via `apply_env_overrides`.
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
# (1) Grep-shape — zero remaining `os.environ.get("AP2_AUTO_APPROVE…"`
#     call sites in the six cross-package files. The pattern matches
#     `os.environ.get("AP2_AUTO_APPROVE` (any quote, any AP2_AUTO_APPROVE
#     suffix), so the legacy `os.getenv(...)` fallback shape that the
#     TB-332 design uses is NOT a violation. Comments / docstrings
#     citing the old call-site shape for historical context are allowed
#     iff they don't form a literal call statement (the pattern's
#     `os\.environ\.get\([\"']AP2_AUTO_APPROVE` anchor only matches the
#     bare-call shape).
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_cross_package_files():
    """The briefing's primary Verification bullet pinned to source.

    Each of the six cross-package files (`automation_status.py`,
    `board_edits.py`, `operator_queue.py`, `doctor.py`, `ideation.py`,
    `cli_daemon.py`) must read `AP2_AUTO_APPROVE*` values via
    `cfg.get_component_value(...)` (or the legacy `os.getenv` fallback
    on the default-None back-compat branch — the briefing's design
    pattern), NOT via direct `os.environ.get("AP2_AUTO_APPROVE…")`.

    A refactor that re-introduces a direct `os.environ.get(...)` read
    surfaces here instead of only via the briefing-level grep gate.
    """
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_AUTO_APPROVE")
    violations: list[str] = []
    for rel in _CROSS_PACKAGE_FILES:
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-332: the cross-package consumers must read `AP2_AUTO_APPROVE*` "
        "via `cfg.get_component_value('auto_approve', <key>)`, not via "
        "direct `os.environ.get('AP2_AUTO_APPROVE…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_cfg_get_component_value_path_present_in_primary_consumers():
    """Positive form of the grep-shape pin: the primary cross-package
    consumers (`automation_status.py`, `operator_queue.py`) call
    `cfg.get_component_value("auto_approve", ...)` at least once.

    TB-383: `board_edits.py` was DROPPED from this list — its add_backlog
    path is now policy-free (it no longer evaluates the auto-approve gate
    nor reads the `AP2_AUTO_APPROVE` knob inline). The gate-chain read
    moved into the `auto_approve` component's loop pass + the gate
    helpers. `operator_queue.py` keeps its queue-drain gate (TB-293), so
    it remains a consumer.
    """
    for rel in (
        "ap2/automation_status.py",
        "ap2/operator_queue.py",
    ):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "get_component_value(\"auto_approve\"" in src, (
            f"TB-332: {rel} should call "
            "`cfg.get_component_value(\"auto_approve\", <key>)` for the "
            "TB-332 cross-package migration; the call path is the "
            "operator-facing back-compat surface (sectioned env > flat "
            "env > cfg snapshot > default)."
        )


# ---------------------------------------------------------------------------
# (2) Per-knob cfg-read parity — for each migrated knob, a
#     `monkeypatch.setenv(...)` value reaches the cfg-based helper
#     identical to what the legacy env-read shape would return.
# ---------------------------------------------------------------------------


def test_dry_run_helper_cfg_reads_match_env(cfg, clean_env, emit_reset):
    """`automation_status._is_auto_approve_dry_run(cfg)` returns True
    iff the env value is truthy — same shape the legacy env-only
    helper returned for the same value. Exercises the cfg-read path
    via the flat-env back-compat reverse-lookup.
    """
    for val in ("1", "true", "yes"):
        clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", val)
        assert automation_status._is_auto_approve_dry_run(cfg) is True, val
    for val in ("0", "false", "no", ""):
        clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", val)
        assert automation_status._is_auto_approve_dry_run(cfg) is False, val


def test_noisy_pause_disabled_helper_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`automation_status._is_validator_judge_noisy_pause_disabled(cfg)`
    returns True iff the env value is truthy. Same shape as the
    dry-run pin above; both helpers share the
    `cfg.get_component_value` + `_is_truthy` chain.
    """
    clean_env.delenv(
        "AP2_COMPONENTS_AUTO_APPROVE_NOISY_PAUSE_DISABLED", raising=False,
    )
    assert automation_status._is_validator_judge_noisy_pause_disabled(cfg) is False
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_NOISY_PAUSE_DISABLED", "1")
    assert automation_status._is_validator_judge_noisy_pause_disabled(cfg) is True


def test_freeze_threshold_helper_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`automation_status._freeze_threshold(cfg)` parses the flat-env
    int value identical to the pre-TB-332 helper. Defaults to 3 on
    unset / non-int; honors operator-set non-positive values.
    """
    clean_env.delenv(
        "AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False,
    )
    assert automation_status._freeze_threshold(cfg) == 3
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "5")
    assert automation_status._freeze_threshold(cfg) == 5
    clean_env.setenv(
        "AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "not-a-number",
    )
    assert automation_status._freeze_threshold(cfg) == 3


def test_positive_int_cap_helper_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`automation_status._positive_int_cap(env_name, cfg)` reads via
    `cfg.get_component_value(...)` for the two known auto_approve cap
    flat-names (`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` /
    `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`). Returns `None` (not `0`) on
    unset / non-positive / non-int — the "cap disabled" distinction
    operator-facing surfaces depend on.
    """
    for env_name in (
        "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
        "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP",
    ):
        # `_positive_int_cap` still takes the FLAT env name as its first
        # positional (it maps that name to the component key internally
        # and reads via `cfg.get_component_value`). TB-413 removed the
        # flat-env OVERRIDE, so inject via the SECTIONED env name (which
        # still overrides) while keeping the flat `env_name` argument.
        sectioned = "AP2_COMPONENTS_AUTO_APPROVE_" + env_name[
            len("AP2_AUTO_APPROVE_"):
        ]
        clean_env.delenv(sectioned, raising=False)
        assert automation_status._positive_int_cap(env_name, cfg) is None
        clean_env.setenv(sectioned, "42000")
        assert automation_status._positive_int_cap(env_name, cfg) == 42000
        clean_env.setenv(sectioned, "0")
        assert automation_status._positive_int_cap(env_name, cfg) is None
        clean_env.setenv(sectioned, "not-an-int")
        assert automation_status._positive_int_cap(env_name, cfg) is None


def test_ideation_should_auto_approve_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`auto_approve.should_auto_approve(tags, cfg)` honors both the master
    switch (TB-430: the suppress-polarity `AP2_AUTO_APPROVE_DISABLED`) and
    the per-shape gate (`AP2_AUTO_APPROVE_GATE_TAGS`) read via cfg. Same
    gate logic as the pre-TB-332 env-only helper, post-flip polarity.
    """
    # Opted OUT (suppress-polarity disable) → False regardless of tags.
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", "1")
    assert auto_approve.should_auto_approve(["#anything"], cfg) is False
    # Default-ON (opt-out cleared), default gate tags → #breaking-change blocks.
    clean_env.delenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", raising=False)
    clean_env.delenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", raising=False)
    assert auto_approve.should_auto_approve(["#breaking-change"], cfg) is False
    assert auto_approve.should_auto_approve(["#autopilot"], cfg) is True
    # Operator-customized gate tags via sectioned env.
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_GATE_TAGS", "#__never__")
    assert auto_approve.should_auto_approve(["#breaking-change"], cfg) is True


def test_collect_auto_approve_state_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`automation_status.collect_auto_approve_state(cfg)` surfaces
    operator-set knobs through the cfg layer end-to-end. Drives the
    `ap2 status` text/JSON + web home automation card.
    """
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_FREEZE_THRESHOLD", "7")
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "150000")
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_WINDOW_TOKEN_CAP", "1000000")
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DRY_RUN", "1")
    state = automation_status.collect_auto_approve_state(cfg)
    assert state["auto_approve_enabled"] is True
    assert state["freeze_threshold"] == 7
    assert state["per_task_token_cap"] == 150000
    assert state["window_token_cap"] == 1000000
    assert state["dry_run_enabled"] is True


def test_doctor_auto_approve_audit_cfg_reads_match_env(
    cfg, clean_env, emit_reset,
):
    """`doctor.auto_approve_audit(cfg)` reads via cfg. TB-430: auto-approve
    is default-ON, so with no caps configured → 3 WARN lines; opting OUT
    via `AP2_COMPONENTS_AUTO_APPROVE_DISABLED` → INFO-only. Same shape the
    TB-234 env-only tests pin on `auto_approve_audit()`, post-flip polarity.
    """
    # Opted OUT → INFO-only.
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", "1")
    res = doctor.auto_approve_audit(cfg)
    assert [lvl for lvl, _ in res.messages] == ["INFO"]

    # Default-ON + no caps → 3 WARN lines.
    clean_env.delenv("AP2_COMPONENTS_AUTO_APPROVE_DISABLED", raising=False)
    res = doctor.auto_approve_audit(cfg)
    levels = [lvl for lvl, _ in res.messages]
    assert levels.count("WARN") == 3, levels


# ---------------------------------------------------------------------------
# (3) Default-None back-compat — each helper preserves the legacy
#     env-read shape when called with `cfg=None`. Pre-TB-332 callers
#     (e.g. TB-232's `test_is_auto_approve_dry_run_helper_directly`)
#     see bit-for-bit identical behavior.
# ---------------------------------------------------------------------------


def test_dry_run_helper_default_none_falls_back_to_env(clean_env):
    """`_is_auto_approve_dry_run()` (no cfg) reads
    `AP2_AUTO_APPROVE_DRY_RUN` via the env-fallback branch. Pins the
    pre-TB-332 contract the TB-232 helper-direct test relies on.
    """
    clean_env.delenv("AP2_AUTO_APPROVE_DRY_RUN", raising=False)
    assert automation_status._is_auto_approve_dry_run() is False
    clean_env.setenv("AP2_AUTO_APPROVE_DRY_RUN", "1")
    assert automation_status._is_auto_approve_dry_run() is True


def test_freeze_threshold_default_none_falls_back_to_env(clean_env):
    """`_freeze_threshold()` (no cfg) reads
    `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` via the env-fallback branch.
    """
    clean_env.delenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)
    assert automation_status._freeze_threshold() == 3
    clean_env.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "9")
    assert automation_status._freeze_threshold() == 9


def test_positive_int_cap_default_none_falls_back_to_env(clean_env):
    """`_positive_int_cap(env_name)` (no cfg) reads the env name
    directly via the back-compat branch. Mirror of the TB-234
    parser-shape unit test.
    """
    clean_env.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    assert automation_status._positive_int_cap(
        "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
    ) is None
    clean_env.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "100000")
    assert automation_status._positive_int_cap(
        "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
    ) == 100000


def test_ideation_should_auto_approve_default_none_falls_back_to_env(clean_env):
    """`auto_approve.should_auto_approve(tags)` (no cfg) reads the env
    layer directly via the registry's env tiers + `AP2_AUTO_APPROVE_GATE_TAGS`.
    TB-430: auto-approve is default-ON when no opt-out env is set; the
    legacy require-polarity `AP2_AUTO_APPROVE=0` and the new suppress-
    polarity `AP2_AUTO_APPROVE_DISABLED=1` both opt OUT.
    """
    clean_env.delenv("AP2_AUTO_APPROVE", raising=False)
    clean_env.delenv("AP2_AUTO_APPROVE_DISABLED", raising=False)
    clean_env.delenv("AP2_AUTO_APPROVE_GATE_TAGS", raising=False)
    # Default-ON: auto-approve unless a gate-tag hits.
    assert auto_approve.should_auto_approve(["#autopilot"]) is True
    assert auto_approve.should_auto_approve(None) is True
    assert auto_approve.should_auto_approve(["#breaking-change"]) is False
    # Legacy require-polarity opt-out (transitional back-compat).
    clean_env.setenv("AP2_AUTO_APPROVE", "0")
    assert auto_approve.should_auto_approve(["#autopilot"]) is False
    clean_env.delenv("AP2_AUTO_APPROVE", raising=False)
    # New suppress-polarity opt-out.
    clean_env.setenv("AP2_AUTO_APPROVE_DISABLED", "1")
    assert auto_approve.should_auto_approve(["#autopilot"]) is False


def test_doctor_auto_approve_audit_default_none_falls_back_to_env(clean_env):
    """`doctor.auto_approve_audit()` (no cfg) reads env directly via the
    registry's env tiers. TB-430: auto-approve is default-ON, so opting
    OUT via the suppress-polarity `AP2_AUTO_APPROVE_DISABLED=1` yields the
    INFO-only line. Pins the TB-234 contract, post-flip polarity.
    """
    clean_env.delenv("AP2_AUTO_APPROVE", raising=False)
    clean_env.setenv("AP2_AUTO_APPROVE_DISABLED", "1")
    res = doctor.auto_approve_audit()
    assert [lvl for lvl, _ in res.messages] == ["INFO"]


# ---------------------------------------------------------------------------
# (4) TypeError-on-non-Config — each migrated helper raises TypeError
#     when called with a non-Config positional value. Closes the
#     "stray positional silently masked as cfg" hazard the briefing's
#     design explicitly names.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper, args",
    [
        (automation_status._is_auto_approve_dry_run, ()),
        (automation_status._is_validator_judge_noisy_pause_disabled, ()),
        (automation_status._freeze_threshold, ()),
        (auto_approve._is_auto_approve_enabled, ()),
        (auto_approve._auto_approve_gate_tags, ()),
        (doctor.auto_approve_audit, ()),
    ],
)
def test_helper_rejects_non_config_positional(helper, args):
    """Each helper that grew a `cfg: Config | None = None` kwarg must
    raise TypeError when called with a non-Config positional value.
    Closes the hazard the TB-327 design pattern names: a stray
    positional from a pre-migration caller would otherwise pass
    silently through as `cfg`.
    """
    with pytest.raises(TypeError):
        helper(*args, "not-a-config")


def test_positive_int_cap_rejects_non_config_positional():
    """`_positive_int_cap(env_name, cfg)` takes a required first
    positional `env_name`; the `cfg=` kwarg is second. Pin
    explicitly because the parametrize fixture above would call
    `_positive_int_cap("not-a-config")` which misinterprets the arg
    as the env_name.
    """
    with pytest.raises(TypeError):
        automation_status._positive_int_cap(
            "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "not-a-config",
        )


def test_should_auto_approve_rejects_non_config_positional():
    """`should_auto_approve(tags, cfg)` takes a required first
    positional `tags`; pin TypeError on the second arg explicitly.
    """
    with pytest.raises(TypeError):
        auto_approve.should_auto_approve(["#x"], "not-a-config")


# ---------------------------------------------------------------------------
# (5) FLAT_TO_SECTIONED entries pinned — the seven migrated knobs are
#     listed in the back-compat map. A refactor that drops any entry
#     would silently break the cfg-based helper's reverse-lookup.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        # TB-430: the master switch flipped to the suppress-polarity
        # `AP2_AUTO_APPROVE_DISABLED` → `components.auto_approve.disabled`.
        # The legacy require-polarity `AP2_AUTO_APPROVE` is now env-only
        # (in `_KNOBS_STAYING_ENV_ONLY`, NOT `FLAT_TO_SECTIONED`) — pinned
        # by `test_legacy_auto_approve_flag_is_env_only` below.
        ("AP2_AUTO_APPROVE_DISABLED", "components.auto_approve.disabled"),
        ("AP2_AUTO_APPROVE_DRY_RUN", "components.auto_approve.dry_run"),
        ("AP2_AUTO_APPROVE_GATE_TAGS", "components.auto_approve.gate_tags"),
        (
            "AP2_AUTO_APPROVE_FREEZE_THRESHOLD",
            "components.auto_approve.freeze_threshold",
        ),
        (
            "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
            "components.auto_approve.per_task_token_cap",
        ),
        (
            "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP",
            "components.auto_approve.window_token_cap",
        ),
        (
            "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED",
            "components.auto_approve.noisy_pause_disabled",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_seven_migrated_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops any of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-332: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the auto_approve cross-package reverse-"
        f"lookup back-compat path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


def test_legacy_auto_approve_flag_is_env_only():
    """TB-430: the DEPRECATED legacy master flag `AP2_AUTO_APPROVE` is
    resolved env-only (the registry's `legacy_env_flag` tier), so it must
    NOT appear in `FLAT_TO_SECTIONED` (which would dead-map it to the
    retired `components.auto_approve.enabled` key) — it belongs in
    `_KNOBS_STAYING_ENV_ONLY`, the same category as the other
    deployment-shell master switches (`AP2_CRON_DISABLED`, etc.).
    """
    assert "AP2_AUTO_APPROVE" not in FLAT_TO_SECTIONED, (
        "TB-430: legacy `AP2_AUTO_APPROVE` must not map through "
        "FLAT_TO_SECTIONED — it is env-only (registry legacy tier)."
    )
    assert "AP2_AUTO_APPROVE" in _KNOBS_STAYING_ENV_ONLY, (
        "TB-430: legacy `AP2_AUTO_APPROVE` must be declared in "
        "`_KNOBS_STAYING_ENV_ONLY` so the TB-338 cut-line gate accepts "
        "its env-only resolution."
    )
