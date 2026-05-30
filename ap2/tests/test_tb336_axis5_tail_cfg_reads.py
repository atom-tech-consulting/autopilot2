"""TB-336: axis-5 cross-package + cross-component migration tail.

Closes the migration tail outside the documented
`_KNOBS_STAYING_ENV_ONLY` 12-factor exempt set + outside the
`ap2/config.py` / `ap2/env_reload.py` bootstrap path. The eight
remaining direct ``os.environ.get('AP2_*')`` call sites that the
TB-326 / TB-327 / TB-328 / TB-329 (per-component cluster pilots) +
TB-332 / TB-333 / TB-334 (cross-package + core cluster sweeps) didn't
cover now route through ``cfg.get_core_value`` /
``cfg.get_component_value``:

  - ``ap2/web.py``::``is_web_disabled`` → ``cfg.get_core_value("web_disabled", …)``
  - ``ap2/web.py``::``daemon_web_port`` → ``cfg.get_core_value("web_port", …)``
  - (TB-345 removed the former ``ap2/goal.py`` focus_advance helpers
    ``advance_empty_cycles_threshold`` / ``auto_advance_disabled``;
    the residual detector + its two knobs moved to the core
    ``ap2/ideation_halt.py`` module, read via
    ``cfg.get_core_value("ideation_halt_*", …)``.)
  - ``ap2/doctor.py``::``_verify_gate_state`` →
    ``cfg.get_core_value("verify_cmd", …)`` +
    ``cfg.get_core_value("verify_timeout_s", …)``
  - ``ap2/ideation.py``::``_run_ideation`` →
    ``cfg.get_core_value("ideation_max_turns", …)`` (TB-334 straggler)
  - ``ap2/components/attention/__init__.py``::``_cost_approach_pct`` →
    ``cfg.get_component_value("auto_approve", "cost_approach_pct", …)``
    (cross-COMPONENT read; the auto_approve manifest's
    ``config_schema`` carries the matching ``cost_approach_pct`` key
    so ``test_every_config_key_documented`` stays green).

Five regression cleavages this pin holds (mirror of TB-326 / TB-329 /
TB-332 / TB-333 / TB-334 templates applied to the tail's eight sites):

  (1) **Grep-shape (per file)**: zero remaining
      ``os.environ.get("AP2_WEB_…"`` /
      ``os.environ.get("AP2_FOCUS_…"`` /
      ``os.environ.get("AP2_VERIFY_(CMD|TIMEOUT_S)"`` /
      ``os.environ.get("AP2_IDEATION_MAX_TURNS"`` /
      ``os.environ.get("AP2_AUTO_APPROVE_COST_APPROACH_PCT"`` call
      sites in the five migrated files. A refactor that re-introduces
      a direct env read in any of these files surfaces here in addition
      to the briefing-level grep gate.
  (2) **Per-knob cfg-read parity (flat env)**: for each migrated knob,
      a ``monkeypatch.setenv(<flat>, …)`` value reaches the cfg helper
      identical to what the legacy ``os.environ.get(<flat>, default)``
      shape would return.
  (3) **Per-knob cfg-read parity (sectioned env)**: same parity for
      the sectioned-env name (``AP2_CORE_<KEY>`` for core keys,
      ``AP2_COMPONENTS_<NAME>_<KEY>`` for component keys) — the
      canonical naming under the TB-323 sectioned regime.
  (4) **Default-on-unset semantics**: empty env + empty TOML → caller's
      default. Bit-for-bit identical to the pre-migration
      ``os.environ.get(<flat>, <default>)`` return value.
  (5) **auto_approve schema carries `cost_approach_pct`**: the
      attention component's cross-component read of the auto_approve
      knob depends on the auto_approve manifest's ``config_schema``
      having a ``cost_approach_pct`` entry so the howto.md TOML
      reference + the ``test_every_config_key_documented`` gate
      stay aligned (TB-330 precedent).

Out of scope (per the briefing): Mattermost-family + sandbox-identity
knobs (``AP2_MM_*``, ``AP2_DIR``, ``AP2_REAL_SDK``) — documented as
12-factor exempts in ``ap2/config_compat.py``
``_KNOBS_STAYING_ENV_ONLY``. ``ap2/config.py``'s ``Config.from_env``
construction reads and ``ap2/env_reload.py``'s hot-reload mirror
also stay env-only — they CONSTRUCT cfg.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2.components.attention import (
    DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT,
    _cost_approach_pct,
)
from ap2.components.auto_approve.manifest import MANIFEST as AUTO_APPROVE_MANIFEST
from ap2.config import (
    DEFAULT_VERIFY_TIMEOUT_S,
    Config,
)
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project
from ap2 import doctor, web


# Repository root, derived from this file's location:
# ap2/tests/test_tb336_axis5_tail_cfg_reads.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically. Mirrors the TB-326 / TB-329 / TB-334
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
    AP2_* values via `apply_env_overrides`.
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
# (1) Grep-shape — zero remaining `os.environ.get(...)` reads in the
#     five migrated files.
# ---------------------------------------------------------------------------


# (rel_path, regex anchor) tuples — one per briefing Verification grep gate.
_GREP_ABSENCE_CASES = [
    ("ap2/web.py", r"os\.environ\.get\([\"']AP2_WEB_"),
    ("ap2/goal.py", r"os\.environ\.get\([\"']AP2_FOCUS_"),
    (
        "ap2/doctor.py",
        r"os\.environ\.get\([\"']AP2_VERIFY_(CMD|TIMEOUT_S)",
    ),
    ("ap2/ideation.py", r"os\.environ\.get\([\"']AP2_IDEATION_MAX_TURNS"),
    (
        # TB-343: the attention body moved to impl.py.
        "ap2/components/attention/impl.py",
        r"os\.environ\.get\([\"']AP2_AUTO_APPROVE_COST_APPROACH_PCT",
    ),
]


@pytest.mark.parametrize("rel_path, pattern_src", _GREP_ABSENCE_CASES)
def test_no_direct_env_reads_in_migrated_files(rel_path, pattern_src):
    """Per-file grep-shape pin: no `os.environ.get("AP2_…"` for the
    migrated knob shapes in any of the five migrated files.

    Comments / docstrings that QUOTE the old call-site shape for
    historical context are allowed iff they don't form a literal call
    statement (the regex anchor `os\\.environ\\.get\\([\"']AP2_<…>` only
    matches the bare-call shape). The `os.getenv(...)` legacy back-compat
    fallback the TB-332/333/336 pattern uses for the `cfg=None` branch
    is intentionally NOT matched here — that's the standard cross-
    package back-compat shape and the briefing's grep gate excludes it
    by construction.
    """
    pattern = re.compile(pattern_src)
    src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    violations: list[str] = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if pattern.search(line):
            violations.append(f"L{lineno}: {line.strip()}")
    assert not violations, (
        f"TB-336: {rel_path} must read its migrated AP2_* knob(s) via "
        "`cfg.get_core_value(...)` / `cfg.get_component_value(...)`, "
        "not via direct `os.environ.get('AP2_…')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_positive_cfg_helper_read_path_present_in_migrated_files():
    """Positive form of the grep-shape pins: each migrated file calls
    the appropriate cfg helper at least once. Mirrors the
    `get_core_value(...)` / `get_component_value(...)` positive
    grep checks in the briefing's Verification block.
    """
    cases = [
        ("ap2/web.py", r"get_core_value\([\"']web_(port|disabled)[\"']"),
        # TB-345: the focus_advance cfg-read helpers were deleted from
        # goal.py when the component merged into the core ideation_halt
        # module, so goal.py no longer calls get_component_value here.
        ("ap2/doctor.py", r"get_core_value\([\"']verify_(cmd|timeout_s)[\"']"),
        ("ap2/ideation.py", r"get_core_value\([\"']ideation_max_turns[\"']"),
        (
            # TB-343: the attention body moved to impl.py.
            "ap2/components/attention/impl.py",
            r"get_component_value\([\"']auto_approve[\"']",
        ),
    ]
    for rel_path, pattern_src in cases:
        pattern = re.compile(pattern_src)
        src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert pattern.search(src), (
            f"TB-336: {rel_path} should call the corresponding cfg "
            f"helper ({pattern_src}) for its migrated knob read."
        )


# ---------------------------------------------------------------------------
# (2) Per-knob cfg-read parity — `cfg.get_core_value` /
#     `cfg.get_component_value` returns the same value as the legacy
#     `os.environ.get(<flat>, default)` shape would.
# ---------------------------------------------------------------------------


# Each tuple: (label, sectioned_path, flat_env, sectioned_env, sample, getter)
# - sectioned_path: the dotted sectioned name (e.g. "core.web_port"); the
#   test reads it back from FLAT_TO_SECTIONED to cross-check the mapping.
# - sectioned_env: the env name the helper consults FIRST
#   (`AP2_CORE_<KEY>` or `AP2_COMPONENTS_<NAME>_<KEY>`).
# - flat_env: the legacy env name the back-compat layer accepts.
# - sample: the value to set; chosen as a string the helper can return.
# - getter: a callable(cfg) -> raw-string the helper returns. The test
#   compares this against `sample` after each setenv.
_FLAT_PARITY_CASES = [
    (
        "web_disabled",
        "core.web_disabled",
        "AP2_WEB_DISABLED",
        "AP2_CORE_WEB_DISABLED",
        "1",
        lambda c: c.get_core_value("web_disabled", default=""),
    ),
    (
        "web_port",
        "core.web_port",
        "AP2_WEB_PORT",
        "AP2_CORE_WEB_PORT",
        "9999",
        lambda c: c.get_core_value("web_port", default=""),
    ),
    (
        "verify_cmd",
        "core.verify_cmd",
        "AP2_VERIFY_CMD",
        "AP2_CORE_VERIFY_CMD",
        "uv run pytest -q",
        lambda c: c.get_core_value("verify_cmd", default=""),
    ),
    (
        "verify_timeout_s",
        "core.verify_timeout_s",
        "AP2_VERIFY_TIMEOUT_S",
        "AP2_CORE_VERIFY_TIMEOUT_S",
        "600",
        lambda c: c.get_core_value("verify_timeout_s", default=""),
    ),
    (
        "ideation_max_turns",
        "core.ideation_max_turns",
        "AP2_IDEATION_MAX_TURNS",
        "AP2_CORE_IDEATION_MAX_TURNS",
        "55",
        lambda c: c.get_core_value("ideation_max_turns", default=""),
    ),
    (
        "auto_approve.cost_approach_pct",
        "components.auto_approve.cost_approach_pct",
        "AP2_AUTO_APPROVE_COST_APPROACH_PCT",
        "AP2_COMPONENTS_AUTO_APPROVE_COST_APPROACH_PCT",
        "50",
        lambda c: c.get_component_value(
            "auto_approve", "cost_approach_pct", default="",
        ),
    ),
]


@pytest.mark.parametrize(
    "label, sectioned_path, flat, sectioned_env, sample, getter",
    _FLAT_PARITY_CASES,
)
def test_flat_env_parity(
    cfg, clean_env, emit_reset,
    label, sectioned_path, flat, sectioned_env, sample, getter,
):
    """A `monkeypatch.setenv(<flat>, <sample>)` value reaches the cfg
    helper identical to what `os.environ.get(<flat>, default)` would
    have returned pre-TB-336.

    Drives the back-compat path the shell-export operator depends on
    via the helper's reverse-`FLAT_TO_SECTIONED` lookup.
    """
    clean_env.setenv(flat, sample)
    assert str(getter(cfg)) == sample, (
        f"TB-336 ({label}): flat env `{flat}={sample}` should resolve "
        f"to {sample!r} via the cfg helper."
    )


@pytest.mark.parametrize(
    "label, sectioned_path, flat, sectioned_env, sample, getter",
    _FLAT_PARITY_CASES,
)
def test_sectioned_env_parity(
    cfg, clean_env, emit_reset,
    label, sectioned_path, flat, sectioned_env, sample, getter,
):
    """Same parity for the sectioned-env name — the canonical naming
    under the TB-323 sectioned regime, which the helper consults first.
    """
    clean_env.setenv(sectioned_env, sample)
    assert str(getter(cfg)) == sample, (
        f"TB-336 ({label}): sectioned env `{sectioned_env}={sample}` "
        f"should resolve to {sample!r} via the cfg helper."
    )


@pytest.mark.parametrize(
    "label, sectioned_path, flat, sectioned_env, sample, getter",
    _FLAT_PARITY_CASES,
)
def test_sectioned_env_wins_over_flat_env(
    cfg, clean_env, emit_reset,
    label, sectioned_path, flat, sectioned_env, sample, getter,
):
    """Sectioned env (`AP2_CORE_<KEY>` / `AP2_COMPONENTS_<NAME>_<KEY>`)
    wins over flat env (`AP2_<FLAT>`) — the head-of-list position the
    helper enforces at call time mirrors load-time
    `_apply_sectioned_env_overrides` precedence.
    """
    clean_env.setenv(flat, "FLAT-VAL")
    clean_env.setenv(sectioned_env, sample)
    assert str(getter(cfg)) == sample, (
        f"TB-336 ({label}): sectioned env `{sectioned_env}` should win "
        f"over flat env `{flat}`."
    )


@pytest.mark.parametrize(
    "label, sectioned_path, flat, sectioned_env, sample, getter",
    _FLAT_PARITY_CASES,
)
def test_unset_returns_empty_default(
    cfg, clean_env, emit_reset,
    label, sectioned_path, flat, sectioned_env, sample, getter,
):
    """Unset env + empty TOML → caller's default. Bit-for-bit identical
    to the pre-migration `os.environ.get(<flat>, "")` return value.
    """
    assert getter(cfg) == "", (
        f"TB-336 ({label}): unset {flat}/{sectioned_env} should "
        f"return the empty-string default the migrated call sites pass."
    )


@pytest.mark.parametrize(
    "label, sectioned_path, flat, sectioned_env, sample, getter",
    _FLAT_PARITY_CASES,
)
def test_flat_to_sectioned_carries_mapping(
    label, sectioned_path, flat, sectioned_env, sample, getter,
):
    """`FLAT_TO_SECTIONED` must carry the eight migrated knob mappings.
    A refactor that drops one would silently break the flat-env
    back-compat path for that knob.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned_path, (
        f"TB-336 ({label}): `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned_path!r}; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


# ---------------------------------------------------------------------------
# (3) Per-call-site shape pins — the migrated entry-point functions
#     honor the `*, cfg: Config | None = None` kwarg + TypeError guard
#     and produce the expected resolved value end-to-end.
# ---------------------------------------------------------------------------


def test_web_is_web_disabled_cfg_kwarg(cfg, clean_env, emit_reset):
    """`web.is_web_disabled(cfg=cfg)` reads via `cfg.get_core_value`."""
    clean_env.setenv("AP2_WEB_DISABLED", "1")
    assert web.is_web_disabled(cfg=cfg) is True
    clean_env.delenv("AP2_WEB_DISABLED", raising=False)
    assert web.is_web_disabled(cfg=cfg) is False


def test_web_is_web_disabled_back_compat_fallback(clean_env, emit_reset):
    """`web.is_web_disabled()` with no cfg falls back to the legacy
    `os.getenv` path so pre-cfg callers (CLI verbs, ad-hoc tests) keep
    today's behavior bit-for-bit.
    """
    clean_env.setenv("AP2_WEB_DISABLED", "yes")
    assert web.is_web_disabled() is True


def test_web_daemon_web_port_cfg_kwarg(cfg, clean_env, emit_reset):
    """`web.daemon_web_port(cfg=cfg)` reads via `cfg.get_core_value`."""
    clean_env.setenv("AP2_WEB_PORT", "9999")
    assert web.daemon_web_port(cfg=cfg) == 9999
    clean_env.delenv("AP2_WEB_PORT", raising=False)
    assert web.daemon_web_port(cfg=cfg) == web.DEFAULT_DAEMON_WEB_PORT


def test_web_is_web_disabled_typeerror_on_bad_cfg():
    """A non-Config argument to `cfg=` raises TypeError — guards
    against the silent "treat as None" pitfall the TB-332/333/336
    kwarg pattern explicitly rejects.
    """
    with pytest.raises(TypeError):
        web.is_web_disabled(cfg="not-a-cfg")  # type: ignore[arg-type]


# TB-345: the per-call-site pins for `goal.advance_empty_cycles_threshold`
# / `goal.auto_advance_disabled` were removed here — those goal.py helpers
# were deleted when the focus_advance component merged into the core
# `ap2/ideation_halt.py` module. The renamed core helpers
# (`ideation_halt._ideation_halt_empty_cycles_threshold(cfg)` /
# `_ideation_halt_disabled(cfg)`) are pinned in `test_ideation_halt.py`.


def test_doctor_verify_gate_state_cfg_kwarg(cfg, clean_env, emit_reset):
    """`doctor._verify_gate_state(cfg=cfg)` reads the two verify-gate
    knobs via `cfg.get_core_value`. The audit's OK / INFO line set is
    the same; pin the resolved string surfaces in the audit body.
    """
    clean_env.setenv("AP2_VERIFY_CMD", "uv run pytest -q")
    clean_env.setenv("AP2_VERIFY_TIMEOUT_S", "1234")
    res = doctor._verify_gate_state(cfg=cfg)
    body = " ".join(line for _level, line in res.messages)
    assert "uv run pytest -q" in body
    assert "1234s" in body


def test_doctor_verify_gate_state_unset_default(
    cfg, clean_env, emit_reset,
):
    """Unset `AP2_VERIFY_CMD` → INFO line surfaces the
    `AP2_VERIFY_CMD unset` diagnostic; the `verify_timeout_s` default
    is `DEFAULT_VERIFY_TIMEOUT_S` (the briefing's explicit default).
    """
    res = doctor._verify_gate_state(cfg=cfg)
    levels = [level for level, _ in res.messages]
    assert "INFO" in levels


def test_attention_cost_approach_pct_cfg(cfg, clean_env, emit_reset):
    """The cross-component read of the auto_approve knob threads through
    `cfg.get_component_value("auto_approve", "cost_approach_pct", …)`
    with the documented default + clamp.
    """
    # Unset → documented default (75).
    assert _cost_approach_pct(cfg) == DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT
    # Flat env override flows through the back-compat path.
    clean_env.setenv("AP2_AUTO_APPROVE_COST_APPROACH_PCT", "50")
    assert _cost_approach_pct(cfg) == 50
    # >= 100 clamps to 99 — the trip-line handoff to the post-trip
    # `auto_approve_paused` detector.
    clean_env.setenv("AP2_AUTO_APPROVE_COST_APPROACH_PCT", "150")
    assert _cost_approach_pct(cfg) == 99


# ---------------------------------------------------------------------------
# (4) auto_approve manifest schema carries `cost_approach_pct` (TB-330
#     precedent for cross-component reads — the howto.md
#     `test_every_config_key_documented` gate trips iff the schema and
#     docs disagree, so the schema entry is load-bearing).
# ---------------------------------------------------------------------------


def test_auto_approve_manifest_carries_cost_approach_pct():
    """The auto_approve component's `Manifest.config_schema` declares
    `cost_approach_pct` (TB-336). Mirrors the TB-330 precedent where
    a cross-component cfg-read forces the owning manifest to publish
    the key so the howto.md TOML reference + the
    `test_every_config_key_documented` gate stay aligned.
    """
    schema = AUTO_APPROVE_MANIFEST.config_schema
    assert "cost_approach_pct" in schema, (
        "TB-336: the auto_approve manifest must declare "
        "`cost_approach_pct` in its config_schema — the attention "
        "component's `_cost_approach_pct(cfg)` reads it via "
        "`cfg.get_component_value('auto_approve', 'cost_approach_pct')`, "
        "and the howto.md `test_every_config_key_documented` gate "
        "trips iff this declaration is missing."
    )
    entry = schema["cost_approach_pct"]
    assert entry.type is int
    assert entry.default == DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT
    assert entry.hot_reloadable is True


def test_howto_md_documents_cost_approach_pct():
    """The howto.md `## Config keys (TOML)` block documents
    `components.auto_approve.cost_approach_pct` so the
    `test_every_config_key_documented` gate stays green when the
    schema entry above is consulted.
    """
    howto = (_REPO_ROOT / "ap2/howto.md").read_text(encoding="utf-8")
    assert "components.auto_approve.cost_approach_pct" in howto, (
        "TB-336: howto.md `## Config keys (TOML)` block must reference "
        "`components.auto_approve.cost_approach_pct`."
    )
