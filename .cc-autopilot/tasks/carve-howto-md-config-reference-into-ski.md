## Goal

This task advances goal.md's "Current focus: consolidate the operator manual into auto-triggered, cross-runtime skills" by carving howto's configuration reference into `skills/ap2-config/SKILL.md` and retargeting the config-coverage drift gates onto it. It directly delivers the structured-config Done-when criterion that "a docs-drift gate enforces that every config schema key is documented in the operator-facing reference (the config/knobs operator skill — formerly `ap2/howto.md`'s `## Configuration knobs` section)", and the related promise that operators "discover knobs by reading one file, not by grepping howto.md".

Why now: the config domain is the largest, most-gated slice of howto and the one goal.md's structured-config Done-when explicitly relocates into a skill — so once the TB-397 canary settles conventions, it is the highest-leverage carve and the most direct closure of a stated Done-when criterion.

## Scope

- Create `skills/ap2-config/SKILL.md` (frontmatter + progressive disclosure) following the TB-397 canary conventions.
- Move howto's `## Configuration knobs`, `## Config keys (TOML)` (including the per-`[core]` / `[components.*]` / `[agent_backends]` blocks), and the Codex backend-setup content into the skill.
- Retarget `ap2/tests/test_docs_drift.py`'s env-knob coverage gate (`test_every_env_knob_documented`) and the config-schema-key coverage gate from `HOWTO_PATH` to the new skill.
- Remove the moved sections from `ap2/howto.md`; fix any dangling cross-references.

## Design

- Mirror the canary's frontmatter + gate-retarget pattern; add a `CONFIG_SKILL` constant read by the two config gates.
- Keep the deprecated-`AP2_*`-alias documentation (per goal.md Done-when's back-compat shim criterion) inside the skill so the env-knob gate still finds every knob name.
- Does not touch `sync_assets` or delete `howto.md` — only this domain's sections and its two gates move.

## Verification

- `test -f skills/ap2-config/SKILL.md` — config skill exists.
- `grep -qE '^description:' skills/ap2-config/SKILL.md` — auto-trigger description present.
- `! grep -q '## Configuration knobs' ap2/howto.md` — the knobs section is retired from howto.
- `grep -q 'ap2-config' ap2/tests/test_docs_drift.py` — env-knob & config-key gates retargeted to the skill.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite green (env-knob + config-key coverage now enforced against the skill).

## Out of scope

- Carving any non-config howto domain (TB-397 / TB-399 / TB-400).
- Deleting `ap2/howto.md` or changing the deploy targets (later retirement + TB-401).
