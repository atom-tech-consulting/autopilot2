"""TB-327: auto_unfreeze component reads via `cfg.components_config` (axis-5 cluster).

Long-tail-cluster sibling to TB-326's auto_approve pilot
(`test_tb326_auto_approve_cfg_reads.py`); same five regression
cleavages applied to the five operator-tunable auto_unfreeze knobs
the component logically owns: `AP2_AUTO_UNFREEZE_DISABLED`,
`AP2_AUTO_UNFREEZE_FIX_SHAPES`, `AP2_AUTO_UNFREEZE_DRY_RUN`,
`AP2_AUTO_UNFREEZE_MAX_PER_TASK`, `AP2_AUTO_UNFREEZE_MAX_PER_DAY`.

The migrated helpers no longer read these via direct
`os.environ.get` calls inside the component body; they now flow
through `Config.get_component_value("auto_unfreeze", <key>)`, which
inspects sectioned env > flat env (via reverse-`FLAT_TO_SECTIONED`)
> `cfg.components_config["auto_unfreeze"][<key>]` > default at
call time. Behavior preservation contract: every existing
`AP2_AUTO_UNFREEZE_*` flat-env consumer (operator shell exports,
`.cc-autopilot/env`) keeps today's behavior bit-for-bit while a
TOML-opted operator's `[components.auto_unfreeze]` values win
transparently once env-side overrides are unset.

Five regression cleavages this pin holds (mirror of TB-326):

  (1) **Grep-shape**: zero remaining `os.environ.get("AP2_AUTO_UNFREEZE_…"`
      call sites in `ap2/components/auto_unfreeze/`. A refactor that
      re-introduces a direct env read here loses the back-compat layer
      and side-steps the structured-config precedence the operator
      depends on.
  (2) **TOML-first read path**: a `cfg.components_config` value
      populated from `config.toml` (or the sectioned-env override
      layer) wins over the legacy flat env name once env-side
      overrides are unset — the operator's TOML becomes the
      authoritative source the moment they opt in.
  (3) **Flat-env back-compat**: a flat env name unaccompanied by a
      TOML value still resolves the same value the old direct
      `os.environ.get` path did. The shell-export operator who never
      migrated `.cc-autopilot/env` sees zero observable change.
  (4) **Parser semantics preserved**: empty / non-int / non-positive
      values still default to the original sentinels (1 for
      `max_per_task`, 3 for `max_per_day`, False for the bool knobs,
      empty frozenset for `fix_shapes`).
  (5) **Chosen access shape published**: the manifest's docstring
      cites the chosen resolved-config access shape (`cfg.get_component_value`)
      with a TB-327 anchor so the remaining four cluster migrations
      (attention, focus_advance, mattermost, validator_judge, janitor)
      adopt it verbatim.

Why this matters: axis (5)'s long tail (per goal.md L353-364) sets
"≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads" as the Progress signal at L398-399. TB-326's
pilot landed 3 of N; this cluster adds 5 more (axis-2-critical: the
auto-unfreeze sweep is the failure-recovery operator-dependency
closure the focus depends on).
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2 import daemon
from ap2.components.auto_unfreeze import _is_auto_unfreeze_disabled
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb327_auto_unfreeze_cfg_reads.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob the harness/CI might have set so each
    test owns its `os.environ` surface deterministically. Other test
    fixtures that depend on a clean env (notably `cfg` below) take this
    as a parameter so the strip lands BEFORE `Config.load` reads any
    AP2_* override. Mirror of the TB-326 pilot's `clean_env` shape.
    """
    import os

    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def cfg(tmp_path, clean_env):
    """Per-test cfg over a fresh project root with a stripped env surface.

    `init_project` scaffolds `.cc-autopilot/config.toml` from the
    schema-rendered CONFIG_TEMPLATE (TB-325), so `Config.load` lands on
    the TOML branch. `clean_env` strips every `AP2_*` env knob FIRST so
    the project's `.cc-autopilot/env` doesn't leak into the cfg via the
    env-override layer; the back-compat shim sees an empty `os.environ`
    and contributes nothing. Tests that exercise the flat-env back-
    compat path use `clean_env.setenv(...)` AFTER cfg is built.
    """
    init_project(tmp_path)
    return Config.load(tmp_path)


@pytest.fixture
def emit_reset():
    """Reset the module-level `_EMITTED_ONCE` set in `config_compat` so
    the one-shot `env_deprecated` accounting doesn't leak between tests.
    """
    reset_env_deprecated_emit_for_tests()
    yield
    reset_env_deprecated_emit_for_tests()


def _load_toml_cfg(tmp_path, body: str) -> Config:
    """Helper that writes `body` to `.cc-autopilot/config.toml` and
    returns the corresponding `Config.load` result (TOML branch).
    Caller is responsible for stripping `AP2_*` env vars BEFORE
    invoking this — the helper itself does not touch `os.environ`. The
    TOML-first read-path tests below take `clean_env` as a fixture
    parameter so the strip lands before `Config.load`; that strip
    persists across this helper call too because the underlying
    `monkeypatch` is the same per-test instance.
    """
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


# ---------------------------------------------------------------------------
# (1) Grep-shape — zero remaining `os.environ.get("AP2_AUTO_UNFREEZE_…"`
#     call sites in the component body.
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_auto_unfreeze_component():
    """The grep-shape Verification bullet, pinned to source so a refactor
    that re-introduces a direct env read inside the component body
    surfaces here instead of only via the briefing-level grep gate.

    The component package is `ap2/components/auto_unfreeze/` (both
    `__init__.py` and `manifest.py`); the test reads each `.py` file
    in the package and rejects any literal `os.environ.get("AP2_AUTO_UNFREEZE_`
    fragment. Comments / docstrings that QUOTE the old call sites for
    historical context are allowed iff they DON'T form a valid call
    statement — the pattern below matches only the bare call shape
    `os.environ.get("AP2_AUTO_UNFREEZE_…"` (the briefing-level grep's
    own anchor), so a backticked-in-docstring mention does NOT match
    because the docstring quotes break the literal.
    """
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_AUTO_UNFREEZE_")
    component_dir = _REPO_ROOT / "ap2/components/auto_unfreeze"
    violations: list[str] = []
    for py_path in sorted(component_dir.rglob("*.py")):
        src = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = py_path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-327: the auto_unfreeze component body must read its five "
        "operator-tunable knobs via `cfg.get_component_value(...)`, "
        "not via direct `os.environ.get('AP2_AUTO_UNFREEZE_…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_cfg_get_component_value_path_present_in_component_body():
    """Positive form of the grep-shape pin: the component body
    documents+uses the chosen `cfg.get_component_value` resolved-
    config access shape. A refactor that swaps the helper out for
    something else (e.g. inlining `cfg.components_config[...]`)
    surfaces here so the documented TB-326 pilot pattern stays the
    canonical template for the cluster.
    """
    init_src = (
        _REPO_ROOT / "ap2/components/auto_unfreeze/__init__.py"
    ).read_text(encoding="utf-8")
    assert "cfg.get_component_value" in init_src, (
        "TB-327: the auto_unfreeze component body should use "
        "`cfg.get_component_value(...)` to resolve the five migrated "
        "knobs (per the TB-326 pilot's chosen access shape — see the "
        "auto_approve manifest docstring and the auto_unfreeze "
        "manifest's TB-327 doc block)."
    )


# ---------------------------------------------------------------------------
# (2) TOML-first read path — cfg.components_config wins over flat env.
# ---------------------------------------------------------------------------


def test_disabled_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_unfreeze] disabled = true` TOML value
    populates `cfg.components_config["auto_unfreeze"]["disabled"]`,
    which the helper reads via `cfg.get_component_value`. The legacy
    flat env name is UNSET; the helper returns True from the TOML
    layer (no env fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_unfreeze]\ndisabled = true\n",
    )
    assert cfg.components_config["auto_unfreeze"]["disabled"] is True
    assert _is_auto_unfreeze_disabled(cfg) is True


def test_fix_shapes_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_unfreeze] fix_shapes = "..."` TOML value
    flows through to the helper's frozenset return value. Comma-list
    parsing applies to the TOML-side string just as it did to the
    env-side string pre-migration.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_unfreeze]\n"
        "fix_shapes = \"grep_missing_r_on_dir, literal_backtick_in_shell_bullet\"\n",
    )
    got = daemon._auto_unfreeze_allowlist(cfg)
    assert got == frozenset({
        "grep_missing_r_on_dir",
        "literal_backtick_in_shell_bullet",
    })


def test_dry_run_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_unfreeze] dry_run = true` TOML value flows
    through to the helper's bool return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_unfreeze]\ndry_run = true\n",
    )
    assert daemon._auto_unfreeze_dry_run(cfg) is True


def test_max_per_task_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_unfreeze] max_per_task = 7` TOML value flows
    through to the helper's int return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_unfreeze]\nmax_per_task = 7\n",
    )
    assert daemon._auto_unfreeze_max_per_task(cfg) == 7


def test_max_per_day_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_unfreeze] max_per_day = 42` TOML value flows
    through to the helper's int return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_unfreeze]\nmax_per_day = 42\n",
    )
    assert daemon._auto_unfreeze_max_per_day(cfg) == 42


# ---------------------------------------------------------------------------
# (3) Flat-env back-compat — same value the legacy direct read returned.
# ---------------------------------------------------------------------------


def test_disabled_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_UNFREEZE_DISABLED=1` set on an env-only project still
    resolves to True via the `Config.get_component_value` reverse-
    `FLAT_TO_SECTIONED` lookup. Pins the back-compat path the
    shell-export operator depends on.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_DISABLED", "1")
    assert _is_auto_unfreeze_disabled(cfg) is True


def test_fix_shapes_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_UNFREEZE_FIX_SHAPES=foo,bar` on an env-only project
    resolves to `{"foo", "bar"}` via the flat-env back-compat path.
    """
    clean_env.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir,bare_path_to_test_f",
    )
    assert daemon._auto_unfreeze_allowlist(cfg) == frozenset({
        "grep_missing_r_on_dir",
        "bare_path_to_test_f",
    })


def test_dry_run_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_UNFREEZE_DRY_RUN=true` on an env-only project resolves
    to True via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "true")
    assert daemon._auto_unfreeze_dry_run(cfg) is True


def test_max_per_task_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_UNFREEZE_MAX_PER_TASK=9` on an env-only project resolves
    to 9 via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "9")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 9


def test_max_per_day_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_UNFREEZE_MAX_PER_DAY=11` on an env-only project resolves
    to 11 via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "11")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 11


# ---------------------------------------------------------------------------
# (4) Parser semantics preserved — default-on-bad-value pins.
# ---------------------------------------------------------------------------


def test_disabled_unset_defaults_to_false(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_is_auto_unfreeze_disabled`
    returns False (kill switch off). Same default the pre-migration
    env-only path returned for the unset case.
    """
    assert _is_auto_unfreeze_disabled(cfg) is False


def test_fix_shapes_unset_defaults_to_empty(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_auto_unfreeze_allowlist`
    returns the empty frozenset (feature disabled).
    """
    assert daemon._auto_unfreeze_allowlist(cfg) == frozenset()


def test_fix_shapes_whitespace_only_treated_as_empty(
    cfg, clean_env, emit_reset,
):
    """Whitespace-only env value → empty frozenset. Pins the
    `if not text: return frozenset()` post-strip guard.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_FIX_SHAPES", "  ,  ,  ")
    assert daemon._auto_unfreeze_allowlist(cfg) == frozenset()


def test_dry_run_unset_defaults_to_false(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_auto_unfreeze_dry_run` returns
    False (feature off).
    """
    assert daemon._auto_unfreeze_dry_run(cfg) is False


def test_dry_run_garbage_treated_as_false(cfg, clean_env, emit_reset):
    """Non-truthy env value → False. Pins the parser-fallback shape
    so a typo doesn't silently enable a feature that's supposed to
    flip the WRITE step.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "garbage")
    assert daemon._auto_unfreeze_dry_run(cfg) is False


def test_max_per_task_unset_defaults_to_one(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_auto_unfreeze_max_per_task`
    returns 1 (the briefing's stated default).
    """
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1


def test_max_per_task_garbage_defaults_to_one(cfg, clean_env, emit_reset):
    """Non-int env value → default 1. Pins the parser-fallback shape
    the pre-migration `try: int(raw) except ValueError: return default`
    chain enforced.
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "not-a-number")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1


def test_max_per_task_negative_defaults_to_one(cfg, clean_env, emit_reset):
    """Negative env value → default 1. Pins the
    `v if v >= 0 else default` post-parse guard. (Zero is honored as
    explicit disable; see the next test.)
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "-5")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1


def test_max_per_task_zero_honored_as_disable(cfg, clean_env, emit_reset):
    """Explicit `0` env value honored as "cap disabled" (the briefing's
    stated semantics — disabling the per-task cap should be an
    explicit operator decision, not a side effect of garbage).
    """
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "0")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 0


def test_max_per_day_unset_defaults_to_three(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_auto_unfreeze_max_per_day`
    returns 3 (rolling 24h default).
    """
    assert daemon._auto_unfreeze_max_per_day(cfg) == 3


def test_max_per_day_garbage_defaults_to_three(cfg, clean_env, emit_reset):
    """Non-int env value → default 3."""
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "not-a-number")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 3


def test_max_per_day_negative_defaults_to_three(cfg, clean_env, emit_reset):
    """Negative env value → default 3 (symmetric to per-task pin)."""
    clean_env.setenv("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "-1")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 3


# ---------------------------------------------------------------------------
# (5) Chosen access shape published — manifest docstring cites it.
# ---------------------------------------------------------------------------


def test_manifest_documents_chosen_access_shape():
    """The auto_unfreeze manifest documents (top-of-file docstring or
    in-body comment) the chosen resolved-config access shape so the
    follow-up cluster migrations (attention, focus_advance,
    mattermost, validator_judge, janitor) read the same pattern from
    one place. Looks for the `cfg.get_component_value` call shape +
    a TB-327 reference. Loose enough that a docstring rewrite doesn't
    false-positive; strict enough that an accidental documentation
    drop fires.
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/auto_unfreeze/manifest.py"
    ).read_text(encoding="utf-8")
    assert "TB-327" in manifest_src, (
        "TB-327: the auto_unfreeze manifest must cite the TB-327 "
        "axis-5 cluster anchor so the follow-up cluster migrations "
        "have a discoverable pointer to the chosen access shape."
    )
    assert "cfg.get_component_value" in manifest_src, (
        "TB-327: the auto_unfreeze manifest must name the chosen "
        "resolved-config access shape (`cfg.get_component_value`) so "
        "the follow-up cluster migrations adopt the same pattern "
        "verbatim instead of each picking ad-hoc shapes."
    )


# ---------------------------------------------------------------------------
# Sanity: the five migrated knobs are listed in FLAT_TO_SECTIONED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        (
            "AP2_AUTO_UNFREEZE_DISABLED",
            "components.auto_unfreeze.disabled",
        ),
        (
            "AP2_AUTO_UNFREEZE_FIX_SHAPES",
            "components.auto_unfreeze.fix_shapes",
        ),
        (
            "AP2_AUTO_UNFREEZE_DRY_RUN",
            "components.auto_unfreeze.dry_run",
        ),
        (
            "AP2_AUTO_UNFREEZE_MAX_PER_TASK",
            "components.auto_unfreeze.max_per_task",
        ),
        (
            "AP2_AUTO_UNFREEZE_MAX_PER_DAY",
            "components.auto_unfreeze.max_per_day",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_five_migrated_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops one of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-327: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the auto_unfreeze reverse-lookup back-compat "
        f"path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )
