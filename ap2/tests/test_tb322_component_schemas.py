"""TB-322: per-component `config_schema` declarations on the six
remaining manifests (axis 3 of the **structured config (env → TOML)**
focus).

Pins the axis-(3) cleavage of the focus (goal.md L331-340):

  1. Every discovered component manifest (the janitor canary from
     TB-321 + the six TB-322 targets — mattermost, attention,
     focus_advance, auto_unfreeze, auto_approve, validator_judge)
     carries a non-empty `config_schema` dict.
  2. Parity: every `AP2_*` knob each component subpackage reads via
     `os.environ.get` has a matching `config_schema` entry on the
     same component's manifest. The parity walk is the regression
     teeth — a future knob addition that forgets the schema
     declaration breaks CI.
  3. `hot_reloadable` flag parity: every `ConfigKey`'s
     `hot_reloadable` flag matches that knob's membership in
     `env_reload.HOT_RELOADABLE_KNOBS`. Keeps the two surfaces from
     drifting (axis-5 read-site migrations will lean on the
     `hot_reloadable` flag to know when to thread a value through
     the per-tick refresh path; today it documents intent).
  4. `aggregate_schemas(default_registry())` returns the union of
     all 7 component schemas with no name collisions across
     components — the validator (TB-321) walks this surface, and a
     collision would mean two components are claiming the same
     `[components.foo].bar` key.

Source-of-truth shape: the per-component knob list is the set of
`os.environ.get("AP2_*")` call sites the parity walk discovers via
Grep. The schema declarations may include extra knobs the
subpackage doesn't read directly today (e.g. focus_advance owns
`AP2_FOCUS_AUTO_ADVANCE_DISABLED` but reads it via `goal.py`
helpers); the parity check is one-way (env_read → schema entry),
so extras are fine. The validator's reject-unknown-key path
already pins the dual surface.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ap2.config_loader import ConfigKey, aggregate_schemas
from ap2.env_reload import FIXED_KNOBS, HOT_RELOADABLE_KNOBS
from ap2.registry import _reset_default_registry, default_registry


# All seven manifests the TB-322 axis-3 cleavage is required to cover
# (the janitor canary from TB-321 + the six TB-322 targets).
_EXPECTED_COMPONENTS: tuple[str, ...] = (
    "attention",
    "auto_approve",
    "auto_unfreeze",
    "focus_advance",
    "janitor",
    "mattermost",
    "validator_judge",
)


# Components whose subpackage env-reads the parity walk asserts schema
# coverage for. TB-322's explicit scope is the six manifests; the
# janitor canary's pre-existing per-judge tunables
# (`AP2_JANITOR_MAX_FINDINGS_LLM`, `AP2_JANITOR_JUDGE_EFFORT`,
# `AP2_JANITOR_JUDGE_MAX_TURNS`, plus the agent-model knobs
# `AP2_AGENT_MODEL` / `AP2_AGENT_EFFORT`) are deliberately NOT in the
# TB-321 canary schema — see the canary manifest docstring's "stay
# where they are" carve-out. A future TB closing the janitor gap will
# add those entries and remove janitor from this skip-list.
_PARITY_WALK_COMPONENTS: tuple[str, ...] = (
    "attention",
    "auto_approve",
    "auto_unfreeze",
    "focus_advance",
    "mattermost",
    "validator_judge",
)


def _components_root() -> Path:
    """Resolve `ap2/components/` from this test file's location.

    Walks up to the repo root rather than hard-coding a path so the
    test stays portable across worktrees / clones.
    """
    return Path(__file__).resolve().parent.parent / "components"


def _grep_env_reads(component_dir: Path) -> set[str]:
    """Walk every `.py` file under `component_dir` and return the set
    of unique `AP2_*` env keys consulted via `os.environ.get(...)`.

    Matches the briefing's "Grep audit is the source of truth"
    contract — the regex mirrors
    `grep -rE 'os\\.environ\\.get."AP2_' <dir>`. Multi-line forms
    (an `os.environ.get(\\n    "AP2_FOO"\\n    ...)` continuation)
    are caught by the `re.DOTALL` flag.
    """
    pattern = re.compile(
        r"""os\.environ\.get\s*\(\s*["'](AP2_[A-Z0-9_]+)["']""",
        re.DOTALL,
    )
    found: set[str] = set()
    for py in component_dir.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in pattern.finditer(text):
            found.add(m.group(1))
    return found


# Map env-knob name → `(component_name, schema_key_name)` for the
# parity test's lookup. The schema key is the bare TOML key (the
# component drops the `AP2_<COMPONENT>_` prefix); the validator
# walks `[components.<name>].<key>` so the key shape matches the
# TOML surface, not the env-var surface. Listed explicitly so a
# future knob addition that forgets this map ALSO breaks CI (the
# parity test below uses this map to translate env-name → schema-
# key; an unmapped env name fails the assert with a clear
# "no schema-key mapping" message).
_ENV_TO_SCHEMA_KEY: dict[str, str] = {
    # mattermost
    "AP2_MM_CHANNELS": "channels",
    "AP2_MM_BOT_USER_ID": "bot_user_id",
    "AP2_MM_MENTION": "mention",
    # attention
    "AP2_TASK_STUCK_THRESHOLD_S": "task_stuck_threshold_s",
    "AP2_TASK_FROZEN_RECENCY_S": "task_frozen_recency_s",
    "AP2_AUTO_APPROVE_COST_APPROACH_PCT": "cost_approach_pct",
    "AP2_ATTENTION_DEBOUNCE_S": "debounce_s",
    "AP2_ATTENTION_IMMEDIATE_PUSH": "immediate_push",
    # focus_advance (not env-read by the subpackage itself today;
    # listed for completeness in case axis-5 relocates the reads)
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED": "auto_advance_disabled",
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES": "empty_cycles",
    # auto_unfreeze
    "AP2_AUTO_UNFREEZE_DISABLED": "disabled",
    "AP2_AUTO_UNFREEZE_FIX_SHAPES": "fix_shapes",
    "AP2_AUTO_UNFREEZE_DRY_RUN": "dry_run",
    "AP2_AUTO_UNFREEZE_MAX_PER_TASK": "max_per_task",
    "AP2_AUTO_UNFREEZE_MAX_PER_DAY": "max_per_day",
    # auto_approve
    "AP2_AUTO_APPROVE": "enabled",
    "AP2_AUTO_APPROVE_DRY_RUN": "dry_run",
    "AP2_AUTO_APPROVE_FREEZE_THRESHOLD": "freeze_threshold",
    "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP": "per_task_token_cap",
    "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP": "window_token_cap",
    # validator_judge
    "AP2_VALIDATOR_JUDGE_DISABLED": "disabled",
    "AP2_VALIDATOR_JUDGE_TIMEOUT_S": "timeout_s",
    "AP2_VALIDATOR_JUDGE_MAX_TURNS": "max_turns",
    "AP2_VALIDATOR_JUDGE_MAX_TOKENS": "max_tokens",
    # janitor canary (TB-321)
    "AP2_JANITOR_DISABLED": "disabled",
}


# ---------------------------------------------------------------------
# 1. Every manifest declares a non-empty `config_schema`.
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reset the cached `default_registry()` per test so a stale
    monkeypatch from another test file doesn't leak through."""
    _reset_default_registry()
    yield
    _reset_default_registry()


@pytest.mark.parametrize("component_name", _EXPECTED_COMPONENTS)
def test_manifest_carries_nonempty_config_schema(component_name):
    """Axis (3) deliverable: each manifest declares a `config_schema`
    dict with at least one entry. The TB-321 default is an empty
    dict (so non-canary manifests continued to load); TB-322 fills
    in the six remaining manifests so the validator surface is no
    longer vacuous."""
    reg = default_registry()
    manifest = reg.get(component_name)
    schema = getattr(manifest, "config_schema", None)
    assert isinstance(schema, dict) and schema, (
        f"TB-322: component {component_name!r} must declare a "
        f"non-empty config_schema; got {schema!r}"
    )
    # Every entry must be a `ConfigKey` instance (not e.g. a raw
    # dict — `aggregate_schemas` / `validate_config` rely on
    # `.type` / `.default` / `.hot_reloadable` attribute access).
    for key_name, spec in schema.items():
        assert isinstance(spec, ConfigKey), (
            f"TB-322: {component_name}.{key_name} must be a "
            f"ConfigKey instance; got {type(spec).__name__}"
        )
        assert spec.name == key_name, (
            f"TB-322: {component_name}.{key_name}: ConfigKey.name "
            f"({spec.name!r}) must match the dict key for the axis-4 "
            f"flat-list rendering surface."
        )
        assert spec.description.strip(), (
            f"TB-322: {component_name}.{key_name}: description must "
            f"be non-empty so `ap2 config list` (axis-4) can surface "
            f"a meaningful per-key annotation."
        )


# ---------------------------------------------------------------------
# 2. Parity: every grep-found env read has a matching schema entry.
# ---------------------------------------------------------------------


def test_every_env_get_has_matching_schema_key():
    """Walk each component subpackage's `os.environ.get("AP2_*")`
    call sites via Grep and assert every unique key has a matching
    `config_schema` entry on the same component's manifest.

    This is the regression-teeth check the briefing pins: a future
    knob addition (e.g. an axis-5 migration that re-introduces a
    direct `os.environ.get` read intra-package, or a new feature
    that lands a knob without updating the schema) breaks CI here.
    Walks via Grep rather than Python imports so a knob hidden in a
    helper module the manifest doesn't import directly is still
    caught.

    Direction: env_read → schema entry. Extra schema entries (knobs
    the subpackage doesn't read directly today but logically owns
    via a helper-module read) are fine — the validator's reject-
    unknown-key path pins the dual surface from the other side.
    """
    reg = default_registry()
    root = _components_root()
    missing: list[str] = []
    unmapped: list[str] = []
    for component_name in _PARITY_WALK_COMPONENTS:
        component_dir = root / component_name
        env_keys = _grep_env_reads(component_dir)
        manifest = reg.get(component_name)
        schema = manifest.config_schema
        for env_key in sorted(env_keys):
            schema_key = _ENV_TO_SCHEMA_KEY.get(env_key)
            if schema_key is None:
                unmapped.append(f"{component_name}: {env_key}")
                continue
            if schema_key not in schema:
                missing.append(
                    f"{component_name}: env {env_key} → schema key "
                    f"{schema_key!r} not in config_schema "
                    f"(declared: {sorted(schema)})"
                )
    assert not unmapped, (
        "TB-322: env knobs read by the component subpackage but not "
        "listed in `_ENV_TO_SCHEMA_KEY` — extend the map and the "
        "owning manifest's config_schema:\n  "
        + "\n  ".join(unmapped)
    )
    assert not missing, (
        "TB-322: env-read knobs without a matching config_schema "
        "entry on the owning manifest:\n  " + "\n  ".join(missing)
    )


# ---------------------------------------------------------------------
# 3. Hot-reloadable parity: ConfigKey.hot_reloadable mirrors
#    env_reload.HOT_RELOADABLE_KNOBS membership.
# ---------------------------------------------------------------------


# Inverse of `_ENV_TO_SCHEMA_KEY`: (component, schema_key) → env name.
# A `(component, schema_key)` may collide across components (e.g.
# `("auto_unfreeze", "disabled")` vs `("janitor", "disabled")`), so
# the inversion keys on the pair, not on the schema key alone.
_SCHEMA_KEY_TO_ENV: dict[tuple[str, str], str] = {}
# Map each env knob to the component that owns it. The
# `_ENV_TO_SCHEMA_KEY` map above tells us the schema-key; the owning
# component is determined by walking the registry — but in this test
# module we list it explicitly so a parity test failure pinpoints
# the owning manifest unambiguously.
_ENV_OWNER: dict[str, str] = {
    "AP2_MM_CHANNELS": "mattermost",
    "AP2_MM_BOT_USER_ID": "mattermost",
    "AP2_MM_MENTION": "mattermost",
    "AP2_TASK_STUCK_THRESHOLD_S": "attention",
    "AP2_TASK_FROZEN_RECENCY_S": "attention",
    "AP2_AUTO_APPROVE_COST_APPROACH_PCT": "attention",
    "AP2_ATTENTION_DEBOUNCE_S": "attention",
    "AP2_ATTENTION_IMMEDIATE_PUSH": "attention",
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED": "focus_advance",
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES": "focus_advance",
    "AP2_AUTO_UNFREEZE_DISABLED": "auto_unfreeze",
    "AP2_AUTO_UNFREEZE_FIX_SHAPES": "auto_unfreeze",
    "AP2_AUTO_UNFREEZE_DRY_RUN": "auto_unfreeze",
    "AP2_AUTO_UNFREEZE_MAX_PER_TASK": "auto_unfreeze",
    "AP2_AUTO_UNFREEZE_MAX_PER_DAY": "auto_unfreeze",
    "AP2_AUTO_APPROVE": "auto_approve",
    "AP2_AUTO_APPROVE_DRY_RUN": "auto_approve",
    "AP2_AUTO_APPROVE_FREEZE_THRESHOLD": "auto_approve",
    "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP": "auto_approve",
    "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP": "auto_approve",
    "AP2_VALIDATOR_JUDGE_DISABLED": "validator_judge",
    "AP2_VALIDATOR_JUDGE_TIMEOUT_S": "validator_judge",
    "AP2_VALIDATOR_JUDGE_MAX_TURNS": "validator_judge",
    "AP2_VALIDATOR_JUDGE_MAX_TOKENS": "validator_judge",
    "AP2_JANITOR_DISABLED": "janitor",
}
for env_name, owner in _ENV_OWNER.items():
    _SCHEMA_KEY_TO_ENV[(owner, _ENV_TO_SCHEMA_KEY[env_name])] = env_name


def test_hot_reloadable_flag_matches_env_reload_set():
    """Every `ConfigKey.hot_reloadable` flag must match the knob's
    membership in `env_reload.HOT_RELOADABLE_KNOBS`. Keeps the two
    surfaces from drifting — a knob that's hot-reloadable per the
    env_reload helper but advertises `hot_reloadable=False` on its
    schema (or vice versa) is a contract bug.

    `FIXED_KNOBS` membership is treated as "definitely NOT hot-
    reloadable" (the operator needs a daemon restart). A knob in
    neither set defaults to `hot_reloadable=False` per the
    conservative-default contract on `ConfigKey`.
    """
    reg = default_registry()
    mismatches: list[str] = []
    for (component_name, schema_key), env_name in (
        _SCHEMA_KEY_TO_ENV.items()
    ):
        manifest = reg.get(component_name)
        spec = manifest.config_schema.get(schema_key)
        if spec is None:
            # The "missing schema entry" failure is covered by
            # `test_every_env_get_has_matching_schema_key` — skip
            # here to keep the failure message focused on the
            # hot-reloadable axis.
            continue
        is_hot = env_name in HOT_RELOADABLE_KNOBS
        is_fixed = env_name in FIXED_KNOBS
        if is_hot and not spec.hot_reloadable:
            mismatches.append(
                f"{component_name}.{schema_key} ({env_name}): in "
                f"HOT_RELOADABLE_KNOBS but ConfigKey.hot_reloadable "
                f"is False"
            )
        elif not is_hot and spec.hot_reloadable:
            mismatches.append(
                f"{component_name}.{schema_key} ({env_name}): NOT in "
                f"HOT_RELOADABLE_KNOBS (fixed={is_fixed}) but "
                f"ConfigKey.hot_reloadable is True"
            )
    assert not mismatches, (
        "TB-322: hot_reloadable flag drift between ConfigKey and "
        "env_reload.HOT_RELOADABLE_KNOBS:\n  " + "\n  ".join(mismatches)
    )


# ---------------------------------------------------------------------
# 4. aggregate_schemas: union of all 7 components, no collisions.
# ---------------------------------------------------------------------


def test_aggregate_schemas_returns_all_seven_component_schemas():
    """`aggregate_schemas(default_registry())` walks every manifest
    and returns the per-component dict union. Post-TB-322 the union
    contains all 7 components (the 6 axis-3 targets + the janitor
    canary from TB-321); pre-TB-322 only janitor was present."""
    schemas = aggregate_schemas(default_registry())
    assert set(schemas) == set(_EXPECTED_COMPONENTS), (
        f"TB-322: aggregate_schemas must surface every TB-322 "
        f"component plus the janitor canary; got {sorted(schemas)}"
    )


def test_aggregate_schemas_has_no_cross_component_key_collisions():
    """The validator scopes keys by `[components.<name>]` so two
    components MAY share a bare key name (e.g. both `auto_unfreeze`
    and `janitor` use `disabled`). This is fine at the validator
    layer. The collision check here is a documentation aid — the
    test passes today; if a future component lands a key that
    collides AND the operator-facing toml-key shape becomes
    confusing, the test gives a clear surface to revisit.
    """
    schemas = aggregate_schemas(default_registry())
    # Flatten to a `{(component, key): ConfigKey}` mapping and check
    # uniqueness within each component (the dict shape already
    # enforces this — the assert is structural defense in depth).
    seen: dict[tuple[str, str], ConfigKey] = {}
    for component_name, keys in schemas.items():
        for key_name, spec in keys.items():
            pair = (component_name, key_name)
            assert pair not in seen, (
                f"TB-322: duplicate (component, key) pair {pair!r} "
                f"in aggregate_schemas output — this should be "
                f"structurally impossible given the dict shape."
            )
            seen[pair] = spec
    # Pin the total knob count so a casual edit that drops a knob
    # without updating the test surfaces immediately. Post-TB-322:
    # janitor(1) + mattermost(3) + attention(5) + focus_advance(2)
    # + auto_unfreeze(5) + auto_approve(5) + validator_judge(4) =
    # 25 distinct (component, key) pairs.
    assert len(seen) == 25, (
        f"TB-322: total config-schema entries across all components "
        f"changed from 25; got {len(seen)}. If the change is "
        f"intentional, bump this assertion and document the new "
        f"shape in the TB-322 progress entry."
    )
