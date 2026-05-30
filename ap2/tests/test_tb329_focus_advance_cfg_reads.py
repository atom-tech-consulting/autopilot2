"""TB-329: focus_advance component reads via `cfg.components_config` (axis-5 cluster).

Long-tail-cluster sibling to TB-326's auto_approve pilot
(`test_tb326_auto_approve_cfg_reads.py`), TB-327's auto_unfreeze
follow-on (`test_tb327_auto_unfreeze_cfg_reads.py`), and TB-328's
attention follow-on (`test_tb328_attention_cfg_reads.py`); the same
five regression cleavages applied to the two operator-tunable
focus_advance knobs the component logically owns:
`AP2_FOCUS_AUTO_ADVANCE_DISABLED`, `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`.

The two migrated call sites used to read these via
`goal.auto_advance_disabled()` / `goal.advance_empty_cycles_threshold()`
(env-only helpers in `ap2/goal.py`); they now route through the
intra-package `_focus_auto_advance_disabled(cfg)` /
`_advance_empty_cycles_threshold(cfg)` helpers, which themselves call
`Config.get_component_value("focus_advance", <key>)`. The latter
evaluates sectioned env > flat env (via reverse-`FLAT_TO_SECTIONED`)
> `cfg.components_config["focus_advance"][<key>]` > default at
call time. Behavior preservation contract: every existing
`AP2_FOCUS_*` flat-env consumer (operator shell exports,
`.cc-autopilot/env`) keeps today's behavior bit-for-bit while a
TOML-opted operator's `[components.focus_advance]` values win
transparently once env-side overrides are unset. The env-only
`goal.*` helpers are retained as-is for the
`test_tb226_focus_rotation.py` unit pins (env-knob parser shape).

Five regression cleavages this pin holds (mirror of TB-326/327/328):

  (1) **Grep-shape**: zero remaining
      `os.environ.get("AP2_FOCUS_AUTO_ADVANCE_DISABLED")` or
      `os.environ.get("AP2_FOCUS_ADVANCE_EMPTY_CYCLES")` call sites in
      `ap2/components/focus_advance/`. A refactor that re-introduces a
      direct env read here loses the back-compat layer and side-steps
      the structured-config precedence the operator depends on.
  (2) **TOML-first read path**: a `cfg.components_config` value
      populated from `config.toml` (or the sectioned-env override
      layer) wins over the legacy flat env name once env-side
      overrides are unset — the operator's TOML becomes the
      authoritative source the moment they opt in.
  (3) **Flat-env back-compat**: a flat env name unaccompanied by a
      TOML value still resolves the same value the old direct
      `goal.*` env-read path did. The shell-export operator who never
      migrated `.cc-autopilot/env` sees zero observable change.
  (4) **Parser semantics preserved**: empty / non-int / out-of-range
      values still default + clamp to the original sentinels
      (`ADVANCE_EMPTY_CYCLES_DEFAULT` = 3, clamped to
      [`ADVANCE_EMPTY_CYCLES_MIN`, `ADVANCE_EMPTY_CYCLES_MAX`] = [1, 20]
      for `empty_cycles`; `auto_advance_disabled` parses the same
      truthy set as `goal.auto_advance_disabled()`).
  (5) **Chosen access shape published**: the manifest's docstring
      cites the chosen resolved-config access shape (`cfg.get_component_value`)
      with a TB-329 anchor so the remaining two cluster migrations
      (mattermost, validator_judge, janitor) have one more reference
      to anchor against.

Sanity pin: the two migrated knobs are listed in `FLAT_TO_SECTIONED`
mapping to `components.focus_advance.auto_advance_disabled` and
`components.focus_advance.empty_cycles`. The TB-329 migration
corrected the original TB-323 map's `components.focus_advance.disabled`
entry — see the manifest's TB-329 doc block for the rationale
(schema + howto.md alignment).

Why this matters: axis (5)'s long tail (per goal.md L353-364) sets
"≥80% of source-side `os.environ.get('AP2_*')` calls migrated to
`cfg.<path>.<key>` reads" as the Progress signal at L398-399.
TB-326/327/328 landed 12 of N; this cluster adds 2 more.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from ap2 import events, goal
from ap2.components.focus_advance import (
    _advance_empty_cycles_threshold,
    _focus_auto_advance_disabled,
    _maybe_advance_focus,
)
from ap2.config import Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import init_project


# Repository root, derived from this file's location:
# ap2/tests/test_tb329_focus_advance_cfg_reads.py -> repo/
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
# (1) Grep-shape — zero remaining `os.environ.get("AP2_FOCUS_*")`
#     call sites in the component body.
# ---------------------------------------------------------------------------


def test_no_direct_env_reads_in_focus_advance_component():
    """The grep-shape Verification bullet, pinned to source so a refactor
    that re-introduces a direct env read inside the component body
    surfaces here instead of only via the briefing-level grep gate.

    The component package is `ap2/components/focus_advance/` (both
    `__init__.py` and `manifest.py`); the test reads each `.py` file
    in the package and rejects any literal
    `os.environ.get("AP2_FOCUS_AUTO_ADVANCE_DISABLED"` or
    `os.environ.get("AP2_FOCUS_ADVANCE_EMPTY_CYCLES"` fragment.
    Comments / docstrings that QUOTE the old call sites for historical
    context are allowed iff they DON'T form a valid call statement —
    the pattern below matches only the bare call shape (the briefing-
    level grep's own anchor), so a backticked-in-docstring mention
    does NOT match because the docstring quotes break the literal.
    """
    pattern = re.compile(
        r"os\.environ\.get\([\"']AP2_FOCUS_"
        r"(AUTO_ADVANCE_DISABLED|ADVANCE_EMPTY_CYCLES)"
    )
    component_dir = _REPO_ROOT / "ap2/components/focus_advance"
    violations: list[str] = []
    for py_path in sorted(component_dir.rglob("*.py")):
        src = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if pattern.search(line):
                rel = py_path.relative_to(_REPO_ROOT).as_posix()
                violations.append(f"{rel}:L{lineno}: {line.strip()}")
    assert not violations, (
        "TB-329: the focus_advance component body must read its two "
        "operator-tunable knobs via `cfg.get_component_value(...)`, "
        "not via direct `os.environ.get('AP2_FOCUS_…')` calls. "
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
        _REPO_ROOT / "ap2/components/focus_advance/impl.py"
    ).read_text(encoding="utf-8")
    assert "cfg.get_component_value" in init_src, (
        "TB-329: the focus_advance component body should use "
        "`cfg.get_component_value(...)` to resolve the two migrated "
        "knobs (per the TB-326 pilot's chosen access shape — see the "
        "auto_approve manifest docstring and the focus_advance "
        "manifest's TB-329 doc block)."
    )


# ---------------------------------------------------------------------------
# (2) TOML-first read path — cfg.components_config wins over flat env.
# ---------------------------------------------------------------------------


def test_auto_advance_disabled_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.focus_advance] auto_advance_disabled = true` TOML
    value populates
    `cfg.components_config["focus_advance"]["auto_advance_disabled"]`,
    which the helper reads via `cfg.get_component_value`. The legacy
    flat env name is UNSET; the helper returns True from the TOML
    layer (no env fallback fired).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.focus_advance]\nauto_advance_disabled = true\n",
    )
    assert (
        cfg.components_config["focus_advance"]["auto_advance_disabled"]
        is True
    )
    assert _focus_auto_advance_disabled(cfg) is True


def test_empty_cycles_reads_from_toml(tmp_path, clean_env, emit_reset):
    """A `[components.focus_advance] empty_cycles = 7` TOML value flows
    through to the helper's int return value (within the clamp window
    so the value is returned verbatim).
    """
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.focus_advance]\nempty_cycles = 7\n",
    )
    assert cfg.components_config["focus_advance"]["empty_cycles"] == 7
    assert _advance_empty_cycles_threshold(cfg) == 7


# ---------------------------------------------------------------------------
# (3) Flat-env back-compat — same value the legacy goal-helper read returned.
# ---------------------------------------------------------------------------


def test_auto_advance_disabled_flat_env_back_compat(
    cfg, clean_env, emit_reset,
):
    """`AP2_FOCUS_AUTO_ADVANCE_DISABLED=1` set on an env-only project
    (no `config.toml`-side override) still resolves to True via the
    `Config.get_component_value` reverse-`FLAT_TO_SECTIONED` lookup.
    Pins the back-compat path the shell-export operator depends on.
    """
    clean_env.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    assert _focus_auto_advance_disabled(cfg) is True


def test_empty_cycles_flat_env_back_compat(cfg, clean_env, emit_reset):
    """`AP2_FOCUS_ADVANCE_EMPTY_CYCLES=5` on an env-only project resolves
    to 5 via the flat-env back-compat path (within the [1, 20] clamp
    window so the value is returned verbatim).
    """
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "5")
    assert _advance_empty_cycles_threshold(cfg) == 5


# ---------------------------------------------------------------------------
# (4) Parser semantics preserved — default-on-bad-value pins.
# ---------------------------------------------------------------------------


def test_auto_advance_disabled_unset_defaults_to_false(
    cfg, clean_env, emit_reset,
):
    """Unset flat-env + empty TOML → `_focus_auto_advance_disabled`
    returns False (kill switch off). Same default the pre-migration
    `goal.auto_advance_disabled()` returned for the unset case.
    """
    assert _focus_auto_advance_disabled(cfg) is False


def test_auto_advance_disabled_truthy_values(cfg, clean_env, emit_reset):
    """`1` / `true` / `yes` / `on` (case-insensitive) all parse as True.
    Mirrors `goal.auto_advance_disabled()`'s truthy set so the
    behavior-preservation contract on the existing
    `test_auto_advance_disabled_truthy` unit pin (env-only path) also
    holds for the cfg-routed read.
    """
    for val in ("1", "true", "TRUE", "yes", "Yes", "on", "ON"):
        clean_env.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", val)
        assert _focus_auto_advance_disabled(cfg) is True, (
            f"failed for {val!r}"
        )


def test_auto_advance_disabled_falsy_values(cfg, clean_env, emit_reset):
    """`0` / `false` / `no` / empty / `off` all parse as False.
    Mirrors `goal.auto_advance_disabled()`'s falsy enumeration.
    """
    for val in ("0", "false", "no", "", "off"):
        clean_env.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", val)
        assert _focus_auto_advance_disabled(cfg) is False, (
            f"failed for {val!r}"
        )


def test_auto_advance_disabled_typed_bool_from_toml(
    tmp_path, clean_env, emit_reset,
):
    """A TOML-typed `False` flows through verbatim (no string parse).
    Pins the `isinstance(raw, bool)` short-circuit so a typed False
    isn't accidentally coerced via the `.lower()` truthy-set path."""
    cfg = _load_toml_cfg(
        tmp_path,
        "[components.focus_advance]\nauto_advance_disabled = false\n",
    )
    assert _focus_auto_advance_disabled(cfg) is False


def test_empty_cycles_unset_defaults_to_three(cfg, clean_env, emit_reset):
    """Unset flat-env + empty TOML → `_advance_empty_cycles_threshold`
    returns `ADVANCE_EMPTY_CYCLES_DEFAULT` (3). Same default the
    pre-migration env-only path returned for the unset case.
    """
    assert _advance_empty_cycles_threshold(cfg) == (
        goal.ADVANCE_EMPTY_CYCLES_DEFAULT
    )


def test_empty_cycles_garbage_defaults_to_three(cfg, clean_env, emit_reset):
    """Non-int env value → default 3. Pins the parser-fallback shape
    the pre-migration `try: int(raw) except ValueError: return default`
    chain enforced.
    """
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "garbage")
    assert _advance_empty_cycles_threshold(cfg) == (
        goal.ADVANCE_EMPTY_CYCLES_DEFAULT
    )


def test_empty_cycles_whitespace_only_defaults_to_three(
    cfg, clean_env, emit_reset,
):
    """Whitespace-only env value → default 3. Pins the `if not
    raw.strip(): return default` guard."""
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "   ")
    assert _advance_empty_cycles_threshold(cfg) == (
        goal.ADVANCE_EMPTY_CYCLES_DEFAULT
    )


def test_empty_cycles_clamps_to_min(cfg, clean_env, emit_reset):
    """Out-of-range below → clamp to `ADVANCE_EMPTY_CYCLES_MIN` (1).
    Pins the safety floor so an operator typo (e.g. `0`) doesn't
    disable the advance path."""
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "0")
    assert _advance_empty_cycles_threshold(cfg) == (
        goal.ADVANCE_EMPTY_CYCLES_MIN
    )
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "-50")
    assert _advance_empty_cycles_threshold(cfg) == (
        goal.ADVANCE_EMPTY_CYCLES_MIN
    )


def test_empty_cycles_clamps_to_max(cfg, clean_env, emit_reset):
    """Out-of-range above → clamp to `ADVANCE_EMPTY_CYCLES_MAX` (20).
    Pins the safety ceiling so an operator typo (e.g. `999999`)
    doesn't wedge the advance path permanently."""
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "999")
    assert _advance_empty_cycles_threshold(cfg) == (
        goal.ADVANCE_EMPTY_CYCLES_MAX
    )


# ---------------------------------------------------------------------------
# (5) Chosen access shape published — manifest docstring cites it.
# ---------------------------------------------------------------------------


def test_manifest_documents_chosen_access_shape():
    """The focus_advance manifest documents (top-of-file docstring or
    in-body comment) the chosen resolved-config access shape so the
    follow-up cluster migrations (mattermost, validator_judge,
    janitor) read the same pattern from one more place. Looks for the
    `cfg.get_component_value` call shape + a TB-329 reference. Loose
    enough that a docstring rewrite doesn't false-positive; strict
    enough that an accidental documentation drop fires.
    """
    manifest_src = (
        _REPO_ROOT / "ap2/components/focus_advance/manifest.py"
    ).read_text(encoding="utf-8")
    assert "TB-329" in manifest_src, (
        "TB-329: the focus_advance manifest must cite the TB-329 "
        "axis-5 cluster anchor so the follow-up cluster migrations "
        "have a discoverable pointer to the chosen access shape."
    )
    assert "cfg.get_component_value" in manifest_src, (
        "TB-329: the focus_advance manifest must name the chosen "
        "resolved-config access shape (`cfg.get_component_value`) so "
        "the follow-up cluster migrations adopt the same pattern "
        "verbatim instead of each picking ad-hoc shapes."
    )


# ---------------------------------------------------------------------------
# Sanity: the two migrated knobs are listed in FLAT_TO_SECTIONED with the
# schema-aligned sectioned key (TB-329 corrected the original `disabled`
# entry to `auto_advance_disabled`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flat, sectioned",
    [
        (
            "AP2_FOCUS_AUTO_ADVANCE_DISABLED",
            "components.focus_advance.auto_advance_disabled",
        ),
        (
            "AP2_FOCUS_ADVANCE_EMPTY_CYCLES",
            "components.focus_advance.empty_cycles",
        ),
    ],
)
def test_flat_to_sectioned_pins_the_two_migrated_knobs(
    flat: str, sectioned: str,
):
    """`FLAT_TO_SECTIONED` (TB-323) is the contract the
    `Config.get_component_value` reverse-lookup walks. A refactor that
    drops one of these mappings — or reverts the TB-329 latent-bug fix
    that aligned `AP2_FOCUS_AUTO_ADVANCE_DISABLED` with the TB-322
    schema's `auto_advance_disabled` key — would silently break the
    flat-env back-compat path for that knob; the pin catches it.
    """
    assert FLAT_TO_SECTIONED.get(flat) == sectioned, (
        f"TB-329: `FLAT_TO_SECTIONED[{flat!r}]` must map to "
        f"{sectioned!r} for the focus_advance reverse-lookup back-compat "
        f"path; got {FLAT_TO_SECTIONED.get(flat)!r}"
    )


# ---------------------------------------------------------------------------
# Integration: the migrated read path is wired through `_maybe_advance_focus`
# end-to-end. Pins the kill-switch + threshold behavior through the cfg-
# routed call instead of the env-only goal-helper.
# ---------------------------------------------------------------------------


def _write_goal_with_foci(cfg: Config, *titles: str) -> None:
    """Minimal goal.md with `## Current focus:` heading per title.
    Mirrors `test_tb226_focus_rotation._write_goal_with_foci` so this
    test exercises the same scaffold the existing rotation pins use.
    """
    body = "# Project\n\n"
    for t in titles:
        body += f"## Current focus: {t}\n\nbody\n\n"
    (cfg.project_root / "goal.md").write_text(body)


def _emit_ideation_empty_cycle(cfg: Config) -> None:
    """One full empty ideation cycle (entry + exit, no proposal)
    so `_ideation_empty_against_focus` counts it. Mirrors the TB-292
    cycle-grouped shape.
    """
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="empty cycle")


def test_kill_switch_via_cfg_route_blocks_halt(cfg, clean_env, emit_reset):
    """End-to-end pin: setting `AP2_FOCUS_AUTO_ADVANCE_DISABLED=1`
    after cfg load (the operator's shell-export pattern + the
    `monkeypatch.setenv(...); helper(cfg)` test idiom) blocks the
    halt even when the empty-cycles threshold trips. The cfg-routed
    read path (`_focus_auto_advance_disabled(cfg)` inside
    `_maybe_advance_focus`) propagates the env value at call time
    without rebuilding cfg, mirroring the pre-TB-329
    `goal.auto_advance_disabled()` lazy-read behavior. TB-342: the
    detector now emits `roadmap_complete` instead of walking the
    pointer; the kill-switch path still surfaces a decisions-needed
    bullet.
    """
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "1")
    clean_env.setenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    _emit_ideation_empty_cycle(cfg)

    asyncio.run(_maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is False, (
        "kill-switch should have blocked the halt via the cfg-routed "
        "read"
    )
    # Decisions-needed bullet still surfaces (kill-switch branch
    # writes it regardless of the read source).
    ideation_state = (
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    assert ideation_state.exists()
    text = ideation_state.read_text()
    assert "Decisions needed from operator" in text
    assert "AP2_FOCUS_AUTO_ADVANCE_DISABLED" in text


def test_threshold_via_cfg_route_drives_halt(cfg, clean_env, emit_reset):
    """End-to-end pin: setting `AP2_FOCUS_ADVANCE_EMPTY_CYCLES=1`
    after cfg load makes one empty cycle trip the heuristic and emits
    the `roadmap_complete` halt event. Exercises the cfg-routed read
    path (`_advance_empty_cycles_threshold(cfg)` inside
    `_maybe_advance_focus`) for the threshold lookup; kill-switch off
    so the halt lands. TB-342: the detector emits the halt directly
    (the pre-TB-342 pointer walk is gone).
    """
    clean_env.setenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "1")
    _write_goal_with_foci(cfg, "alpha", "beta")
    _emit_ideation_empty_cycle(cfg)

    asyncio.run(_maybe_advance_focus(cfg, sdk=None))

    pointer = goal.load_pointer(cfg)
    assert pointer["roadmap_complete_emitted"] is True, (
        "one empty cycle at threshold=1 should trip the halt via the "
        "cfg-routed threshold read"
    )
    # Event landed.
    tail = events.tail(cfg.events_file, 50)
    rc = [e for e in tail if e.get("type") == "roadmap_complete"]
    assert len(rc) == 1
    assert rc[-1]["trigger"] == "empty_cycles_heuristic"
