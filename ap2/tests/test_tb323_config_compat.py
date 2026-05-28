"""TB-323: env-var override layer + back-compat map regression-pin module.

Pins axis-(2) of the **structured config (env → TOML)** focus (goal.md
L317-329):

  1. Sectioned env override applies — `AP2_COMPONENTS_AUTO_APPROVE_ENABLED=1`
     overrides `[components.auto_approve] enabled = false` on the loaded
     `Config.from_toml` result.
  2. Flat back-compat override applies — `AP2_AUTO_APPROVE=1` lands the
     value at the matching sectioned path on `cfg.components_config` AND
     fires exactly one `env_deprecated` event per process per knob; a
     second read on the same process stays silent.
  3. 12-factor `_KNOBS_STAYING_ENV_ONLY` entries don't fire
     `env_deprecated` even when present in env (the partition's
     env-only side is documented-permanent — no deprecation framing).
  4. `env_reload.maybe_reload_env`'s `config.toml` mtime trick triggers
     the HOT_RELOADABLE-filtered refresh on file change — an operator
     editing the TOML with a paired env-side bump gets the next-tick
     propagation the env file already enjoyed.
  5. Partition totality: every `AP2_*` knob in
     `ap2.init._TEMPLATE_EXEMPT_KNOBS` (TB-305's source-of-truth set)
     appears in EITHER `FLAT_TO_SECTIONED` or `_KNOBS_STAYING_ENV_ONLY`
     — no leakage. A future knob-adder's PR fails this gate until they
     pick a side.

Why these pin the axis-2 cleavage: TB-323 is the contract every
existing operator-tunable knob carries into the structured-config
world. A refactor that weakens the partition (a knob slips off both
sides) silently breaks back-compat — the operator's shell export
stops being a documented escape hatch. A refactor that drops the
sectioned-env override (the precedence head of the new vocabulary)
silently regresses to TOML-only resolution. A refactor that
forgets the one-shot semantics floods `events.jsonl` with a
deprecation event per tick per knob. The five tests cover each
failure shape.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ap2 import config_compat, env_reload, events
from ap2.config import CONFIG_TOML_FILE, ENV_FILE, Config
from ap2.config_compat import (
    FLAT_TO_SECTIONED,
    _KNOBS_STAYING_ENV_ONLY,
    apply_env_overrides,
    reset_env_deprecated_emit_for_tests,
)
from ap2.init import _TEMPLATE_EXEMPT_KNOBS, init_project


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every `AP2_*` env knob the harness/CI might have set so each
    test owns its `os.environ` surface deterministically."""
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def emit_reset():
    """Reset the module-level `_EMITTED_ONCE` set in `config_compat` so
    each test starts from clean one-shot accounting."""
    reset_env_deprecated_emit_for_tests()
    yield
    reset_env_deprecated_emit_for_tests()


def _write_toml(tmp_path: Path, body: str) -> Path:
    """Initialize a project skeleton at `tmp_path` and write `body` to
    `.cc-autopilot/config.toml`. Returns the file path. Mirrors the
    helper in `test_tb321_toml_config.py` so the two regression-pin
    modules build their fixtures the same way."""
    init_project(tmp_path)
    p = tmp_path / CONFIG_TOML_FILE
    p.write_text(body)
    return p


def _force_newer_mtime(path: Path, baseline: float, *, delta: float = 5.0) -> None:
    """Force `path`'s mtime to `baseline + delta` so the reload's
    `current != cached` comparison fires deterministically on
    filesystems with 1s mtime resolution (HFS, older ext)."""
    new_mtime = baseline + delta
    os.utime(path, (new_mtime, new_mtime))


def _read_events(events_file: Path) -> list[dict]:
    """Read the per-test events file as a list of dicts."""
    if not events_file.exists():
        return []
    import json
    out: list[dict] = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# (1) Sectioned env override applies.
# ---------------------------------------------------------------------------


def test_sectioned_env_override_applies_on_components_path(
    tmp_path, clean_env, emit_reset
):
    """`AP2_COMPONENTS_AUTO_APPROVE_ENABLED=1` overrides the loaded
    `[components.auto_approve] enabled = false` value on
    `cfg.components_config`."""
    _write_toml(
        tmp_path,
        "[components.auto_approve]\nenabled = false\n",
    )
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    cfg = Config.load(tmp_path)
    assert cfg.components_config["auto_approve"]["enabled"] is True


def test_sectioned_env_override_applies_on_core_field(
    tmp_path, clean_env, emit_reset
):
    """`AP2_CORE_TICK_INTERVAL_S=99` overrides the loaded
    `[core] tick_interval_s = 60` value on the `Config` dataclass."""
    _write_toml(
        tmp_path,
        "[core]\ntick_interval_s = 60\n",
    )
    clean_env.setenv("AP2_CORE_TICK_INTERVAL_S", "99")
    cfg = Config.load(tmp_path)
    assert cfg.tick_interval_s == 99


def test_sectioned_env_override_does_not_emit_env_deprecated(
    tmp_path, clean_env, emit_reset
):
    """Sectioned env names are the new canonical vocabulary — overrides
    via that path do NOT emit `env_deprecated` (no deprecation framing
    on a forward-compatible surface)."""
    _write_toml(
        tmp_path,
        "[components.auto_approve]\nenabled = false\n",
    )
    clean_env.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    cfg = Config.load(tmp_path)
    deprecations = [
        e for e in _read_events(cfg.events_file) if e.get("type") == "env_deprecated"
    ]
    assert deprecations == [], (
        f"sectioned-env override should not emit env_deprecated; got: {deprecations}"
    )


# ---------------------------------------------------------------------------
# (2) Flat back-compat override applies + one-shot env_deprecated.
# ---------------------------------------------------------------------------


def test_flat_back_compat_override_applies_to_sectioned_path(
    tmp_path, clean_env, emit_reset
):
    """`AP2_AUTO_APPROVE=1` (a flat-name knob present in
    `FLAT_TO_SECTIONED`) lands the value at
    `components.auto_approve.enabled` on `cfg.components_config`, even
    when the TOML omits the components section entirely."""
    _write_toml(tmp_path, "[core]\ntick_interval_s = 30\n")
    clean_env.setenv("AP2_AUTO_APPROVE", "1")
    cfg = Config.load(tmp_path)
    assert cfg.components_config.get("auto_approve", {}).get("enabled") is True


def test_flat_back_compat_override_overlays_existing_toml_value(
    tmp_path, clean_env, emit_reset
):
    """When the TOML already declares the sectioned key with a typed
    default (`enabled = false`), the flat-name env hit coerces against
    the existing type — `"1"` becomes `True` (bool), not int 1."""
    _write_toml(
        tmp_path,
        "[components.auto_approve]\nenabled = false\n",
    )
    clean_env.setenv("AP2_AUTO_APPROVE", "1")
    cfg = Config.load(tmp_path)
    assert cfg.components_config["auto_approve"]["enabled"] is True


def test_flat_back_compat_emits_env_deprecated_exactly_once(
    tmp_path, clean_env, emit_reset
):
    """Each flat-name hit fires exactly ONE `env_deprecated` event per
    process per knob. A second `apply_env_overrides` call (e.g. on a
    later config reload pass) stays silent — the module-level
    `_EMITTED_ONCE` set guards against per-tick repeats that would
    flood `events.jsonl`."""
    _write_toml(tmp_path, "[core]\ntick_interval_s = 30\n")
    clean_env.setenv("AP2_AUTO_APPROVE", "1")
    cfg = Config.load(tmp_path)

    deprecations = [
        e for e in _read_events(cfg.events_file) if e.get("type") == "env_deprecated"
    ]
    auto_approve_hits = [
        e for e in deprecations if e.get("flat") == "AP2_AUTO_APPROVE"
    ]
    assert len(auto_approve_hits) == 1, (
        f"expected exactly one env_deprecated for AP2_AUTO_APPROVE; "
        f"got: {auto_approve_hits}"
    )
    # Second read should stay silent — re-apply against the same cfg.
    apply_env_overrides(cfg)
    deprecations_after = [
        e for e in _read_events(cfg.events_file) if e.get("type") == "env_deprecated"
    ]
    auto_approve_after = [
        e for e in deprecations_after if e.get("flat") == "AP2_AUTO_APPROVE"
    ]
    assert len(auto_approve_after) == 1, (
        f"second read should not re-emit; got: {auto_approve_after}"
    )


def test_env_deprecated_payload_shape(tmp_path, clean_env, emit_reset):
    """The `env_deprecated` event payload carries `flat`, `sectioned`,
    and `process_pid` — the briefing's pinned shape. `ts` is auto-added
    by `events.append`."""
    _write_toml(tmp_path, "[core]\ntick_interval_s = 30\n")
    clean_env.setenv("AP2_AUTO_APPROVE", "true")
    cfg = Config.load(tmp_path)
    hits = [
        e for e in _read_events(cfg.events_file)
        if e.get("type") == "env_deprecated" and e.get("flat") == "AP2_AUTO_APPROVE"
    ]
    assert len(hits) == 1
    evt = hits[0]
    assert evt["flat"] == "AP2_AUTO_APPROVE"
    assert evt["sectioned"] == "components.auto_approve.enabled"
    assert evt["process_pid"] == os.getpid()
    assert "ts" in evt


# ---------------------------------------------------------------------------
# (3) 12-factor knobs don't fire env_deprecated.
# ---------------------------------------------------------------------------


def test_env_only_knobs_do_not_fire_env_deprecated(
    tmp_path, clean_env, emit_reset
):
    """Knobs in `_KNOBS_STAYING_ENV_ONLY` (Mattermost auth identity,
    integration secrets, channel-subscription identity, deployment
    paths) NEVER emit `env_deprecated` even when present in
    `os.environ` — they don't migrate to TOML by design, so there's
    no deprecation framing to surface."""
    _write_toml(tmp_path, "[core]\ntick_interval_s = 30\n")
    # Set one representative knob from each category in the env-only set
    # — covers the documentation cut-line in `_KNOBS_STAYING_ENV_ONLY`.
    clean_env.setenv("AP2_MM_BOT_USER_ID", "bot-uid-12345")
    clean_env.setenv("AP2_MM_CHANNELS", "channel-id-12345")
    clean_env.setenv("AP2_WEBHOOK_URL", "https://example.com/hook")
    clean_env.setenv("AP2_CHANNEL_FILE_PATH", "/tmp/ap2-channel.log")
    cfg = Config.load(tmp_path)
    deprecations = [
        e for e in _read_events(cfg.events_file) if e.get("type") == "env_deprecated"
    ]
    env_only_hits = [
        e for e in deprecations if e.get("flat") in _KNOBS_STAYING_ENV_ONLY
    ]
    assert env_only_hits == [], (
        f"12-factor exempt knobs should never fire env_deprecated; got: "
        f"{env_only_hits}"
    )


# ---------------------------------------------------------------------------
# (4) env_reload watches config.toml mtime.
# ---------------------------------------------------------------------------


def test_env_reload_config_toml_mtime_triggers_hot_reload(
    tmp_path, clean_env, emit_reset
):
    """`env_reload.maybe_reload_env` un-no-ops on a `.cc-autopilot/config.toml`
    mtime bump even when `.cc-autopilot/env` is unchanged — triggers
    the same HOT_RELOADABLE-filtered refresh pass an env-file edit
    triggers today. The refresh re-reads `os.environ`, so a paired
    env-side bump propagates onto the `Config` dataclass on the next
    tick.
    """
    toml_path = _write_toml(tmp_path, "[core]\ntick_interval_s = 60\n")
    cfg = Config.load(tmp_path)
    # Baseline: cfg.task_timeout_s should reflect the (default) startup
    # value. We'll bump the os.environ value to simulate an operator
    # change paired with a TOML edit.
    baseline_timeout = cfg.task_timeout_s
    new_timeout = baseline_timeout + 333
    clean_env.setenv("AP2_TASK_TIMEOUT_S", str(new_timeout))
    # Touch the TOML file to advance its mtime past the cached baseline.
    toml_mtime_before = toml_path.stat().st_mtime
    _force_newer_mtime(toml_path, toml_mtime_before)
    # The env file's mtime stays the same — only the TOML file changed.
    result = env_reload.maybe_reload_env(cfg)
    # The reload returns None (TOML-only changes are silent — no
    # `env_reloaded` event), but the Config dataclass field IS refreshed.
    assert result is None
    assert cfg.task_timeout_s == new_timeout, (
        f"TOML mtime bump should have triggered tunable refresh; "
        f"cfg.task_timeout_s={cfg.task_timeout_s}, expected={new_timeout}"
    )


def test_env_reload_no_op_when_neither_file_changed(
    tmp_path, clean_env, emit_reset
):
    """The hot-path no-op: neither env file nor TOML file mtime changed
    since the last reload → silent return. Pins the cheap-tick
    behavior — `maybe_reload_env` runs on every tick and must not
    re-parse on a static project."""
    _write_toml(tmp_path, "[core]\ntick_interval_s = 60\n")
    cfg = Config.load(tmp_path)
    # First call after Config.load — note_initial_applied already pinned
    # both mtimes. A second invocation against the unchanged files is a
    # no-op.
    result = env_reload.maybe_reload_env(cfg)
    assert result is None


# ---------------------------------------------------------------------------
# (5) Partition totality against `_TEMPLATE_EXEMPT_KNOBS`.
# ---------------------------------------------------------------------------


def test_template_exempt_knobs_partitioned_by_compat_sets():
    """Every `AP2_*` knob in `ap2.init._TEMPLATE_EXEMPT_KNOBS` (TB-305's
    source-of-truth set listing every knob exempt from the per-project
    env scaffold) appears in EITHER `FLAT_TO_SECTIONED` or
    `_KNOBS_STAYING_ENV_ONLY`. No leakage — every existing knob gets
    either a documented migration path or an explicit 12-factor
    exemption."""
    flat_keys = set(FLAT_TO_SECTIONED.keys())
    partition = flat_keys | _KNOBS_STAYING_ENV_ONLY
    missing = sorted(_TEMPLATE_EXEMPT_KNOBS - partition)
    assert not missing, (
        f"TB-323 partition leakage: knob(s) in `_TEMPLATE_EXEMPT_KNOBS` "
        f"appear in NEITHER `FLAT_TO_SECTIONED` nor "
        f"`_KNOBS_STAYING_ENV_ONLY`: {missing}.\n\n"
        f"Pick a side: add to `FLAT_TO_SECTIONED` (with a sectioned "
        f"path that names where the knob lives in the structured "
        f"config), OR add to `_KNOBS_STAYING_ENV_ONLY` (and document "
        f"the cut-line rationale in the comment block above the "
        f"frozenset). The partition is the operator-facing migration "
        f"contract — a knob on neither side silently breaks back-compat "
        f"or hides a 12-factor secret."
    )


def test_flat_and_env_only_sets_are_disjoint():
    """`FLAT_TO_SECTIONED` and `_KNOBS_STAYING_ENV_ONLY` must be
    disjoint — a knob in BOTH would fire deprecation events for an
    explicitly env-only knob (and the runtime double-check in
    `_apply_flat_back_compat` would mask the contract by silently
    skipping). Pin the disjoint invariant here so a future edit
    listing a knob in both sides fails this test loudly."""
    overlap = set(FLAT_TO_SECTIONED.keys()) & _KNOBS_STAYING_ENV_ONLY
    assert not overlap, (
        f"`FLAT_TO_SECTIONED` and `_KNOBS_STAYING_ENV_ONLY` must be "
        f"disjoint; overlap: {sorted(overlap)}"
    )


def test_flat_to_sectioned_paths_use_known_section_prefix():
    """Every sectioned path in `FLAT_TO_SECTIONED` starts with `core.`
    or `components.<name>.` — the two section prefixes
    `config_compat._set_path` knows how to write. Catches a typo'd
    map entry (e.g. `"compoents.foo.bar"`) before it silently
    fails to apply at daemon start."""
    bad = sorted(
        (flat, sectioned)
        for flat, sectioned in FLAT_TO_SECTIONED.items()
        if not (sectioned.startswith("core.") or sectioned.startswith("components."))
    )
    assert not bad, (
        f"FLAT_TO_SECTIONED entries with unknown section prefix: {bad}"
    )


# ---------------------------------------------------------------------------
# Sanity: events module knows the type (the docs-drift gate checks this).
# ---------------------------------------------------------------------------


def test_env_deprecated_event_type_registered(tmp_path, clean_env, emit_reset):
    """`env_deprecated` appears in the `ap2/events.py` vocabulary
    docstring so the docs-drift gate (TB-203) and the coverage-drift
    gate (TB-204) pick it up as a known event type. Smoke-checked
    here in addition to the docs-drift gate so a future refactor
    that drops the docstring entry fails this regression-pin module
    directly, not a generic `test_every_event_type_documented` failure
    elsewhere in the suite.
    """
    events_path = Path(events.__file__)
    text = events_path.read_text()
    assert "env_deprecated" in text, (
        "env_deprecated event type should appear in ap2/events.py — "
        "either as a docstring entry or an `events.append(...)` call"
    )


# ---------------------------------------------------------------------------
# Sanity: config_compat avoids static component imports (TB-311 parity).
# ---------------------------------------------------------------------------


def test_config_compat_has_no_static_component_import():
    """Mirror the briefing's `! grep -qE "^from ap2\\.components"` gate
    in a python-level assertion: no line in `ap2/config_compat.py`
    starts with `from ap2.components` (lazy / inline imports inside
    function bodies are still allowed, but they'd be indented and
    therefore not match the import-line anchor)."""
    compat_path = Path(config_compat.__file__)
    for line in compat_path.read_text().splitlines():
        assert not line.startswith("from ap2.components"), (
            f"config_compat.py must not statically import from "
            f"ap2.components (TB-311 import-direction gate parity); "
            f"offending line: {line!r}"
        )
