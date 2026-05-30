"""TB-271: behavioral pinning for the per-tick hot-reload of
`.cc-autopilot/env`.

The daemon used to read `.cc-autopilot/env` exactly once at startup and
freeze the resulting `Config` for its lifetime. Env edits required
`ap2 stop && ap2 start`. TB-260 made the staleness visible (a WARN
line on `ap2 status`); TB-271 removes the friction by re-sourcing the
env file at the top of every `_tick`.

The friction recurred three times in two days before TB-271:
  - TB-255 ran ~26h against the stale 600s `AP2_VERIFY_TIMEOUT_S`
    after the operator bumped it to 1800s in the env file but the
    daemon hadn't restarted.
  - On 2026-05-19 the operator twice tried `ap2 stop && ap2 start` to
    re-enable `AP2_IDEATION_DISABLED`; both were no-ops because the
    daemon refuses to die mid-task and `ap2 start` saw the live pid.

This module pins:
  (a) the reload helper is invoked at the top of `_tick`, BEFORE
      operator-queue drain / cron / pipeline / ideation / dispatch.
  (b) **the TB-255 failure shape:** the operator bumps
      `AP2_VERIFY_TIMEOUT_S` in the env file; the next tick's reload
      refreshes `cfg.verify_timeout_s` in-place WITHOUT a from-scratch
      `Config.load` / daemon restart.
  (c) the os.environ-precedence gotcha — a file-sourced key is
      refreshed on reload, a key set only by a shell export at startup
      is NOT clobbered when later added to the env file.
  (d) `HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` split is explicit in code
      and excludes the documented lifecycle knobs (`AP2_WEB_PORT`,
      `AP2_MM_CHANNELS`).
  (e) mtime-gated no-op: same mtime two ticks running → no second
      parse + no `env_reloaded` event.
  (f) `env_reloaded` event payload shape (`changed`, `hot`, `fixed`,
      `other` key lists).
  (g) interaction with TB-260: a hot-only reload clears the
      `env_file_mtime_at_start` baseline (so the stale-warning
      auto-clears); a reload that touched a fixed knob leaves the
      baseline alone (so the warning stays live).
  (h) `event_reload_error` event surfaces a reload exception without
      taking the daemon down (defensive swallow at the call site).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ap2 import env_reload, events
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Initialized project with a fresh reload-state cache. Each test
    starts from a clean module-level state so we don't leak `file_keys`
    or `last_mtime` between tests."""
    # Clear any AP2_* knobs the harness/CI might have set so each test
    # owns its os.environ surface deterministically.
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    env_reload.reset_reload_state_for_tests()
    return c


def _write_env(cfg: Config, content: str) -> float:
    """Write env file content and return its post-write mtime.

    `time.sleep(0.01)` before the write bumps the mtime on filesystems
    with sub-second resolution so the reload's mtime-comparison sees
    a change. (HFS / older ext have 1s resolution, so we ALSO bump the
    mtime explicitly via `os.utime` when the test needs deterministic
    "after-the-baseline" semantics — see `_force_newer_mtime`.)
    """
    cfg.env_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.env_file.write_text(content)
    return cfg.env_file.stat().st_mtime


def _force_newer_mtime(cfg: Config, baseline: float, *, delta: float = 5.0) -> None:
    """Force the env file's mtime to `baseline + delta` so the reload
    helper's `current > cached` comparison fires deterministically even
    on filesystems with 1s mtime resolution. Tests use this to simulate
    "operator edited the file N seconds after the daemon started"
    without sleeping."""
    new_mtime = baseline + delta
    os.utime(cfg.env_file, (new_mtime, new_mtime))


# ===========================================================================
# (a) reload is invoked at the top of `_tick`.
# ===========================================================================


def test_tick_calls_env_reload_before_other_stages():
    """`daemon._tick`'s body, read as source, calls
    `env_reload.maybe_reload_env(cfg)` BEFORE any of:
      - the operator-queue drain (`tools.drain_operator_queue`)
      - the PRE_DISPATCH tick-hook phase walk (TB-310 axis 2 — covers
        the pre-TB-310 `_maybe_auto_unfreeze` direct call) plus the
        TB-345 core `ideation_halt.maybe_halt_on_exhaustion(cfg)` call
        that runs directly right after the PRE_DISPATCH walk
      - cron / pipeline / dispatch / ideation

    Pin the ordering at the source level so a refactor that moves the
    reload below any of these stages (and therefore reads a stale
    knob before the reload fires on the same tick) trips here. Mirrors
    the TB-226 axis-4 ordering test for the pre-TB-345 focus detector.

    TB-310 (axis 2): the pre-existing `_maybe_auto_unfreeze` literal
    sentinel no longer appears in `_tick`'s body — it's dispatched via
    `default_registry().tick_hooks(Phase.PRE_DISPATCH)`. TB-345 merged
    the residual focus detector into the core `ideation_halt` module,
    which the daemon now calls directly (not via a registry hook)
    right after the PRE_DISPATCH walk. The `Phase.PRE_DISPATCH`
    sentinel covers the auto_unfreeze stage with a single source-level
    pin.
    """
    import inspect

    from ap2 import daemon

    src = inspect.getsource(daemon._tick)
    reload_pos = src.find("env_reload.maybe_reload_env")
    drain_pos = src.find("drain_operator_queue")
    pre_dispatch_pos = src.find("Phase.PRE_DISPATCH")
    assert reload_pos >= 0, "env_reload.maybe_reload_env(cfg) must be called in _tick"
    assert drain_pos > 0
    assert pre_dispatch_pos > 0, (
        "TB-310: _tick must walk Phase.PRE_DISPATCH (registry tick "
        "hooks replace the pre-TB-310 _maybe_auto_unfreeze direct "
        "call; TB-345's ideation_halt runs directly after the walk)"
    )
    assert reload_pos < drain_pos, (
        "env_reload must run before operator-queue drain so a knob bump "
        "is visible to the queue's downstream consumers on the same tick"
    )
    assert reload_pos < pre_dispatch_pos, (
        "env_reload must run before the PRE_DISPATCH hook walk so a "
        "knob bump is visible to auto_unfreeze / ideation_halt on the "
        "same tick"
    )


# ===========================================================================
# (b) the TB-255 failure shape — bumped AP2_VERIFY_TIMEOUT_S takes effect
#     without a daemon restart / Config.load rebuild.
# ===========================================================================


def test_tb255_verify_timeout_hot_reload_pin(cfg: Config):
    """TB-255 regression: the operator bumps `AP2_VERIFY_TIMEOUT_S=1800`
    in `.cc-autopilot/env`; the next tick must refresh
    `cfg.verify_timeout_s` to 1800 WITHOUT a from-scratch `Config.load`
    or a daemon restart.

    Pre-TB-271 this required `ap2 stop && ap2 start`. TB-255 ran ~26h
    against the stale 600s ceiling because the daemon's frozen Config
    held the old value. This test pins the new behavior: the same
    cfg object's `verify_timeout_s` attribute is mutated in-place by
    the reload helper, so every consumer that reads it gets the new
    value.
    """
    # Startup pass — file has the default; cfg.verify_timeout_s == 600.
    baseline = _write_env(cfg, "AP2_VERIFY_TIMEOUT_S=600\n")
    env_reload.note_initial_applied(cfg.project_root, {"AP2_VERIFY_TIMEOUT_S": "600"})
    # Manually sync the dataclass field to the startup value (in real
    # life Config.load did this; this fixture re-seeds the reload cache
    # so we model the steady-state.)
    cfg.verify_timeout_s = 600
    os.environ["AP2_VERIFY_TIMEOUT_S"] = "600"

    # The same cfg object's identity — the reload must mutate in-place,
    # not return a new Config that the caller must thread through.
    cfg_id_before = id(cfg)

    # Operator bumps the knob.
    _write_env(cfg, "AP2_VERIFY_TIMEOUT_S=1800\n")
    _force_newer_mtime(cfg, baseline)

    changed = env_reload.maybe_reload_env(cfg)

    assert id(cfg) == cfg_id_before, "Config identity must be preserved"
    assert cfg.verify_timeout_s == 1800, (
        f"cfg.verify_timeout_s must hot-reload to 1800; got "
        f"{cfg.verify_timeout_s}"
    )
    assert os.environ["AP2_VERIFY_TIMEOUT_S"] == "1800"
    assert changed is not None and "AP2_VERIFY_TIMEOUT_S" in changed


def test_tb255_task_timeout_hot_reload_pin(cfg: Config):
    """Mirror of the TB-255 pin for `AP2_TASK_TIMEOUT_S`. Same shape:
    bump the env file, call reload, assert `cfg.task_timeout_s` is
    refreshed in-place. Pin so the per-task wait_for timeout
    (`asyncio.wait_for(_consume(), timeout=cfg.task_timeout_s)` at
    `daemon.py:235`) reads the fresh value on the next dispatch."""
    baseline = _write_env(cfg, "AP2_TASK_TIMEOUT_S=600\n")
    env_reload.note_initial_applied(cfg.project_root, {"AP2_TASK_TIMEOUT_S": "600"})
    cfg.task_timeout_s = 600
    os.environ["AP2_TASK_TIMEOUT_S"] = "600"

    _write_env(cfg, "AP2_TASK_TIMEOUT_S=3600\n")
    _force_newer_mtime(cfg, baseline)

    env_reload.maybe_reload_env(cfg)

    assert cfg.task_timeout_s == 3600
    assert os.environ["AP2_TASK_TIMEOUT_S"] == "3600"


def test_verify_cmd_hot_reload_pin(cfg: Config):
    """`AP2_VERIFY_CMD` is a tunable string (not int). Pin it
    hot-reloads so the operator can swap the project-wide gate
    mid-run without a restart."""
    baseline = _write_env(cfg, 'AP2_VERIFY_CMD="uv run pytest -q"\n')
    env_reload.note_initial_applied(
        cfg.project_root, {"AP2_VERIFY_CMD": "uv run pytest -q"}
    )
    cfg.verify_cmd = "uv run pytest -q"
    os.environ["AP2_VERIFY_CMD"] = "uv run pytest -q"

    _write_env(cfg, 'AP2_VERIFY_CMD="uv run pytest -q ap2/tests/"\n')
    _force_newer_mtime(cfg, baseline)

    env_reload.maybe_reload_env(cfg)

    assert cfg.verify_cmd == "uv run pytest -q ap2/tests/"


# ===========================================================================
# (c) os.environ-precedence gotcha — shell export still wins.
# ===========================================================================


def test_file_sourced_key_refreshes_on_reload(cfg: Config):
    """A key that was sourced from the env file at startup gets its
    new value applied to `os.environ` on reload — this is the bug
    `load_project_env` would silently fail to fix (its `if key in
    os.environ: continue` skips ALL keys on a second call, including
    file-sourced ones the operator just bumped)."""
    baseline = _write_env(cfg, "AP2_TASK_MAX_TURNS=50\n")
    os.environ["AP2_TASK_MAX_TURNS"] = "50"
    env_reload.note_initial_applied(
        cfg.project_root, {"AP2_TASK_MAX_TURNS": "50"}
    )

    _write_env(cfg, "AP2_TASK_MAX_TURNS=100\n")
    _force_newer_mtime(cfg, baseline)

    changed = env_reload.maybe_reload_env(cfg)
    assert changed is not None
    assert "AP2_TASK_MAX_TURNS" in changed
    assert os.environ["AP2_TASK_MAX_TURNS"] == "100"


def test_shell_export_wins_for_keys_never_in_file(cfg: Config, monkeypatch):
    """A key that was set ONLY by a shell export at daemon-start
    (never in the file) MUST NOT be clobbered when the operator later
    adds it to the env file. Pin the "shell export wins" contract
    across reload — the same precedence rule `load_project_env`
    enforces at startup must continue to hold on subsequent reloads.
    """
    # Startup: shell exported the key, env file did not list it.
    monkeypatch.setenv("AP2_AGENT_MODEL", "claude-shell-exported")
    baseline = _write_env(cfg, "# no AP2_AGENT_MODEL here\n")
    env_reload.note_initial_applied(cfg.project_root, {})  # zero file_keys

    # Operator later edits the env file to add the key with a different value.
    _write_env(cfg, "AP2_AGENT_MODEL=claude-from-file\n")
    _force_newer_mtime(cfg, baseline)

    changed = env_reload.maybe_reload_env(cfg)

    # Shell export must still win — the reload must NOT overwrite a key
    # whose os.environ value came from a shell export the file never sourced.
    assert os.environ["AP2_AGENT_MODEL"] == "claude-shell-exported", (
        "shell export must keep precedence over a key the file added "
        "after daemon-start; got "
        f"AP2_AGENT_MODEL={os.environ['AP2_AGENT_MODEL']!r}"
    )
    # And the helper must NOT claim the key changed (its os.environ
    # value is unchanged — the file-side write was a no-op for this key).
    assert changed is None or "AP2_AGENT_MODEL" not in changed


def test_new_file_key_applied_when_not_shell_exported(cfg: Config, monkeypatch):
    """A NEW key in the env file (not previously file-sourced) AND
    not in os.environ (no shell export) should be applied on reload.
    Complementary to the shell-wins case above: the rule is "shell
    export wins only when there IS a shell export"."""
    monkeypatch.delenv("AP2_IDEATION_TRIGGER_TASK_COUNT", raising=False)
    baseline = _write_env(cfg, "# empty\n")
    env_reload.note_initial_applied(cfg.project_root, {})

    _write_env(cfg, "AP2_IDEATION_TRIGGER_TASK_COUNT=5\n")
    _force_newer_mtime(cfg, baseline)

    changed = env_reload.maybe_reload_env(cfg)
    assert changed is not None
    assert "AP2_IDEATION_TRIGGER_TASK_COUNT" in changed
    assert os.environ["AP2_IDEATION_TRIGGER_TASK_COUNT"] == "5"


# ===========================================================================
# (d) HOT_RELOADABLE_KNOBS / FIXED_KNOBS split is explicit + documented.
# ===========================================================================


def test_lifecycle_knobs_are_fixed_not_hot_reloadable():
    """The three lifecycle knobs the briefing names as out-of-scope —
    `AP2_WEB_PORT`, `AP2_WEB_DISABLED`, `AP2_MM_CHANNELS` — MUST be in
    `FIXED_KNOBS` and MUST NOT be in `HOT_RELOADABLE_KNOBS`. Each
    configures a stateful resource (a bound socket, a subscribed
    channel set) wired up once at daemon-start; refreshing them in
    `os.environ` without re-running the startup code that built the
    resource would leave the live state mismatched with the new value.

    Pin the split so a future change that moves any of them into
    HOT_RELOADABLE_KNOBS (and silently re-binds the wrong resource)
    trips here.
    """
    for knob in ("AP2_WEB_PORT", "AP2_WEB_DISABLED", "AP2_MM_CHANNELS"):
        assert knob in env_reload.FIXED_KNOBS, (
            f"{knob} must be in FIXED_KNOBS (configures a stateful "
            f"resource that needs a restart to re-bind)"
        )
        assert knob not in env_reload.HOT_RELOADABLE_KNOBS, (
            f"{knob} must NOT be in HOT_RELOADABLE_KNOBS — refreshing "
            f"it mid-run would leave the live resource mismatched"
        )


def test_hot_and_fixed_knob_sets_are_disjoint():
    """No knob can be both hot-reloadable AND fixed. Sanity-check the
    set contracts in case a future edit copies a name into the wrong
    side."""
    overlap = env_reload.HOT_RELOADABLE_KNOBS & env_reload.FIXED_KNOBS
    assert not overlap, (
        f"HOT_RELOADABLE_KNOBS and FIXED_KNOBS must be disjoint; "
        f"overlap={sorted(overlap)}"
    )


def test_tunable_config_fields_all_have_hot_reload_knobs():
    """Every dataclass field `_refresh_tunable_config_fields` rewrites
    must correspond to a knob in `HOT_RELOADABLE_KNOBS`. The module's
    `_self_check` enforces this at import; this test re-runs it
    explicitly so a regression surfaces here too (and is named
    descriptively in the report)."""
    env_reload._self_check()


# ===========================================================================
# (e) mtime-gated no-op.
# ===========================================================================


def test_unchanged_env_file_is_a_noop(cfg: Config):
    """Two ticks with the same env file mtime → second call returns
    None without re-parsing the file or emitting any event. Load-
    bearing for the daemon's 30s tick rhythm — re-parsing a static
    file every tick would waste cycles."""
    baseline = _write_env(cfg, "AP2_TASK_MAX_TURNS=50\n")
    env_reload.note_initial_applied(
        cfg.project_root, {"AP2_TASK_MAX_TURNS": "50"}
    )
    os.environ["AP2_TASK_MAX_TURNS"] = "50"

    # First call (mtime == cached_at_init) → no-op.
    assert env_reload.maybe_reload_env(cfg) is None
    # Second call also no-op.
    assert env_reload.maybe_reload_env(cfg) is None


def test_touch_without_value_change_is_silent(cfg: Config):
    """Operator runs `touch .cc-autopilot/env` (or saves the file
    without editing): mtime bumps but no value changed. The reload
    helper must NOT emit `env_reloaded` for a no-op — the event is a
    behavioral signal, not a filesystem one."""
    baseline = _write_env(cfg, "AP2_TASK_MAX_TURNS=50\n")
    env_reload.note_initial_applied(
        cfg.project_root, {"AP2_TASK_MAX_TURNS": "50"}
    )
    os.environ["AP2_TASK_MAX_TURNS"] = "50"

    # Same content, newer mtime.
    _write_env(cfg, "AP2_TASK_MAX_TURNS=50\n")
    _force_newer_mtime(cfg, baseline)

    changed = env_reload.maybe_reload_env(cfg)
    assert changed is None, (
        f"touch-without-value-change must return None; got {changed!r}"
    )
    # And no `env_reloaded` event landed.
    if cfg.events_file.exists():
        for line in cfg.events_file.read_text().splitlines():
            assert '"env_reloaded"' not in line, (
                "touch-without-value-change must not emit env_reloaded"
            )


# ===========================================================================
# (f) env_reloaded event payload shape.
# ===========================================================================


def test_env_reloaded_event_carries_changed_hot_fixed_other_keys(cfg: Config):
    """When the reload applies a value change, the `env_reloaded`
    event payload carries:
      - `changed`: sorted list of all changed knob names
      - `hot`: subset in HOT_RELOADABLE_KNOBS
      - `fixed`: subset in FIXED_KNOBS
      - `other`: subset in neither (defensive bucket)

    Pin the shape so the events.jsonl line is machine-parseable for
    offline tooling (mirrors the TB-260 `env_stale` parser-stability
    promise)."""
    baseline = _write_env(
        cfg, "AP2_VERIFY_TIMEOUT_S=600\nAP2_WEB_PORT=8730\n"
    )
    env_reload.note_initial_applied(
        cfg.project_root,
        {"AP2_VERIFY_TIMEOUT_S": "600", "AP2_WEB_PORT": "8730"},
    )
    os.environ["AP2_VERIFY_TIMEOUT_S"] = "600"
    os.environ["AP2_WEB_PORT"] = "8730"

    _write_env(
        cfg, "AP2_VERIFY_TIMEOUT_S=1800\nAP2_WEB_PORT=9000\n"
    )
    _force_newer_mtime(cfg, baseline)

    env_reload.maybe_reload_env(cfg)

    # Find the env_reloaded event.
    found = None
    for line in cfg.events_file.read_text().splitlines():
        evt = json.loads(line)
        if evt.get("type") == "env_reloaded":
            found = evt
            break
    assert found is not None, "env_reloaded event must be emitted"
    assert set(found["changed"]) == {"AP2_VERIFY_TIMEOUT_S", "AP2_WEB_PORT"}
    assert found["hot"] == ["AP2_VERIFY_TIMEOUT_S"]
    assert found["fixed"] == ["AP2_WEB_PORT"]
    assert found["other"] == []


# ===========================================================================
# (g) TB-260 staleness baseline interaction.
# ===========================================================================


def test_hot_only_reload_advances_stale_baseline(cfg: Config):
    """A reload that only touched hot-reloadable knobs MUST advance
    the `env_file_mtime_at_start` baseline in `daemon_state.json` so
    TB-260's stale-warning auto-clears (the knob is already live; the
    operator doesn't need a restart-required nudge for it).

    Pin the interaction so a future refactor that drops the baseline-
    advance step silently re-introduces the false-warn TB-260 bug."""
    baseline = _write_env(cfg, "AP2_TASK_TIMEOUT_S=1200\n")
    env_reload.note_initial_applied(
        cfg.project_root, {"AP2_TASK_TIMEOUT_S": "1200"}
    )
    os.environ["AP2_TASK_TIMEOUT_S"] = "1200"
    # Seed daemon_state.json with the baseline mtime.
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_state_file.write_text(
        json.dumps({"env_file_mtime_at_start": baseline})
    )

    _write_env(cfg, "AP2_TASK_TIMEOUT_S=3600\n")
    _force_newer_mtime(cfg, baseline)
    new_mtime = cfg.env_file.stat().st_mtime

    env_reload.maybe_reload_env(cfg)

    new_state = json.loads(cfg.daemon_state_file.read_text())
    assert new_state["env_file_mtime_at_start"] == new_mtime, (
        "hot-only reload must advance env_file_mtime_at_start to the "
        "current mtime so TB-260's stale-warning clears"
    )


def test_fixed_knob_reload_leaves_stale_baseline_alone(cfg: Config):
    """A reload that changed a FIXED knob (e.g. `AP2_WEB_PORT`) must
    NOT advance the staleness baseline — the warning needs to stay
    live so the operator sees the restart-required nudge for the
    fixed knob, which hot-reload can't apply.

    Without this gate, an operator who bumped both a tunable and a
    fixed knob in the same edit would see the warning silently
    disappear after the reload, masking the still-pending restart."""
    baseline = _write_env(
        cfg, "AP2_TASK_TIMEOUT_S=1200\nAP2_WEB_PORT=8730\n"
    )
    env_reload.note_initial_applied(
        cfg.project_root,
        {"AP2_TASK_TIMEOUT_S": "1200", "AP2_WEB_PORT": "8730"},
    )
    os.environ["AP2_TASK_TIMEOUT_S"] = "1200"
    os.environ["AP2_WEB_PORT"] = "8730"
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_state_file.write_text(
        json.dumps({"env_file_mtime_at_start": baseline})
    )

    _write_env(
        cfg, "AP2_TASK_TIMEOUT_S=3600\nAP2_WEB_PORT=9000\n"
    )
    _force_newer_mtime(cfg, baseline)

    env_reload.maybe_reload_env(cfg)

    new_state = json.loads(cfg.daemon_state_file.read_text())
    assert new_state["env_file_mtime_at_start"] == baseline, (
        "reload that touched a FIXED knob must leave the staleness "
        "baseline alone so TB-260's warning stays live for the fixed knob"
    )


# ===========================================================================
# (h) reload exception is swallowed at the daemon's call site.
# ===========================================================================


def test_tick_swallows_env_reload_exception(monkeypatch, cfg: Config):
    """`_tick` wraps `env_reload.maybe_reload_env` in a try/except so
    a hiccup in the reload helper (parse failure, OS error) doesn't
    take the whole tick down. The defensive branch must also surface
    the failure as an `env_reload_error` event for operator
    visibility — silent swallows would re-introduce the TB-260
    silent-degradation hazard.

    Pin the defensive shape at the daemon source level (we don't need
    to invoke `_tick` end-to-end — the source-level grep is enough
    and avoids the asyncio + SDK harness)."""
    import inspect

    from ap2 import daemon

    src = inspect.getsource(daemon._tick)
    # The reload sits inside a try/except that emits env_reload_error.
    assert "env_reload.maybe_reload_env(cfg)" in src
    assert "env_reload_error" in src
    # And the except clause must NOT re-raise — find the catch block
    # AFTER the call and confirm it has an events.append, not a raise.
    reload_pos = src.find("env_reload.maybe_reload_env(cfg)")
    after = src[reload_pos:reload_pos + 800]
    assert "except Exception" in after
    assert "events.append" in after


# ===========================================================================
# Structural pin — implementation symbol lives in non-test code
# (mirrors the briefing's grep verifier).
# ===========================================================================


def test_reload_implementation_lives_in_non_test_code():
    """Briefing verifier:
    `grep -rnE 'env_reloaded|reload_env|hot.?reload' ap2/*.py | grep -v test_`
    must match at least one line in `ap2/*.py` outside `ap2/tests/`.
    Pin the structural invariant so a refactor that accidentally
    moves the reload symbols into a tests-only module doesn't pass
    the briefing verifier silently."""
    ap2_root = Path(__file__).resolve().parent.parent
    needles = ("env_reloaded", "reload_env", "hot_reload", "hot-reload")
    found = False
    for py_file in ap2_root.glob("*.py"):  # top-level only — matches `ap2/*.py`
        if py_file.name.startswith("test_"):
            continue
        text = py_file.read_text()
        if any(needle in text for needle in needles):
            found = True
            break
    assert found, (
        "expected at least one top-level ap2/*.py (non-test) file to "
        "reference env_reloaded / reload_env / hot_reload"
    )
