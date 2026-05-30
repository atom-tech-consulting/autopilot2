"""TB-326: auto_approve component reads via `cfg.components_config` (axis-5 pilot).

Pilot regression-pin for the **structured config (env → TOML)** axis-(5)
read-site migration (goal.md L353-364). The three operator-tunable
auto_approve knobs the component logically owns
(`AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`,
`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`) no longer get their values via
direct `os.environ.get` calls inside the component body; they now flow
through `Config.get_component_value("auto_approve", <key>)`, which
inspects `cfg.components_config["auto_approve"][<key>]` first (TOML +
sectioned-env + flat-env back-compat) and falls back to the flat env
name via the reverse-`FLAT_TO_SECTIONED` lookup.

Five regression cleavages this pin holds (the pilot template every
follow-up cluster reuses):

  (1) **Grep-shape**: zero remaining `os.environ.get("AP2_AUTO_APPROVE_…"`
      call sites in `ap2/components/auto_approve/`. A refactor that
      re-introduces a direct env read here loses the back-compat layer's
      one-shot `env_deprecated` event and side-steps the structured-
      config precedence the operator depends on.
  (2) **TOML-first read path**: a `cfg.components_config` value
      populated from `config.toml` (or the sectioned-env override
      layer) wins over the legacy flat env name — the operator's TOML
      becomes the authoritative source the moment they opt in.
  (3) **Flat-env back-compat**: a flat env name unaccompanied by a
      TOML value still resolves the same int value the old direct
      `os.environ.get` path did. The shell-export operator who never
      migrated `.cc-autopilot/env` sees zero observable change.
  (4) **Parser semantics preserved**: empty / non-int / non-positive
      values still default to the original sentinels
      (`AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT` for the threshold,
      `0`/cap-disabled for the two caps).
  (5) **Chosen access shape published**: the manifest's docstring
      cites the chosen resolved-config access shape so the follow-up
      cluster migrations (attention, focus_advance, auto_unfreeze,
      mattermost, validator_judge, janitor) adopt it verbatim.

Why this pilot now: axis (5)'s long tail (per goal.md L353-364) sets
"≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads" as the Progress signal at L398-399. Today's
migrated count is 0/N; the auto_approve cluster (9 sectioned mappings
on `FLAT_TO_SECTIONED`) is the largest single migration and the
operator-facing surface (`ap2 status` renders the on/off state and
the validator-judge / cost-cap detectors all hit these read paths), so
the pilot is independently verifiable end-to-end.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2 import daemon, ideation
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb326_auto_approve_cfg_reads.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob the harness/CI might have set so each
    test owns its `os.environ` surface deterministically. Other test
    fixtures that depend on a clean env (notably `cfg` below) take this
    as a parameter so the strip lands BEFORE `Config.load` reads any
    AP2_* override.
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
    the project's `.cc-autopilot/env` (which may carry operator-tuned
    `AP2_AUTO_APPROVE_*` values in this repo) doesn't leak into the
    cfg via the env-override layer; the back-compat shim sees an empty
    `os.environ` and contributes nothing. Tests that exercise the
    flat-env back-compat path use `clean_env.setenv(...)` AFTER cfg is
    built, mirroring the
    `monkeypatch.setenv(...); helper(cfg)` shape the pre-TB-326 tests
    used against the env-only helper.
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
# (1) Grep-shape — zero remaining `os.environ.get("AP2_AUTO_APPROVE_…"`
#     call sites in the component body.
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_auto_approve_component():
    """The grep-shape Verification bullet, pinned to source so a refactor
    that re-introduces a direct env read inside the component body
    surfaces here instead of only via the briefing-level grep gate.

    The component package is `ap2/components/auto_approve/` (both
    `__init__.py` and `manifest.py`); the test reads each `.py` file
    in the package and rejects any literal `os.environ.get("AP2_AUTO_APPROVE_`
    fragment. Comments / docstrings that QUOTE the old call sites for
    historical context are allowed iff they DON'T form a valid call
    statement — the pattern below matches only the bare call shape
    `os.environ.get("AP2_AUTO_APPROVE_…"` (the briefing-level grep's
    own anchor), so a backticked-in-docstring mention like
    `\\`os.environ.get(...)\\`` does NOT match because the docstring
    quotes break the literal.
    """
    pattern = re.compile(r"os\.environ\.get\([\"']AP2_AUTO_APPROVE_")
    component_dir = _REPO_ROOT / "ap2/components/auto_approve"
    violations: list[str] = []
    for py_path in sorted(component_dir.rglob("*.py")):
        src = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = py_path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-326: the auto_approve component body must read the three "
        "operator-tunable knobs via `cfg.get_component_value(...)`, "
        "not via direct `os.environ.get('AP2_AUTO_APPROVE_…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_cfg_get_component_value_path_present_in_component_body():
    """Positive form of the grep-shape pin: the component body
    documents+uses the chosen `cfg.get_component_value` resolved-
    config access shape. A refactor that swaps the helper out for
    something else (e.g. inlining `cfg.components_config[...]`)
    surfaces here so the documented pilot pattern stays the canonical
    template for the remaining six clusters.
    """
    # TB-343: the body (with its cfg.get_component_value calls) moved to impl.py.
    init_src = (
        _REPO_ROOT / "ap2/components/auto_approve/impl.py"
    ).read_text(encoding="utf-8")
    assert "cfg.get_component_value" in init_src, (
        "TB-326: the auto_approve component body should use "
        "`cfg.get_component_value(...)` to resolve the three migrated "
        "knobs (per the pilot's chosen access shape — see the manifest "
        "docstring)."
    )


# ---------------------------------------------------------------------------
# (2) TOML-first read path — cfg.components_config wins over flat env.
# ---------------------------------------------------------------------------


def test_freeze_threshold_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_approve] freeze_threshold = 7` TOML value
    populates `cfg.components_config["auto_approve"]["freeze_threshold"]`,
    which the helper reads via `cfg.get_component_value`. The legacy
    flat env name is UNSET; the helper returns 7 from the TOML layer
    (no env fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_approve]\nfreeze_threshold = 7\n",
    )
    assert cfg.components_config["auto_approve"]["freeze_threshold"] == 7
    assert daemon._auto_approve_freeze_threshold(cfg) == 7


def test_per_task_token_cap_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_approve] per_task_token_cap = 42000` TOML
    value flows through to the helper's int return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_approve]\nper_task_token_cap = 42000\n",
    )
    assert daemon._per_task_token_cap(cfg) == 42000


def test_window_token_cap_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.auto_approve] window_token_cap = 999999` TOML
    value flows through to the helper's int return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.auto_approve]\nwindow_token_cap = 999999\n",
    )
    assert daemon._window_token_cap(cfg) == 999999


# ---------------------------------------------------------------------------
# (3) Flat-env back-compat — same value the legacy direct read returned.
# ---------------------------------------------------------------------------


def test_freeze_threshold_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_APPROVE_FREEZE_THRESHOLD=5` set on an env-only project
    (no `config.toml`) still resolves to 5 via the
    `Config.get_component_value` reverse-`FLAT_TO_SECTIONED` lookup.
    Pins the back-compat path the shell-export operator depends on.
    """
    clean_env.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "5")
    assert daemon._auto_approve_freeze_threshold(cfg) == 5


def test_per_task_token_cap_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP=12345` on an env-only
    project resolves to 12345 via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "12345")
    assert daemon._per_task_token_cap(cfg) == 12345


def test_window_token_cap_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=678910` on an env-only
    project resolves to 678910 via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "678910")
    assert daemon._window_token_cap(cfg) == 678910


# ---------------------------------------------------------------------------
# (4) Parser semantics preserved — the three default-on-bad-value pins.
# ---------------------------------------------------------------------------


def test_freeze_threshold_unset_defaults_to_three(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_auto_approve_freeze_threshold`
    returns `AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT` (3). Same default
    the pre-migration env-only path returned for the unset case.
    """
    assert daemon._auto_approve_freeze_threshold(cfg) == (
        ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT
    )


def test_freeze_threshold_garbage_defaults_to_three(
    cfg, clean_env, emit_reset,
):
    """Non-int env value → default 3. Pins the parser-fallback shape
    the pre-migration `try: int(raw) except ValueError: return default`
    chain enforced.
    """
    clean_env.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "not-a-number")
    assert daemon._auto_approve_freeze_threshold(cfg) == (
        ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT
    )


def test_per_task_token_cap_unset_disabled(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_per_task_token_cap` returns 0
    (cap disabled). Operators who haven't budgeted their project don't
    get a hardcoded cap surprising them.
    """
    assert daemon._per_task_token_cap(cfg) == 0


def test_window_token_cap_unset_disabled(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_window_token_cap` returns 0
    (cap disabled). Same parse shape as the per-task cap.
    """
    assert daemon._window_token_cap(cfg) == 0


def test_per_task_token_cap_negative_treated_as_disabled(
    cfg, clean_env, emit_reset,
):
    """Negative env value → 0 (cap disabled). Pins the
    `v if v > 0 else 0` post-parse guard.
    """
    clean_env.setenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "-100")
    assert daemon._per_task_token_cap(cfg) == 0


def test_window_token_cap_negative_treated_as_disabled(
    cfg, clean_env, emit_reset,
):
    """Negative env value → 0 (cap disabled). Symmetric to the
    per-task-cap negative pin.
    """
    clean_env.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "-1")
    assert daemon._window_token_cap(cfg) == 0


# ---------------------------------------------------------------------------
# (5) Chosen access shape published — manifest docstring cites it.
# ---------------------------------------------------------------------------


def test_manifest_documents_chosen_access_shape():
    """The auto_approve manifest documents (top-of-file docstring or
    in-body comment) the chosen resolved-config access shape so the
    follow-up cluster migrations (attention, focus_advance,
    auto_unfreeze, mattermost, validator_judge, janitor) adopt the
    same pattern. Looks for the `cfg.get_component_value` call shape
    + a TB-326 reference. Loose enough that a docstring rewrite
    doesn't false-positive; strict enough that an accidental
    documentation drop fires.
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/auto_approve/manifest.py"
    ).read_text(encoding="utf-8")
    assert "TB-326" in manifest_src, (
        "TB-326: the auto_approve manifest must cite the TB-326 axis-5 "
        "pilot so the follow-up cluster migrations have a discoverable "
        "anchor pointing at the chosen access shape."
    )
    assert "cfg.get_component_value" in manifest_src, (
        "TB-326: the auto_approve manifest must name the chosen "
        "resolved-config access shape (`cfg.get_component_value`) so "
        "the follow-up cluster migrations adopt the same pattern "
        "verbatim instead of each picking ad-hoc shapes."
    )


# ---------------------------------------------------------------------------
# Sanity: the three migrated knobs are listed in FLAT_TO_SECTIONED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
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
    ],
)
def test_flat_to_sectioned_pins_the_three_migrated_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops one of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-326: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the auto_approve reverse-lookup back-compat "
        f"path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )
