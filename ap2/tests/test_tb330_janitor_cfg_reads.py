"""TB-330: janitor component reads via `cfg.components_config` (axis-5 cluster).

Long-tail-cluster sibling to TB-326's auto_approve pilot
(`test_tb326_auto_approve_cfg_reads.py`), TB-327's auto_unfreeze
follow-on (`test_tb327_auto_unfreeze_cfg_reads.py`), TB-328's
attention follow-on (`test_tb328_attention_cfg_reads.py`), and
TB-329's focus_advance follow-on
(`test_tb329_focus_advance_cfg_reads.py`); the same five regression
cleavages applied to the three operator-tunable per-judge knobs the
janitor component logically owns: `AP2_JANITOR_MAX_FINDINGS_LLM`,
`AP2_JANITOR_JUDGE_EFFORT`, `AP2_JANITOR_JUDGE_MAX_TURNS`. The
kill-switch `AP2_JANITOR_DISABLED` continues to flow through
`Manifest.is_enabled()`'s `env_flag` mechanism (in `ap2/registry.py`,
not inside the component body), so its grep-absence already holds by
construction — the FLAT_TO_SECTIONED entry is still pinned below so a
TOML-opted operator's `[components.janitor] disabled = true` keeps the
back-compat write path intact.

The migrated call sites used to read these via direct
`os.environ.get(...)` inside `_max_findings_llm` and `_judge_finding`;
they now route through the intra-package
`_max_findings_llm(cfg)` / `_judge_effort(cfg)` /
`_judge_max_turns(cfg)` helpers, which themselves call
`Config.get_component_value("janitor", <key>)`. The latter evaluates
sectioned env > flat env (via reverse-`FLAT_TO_SECTIONED`) >
`cfg.components_config["janitor"][<key>]` > default at call time.
Behavior preservation contract: every existing `AP2_JANITOR_*` flat-env
consumer (operator shell exports, `.cc-autopilot/env`) keeps today's
behavior bit-for-bit while a TOML-opted operator's
`[components.janitor]` values win transparently once env-side overrides
are unset.

Five regression cleavages this pin holds (mirror of TB-326/327/328/329):

  (1) **Grep-shape**: zero remaining
      `os.environ.get("AP2_JANITOR_<KNOB>")` call sites in
      `ap2/components/janitor/`. A refactor that re-introduces a
      direct env read here loses the back-compat layer and side-steps
      the structured-config precedence the operator depends on.
  (2) **TOML-first read path**: a `cfg.components_config` value
      populated from `config.toml` wins over the legacy flat env name
      once env-side overrides are unset — the operator's TOML becomes
      the authoritative source the moment they opt in.
  (3) **Flat-env back-compat**: a flat env name unaccompanied by a
      TOML value still resolves the same value the old direct
      env-read path did. The shell-export operator who never migrated
      `.cc-autopilot/env` sees zero observable change.
  (4) **Parser semantics preserved**: empty / non-int / non-positive /
      missing values still default to the original sentinels
      (`_AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT` = 10,
      `_AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT` = 12,
      `_judge_effort` falls back to `AP2_AGENT_EFFORT` → `"high"`).
  (5) **Chosen access shape published**: the manifest's docstring
      cites the chosen resolved-config access shape (`cfg.get_component_value`)
      with a TB-330 anchor so the cluster migration arc is fully
      auditable from one place.

Sanity pin: the four janitor knobs (incl. the kill switch) are listed
in `FLAT_TO_SECTIONED` mapping to `components.janitor.<key>`. Asserting
the four-row partition end-to-end catches a future refactor that drops
one of these mappings — which would silently break the flat-env
back-compat path for that knob.

Why this matters: axis (5)'s long tail (per goal.md L353-364) sets
"≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads" as the Progress signal at L398-399.
TB-326/327/328/329 landed the previous clusters; this cluster adds
the three per-judge knobs.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from ap2.components import janitor
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb330_janitor_cfg_reads.py -> repo/
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
    invoking this — the helper itself does not touch `os.environ`.
    """
    init_project(tmp_path)
    (tmp_path / ".cc-autopilot" / "config.toml").write_text(body)
    return Config.load(tmp_path)


# ---------------------------------------------------------------------------
# (1) Grep-shape — zero remaining `os.environ.get("AP2_JANITOR_*")`
#     call sites in the component body.
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_janitor_component():
    """The grep-shape Verification bullet, pinned to source so a refactor
    that re-introduces a direct env read inside the component body
    surfaces here instead of only via the briefing-level grep gate.

    The component package is `ap2/components/janitor/` (both
    `__init__.py` and `manifest.py`); the test reads each `.py` file in
    the package and rejects any literal `os.environ.get("AP2_JANITOR_*"`
    fragment. Comments / docstrings that QUOTE the old call sites for
    historical context are allowed iff they DON'T form a valid call
    statement — the pattern below matches only the bare call shape (the
    briefing-level grep's own anchor), so a backticked-in-docstring
    mention that breaks the literal does NOT match.
    """
    pattern = re.compile(
        r"os\.environ\.get\([\"']AP2_JANITOR_"
    )
    component_dir = _REPO_ROOT / "ap2/components/janitor"
    violations: list[str] = []
    for py_path in sorted(component_dir.rglob("*.py")):
        src = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = py_path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-330: the janitor component body must read its three "
        "operator-tunable per-judge knobs via "
        "`cfg.get_component_value(...)`, not via direct "
        "`os.environ.get('AP2_JANITOR_…')` calls. "
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
    # TB-343: the body (with its cfg.get_component_value calls) moved to impl.py.
    init_src = (
        _REPO_ROOT / "ap2/components/janitor/impl.py"
    ).read_text(encoding="utf-8")
    assert "cfg.get_component_value" in init_src, (
        "TB-330: the janitor component body should use "
        "`cfg.get_component_value(...)` to resolve the three migrated "
        "per-judge knobs (per the TB-326 pilot's chosen access shape "
        "— see the auto_approve manifest docstring and the janitor "
        "manifest's TB-330 doc block)."
    )


# ---------------------------------------------------------------------------
# (2) TOML-first read path — cfg.components_config wins over flat env.
# ---------------------------------------------------------------------------


def test_max_findings_llm_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.janitor] max_findings_llm = 7` TOML value
    populates `cfg.components_config["janitor"]["max_findings_llm"]`,
    which the helper reads via `cfg.get_component_value`. The legacy
    flat env name is UNSET; the helper returns 7 from the TOML layer
    (no env fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.janitor]\nmax_findings_llm = 7\n",
    )
    assert (
        cfg.components_config["janitor"]["max_findings_llm"] == 7
    )
    assert janitor._max_findings_llm(cfg) == 7


def test_judge_effort_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.janitor] judge_effort = "medium"` TOML value
    flows through to the helper's str return value (no env fallback
    fired)."""
    cfg = _load_toml_cfg(
        tmp_path,
        '[components.janitor]\njudge_effort = "medium"\n',
    )
    assert cfg.components_config["janitor"]["judge_effort"] == "medium"
    assert janitor._judge_effort(cfg) == "medium"


def test_judge_max_turns_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.janitor] judge_max_turns = 5` TOML value flows
    through to the helper's int return value."""
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.janitor]\njudge_max_turns = 5\n",
    )
    assert cfg.components_config["janitor"]["judge_max_turns"] == 5
    assert janitor._judge_max_turns(cfg) == 5


# ---------------------------------------------------------------------------
# (3) Flat-env back-compat — same value the legacy env-read path returned.
# ---------------------------------------------------------------------------


def test_max_findings_llm_flat_env_ignored(
    cfg, clean_env, emit_reset,
):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default).
    """
    baseline = janitor._max_findings_llm(cfg)  # flat unset
    clean_env.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "7")
    assert janitor._max_findings_llm(cfg) == baseline, (
        "TB-413: flat tunable env must be ignored; config.toml/schema wins"
    )


def test_judge_effort_flat_env_ignored(cfg, clean_env, emit_reset):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default).
    """
    baseline = janitor._judge_effort(cfg)  # flat unset
    clean_env.setenv("AP2_JANITOR_JUDGE_EFFORT", "medium")
    assert janitor._judge_effort(cfg) == baseline, (
        "TB-413: flat tunable env must be ignored; config.toml/schema wins"
    )


def test_judge_max_turns_flat_env_ignored(cfg, clean_env, emit_reset):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default).
    """
    baseline = janitor._judge_max_turns(cfg)  # flat unset
    clean_env.setenv("AP2_JANITOR_JUDGE_MAX_TURNS", "5")
    assert janitor._judge_max_turns(cfg) == baseline, (
        "TB-413: flat tunable env must be ignored; config.toml/schema wins"
    )


# ---------------------------------------------------------------------------
# (4) Parser semantics preserved — default-on-bad-value pins.
# ---------------------------------------------------------------------------


def test_max_findings_llm_unset_defaults_to_ten(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_max_findings_llm` returns the
    in-source default (10). Same default the pre-migration env-only
    path returned for the unset case.
    """
    assert (
        janitor._max_findings_llm(cfg)
        == janitor._AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT
        == 10
    )


def test_max_findings_llm_garbage_defaults_to_ten(
    cfg, clean_env, emit_reset,
):
    """Non-int env value → default 10. Pins the parser-fallback shape
    the pre-migration `try: int(raw) except ValueError: return default`
    chain enforced.
    """
    clean_env.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "garbage")
    assert (
        janitor._max_findings_llm(cfg)
        == janitor._AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT
    )


def test_max_findings_llm_empty_defaults_to_ten(cfg, clean_env, emit_reset):
    """Empty env value (set but blank) → default 10. Pins the
    `raw == ""` guard."""
    clean_env.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "")
    assert (
        janitor._max_findings_llm(cfg)
        == janitor._AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT
    )


def test_max_findings_llm_negative_clamps_to_zero(
    cfg, clean_env, emit_reset,
):
    """Negative env value clamps to 0 (disables the judge). Pins the
    `max(0, v)` clamp the pre-migration helper enforced.
    """
    clean_env.setenv("AP2_COMPONENTS_JANITOR_MAX_FINDINGS_LLM", "-5")
    assert janitor._max_findings_llm(cfg) == 0


def test_max_findings_llm_zero_explicit(cfg, clean_env, emit_reset):
    """Explicit 0 → 0 (judge disabled, deterministic-only fallback).
    Pins the "set to 0 to disable" contract the module docstring
    promises."""
    clean_env.setenv("AP2_COMPONENTS_JANITOR_MAX_FINDINGS_LLM", "0")
    assert janitor._max_findings_llm(cfg) == 0


def test_max_findings_llm_typed_int_from_toml(
    tmp_path, clean_env, emit_reset,
):
    """A TOML-typed int flows through verbatim (no string parse).
    Pins the `int(raw)` round-trip for the structured-config branch."""
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.janitor]\nmax_findings_llm = 3\n",
    )
    assert janitor._max_findings_llm(cfg) == 3


def test_judge_effort_unset_falls_back_to_agent_effort(
    cfg, clean_env, emit_reset,
):
    """Unset janitor-specific env + empty TOML + `AP2_AGENT_EFFORT=low`
    set → `_judge_effort` returns "low". Pins the inner-fallback
    behavior the pre-migration nested `os.environ.get(...,
    os.environ.get('AP2_AGENT_EFFORT', 'high'))` expression carried.
    """
    clean_env.setenv("AP2_CORE_AGENT_EFFORT", "low")
    assert janitor._judge_effort(cfg) == "low"


def test_judge_effort_all_unset_defaults_to_high(
    cfg, clean_env, emit_reset,
):
    """Unset janitor-specific env + empty TOML + unset
    `AP2_AGENT_EFFORT` → default "high". Pins the outer-fallback
    sentinel."""
    assert janitor._judge_effort(cfg) == "high"


def test_judge_effort_janitor_specific_wins_over_agent_effort(
    cfg, clean_env, emit_reset,
):
    """`AP2_JANITOR_JUDGE_EFFORT=medium` AND `AP2_AGENT_EFFORT=low`
    → janitor-specific knob wins ("medium"). Pins the precedence
    contract the pre-migration nested-env-get expression enforced
    (janitor-specific outer, agent-wide inner)."""
    clean_env.setenv("AP2_COMPONENTS_JANITOR_JUDGE_EFFORT", "medium")
    clean_env.setenv("AP2_CORE_AGENT_EFFORT", "low")
    assert janitor._judge_effort(cfg) == "medium"


def test_judge_max_turns_unset_defaults_to_twelve(
    cfg, clean_env, emit_reset,
):
    """Unset flat-env + empty TOML → `_judge_max_turns` returns the
    in-source default (12). Same default the pre-migration env-only
    path returned for the unset case."""
    assert (
        janitor._judge_max_turns(cfg)
        == janitor._AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT
        == 12
    )


def test_judge_max_turns_garbage_defaults_to_twelve(
    cfg, clean_env, emit_reset,
):
    """Non-int env value → default 12. Pins the parser-fallback shape
    the pre-migration `int(...)` chain implicitly enforced."""
    clean_env.setenv("AP2_JANITOR_JUDGE_MAX_TURNS", "garbage")
    assert (
        janitor._judge_max_turns(cfg)
        == janitor._AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT
    )


def test_judge_max_turns_empty_defaults_to_twelve(
    cfg, clean_env, emit_reset,
):
    """Empty env value (set but blank) → default 12. Pins the
    `raw == ""` guard."""
    clean_env.setenv("AP2_JANITOR_JUDGE_MAX_TURNS", "")
    assert (
        janitor._judge_max_turns(cfg)
        == janitor._AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT
    )


# ---------------------------------------------------------------------------
# (5) Chosen access shape published — manifest docstring cites it.
# ---------------------------------------------------------------------------


def test_manifest_documents_chosen_access_shape():
    """The janitor manifest documents (top-of-file docstring) the
    chosen resolved-config access shape so the cluster migration arc
    reads the same pattern from one more place. Looks for the
    `cfg.get_component_value` call shape + a TB-330 reference. Loose
    enough that a docstring rewrite doesn't false-positive; strict
    enough that an accidental documentation drop fires.
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/janitor/manifest.py"
    ).read_text(encoding="utf-8")
    assert "TB-330" in manifest_src, (
        "TB-330: the janitor manifest must cite the TB-330 axis-5 "
        "cluster anchor so the cluster migration arc is fully "
        "auditable from one place."
    )
    assert "cfg.get_component_value" in manifest_src, (
        "TB-330: the janitor manifest must name the chosen "
        "resolved-config access shape (`cfg.get_component_value`) so "
        "the cluster migration arc adopts the same pattern verbatim."
    )


# ---------------------------------------------------------------------------
# Sanity: the four janitor knobs are listed in FLAT_TO_SECTIONED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        (
            "AP2_JANITOR_DISABLED",
            "components.janitor.disabled",
        ),
        (
            "AP2_JANITOR_MAX_FINDINGS_LLM",
            "components.janitor.max_findings_llm",
        ),
        (
            "AP2_JANITOR_JUDGE_EFFORT",
            "components.janitor.judge_effort",
        ),
        (
            "AP2_JANITOR_JUDGE_MAX_TURNS",
            "components.janitor.judge_max_turns",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_four_janitor_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops one of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it. The kill
    switch `AP2_JANITOR_DISABLED` is listed alongside the three
    migrated per-judge knobs even though its read flows through
    `Manifest.is_enabled()` in `ap2/registry.py` (not the component
    body) — the FLAT_TO_SECTIONED entry stays load-bearing for the
    `[components.janitor] disabled = true` TOML-side override path.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-330: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the janitor reverse-lookup back-compat "
        f"path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


# ---------------------------------------------------------------------------
# Sanity: the three migrated keys are declared in the manifest's
# config_schema (TB-330 schema extension so a TOML-opted operator can
# write `[components.janitor] <key> = <value>` without tripping
# `validate_config`'s reject-unknown-key path).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema_key, expected_type, expected_default",
    [
        ("disabled", bool, False),
        ("max_findings_llm", int, 10),
        ("judge_effort", str, "high"),
        ("judge_max_turns", int, 12),
    ],
)
def test_manifest_config_schema_declares_each_migrated_knob(
    schema_key: str, expected_type: type, expected_default,
):
    """The janitor manifest's `config_schema` declares all four
    operator-tunable knobs (the kill switch plus the three per-judge
    knobs) so a TOML-opted operator can write
    `[components.janitor] <key> = <value>` for any of them without
    tripping `validate_config`'s reject-unknown-key path. Defaults
    + types match the in-source `_AP2_JANITOR_*_DEFAULT` sentinels +
    `_judge_effort`'s `"high"` fallback.
    """
    from ap2.components.janitor.manifest import MANIFEST

    spec = MANIFEST.config_schema.get(schema_key)
    assert spec is not None, (
        f"TB-330: janitor manifest must declare `{schema_key}` in "
        f"config_schema; got: {sorted(MANIFEST.config_schema)}"
    )
    assert spec.type is expected_type, (
        f"TB-330: janitor.{schema_key}.type expected "
        f"{expected_type.__name__}, got {spec.type.__name__}"
    )
    assert spec.default == expected_default, (
        f"TB-330: janitor.{schema_key}.default expected "
        f"{expected_default!r}, got {spec.default!r}"
    )
    assert spec.description.strip(), (
        f"TB-330: janitor.{schema_key}.description must be non-empty "
        f"for axis-4 `ap2 config list` rendering."
    )


# ---------------------------------------------------------------------------
# Integration: the migrated read path is wired through `_judge_finding`
# / `run_janitor` end-to-end. Pins the cost-cap kill switch behavior
# through the cfg-routed `_max_findings_llm` call.
# ---------------------------------------------------------------------------


def test_run_janitor_skips_judge_when_cap_zero_via_cfg(
    tmp_path, clean_env, emit_reset,
):
    """End-to-end pin: setting `AP2_JANITOR_MAX_FINDINGS_LLM=0`
    (the disabled-judge fallback the module docstring promises) makes
    `run_janitor` skip every SDK call even when findings exist. The
    cfg-routed read path (`_max_findings_llm(cfg)` inside
    `run_janitor`) propagates the env value at call time without
    rebuilding cfg, mirroring the pre-TB-330 lazy-read behavior the
    TB-178 disabled-judge tests pinned.
    """
    import subprocess as _subprocess

    clean_env.setenv("AP2_COMPONENTS_JANITOR_MAX_FINDINGS_LLM", "0")

    # Minimal git-initialized project (mirrors test_janitor.py's
    # `_project` helper but without the full cron / SDK plumbing).
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    _subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True,
    )
    _subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True,
    )
    (tmp_path / ".cc-autopilot" / ".gitignore").write_text(
        "events.jsonl\ncron_state.json\nmm_state.json\n"
        "daemon.pid\npaused\nauto_diagnose_state.json\n"
        "operator_queue.jsonl\noperator_queue_state.json\n"
        "pipelines/\ndebug/\n*.lock\n"
    )
    _subprocess.run(
        ["git", "add", "TASKS.md", "CLAUDE.md",
         ".cc-autopilot/.gitignore"],
        cwd=tmp_path, check=True,
    )
    _subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True,
    )

    # Seed an untracked file so `run_janitor` produces a finding.
    (tmp_path / "scratch.txt").write_text("untracked scratch\n")

    # A scripted SDK whose call count we can assert against.
    class _SDK:
        called = False

        def ClaudeAgentOptions(self, **kwargs):  # noqa: N802
            return kwargs

        async def query(self, *, prompt, options):  # noqa: D401
            self.called = True
            yield None

    sdk = _SDK()
    report = asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    assert sdk.called is False, (
        "TB-330: `AP2_JANITOR_MAX_FINDINGS_LLM=0` set after cfg load "
        "must disable the judge entirely via the cfg-routed "
        "`_max_findings_llm(cfg)` read"
    )
    assert len(report.findings) >= 1
    for f in report.findings:
        assert f.verdict == janitor.VERDICT_AMBIGUOUS, (
            "TB-330: disabled-judge fallback must emit every finding "
            "with verdict=ambiguous (deterministic-only behavior)"
        )
