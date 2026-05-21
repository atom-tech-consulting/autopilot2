# Raise daemon defaults to battle-tested values + scaffold a documented .cc-autopilot/env template on init

Tags: #autopilot #config #init #defaults #onboarding #regression-pin

## Goal

This repo's `.cc-autopilot/env` is a graveyard of hard-won tuning — every knob carries a comment explaining a failure that forced the bump (TB-122 hit `error_max_turns` at 51 turns → task max-turns; control timeout 300s "too tight" → 1800; ideation hit 31 turns → 100). The CODE defaults are too conservative, so every NEW ap2 project rediscovers the same walls before learning to override. Raise the defaults to values this project already validated, and ship a documented env template so fresh projects start from the lessons learned rather than from scratch.

Concrete default changes (operator-specified):
- `AP2_CONTROL_TIMEOUT_S` default: 300 → 1200 (ideation/MM/cron agents under `xhigh` effort routinely blew the 5-min default).
- task max-turns default: 50 → 200 (bigger refactors blow past 50; TB-122 hit 51).
- ideation max-turns default: 30 → 100 (a goal.md rewrite mid-cycle hit 31).
- `AP2_TASK_TIMEOUT_S` default is ALREADY 1200 (config.py:44) — leave it; but `ap2/prompts.py` still claims "default 1h", which is stale — correct that text to 1200s / 20 min.

Plus: `ap2 init` should scaffold a documented `.cc-autopilot/env` template (it currently writes none), so operators see the available knobs + their defaults instead of discovering them by reading source or hitting walls.

Goal anchor: serves `goal.md` `## Done when` bullet "an operator can point ap2 at a fresh project, paste a goal.md, and walk away for a week without intervention." Conservative defaults + no env template mean a fresh project hits turn/timeout walls and requires operator intervention to diagnose + tune — exactly the friction the walk-away promise rules out. Better defaults + a self-documenting template let a fresh project run well out of the box.

Why now: every env comment in this repo is a battle scar from a default that was too low. The defaults haven't been updated to match, so the next project repeats the discovery. Cheap to fix now (a few constants + a template) and it compounds on every future project.

## Scope

- `ap2/config.py` — change `DEFAULT_CONTROL_TIMEOUT_S` from 300 to 1200. Promote the currently-inline max-turns defaults to named constants alongside the existing `DEFAULT_*_TIMEOUT_S` (matches the established pattern + makes defaults discoverable in one place): add `DEFAULT_TASK_MAX_TURNS = 200` and `DEFAULT_IDEATION_MAX_TURNS = 100` (and optionally `DEFAULT_CONTROL_MAX_TURNS = 15`, unchanged value, for consistency).
- `ap2/daemon.py:217` — the task-agent dispatch reads `os.environ.get("AP2_TASK_MAX_TURNS", 50)`; change the fallback to the new `DEFAULT_TASK_MAX_TURNS` (200).
- `ap2/ideation.py` — `IDEATION_MAX_TURNS_DEFAULT` (currently 30, used at ideation.py:656) becomes 100, or re-point the call site at the new `DEFAULT_IDEATION_MAX_TURNS` constant.
- `ap2/prompts.py` — fix the stale "`AP2_TASK_TIMEOUT_S` (default 1h)" text; the actual default is 1200s (20 min). Correct any other prose that cites the old defaults.
- `ap2/init.py` — scaffold a documented env template. Add an `ENV_TEMPLATE` (matching the existing `BRIEFING_TEMPLATE` / `GOAL_TEMPLATE` constant pattern) that `ap2 init` writes to `.cc-autopilot/env` ONLY IF that file is absent (idempotent — NEVER clobber an operator's existing env). The template is commented, lists the common knobs (`AP2_VERIFY_CMD`, `AP2_VERIFY_TIMEOUT_S`, `AP2_TASK_TIMEOUT_S`, `AP2_TASK_MAX_TURNS`, `AP2_CONTROL_TIMEOUT_S`, `AP2_IDEATION_MAX_TURNS`, `AP2_IDEATION_TRIGGER_TASK_COUNT`, `AP2_AGENT_MODEL`, `AP2_AGENT_EFFORT`, `AP2_MM_CHANNELS`), shows each one's default, and has them commented-out (so the template documents without overriding — the code defaults apply unless the operator uncomments). NOTE: `.cc-autopilot/env` is gitignored, so the generated file is local; the TEMPLATE source (the `ENV_TEMPLATE` constant) is committed as code.
- Update tests that pin the old default values (search for the literals 300 / 50 / 30 in default-assertion contexts) and add init coverage for the env-template scaffolding (written when absent, NOT clobbered when present).

## Design

- The env template documents-by-default: every knob commented out with its default shown inline, so a fresh `.cc-autopilot/env` is self-explanatory without changing behavior (code defaults still apply). Operators uncomment + edit only what they want to override — mirrors how a good `.env.example` works, but written directly as the gitignored `env` since that file is per-project anyway.
- Idempotent + non-clobbering: init must not overwrite an existing `.cc-autopilot/env` (operators put secrets / channel IDs there). Write the template only when the file is absent — same idempotency contract as init's other scaffolding.
- Promoting max-turns to named constants is a small consistency win (timeouts are already `DEFAULT_*` constants; turns were inline literals) and makes the env template's documented defaults reference a single source of truth.
- This repo's own `.cc-autopilot/env` already overrides all four knobs (task 500 turns / 3600s, control 1800s, ideation 100), so these default changes don't alter THIS daemon's behavior — they only help fresh projects. Don't touch this repo's env.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes (default-pinning tests updated).
- `grep -qE "DEFAULT_CONTROL_TIMEOUT_S\s*=\s*1200" ap2/config.py` — control-timeout default raised to 1200.
- `grep -qE "DEFAULT_TASK_MAX_TURNS\s*=\s*200" ap2/config.py` — task max-turns default constant is 200.
- `grep -qE "DEFAULT_IDEATION_MAX_TURNS\s*=\s*100" ap2/config.py` — ideation max-turns default constant is 100.
- Prose: the call sites (`ap2/daemon.py` task dispatch, `ap2/ideation.py`) now read the new named-constant defaults rather than inline `50` / `30` literals. The judge confirms via Read.
- `! grep -qE "AP2_TASK_TIMEOUT_S.{0,20}default 1h|default 1h.{0,20}AP2_TASK_TIMEOUT_S" ap2/prompts.py` — the stale "default 1h" task-timeout claim is gone from prompts.py (`!` inverts so absence passes; the real default is 1200s).
- Prose: `ap2 init` scaffolds a documented `.cc-autopilot/env` template (an `ENV_TEMPLATE`-style constant) listing the common knobs with their defaults, commented-out so it documents without overriding. The judge confirms via Read of init.py.
- Prose: a regression test pins that `ap2 init` writes the env template when `.cc-autopilot/env` is ABSENT and does NOT clobber it when PRESENT. The judge confirms the test covers both cases.

## Out of scope

- This repo's own `.cc-autopilot/env` — already overrides all these knobs; leave it untouched (the default changes don't affect this daemon).
- Changing `AP2_VERIFY_TIMEOUT_S` default (600) or `AP2_CONTROL_MAX_TURNS` default (15) values — not in this request (though `DEFAULT_CONTROL_MAX_TURNS` may be promoted to a named constant for consistency without changing its value).
- A committed `.cc-autopilot/env.example` separate from the init-generated file — the operator-specified shape is init scaffolding the (gitignored) `env` directly from a committed template constant; don't add a parallel example file.
- Re-running `ap2 init` on existing projects — operators get the template on next init of a fresh project; existing envs are never clobbered.
- Tuning the model / effort defaults (`AP2_AGENT_MODEL`, `AP2_AGENT_EFFORT`) — document them in the template but don't change their code defaults in this task.
