"""TB-337: typed `[core.*]` ConfigKey schema for the 21 non-component
tunables.

Axis (1) completion of the **structured config (env → TOML)** focus
(goal.md L266-403). TB-321 shipped the per-component schema slice
(`Manifest.config_schema` declared on every component manifest via
TB-322) and explicitly deferred a typed core schema to a later axis
— `howto.md` L2376-2379 ("schema deferred to a future axis; current
round-trip is shape-only") + `config_loader.validate_config`'s
docstring both flag the asymmetry. This module closes that gap.

The keys declared here are the non-component cluster — verifier,
ideation, agent runtime, control / mattermost timeouts, tick
intervals, web port, project identity. The cut-line mirrors
`config_compat.FLAT_TO_SECTIONED`'s `core.*` entries (config_compat.py
L88-115) minus the two detector-sensitivity knobs that are
intentionally out of scope per the briefing
(`auto_diagnose_cooldown_s`, `auto_diagnose_idle_threshold_s` —
operator-tunable but not currently part of the original "21 known
core keys" round-trip set). TB-339 drained the prior carve-out for
`verify_judge_effort` + `status_report_effort` — both are now
declared here so the corresponding read sites in `verify.py` and
`status_report.py` can route through `cfg.get_core_value(...)`
instead of carrying the last two direct `os.environ.get("AP2_*")`
reads outside the bootstrap path.

Why this lives in its own module (not folded into `ap2/config.py`):
`ConfigKey` is declared in `ap2/config_loader.py`. A schema declaration
inside `config.py` would force `config.py` to import `config_loader`,
which already lazy-imports `Config` from `config.py` — a textbook
cycle. Putting the schema in a sibling module keeps the import graph
acyclic and mirrors the per-component pattern
(`ap2/components/<name>/manifest.py` declares its own `config_schema`
via `from ap2.config_loader import ConfigKey`).

Why the schema names match the canonical TOML / Config dataclass field
names (`tick_interval_s`, `mm_tick_interval_s`, `event_context_size`)
and NOT the env-knob suffixes (`tick_s`, `mm_tick_s`, `event_context`):
the schema is the source of truth for the TOML key the operator
authors. `[core.tick_interval_s] = 60` is what `config_loader.from_toml`
overlays onto `cfg.tick_interval_s` by name (TB-321), and what
`config_compat.FLAT_TO_SECTIONED` maps `AP2_TICK_S` to. Naming the
schema by the env suffix would require either renaming the
FLAT_TO_SECTIONED targets (axis-2 back-compat regression) or carrying
two parallel name conventions. The env-knob form is documented
inline in each ConfigKey's description so an operator grepping for
`AP2_TICK_S` still finds the mapping.

Hot-reloadability mirrors `env_reload.HOT_RELOADABLE_KNOBS` /
`FIXED_KNOBS` membership: lifecycle knobs that wire a stateful
resource at daemon-start (`web_port` / `web_disabled`) carry
`hot_reloadable=False`; everything else is `True` because
`env_reload._refresh_tunable_config_fields` rewrites the matching
Config attribute on the next tick after an env / TOML mtime bump.
The flag is advisory metadata (surfaced by axis-4's
`ap2 config list --hot-reloadable`); the actual reload behavior
stays under `env_reload.HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` so a
parity drift here doesn't silently change the reload contract — a
regression-pin in `ap2/tests/test_tb337_core_schema.py` checks the
two surfaces agree.

Defaults are pulled from the in-source `DEFAULT_*` constants in
`ap2/config.py` (and a couple of module-local defaults in
`ap2/web.py` and `ap2/ideation.py`) so the schema never drifts from
the runtime fallback path — a future bump to any constant updates
the schema's default the next time this module is imported.
"""
from __future__ import annotations

from .config import (
    DEFAULT_CONTROL_MAX_TURNS,
    DEFAULT_CONTROL_TIMEOUT_S,
    DEFAULT_EVENT_CONTEXT_SIZE,
    DEFAULT_IDEATION_MAX_TURNS,
    DEFAULT_IDEATION_SCRUB_MODEL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MM_TICK_INTERVAL_S,
    DEFAULT_TASK_MAX_TURNS,
    DEFAULT_TASK_TIMEOUT_S,
    DEFAULT_TICK_INTERVAL_S,
    DEFAULT_VERIFY_TIMEOUT_S,
)
from .config_loader import ConfigKey


# Sibling defaults that don't live in `config.py` — pulled from the
# modules that own the read-site fallback so a future bump there
# propagates here automatically.
#
# `_DEFAULT_DAEMON_WEB_PORT` mirrors `ap2/web.py::DEFAULT_DAEMON_WEB_PORT`
# (8729) — the daemon-spawned web server's default port. Inlined as a
# constant (rather than imported from `ap2.web`) to keep this module
# free of a heavyweight transitive import chain through FastAPI /
# starlette / etc.; the value is pinned via a parity test in
# `test_tb337_core_schema.py`.
_DEFAULT_DAEMON_WEB_PORT = 8729
# `_DEFAULT_IDEATION_COOLDOWN_S` / `_DEFAULT_IDEATION_TRIGGER_TASK_COUNT`
# mirror the module-local defaults in `ap2/ideation.py`
# (`IDEATION_COOLDOWN_DEFAULT_S` / `IDEATION_TRIGGER_TASK_COUNT_DEFAULT`).
# Same parity-test pin keeps them aligned.
_DEFAULT_IDEATION_COOLDOWN_S = 7200
_DEFAULT_IDEATION_TRIGGER_TASK_COUNT = 3
# `_DEFAULT_VERIFY_JUDGE_MAX_TURNS` mirrors the inline default in
# `ap2/verify.py`'s `cfg.get_core_value("verify_judge_max_turns",
# default=20)` call. Verifier judge defaults to 20 max-turns — enough
# for the dep-coherence judge to fire two rounds (one Read + one
# verdict) without runaway.
_DEFAULT_VERIFY_JUDGE_MAX_TURNS = 20


# ---------------------------------------------------------------------
# CORE_CONFIG_SCHEMA — the 21 typed core-cluster knobs.
#
# Key names = the canonical TOML key under `[core.*]` (also the
# `Config` dataclass field name for the 11 dataclass-attribute knobs;
# the 10 non-dataclass knobs land in `cfg.core_config` via the TB-334
# helper path). Env-knob form is documented inline in each
# description so a `grep AP2_TICK_S` finds the schema row.
# ---------------------------------------------------------------------
CORE_CONFIG_SCHEMA: dict[str, ConfigKey] = {
    # --- Tick intervals / loop tempo --------------------------------------
    "tick_interval_s": ConfigKey(
        name="tick_interval_s",
        type=int,
        default=DEFAULT_TICK_INTERVAL_S,
        description=(
            "Main daemon tick interval in seconds. The `_main_tick_loop` "
            "fires roughly once per `tick_interval_s` to walk cron, "
            "pipeline sweep, task dispatch, ideation, and watchdog. "
            "Lower values shorten reaction time at the cost of more "
            "loop overhead. Mirrors the flat env `AP2_TICK_S` (alias "
            "`tick_s` in operator shorthand); hot-reloadable via the "
            "`env_reload._refresh_tunable_config_fields` rewrite path."
        ),
        hot_reloadable=True,
    ),
    "mm_tick_interval_s": ConfigKey(
        name="mm_tick_interval_s",
        type=int,
        default=DEFAULT_MM_TICK_INTERVAL_S,
        description=(
            "Mattermost polling tick interval in seconds. The `_mm_loop` "
            "runs in its own coroutine at a faster tempo than the main "
            "tick so operator pause / add / delete @bot mentions don't "
            "sit behind a 30s `tick_interval_s`. Mirrors the flat env "
            "`AP2_MM_TICK_S` (alias `mm_tick_s`); hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Per-call timeouts / retry budgets --------------------------------
    "task_timeout_s": ConfigKey(
        name="task_timeout_s",
        type=int,
        default=DEFAULT_TASK_TIMEOUT_S,
        description=(
            "Per-task SDK query timeout in seconds. The daemon hard-caps "
            "each `await sdk.query(...)` task-agent invocation at this "
            "bound; tasks exceeding it surface as `task_timeout` and "
            "route through the retry path. Bumped from 5min to 20min in "
            "TB-278 after xhigh-effort tasks routinely blew the wall. "
            "Mirrors the flat env `AP2_TASK_TIMEOUT_S`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "control_timeout_s": ConfigKey(
        name="control_timeout_s",
        type=int,
        default=DEFAULT_CONTROL_TIMEOUT_S,
        description=(
            "Per-control-agent (mattermost / cron / ideation) SDK query "
            "timeout in seconds. Same hard-cap semantics as "
            "`task_timeout_s` but applied to the non-task agent family. "
            "Default raised to 20min in TB-278 after xhigh-effort "
            "ideation cycles routinely blew the old 5-min wall against "
            "populated progress.md / operator_log.md. Mirrors the flat "
            "env `AP2_CONTROL_TIMEOUT_S`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "max_retries": ConfigKey(
        name="max_retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        description=(
            "Number of retry attempts per task before it lands Frozen "
            "and routes through the auto-unfreeze / operator-ack path. "
            "Failing the Nth retry emits `retry_exhausted` and stops "
            "the retry chain. Mirrors the flat env `AP2_MAX_RETRIES`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Verifier ---------------------------------------------------------
    "verify_cmd": ConfigKey(
        name="verify_cmd",
        type=str,
        default="",
        description=(
            "Project-wide regression gate shell command. Runs after every "
            "successful task-agent commit; failure routes the task "
            "through retry like any other crash. Empty (default) = no "
            "project-wide gate. Mirrors the flat env `AP2_VERIFY_CMD`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "verify_timeout_s": ConfigKey(
        name="verify_timeout_s",
        type=int,
        default=DEFAULT_VERIFY_TIMEOUT_S,
        description=(
            "Timeout in seconds for the `verify_cmd` regression gate. "
            "`ap2 doctor` warns when this is set below observed-typical "
            "successful verify duration. Mirrors the flat env "
            "`AP2_VERIFY_TIMEOUT_S`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Event context surface -------------------------------------------
    "event_context_size": ConfigKey(
        name="event_context_size",
        type=int,
        default=DEFAULT_EVENT_CONTEXT_SIZE,
        description=(
            "Number of most-recent events from `.cc-autopilot/events.jsonl` "
            "the daemon injects into each agent briefing as the "
            "`Recent events` context tail. Larger values give agents "
            "more historical context at the cost of prompt bytes. "
            "Mirrors the flat env `AP2_EVENT_CONTEXT` (alias "
            "`event_context`); hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Agent runtime (model + effort + max-turns) ----------------------
    "agent_model": ConfigKey(
        name="agent_model",
        type=str,
        default="",
        description=(
            "Model name passed to `ClaudeAgentOptions` for task / "
            "control / verifier / janitor agents. Empty default falls "
            "through to the SDK's own default. Project convention is "
            "`claude-opus-4-7` for heavy work; per-agent overrides "
            "(`AP2_STATUS_REPORT_EFFORT`, etc.) tune effort separately. "
            "Mirrors the flat env `AP2_AGENT_MODEL`; hot-reloadable "
            "(read fresh from os.environ at each SDK invocation)."
        ),
        hot_reloadable=True,
    ),
    "agent_effort": ConfigKey(
        name="agent_effort",
        type=str,
        default="",
        description=(
            "Global reasoning-effort label (low | medium | high | xhigh "
            "| max) passed as `extra_args={'effort': <value>}` to the "
            "SDK options. Per-job sub-knobs override for their "
            "respective agents. Empty default = no extra_args sent. "
            "Mirrors the flat env `AP2_AGENT_EFFORT`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "task_max_turns": ConfigKey(
        name="task_max_turns",
        type=int,
        default=DEFAULT_TASK_MAX_TURNS,
        description=(
            "Max turns per task-agent SDK query. Default raised from "
            "50 → 200 in TB-278 after TB-122 hit the old wall at 51 "
            "turns. Bump further (e.g. 500) for heavy-refactor "
            "projects. Mirrors the flat env `AP2_TASK_MAX_TURNS`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "control_max_turns": ConfigKey(
        name="control_max_turns",
        type=int,
        default=DEFAULT_CONTROL_MAX_TURNS,
        description=(
            "Max turns per control-agent (mattermost / cron) SDK query. "
            "Tighter than `task_max_turns` because control agents do "
            "small focused work (decide-then-route, not implement). "
            "Mirrors the flat env `AP2_CONTROL_MAX_TURNS`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "verify_judge_max_turns": ConfigKey(
        name="verify_judge_max_turns",
        type=int,
        default=_DEFAULT_VERIFY_JUDGE_MAX_TURNS,
        description=(
            "Max turns per verify-judge SDK query (the per-task "
            "verifier's optional LLM judge step). Default 20 — enough "
            "for a Read + verdict round-trip without runaway. Mirrors "
            "the flat env `AP2_VERIFY_JUDGE_MAX_TURNS`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "verify_judge_effort": ConfigKey(
        name="verify_judge_effort",
        type=str,
        default="",
        description=(
            "Per-site reasoning-effort label override for the verify "
            "judge SDK query (the per-task verifier's optional LLM "
            "judge step). Same value space as `agent_effort` (low | "
            "medium | high | xhigh | max). Empty default = fall "
            "through to `agent_effort` at the call site (the `or`-"
            "chain in `verify.py`); the per-site hardcoded fallback "
            "is `high`. Mirrors the flat env `AP2_VERIFY_JUDGE_EFFORT`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "status_report_effort": ConfigKey(
        name="status_report_effort",
        type=str,
        default="",
        description=(
            "Per-site reasoning-effort label override for the status-"
            "report cron's control-agent SDK query. Same value space "
            "as `agent_effort` (low | medium | high | xhigh | max). "
            "Empty default = fall through to `agent_effort` at the "
            "call site (the `or`-chain in `status_report.py`); the "
            "per-site hardcoded fallback is `medium`. Mirrors the "
            "flat env `AP2_STATUS_REPORT_EFFORT`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Ideation cluster ------------------------------------------------
    "ideation_disabled": ConfigKey(
        name="ideation_disabled",
        type=bool,
        default=False,
        description=(
            "Kill switch for the empty-board ideation cron. Truthy "
            "value opts the project out of automatic backlog refill "
            "(manual-only projects, tests, or temporarily quieting the "
            "loop). Mirrors the flat env `AP2_IDEATION_DISABLED`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "ideation_trigger_task_count": ConfigKey(
        name="ideation_trigger_task_count",
        type=int,
        default=_DEFAULT_IDEATION_TRIGGER_TASK_COUNT,
        description=(
            "Fire ideation when Ready+Backlog count is BELOW this "
            "threshold AND Active is empty. Doubles as the per-cycle "
            "proposal-slot budget. Set to 1 for the legacy 'fire only "
            "when working queue is fully empty' behavior. Mirrors the "
            "flat env `AP2_IDEATION_TRIGGER_TASK_COUNT`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "ideation_cooldown_s": ConfigKey(
        name="ideation_cooldown_s",
        type=int,
        default=_DEFAULT_IDEATION_COOLDOWN_S,
        description=(
            "Minimum seconds between ideation cron fires when the "
            "board stays empty. Throttles the cycle so the agent isn't "
            "hammered every tick on a quiet project. Default 7200 "
            "(2h). Mirrors the flat env `AP2_IDEATION_COOLDOWN_S`; "
            "hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "ideation_max_turns": ConfigKey(
        name="ideation_max_turns",
        type=int,
        default=DEFAULT_IDEATION_MAX_TURNS,
        description=(
            "Max turns per ideation-agent SDK query. Default raised "
            "30 → 100 in TB-278 after a goal.md rewrite mid-cycle hit "
            "`error_max_turns` at 31. `control_timeout_s` still bounds "
            "runaway wall-clock. Mirrors the flat env "
            "`AP2_IDEATION_MAX_TURNS`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    "ideation_scrub_model": ConfigKey(
        name="ideation_scrub_model",
        type=str,
        default=DEFAULT_IDEATION_SCRUB_MODEL,
        description=(
            "Model name for `ideation_scrub.py`'s post-write filter "
            "that strips exhaustion-asserting sentences from "
            "`ideation_state.md` after each ideation cycle. Haiku-4.5 "
            "is the cost-target floor (sentence-level classification, "
            "not deep reasoning). Mirrors the flat env "
            "`AP2_IDEATION_SCRUB_MODEL`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Project identity -------------------------------------------------
    "project_name": ConfigKey(
        name="project_name",
        type=str,
        default="",
        description=(
            "Operator-facing project name. Leads every status-report "
            "Mattermost headline (`**[<project_name>] Autopilot Status "
            "Report**`) so a multi-project operator can identify a "
            "post's source. Empty default falls back to "
            "`project_root.name`. Mirrors the flat env "
            "`AP2_PROJECT_NAME`; hot-reloadable."
        ),
        hot_reloadable=True,
    ),
    # --- Web server (lifecycle / FIXED_KNOBS — restart required) ----------
    "web_port": ConfigKey(
        name="web_port",
        type=int,
        default=_DEFAULT_DAEMON_WEB_PORT,
        description=(
            "TCP port the daemon-spawned web server binds to. Default "
            "8729 — stable across restarts so bookmarks survive. "
            "Lifecycle knob (FIXED_KNOBS): the web task is bound at "
            "daemon-start, so changes require `ap2 stop && ap2 start`. "
            "Mirrors the flat env `AP2_WEB_PORT`; NOT hot-reloadable."
        ),
        hot_reloadable=False,
    ),
    "web_disabled": ConfigKey(
        name="web_disabled",
        type=bool,
        default=False,
        description=(
            "Kill switch for the daemon-spawned web server. Truthy "
            "value skips the web task entirely (useful for headless / "
            "sandbox runs). Lifecycle knob (FIXED_KNOBS): consulted "
            "once at daemon-start, so changes require `ap2 stop && ap2 "
            "start`. Mirrors the flat env `AP2_WEB_DISABLED`; NOT "
            "hot-reloadable."
        ),
        hot_reloadable=False,
    ),
}
