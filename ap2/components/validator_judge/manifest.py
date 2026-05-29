"""validator_judge component manifest (TB-316 axis 4 + axis 5).

Registers the TB-235 LLM-driven dependency-coherence check as a
`briefing_validator` hook so `briefing_validators._validate_briefing_structure`
walks `default_registry().briefing_validators()` (the new axis-4
pipeline-as-list orchestrator) and picks the dep-coherence check up
through the same iteration mechanism every future briefing-validator
component will use.

The check's runtime body lives intra-package at
`ap2/components/validator_judge/__init__.py` (relocated from
`ap2/validator_judge.py` by TB-316); the manifest references the
runtime symbols via `from . import …`. Match the migration shape
established by TB-313 (`focus_advance/`) and TB-314
(`auto_unfreeze/`): the `__init__.py` carries the actual module body
verbatim, and the manifest is the new file that registers the
component with the registry.

env_flag polarity (`AP2_VALIDATOR_JUDGE_DISABLED`, suppress-style):
the component is default-enabled to preserve the current behavior;
the env flag suppresses when truthy. Per the registry's polarity
rule (`Registry._is_enabled` in `ap2/registry.py` L211-221), a
`default_enabled=True` manifest with an env_flag set DISABLES on
truthy values — so an operator who sets
`AP2_VALIDATOR_JUDGE_DISABLED=1` drops the validator_judge component
entirely from `registry.briefing_validators()`, the orchestrator
walks the five core checks only, and no SDK call fires on
queue-append. The pre-TB-316 inline `os.environ.get(
"AP2_VALIDATOR_JUDGE_DISABLED", ...)` short-circuit inside
`_check_dependency_coherence` is preserved verbatim for defense in
depth — a future caller that resolves the check directly (without
going through the registry walk) still sees the same kill-switch
behavior.

`hook_points` exposure (TB-316): the manifest publishes every symbol
that `ap2/tools.py`'s pre-TB-316 flat-import block at L115-128
sourced from the flat module (the operator-facing knob defaults,
the parse-error category tuple, the dispatcher, the SDK helper, the
NamedTuple + sentinel types) plus the `briefing_validator` adapter.
Tests that import these symbols directly are exempt per the TB-311
gate's `_iter_core_py_files` skip of `ap2/tests/`. Core (`tools.py`,
`briefing_validators.py`, `doctor.py`) resolves these via
`default_registry().get("validator_judge").hook_points[…]` rather
than a direct `from ap2.components.validator_judge import …` — the
TB-311 gate forbids the latter.

The `briefing_validator` adapter (`_briefing_validator`) wraps
`_check_dependency_coherence` so the registry walk can call it with
the canonical `(ctx) -> str | None` shape every other briefing
validator uses post-TB-316. The adapter preserves the pre-TB-316
opt-in contract: the dep-coherence check only fires when the caller
supplied an `events_file` (real queue-append / board-edit surface)
or a `dep_judge_fn` (test injection seam). Unit tests that exercise
only the deterministic checks omit both kwargs and the adapter
short-circuits with `None` — every historical caller stays green.

TB-331 axis-5 read-site migration — chosen resolved-config access shape
=======================================================================
The four operator-tunable knobs the component logically owns
(`disabled`, `timeout_s`, `max_turns`, `max_tokens` — the deprecated
TB-249 alias) are now read via the
**`cfg.get_component_value(component, key)`** helper on `Config`
(option 2 of the briefing's three candidate shapes — the same pattern
TB-326's auto_approve pilot ratified and TB-327/TB-328/TB-329/TB-330
re-used verbatim across `auto_unfreeze`, `attention`, `focus_advance`,
and `janitor`). The four legacy flat env names
(`AP2_VALIDATOR_JUDGE_DISABLED`, `AP2_VALIDATOR_JUDGE_TIMEOUT_S`,
`AP2_VALIDATOR_JUDGE_MAX_TURNS`, `AP2_VALIDATOR_JUDGE_MAX_TOKENS`)
still resolve transparently because `Config.get_component_value`'s
call-time env-first precedence (sectioned env > flat env via
reverse-`FLAT_TO_SECTIONED` > `cfg.components_config` TOML snapshot >
default) keeps the shell-export operator's `.cc-autopilot/env`
exports working bit-for-bit. The `noisy_threshold` knob (per
`FLAT_TO_SECTIONED`) is read in `ap2/automation_status.py` (the
attention-detector aggregation surface), not inside this component
subpackage, so its migration lives outside TB-331's scope — the
five-mapping count on `FLAT_TO_SECTIONED` for `validator_judge` is
larger than the four-mapping read-site swap because of this split.

The cfg-routing also flows through the `_briefing_validator` adapter's
new `ctx.cfg` plumbing: `BriefingContext` (TB-316's pipeline-as-list
shape) gained an optional `cfg: "Config | None"` field, and
`_validate_briefing_structure` threads the caller's `cfg` into the
context so the dep-coherence check resolves component knobs against
the same Config the surrounding board-edit / operator-queue surface
already has in hand. Legacy unit-test paths that exercise the judge
without constructing a Config (`test_dep_validator_judge.py`,
`test_tb247_*`, `test_tb316_*` — they pass `events_file` and
`dep_judge_fn` from a `tmp_path` without ever calling `Config.load`)
fall through a synthetic empty `Config` here so their
`monkeypatch.setenv(...)` shape preserves the pre-TB-331 env-only
behavior exactly. The shim is a back-compat carve-out, NOT a new
production path — every queue-append / board-edit caller supplies a
real `cfg` and the synthetic Config never materializes on the live
daemon path.
"""
from __future__ import annotations

from ap2.config import Config
from ap2.config_loader import ConfigKey
from ap2.registry import Manifest

from . import (
    _DEP_JUDGE_PARSE_ERRORS,
    _DepJudgeOutcome,
    _DepJudgeTimeout,
    _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL,
    _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED,
    _VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT,
    _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT,
    _VALIDATOR_JUDGE_MODEL,
    _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT,
    _check_dependency_coherence,
    _judge_dep_coherence_default,
    _parse_dep_judge_response,
    _slice_briefing_for_dep_judge,
)


def _empty_cfg_for_back_compat() -> Config:
    """TB-331: synthetic empty `Config` for legacy test paths that
    exercise the dep-coherence judge without constructing a real one.

    `test_dep_validator_judge.py` and the TB-247 / TB-316 sibling
    modules call `tools._validate_briefing_structure(...)` with
    `events_file=tmp_path/'events.jsonl'` and `dep_judge_fn=<stub>`
    but no `cfg` — they explicitly opt out of building a Config
    because the judge's decision logic doesn't need one (the env-knob
    smoke tests use `monkeypatch.setenv(...)` plus an in-process read).
    Post-TB-331 the component body's `cfg.get_component_value(...)`
    calls require a Config-shaped object, so this helper synthesizes
    one via `Config.__new__(Config)` and sets only the
    `components_config` attribute the resolver consults (the empty
    dict means the snapshot branch is a no-op; the env-first
    precedence inside `get_component_value` still walks sectioned-env
    + flat-env before falling through to the default).

    NOT a new production path — every queue-append / board-edit caller
    supplies a real `cfg` via `BriefingContext.cfg`, so the synthetic
    only materializes on the back-compat test surface. The
    `Config.__new__` shape (bypassing `__init__`) is the cheapest way
    to produce a Config-shaped object without touching disk or
    paying the env-overlay cost.
    """
    cfg = Config.__new__(Config)
    cfg.components_config = {}
    return cfg


def _briefing_validator(ctx) -> str | None:
    """Adapter from `BriefingContext` to `_check_dependency_coherence`.

    Matches the canonical `BriefingValidator = Callable[[BriefingContext],
    str | None]` shape post-TB-316 so the registry's
    `briefing_validators()` walk can dispatch through this adapter
    uniformly with every other briefing validator.

    Preserves the pre-TB-316 opt-in contract verbatim: the dep-coherence
    check only fires when the caller supplied either an `events_file`
    (real queue-append / board-edit surface) or a `dep_judge_fn` (test
    injection seam). Unit tests that exercise only the deterministic
    checks omit both kwargs and this adapter short-circuits with `None`
    — the >30 historical call sites that rely on this behavior stay
    green without modification.

    TB-331 axis-5: threads `ctx.cfg` into
    `_check_dependency_coherence` so the four migrated knob reads
    resolve against the same `Config` the surrounding board-edit /
    operator-queue surface already has. Legacy test paths that don't
    populate `ctx.cfg` (test_dep_validator_judge.py et al) get a
    synthetic empty Config via `_empty_cfg_for_back_compat()` so the
    env-first precedence inside `Config.get_component_value` preserves
    their `monkeypatch.setenv(...)` semantics bit-for-bit.

    The inner `_check_dependency_coherence` body still honors the
    `AP2_VALIDATOR_JUDGE_DISABLED=1` kill switch defensively (so a
    direct call without going through the registry walk also sees
    the kill switch). At the manifest level, the env_flag suppresses
    the component entirely — the registry's
    `enabled_components(...)` filter drops this manifest, so
    `briefing_validators()` returns the five core checks only and the
    adapter is never invoked.
    """
    if ctx.events_file is None and ctx.dep_judge_fn is None:
        return None
    cfg = ctx.cfg if ctx.cfg is not None else _empty_cfg_for_back_compat()
    return _check_dependency_coherence(
        cfg,
        briefing_text=ctx.text,
        description=ctx.description or "",
        blocked_csv=ctx.blocked_csv or "",
        events_file=ctx.events_file,
        judge_fn=ctx.dep_judge_fn,
    )


MANIFEST = Manifest(
    name="validator_judge",
    # TB-316: suppress-style env flag. `default_enabled=True` means the
    # validator_judge component participates in `registry.briefing_validators()`
    # by default — preserves the pre-TB-316 observable behavior where
    # every `do_board_edit({add_*})` / `do_operator_queue_append({add_*})`
    # call fans out to the SDK judge unless the operator opted out via
    # `AP2_VALIDATOR_JUDGE_DISABLED=1`. The env flag's truthy value
    # disables the component (`Registry._is_enabled`'s polarity rule for
    # `default_enabled=True`).
    env_flag="AP2_VALIDATOR_JUDGE_DISABLED",
    default_enabled=True,
    hook_points={
        # The axis-4 pipeline-as-list adapter — the registry's
        # `briefing_validators()` walk picks this up and the
        # orchestrator in `_validate_briefing_structure` calls it with
        # a `BriefingContext` instance.
        "briefing_validator": _briefing_validator,
        # TB-316: expose every symbol `tools.py`'s pre-migration flat-
        # import block at L115-128 sourced from the flat module so core
        # resolves the rebinds via the registry rather than statically
        # importing from `ap2/components/validator_judge/`. Tests that
        # import these symbols directly are exempt per the TB-311 gate's
        # `_iter_core_py_files` skip of `ap2/tests/`. Constants vs.
        # functions vs. types all live in `hook_points`; the dict's
        # value is just a callable-or-value.
        "DEP_JUDGE_PARSE_ERRORS": _DEP_JUDGE_PARSE_ERRORS,
        "DepJudgeOutcome": _DepJudgeOutcome,
        "DepJudgeTimeout": _DepJudgeTimeout,
        "VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL": _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL,
        "VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED": _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED,
        "VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT": _VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT,
        "VALIDATOR_JUDGE_MAX_TURNS_DEFAULT": _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT,
        "VALIDATOR_JUDGE_MODEL": _VALIDATOR_JUDGE_MODEL,
        "VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT": _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT,
        "check_dependency_coherence": _check_dependency_coherence,
        "judge_dep_coherence_default": _judge_dep_coherence_default,
        "parse_dep_judge_response": _parse_dep_judge_response,
        "slice_briefing_for_dep_judge": _slice_briefing_for_dep_judge,
    },
    dependencies=[],
    # TB-322 (axis 3): per-component `config_schema` declarations for
    # every validator_judge knob the subpackage reads via
    # `os.environ.get` in `ap2/components/validator_judge/__init__.py`
    # (the four `AP2_VALIDATOR_JUDGE_DISABLED` /
    # `AP2_VALIDATOR_JUDGE_TIMEOUT_S` /
    # `AP2_VALIDATOR_JUDGE_MAX_TURNS` /
    # `AP2_VALIDATOR_JUDGE_MAX_TOKENS` call sites at
    # L689 / L695 / L708 / L709). All four are in
    # `env_reload.HOT_RELOADABLE_KNOBS` except the deprecated
    # `max_tokens` alias (kept as a back-compat sentinel — not listed
    # in either set, so the conservative default `hot_reloadable=False`
    # applies). The component's model identifier
    # (`_VALIDATOR_JUDGE_MODEL`) is intentionally NOT env-tunable
    # today (see `__init__.py` L102-109 — "Intentionally NOT exposed
    # as an env knob yet") so it is omitted from the schema; a future
    # TB introducing a model-override knob will add the `model` entry.
    config_schema={
        "disabled": ConfigKey(
            name="disabled",
            type=bool,
            default=False,
            description=(
                "Kill switch for the LLM-driven dep-coherence check "
                "(TB-235). True short-circuits the validator inside "
                "`_check_dependency_coherence` and (at the manifest "
                "env_flag layer) drops the component from "
                "`registry.briefing_validators()`. Mirrors "
                "`AP2_VALIDATOR_JUDGE_DISABLED`; in "
                "`HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
        "timeout_s": ConfigKey(
            name="timeout_s",
            type=float,
            default=60.0,
            description=(
                "Per-call timeout (seconds) for the dep-coherence "
                "SDK invocation (TB-235). Default 60s — short enough "
                "to keep `ap2 add` responsive, long enough for a "
                "Haiku judge round-trip. Mirrors "
                "`AP2_VALIDATOR_JUDGE_TIMEOUT_S`; in "
                "`HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
        "max_turns": ConfigKey(
            name="max_turns",
            type=int,
            default=2,
            description=(
                "Per-call max-turns budget for the dep-coherence "
                "judge (TB-249). Default 2 — one verdict message "
                "plus one optional Read/Grep tool call. Mirrors "
                "`AP2_VALIDATOR_JUDGE_MAX_TURNS`; in "
                "`HOT_RELOADABLE_KNOBS`."
            ),
            hot_reloadable=True,
        ),
        "max_tokens": ConfigKey(
            name="max_tokens",
            type=int,
            default=500,
            description=(
                "TB-249 deprecated alias for `max_turns`; honored "
                "(ceiling-capped at 5) when `max_turns` is unset so "
                "operators with stale `AP2_VALIDATOR_JUDGE_MAX_TOKENS` "
                "exports don't break. Mirrors "
                "`AP2_VALIDATOR_JUDGE_MAX_TOKENS`; not in "
                "`HOT_RELOADABLE_KNOBS` (deprecated knob — operators "
                "should migrate to `max_turns`)."
            ),
            hot_reloadable=False,
        ),
    },
)
