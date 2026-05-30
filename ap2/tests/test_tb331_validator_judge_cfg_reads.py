"""TB-331: validator_judge component reads via `cfg.components_config` (axis-5 cluster).

Long-tail-cluster sibling to TB-326's auto_approve pilot
(`test_tb326_auto_approve_cfg_reads.py`), TB-327's auto_unfreeze
follow-on (`test_tb327_auto_unfreeze_cfg_reads.py`), TB-328's
attention follow-on (`test_tb328_attention_cfg_reads.py`),
TB-329's focus_advance follow-on
(`test_tb329_focus_advance_cfg_reads.py`), and TB-330's janitor
follow-on (`test_tb330_janitor_cfg_reads.py`); applies the same five
regression cleavages to the four operator-tunable knobs the
validator_judge component logically owns:
`AP2_VALIDATOR_JUDGE_DISABLED`, `AP2_VALIDATOR_JUDGE_TIMEOUT_S`,
`AP2_VALIDATOR_JUDGE_MAX_TURNS`, and the deprecated
`AP2_VALIDATOR_JUDGE_MAX_TOKENS` alias (TB-249 sentinel,
ceiling-capped at 5).

The pre-TB-331 component body read these four knobs via direct
`os.environ.get(...)` calls inside `_check_dependency_coherence`
(L689 / L695 / L708 / L709 at HEAD~1); they now route through the
intra-package `_validator_judge_disabled(cfg)` /
`_validator_judge_timeout_s(cfg)` /
`_validator_judge_max_turns(cfg)` /
`_validator_judge_max_tokens_legacy(cfg)` helpers, which themselves
call `Config.get_component_value("validator_judge", <key>)`. The
latter evaluates sectioned env > flat env (via
reverse-`FLAT_TO_SECTIONED`) >
`cfg.components_config["validator_judge"][<key>]` > default at call
time, preserving the env-first semantics the operator-facing knob
contract has carried since TB-235.

The `noisy_threshold` knob (`AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` —
the fifth `FLAT_TO_SECTIONED` mapping under `components.validator_judge`)
is read in `ap2/automation_status.py`, NOT inside the component
subpackage. Its migration lives outside TB-331's scope per the
briefing's "Migrate every `os.environ.get(...)` call site in
`ap2/components/validator_judge/__init__.py` and `manifest.py`"
narrowing; the FLAT_TO_SECTIONED parametrized sanity pin below still
lists it so a refactor that drops the back-compat entry surfaces
here. The four-vs-five accounting is documented on the manifest's
TB-322 schema declaration.

Five regression cleavages this pin holds (mirror of TB-326 / 327 /
328 / 329 / 330):

  (1) **Grep-shape**: zero remaining
      `os.environ.get("AP2_VALIDATOR_JUDGE_<KNOB>")` call sites in
      `ap2/components/validator_judge/`. A refactor that
      re-introduces a direct env read here loses the back-compat
      layer and side-steps the structured-config precedence the
      operator depends on.
  (2) **TOML-first read path**: a `cfg.components_config` value
      populated from `config.toml` wins over the legacy flat env
      name once env-side overrides are unset — the operator's TOML
      becomes the authoritative source the moment they opt in.
  (3) **Flat-env back-compat**: a flat env name unaccompanied by a
      TOML value still resolves the same value the old direct
      env-read path did. The shell-export operator who never
      migrated `.cc-autopilot/env` sees zero observable change.
  (4) **Parser semantics preserved**: empty / non-int / non-float /
      missing / zero / negative values still default to the original
      sentinels (`_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT` = 60.0,
      `_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT` = 2,
      kill switch False unset, deprecated `max_tokens` alias
      ceiling-capped at `_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL` = 5).
  (5) **Chosen access shape published**: the manifest's docstring
      cites the chosen resolved-config access shape
      (`cfg.get_component_value`) with a TB-331 anchor so the
      cluster migration arc is fully auditable from one place.

Plus three integration pins:
  - `BriefingContext.cfg` is plumbed through `_validate_briefing_structure`
    into the manifest adapter so the live daemon path never touches
    the synthetic empty-Config back-compat fallback.
  - The `_briefing_validator` adapter resolves an empty Config for
    the legacy test path that doesn't supply one (the >18 existing
    `test_dep_validator_judge.py` / `test_tb247_*` / `test_tb316_*`
    tests use `monkeypatch.setenv(...)` + no Config — env-first
    precedence preserves their semantics).
  - End-to-end kill-switch behavior through the cfg-routed
    `_validator_judge_disabled(cfg)` read — pins the operator-facing
    `AP2_VALIDATOR_JUDGE_DISABLED=1` kill-switch contract.

Why this matters: axis (5)'s long tail (per goal.md L353-364) sets
"≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads" as the Progress signal at L398-399.
TB-326 / 327 / 328 / 329 / 330 landed the previous clusters; this
cluster closes the validator_judge surface.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from ap2.briefing_validators import BriefingContext
from ap2.components import validator_judge as vj
from ap2.components.validator_judge.manifest import (
    _briefing_validator,
    _empty_cfg_for_back_compat,
)
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb331_validator_judge_cfg_reads.py -> repo/
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
    Also reset the validator_judge per-process deprecated-knob log so
    the `AP2_VALIDATOR_JUDGE_MAX_TOKENS` warning fires fresh per test.
    """
    reset_env_deprecated_emit_for_tests()
    vj._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()
    yield
    reset_env_deprecated_emit_for_tests()
    vj._VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.clear()


def _load_toml_cfg(tmp_path, body: str) -> Config:
    """Helper that writes `body` to `.cc-autopilot/config.toml` and
    returns the corresponding `Config.load` result (TOML branch).
    Caller is responsible for stripping `AP2_*` env vars BEFORE
    invoking this — the helper itself does not touch `os.environ`.
    """
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


# ---------------------------------------------------------------------------
# (1) Grep-shape — zero remaining `os.environ.get("AP2_VALIDATOR_JUDGE_*")`
#     call sites in the component body.
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_validator_judge_component():
    """The grep-shape Verification bullet, pinned to source so a refactor
    that re-introduces a direct env read inside the component body
    surfaces here instead of only via the briefing-level grep gate.

    The component package is `ap2/components/validator_judge/` (both
    `__init__.py` and `manifest.py`); the test reads each `.py` file in
    the package and rejects any literal
    `os.environ.get("AP2_VALIDATOR_JUDGE_*"` fragment. Comments /
    docstrings that QUOTE the old call sites for historical context are
    allowed iff they DON'T form a valid call statement — the pattern
    below matches only the bare call shape (the briefing-level grep's
    own anchor), so a backticked-in-docstring mention that breaks the
    literal does NOT match.
    """
    pattern = re.compile(
        r"os\.environ\.get\([\"']AP2_VALIDATOR_JUDGE_"
    )
    component_dir = _REPO_ROOT / "ap2/components/validator_judge"
    violations: list[str] = []
    for py_path in sorted(component_dir.rglob("*.py")):
        src = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = py_path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-331: the validator_judge component body must read its four "
        "operator-tunable knobs via `cfg.get_component_value(...)`, "
        "not via direct `os.environ.get('AP2_VALIDATOR_JUDGE_…')` "
        f"calls. Found {len(violations)} violation(s):\n"
        + "\n".join(violations)
    )


def test_cfg_get_component_value_path_present_in_component_body():
    """Positive form of the grep-shape pin: the component body
    documents+uses the chosen `cfg.get_component_value` resolved-
    config access shape. A refactor that swaps the helper out for
    something else (e.g. inlining `cfg.components_config[...]`)
    surfaces here so the documented TB-326 pilot pattern stays the
    canonical template for the cluster.
    """
    # TB-343: the body (with its cfg.get_component_value calls) moved to impl.py.
    init_src = (
        _REPO_ROOT / "ap2/components/validator_judge/impl.py"
    ).read_text(encoding="utf-8")
    assert "cfg.get_component_value" in init_src, (
        "TB-331: the validator_judge component body should use "
        "`cfg.get_component_value(...)` to resolve the four migrated "
        "knobs (per the TB-326 pilot's chosen access shape — see "
        "the auto_approve manifest docstring and the validator_judge "
        "manifest's TB-331 doc block)."
    )


def test_validator_judge_no_os_import_in_init():
    """TB-331: `import os` should no longer appear in the component
    body's `__init__.py` — the four env reads (`DISABLED` /
    `TIMEOUT_S` / `MAX_TURNS` / `MAX_TOKENS`) were the only `os` use,
    so dropping the import is a defensive check that no other env read
    has snuck in.
    """
    # TB-343: the body moved to impl.py; the `import os` absence pin tracks
    # the body, so read impl.py (the __init__.py shim is re-export-only).
    src = (
        _REPO_ROOT / "ap2/components/validator_judge/impl.py"
    ).read_text(encoding="utf-8")
    # Look for line-anchored bare `import os` (not `from os import …`
    # which is a separate path we'd want to flag too).
    pattern = re.compile(r"^import os\b", re.MULTILINE)
    assert not pattern.search(src), (
        "TB-331: the validator_judge component body should not import "
        "`os` post-migration; the four env reads route through "
        "`cfg.get_component_value(...)` instead. A re-introduced "
        "`import os` is a leading indicator that an env-read crept "
        "back in."
    )


# ---------------------------------------------------------------------------
# (2) TOML-first read path — cfg.components_config wins over flat env.
# ---------------------------------------------------------------------------


def test_disabled_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.validator_judge] disabled = true` TOML value
    populates `cfg.components_config["validator_judge"]["disabled"]`,
    which the helper reads via `cfg.get_component_value`. The legacy
    flat env name is UNSET; the helper returns True from the TOML
    layer (no env fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.validator_judge]\ndisabled = true\n",
    )
    assert (
        cfg.components_config["validator_judge"]["disabled"] is True
    )
    assert vj._validator_judge_disabled(cfg) is True


def test_timeout_s_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.validator_judge] timeout_s = 30.0` TOML value
    flows through to the helper's float return value (no env
    fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.validator_judge]\ntimeout_s = 30.0\n",
    )
    assert cfg.components_config["validator_judge"]["timeout_s"] == 30.0
    assert vj._validator_judge_timeout_s(cfg) == 30.0


def test_max_turns_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.validator_judge] max_turns = 4` TOML value
    flows through to the helper's int return value (no env
    fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.validator_judge]\nmax_turns = 4\n",
    )
    assert cfg.components_config["validator_judge"]["max_turns"] == 4
    assert vj._validator_judge_max_turns(cfg) == 4


def test_max_tokens_legacy_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.validator_judge] max_tokens = 3` TOML value (the
    TB-249 deprecated alias) flows through the legacy helper's int
    return value. The caller (`_check_dependency_coherence`) then
    treats the value as a `max_turns` override ceiling-capped at
    `_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL=5`.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.validator_judge]\nmax_tokens = 3\n",
    )
    assert cfg.components_config["validator_judge"]["max_tokens"] == 3
    assert vj._validator_judge_max_tokens_legacy(cfg) == 3


# ---------------------------------------------------------------------------
# (3) Flat-env back-compat — same value the legacy env-read path returned.
# ---------------------------------------------------------------------------


def test_disabled_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_VALIDATOR_JUDGE_DISABLED=1` set on an env-only project (no
    TOML-side override) still resolves to True via the
    `Config.get_component_value` reverse-`FLAT_TO_SECTIONED` lookup.
    Pins the back-compat path the shell-export operator depends on.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")
    assert vj._validator_judge_disabled(cfg) is True


def test_disabled_flat_env_truthy_variants(cfg, clean_env, emit_reset):
    """The pre-TB-331 truthy enumeration (`"1"` / `"true"` / `"yes"`,
    case-insensitive) is preserved through the cfg-routed read."""
    for truthy in ("1", "true", "TRUE", "yes", "YES", "True"):
        clean_env.setenv("AP2_VALIDATOR_JUDGE_DISABLED", truthy)
        assert vj._validator_judge_disabled(cfg) is True, truthy


def test_disabled_flat_env_falsy_variants(cfg, clean_env, emit_reset):
    """Falsy env values (empty, `0`, `false`, garbage) leave the
    kill switch off — bit-for-bit identical to the pre-TB-331
    `os.environ.get(...).lower() in {"1","true","yes"}` shape.
    """
    for falsy in ("0", "false", "no", "off", "garbage", ""):
        clean_env.setenv("AP2_VALIDATOR_JUDGE_DISABLED", falsy)
        assert vj._validator_judge_disabled(cfg) is False, falsy


def test_timeout_s_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_VALIDATOR_JUDGE_TIMEOUT_S=42` on an env-only project
    resolves to 42.0 via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_TIMEOUT_S", "42")
    assert vj._validator_judge_timeout_s(cfg) == 42.0


def test_max_turns_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_VALIDATOR_JUDGE_MAX_TURNS=4` on an env-only project
    resolves to 4 via the flat-env back-compat path.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", "4")
    assert vj._validator_judge_max_turns(cfg) == 4


def test_max_tokens_legacy_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_VALIDATOR_JUDGE_MAX_TOKENS=3` on an env-only project
    resolves to 3 via the flat-env back-compat path (the deprecated
    alias helper). The caller still ceiling-caps at 5.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", "3")
    assert vj._validator_judge_max_tokens_legacy(cfg) == 3


# ---------------------------------------------------------------------------
# (4) Parser semantics preserved — default-on-bad-value pins.
# ---------------------------------------------------------------------------


def test_disabled_unset_defaults_to_false(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → kill switch False. Same default
    the pre-migration env-only path returned for the unset case.
    """
    assert vj._validator_judge_disabled(cfg) is False


def test_timeout_s_unset_defaults_to_sixty(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_validator_judge_timeout_s`
    returns the in-source default (60.0 post-TB-269).
    """
    assert (
        vj._validator_judge_timeout_s(cfg)
        == vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
        == 60.0
    )


def test_timeout_s_garbage_defaults_to_sixty(cfg, clean_env, emit_reset):
    """Non-float env value → default 60.0. Pins the parser-fallback
    shape the pre-migration `try: float(raw) except ValueError`
    chain enforced.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_TIMEOUT_S", "garbage")
    assert (
        vj._validator_judge_timeout_s(cfg)
        == vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    )


def test_timeout_s_empty_defaults_to_sixty(cfg, clean_env, emit_reset):
    """Empty env value (set but blank) → default 60.0. Pins the
    `raw == ""` guard the pre-migration `os.environ.get(...) or default`
    chain enforced.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_TIMEOUT_S", "")
    assert (
        vj._validator_judge_timeout_s(cfg)
        == vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    )


def test_max_turns_unset_returns_none(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_validator_judge_max_turns`
    returns `None` (sentinel for "canonical knob not set", lets the
    caller fall through to the deprecated `max_tokens` alias path).
    """
    assert vj._validator_judge_max_turns(cfg) is None


def test_max_turns_garbage_returns_none(cfg, clean_env, emit_reset):
    """Non-int env value → `None` (sentinel for the alias-fallback
    branch). Mirrors the pre-migration `except ValueError: pass`
    branch that left `max_turns` at the module default.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", "garbage")
    assert vj._validator_judge_max_turns(cfg) is None


def test_max_turns_zero_returns_none(cfg, clean_env, emit_reset):
    """Explicit `0` env value → `None`. Same behavior as the pre-
    migration `if parsed > 0` guard: zero / negative budgets fall
    through to the alias path, NOT a zero-turn SDK call.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_MAX_TURNS", "0")
    assert vj._validator_judge_max_turns(cfg) is None


def test_max_tokens_legacy_unset_returns_zero(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_validator_judge_max_tokens_legacy`
    returns `0` (sentinel for "alias not set"). The caller treats `0`
    as "fall through to the module default".
    """
    assert vj._validator_judge_max_tokens_legacy(cfg) == 0


def test_max_tokens_legacy_garbage_returns_zero(cfg, clean_env, emit_reset):
    """Non-int env value → `0` (sentinel for alias not set). Mirrors
    the pre-migration `legacy_val = 0` branch that left the deprecated
    alias path inactive.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_MAX_TOKENS", "garbage")
    assert vj._validator_judge_max_tokens_legacy(cfg) == 0


# ---------------------------------------------------------------------------
# (5) Chosen access shape published — manifest docstring cites it.
# ---------------------------------------------------------------------------


def test_manifest_documents_chosen_access_shape():
    """The validator_judge manifest documents (top-of-file docstring)
    the chosen resolved-config access shape so the cluster migration
    arc reads the same pattern from one more place. Looks for the
    `cfg.get_component_value` call shape + a TB-331 reference. Loose
    enough that a docstring rewrite doesn't false-positive; strict
    enough that an accidental documentation drop fires.
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/validator_judge/manifest.py"
    ).read_text(encoding="utf-8")
    assert "TB-331" in manifest_src, (
        "TB-331: the validator_judge manifest must cite the TB-331 "
        "axis-5 cluster anchor so the cluster migration arc is fully "
        "auditable from one place."
    )
    assert "cfg.get_component_value" in manifest_src, (
        "TB-331: the validator_judge manifest must name the chosen "
        "resolved-config access shape (`cfg.get_component_value`) so "
        "the cluster migration arc adopts the same pattern verbatim."
    )


# ---------------------------------------------------------------------------
# Sanity: the five validator_judge knobs are listed in FLAT_TO_SECTIONED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        (
            "AP2_VALIDATOR_JUDGE_DISABLED",
            "components.validator_judge.disabled",
        ),
        (
            "AP2_VALIDATOR_JUDGE_MAX_TOKENS",
            "components.validator_judge.max_tokens",
        ),
        (
            "AP2_VALIDATOR_JUDGE_MAX_TURNS",
            "components.validator_judge.max_turns",
        ),
        (
            "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
            "components.validator_judge.noisy_threshold",
        ),
        (
            "AP2_VALIDATOR_JUDGE_TIMEOUT_S",
            "components.validator_judge.timeout_s",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_five_validator_judge_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops one of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it.

    The `noisy_threshold` knob is listed alongside the four migrated
    knobs even though its read flows through
    `ap2/automation_status.py` (not the component subpackage body) —
    the FLAT_TO_SECTIONED entry stays load-bearing for the
    `[components.validator_judge] noisy_threshold = N` TOML-side
    override path that the attention-detector aggregation surface
    will consume once it migrates in a future TB.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-331: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the validator_judge reverse-lookup "
        f"back-compat path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


# ---------------------------------------------------------------------------
# Sanity: the four migrated keys are declared in the manifest's
# config_schema (TB-322 schema pre-declared them; TB-331 keeps them
# pinned so a future revert doesn't silently weaken the back-compat
# surface).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema_key, expected_type, expected_default",
    [
        ("disabled", bool, False),
        ("timeout_s", float, 60.0),
        ("max_turns", int, 2),
        ("max_tokens", int, 500),
    ],
)
def test_manifest_config_schema_declares_each_migrated_knob(
    schema_key: str, expected_type: type, expected_default,
):
    """The validator_judge manifest's `config_schema` (TB-322) declares
    all four operator-tunable knobs the component body reads, so a
    TOML-opted operator can write `[components.validator_judge] <key>
    = <value>` for any of them without tripping `validate_config`'s
    reject-unknown-key path. Defaults + types match the in-source
    `_VALIDATOR_JUDGE_*_DEFAULT` sentinels.
    """
    from ap2.components.validator_judge.manifest import MANIFEST

    spec = MANIFEST.config_schema.get(schema_key)
    assert spec is not None, (
        f"TB-331: validator_judge manifest must declare `{schema_key}` "
        f"in config_schema; got: {sorted(MANIFEST.config_schema)}"
    )
    assert spec.type is expected_type, (
        f"TB-331: validator_judge.{schema_key}.type expected "
        f"{expected_type.__name__}, got {spec.type.__name__}"
    )
    assert spec.default == expected_default, (
        f"TB-331: validator_judge.{schema_key}.default expected "
        f"{expected_default!r}, got {spec.default!r}"
    )
    assert spec.description.strip(), (
        f"TB-331: validator_judge.{schema_key}.description must be "
        f"non-empty for axis-4 `ap2 config list` rendering."
    )


# ---------------------------------------------------------------------------
# BriefingContext.cfg threading — TB-331 added the cfg field so the
# manifest adapter resolves component knobs against the live Config.
# ---------------------------------------------------------------------------


def test_briefing_context_carries_cfg_field():
    """`BriefingContext.cfg` is the new TB-331 field; defaults to None
    so legacy call sites that don't supply one stay green. Frozen
    dataclass shape preserved.
    """
    ctx = BriefingContext(text="hello")
    assert ctx.cfg is None
    ctx_with_cfg = BriefingContext(text="hello", cfg=_empty_cfg_for_back_compat())
    assert ctx_with_cfg.cfg is not None
    # Mutation must still raise (the TB-316 frozen-dataclass contract).
    with pytest.raises(Exception):  # noqa: PT011 — FrozenInstanceError
        ctx_with_cfg.cfg = None  # type: ignore[misc]


def test_briefing_validator_adapter_uses_ctx_cfg_when_supplied():
    """The manifest's `_briefing_validator` adapter threads
    `ctx.cfg` into `_check_dependency_coherence`. Pin the threading
    by supplying a Config whose `components_config` carries
    `disabled = True` and asserting the adapter short-circuits via
    the cfg-routed kill switch (the legacy test path's
    `monkeypatch.setenv` is NOT set; the kill switch flows from the
    TOML layer).
    """
    cfg = Config.__new__(Config)
    cfg.components_config = {"validator_judge": {"disabled": True}}

    def _explode(**_kwargs):
        raise AssertionError(
            "cfg-routed kill switch should bypass the judge; the "
            "adapter should have short-circuited before reaching the "
            "judge stub"
        )

    ctx = BriefingContext(
        text="anything",
        events_file=pathlib.Path("/tmp/should-not-exist-tb331.jsonl"),
        dep_judge_fn=_explode,
        cfg=cfg,
    )
    result = _briefing_validator(ctx)
    assert result is None, result


def test_briefing_validator_adapter_synthesizes_cfg_when_missing(
    tmp_path, clean_env, emit_reset,
):
    """Legacy back-compat: when `ctx.cfg is None` (test paths that
    pre-date TB-331's plumbing), the adapter synthesizes an empty
    Config so `cfg.get_component_value`'s env-first precedence still
    works. Pin by setting `AP2_VALIDATOR_JUDGE_DISABLED=1` and
    asserting the synthetic-Config branch still honors the kill switch.
    """
    clean_env.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")

    def _explode(**_kwargs):
        raise AssertionError(
            "AP2_VALIDATOR_JUDGE_DISABLED=1 should bypass the judge; "
            "the synthetic-Config adapter branch should see the "
            "env-first lookup"
        )

    events_file = tmp_path / "events.jsonl"
    ctx = BriefingContext(
        text="anything",
        events_file=events_file,
        dep_judge_fn=_explode,
        # cfg=None by default — exercises the synthetic-Config branch.
    )
    result = _briefing_validator(ctx)
    assert result is None, result


def test_empty_cfg_for_back_compat_satisfies_get_component_value(clean_env):
    """`_empty_cfg_for_back_compat()` returns a Config-shaped object
    that satisfies the `cfg.get_component_value(...)` surface. Pin
    the synthetic-Config contract so a future refactor that
    accidentally drops the `components_config` attribute (the only
    Config field the resolver consults) surfaces immediately.

    Strip every `AP2_*` env knob first via `clean_env` so the
    project's `.cc-autopilot/env` (which may carry an
    `AP2_VALIDATOR_JUDGE_DISABLED=1` operator-set value in this
    repo or on CI) doesn't leak into the env-first precedence and
    short-circuit the assertion below.
    """
    cfg = _empty_cfg_for_back_compat()
    assert cfg.components_config == {}
    # Env-first precedence still works — the snapshot fallback is the
    # only branch the empty dict affects.
    assert (
        cfg.get_component_value("validator_judge", "disabled") is None
    )
    assert (
        cfg.get_component_value(
            "validator_judge", "disabled", default="fallback",
        ) == "fallback"
    )


def test_validate_briefing_structure_threads_cfg_through(tmp_path, clean_env, emit_reset):
    """End-to-end: `_validate_briefing_structure(..., cfg=cfg)` plumbs
    the Config through to the manifest adapter via `BriefingContext.cfg`.
    Pin the threading by supplying a Config whose TOML carries
    `disabled = true` and asserting the validator path short-circuits
    without calling the dep-judge stub.
    """
    from ap2.briefing_validators import _validate_briefing_structure
    from ap2.tests._briefing_fixtures import canonical_briefing

    cfg = _load_toml_cfg(
        tmp_path,
        "[components.validator_judge]\ndisabled = true\n",
    )

    def _explode(**_kwargs):
        raise AssertionError(
            "TB-331: cfg-routed kill switch should bypass the judge"
        )

    err = _validate_briefing_structure(
        canonical_briefing("TB-XYZ", title="cfg-threading smoke"),
        description="x",
        blocked_csv="",
        events_file=tmp_path / "events.jsonl",
        dep_judge_fn=_explode,
        cfg=cfg,
    )
    assert err is None
