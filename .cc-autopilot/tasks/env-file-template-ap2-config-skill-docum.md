# env-file template + ap2-config skill: document env as secrets + deployment-identity only (drop the flat AP2_ tunable examples)

Tags: #autopilot #config #docs #env #skills #scaffolding

## Goal

Align the operator-facing env surface with the config-model simplification
(config.toml = behavioral tunables; env = secrets + deployment identity). Update the
scaffolded `.cc-autopilot/env` template (`ENV_TEMPLATE` in `ap2/init.py`) and the
`ap2-config` operator skill so env documents ONLY the secrets + deployment-identity
allowlist, with the commented `AP2_*` behavioral-tunable examples removed and
operators pointed at `config.toml` + `ap2 config set` for tunables. Operator-filed
meta-infra surface cleanup; no goal.md focus anchor (filed with
`--skip-goal-alignment`).

Why now: once the flat `AP2_*` tunable override is gone (predecessor task), a
scaffolded env still littered with commented `AP2_*` tunable examples (`ap2/init.py`
`ENV_TEMPLATE` L323-340: `AP2_AGENT_MODEL`, `AP2_AGENT_BACKEND_<KIND>`, …) actively
misleads operators into setting knobs that no longer take effect. If we delete this
task, the env surface contradicts the new resolution and re-teaches the retired
pattern.

## Scope

- Edit `ENV_TEMPLATE` (`ap2/init.py`) so it carries only the allowlist: secret/creds
  placeholders + deployment-identity knobs (`AP2_MM_CHANNELS`, web host/port, sandbox
  user, project name, tick intervals), with a header line directing operators to
  `.cc-autopilot/config.toml` + `ap2 config set` for all behavioral tunables.
- Remove the commented behavioral-tunable `AP2_*` example lines (`AP2_AGENT_MODEL`,
  `AP2_AGENT_BACKEND_<KIND>`, and any others that now have a config.toml home) from
  the template.
- Keep the existing env-template coverage gate green: update the "knobs
  intentionally absent from `ENV_TEMPLATE`" exception list (the comment block + test
  that already live beside `ENV_TEMPLATE` in `ap2/init.py`) to match the trimmed
  template.
- Update the `ap2-config` operator skill (`skills/ap2-config/SKILL.md`) to describe
  the two-tier split (config.toml = tunables; env = secrets + deployment identity)
  and the removed flat-`AP2_*` override path.
- Update any `README.md` / `ap2/architecture.md` config-model prose still describing
  flat `AP2_*` as a live tunable-override mechanism.

## Design

- This is the operator-surface counterpart to the resolution change; it must name the
  SAME allowlist the predecessor defined, so it is sequenced after it (blocked on the
  core task).
- Edit scaffolding + docs only; do NOT change config-resolution code (owned by the
  predecessor task).

## Verification

- `! grep -nE "AP2_AGENT_MODEL|AP2_AGENT_BACKEND" ap2/init.py` — the scaffolded `ENV_TEMPLATE` no longer ships behavioral-tunable AP2_* examples.
- `grep -nqi "config.toml" ap2/init.py` — the env template directs operators to config.toml for tunables.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the suite (incl. the env-template coverage gate beside `ENV_TEMPLATE`) stays green.
- `skills/ap2-config/SKILL.md` Prose: the skill describes env as secrets + deployment-identity only and config.toml (+ `ap2 config set`) as the home for behavioral tunables, with no flat-`AP2_*` tunable override; judge confirms via Read.

## Out of scope

- The config-resolution code change (predecessor task owns it).
- Removing or renaming any allowlisted env knob.
- Migrating existing projects' `.cc-autopilot/env` files (operator hygiene per project; this task only changes what fresh `ap2 init` scaffolds).
- Editing `goal.md`.
