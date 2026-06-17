# Tune ideation defaults (trigger 10 / cooldown 3600 / max_turns 200) and make ideation_scrub_model provider-aware (claude-haiku for claude, gpt-5.4-mini for codex)

Tags: #autopilot #config #ideation #defaults #codex #provider-aware

## Goal

Update ap2's ideation schema defaults and make the ideation-scrub model
provider-aware. Three numeric defaults change in `ap2/core_config_schema.py`
(`ideation_trigger_task_count` 3→10, `ideation_cooldown_s` 7200→3600,
`ideation_max_turns` 100→200), and the `ideation_scrub_model` default stops being a
fixed Claude string: when no operator override is set, it resolves to the cheap
model for whichever backend the `ideation_scrub` kind routes to — `claude-haiku-4-5`
for the Claude backend, `gpt-5.4-mini` for the Codex backend. Operator-directed
config-default tuning; no goal.md focus anchor (filed with `--skip-goal-alignment`).

Why now: autopilot2 runs the stale ideation defaults (3 / 7200 / 100), and the
scrub model defaults to a Claude string regardless of backend — the exact pain hit
on a Codex-routed project, where the scrub default (`claude-haiku-4-5`) would be
handed to Codex and had to be overridden by hand. A provider-aware default fixes
that for every Codex project out of the box (the scrub canary picks the cheap model
for its own provider), and the numeric bumps match the cadence/turn budget the
operator wants as the baseline. If we delete this, every new Codex project re-hits
the manual-scrub-model fix.

## Scope

- In `ap2/core_config_schema.py`, change the `ConfigKey` defaults:
  `ideation_trigger_task_count` 3 → 10, `ideation_cooldown_s` 7200 → 3600,
  `ideation_max_turns` 100 → 200.
- Make `ideation_scrub_model` provider-aware. Change its schema default to empty /
  unset, and in `ideation_scrub._resolved_model(cfg)`, when no explicit override is
  present, resolve the fallback by the `ideation_scrub` kind's backend
  (`cfg.get_agent_backend("ideation_scrub")`): Claude backend → the existing
  claude-haiku scrub default (`claude-haiku-4-5-20251001`); Codex backend →
  `gpt-5.4-mini` (verbatim lowercase — the canonical Codex model id). An explicit
  operator value (config.toml or an allowlisted env override) still wins.
- Update any tests that pin the old numeric defaults (3 / 7200 / 100) or the old
  fixed scrub default so the suite reflects the new contract.

## Design

- Mirror TB-396's per-backend-model approach, but with explicit cheap-per-provider
  fallbacks rather than `None`/self-default: the scrub is a cost-floor canary, so it
  wants the cheap model for its provider (haiku / `gpt-5.4-mini`), not the backend's
  full default (sonnet / `gpt-5.5`).
- Reuse the SAME `get_agent_backend("ideation_scrub")` the dispatcher uses, so the
  fallback automatically follows `[agent_backends]` without a second routing rule.
- Preserve the resolver's existing precedence (explicit override wins; empty falls
  back) — only the fallback VALUE becomes provider-aware.
- **Execution discipline.** Run test / verification commands in the FOREGROUND and
  let them finish; do NOT launch them with `run_in_background` and poll the output
  file. Iterate against TARGETED test files, not the full `ap2/tests/` suite
  repeatedly; the daemon's verifier runs the full suite after you report. Keep total
  tool calls bounded.

## Verification

- `ap2 config get core.ideation_trigger_task_count 2>&1 | grep -qx 10` — default trigger count is now 10.
- `ap2 config get core.ideation_cooldown_s 2>&1 | grep -qx 3600` — default cooldown is now 3600.
- `ap2 config get core.ideation_max_turns 2>&1 | grep -qx 200` — default max turns is now 200.
- `uv run --extra dev pytest -q ap2/tests/test_ideation_provider_defaults.py` — a new test asserts the three numeric defaults AND that the scrub model resolves to `claude-haiku-4-5-20251001` under a Claude-backed `ideation_scrub` kind and `gpt-5.4-mini` under a Codex-backed one when unset, with an explicit override still winning.
- `ap2/ideation_scrub.py` Prose: `_resolved_model` resolves the unset scrub model by the `ideation_scrub` kind's backend (claude → claude-haiku, codex → `gpt-5.4-mini`), and an explicit operator override still takes precedence; judge confirms via Read.

## Out of scope

- Per-backend defaults for the main `agent_model` (TB-396 already made it `None` /
  backend-self-default; this task only covers the scrub canary).
- The verifier/validator judge models (separate knobs; not requested).
- Changing any project's existing `config.toml` overrides (e.g. gpu-bidder's explicit
  scrub-model value stays as the operator set it).
