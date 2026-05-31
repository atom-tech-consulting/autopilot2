"""Paths, constants, and project configuration for autopilot v2.

All shared state lives under `.cc-autopilot/` (the v1 directory — v2 reuses it so
projects don't need a migration). Paths can be overridden by the project's
CLAUDE.md `## Autopilot` section.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


AUTOPILOT_DIR_NAME = ".cc-autopilot"
DEFAULT_TASKS_FILE = "TASKS.md"
DEFAULT_PROGRESS_FILE = f"{AUTOPILOT_DIR_NAME}/progress.md"
DEFAULT_TASKS_DIR = f"{AUTOPILOT_DIR_NAME}/tasks"
EVENTS_FILE = f"{AUTOPILOT_DIR_NAME}/events.jsonl"
CRON_FILE = f"{AUTOPILOT_DIR_NAME}/cron.yaml"
# TB-321: structured config (axis 1) — the canonical TOML file the
# opt-in branch in `Config.load()` prefers when present. Existing
# installs without this file keep the env-only resolution path
# bit-for-bit unchanged. The convention is `[core.*]` for non-
# component tunables and `[components.<name>]` for component-owned
# knobs (goal.md L307-310). `ap2 init` writing this file on fresh
# projects is axis-(6); for now operators opt in by hand.
CONFIG_TOML_FILE = f"{AUTOPILOT_DIR_NAME}/config.toml"
PID_FILE = f"{AUTOPILOT_DIR_NAME}/daemon.pid"
PAUSE_FLAG = f"{AUTOPILOT_DIR_NAME}/paused"
CRON_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/cron_state.json"
MM_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/mm_state.json"
RETRY_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/retry_state.json"
AUTO_DIAGNOSE_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/auto_diagnose_state.json"
# TB-260: per-daemon-lifetime runtime-introspection facts (currently
# `env_file_mtime_at_start` for the `.cc-autopilot/env` stale-detection
# surface). Separate from `auto_diagnose_state.json` because that file
# is dedicated to watchdog-cooldown bookkeeping; this one captures
# "facts pinned at daemon start, valid until daemon stop" so the CLI's
# `cmd_status` (a separate process) can compare current env mtime to
# the daemon's start-mtime without going through the daemon's PID.
DAEMON_STATE_FILE = f"{AUTOPILOT_DIR_NAME}/daemon_state.json"
ENV_FILE = f"{AUTOPILOT_DIR_NAME}/env"

DEFAULT_TICK_INTERVAL_S = 30
# TB-122: Mattermost polling runs in its own loop (`_mm_loop`) at a faster
# tempo than the main tick. The handler is operator-facing — pause / add /
# delete commands shouldn't sit behind a 30s tick when the cheap part of
# the work is just an HTTP poll.
DEFAULT_MM_TICK_INTERVAL_S = 10
DEFAULT_EVENT_CONTEXT_SIZE = 50
DEFAULT_TASK_TIMEOUT_S = 3600  # 60 min per SDK query
# TB-278: bumped from 300s (5 min) to 1200s (20 min) — ideation / mattermost /
# cron agents under `xhigh` effort against a populated progress.md /
# operator_log.md / ideation_state.md routinely blew the old 5-min wall.
# This project's own `.cc-autopilot/env` overrides to 1800s; the bumped
# default just spares fresh projects from rediscovering the same ceiling.
DEFAULT_CONTROL_TIMEOUT_S = 1200  # 20 min for mattermost/cron agents
DEFAULT_MAX_RETRIES = 3
DEFAULT_VERIFY_TIMEOUT_S = 600  # 10 min for the project-wide verify gate
DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S = 10800  # 3h — TB-71 watchdog
DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S = 21600  # 6h — re-fire spam guard

# TB-282: proactive attention-raised detector knobs.
# `AP2_TASK_STUCK_THRESHOLD_S` defaults to 4h — long enough to skip a
# long-but-healthy task agent (TB-122/TB-255 pattern: real-world tasks
# at xhigh effort can sit 30-60 min inside `sdk.query` without being
# stuck), short enough that an actually-hung dispatch surfaces well
# before the next status-report cron tick. `AP2_ATTENTION_DEBOUNCE_S`
# defaults to 6h so a still-stuck task re-fires roughly once per
# operator workday rather than every tick. Both knobs are read fresh
# from `os.environ` at detection-time inside `ap2/attention.py`
# (`_task_stuck_threshold_s` / `_attention_debounce_s`) and listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so an operator tightening either
# floor takes effect on the next tick without a daemon restart — they
# tune detection sensitivity, not lifecycle.
DEFAULT_TASK_STUCK_THRESHOLD_S = 14400  # 4h
DEFAULT_ATTENTION_DEBOUNCE_S = 21600  # 6h
# TB-287: `task_frozen` attention detector recency window. A Frozen task
# whose most-recent `retry_exhausted` / `task_failed` event is within
# `AP2_TASK_FROZEN_RECENCY_S` (default 86400 / 24h) AND has no
# intervening operator-driven `task_unfrozen` / `task_deleted` event
# surfaces as an `attention_raised type=task_frozen` condition so the
# walk-away operator returning after a day sees an `ap2 unfreeze`
# nudge per fresh freeze instead of just a `3F` aggregate count tick.
# Read fresh from `os.environ` at detection-time inside
# `ap2/attention.py` (`_task_frozen_recency_s`) and listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so the operator tuning the recency
# floor takes effect on the next tick without a daemon restart —
# tunes detection sensitivity, not lifecycle.
DEFAULT_TASK_FROZEN_RECENCY_S = 86400  # 24h

# TB-290: `cost_cap_approach` attention detector percentage threshold.
# Pre-trip companion to the post-trip `auto_approve_paused` surface for
# the `window_token_cap_exceeded` reason (TB-224). The detector fires a
# `## Attention needed` bullet when the rolling 24h auto-approved
# token sum is >= `AP2_AUTO_APPROVE_COST_APPROACH_PCT` percent of
# `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` AND strictly below the cap, so
# the walk-away operator gets a budget-spending nudge hours before
# auto-approve halts and they must `ap2 ack auto_approve_window_resume`.
# Default 75 (%) — leaves enough headroom for the operator to react
# (raise the cap, pause via ack, or wait the 24h window out) before
# the trip fires. Read fresh from `os.environ` at detection-time
# inside `ap2/attention.py` (`_cost_approach_pct`) and listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so an operator tightening the
# threshold takes effect on the next tick without a daemon restart.
# The cap itself (`AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`) remains the
# operator opt-in — when the cap is unset / 0 the detector is a
# no-op (same operator-friendly default as the TB-224 trip surface).
DEFAULT_AUTO_APPROVE_COST_APPROACH_PCT = 75

# TB-297: opt-in immediate-Mattermost-push on `attention_raised` emission.
# Default OFF so the status-report cron stays the routine push
# surface for fresh projects (TB-282's `## Attention needed` section
# already carries the same conditions there). Operators flip
# `AP2_ATTENTION_IMMEDIATE_PUSH=1` once they've sampled their own
# detector cadence and confirmed it's low enough not to noise the
# channel — the post-trip `auto_approve_paused` and pre-trip
# `cost_cap_approach` conditions are explicitly time-sensitive
# (waiting for the next status-report cron tick defeats the
# "proactively surfaced" claim), but `task_stuck` / `task_frozen` /
# `validator_judge_noisy` cadence is project-dependent. Bool parse
# mirrors the sibling `AP2_IDEATION_HALT_DISABLED` style
# (`1` / `true` / `yes` / `on` truthy, case-insensitive; anything
# else false). Read fresh from `os.environ` at push-decision time
# inside `daemon._maybe_push_attention` and listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so an operator toggling the knob
# takes effect on the next tick without a daemon restart. Closes the
# TB-282 Out-of-scope axis the briefing's L119-122 named (the
# walk-away operator's time-to-glance for time-sensitive conditions).
DEFAULT_ATTENTION_IMMEDIATE_PUSH = False

# TB-278: max-turn caps promoted to named constants alongside the
# DEFAULT_*_TIMEOUT_S family above so every battle-tested default sits in
# one discoverable place. Defaults raised from the old inline literals
# (task 50, ideation 30) to values this project's `.cc-autopilot/env`
# already validated — TB-122 hit `error_max_turns` at 51 turns on a task,
# and a 2026-05-12 manual ideate hit 31 turns mid-goal-rewrite. Fresh
# projects start from those lessons rather than rediscovering the walls.
# DEFAULT_CONTROL_MAX_TURNS keeps its current value (15) — listed here for
# consistency so the env-template scaffold can document a single source
# of truth for every max-turn knob.
DEFAULT_TASK_MAX_TURNS = 500
DEFAULT_CONTROL_MAX_TURNS = 15
DEFAULT_IDEATION_MAX_TURNS = 100

# TB-284: model for `ap2/ideation_scrub.py`'s post-write filter that
# strips exhaustion-asserting sentences from `ideation_state.md` after
# each ideation cycle. Haiku-4.5 is the cost-target floor — sentence-
# level classification, not deep reasoning. Operator override:
# `AP2_IDEATION_SCRUB_MODEL`. Listed in
# `env_reload.HOT_RELOADABLE_KNOBS` so an operator swapping the scrub
# model takes effect on the next ideation tick without a daemon
# restart. The runtime reads `AP2_IDEATION_SCRUB_MODEL` fresh from
# `os.environ` inside `ideation_scrub._resolved_model()` at call-time
# (parallel to `AP2_AGENT_MODEL`'s wiring), so this default lives
# here for discoverability — `Config.load` does NOT stash it on the
# dataclass because the call-site read is the source of truth.
DEFAULT_IDEATION_SCRUB_MODEL = "claude-haiku-4-5-20251001"

# TB-358 (axis 5): per-agent-kind backend selection. Every agent kind
# defaults to the `claude` backend so an all-claude install behaves
# exactly as it did before the adapter layer existed (OAuth-only auth,
# `sdk.query` against the bundled Claude Code binary). An operator opts a
# kind into the `codex` backend via the `[agent_backends]` config table
# (`task = "codex"`) or the `AP2_AGENT_BACKEND_<KIND>` env override (with
# `<KIND>` upper-cased — for the `task` kind, set the `_TASK` suffix to
# `codex`). `Config.get_agent_backend(kind)`
# resolves the merged value; `ap2.adapters.select.select_adapter` maps it
# to a concrete adapter instance. The canonical kind list lives in
# `ap2.adapters.select.AGENT_KINDS` (the auth gate walks it); the default
# id is pinned here so config + resolver + auth gate share one source.
DEFAULT_AGENT_BACKEND = "claude"


@dataclass
class Config:
    """Resolved per-project configuration."""

    project_root: Path
    tasks_file: Path
    progress_file: Path
    tasks_dir: Path
    events_file: Path
    cron_file: Path
    pid_file: Path
    pause_flag: Path
    cron_state_file: Path
    mm_state_file: Path
    retry_state_file: Path
    auto_diagnose_state_file: Path
    # TB-260: stash for `env_file_mtime_at_start` (and any future
    # daemon-lifetime introspection facts) so the CLI's `cmd_status`
    # can compare the live env file mtime against the value captured
    # at daemon start without going through the daemon's PID.
    daemon_state_file: Path
    # TB-260: the `.cc-autopilot/env` source-of-truth path. Surfaced as
    # a Config attribute (not just the `ENV_FILE` module constant) so
    # both startup-capture (in `daemon._emit_daemon_start`) and the
    # cmd_status / status_report / diagnose stale-detection paths read
    # one canonical attribute — a refactor that moves the env file
    # ripples through the dataclass instead of every call site.
    env_file: Path
    next_task_id: int
    # TB-280: operator-facing project identity. Leads every status-
    # report Mattermost headline (`**[<project_name>] Autopilot Status
    # Report** — <now>`) so a multi-project operator monitoring 5+
    # daemons can identify a post's source project without alt-tabbing
    # to the repo. Default is `project_root.name`; override via
    # `AP2_PROJECT_NAME`. Surfaced on `Config` (not on a Routine-scoped
    # struct) so the same field is available to web home, `ap2 status`,
    # and any future push surface that wants to prefix the identity
    # uniformly.
    project_name: str
    tick_interval_s: int
    mm_tick_interval_s: int
    event_context_size: int
    task_timeout_s: int
    control_timeout_s: int
    max_retries: int
    verify_cmd: str
    verify_timeout_s: int
    auto_diagnose_idle_threshold_s: int
    auto_diagnose_cooldown_s: int
    # TB-321 (axis 1): `[components.<name>]` sub-tables from the
    # loaded `.cc-autopilot/config.toml`, stashed verbatim for axis-
    # (5) per-component read paths to consume at the
    # `cfg.components_config[<name>][<key>]` shape. Empty dict for
    # the env-path branch (today's default) so the field is always
    # safe to read — a component that hasn't been migrated to read
    # from here just sees `{}` and falls back to its env-resolution
    # path. Field order matters for the dataclass constructor — the
    # env-path branch passes positional args, so this MUST be a
    # default-bearing field at the end of the list.
    components_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    # TB-334 (axis 5 core cluster): `[core.<key>]` entries from
    # `.cc-autopilot/config.toml` that are NOT existing Config
    # dataclass fields (`tick_interval_s`, `task_timeout_s`, &c. flow
    # through their named attributes as before — see
    # `config_loader.from_toml`'s `[core.X]` overlay). The new
    # `core_config` dict is the snapshot the `Config.get_core_value`
    # helper consults for non-dataclass core knobs (`agent_model`,
    # `agent_effort`, `task_max_turns`, `control_max_turns`,
    # `verify_judge_max_turns`, etc.) — the agent-runtime tunables
    # whose pre-migration call sites read `os.environ.get("AP2_*",
    # ...)` directly. Empty dict for the env-path branch (today's
    # default) so the helper always finds a safe `{}` to fall back
    # through to its env / default precedence. Default-bearing field
    # at the end of the list for the same dataclass-constructor reason
    # `components_config` is.
    core_config: dict[str, Any] = field(default_factory=dict)
    # TB-358 (axis 5): the `[agent_backends]` config table from
    # `.cc-autopilot/config.toml` — a per-agent-kind backend map
    # (`{"task": "codex", "ideation": "claude", ...}`) stashed verbatim by
    # `config_loader.from_toml`. Empty dict for the env-path branch
    # (today's default) so `get_agent_backend` always finds a safe `{}` to
    # fall through to its env / `DEFAULT_AGENT_BACKEND` precedence. A kind
    # absent from the map (or absent table entirely) resolves to
    # `claude`. Default-bearing field at the end of the list for the same
    # dataclass-constructor reason `core_config` / `components_config` are.
    agent_backends_config: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, project_root: str | Path | None = None) -> "Config":
        """Resolve a `Config` for `project_root` (default: `os.getcwd()`).

        TB-321 (axis 1): if `.cc-autopilot/config.toml` exists, prefer
        it — delegate to `Config.from_toml` to get the TOML-merged
        result. Else fall back to today's env-only resolution path
        (`Config._load_env_path`). Existing installs without
        `config.toml` see zero behavior change; fresh installs that
        opt in get the structured-config layer transparently.
        """
        root = Path(project_root or os.getcwd()).resolve()
        toml_path = root / CONFIG_TOML_FILE
        if toml_path.exists():
            return cls.from_toml(toml_path)
        return cls._load_env_path(root)

    @classmethod
    def from_toml(cls, toml_path: str | Path) -> "Config":
        """Build a `Config` from `.cc-autopilot/config.toml` at `toml_path`.

        TB-321 (axis 1): thin classmethod shim that delegates to
        `ap2.config_loader.from_toml`. Implementation lives there so
        the parser, schema dataclass, validator, and constructor all
        sit in one module (the briefing's axis-1 deliverable). The
        method exists on `Config` so callers naming the class-level
        constructor (`Config.from_toml(path)`) resolve through the
        same surface as `Config.load()` — both return a
        shape-compatible `Config` instance.
        """
        # Lazy import keeps the config.py ↔ config_loader.py cycle
        # broken (config_loader.from_toml lazy-imports `Config`).
        from .config_loader import from_toml as _from_toml

        return _from_toml(Path(toml_path))

    @classmethod
    def _load_env_path(cls, project_root: str | Path | None = None) -> "Config":
        """The original env-only `Config.load()` body (pre-TB-321).

        Factored out so both `Config.load()` (when no config.toml
        exists) and `config_loader.from_toml` (which uses this as the
        baseline before overlaying TOML values) share one
        implementation. Behavior is bit-for-bit identical to the
        pre-TB-321 `Config.load` — same env reads, same defaults,
        same `load_project_env` + `note_initial_applied` startup
        side effects.
        """
        root = Path(project_root or os.getcwd()).resolve()
        applied = load_project_env(root)
        # TB-271: seed the env-reload tracker with the set of keys the
        # startup pass actually wrote into os.environ. The reload helper
        # uses this set to honor "shell export wins" on later ticks —
        # keys never file-sourced at startup keep shell-export precedence
        # even if the operator later adds them to the env file. Lazy
        # import to avoid the config↔env_reload module cycle (env_reload
        # imports Config for type signatures + defaults).
        from .env_reload import note_initial_applied
        note_initial_applied(root, applied)
        autopilot_section = _read_autopilot_section(root / "CLAUDE.md")

        tasks_file = _resolve(root, autopilot_section.get("task_list"), DEFAULT_TASKS_FILE)
        progress_file = _resolve(
            root, autopilot_section.get("progress_log"), DEFAULT_PROGRESS_FILE
        )
        tasks_dir = _resolve(root, autopilot_section.get("task_briefings"), DEFAULT_TASKS_DIR)

        return cls(
            project_root=root,
            tasks_file=tasks_file,
            progress_file=progress_file,
            tasks_dir=tasks_dir,
            events_file=root / EVENTS_FILE,
            cron_file=root / CRON_FILE,
            pid_file=root / PID_FILE,
            pause_flag=root / PAUSE_FLAG,
            cron_state_file=root / CRON_STATE_FILE,
            mm_state_file=root / MM_STATE_FILE,
            retry_state_file=root / RETRY_STATE_FILE,
            auto_diagnose_state_file=root / AUTO_DIAGNOSE_STATE_FILE,
            # TB-260: daemon-lifetime state stash (env_file_mtime_at_start).
            daemon_state_file=root / DAEMON_STATE_FILE,
            env_file=root / ENV_FILE,
            next_task_id=autopilot_section.get("next_task_id", 1),
            # TB-280: project identity for status-report headline. Env
            # override wins over the `project_root.name` default so a
            # daemon hosting the project under a generic-named root
            # (`/tmp/proj`, `/home/user/code/main`) can still post with
            # an operator-meaningful identifier. Whitespace-stripped so
            # an accidental `AP2_PROJECT_NAME=" foo"` doesn't render a
            # leading space in the bracketed headline.
            project_name=(
                os.environ.get("AP2_PROJECT_NAME", "").strip()
                or root.name
            ),
            tick_interval_s=int(os.environ.get("AP2_TICK_S", DEFAULT_TICK_INTERVAL_S)),
            mm_tick_interval_s=int(
                os.environ.get("AP2_MM_TICK_S", DEFAULT_MM_TICK_INTERVAL_S)
            ),
            event_context_size=int(
                os.environ.get("AP2_EVENT_CONTEXT", DEFAULT_EVENT_CONTEXT_SIZE)
            ),
            task_timeout_s=int(
                os.environ.get("AP2_TASK_TIMEOUT_S", DEFAULT_TASK_TIMEOUT_S)
            ),
            control_timeout_s=int(
                os.environ.get("AP2_CONTROL_TIMEOUT_S", DEFAULT_CONTROL_TIMEOUT_S)
            ),
            max_retries=int(os.environ.get("AP2_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            verify_cmd=os.environ.get("AP2_VERIFY_CMD", "").strip(),
            verify_timeout_s=int(
                os.environ.get("AP2_VERIFY_TIMEOUT_S", DEFAULT_VERIFY_TIMEOUT_S)
            ),
            auto_diagnose_idle_threshold_s=int(
                os.environ.get(
                    "AP2_AUTO_DIAGNOSE_IDLE_THRESHOLD_S",
                    DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S,
                )
            ),
            auto_diagnose_cooldown_s=int(
                os.environ.get(
                    "AP2_AUTO_DIAGNOSE_COOLDOWN_S",
                    DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S,
                )
            ),
        )

    def ensure_dirs(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)

    def get_component_value(
        self,
        component: str,
        key: str,
        *,
        default: Any = None,
    ) -> Any:
        """Resolved value for a per-component config key (TB-326 axis-5 pilot).

        Precedence (high → low) — call-time-evaluated, matching the
        ``apply_env_overrides`` precedence at load time but kept live
        so a mid-process env change (the hot-reload story that
        ``env_reload.HOT_RELOADABLE_KNOBS`` carries today, plus the
        ``monkeypatch.setenv(...); helper(cfg)`` test idiom that
        pre-migration helpers exercised by reading env every call)
        takes effect on the next read without an explicit cfg reload:

          1. Sectioned env (``AP2_COMPONENTS_<NAME>_<KEY>``) — the new
             canonical naming. Wins over the cfg snapshot so an
             operator who exports the sectioned env mid-process sees
             the next-tick propagation immediately (parallel to
             ``apply_env_overrides`` putting sectioned env at the head
             of the load-time precedence).
          2. Flat env (``AP2_<knob>``) via reverse-``FLAT_TO_SECTIONED``
             lookup — the back-compat path the shell-export operator
             who never migrated their ``.cc-autopilot/env`` carries.
             Same precedence ordering as ``_apply_flat_back_compat``'s
             load-time hits.
          3. ``self.components_config[component][key]`` — the cfg
             snapshot populated by ``config_loader.from_toml`` (TOML
             overlay) at load time. Values here are already type-
             coerced against the manifest schema. This is the layer
             the operator who opted into ``config.toml`` reads from
             once env-side overrides are unset.
          4. ``default`` — when none of the above carry a value.

        Why call-time env-first (not snapshot-only): the pre-migration
        ``os.environ.get("AP2_<flat>")`` helpers naturally hot-reloaded
        by reading env on every call. ``env_reload.maybe_reload_env``
        re-syncs cfg from the env file on mtime change, but tests +
        operators who monkeypatch / shell-export mid-process don't
        trip that mtime check. Reading env-first at call time
        preserves the pre-migration call-site contract bit-for-bit;
        the cfg snapshot is the lower-precedence default that fires
        only when no env override is live. The flat env lookup keeps
        the component body free of ``os.environ.get('AP2_<flat>')``
        calls (TB-326's grep-shape Verification bullet); this helper
        in ``config.py`` is the one centralized site that reads env.

        Why this shape (option 2 of the briefing's three candidates):
        a single ``get_component_value`` helper is the lightest-touch
        incremental pattern the remaining six component clusters
        (attention, focus_advance, auto_unfreeze, mattermost,
        validator_judge, janitor, core) can adopt verbatim. Option 1
        (raw ``cfg.components_config[component][key]`` access) loses
        the env-only-mode back-compat without a wrapper, and option 3
        (per-component dataclass synthesis) requires a code-gen pass
        on every ``Manifest.config_schema`` plus a constructor-time
        invocation hook — both deferred to a post-pilot follow-up.

        Pure / read-only — no caching, no side effects. The env lookup
        is bounded by ``FLAT_TO_SECTIONED``'s ~40 entries; an O(N)
        walk is faster than a reverse-map cache for the rare per-tick
        call rate of these knobs.
        """
        # 1) Sectioned env (highest precedence — same head-of-list
        #    position `_apply_sectioned_env_overrides` enforces at
        #    load time).
        sectioned_env_name = f"AP2_COMPONENTS_{component.upper()}_{key.upper()}"
        raw = os.environ.get(sectioned_env_name)
        if raw is not None:
            return raw
        # 2) Flat env via reverse-FLAT_TO_SECTIONED lookup. Late import
        #    keeps the config.py ↔ config_compat.py boundary tidy and
        #    avoids a startup-time import cycle (config_compat imports
        #    Config only behind TYPE_CHECKING).
        from .config_compat import FLAT_TO_SECTIONED
        sectioned_target = f"components.{component}.{key}"
        for flat, sectioned in FLAT_TO_SECTIONED.items():
            if sectioned != sectioned_target:
                continue
            raw = os.environ.get(flat)
            if raw is not None:
                return raw
            break
        # 3) cfg snapshot (TOML overlay / load-time env-coerced value).
        comp = (self.components_config or {}).get(component)
        if isinstance(comp, dict) and key in comp:
            return comp[key]
        # 4) Default.
        return default

    def get_core_value(
        self,
        key: str,
        *,
        default: Any = None,
    ) -> Any:
        """Resolved value for a non-component (core-cluster) config key.

        TB-334 axis-5 sibling to ``get_component_value`` for the
        ``[core.*]`` section the structured-config focus (goal.md L308)
        carves out for non-component tunables. Same precedence shape,
        same call-time-evaluated env-first contract:

          1. Sectioned env (``AP2_CORE_<KEY>``) — the canonical naming
             under the sectioned regime. Wins over every other layer so
             an operator exporting the sectioned env mid-process sees
             the next-tick propagation (parallel to
             ``_apply_sectioned_env_overrides``'s head-of-list position
             at load time).
          2. Flat env (``AP2_<knob>``) via reverse-``FLAT_TO_SECTIONED``
             lookup — catches the pre-migration flat names
             (``AP2_AGENT_MODEL`` → ``core.agent_model``,
             ``AP2_TASK_MAX_TURNS`` → ``core.task_max_turns``, &c.)
             without forcing the operator to rename their env file.
          3. ``self.core_config.get(key)`` — the cfg snapshot populated
             by ``config_loader.from_toml``'s ``[core.<key>]`` overlay.
             Values here are the TOML-parsed types (int / bool / str)
             ready for the caller to use directly.
          4. ``default`` — when none of the above carry a value.
             TB-337 extension: when the caller passes ``default=None``
             AND the key is declared in ``CORE_CONFIG_SCHEMA`` with a
             non-None default, the schema's default wins over ``None``.
             This makes the schema the single source of truth for
             default values — a future bump to a `DEFAULT_*` constant
             in `ap2/config.py` propagates through the schema to every
             call site that didn't supply its own explicit default.
             Callers that pass an explicit ``default=...`` still win
             for back-compat (the pre-TB-337 contract is preserved bit
             for bit for migrated readers).

        Why call-time env-first (matching ``get_component_value``): the
        pre-migration ``os.environ.get('AP2_AGENT_MODEL')`` /
        ``os.environ.get('AP2_TASK_MAX_TURNS')`` helpers naturally
        hot-reloaded by reading env on every call. Tests +
        operators who monkeypatch / shell-export mid-process don't
        trip ``env_reload.maybe_reload_env``'s mtime check. Reading
        env-first at call time preserves the pre-migration call-site
        contract bit-for-bit; the cfg snapshot is the lower-precedence
        default that fires only when no env override is live.

        Note that ``[core.<key>]`` entries that ALSO name an existing
        Config dataclass field (``tick_interval_s``, ``task_timeout_s``,
        &c.) are overlaid onto the dataclass attribute by
        ``config_loader.from_toml`` for pre-TB-334 readers that access
        ``cfg.tick_interval_s`` directly; the same value is stashed in
        ``core_config`` so this helper finds it too. Non-dataclass
        keys (``agent_model``, ``task_max_turns``, etc. — the
        agent-runtime knobs this TB migrates) flow solely through
        ``core_config``.

        Pure / read-only — no caching, no side effects. The env lookup
        is bounded by ``FLAT_TO_SECTIONED``'s ~40 entries; an O(N)
        walk is faster than a reverse-map cache for the rare per-tick
        call rate of these knobs.
        """
        # 1) Sectioned env (highest precedence).
        sectioned_env_name = f"AP2_CORE_{key.upper()}"
        raw = os.environ.get(sectioned_env_name)
        if raw is not None:
            return raw
        # 2) Flat env via reverse-FLAT_TO_SECTIONED lookup. Late import
        #    keeps the config.py ↔ config_compat.py boundary tidy.
        #    TB-345: a single sectioned target may now carry MULTIPLE
        #    flat aliases (a canonical name plus one or more deprecated
        #    back-compat aliases — e.g. `core.ideation_halt_disabled`
        #    is reachable via both `AP2_IDEATION_HALT_DISABLED` and a
        #    deprecated focus-era alias). Check every
        #    matching flat name (canonical first by dict order) and
        #    return the first one actually set in os.environ, so a
        #    stale operator env that still exports the deprecated name
        #    resolves at call-time. (Pre-TB-345 every core target had
        #    exactly one flat alias, so the loop never iterated past
        #    the first match.)
        from .config_compat import FLAT_TO_SECTIONED
        sectioned_target = f"core.{key}"
        for flat, sectioned in FLAT_TO_SECTIONED.items():
            if sectioned != sectioned_target:
                continue
            raw = os.environ.get(flat)
            if raw is not None:
                return raw
        # 3) cfg snapshot (TOML overlay).
        if isinstance(self.core_config, dict) and key in self.core_config:
            return self.core_config[key]
        # 4) Default. TB-337: when the caller didn't supply one, fall
        #    back to the schema's declared default if any. Lazy import
        #    to avoid a startup-time `config.py` ↔ `core_config_schema`
        #    cycle (the schema module imports DEFAULT_* constants from
        #    config.py for its `default=` values).
        if default is None:
            from .core_config_schema import CORE_CONFIG_SCHEMA
            spec = CORE_CONFIG_SCHEMA.get(key)
            if spec is not None:
                return spec.default
        return default

    def get_agent_backend(self, kind: str) -> str:
        """Resolved backend id (``"claude"`` / ``"codex"``) for an agent kind.

        TB-358 (axis 5): the per-agent-kind selection read path. Precedence
        (high → low), call-time-evaluated so a mid-process env change takes
        effect on the next read without an explicit cfg reload — the same
        env-first contract ``get_core_value`` / ``get_component_value``
        carry:

          1. ``AP2_AGENT_BACKEND_<KIND>`` env override (``kind`` upper-cased
             onto the suffix — the ``task`` / ``status_report`` kinds read
             the ``_TASK`` / ``_STATUS_REPORT`` suffixed names).
             Wins over every other layer so an operator exporting it
             mid-process sees the next-dispatch propagation. A blank value
             is treated as unset (falls through), mirroring the
             whitespace-strip the auth gate / project-name resolution apply.
          2. ``self.agent_backends_config[kind]`` — the ``[agent_backends]``
             snapshot populated by ``config_loader.from_toml`` at load time.
          3. ``DEFAULT_AGENT_BACKEND`` (``"claude"``) — when neither layer
             carries a value, so an unmapped kind (and an all-default
             install) resolves to the Claude backend exactly as before the
             adapter layer existed.

        Returns the raw backend id verbatim — normalization of an unknown id
        to a concrete adapter is ``select_adapter``'s job (it defaults an
        unrecognized id to the Claude adapter), keeping this resolver a pure
        config read.
        """
        env_name = f"AP2_AGENT_BACKEND_{kind.upper()}"
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip():
            return raw.strip()
        snap = (self.agent_backends_config or {}).get(kind)
        if isinstance(snap, str) and snap.strip():
            return snap.strip()
        return DEFAULT_AGENT_BACKEND


def load_project_env(project_root: Path) -> dict[str, str]:
    """Read `.cc-autopilot/env` (KEY=VALUE lines) and merge into `os.environ`.

    Existing env vars win — the file only fills in keys not already set, so a
    shell export still overrides the file (useful for one-off runs).
    Blank lines and `#`-comments are ignored. Values may be wrapped in single
    or double quotes. Returns the dict of keys that were actually applied.
    """
    env_file = project_root / ENV_FILE
    if not env_file.exists():
        return {}
    applied: dict[str, str] = {}
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = val
        applied[key] = val
    return applied


def _resolve(root: Path, configured: str | None, default: str) -> Path:
    p = Path(configured or default)
    return p if p.is_absolute() else root / p


def _read_autopilot_section(claude_md: Path) -> dict:
    """Parse the `## Autopilot` section of CLAUDE.md into a dict."""
    if not claude_md.exists():
        return {}
    text = claude_md.read_text()
    # `\b[^\n]*$` matches `## Autopilot` with or without trailing
    # disambiguators (e.g. `## Autopilot (per-project)`). Same brittleness
    # pattern as TB-91's verifier regex; eliminating proactively (TB-102).
    m = re.search(r"^##\s+Autopilot\b[^\n]*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return {}
    body = m.group(1)
    result: dict = {}
    for label, key in [
        ("Task list", "task_list"),
        ("Task briefings", "task_briefings"),
        ("Progress log", "progress_log"),
    ]:
        mm = re.search(rf"- {re.escape(label)}:\s*`?([^`\n]+?)`?\s*$", body, re.M)
        if mm:
            result[key] = mm.group(1).strip()
    mm = re.search(r"- Next task ID:\s*TB-(\d+)", body)
    if mm:
        result["next_task_id"] = int(mm.group(1))
    return result


def bump_next_task_id(claude_md: Path, new_next: int) -> None:
    """Update the `- Next task ID: TB-N` line in CLAUDE.md."""
    text = claude_md.read_text()
    new_text, n = re.subn(
        r"(- Next task ID:\s*TB-)(\d+)",
        lambda _: f"- Next task ID: TB-{new_next}",
        text,
    )
    if n:
        claude_md.write_text(new_text)
