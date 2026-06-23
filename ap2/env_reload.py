"""Per-tick re-source of `.cc-autopilot/env` (TB-271).

Removes the restart-to-apply-a-knob friction TB-260 only warned about.
The daemon's `_tick` calls `maybe_reload_env(cfg)` at the top of every
tick, BEFORE operator-queue drain / cron / pipeline sweep / ideation /
task dispatch, so a fresh env-file edit takes effect on the very next
tick instead of waiting for `ap2 stop && ap2 start`.

Design notes:

  * **mtime-gated.** The reload reads the env file's current mtime and
    only re-parses + refreshes when it changed since the last reload.
    A no-op on the common unchanged-file tick (the daemon's main loop
    runs every 30s and the env file rarely changes).
  * **os.environ-precedence gotcha.** `config.load_project_env` skips
    any key already in `os.environ` ("existing env vars win"), so a
    naive second invocation picks up NOTHING. The reload tracks which
    keys were originally file-sourced (the dict
    `load_project_env` returns at startup, threaded through
    `note_initial_applied`) and overwrites `os.environ` ONLY for that
    set — preserving "shell export wins" for keys the operator set
    only in their shell.
  * **In-place Config mutation.** `Config` is a non-frozen dataclass.
    The reload helper rewrites the tunable attributes on the existing
    instance so downstream stages on this tick (and every later tick)
    see the fresh values without re-threading a new Config through
    every call site. Structural fields (`project_root`, file paths,
    `next_task_id`) are NEVER mutated — those are identity, not
    tunables, and a refactor that tried to swap them would silently
    re-bind paths mid-run.
  * **Hot-reloadable vs fixed split.** Knobs that configure a stateful
    resource (a bound socket, a subscribed channel set) can't take
    effect mid-run without re-running the startup code that built it.
    Those live in `FIXED_KNOBS` and DON'T hot-reload — the operator
    still needs `ap2 stop && ap2 start`. Everything else is a tunable
    read fresh each time it's consulted (timeouts, max-turns, model
    name, thresholds) and lives in `HOT_RELOADABLE_KNOBS`.
  * **TB-260 stale-warning interaction.** When a reload completes AND
    every changed key is hot-reloadable, the helper advances the
    `env_file_mtime_at_start` baseline in `daemon_state.json` so the
    stale-warning clears. If any changed key is a fixed knob, the
    baseline is left alone — the warning stays live to remind the
    operator that knob still needs a restart.
"""
from __future__ import annotations

import os
from dataclasses import fields as _dataclass_fields
from pathlib import Path

from . import events
from .config import (
    CONFIG_TOML_FILE,
    ENV_FILE,
    Config,
    DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S,
    DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S,
    DEFAULT_CONTROL_TIMEOUT_S,
    DEFAULT_EVENT_CONTEXT_SIZE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MM_TICK_INTERVAL_S,
    DEFAULT_TASK_TIMEOUT_S,
    DEFAULT_TICK_INTERVAL_S,
    DEFAULT_VERIFY_TIMEOUT_S,
)
from .daemon_state import _load_daemon_state, _save_daemon_state


# Hot-reloadable knob set. Edits to any of these take effect on the
# next tick without a daemon restart. The list mirrors the briefing's
# "Hot-reloadable" enumeration. Each one is either (a) a tunable
# read fresh from os.environ at use-time (model, effort, max-turns),
# or (b) a `Config` dataclass field that the reload helper rewrites
# in-place after re-parsing the env file.
HOT_RELOADABLE_KNOBS: frozenset[str] = frozenset({
    # Timeouts / retry budgets (Config dataclass fields)
    "AP2_TASK_TIMEOUT_S",
    "AP2_CONTROL_TIMEOUT_S",
    "AP2_VERIFY_TIMEOUT_S",
    "AP2_VALIDATOR_JUDGE_TIMEOUT_S",
    "AP2_TASK_MAX_TURNS",
    "AP2_CONTROL_MAX_TURNS",
    "AP2_IDEATION_MAX_TURNS",
    "AP2_MAX_RETRIES",
    # Agent model / effort — read from os.environ each query() call
    "AP2_AGENT_MODEL",
    "AP2_AGENT_EFFORT",
    # TB-356: kill switch for the thinking-block-400 effort-downshift path.
    # Read fresh via `cfg.get_core_value` at each task dispatch /
    # failure-classification, so a toggle takes effect on the next tick
    # without a daemon restart — same call-time-read shape as AP2_AGENT_EFFORT.
    "AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED",
    # Ideation
    "AP2_IDEATION_DISABLED",
    "AP2_IDEATION_TRIGGER_TASK_COUNT",
    "AP2_IDEATION_COOLDOWN_S",
    # TB-284: model for the post-write scrub that strips exhaustion
    # language from `ideation_state.md` after each ideation cycle.
    # Read fresh from `os.environ` inside
    # `ideation_scrub._resolved_model()` at call-time so a hot-reload
    # propagates without rebinding any cached state — parallel to
    # `AP2_AGENT_MODEL` / `AP2_AGENT_EFFORT`.
    "AP2_IDEATION_SCRUB_MODEL",
    # Auto-approve / auto-unfreeze thresholds (all read from os.environ
    # at decision-time inside `auto_approve.py` / `auto_unfreeze.py`).
    # TB-427/TB-430: auto-approve ENABLEMENT now resolves through the
    # registry's single source of truth (`Manifest.is_enabled`), which
    # reads the sectioned `AP2_COMPONENTS_<NAME>_<KEY>` env override →
    # the flat master flag → config.toml → legacy override. TB-430
    # flipped auto-approve to default-on / opt-out: the flat master
    # switch is now the suppress-polarity kill switch
    # `AP2_AUTO_APPROVE_DISABLED` (tier-2 of that resolution). The legacy
    # require-polarity `AP2_AUTO_APPROVE` is retained as a hot-reloadable
    # transitional back-compat override (final tier). Both stay
    # hot-reloadable so a `.cc-autopilot/env` edit toggling either takes
    # effect on the next tick without a daemon restart (and they remain
    # the canonical knobs for the env-only paths: a `cfg=None` gate call
    # and the daemon's shell-pin effective-config snapshot).
    "AP2_AUTO_APPROVE_DISABLED",
    "AP2_AUTO_APPROVE",
    "AP2_AUTO_APPROVE_DRY_RUN",
    "AP2_AUTO_APPROVE_GATE_TAGS",
    "AP2_AUTO_APPROVE_FREEZE_THRESHOLD",
    "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
    "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP",
    "AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED",
    "AP2_AUTO_UNFREEZE_FIX_SHAPES",
    "AP2_AUTO_UNFREEZE_DRY_RUN",
    "AP2_AUTO_UNFREEZE_MAX_PER_TASK",
    "AP2_AUTO_UNFREEZE_MAX_PER_DAY",
    # TB-320: kill-switch knob added with the auto_unfreeze manifest
    # env_flag wiring. Read lazily from `os.environ` inside
    # `auto_unfreeze._is_auto_unfreeze_disabled()` at tick-hook
    # call-time so a hot-reload toggling the knob takes effect on
    # the next tick without a daemon restart. Mirrors the existing
    # auto-unfreeze-family knobs above and the polarity / hot-reload
    # treatment of `AP2_VALIDATOR_JUDGE_DISABLED` /
    # `AP2_IDEATION_HALT_DISABLED`.
    "AP2_AUTO_UNFREEZE_DISABLED",
    # Project-wide verify gate (Config dataclass fields)
    "AP2_VERIFY_CMD",
    # Tick intervals (Config dataclass fields — next sleep picks up the
    # new value; the in-flight sleep finishes at the old interval, but
    # that's at most one tick of drift, which is fine)
    "AP2_TICK_S",
    "AP2_MM_TICK_S",
    "AP2_EVENT_CONTEXT",
    # Watchdog (Config dataclass fields)
    "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
    "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
    # Validator escape hatches (read from os.environ at validator call time)
    "AP2_VALIDATOR_JUDGE_DISABLED",
    "AP2_VALIDATOR_JUDGE_MAX_TURNS",
    "AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD",
    # Kill switch for the optional LLM prose-bullet verify judge. TB-386
    # demoted the judge out of `ap2/components/` into the core verify runner;
    # `verify.verify_task` reads this knob live via `os.environ.get` on each
    # call, so a toggle takes effect on the next verification without a daemon
    # restart — same hot-reload treatment as `AP2_VALIDATOR_JUDGE_DISABLED`.
    "AP2_VERIFY_JUDGE_DISABLED",
    # Ideation-exhaustion halt (TB-345 — read fresh from os.environ at
    # tick-time inside `ideation_halt.maybe_halt_on_exhaustion` via
    # `cfg.get_core_value(...)`, so a hot-reload propagates on the next
    # tick without a daemon restart).
    "AP2_IDEATION_HALT_DISABLED",
    "AP2_IDEATION_HALT_EMPTY_CYCLES",
    # TB-280: project identity prefix for the status-report headline.
    # Operator renames should not require a daemon restart — the next
    # `## Current state` snapshot build picks up the new value via the
    # `_refresh_tunable_config_fields` rewrite below.
    "AP2_PROJECT_NAME",
    # TB-282: proactive attention-raised detector knobs. Both are
    # read fresh from `os.environ` at detection-time inside
    # `ap2/attention.py` (`_task_stuck_threshold_s` /
    # `_attention_debounce_s`), so a hot-reload propagates without
    # any Config-dataclass rewrite. They tune detection sensitivity,
    # not lifecycle — an operator tightening the stuck-task floor
    # from 4h to 2h should not require a daemon restart.
    "AP2_TASK_STUCK_THRESHOLD_S",
    "AP2_ATTENTION_DEBOUNCE_S",
    # TB-287: `task_frozen` detector recency window — read fresh from
    # `os.environ` at detection-time inside
    # `ap2/attention.py` (`_task_frozen_recency_s`), same shape as the
    # TB-282 pair above.
    "AP2_TASK_FROZEN_RECENCY_S",
    # TB-290: `cost_cap_approach` detector percentage threshold — read
    # fresh from `os.environ` at detection-time inside
    # `ap2/attention.py` (`_cost_approach_pct`), same shape as the
    # TB-282 / TB-287 detector-sensitivity knobs above. An operator
    # tightening the threshold (e.g. 75 → 50 for an earlier nudge)
    # takes effect on the next tick without a daemon restart.
    "AP2_AUTO_APPROVE_COST_APPROACH_PCT",
    # TB-297: opt-in immediate-Mattermost-push on `attention_raised`
    # emission. Read fresh from `os.environ` at push-decision time
    # inside `daemon._maybe_push_attention` so an operator toggling
    # the knob on/off takes effect on the next tick without a daemon
    # restart — symmetric with the detector-sensitivity knobs above.
    "AP2_ATTENTION_IMMEDIATE_PUSH",
})

# Lifecycle knobs that CAN'T hot-reload. Each configures a stateful
# resource (a bound socket, a subscribed channel set) that's wired up
# once at daemon-start and can't be re-applied without re-running the
# startup code. Operator still needs `ap2 stop && ap2 start` to pick
# them up; TB-260's stale-warning persists when any of these change so
# the operator sees the nudge.
FIXED_KNOBS: frozenset[str] = frozenset({
    # Web server — bound to AP2_WEB_PORT at startup in `_web_loop_for_daemon`.
    # Rebinding requires the web task to die and re-spawn on the new port.
    "AP2_WEB_PORT",
    "AP2_WEB_DISABLED",
    # Mattermost — subscribed to AP2_MM_CHANNELS at startup in `_mm_loop`.
    # Subscription set is read once; resubscribing requires a restart.
    "AP2_MM_CHANNELS",
})


# Module-level reload bookkeeping. Seeded by `note_initial_applied`
# (called from `Config.load`) with the dict of keys file-sourced at
# startup, so the reload helper knows which keys are safe to overwrite
# on a later reload vs which were set only by a genuine shell export.
#
# `last_toml_mtime` (TB-323) extends the watcher to the
# `.cc-autopilot/config.toml` file: a bumped mtime on EITHER the env
# file OR the TOML file un-no-ops the per-tick reload, so an operator
# editing `config.toml` to bump a hot-reloadable knob (e.g.
# `[components.attention] task_stuck_threshold_s = ...`) gets the
# same next-tick propagation the env file already enjoys. The TOML
# values themselves are not re-parsed here — the helper still pulls
# tunables from `os.environ` (the structured-config layer's overrides
# already wrote there at daemon-start). A TOML-only edit therefore
# refreshes the `Config` dataclass via `_refresh_tunable_config_fields`,
# picking up any companion env-side change the operator also made.
_RELOAD_STATE: dict = {
    "file_keys": None,        # set[str] | None
    "last_mtime": None,       # float | None  — `.cc-autopilot/env` mtime
    "last_toml_mtime": None,  # float | None  — `.cc-autopilot/config.toml` mtime
}


def note_initial_applied(
    project_root: Path,
    initial_applied: dict[str, str],
) -> None:
    """Seed the reload tracker at `Config.load` time (TB-271).

    `initial_applied` is the dict `load_project_env` returned on the
    startup pass — the set of keys the env file actually wrote into
    `os.environ` (post the "shell export wins" check). Hand them to
    the reload helper so it knows which keys are safe to overwrite on
    a later reload vs which were already shell-exported and must keep
    shell-precedence.

    Also pins the env file's startup mtime so the first
    `maybe_reload_env` call no-ops when the file hasn't been touched
    since boot — the common case for the first tick.
    """
    env_file = project_root / ENV_FILE
    toml_file = project_root / CONFIG_TOML_FILE
    _RELOAD_STATE["file_keys"] = set(initial_applied.keys())
    try:
        _RELOAD_STATE["last_mtime"] = (
            env_file.stat().st_mtime if env_file.exists() else None
        )
    except OSError:
        _RELOAD_STATE["last_mtime"] = None
    # TB-323: pin the TOML file's startup mtime so the next
    # `maybe_reload_env` call no-ops when neither file has been touched
    # since boot. None when the file is absent (today's default — opt-in
    # via `Config.load`'s TOML branch).
    try:
        _RELOAD_STATE["last_toml_mtime"] = (
            toml_file.stat().st_mtime if toml_file.exists() else None
        )
    except OSError:
        _RELOAD_STATE["last_toml_mtime"] = None


def reset_reload_state_for_tests() -> None:
    """Reset the module-level cache. Call from a test's setup to start
    from a known empty state.

    Production never calls this — `note_initial_applied` is the
    single seed point.
    """
    _RELOAD_STATE["file_keys"] = None
    _RELOAD_STATE["last_mtime"] = None
    _RELOAD_STATE["last_toml_mtime"] = None


def _parse_env_file(env_file: Path) -> dict[str, str]:
    """Parse `.cc-autopilot/env` (KEY=VALUE lines) into a dict.

    Same line-format rules as `config.load_project_env` (blank /
    `#`-comment lines skipped; single- or double-quote pairs stripped)
    BUT does not mutate `os.environ` — the reload helper applies its
    own precedence decisions on the parsed result.
    """
    parsed: dict[str, str] = {}
    if not env_file.exists():
        return parsed
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val and len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if not key:
            continue
        parsed[key] = val
    return parsed


def _refresh_tunable_config_fields(cfg: Config) -> None:
    """Rewrite the tunable `Config` dataclass fields from `os.environ`.

    Mirrors the per-field reads in `Config.load` but mutates the
    existing instance instead of constructing a new one — keeps cfg
    identity stable across the tick so every helper holding a
    reference sees the refreshed values without re-threading.

    Structural fields (`project_root`, file paths, `next_task_id`)
    are NEVER touched here; they're identity, not tunables.
    """
    cfg.tick_interval_s = int(
        os.environ.get("AP2_TICK_S", DEFAULT_TICK_INTERVAL_S)
    )
    cfg.mm_tick_interval_s = int(
        os.environ.get("AP2_MM_TICK_S", DEFAULT_MM_TICK_INTERVAL_S)
    )
    cfg.event_context_size = int(
        os.environ.get("AP2_EVENT_CONTEXT", DEFAULT_EVENT_CONTEXT_SIZE)
    )
    cfg.task_timeout_s = int(
        os.environ.get("AP2_TASK_TIMEOUT_S", DEFAULT_TASK_TIMEOUT_S)
    )
    cfg.control_timeout_s = int(
        os.environ.get("AP2_CONTROL_TIMEOUT_S", DEFAULT_CONTROL_TIMEOUT_S)
    )
    cfg.max_retries = int(
        os.environ.get("AP2_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    )
    cfg.verify_cmd = os.environ.get("AP2_VERIFY_CMD", "").strip()
    cfg.verify_timeout_s = int(
        os.environ.get("AP2_VERIFY_TIMEOUT_S", DEFAULT_VERIFY_TIMEOUT_S)
    )
    cfg.auto_diagnose_idle_threshold_s = int(
        os.environ.get(
            "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
            DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S,
        )
    )
    cfg.auto_diagnose_cooldown_s = int(
        os.environ.get(
            "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
            DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S,
        )
    )
    # TB-280: project identity prefix for the status-report headline.
    # Mirrors `Config.load`'s "env override OR project_root.name" rule
    # so a hot-reload reflects the same default-resolution semantics
    # as a fresh daemon start. Whitespace-stripped so a stray space in
    # the env file doesn't render a leading space in the bracketed
    # headline.
    cfg.project_name = (
        os.environ.get("AP2_PROJECT_NAME", "").strip()
        or cfg.project_root.name
    )


def maybe_reload_env(cfg: Config) -> dict | None:
    """Re-source `.cc-autopilot/env` at the top of a daemon tick (TB-271).

    Cheap no-op when the env file's mtime hasn't changed since the
    last reload (the common case — the daemon tick runs every 30s and
    the env file is edited rarely). When it has:

      1. Parse the file (no `os.environ` side-effects yet).
      2. For each parsed `KEY=value`:
         - If `KEY` is in the tracked `file_keys` set (was either
           file-sourced at startup OR file-sourced on a prior reload):
           overwrite `os.environ[KEY]` so the new value takes effect.
         - Otherwise `KEY` is new to the file. If it's already in
           `os.environ` (a shell export from the daemon's parent
           process), leave it alone — "shell export wins" still holds
           for keys the file never set. If it's not in `os.environ`,
           apply it and track it in `file_keys`.
      3. Rewrite the tunable `Config` dataclass fields from the
         refreshed `os.environ` (in-place — preserves cfg identity).
      4. Emit `env_reloaded` with the list of changed keys (omit when
         no values actually changed — a touch / comment edit bumps the
         mtime but shouldn't be a noisy event).
      5. If every changed key is in `HOT_RELOADABLE_KNOBS`, advance
         the TB-260 `env_file_mtime_at_start` baseline in
         `daemon_state.json` so the stale-warning clears. If ANY
         changed key is in `FIXED_KNOBS`, leave the baseline alone so
         the warning stays live prompting an `ap2 stop && ap2 start`.

    Returns the dict of changed keys (`{key: (old, new)}`) on a
    successful reload, or `None` when the call was a no-op (mtime
    unchanged or no values differed). Caller can use the return value
    for logging / metrics; the daemon tick path only cares about the
    side-effects.

    Best-effort by design: a parse hiccup or os-error reading the file
    surfaces as a swallowed exception (caller's outer try/except in
    `_tick` catches it) — the daemon continues on the stale Config
    rather than dying mid-tick.
    """
    env_file = cfg.env_file
    # TB-323: also watch `.cc-autopilot/config.toml` mtime — an operator
    # editing the TOML file should get the same next-tick HOT_RELOADABLE
    # refresh pass an env-file edit triggers today. Derive the path from
    # `cfg.project_root` rather than adding a Config dataclass field
    # (the new attribute would ripple through `_load_env_path`'s
    # positional constructor; deriving keeps the reload contract local).
    toml_file = cfg.project_root / CONFIG_TOML_FILE
    try:
        current_mtime = (
            env_file.stat().st_mtime if env_file.exists() else None
        )
    except OSError:
        current_mtime = None
    try:
        current_toml_mtime = (
            toml_file.stat().st_mtime if toml_file.exists() else None
        )
    except OSError:
        current_toml_mtime = None

    cached_mtime = _RELOAD_STATE["last_mtime"]
    cached_toml_mtime = _RELOAD_STATE["last_toml_mtime"]
    env_mtime_changed = current_mtime != cached_mtime
    toml_mtime_changed = current_toml_mtime != cached_toml_mtime
    # mtime-gated no-op (the hot path) — neither watched file has
    # changed since the last reload.
    if not env_mtime_changed and not toml_mtime_changed:
        return None

    parsed = _parse_env_file(env_file)
    file_keys: set[str] = _RELOAD_STATE.get("file_keys") or set()

    changed: dict[str, tuple[str | None, str | None]] = {}
    for key, new_val in parsed.items():
        old_val = os.environ.get(key)
        if key in file_keys:
            # Previously file-sourced — refresh unconditionally so
            # operator edits propagate. Skip the os.environ write if
            # the value is byte-identical to avoid a spurious "changed"
            # entry (an unrelated edit on a different line shouldn't
            # claim this key changed).
            if old_val != new_val:
                os.environ[key] = new_val
                changed[key] = (old_val, new_val)
            continue
        # New key in the file. Honor "shell export wins": if it's
        # already in os.environ the operator's shell already set it
        # before the daemon started; leave it alone. Otherwise apply
        # and mark file-sourced.
        if old_val is not None:
            # Shell-export-wins. Don't track in `file_keys` either —
            # if the operator later un-exports it in their shell and
            # the file value should win, that takes a restart (the
            # daemon process inherits the shell's env once at fork
            # time, never re-reads it).
            continue
        os.environ[key] = new_val
        file_keys.add(key)
        changed[key] = (None, new_val)

    # Note: keys that disappeared from the env file are not unset
    # from os.environ. There's no reliable "previous default" to
    # restore (the parent shell may never have set them either, and
    # un-setting could nuke a downstream library's required var).
    # Operators reverting a knob set its value explicitly or restart.

    _RELOAD_STATE["file_keys"] = file_keys
    _RELOAD_STATE["last_mtime"] = current_mtime
    # TB-323: track the TOML file's mtime even when the env file is the
    # one that bumped. A subsequent TOML-only edit then un-no-ops the
    # next reload without false-firing on this tick's `current_toml_mtime`
    # already being baselined.
    _RELOAD_STATE["last_toml_mtime"] = current_toml_mtime

    if not changed:
        # Env file's value set didn't differ. If the TOML file ALSO didn't
        # change, this is a touch / comment edit / key reorder on the env
        # file — silent (no `env_reloaded` event, no Config refresh).
        # If the TOML file IS what changed, still trigger the
        # HOT_RELOADABLE-filtered refresh pass so an operator editing
        # `config.toml` to bump a tunable (with the matching env var
        # also re-exported) gets the same next-tick propagation the env
        # file already enjoys. The refresh re-reads `os.environ` — the
        # TOML file is not re-parsed here (TB-323 docstring above
        # captures this trade-off).
        if toml_mtime_changed:
            _refresh_tunable_config_fields(cfg)
        return None

    # Rewrite tunable Config fields from the refreshed os.environ.
    _refresh_tunable_config_fields(cfg)

    hot_changed = sorted(k for k in changed if k in HOT_RELOADABLE_KNOBS)
    fixed_changed = sorted(k for k in changed if k in FIXED_KNOBS)
    other_changed = sorted(
        k for k in changed
        if k not in HOT_RELOADABLE_KNOBS and k not in FIXED_KNOBS
    )

    events.append(
        cfg.events_file,
        "env_reloaded",
        changed=sorted(changed.keys()),
        hot=hot_changed,
        fixed=fixed_changed,
        other=other_changed,
    )

    # TB-260 interaction: clear the stale-warning baseline only when
    # every changed key is hot-reloadable. If a fixed knob changed,
    # leave the baseline alone so the operator still sees the
    # restart-required nudge — hot-reload doesn't help them apply a
    # fixed knob, and silencing the warning would mask the real need.
    # Also leave the baseline alone for `other_changed` keys: an
    # unknown knob might be load-bearing in a way the split doesn't
    # capture (defense-in-depth — better to false-warn than to silently
    # clear after refreshing a knob whose semantics we can't classify).
    if not fixed_changed and not other_changed and current_mtime is not None:
        try:
            state = _load_daemon_state(cfg)
            state["env_file_mtime_at_start"] = current_mtime
            _save_daemon_state(cfg, state)
        except OSError:
            # Best-effort — stale-warning will stay live; surfaces are
            # not load-bearing on this write succeeding.
            pass

    return changed


# Sanity-check at import time: every tunable `Config` field that
# `_refresh_tunable_config_fields` rewrites corresponds to a knob in
# `HOT_RELOADABLE_KNOBS`. Catches a refactor that adds a Config field
# without listing it as hot-reloadable (silently regresses to the
# pre-TB-271 "needs restart" behavior). The pairs map field-name →
# env-knob-name so a single source-of-truth lives here.
_TUNABLE_CONFIG_FIELDS: dict[str, str] = {
    "tick_interval_s": "AP2_TICK_S",
    "mm_tick_interval_s": "AP2_MM_TICK_S",
    "event_context_size": "AP2_EVENT_CONTEXT",
    "task_timeout_s": "AP2_TASK_TIMEOUT_S",
    "control_timeout_s": "AP2_CONTROL_TIMEOUT_S",
    "max_retries": "AP2_MAX_RETRIES",
    "verify_cmd": "AP2_VERIFY_CMD",
    "verify_timeout_s": "AP2_VERIFY_TIMEOUT_S",
    "auto_diagnose_idle_threshold_s": "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
    "auto_diagnose_cooldown_s": "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
    # TB-280: project identity hot-reloads via the same `os.environ.get(...)
    # or cfg.project_root.name` rule the startup pass uses.
    "project_name": "AP2_PROJECT_NAME",
}


def _self_check() -> None:
    """Catch import-time drift between the Config dataclass, the
    `_refresh_tunable_config_fields` writer, and the
    `HOT_RELOADABLE_KNOBS` set. Raises `AssertionError` at module
    import if any pair is missing, so a refactor breaks loudly.
    """
    declared_fields = {f.name for f in _dataclass_fields(Config)}
    for fname, knob in _TUNABLE_CONFIG_FIELDS.items():
        assert fname in declared_fields, (
            f"env_reload: tunable field {fname!r} not declared on Config"
        )
        assert knob in HOT_RELOADABLE_KNOBS, (
            f"env_reload: knob {knob!r} (rewriting Config.{fname}) is "
            f"not listed in HOT_RELOADABLE_KNOBS"
        )


_self_check()
