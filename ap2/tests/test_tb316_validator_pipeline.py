"""TB-316: Validator pipeline-as-list + `validator_judge/` subpackage migration
(axes 4 + 5 bundled).

Pins the structural cleavage required by goal.md L218's explicit bundling
of axis 4 ("validator pipeline as a list") and axis 5's `validator_judge/`
migration ("(4) gates on (5)'s `validator_judge` migration"):

  (a) `_validate_briefing_structure` walks a list of `BriefingValidator`
      callables (the five core checks + the registry-walked validators)
      rather than calling each check inline. The five deterministic
      structural checks live in core (`_CORE_VALIDATORS`) and always
      run; the LLM dep-coherence check ships as the `validator_judge`
      component whose manifest registers it as a `briefing_validator`
      hook.
  (b) `BriefingContext` carries every kwarg the pre-TB-316 inline chain
      consumed (text, goal_md_path, skip_goal_alignment, description,
      blocked_csv, events_file, dep_judge_fn) so each top-level
      validator callable reads its inputs through one frozen dataclass.
  (c) The flat module `ap2/validator_judge.py` is gone; the body lives
      at `ap2/components/validator_judge/__init__.py` and the manifest
      at `ap2/components/validator_judge/manifest.py` registers the
      component with `env_flag=AP2_VALIDATOR_JUDGE_DISABLED` (suppress-
      style; default-enabled).
  (d) The manifest's `hook_points` dict exposes every symbol
      `ap2/tools.py`'s pre-TB-316 flat-import block at L115-128 sourced
      from the flat module plus the `briefing_validator` adapter.
  (e) The registry's new `briefing_validators()` accessor returns the
      validator_judge component's `briefing_validator` hook (and is
      empty when the component is disabled via the env flag).
  (f) The env-knob preservation contract holds: `AP2_VALIDATOR_JUDGE_*`
      names appear verbatim in the relocated module body (goal.md
      L64-67 names them as load-bearing operator contract).
  (g) The three flat-import callers (`ap2/tools.py`,
      `ap2/briefing_validators.py`, `ap2/doctor.py`) no longer import
      from `ap2.validator_judge` — the symbols resolve via the
      registry instead.
  (h) The TB-311 import-direction gate stays green post-migration —
      no new static `ap2.components` import sneaks in via the core
      files updated in this task.

The tests live under `ap2/tests/` and are therefore allowed to
import `ap2.components.validator_judge` directly per the TB-311
gate's `_iter_core_py_files` skip of the tests directory.
"""
from __future__ import annotations

import ast
import pathlib
from pathlib import Path

import pytest

from ap2 import events, tools
from ap2.briefing_validators import (
    BriefingContext,
    BriefingValidator,
    _CORE_VALIDATORS,
    _validate_briefing_structure,
    _validate_goal_anchor,
    _validate_no_fenced_paths_in_scope_check,
    _validate_no_manual_bullets,
    _validate_required_sections,
    _validate_why_now,
)
from ap2.components import validator_judge as vj
from ap2.registry import Registry, _reset_default_registry, default_registry
from ap2.tests._briefing_fixtures import canonical_briefing


# Repository root, derived from this file's location:
# ap2/tests/test_tb316_validator_pipeline.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# (a) + (c) Structural pins: the move actually happened
# ---------------------------------------------------------------------------


def test_subpackage_init_file_exists():
    """`ap2/components/validator_judge/__init__.py` exists post-migration
    and carries the actual module body (not a stub marker).
    """
    init_path = _REPO_ROOT / "ap2/components/validator_judge/__init__.py"
    assert init_path.is_file(), (
        "TB-316: the subpackage body should live at "
        "`ap2/components/validator_judge/__init__.py` after the axis-5 "
        "relocation."
    )
    # TB-343: the body moved to the sibling `impl.py`; read it there (the
    # `__init__.py` shim above re-exports the surface).
    src = (
        _REPO_ROOT / "ap2/components/validator_judge/impl.py"
    ).read_text(encoding="utf-8")
    # Sanity: the body file carries the real module — the dispatcher
    # and the SDK helper are defined in source, not stubs.
    assert "def _check_dependency_coherence" in src, (
        "TB-316: the subpackage `__init__.py` should carry the real "
        "module body, not the pre-migration stub-marker file."
    )
    assert "def _judge_dep_coherence_default" in src, (
        "TB-316: the SDK helper `_judge_dep_coherence_default` must "
        "live in the subpackage so the manifest can expose it."
    )


def test_flat_module_file_is_gone():
    """`ap2/validator_judge.py` is removed — the briefing's `test ! -f`
    Verification bullet pinned to source.
    """
    flat_path = _REPO_ROOT / "ap2/validator_judge.py"
    assert not flat_path.exists(), (
        "TB-316: the flat module `ap2/validator_judge.py` should be "
        "removed after the axis-5 relocation; the subpackage at "
        "`ap2/components/validator_judge/__init__.py` is the canonical "
        "location now."
    )


def test_manifest_file_exists():
    """The manifest at `ap2/components/validator_judge/manifest.py`
    exists — registers the component with the registry.
    """
    manifest_path = _REPO_ROOT / "ap2/components/validator_judge/manifest.py"
    assert manifest_path.is_file(), (
        "TB-316: the validator_judge manifest must live at "
        "`ap2/components/validator_judge/manifest.py` so the registry's "
        "discovery walk picks it up."
    )


def test_env_knobs_preserved_verbatim():
    """The TB-235 / TB-249 / TB-269 env knobs `AP2_VALIDATOR_JUDGE_*`
    (goal.md L64-67) are referenced in the subpackage body verbatim —
    the operator-facing knob names are not renamed across the
    relocation. Any rename would be an operator-visible regression.
    """
    # TB-343: the body (and its env-knob references) moved to `impl.py`.
    init_path = _REPO_ROOT / "ap2/components/validator_judge/impl.py"
    src = init_path.read_text(encoding="utf-8")
    for knob in (
        "AP2_VALIDATOR_JUDGE_DISABLED",
        "AP2_VALIDATOR_JUDGE_TIMEOUT_S",
        "AP2_VALIDATOR_JUDGE_MAX_TURNS",
        "AP2_VALIDATOR_JUDGE_MAX_TOKENS",
    ):
        assert knob in src, (
            f"TB-316: the env knob `{knob}` must appear verbatim in "
            f"the relocated module body — the operator-facing env "
            f"knob name (goal.md L64-67) is not renameable."
        )


# ---------------------------------------------------------------------------
# (a) + (b) Pipeline-as-list shape: the five core validators are
# top-level callables and the orchestrator walks them as a list.
# ---------------------------------------------------------------------------


def test_briefing_context_dataclass_shape():
    """`BriefingContext` is a frozen dataclass carrying every kwarg
    the pre-TB-316 `_validate_briefing_structure` signature consumed.
    """
    ctx = BriefingContext(
        text="hello",
        goal_md_path=None,
        skip_goal_alignment=False,
        description="desc",
        blocked_csv="TB-1",
        events_file=None,
        dep_judge_fn=None,
    )
    assert ctx.text == "hello"
    assert ctx.goal_md_path is None
    assert ctx.skip_goal_alignment is False
    assert ctx.description == "desc"
    assert ctx.blocked_csv == "TB-1"
    assert ctx.events_file is None
    assert ctx.dep_judge_fn is None
    # Frozen: mutation must raise (a downstream validator can't sneak
    # state past another validator in the chain).
    with pytest.raises(Exception):  # noqa: PT011 — dataclasses raise FrozenInstanceError
        ctx.text = "mutated"  # type: ignore[misc]


def test_core_validators_is_a_callable_list_in_canonical_order():
    """`_CORE_VALIDATORS` is an ordered iterable of the five
    deterministic structural-check callables in canonical order
    (sections-present, goal-anchor, why-now, no-manual-bullets,
    no-fenced-paths-in-scope). Each callable matches the
    `BriefingValidator = Callable[[BriefingContext], str | None]`
    contract.
    """
    assert len(_CORE_VALIDATORS) == 5, (
        f"TB-316: the five deterministic structural checks should be "
        f"the canonical core list; got {len(_CORE_VALIDATORS)} "
        f"validators: {_CORE_VALIDATORS!r}"
    )
    expected_order = (
        _validate_required_sections,
        _validate_goal_anchor,
        _validate_why_now,
        _validate_no_manual_bullets,
        _validate_no_fenced_paths_in_scope_check,
    )
    assert _CORE_VALIDATORS == expected_order, (
        f"TB-316: core validators must walk in canonical order "
        f"(sections, goal-anchor, why-now, no-manual, no-fenced); "
        f"got: {_CORE_VALIDATORS!r}"
    )
    # Each callable accepts a BriefingContext and returns str | None.
    ctx = BriefingContext(text=canonical_briefing("TB-1", title="x"))
    for validator in _CORE_VALIDATORS:
        result = validator(ctx)
        assert result is None or isinstance(result, str), (
            f"TB-316: every core validator must return str | None; "
            f"{validator.__name__} returned {result!r}"
        )


def test_briefing_validator_typedef_is_exported():
    """The `BriefingValidator` typedef is exported from
    `ap2.briefing_validators` so a future component implementing the
    `briefing_validator` hook can type-annotate against the canonical
    callable shape.
    """
    # The typedef is a `Callable[[BriefingContext], str | None]`. We
    # can't compare against an exact object (typing alias identity is
    # finicky across Python versions), but we CAN assert the name is
    # reachable from the module.
    from ap2 import briefing_validators as bv

    assert hasattr(bv, "BriefingValidator"), (
        "TB-316: the canonical `BriefingValidator` typedef must be "
        "exported from `ap2.briefing_validators` so component manifests "
        "can type-annotate their hook callables."
    )


# ---------------------------------------------------------------------------
# (d) Manifest `hook_points` exposure — the registry-side contract the
# core re-exports source from.
# ---------------------------------------------------------------------------


_EXPECTED_HOOK_POINT_KEYS = (
    "briefing_validator",
    # Constants the pre-TB-316 tools.py imported from the flat module:
    "DEP_JUDGE_PARSE_ERRORS",
    "DepJudgeOutcome",
    "DepJudgeTimeout",
    "VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL",
    "VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED",
    "VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT",
    "VALIDATOR_JUDGE_MAX_TURNS_DEFAULT",
    "VALIDATOR_JUDGE_MODEL",
    "VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT",
    # Callables the pre-TB-316 tools.py imported from the flat module:
    "check_dependency_coherence",
    "judge_dep_coherence_default",
    "parse_dep_judge_response",
)


def test_manifest_hook_points_expose_tools_alias_surface(monkeypatch):
    """The manifest's `hook_points` dict exposes every symbol
    `tools.py`'s pre-TB-316 `from .validator_judge import (...)` block
    sourced from the flat module, plus the `briefing_validator` adapter.
    Identity checks confirm the manifest publishes the EXACT callable /
    value the subpackage body exports — a wrapper or copy would defeat
    monkey-patch-via-module seams tests rely on.
    """
    # Make sure the env flag is unset so the component shows up enabled
    # for the identity probes (the test's session conftest may shield it).
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    _reset_default_registry()
    registry = Registry.discover()
    manifest = registry.get("validator_judge")
    for key in _EXPECTED_HOOK_POINT_KEYS:
        assert key in manifest.hook_points, (
            f"TB-316: validator_judge manifest's `hook_points` should "
            f"expose {key!r}; got {sorted(manifest.hook_points)}"
        )
    # Identity check on each function-shaped hook point.
    assert (
        manifest.hook_points["check_dependency_coherence"]
        is vj._check_dependency_coherence
    ), (
        "TB-316: `hook_points['check_dependency_coherence']` must be "
        "the EXACT callable object reachable from "
        "`ap2.components.validator_judge._check_dependency_coherence` "
        "— a wrapper would break call-site identity assumptions."
    )
    assert (
        manifest.hook_points["judge_dep_coherence_default"]
        is vj._judge_dep_coherence_default
    )
    assert (
        manifest.hook_points["parse_dep_judge_response"]
        is vj._parse_dep_judge_response
    )
    assert manifest.hook_points["DepJudgeTimeout"] is vj._DepJudgeTimeout
    assert manifest.hook_points["DepJudgeOutcome"] is vj._DepJudgeOutcome
    # Constants compare by value.
    assert (
        manifest.hook_points["VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT"]
        == vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    )
    assert (
        manifest.hook_points["VALIDATOR_JUDGE_MAX_TURNS_DEFAULT"]
        == vj._VALIDATOR_JUDGE_MAX_TURNS_DEFAULT
    )
    assert (
        manifest.hook_points["VALIDATOR_JUDGE_MODEL"]
        == vj._VALIDATOR_JUDGE_MODEL
    )
    assert (
        manifest.hook_points["DEP_JUDGE_PARSE_ERRORS"]
        == vj._DEP_JUDGE_PARSE_ERRORS
    )


def test_manifest_declares_briefing_validator_hook_and_env_flag(monkeypatch):
    """The validator_judge manifest's `env_flag` is
    `AP2_VALIDATOR_JUDGE_DISABLED` (suppress-style; default-enabled
    preserves current behavior) and its `hook_points` carries a
    `briefing_validator` callable.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    _reset_default_registry()
    registry = Registry.discover()
    manifest = registry.get("validator_judge")
    assert manifest.env_flag == "AP2_VALIDATOR_JUDGE_DISABLED", (
        f"TB-316: validator_judge manifest's `env_flag` must be "
        f"`AP2_VALIDATOR_JUDGE_DISABLED`; got {manifest.env_flag!r}."
    )
    assert manifest.default_enabled is True, (
        "TB-316: validator_judge manifest must be default-enabled — "
        "the env flag is suppress-style; an operator who sets it to a "
        "truthy value disables the dep-coherence check."
    )
    assert "briefing_validator" in manifest.hook_points, (
        f"TB-316: the validator_judge manifest must register a "
        f"`briefing_validator` hook; got: {sorted(manifest.hook_points)}"
    )
    assert callable(manifest.hook_points["briefing_validator"]), (
        "TB-316: the `briefing_validator` hook value must be callable."
    )


# ---------------------------------------------------------------------------
# (e) Registry's `briefing_validators()` walk
# ---------------------------------------------------------------------------


def test_registry_briefing_validators_walk_returns_validator_judge_hook(
    monkeypatch,
):
    """When the validator_judge component is enabled (env flag unset),
    `registry.briefing_validators()` returns the manifest's
    `briefing_validator` hook in the walk.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    _reset_default_registry()
    registry = default_registry()
    validators = registry.briefing_validators()
    manifest = registry.get("validator_judge")
    expected_hook = manifest.hook_points["briefing_validator"]
    assert expected_hook in validators, (
        f"TB-316: `registry.briefing_validators()` should include the "
        f"validator_judge component's `briefing_validator` hook when "
        f"the component is enabled; got {validators!r}"
    )


def test_registry_briefing_validators_walk_skips_disabled_component(
    monkeypatch,
):
    """When `AP2_VALIDATOR_JUDGE_DISABLED=1` is set, the registry's
    `briefing_validators()` walk drops the validator_judge component
    entirely — the env-flag polarity (default-enabled, env-disables)
    short-circuits the component out of `enabled_components(...)`.
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")
    _reset_default_registry()
    registry = default_registry()
    validators = registry.briefing_validators()
    manifest = registry.get("validator_judge")
    expected_hook = manifest.hook_points["briefing_validator"]
    assert expected_hook not in validators, (
        f"TB-316: `registry.briefing_validators()` must not include the "
        f"validator_judge component's hook when "
        f"`AP2_VALIDATOR_JUDGE_DISABLED=1`; got {validators!r}"
    )


# ---------------------------------------------------------------------------
# Pipeline behavior pins: the dep-coherence check fires through the
# registry path by default; disabling the component suppresses it.
# ---------------------------------------------------------------------------


_CANONICAL = canonical_briefing("TB-700", title="tb316 fixture")


def test_dep_coherence_check_fires_by_default_through_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: with the validator_judge component enabled and a
    stub `dep_judge_fn` provided, the pipeline-as-list orchestrator
    reaches the dep-coherence check via the registry walk and the
    stub is invoked. The error path (judge names a missing hard
    predecessor) surfaces verbatim from the registry-walked hook.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    _reset_default_registry()
    events_file = tmp_path / "events.jsonl"
    captured: list[dict] = []

    def _judge(**kwargs):
        captured.append(dict(kwargs))
        return {
            "hard_predecessors": ["TB-217"],
            "reasoning": "TB-217 created the precondition",
        }

    err = _validate_briefing_structure(
        _CANONICAL,
        description="this needs TB-217's work",
        blocked_csv="",  # nothing declared — TB-217 is missing
        events_file=events_file,
        dep_judge_fn=_judge,
    )
    assert err is not None
    assert "TB-217" in err
    assert "hard predecessor" in err
    assert captured, (
        "TB-316: the dep-coherence check must fire through the "
        "registry-walked validator_judge hook when the component is "
        "enabled and a `dep_judge_fn` is supplied."
    )


def test_dep_coherence_check_suppressed_when_component_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When `AP2_VALIDATOR_JUDGE_DISABLED=1` is set, the registry walk
    drops the validator_judge component and the orchestrator's
    pipeline walks the five core checks only. A stub
    `dep_judge_fn` that would fail loudly (raises) MUST NOT be
    invoked — proves the env flag suppresses the check end-to-end via
    the registry path, not just via the validator_judge module's
    inner short-circuit.
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")
    _reset_default_registry()
    events_file = tmp_path / "events.jsonl"

    def _explode(**_kwargs):
        raise AssertionError(
            "TB-316: AP2_VALIDATOR_JUDGE_DISABLED=1 must suppress the "
            "dep-coherence check at the registry walk; this stub should "
            "never fire."
        )

    err = _validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=_explode,
    )
    assert err is None
    # No event should have been emitted (the disable is a clean skip,
    # not a fail-open).
    assert not events_file.exists() or events_file.read_text() == "", (
        "TB-316: disabling the component is a clean skip; no event "
        "should be appended."
    )


def test_orchestrator_walks_pipeline_as_list_not_inline_chain():
    """The post-TB-316 `_validate_briefing_structure` body must walk a
    list of validator callables — `for validator in pipeline` — rather
    than the pre-TB-316 inline call chain. AST-walk the function body
    so docstring / comment mentions of the pre-migration shape don't
    false-positive a plain substring check.
    """
    src_path = _REPO_ROOT / "ap2/briefing_validators.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    func: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (
            node.name == "_validate_briefing_structure"
        ):
            func = node
            break
    assert func is not None, (
        "TB-316: `_validate_briefing_structure` must be defined in "
        "`ap2/briefing_validators.py`."
    )
    # Find a `for` loop walking a pipeline iterable.
    pipeline_loop_seen = False
    for node in ast.walk(func):
        if isinstance(node, ast.For):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "validator":
                pipeline_loop_seen = True
                break
    assert pipeline_loop_seen, (
        "TB-316: `_validate_briefing_structure` must walk a list of "
        "validator callables (`for validator in pipeline: ...`); the "
        "pre-TB-316 inline chain shape is the regression this task "
        "removed."
    )


# ---------------------------------------------------------------------------
# (g) Three flat-import callers rewired
# ---------------------------------------------------------------------------


def test_three_flat_import_callers_rewired():
    """`ap2/tools.py`, `ap2/briefing_validators.py`, and `ap2/doctor.py`
    no longer carry a static `from ap2.validator_judge import …` or
    `from .validator_judge import …` — the flat module is gone and
    the symbols resolve via the registry's `hook_points` dict.

    AST-walk each file so docstring / comment mentions of the
    pre-migration shape (legitimately documenting what changed) don't
    false-positive a plain substring check.
    """
    forbidden_modules = {"ap2.validator_judge"}
    for rel in (
        "ap2/tools.py",
        "ap2/briefing_validators.py",
        "ap2/doctor.py",
    ):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    if node.module in forbidden_modules:
                        pytest.fail(
                            f"TB-316: `{rel}:L{node.lineno}` still "
                            f"statically imports from "
                            f"`ap2.validator_judge` "
                            f"(`from {node.module} import …`). The "
                            f"flat module is gone — resolve via the "
                            f"registry's hook_points dict."
                        )
                else:
                    # Relative imports — resolve to absolute.
                    # The file lives at `ap2/<name>.py`, so level=1
                    # `validator_judge` -> `ap2.validator_judge`.
                    if (
                        node.level == 1
                        and node.module == "validator_judge"
                    ):
                        pytest.fail(
                            f"TB-316: `{rel}:L{node.lineno}` still "
                            f"carries `from .validator_judge import …`."
                        )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "ap2.validator_judge":
                        pytest.fail(
                            f"TB-316: `{rel}:L{node.lineno}` still "
                            f"carries `import ap2.validator_judge`."
                        )


def test_tools_module_still_exposes_pre_tb316_attribute_names(monkeypatch):
    """The pre-TB-316 attribute names (`tools._DepJudgeTimeout`,
    `tools._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT`, …) still resolve on
    the `tools` module via the PEP 562 `__getattr__` hook — preserves
    back-compat for the >30 test modules that touch the
    `from ap2.tools import _DepJudgeTimeout` surface.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_DISABLED", raising=False)
    _reset_default_registry()
    # Direct attribute access through the module.
    assert tools._DepJudgeTimeout is vj._DepJudgeTimeout
    assert tools._DepJudgeOutcome is vj._DepJudgeOutcome
    assert (
        tools._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
        == vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    )
    assert (
        tools._check_dependency_coherence is vj._check_dependency_coherence
    )
    assert (
        tools._judge_dep_coherence_default is vj._judge_dep_coherence_default
    )
    assert tools._parse_dep_judge_response is vj._parse_dep_judge_response
    assert tools._VALIDATOR_JUDGE_MODEL == vj._VALIDATOR_JUDGE_MODEL


# ---------------------------------------------------------------------------
# (h) TB-311 import-direction gate stays green post-migration
# ---------------------------------------------------------------------------


def test_import_direction_gate_stays_green_post_migration():
    """The TB-311 import-direction gate must stay green — none of
    the core files this task touched (tools.py, briefing_validators.py,
    doctor.py, registry.py) introduces a static
    `from ap2.components.validator_judge import …`. They all resolve
    the symbols via the registry's `hook_points` dict (a runtime dict
    lookup the AST walker doesn't flag).
    """
    from ap2.tests.test_core_import_direction import (
        _iter_core_py_files,
        find_violations,
        _EXEMPT_FILES,
    )

    all_violations: list[tuple[str, int, str]] = []
    for path in _iter_core_py_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        for lineno, stmt in find_violations(source, rel):
            all_violations.append((rel, lineno, stmt))
    assert not all_violations, (
        f"TB-316: the import-direction gate must stay green after "
        f"the validator_judge migration; got violations: {all_violations}"
    )
