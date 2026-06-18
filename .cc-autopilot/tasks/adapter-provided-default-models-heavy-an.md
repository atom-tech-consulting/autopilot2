# Adapter-provided default models: heavy and light tiers per backend; primary agents use heavy, scrub and validator judge use light

Tags: #autopilot #adapters #models #provider-aware #codex #refactor

## Goal

Give each agent backend adapter two declared default models — a HEAVY tier (for
primary agents) and a LIGHT tier (for cost-sensitive sub-calls) — and route the call
sites through the SELECTED adapter's tier instead of hard-coding provider-specific
model strings or relying on the backend's opaque native default. Concretely:
`ClaudeCodeAdapter` → heavy `claude-opus-4-8` / light `claude-sonnet-4-6`;
`CodexAdapter` → heavy `gpt-5.5` / light `gpt-5.4-mini`. Operator-directed
architecture improvement; no goal.md focus anchor (filed `--skip-goal-alignment`).
Builds on TB-418 (which set the ideation numeric defaults and a call-site scrub
resolver) and supersedes that call-site scrub logic with the adapter tier.

Why now: provider-model knowledge is currently scattered and provider-coupled — the
validator judge hard-codes `_VALIDATOR_JUDGE_MODEL = "claude-haiku-4-5"`
(`briefing_validators.py:1147`), which a Codex-routed `validator_judge` kind hands
to Codex verbatim → Codex rejects the unknown model → the judge fails (the live
`validator_judge_noisy` symptom on the codex-routed gpu-bidder project). Putting the
heavy/light defaults in the adapter (the provider-knowledge boundary) fixes that
class of leak for every cost-sensitive call site at once and makes the default tier
controllable rather than the backend's opaque native default.

## Scope

- Add two declarations to the `AgentAdapter` base (`ap2/adapters/base.py`) —
  `default_model_heavy` and `default_model_light` (properties or class attrs) — and
  implement them per concrete adapter:
  - `ClaudeCodeAdapter`: heavy `claude-opus-4-8`, light `claude-sonnet-4-6`.
  - `CodexAdapter`: heavy `gpt-5.5`, light `gpt-5.4-mini`.
- Route the call sites through `select_adapter(kind, cfg)`'s tier when the relevant
  model config is unset (explicit config override always wins):
  - **Primary-agent dispatch** (task / ideation / cron / status_report / mattermost):
    `model = explicit agent_model override or select_adapter(kind, cfg).default_model_heavy`
    (replaces the current `... or None` → backend-native-default).
  - **Validator judge** (`briefing_validators.py`): replace the hard-coded
    `_VALIDATOR_JUDGE_MODEL` with the selected adapter's `default_model_light`.
  - **Ideation scrub** (`ideation_scrub._resolved_model`): when unset, use the
    selected adapter's `default_model_light` — superseding TB-418's call-site
    backend-string-match with the adapter tier.
- Leave the **verifier judge** as-is (`verify.py:647` already uses
  `cfg.get_core_value("agent_model") or None`, so it follows the heavy/agent_model
  path) — it is the substantive diff-reading judge, kept on the heavy tier.
- Update tests that pinned the old hard-coded / backend-native-default behavior.

## Design

- The adapter is ap2's provider boundary; "what is the heavy/light model for this
  provider" is provider knowledge and belongs there. New backends declare their own
  tiers without touching any call site.
- This generalizes TB-396 (which delegates the full default to the backend via
  `None`): instead of the backend's opaque native default, ap2 now names both tiers
  explicitly and picks heavy-vs-light per call site.
- Precedence is unchanged everywhere: an explicit model value (config.toml
  `agent_model` / `ideation_scrub_model`, or an allowlisted env override) still wins
  over the adapter tier; the tier is only the fallback.
- **Execution discipline.** Run test / verification commands in the FOREGROUND and
  let them finish; do NOT `run_in_background` + poll the output file. Iterate against
  TARGETED test files, not the full `ap2/tests/` suite repeatedly; the daemon's
  verifier runs the full suite after you report. Keep total tool calls bounded.

## Verification

- `grep -qE "default_model_heavy" ap2/adapters/base.py` — the adapter interface declares the heavy tier (light declared alongside).
- `! grep -qE '_VALIDATOR_JUDGE_MODEL *= *"claude-haiku' ap2/briefing_validators.py` — the validator judge no longer hard-codes a Claude model.
- `uv run --extra dev pytest -q ap2/tests/test_adapter_default_models.py` — a new test asserts `ClaudeCodeAdapter` heavy=`claude-opus-4-8` / light=`claude-sonnet-4-6`, `CodexAdapter` heavy=`gpt-5.5` / light=`gpt-5.4-mini`; that unset `agent_model` resolves to the selected adapter's heavy; that the validator judge and ideation scrub resolve to the selected adapter's light; and that an explicit model override still wins.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `ap2/adapters/base.py` + `ap2/briefing_validators.py` Prose: `AgentAdapter` declares heavy/light tiers per backend (Claude opus-4-8 / sonnet-4-6, Codex gpt-5.5 / gpt-5.4-mini); the validator judge and ideation scrub use the selected adapter's light tier, primary-agent dispatch uses heavy when `agent_model` is unset, and explicit model config still overrides; judge confirms via Read.

## Out of scope

- The ideation numeric defaults (trigger / cooldown / max_turns) — landed in TB-418.
- The verifier judge's model (stays on the `agent_model`/heavy path).
- Per-project config knobs for the tiers — the adapter declares them; per-call model
  config (`agent_model` / `ideation_scrub_model`) remains the override surface.
- Changing any project's existing `config.toml` model overrides.
