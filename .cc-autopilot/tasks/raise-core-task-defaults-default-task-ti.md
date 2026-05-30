# Raise core task defaults (DEFAULT_TASK_TIMEOUT_S 1200 to 3600, DEFAULT_TASK_MAX_TURNS 200 to 500)

Tags: #autopilot #config #core #defaults

## Goal

This project has run for its entire history with
`AP2_TASK_TIMEOUT_S=3600` and `AP2_TASK_MAX_TURNS=500` set in
`.cc-autopilot/env` — overriding the code defaults (1200s / 200
turns) — because the shipped defaults are too tight for the
heavy-refactor task agents ap2 dispatches. The env comments document
why: TB-122 hit `error_max_turns` at 51 turns against the old wall,
and the 600s-era verify/task timeouts repeatedly tripped on real
work. Those hard-won operational values should become the code
defaults so a fresh `ap2 init` project inherits them without manual
tuning, and so this project's env overrides become redundant rather
than load-bearing.

Raise the two core defaults to match the current ap2 operating
values:
- `DEFAULT_TASK_TIMEOUT_S`: 1200 → 3600 (`ap2/config.py:53`)
- `DEFAULT_TASK_MAX_TURNS`: 200 → 500 (`ap2/config.py:144`)

Why now: the values are validated by this project's long production
history (every task agent has run at 3600/500), so codifying them is
low-risk and removes a per-project tuning step. Operator-directed
2026-05-30; meta-infra config-default change, roadmap parked →
`--skip-goal-alignment`.

## Scope

- `ap2/config.py` — `DEFAULT_TASK_TIMEOUT_S = 1200` → `3600`;
  `DEFAULT_TASK_MAX_TURNS = 200` → `500`. Update the inline
  end-of-line comments accordingly (e.g. `# 20 min per SDK query` →
  `# 60 min per SDK query`).

- These constants are referenced (not re-literal'd) by
  `CORE_CONFIG_SCHEMA` (`default=DEFAULT_TASK_TIMEOUT_S` /
  `default=DEFAULT_TASK_MAX_TURNS`), the daemon task-dispatch call
  sites (`cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS)`,
  &c.), `env_reload.py`, and the `ap2/init.py` `ENV_TEMPLATE`
  f-string (`AP2_TASK_MAX_TURNS={DEFAULT_TASK_MAX_TURNS}`). All
  pick up the new value automatically — no edits needed there, but
  verify nothing re-hardcodes `200` / `1200`.

- `ap2/init.py` ENV_TEMPLATE comments — the prose around the task
  knobs is now stale (e.g. "Default raised from 50 → 200 in TB-278";
  "this project's own env bumps to 3600"). Update it to reflect the
  new defaults (500 turns / 3600s are now the shipped defaults;
  bump further only for unusually heavy projects). Keep it accurate,
  don't delete the TB-122 rationale.

- Tests pinning the OLD literal defaults — update to the new values:
  - `ap2/tests/test_tb210_env_knobs.py` (~L270-271): asserts
    `_eval_task_max_turns_via_helper(...) == 200` and
    `DEFAULT_TASK_MAX_TURNS == 200` → change to `500`.
  - `ap2/tests/test_tb334_core_cfg_reads.py` (~L305): the
    `("task_max_turns", 200)` default expectation → `500`.
  - Scan for any other test asserting `DEFAULT_TASK_TIMEOUT_S` /
    `DEFAULT_TASK_MAX_TURNS` (or the literals `1200`/`200` as the
    *default*) and update. Leave tests that merely pass `1200`/`200`
    as an explicit fixture INPUT (e.g. `task_timeout_s=1200` in a
    constructed Config) — those aren't default assertions.

## Design

- **Constants are the single source.** Because the schema, call
  sites, and ENV_TEMPLATE all reference the `DEFAULT_*` constants
  (not literals), bumping the two constants propagates everywhere in
  one edit — fresh `ap2 init` writes a template showing 3600/500,
  `CORE_CONFIG_SCHEMA` declares them, and `ap2 config get
  core.task_timeout_s` (on a project with no override) reports the
  new default.

- **Behavior-preserving for THIS project.** The daemon already runs
  at 3600/500 via the env override (env wins over default), so this
  project's runtime is unchanged. The change only affects (a) fresh
  projects and (b) the now-redundant status of this project's env
  override.

- **Operator-env cleanup is out of scope.** This project's
  `.cc-autopilot/env` still sets `AP2_TASK_TIMEOUT_S=3600` /
  `AP2_TASK_MAX_TURNS=500` — now equal to the defaults, so harmless.
  Removing them is an operator-local `.cc-autopilot/env` edit (the
  file is gitignored), not part of this code change.

## Verification

- `uv run --extra dev pytest -q ap2/tests/` — full suite passes with
  the updated default assertions.
- `grep -qE "^DEFAULT_TASK_TIMEOUT_S = 3600" ap2/config.py` — timeout
  default is 3600.
- `grep -qE "^DEFAULT_TASK_MAX_TURNS = 500" ap2/config.py` — max-turns
  default is 500.
- `! grep -nE "DEFAULT_TASK_MAX_TURNS == 200|\(.task_max_turns., 200\)" ap2/tests/` — no test still asserts 200 as the task_max_turns default.
- `uv run --extra dev python -c "from ap2.config import DEFAULT_TASK_TIMEOUT_S as t, DEFAULT_TASK_MAX_TURNS as m; assert (t, m) == (3600, 500), (t, m); print('ok', t, m)"` — the constants resolve to the new values.
- `uv run --extra dev python -c "from ap2.core_config_schema import CORE_CONFIG_SCHEMA as S; assert S['task_timeout_s'].default == 3600 and S['task_max_turns'].default == 500; print('schema ok')"` — the core schema defaults track the bumped constants.
- `ap2/init.py` Prose: the ENV_TEMPLATE task-knob comments reflect the new shipped defaults (500 turns / 3600s) rather than the stale "200"/"600s" prose, while preserving the TB-122 rationale. Judge confirms via Read.

## Out of scope

- Removing the redundant `AP2_TASK_TIMEOUT_S` / `AP2_TASK_MAX_TURNS`
  entries from this project's `.cc-autopilot/env` (operator-local,
  gitignored; harmless now that they equal the defaults).
- Changing any OTHER default (control timeout, verify timeout,
  ideation knobs, &c.) — only the two task-agent defaults.
- The structured-config introspection / focus_advance work
  (TB-345 / TB-346) — unrelated; this touches only the `config.py`
  constants + their test pins.
