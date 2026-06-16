# Make config.toml the sole source for behavioral tunables; restrict env to a secrets + deployment-identity allowlist

Tags: #autopilot #config #simplification #env #toml #back-compat

## Goal

Simplify ap2's two-file config model so `.cc-autopilot/config.toml` is the SINGLE
source for behavioral tunables, and `.cc-autopilot/env` is reserved for secrets +
deployment-identity (12-factor) values only. Today config resolution lets a flat
`AP2_<KNOB>` env var override the TOML value via the reverse-`FLAT_TO_SECTIONED`
lookup (`ap2/config.py` `get_component_value` / core `load` + `ap2/config_compat.py`'s
flat→sectioned shim that emits one-shot `env_deprecated` events). Remove that flat
`AP2_*` tunable-override path, restricting env consultation to an explicit allowlist
of secret + deployment-identity keys. Operator-filed meta-infra simplification; no
goal.md focus anchor (filed with `--skip-goal-alignment`).

Why now: the structured-config focus (shipped 2026-05-29) deliberately kept the
flat `AP2_*` override as a transitional back-compat shim. The operator is retiring
it now — collapsing the config model to one discoverable, schema-validated place
(config.toml), removing the "existing env wins" shell-pin footgun that silently
pins a running daemon, and clearing the way for a clean config story in the public
distribution cut. If we delete this task, the two-file override ambiguity and its
footgun ship as-is.

## Scope

- Define an explicit, named env allowlist (e.g. `ENV_PERMITTED_KEYS`) = the keys
  that legitimately live in env: credential/secret names (`CLAUDE_CODE_OAUTH_TOKEN`,
  `OPENAI_API_KEY`, `CODEX_HOME`, `MATTERMOST_URL`, `MATTERMOST_TOKEN`),
  deployment-identity knobs (`AP2_MM_CHANNELS`, `AP2_WEB_HOST`, `AP2_WEB_PORT`,
  `AP2_WEB_DISABLED`, `AP2_SANDBOX_USER`, `AP2_PROJECT_NAME`), and the
  runtime-fixed knobs with no config.toml home (`AP2_TICK_S`, `AP2_MM_TICK_S`, the
  path knobs, `AP2_REAL_SDK`). Document it in the single 12-factor exception comment
  block.
- Remove the flat-`AP2_<knob>` override path (reverse-`FLAT_TO_SECTIONED`) from the
  config resolution for every NON-allowlisted (behavioral-tunable) knob, so the
  config.toml value → schema default are the only resolution sources for tunables.
- Retire (or pare to the allowlist) `ap2/config_compat.py`'s flat→sectioned shim and
  its `env_deprecated` emission — a flat tunable env name no longer overrides the
  TOML; it is ignored (a one-time `env_ignored` debug log is acceptable but optional,
  implementer's call).
- Migrate any remaining behavioral-tunable detection-time `os.environ.get("AP2_*")`
  reads (e.g. attention `immediate_push`, the auto_approve circuit-breaker knobs, the
  ideation-scrub model, the agent-backend map) to `cfg.get_component_value(...)` /
  `cfg.get_core_value(...)` config.toml-sourced reads.
- Update tests that pin flat-env-override precedence to the new "config.toml wins;
  flat tunable env ignored" contract.

## Design

- The allowlist is the structural inverse of "tunable": a key is env-permitted iff
  it is a secret or deployment-identity / runtime-fixed knob; any key with a
  component/core `config_schema` home is config.toml-only.
- Do NOT change how secret / deployment-identity values are read from env — only the
  behavioral-tunable override path is removed.
- New/updated tests MUST set and tear down env via `monkeypatch.setenv` /
  `monkeypatch.delenv` so a stray `AP2_*` cannot leak into other tests' resolution
  (the known env-knob verifier-leak failure mode).
- Keep scope to the flat `AP2_*` override path; do not expand into a config-loader
  rewrite.

## Verification

- `grep -nE "ENV_PERMITTED_KEYS|env_permitted|env allowlist" ap2/config.py` — the explicit env allowlist symbol exists.
- New test (no real SDK): with a behavioral-tunable flat env var SET (e.g. `AP2_ATTENTION_IMMEDIATE_PUSH=1`) and the opposing value in config.toml, the resolved `cfg` value equals the config.toml/schema value, NOT the env value — the flat tunable override is ignored.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green under the removed override path.
- `ap2/config.py` Prose: behavioral tunables resolve from config.toml → schema default only (no flat-`AP2_*` override), and env is consulted only for the documented secrets + deployment-identity allowlist; judge confirms via Read.

## Out of scope

- The env-file template / `ap2 init` scaffolding and the `ap2-config` skill wording (separate surface task, blocked on this one).
- How secret / deployment-identity values are read from env (unchanged).
- Removing or renaming any allowlisted env knob.
- Editing `goal.md` (the structured-config "one release cycle" Done-when bullet may later be tightened; this task does not touch goal.md).
