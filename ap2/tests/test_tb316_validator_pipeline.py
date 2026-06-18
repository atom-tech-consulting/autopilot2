"""TB-316 (+ TB-386): validator pipeline-as-list + dep-coherence judge location.

TB-316 refactored `_validate_briefing_structure` from an inline call chain
into a pipeline-as-list orchestrator and relocated the LLM dep-coherence
check into a `validator_judge` component reached via the registry. TB-386
then demoted that judge back into the core briefing-validation runner
(`ap2/briefing_validators.py`) — a judge invoked only as an internal sub-step
of `_validate_briefing_structure` is NOT a loop-level component. The judge
still resolves its backend via `select_adapter("validator_judge", cfg)`.

This module pins the surviving TB-316 contracts after the TB-386 demotion:

  (a) `_validate_briefing_structure` walks a list of `BriefingValidator`
      callables (the five core checks + the appended dep-coherence judge)
      rather than calling each check inline.
  (b) `BriefingContext` carries every kwarg the pre-TB-316 inline chain
      consumed so each top-level validator reads its inputs through one
      frozen dataclass.
  (c) The dep-coherence judge body (`_check_dependency_coherence`,
      `_judge_dep_coherence_default`, …) lives in `ap2/briefing_validators.py`
      and the `AP2_VALIDATOR_JUDGE_*` operator knobs are referenced verbatim
      there (goal.md L64-67 names them as load-bearing operator contract).
  (d) End-to-end: the dep-coherence check fires by default (component-free,
      called directly by the orchestrator) and is suppressed by
      `AP2_VALIDATOR_JUDGE_DISABLED=1`.
  (e) `ap2/tools.py` re-exports the dep-coherence surface (`_DepJudgeTimeout`,
      `_check_dependency_coherence`, …) from `ap2.briefing_validators`.
  (f) The TB-311 import-direction gate stays green — no core file imports
      statically from `ap2/components/`.
"""
from __future__ import annotations

import ast
import pathlib
from pathlib import Path

import pytest

from ap2 import briefing_validators as vj
from ap2 import tools
from ap2.briefing_validators import (
    BriefingContext,
    _CORE_VALIDATORS,
    _validate_briefing_structure,
    _validate_goal_anchor,
    _validate_no_fenced_paths_in_scope_check,
    _validate_no_manual_bullets,
    _validate_required_sections,
    _validate_why_now,
)
from ap2.registry import _reset_default_registry
from ap2.tests._briefing_fixtures import canonical_briefing


# Repository root, derived from this file's location:
# ap2/tests/test_tb316_validator_pipeline.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# (c) Structural pins: the dep-coherence judge lives in core (TB-386), not in
# a flat module and not in a `ap2/components/validator_judge/` component.
# ---------------------------------------------------------------------------


def test_dep_coherence_body_lives_in_briefing_validators():
    """The dep-coherence dispatcher + SDK helper live in
    `ap2/briefing_validators.py` after TB-386 demoted them out of the
    `validator_judge` component."""
    src = (
        _REPO_ROOT / "ap2/briefing_validators.py"
    ).read_text(encoding="utf-8")
    assert "def _check_dependency_coherence" in src, (
        "TB-386: the dep-coherence dispatcher must live in core "
        "`ap2/briefing_validators.py`."
    )
    assert "def _judge_dep_coherence_default" in src, (
        "TB-386: the SDK helper `_judge_dep_coherence_default` must live "
        "in core `ap2/briefing_validators.py`."
    )


def test_validator_judge_component_is_gone():
    """The `ap2/components/validator_judge/` component subpackage is removed
    (TB-386). The flat `ap2/validator_judge.py` module is also gone (TB-316)."""
    assert not (
        _REPO_ROOT / "ap2/components/validator_judge"
    ).exists(), (
        "TB-386: the `validator_judge` component subpackage must be removed "
        "— the dep-coherence judge is a core sub-step, not a component."
    )
    assert not (_REPO_ROOT / "ap2/validator_judge.py").exists(), (
        "TB-316: the flat `ap2/validator_judge.py` module stays removed."
    )


def test_env_knobs_preserved_verbatim():
    """The TB-235 / TB-249 / TB-269 env knobs `AP2_VALIDATOR_JUDGE_*`
    (goal.md L64-67) are referenced in the relocated core body verbatim —
    the operator-facing knob names are not renamed across the demotion.
    """
    src = (
        _REPO_ROOT / "ap2/briefing_validators.py"
    ).read_text(encoding="utf-8")
    for knob in (
        "AP2_VALIDATOR_JUDGE_DISABLED",
        "AP2_VALIDATOR_JUDGE_TIMEOUT_S",
        "AP2_VALIDATOR_JUDGE_MAX_TURNS",
        "AP2_VALIDATOR_JUDGE_MAX_TOKENS",
    ):
        assert knob in src, (
            f"TB-386: the env knob `{knob}` must appear verbatim in the "
            f"relocated core body — the operator-facing env knob name "
            f"(goal.md L64-67) is not renameable."
        )


# ---------------------------------------------------------------------------
# (a) + (b) Pipeline-as-list shape: the five core validators are top-level
# callables and the orchestrator walks them as a list.
# ---------------------------------------------------------------------------


def test_briefing_context_dataclass_shape():
    """`BriefingContext` is a frozen dataclass carrying every kwarg the
    pre-TB-316 `_validate_briefing_structure` signature consumed."""
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
    # Frozen: mutation must raise (a downstream validator can't sneak state
    # past another validator in the chain).
    with pytest.raises(Exception):  # noqa: PT011 — dataclasses raise FrozenInstanceError
        ctx.text = "mutated"  # type: ignore[misc]


def test_core_validators_is_a_callable_list_in_canonical_order():
    """`_CORE_VALIDATORS` is an ordered iterable of the five deterministic
    structural-check callables in canonical order. Each callable matches the
    `BriefingValidator = Callable[[BriefingContext], str | None]` contract.
    """
    assert len(_CORE_VALIDATORS) == 5, (
        f"TB-316: the five deterministic structural checks should be the "
        f"canonical core list; got {len(_CORE_VALIDATORS)} validators: "
        f"{_CORE_VALIDATORS!r}"
    )
    expected_order = (
        _validate_required_sections,
        _validate_goal_anchor,
        _validate_why_now,
        _validate_no_manual_bullets,
        _validate_no_fenced_paths_in_scope_check,
    )
    assert _CORE_VALIDATORS == expected_order, (
        f"TB-316: core validators must walk in canonical order (sections, "
        f"goal-anchor, why-now, no-manual, no-fenced); got: "
        f"{_CORE_VALIDATORS!r}"
    )
    ctx = BriefingContext(text=canonical_briefing("TB-1", title="x"))
    for validator in _CORE_VALIDATORS:
        result = validator(ctx)
        assert result is None or isinstance(result, str), (
            f"TB-316: every core validator must return str | None; "
            f"{validator.__name__} returned {result!r}"
        )


def test_briefing_validator_typedef_is_exported():
    """The `BriefingValidator` typedef is exported from
    `ap2.briefing_validators` so a future validator can type-annotate against
    the canonical callable shape."""
    from ap2 import briefing_validators as bv

    assert hasattr(bv, "BriefingValidator"), (
        "TB-316: the canonical `BriefingValidator` typedef must be exported "
        "from `ap2.briefing_validators`."
    )


def test_dep_coherence_adapter_is_a_briefing_validator():
    """`_briefing_validator` (the dep-coherence adapter the orchestrator
    appends) matches the `BriefingValidator` shape and short-circuits with
    `None` when neither `events_file` nor `dep_judge_fn` is supplied (the
    unit-test path exercising only the deterministic checks)."""
    assert callable(vj._briefing_validator)
    ctx = BriefingContext(text=canonical_briefing("TB-1", title="x"))
    assert vj._briefing_validator(ctx) is None


# ---------------------------------------------------------------------------
# (d) Pipeline behavior pins: the dep-coherence check fires by default (called
# directly by the orchestrator) and is suppressed by the off-switch.
# ---------------------------------------------------------------------------


_CANONICAL = canonical_briefing("TB-700", title="tb316 fixture")


def test_dep_coherence_check_fires_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: with the off-switch unset and a stub `dep_judge_fn`
    provided, the pipeline-as-list orchestrator reaches the dep-coherence
    check (appended after the core validators) and the stub is invoked. The
    error path (judge names a missing hard predecessor) surfaces verbatim.
    """
    monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", raising=False)
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
        "TB-386: the dep-coherence check must fire (called directly by the "
        "orchestrator) when the off-switch is unset and a `dep_judge_fn` is "
        "supplied."
    )


def test_dep_coherence_check_suppressed_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When `AP2_VALIDATOR_JUDGE_DISABLED=1` is set, the dep-coherence check
    short-circuits inside `_check_dependency_coherence` before the judge is
    consulted. A stub `dep_judge_fn` that would fail loudly (raises) MUST NOT
    be invoked — proves the plain off-switch suppresses the check end-to-end.
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")
    _reset_default_registry()
    events_file = tmp_path / "events.jsonl"

    def _explode(**_kwargs):
        raise AssertionError(
            "TB-386: AP2_VALIDATOR_JUDGE_DISABLED=1 must suppress the "
            "dep-coherence check before the judge fires; this stub should "
            "never run."
        )

    err = _validate_briefing_structure(
        _CANONICAL,
        description="x",
        blocked_csv="",
        events_file=events_file,
        dep_judge_fn=_explode,
    )
    assert err is None
    # No event should have been emitted (the disable is a clean skip, not a
    # fail-open).
    assert not events_file.exists() or events_file.read_text() == "", (
        "TB-386: disabling the judge is a clean skip; no event should be "
        "appended."
    )


def test_orchestrator_walks_pipeline_as_list_not_inline_chain():
    """The post-TB-316 `_validate_briefing_structure` body must walk a list of
    validator callables — `for validator in pipeline` — rather than the
    pre-TB-316 inline call chain. AST-walk the function body so docstring /
    comment mentions of the pre-migration shape don't false-positive."""
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
    pipeline_loop_seen = False
    for node in ast.walk(func):
        if isinstance(node, ast.For):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "validator":
                pipeline_loop_seen = True
                break
    assert pipeline_loop_seen, (
        "TB-316: `_validate_briefing_structure` must walk a list of validator "
        "callables (`for validator in pipeline: ...`); the pre-TB-316 inline "
        "chain shape is the regression this task removed."
    )


# ---------------------------------------------------------------------------
# (e) tools.py re-exports the dep-coherence surface from briefing_validators.
# ---------------------------------------------------------------------------


def test_no_flat_validator_judge_import_in_core_callers():
    """`ap2/tools.py`, `ap2/briefing_validators.py`, and `ap2/doctor.py` carry
    no static `from ap2.validator_judge import …` or `from .validator_judge
    import …` — the flat module is gone (TB-316). AST-walk each file so
    docstring / comment mentions don't false-positive."""
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
                            f"TB-316: `{rel}:L{node.lineno}` still statically "
                            f"imports from `ap2.validator_judge`."
                        )
                elif node.level == 1 and node.module == "validator_judge":
                    pytest.fail(
                        f"TB-316: `{rel}:L{node.lineno}` still carries "
                        f"`from .validator_judge import …`."
                    )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "ap2.validator_judge":
                        pytest.fail(
                            f"TB-316: `{rel}:L{node.lineno}` still carries "
                            f"`import ap2.validator_judge`."
                        )


def test_tools_module_still_exposes_dep_judge_attribute_names():
    """The dep-coherence attribute names (`tools._DepJudgeTimeout`,
    `tools._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT`, …) still resolve on the
    `tools` module — TB-386 re-exports them from `ap2.briefing_validators`
    via a plain import, preserving back-compat for the >30 test modules that
    touch the `from ap2.tools import _DepJudgeTimeout` surface.
    """
    assert tools._DepJudgeTimeout is vj._DepJudgeTimeout
    assert tools._DepJudgeOutcome is vj._DepJudgeOutcome
    assert (
        tools._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
        == vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    )
    assert tools._check_dependency_coherence is vj._check_dependency_coherence
    assert (
        tools._judge_dep_coherence_default is vj._judge_dep_coherence_default
    )
    assert tools._parse_dep_judge_response is vj._parse_dep_judge_response


# ---------------------------------------------------------------------------
# (f) TB-311 import-direction gate stays green post-demotion.
# ---------------------------------------------------------------------------


def test_import_direction_gate_stays_green_post_demotion():
    """The TB-311 import-direction gate must stay green — none of the core
    files this task touched introduces a static `from ap2.components… import …`.
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
        f"TB-386: the import-direction gate must stay green after the "
        f"validator_judge demotion; got violations: {all_violations}"
    )
