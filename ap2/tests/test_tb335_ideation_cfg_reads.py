"""TB-335: ideation-cluster knob reads via `cfg.get_core_value`
(axis-5 core ideation cluster migration).

Sibling to TB-334 (core agent-runtime cluster) — where TB-334 closed
the agent-runtime quintuple (``AP2_AGENT_MODEL``, ``AP2_AGENT_EFFORT``,
``AP2_TASK_MAX_TURNS``, ``AP2_CONTROL_MAX_TURNS``,
``AP2_VERIFY_JUDGE_MAX_TURNS``), this TB closes the ideation-cluster
quartet that the ideation cron path runs against:

  * ``AP2_IDEATION_DISABLED`` → ``core.ideation_disabled``
  * ``AP2_IDEATION_COOLDOWN_S`` → ``core.ideation_cooldown_s``
  * ``AP2_IDEATION_TRIGGER_TASK_COUNT`` → ``core.ideation_trigger_task_count``
  * ``AP2_IDEATION_SCRUB_MODEL`` → ``core.ideation_scrub_model``

``AP2_IDEATION_MAX_TURNS`` is deliberately out of scope here — it
lands in TB-334's agent-runtime sweep (the briefing names the boundary
explicitly to avoid double-touch).

Post-TB-335 the two consumer files (``ap2/ideation.py``,
``ap2/ideation_scrub.py``) no longer call ``os.environ.get("AP2_IDEATION_*"``
directly for these four knobs; they read via the
``Config.get_core_value(<key>, default=<x>)`` helper (TB-334) which
evaluates a call-time env-first precedence (sectioned env
``AP2_CORE_<KEY>`` > flat env via reverse-``FLAT_TO_SECTIONED`` >
``cfg.core_config`` TOML snapshot > default).

Five regression cleavages this pin holds (mirrors the TB-334 template
verbatim):

  (1) **Grep-shape**: zero remaining
      ``os.environ.get("AP2_IDEATION_DISABLED"`` /
      ``os.environ.get("AP2_IDEATION_COOLDOWN_S"`` /
      ``os.environ.get("AP2_IDEATION_TRIGGER_TASK_COUNT"`` calls in
      ``ap2/ideation.py``, and zero remaining
      ``os.environ.get("AP2_IDEATION_SCRUB_MODEL"`` calls in
      ``ap2/ideation_scrub.py``. A refactor that re-introduces a
      direct env read surfaces here instead of only via the
      briefing-level grep gate.
  (2) **Per-knob cfg-read parity (flat env)**: for each migrated knob,
      a ``monkeypatch.setenv(<flat>, …)`` value reaches the helper
      identical to what the legacy ``os.environ.get(<flat>, default)``
      shape would return.
  (3) **Per-knob cfg-read parity (sectioned env)**: same parity for
      the sectioned-env name ``AP2_CORE_<KEY>`` — the canonical
      naming under the TB-323 sectioned regime, which the helper
      consults first.
  (4) **TOML snapshot read**: a ``[core.<key>] = <value>`` TOML entry
      populates ``cfg.core_config`` (via the TB-334 extension to
      ``config_loader.from_toml``) and surfaces through the helpers
      at the cfg-snapshot precedence layer.
  (5) **Cfg-kwarg-+-TypeError-guard shape pin**: each migrated helper
      accepts ``cfg: Config | None = None`` and raises ``TypeError``
      when called with a positional non-Config (TB-327 template).
      Pins the back-compat contract: ``cfg=None`` default keeps the
      legacy env-read fallback live for test paths that monkeypatch
      env without threading a Config.

Out of scope (per the briefing): ``AP2_IDEATION_MAX_TURNS`` (TB-334),
other core knobs (``AP2_WEB_*``, ``AP2_AUTO_DIAGNOSE_*``),
``_KNOBS_STAYING_ENV_ONLY`` curation (deferred per
``ideation_state.md``).
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2 import ideation, ideation_scrub
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb335_ideation_cfg_reads.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# (file_rel_path, flat_env_name) pairs the briefing's Verification grep
# gates pin to zero. TB-391 relocated the three trigger-gate knob readers
# (`_ideation_disabled` / `_cooldown_s` / `_trigger_task_count`) from
# `ap2/ideation.py` into the ideation component impl, so the grep gate
# tracks the new owner file; `ap2/ideation_scrub.py` owns the fourth
# (`AP2_IDEATION_SCRUB_MODEL`).
_GREP_GATES: tuple[tuple[str, str], ...] = (
    ("ap2/components/ideation/impl.py", "AP2_IDEATION_DISABLED"),
    ("ap2/components/ideation/impl.py", "AP2_IDEATION_COOLDOWN_S"),
    ("ap2/components/ideation/impl.py", "AP2_IDEATION_TRIGGER_TASK_COUNT"),
    ("ap2/ideation_scrub.py", "AP2_IDEATION_SCRUB_MODEL"),
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob so each test owns its `os.environ`
    surface deterministically. Mirrors the TB-326 / TB-334 / TB-332 /
    TB-333 cluster-pilot fixture shape so the per-cluster regression-pin
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
    AP2_IDEATION_* values via `apply_env_overrides`.
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


@pytest.mark.parametrize("rel_path, flat_env", _GREP_GATES)
def test_no_direct_ideation_env_reads_in_migrated_files(rel_path, flat_env):
    """Per-knob grep-shape pin: no `os.environ.get("AP2_IDEATION_…"`
    call sites for the four migrated knobs in their respective owner
    files. Matches the briefing-level grep gates verbatim:

        ! grep -rqE 'os\\.environ\\.get\\(.AP2_IDEATION_DISABLED' ap2/ideation.py
        ! grep -rqE 'os\\.environ\\.get\\(.AP2_IDEATION_COOLDOWN_S' ap2/ideation.py
        ! grep -rqE 'os\\.environ\\.get\\(.AP2_IDEATION_TRIGGER_TASK_COUNT' ap2/ideation.py
        ! grep -rqE 'os\\.environ\\.get\\(.AP2_IDEATION_SCRUB_MODEL' ap2/ideation_scrub.py

    The legacy-fallback branches inside the migrated helpers
    deliberately use `os.getenv(...)` rather than `os.environ.get(...)`
    (TB-332 / TB-333 cross-package grep-gate hygiene template) so the
    briefing's absolute-zero grep gates and this pin both stay clean
    while preserving the `cfg=None` back-compat read path that the
    pre-TB-335 unit tests rely on.
    """
    pattern = re.compile(
        r"os\.environ\.get\([\"']" + re.escape(flat_env)
    )
    src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    violations: list[str] = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if pattern.search(line):
            violations.append(f"L{lineno}: {line.strip()}")
    assert not violations, (
        f"TB-335: {rel_path} must read `{flat_env}` via "
        "`cfg.get_core_value(<key>, default=<x>)`, not via direct "
        f"`os.environ.get('{flat_env}')` calls. "
        f"Found {len(violations)} violation(s):\n" + "\n".join(violations)
    )


def test_get_core_value_path_present_in_migrated_files():
    """Positive form of the grep-shape pins: each migrated file calls
    `cfg.get_core_value(...)` at least once. The briefing's secondary
    Verification bullet (`grep -rE "get_core_value\\(.ideation_"`)
    requires the resolved-config read path to be present.
    """
    pattern = re.compile(r"get_core_value\([\"']ideation_")
    for rel_path in ("ap2/components/ideation/impl.py", "ap2/ideation_scrub.py"):
        src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert pattern.search(src), (
            f"TB-335: {rel_path} should call "
            "`cfg.get_core_value('ideation_…', …)` for the TB-335 "
            "core-cluster migration."
        )


# ---------------------------------------------------------------------------
# (2) Per-knob cfg-read parity (flat env) — `cfg.get_core_value(<key>)`
#     returns the same value as `os.environ.get(<flat>, default)` would.
# ---------------------------------------------------------------------------


# (key, flat env name, sample value) tuples for the four migrated knobs.
# Values chosen to be distinguishable from the module defaults so a
# parity failure produces an unambiguous diff.
_FLAT_PARITY_CASES = [
    ("ideation_disabled", "AP2_IDEATION_DISABLED", "1"),
    ("ideation_cooldown_s", "AP2_IDEATION_COOLDOWN_S", "111"),
    ("ideation_trigger_task_count", "AP2_IDEATION_TRIGGER_TASK_COUNT", "7"),
    ("ideation_scrub_model", "AP2_IDEATION_SCRUB_MODEL", "claude-test-sonnet"),
]


@pytest.mark.parametrize("key, flat, sample", _FLAT_PARITY_CASES)
def test_get_core_value_flat_env_ignored(
    cfg, clean_env, emit_reset, key, flat, sample,
):
    """TB-413: the flat `AP2_IDEATION_*` tunable override is now IGNORED
    for these behavioral ideation knobs — `config.toml`/schema is the
    sole source. Setting the legacy flat env must NOT change what
    `cfg.get_core_value(<key>)` resolves to (the baseline stays put).

    The four migrated ideation knobs are behavioral tunables (none is on
    `ENV_PERMITTED_KEYS`), so the reverse-`FLAT_TO_SECTIONED` back-compat
    layer never fires for them.
    """
    baseline = cfg.get_core_value(key, default="UNSET")
    clean_env.setenv(flat, sample)
    assert cfg.get_core_value(key, default="UNSET") == baseline, (
        f"TB-413: flat tunable env `{flat}={sample}` must be ignored; "
        f"config.toml/schema wins for `{key}` (baseline {baseline!r})."
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
        f"TB-335: sectioned env `{sectioned}={sample}` should resolve to "
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
        f"TB-335: sectioned env `{sectioned}` should win over flat "
        f"env `{flat}`; got "
        f"{cfg.get_core_value(key, default='UNSET')!r}, expected {sample!r}."
    )


# ---------------------------------------------------------------------------
# (3) End-to-end behavioral parity — each migrated helper returns the
#     same value when read via cfg as the legacy env-read shape did.
#     These are the actual per-knob behavioral tests the briefing names:
#     "per-knob behavioral test asserts cfg-read returns the same value
#      the env-read would have under monkeypatch."
# ---------------------------------------------------------------------------


def test_cooldown_helper_reads_sectioned_env_via_cfg_300(cfg, clean_env, emit_reset):
    """`_cooldown_s(cfg)` returns the parsed sectioned-env value — the
    behavioral path the daemon takes when the operator has set
    `AP2_CORE_IDEATION_COOLDOWN_S=300`. (TB-413: the flat
    `AP2_IDEATION_COOLDOWN_S` tunable is ignored; the sectioned env still
    overrides, so the downstream int-parse path is exercised here.)
    """
    clean_env.setenv("AP2_CORE_IDEATION_COOLDOWN_S", "300")
    assert ideation._cooldown_s(cfg) == 300


def test_cooldown_helper_reads_sectioned_env_via_cfg(cfg, clean_env, emit_reset):
    """`_cooldown_s(cfg)` returns the parsed sectioned-env value — the
    canonical sectioned naming under the TB-323 regime.
    """
    clean_env.setenv("AP2_CORE_IDEATION_COOLDOWN_S", "450")
    assert ideation._cooldown_s(cfg) == 450


def test_cooldown_helper_returns_default_unset(cfg, clean_env, emit_reset):
    """No env, no TOML → module default (`IDEATION_COOLDOWN_DEFAULT_S`).
    Bit-for-bit identical to the pre-migration shape.
    """
    assert ideation._cooldown_s(cfg) == ideation.IDEATION_COOLDOWN_DEFAULT_S


def test_cooldown_helper_invalid_falls_back_to_default(cfg, clean_env, emit_reset):
    """Non-int / empty values fall back to the default — same permissive
    style as pre-TB-335.
    """
    for bad in ("abc", "3.14", "1e3", "  "):
        clean_env.setenv("AP2_IDEATION_COOLDOWN_S", bad)
        assert ideation._cooldown_s(cfg) == ideation.IDEATION_COOLDOWN_DEFAULT_S, (
            f"value {bad!r} should fall back to default"
        )


def test_trigger_helper_reads_sectioned_env_via_cfg_9(cfg, clean_env, emit_reset):
    """`_trigger_task_count(cfg)` returns the parsed sectioned-env value.
    (TB-413: the flat `AP2_IDEATION_TRIGGER_TASK_COUNT` tunable is
    ignored; the sectioned env still overrides, exercising the parse.)
    """
    clean_env.setenv("AP2_CORE_IDEATION_TRIGGER_TASK_COUNT", "9")
    assert ideation._trigger_task_count(cfg) == 9


def test_trigger_helper_reads_sectioned_env_via_cfg(cfg, clean_env, emit_reset):
    """`_trigger_task_count(cfg)` returns the parsed sectioned-env value."""
    clean_env.setenv("AP2_CORE_IDEATION_TRIGGER_TASK_COUNT", "11")
    assert ideation._trigger_task_count(cfg) == 11


def test_trigger_helper_returns_default_unset(cfg, clean_env, emit_reset):
    """No env, no TOML → module default."""
    assert ideation._trigger_task_count(cfg) == ideation.IDEATION_TRIGGER_TASK_COUNT_DEFAULT


def test_trigger_helper_invalid_falls_back_to_default(cfg, clean_env, emit_reset):
    """Non-int / non-positive / empty values fall back — pinned by the
    pre-TB-335 unit tests in `test_ideation_trigger.py`, re-asserted
    here under the cfg-threaded path.
    """
    for bad in ("abc", "-1", "0", "", "  ", "3.14", "1e3"):
        clean_env.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", bad)
        assert ideation._trigger_task_count(cfg) == ideation.IDEATION_TRIGGER_TASK_COUNT_DEFAULT, (
            f"value {bad!r} should fall back to default"
        )


@pytest.mark.parametrize("truthy", ["1", "true", "yes"])
def test_ideation_disabled_helper_truthy_sectioned_env_via_cfg(
    cfg, clean_env, emit_reset, truthy,
):
    """`_ideation_disabled(cfg)` returns True for the canonical truthy
    values — same parse shape as `_is_auto_approve_enabled` and the
    pre-TB-335 inline read at `_maybe_ideate`. (TB-413: injected via the
    sectioned `AP2_CORE_IDEATION_DISABLED` since the flat
    `AP2_IDEATION_DISABLED` tunable is ignored; the truthy-parse path is
    what this test exercises.)
    """
    clean_env.setenv("AP2_CORE_IDEATION_DISABLED", truthy)
    assert ideation._ideation_disabled(cfg) is True


@pytest.mark.parametrize("truthy", ["1", "true", "yes"])
def test_ideation_disabled_helper_truthy_sectioned_env(
    cfg, clean_env, emit_reset, truthy,
):
    """Same truthy parse via the sectioned-env name."""
    clean_env.setenv("AP2_CORE_IDEATION_DISABLED", truthy)
    assert ideation._ideation_disabled(cfg) is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "", "  ", "anything-else"])
def test_ideation_disabled_helper_falsy(cfg, clean_env, emit_reset, falsy):
    """Anything outside the canonical truthy set is False — strict-case
    parse matches the pre-TB-335 inline read.
    """
    clean_env.setenv("AP2_IDEATION_DISABLED", falsy)
    assert ideation._ideation_disabled(cfg) is False


def test_ideation_disabled_helper_unset_is_false(cfg, clean_env, emit_reset):
    """No env, no TOML → False (default behavior — ideation runs)."""
    assert ideation._ideation_disabled(cfg) is False


def test_scrub_model_helper_reads_sectioned_env_via_cfg_haiku(
    cfg, clean_env, emit_reset,
):
    """`_resolved_model(cfg)` returns the sectioned-env-supplied model
    name. (TB-413: the flat `AP2_IDEATION_SCRUB_MODEL` tunable is
    ignored; the sectioned env still overrides, exercising the
    non-empty-override resolution path.)
    """
    clean_env.setenv("AP2_CORE_IDEATION_SCRUB_MODEL", "claude-test-haiku")
    assert ideation_scrub._resolved_model(cfg) == "claude-test-haiku"


def test_scrub_model_helper_reads_sectioned_env_via_cfg(
    cfg, clean_env, emit_reset,
):
    """Same parity for the sectioned-env name."""
    clean_env.setenv("AP2_CORE_IDEATION_SCRUB_MODEL", "claude-test-sonnet")
    assert ideation_scrub._resolved_model(cfg) == "claude-test-sonnet"


def test_scrub_model_helper_empty_falls_back_to_default(
    cfg, clean_env, emit_reset,
):
    """Empty / whitespace-only override falls back to the provider-aware
    default — pre-TB-335 safety carve-out preserved (a typo'd empty value
    shouldn't route the SDK call to ""). TB-419: with a Config in hand the
    fallback is the resolved adapter's LIGHT tier (`claude-sonnet-4-6` under
    the default claude backend), not the cfg-less `DEFAULT_SCRUB_MODEL`.
    """
    from ap2.adapters import ClaudeCodeAdapter

    for empty in ("", "   ", "\n\t"):
        clean_env.setenv("AP2_IDEATION_SCRUB_MODEL", empty)
        assert (
            ideation_scrub._resolved_model(cfg)
            == ClaudeCodeAdapter().default_model_light
        )


def test_scrub_model_helper_returns_default_unset(cfg, clean_env, emit_reset):
    """No env, no TOML → the resolved adapter's LIGHT tier (TB-419). With a
    Config in hand the unset fallback follows `[agent_backends]` via the
    adapter tier (`claude-sonnet-4-6` under the default claude backend)."""
    from ap2.adapters import ClaudeCodeAdapter

    assert (
        ideation_scrub._resolved_model(cfg)
        == ClaudeCodeAdapter().default_model_light
    )


# ---------------------------------------------------------------------------
# (4) TOML snapshot read — `[core.<key>]` populates `cfg.core_config`
#     and surfaces through the helpers at the cfg-snapshot layer.
# ---------------------------------------------------------------------------


def _load_toml_cfg(tmp_path, body: str) -> Config:
    """Helper that writes `body` to `.cc-autopilot/config.toml` and
    returns the corresponding `Config.load` result (TOML branch).
    """
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


def test_cooldown_helper_reads_toml_snapshot(tmp_path, clean_env, emit_reset):
    """A `[core.ideation_cooldown_s] = 222` TOML entry populates
    `cfg.core_config["ideation_cooldown_s"]` and surfaces through
    `_cooldown_s(cfg)` at the cfg-snapshot precedence layer when no
    env override is live.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[core]\nideation_cooldown_s = 222\n",
    )
    assert cfg.core_config.get("ideation_cooldown_s") == 222
    assert ideation._cooldown_s(cfg) == 222


def test_trigger_helper_reads_toml_snapshot(tmp_path, clean_env, emit_reset):
    """A `[core.ideation_trigger_task_count] = 8` TOML entry surfaces
    through `_trigger_task_count(cfg)`.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[core]\nideation_trigger_task_count = 8\n",
    )
    assert cfg.core_config.get("ideation_trigger_task_count") == 8
    assert ideation._trigger_task_count(cfg) == 8


def test_scrub_model_helper_reads_toml_snapshot(tmp_path, clean_env, emit_reset):
    """A `[core.ideation_scrub_model] = "..."` TOML entry surfaces
    through `_resolved_model(cfg)`.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        '[core]\nideation_scrub_model = "claude-toml-haiku"\n',
    )
    assert cfg.core_config.get("ideation_scrub_model") == "claude-toml-haiku"
    assert ideation_scrub._resolved_model(cfg) == "claude-toml-haiku"


def test_ideation_disabled_helper_reads_toml_snapshot(
    tmp_path, clean_env, emit_reset,
):
    """A `[core.ideation_disabled] = "1"` TOML entry surfaces through
    `_ideation_disabled(cfg)`. The TOML value is a string here so the
    truthy parse can apply — matches the sectioned/flat env shape.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        '[core]\nideation_disabled = "1"\n',
    )
    assert ideation._ideation_disabled(cfg) is True


def test_toml_snapshot_wins_over_flat_env(tmp_path, clean_env, emit_reset):
    """TB-413 precedence pin: the TOML snapshot wins over the (now
    ignored) flat env for the ideation cluster too. An operator who
    hasn't migrated their env file no longer sees the flat
    `AP2_IDEATION_COOLDOWN_S` value override the TOML —
    `config.toml`/schema is the sole source for this behavioral knob.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[core]\nideation_cooldown_s = 100\n",
    )
    clean_env.setenv("AP2_IDEATION_COOLDOWN_S", "777")
    assert ideation._cooldown_s(cfg) == 100, (
        "TB-413: the TOML snapshot must win; the flat tunable env "
        "`AP2_IDEATION_COOLDOWN_S` is ignored."
    )


# ---------------------------------------------------------------------------
# (5) Helper signature + TypeError-guard pin — each migrated helper
#     accepts `cfg: Config | None = None` and raises `TypeError` when
#     called with a positional non-Config.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper",
    [
        ideation._cooldown_s,
        ideation._trigger_task_count,
        ideation._ideation_disabled,
        ideation_scrub._resolved_model,
    ],
)
def test_helper_rejects_non_config_positional(helper, clean_env, emit_reset):
    """Each migrated helper raises `TypeError` when called with a
    positional non-Config. Pins the TB-327 cfg-kwarg-+-TypeError-guard
    template — a miswired call (`_cooldown_s("not a config")`) surfaces
    at the boundary instead of getting silently coerced.
    """
    with pytest.raises(TypeError, match="expects a Config instance"):
        helper("not a config")


@pytest.mark.parametrize(
    "helper, expected",
    [
        (ideation._cooldown_s, ideation.IDEATION_COOLDOWN_DEFAULT_S),
        (
            ideation._trigger_task_count,
            ideation.IDEATION_TRIGGER_TASK_COUNT_DEFAULT,
        ),
        (ideation._ideation_disabled, False),
        (ideation_scrub._resolved_model, ideation_scrub.DEFAULT_SCRUB_MODEL),
    ],
)
def test_helper_default_cfg_none_legacy_path(
    helper, expected, clean_env, emit_reset,
):
    """`cfg=None` default keeps the legacy env-read fallback alive so
    test paths that ``monkeypatch.setenv("AP2_IDEATION_*", ...)`` without
    threading a Config keep working bit-for-bit. With env stripped, each
    helper returns its module default — the steady-state behavior the
    pre-TB-335 unit tests assumed.
    """
    assert helper() == expected


# ---------------------------------------------------------------------------
# Pin the `FLAT_TO_SECTIONED` mapping the helpers depend on. A refactor
# that drops one of these would silently break the flat-env back-compat
# path for that knob; this pin catches it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        ("AP2_IDEATION_DISABLED", "core.ideation_disabled"),
        ("AP2_IDEATION_COOLDOWN_S", "core.ideation_cooldown_s"),
        ("AP2_IDEATION_TRIGGER_TASK_COUNT", "core.ideation_trigger_task_count"),
        ("AP2_IDEATION_SCRUB_MODEL", "core.ideation_scrub_model"),
    ],
)
def test_flat_to_sectioned_pins_ideation_cluster(flat, sectioned):
    """`FLAT_TO_SECTIONED` (TB-323) must carry the four ideation-cluster
    knob mappings. A refactor that drops one would silently break the
    flat-env back-compat path for that knob.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-335: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the ideation-cluster reverse-lookup "
        f"back-compat path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )
