"""TB-328: attention component reads via `cfg.components_config` (axis-5 cluster).

Long-tail-cluster sibling to TB-326's auto_approve pilot
(`test_tb326_auto_approve_cfg_reads.py`) and TB-327's auto_unfreeze
follow-on (`test_tb327_auto_unfreeze_cfg_reads.py`); the same five
regression cleavages applied to the four operator-tunable attention
knobs the component logically owns: `AP2_TASK_STUCK_THRESHOLD_S`,
`AP2_TASK_FROZEN_RECENCY_S`, `AP2_ATTENTION_DEBOUNCE_S`,
`AP2_ATTENTION_IMMEDIATE_PUSH`.

The migrated helpers no longer read these via direct
`os.environ.get` calls inside the component body; they now flow
through `Config.get_component_value("attention", <key>)`, which
inspects sectioned env > flat env (via reverse-`FLAT_TO_SECTIONED`)
> `cfg.components_config["attention"][<key>]` > default at call
time. Behavior preservation contract: every existing `AP2_*`
flat-env consumer (operator shell exports, `.cc-autopilot/env`)
keeps today's behavior bit-for-bit while a TOML-opted operator's
`[components.attention]` values win transparently once env-side
overrides are unset.

Five regression cleavages this pin holds (mirror of TB-326/TB-327):

  (1) **Grep-shape**: zero remaining `os.environ.get("AP2_TASK_STUCK_THRESHOLD_S",
      "AP2_TASK_FROZEN_RECENCY_S", "AP2_ATTENTION_…"` call sites in
      `ap2/components/attention/`. A refactor that re-introduces a
      direct env read here loses the back-compat layer and side-steps
      the structured-config precedence the operator depends on. The
      `AP2_AUTO_APPROVE_COST_APPROACH_PCT` read in `_cost_approach_pct`
      is intentionally EXEMPT — it belongs to the `auto_approve`
      cluster per `FLAT_TO_SECTIONED` and migrates on a separate
      auto_approve-cluster sweep (the briefing's Out-of-scope list
      names that knob's cluster ownership explicitly).
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
      values still default to the original sentinels
      (`DEFAULT_TASK_STUCK_THRESHOLD_S` 14400 for the stuck floor,
      `DEFAULT_TASK_FROZEN_RECENCY_S` 86400 for the frozen recency
      window, `DEFAULT_ATTENTION_DEBOUNCE_S` 21600 for the per-(type,
      key) debounce, `False` for the immediate-push boolean).
  (5) **Chosen access shape published**: the manifest's docstring
      cites the chosen resolved-config access shape (`cfg.get_component_value`)
      with a TB-328 anchor so the remaining three cluster migrations
      (focus_advance, mattermost, validator_judge, janitor) adopt it
      verbatim.

Why this matters: axis (5)'s long tail (per goal.md L353-364) sets
"≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads" as the Progress signal at L398-399. TB-326's
pilot landed 3 of N; TB-327 added 5; this cluster adds 4 more
(attention is the operator-facing surface for the proactive
monitoring promise — every detector here was already plumbed through
`apply_env_overrides` via TB-323, so the read-side swap closes the
contract without changing observable detector behavior).
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import re

import pytest

from ap2.components import attention
from ap2.components.attention import (
    AttentionCondition,
    DEFAULT_ATTENTION_DEBOUNCE_S,
    DEFAULT_TASK_FROZEN_RECENCY_S,
    DEFAULT_TASK_STUCK_THRESHOLD_S,
    _attention_debounce_s,
    _is_attention_immediate_push_enabled,
    _task_frozen_recency_s,
    _task_stuck_threshold_s,
    should_suppress,
)
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb328_attention_cfg_reads.py -> repo/
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob the harness/CI might have set so each
    test owns its `os.environ` surface deterministically. Other test
    fixtures that depend on a clean env (notably `cfg` below) take this
    as a parameter so the strip lands BEFORE `Config.load` reads any
    AP2_* override. Mirror of the TB-326/TB-327 pilots' `clean_env`
    shape.
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
# (1) Grep-shape — zero remaining `os.environ.get("AP2_TASK_STUCK_THRESHOLD_S
#     |AP2_TASK_FROZEN_RECENCY_S|AP2_ATTENTION_…"` call sites in the body.
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_attention_component():
    """The grep-shape Verification bullet, pinned to source so a refactor
    that re-introduces a direct env read inside the component body
    surfaces here instead of only via the briefing-level grep gate.

    The component package is `ap2/components/attention/` (both
    `__init__.py` and `manifest.py`); the test reads each `.py` file
    in the package and rejects any literal
    `os.environ.get("AP2_TASK_STUCK_THRESHOLD_S` /
    `os.environ.get("AP2_TASK_FROZEN_RECENCY_S` /
    `os.environ.get("AP2_ATTENTION_…` fragment. The
    `AP2_AUTO_APPROVE_COST_APPROACH_PCT` read in `_cost_approach_pct`
    is EXEMPT — it belongs to the auto_approve cluster per
    `FLAT_TO_SECTIONED` and migrates separately. Comments / docstrings
    that QUOTE the old call sites for historical context are allowed
    iff they DON'T form a valid call statement — the pattern below
    matches only the bare call shape, so a backticked-in-docstring
    mention does NOT match.
    """
    pattern = re.compile(
        r"os\.environ\.get\([\"']"
        r"AP2_(TASK_STUCK_THRESHOLD_S|TASK_FROZEN_RECENCY_S|ATTENTION_)"
    )
    component_dir = _REPO_ROOT / "ap2/components/attention"
    violations: list[str] = []
    for py_path in sorted(component_dir.rglob("*.py")):
        src = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = py_path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-328: the attention component body must read its four "
        "operator-tunable knobs via `cfg.get_component_value(...)`, "
        "not via direct `os.environ.get('AP2_TASK_STUCK_THRESHOLD_S'|"
        "'AP2_TASK_FROZEN_RECENCY_S'|'AP2_ATTENTION_…')` calls. "
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
        _REPO_ROOT / "ap2/components/attention/impl.py"
    ).read_text(encoding="utf-8")
    assert "cfg.get_component_value" in init_src, (
        "TB-328: the attention component body should use "
        "`cfg.get_component_value(...)` to resolve the four migrated "
        "knobs (per the TB-326 pilot's chosen access shape — see the "
        "auto_approve manifest docstring and the attention manifest's "
        "TB-328 doc block)."
    )


# ---------------------------------------------------------------------------
# (2) TOML-first read path — cfg.components_config wins over flat env.
# ---------------------------------------------------------------------------


def test_task_stuck_threshold_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.attention] task_stuck_threshold_s = N` TOML value
    populates `cfg.components_config["attention"]["task_stuck_threshold_s"]`,
    which the helper reads via `cfg.get_component_value`. The legacy
    flat env name is UNSET; the helper returns the TOML value (no env
    fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.attention]\ntask_stuck_threshold_s = 7200\n",
    )
    assert cfg.components_config["attention"]["task_stuck_threshold_s"] == 7200
    assert _task_stuck_threshold_s(cfg) == 7200


def test_task_frozen_recency_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.attention] task_frozen_recency_s = N` TOML value
    flows through to the helper's int return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.attention]\ntask_frozen_recency_s = 43200\n",
    )
    assert _task_frozen_recency_s(cfg) == 43200


def test_attention_debounce_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.attention] debounce_s = N` TOML value flows
    through to the helper's int return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.attention]\ndebounce_s = 3600\n",
    )
    assert _attention_debounce_s(cfg) == 3600


def test_immediate_push_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.attention] immediate_push = true` TOML value flows
    through to the helper's bool return value.
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.attention]\nimmediate_push = true\n",
    )
    assert _is_attention_immediate_push_enabled(cfg) is True


# ---------------------------------------------------------------------------
# (3) Flat-env back-compat — same value the legacy direct read returned.
# ---------------------------------------------------------------------------


def test_task_stuck_threshold_flat_env_ignored(cfg, clean_env, emit_reset):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default).
    """
    baseline = _task_stuck_threshold_s(cfg)  # flat unset
    clean_env.setenv("AP2_TASK_STUCK_THRESHOLD_S", "7200")
    assert _task_stuck_threshold_s(cfg) == baseline, (
        "TB-413: flat tunable env must be ignored; config.toml/schema wins"
    )


def test_task_frozen_recency_flat_env_ignored(cfg, clean_env, emit_reset):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default).
    """
    baseline = _task_frozen_recency_s(cfg)  # flat unset
    clean_env.setenv("AP2_TASK_FROZEN_RECENCY_S", "3600")
    assert _task_frozen_recency_s(cfg) == baseline, (
        "TB-413: flat tunable env must be ignored; config.toml/schema wins"
    )


def test_attention_debounce_flat_env_ignored(cfg, clean_env, emit_reset):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default).
    """
    baseline = _attention_debounce_s(cfg)  # flat unset
    clean_env.setenv("AP2_ATTENTION_DEBOUNCE_S", "1800")
    assert _attention_debounce_s(cfg) == baseline, (
        "TB-413: flat tunable env must be ignored; config.toml/schema wins"
    )


def test_immediate_push_flat_env_ignored(cfg, clean_env, emit_reset):
    """TB-413: a flat tunable env name no longer overrides config.toml —
    it is IGNORED. The helper returns the same value it returns with the
    flat env unset (config.toml/schema default), regardless of which
    truthy spelling the stale flat env carries.
    """
    baseline = _is_attention_immediate_push_enabled(cfg)  # flat unset
    for truthy in ("1", "true", "yes", "on"):
        clean_env.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", truthy)
        assert _is_attention_immediate_push_enabled(cfg) == baseline, (
            "TB-413: flat tunable env must be ignored; "
            "config.toml/schema wins"
        )


# ---------------------------------------------------------------------------
# (4) Parser semantics preserved — default-on-bad-value pins.
# ---------------------------------------------------------------------------


def test_task_stuck_threshold_unset_defaults(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_task_stuck_threshold_s` returns
    `DEFAULT_TASK_STUCK_THRESHOLD_S` (14400 / 4h).
    """
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S
    assert DEFAULT_TASK_STUCK_THRESHOLD_S == 14400


def test_task_stuck_threshold_garbage_defaults(cfg, clean_env, emit_reset):
    """Non-int env value → default. Pins the parser-fallback shape so
    an operator typo doesn't disable the detector silently.
    """
    clean_env.setenv("AP2_TASK_STUCK_THRESHOLD_S", "not-a-number")
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S


def test_task_stuck_threshold_zero_defaults(cfg, clean_env, emit_reset):
    """Zero env value → default. Pre-migration semantics treated `<= 0`
    as invalid (a zero-threshold would fire on every Active task every
    tick); the cfg-side path preserves that floor.
    """
    clean_env.setenv("AP2_TASK_STUCK_THRESHOLD_S", "0")
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S


def test_task_stuck_threshold_negative_defaults(cfg, clean_env, emit_reset):
    """Negative env value → default."""
    clean_env.setenv("AP2_TASK_STUCK_THRESHOLD_S", "-1")
    assert _task_stuck_threshold_s(cfg) == DEFAULT_TASK_STUCK_THRESHOLD_S


def test_task_frozen_recency_unset_defaults(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_task_frozen_recency_s` returns
    `DEFAULT_TASK_FROZEN_RECENCY_S` (86400 / 24h).
    """
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S
    assert DEFAULT_TASK_FROZEN_RECENCY_S == 86400


def test_task_frozen_recency_garbage_defaults(cfg, clean_env, emit_reset):
    """Non-int env value → default."""
    clean_env.setenv("AP2_TASK_FROZEN_RECENCY_S", "not-a-number")
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S


def test_task_frozen_recency_nonpositive_defaults(cfg, clean_env, emit_reset):
    """`<= 0` env value → default (same floor as the stuck threshold)."""
    clean_env.setenv("AP2_TASK_FROZEN_RECENCY_S", "0")
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S
    clean_env.setenv("AP2_TASK_FROZEN_RECENCY_S", "-5")
    assert _task_frozen_recency_s(cfg) == DEFAULT_TASK_FROZEN_RECENCY_S


def test_attention_debounce_unset_defaults(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_attention_debounce_s` returns
    `DEFAULT_ATTENTION_DEBOUNCE_S` (21600 / 6h).
    """
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S
    assert DEFAULT_ATTENTION_DEBOUNCE_S == 21600


def test_attention_debounce_garbage_defaults(cfg, clean_env, emit_reset):
    """Non-int env value → default."""
    clean_env.setenv("AP2_ATTENTION_DEBOUNCE_S", "garbage")
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S


def test_attention_debounce_nonpositive_defaults(cfg, clean_env, emit_reset):
    """`<= 0` env value → default."""
    clean_env.setenv("AP2_ATTENTION_DEBOUNCE_S", "0")
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S
    clean_env.setenv("AP2_ATTENTION_DEBOUNCE_S", "-3600")
    assert _attention_debounce_s(cfg) == DEFAULT_ATTENTION_DEBOUNCE_S


def test_immediate_push_unset_defaults_to_false(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_is_attention_immediate_push_enabled`
    returns False (conservative default per goal.md Non-goals L253-256).
    """
    assert _is_attention_immediate_push_enabled(cfg) is False


def test_immediate_push_garbage_defaults_to_false(cfg, clean_env, emit_reset):
    """Non-truthy env value → False. Pins the parser-fallback shape so
    a typo doesn't silently enable the push channel.
    """
    clean_env.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "garbage")
    assert _is_attention_immediate_push_enabled(cfg) is False
    clean_env.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "0")
    assert _is_attention_immediate_push_enabled(cfg) is False
    clean_env.setenv("AP2_ATTENTION_IMMEDIATE_PUSH", "false")
    assert _is_attention_immediate_push_enabled(cfg) is False


# ---------------------------------------------------------------------------
# (5) Chosen access shape published — manifest docstring cites it.
# ---------------------------------------------------------------------------


def test_manifest_documents_chosen_access_shape():
    """The attention manifest documents (top-of-file docstring or
    in-body comment) the chosen resolved-config access shape so the
    follow-up cluster migrations (focus_advance, mattermost,
    validator_judge, janitor) read the same pattern from one place.
    Looks for the `cfg.get_component_value` call shape + a TB-328
    reference. Loose enough that a docstring rewrite doesn't
    false-positive; strict enough that an accidental documentation
    drop fires.
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/attention/manifest.py"
    ).read_text(encoding="utf-8")
    assert "TB-328" in manifest_src, (
        "TB-328: the attention manifest must cite the TB-328 "
        "axis-5 cluster anchor so the follow-up cluster migrations "
        "have a discoverable pointer to the chosen access shape."
    )
    assert "cfg.get_component_value" in manifest_src, (
        "TB-328: the attention manifest must name the chosen "
        "resolved-config access shape (`cfg.get_component_value`) so "
        "the follow-up cluster migrations adopt the same pattern "
        "verbatim instead of each picking ad-hoc shapes."
    )


# ---------------------------------------------------------------------------
# Sanity: the four migrated knobs are listed in FLAT_TO_SECTIONED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        (
            "AP2_TASK_STUCK_THRESHOLD_S",
            "components.attention.task_stuck_threshold_s",
        ),
        (
            "AP2_TASK_FROZEN_RECENCY_S",
            "components.attention.task_frozen_recency_s",
        ),
        (
            "AP2_ATTENTION_DEBOUNCE_S",
            "components.attention.debounce_s",
        ),
        (
            "AP2_ATTENTION_IMMEDIATE_PUSH",
            "components.attention.immediate_push",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_four_migrated_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops one of these mappings would silently break the flat-env
    back-compat path for that knob; the pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-328: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the attention reverse-lookup back-compat "
        f"path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


# ---------------------------------------------------------------------------
# Behavioral cleavage: the env read still flows end-to-end through
# `should_suppress` and the detector loop. Mirrors the briefing's
# "behavior preservation" contract — the cfg read returns the same
# value the env read would have returned.
# ---------------------------------------------------------------------------


def test_should_suppress_resolves_debounce_via_cfg(cfg, clean_env, emit_reset):
    """`should_suppress` falls back to `_attention_debounce_s(cfg)` when
    no explicit `debounce_s` is passed. With a flat env override set,
    the helper resolves through the cfg-side path and respects the
    new window. Pins the cfg-threading TB-328 introduced.
    """
    clean_env.setenv("AP2_COMPONENTS_ATTENTION_DEBOUNCE_S", "120")  # 2 min
    cond = AttentionCondition(
        type="task_stuck",
        key="task_stuck:TB-1",
        summary="x",
        ts="2026-05-28T12:00:00Z",
    )
    # A prior fire 60s ago is INSIDE the 120s window — suppress True.
    now = _dt.datetime(2026, 5, 28, 12, 1, 0, tzinfo=_dt.timezone.utc)
    tail = [{
        "type": "attention_raised",
        "attention_type": "task_stuck",
        "key": "task_stuck:TB-1",
        "ts": "2026-05-28T12:00:00Z",
    }]
    assert should_suppress(cond, tail=tail, now=now, cfg=cfg) is True

    # A prior fire 200s ago is OUTSIDE the 120s window — suppress False.
    now2 = _dt.datetime(2026, 5, 28, 12, 3, 20, tzinfo=_dt.timezone.utc)
    assert should_suppress(cond, tail=tail, now=now2, cfg=cfg) is False


def test_should_suppress_requires_cfg_when_debounce_unset(cfg, clean_env, emit_reset):
    """`should_suppress` raises `TypeError` when both `debounce_s` AND
    `cfg` are None — pins the contract: the helper must have a way to
    resolve the window. A refactor that drops cfg from
    `_maybe_emit_attention_events` would surface here.
    """
    cond = AttentionCondition(
        type="task_stuck",
        key="task_stuck:TB-1",
        summary="x",
        ts="2026-05-28T12:00:00Z",
    )
    now = _dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=_dt.timezone.utc)
    with pytest.raises(TypeError):
        should_suppress(cond, tail=[], now=now)


def test_explicit_debounce_kwarg_still_wins(cfg, clean_env, emit_reset):
    """An explicit `debounce_s=...` kwarg bypasses the cfg read entirely
    so unit tests that pin specific values stay green without needing
    a cfg fixture. Pins the optionality of the cfg fallback.
    """
    cond = AttentionCondition(
        type="task_stuck",
        key="task_stuck:TB-2",
        summary="x",
        ts="2026-05-28T12:00:00Z",
    )
    now = _dt.datetime(2026, 5, 28, 12, 0, 30, tzinfo=_dt.timezone.utc)
    tail = [{
        "type": "attention_raised",
        "attention_type": "task_stuck",
        "key": "task_stuck:TB-2",
        "ts": "2026-05-28T12:00:00Z",
    }]
    # 30s elapsed; explicit 60s debounce → suppress.
    assert should_suppress(
        cond, tail=tail, now=now, debounce_s=60,
    ) is True
    # 30s elapsed; explicit 10s debounce → don't suppress.
    assert should_suppress(
        cond, tail=tail, now=now, debounce_s=10,
    ) is False


# ---------------------------------------------------------------------------
# Manifest hook_points still expose the migrated helpers with their new
# signatures. The daemon's module-level alias block rebinds them via the
# registry; a refactor that drops a hook_points entry surfaces here.
# ---------------------------------------------------------------------------


def test_manifest_hook_points_carry_migrated_helpers():
    """The four migrated helpers are still listed in the attention
    manifest's `hook_points` dict — the daemon's alias block
    (`daemon._task_stuck_threshold_s` etc.) resolves through this dict.
    Pin the surface so a refactor that drops an entry blows here
    rather than at daemon module-import time.
    """
    from ap2.components.attention.manifest import MANIFEST

    for key in (
        "task_stuck_threshold_s",
        "task_frozen_recency_s",
        "attention_debounce_s",
        "is_attention_immediate_push_enabled",
    ):
        assert key in MANIFEST.hook_points, (
            f"TB-328: attention manifest's hook_points must expose "
            f"`{key}` so the daemon's module-level alias block can "
            f"resolve through the registry."
        )

    assert MANIFEST.hook_points["task_stuck_threshold_s"] is _task_stuck_threshold_s
    assert MANIFEST.hook_points["task_frozen_recency_s"] is _task_frozen_recency_s
    assert MANIFEST.hook_points["attention_debounce_s"] is _attention_debounce_s
    assert (
        MANIFEST.hook_points["is_attention_immediate_push_enabled"]
        is _is_attention_immediate_push_enabled
    )
