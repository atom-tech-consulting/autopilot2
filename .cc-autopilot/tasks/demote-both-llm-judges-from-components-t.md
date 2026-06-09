# TB-386: Demote both LLM judges from components to select_adapter layers

## Goal

Advance the focus "get the component boundary right — loop-level participants
only" by satisfying goal.md's Done-when bullet: "A CI gate fails the build if any
core module directly imports from ap2/components/<name>/. All cross-references flow
through the registry's single generic accessor — no per-kind registration
methods, and no core to component hook_points symbol lookups." A component is a
loop-level participant; an LLM judge invoked only as an internal sub-step of a
core runner is NOT a component. Today both judges are mis-modeled as components:
`ap2/components/verifier_judge/` and `ap2/components/validator_judge/`. This task
demotes both back to select_adapter layers so neither LLM judge is a component and
`registry.briefing_validators()` is gone.

Why now: verifier-judge-as-a-component shipped hours before the operator reframed
the boundary to exclude sub-step leaves; left in place it is a live wrong-direction
precedent that later axis-5 work would build on, and it keeps two dead per-kind
registry accessors (briefing_validators, verifier_judge) blocking the
single-generic-accessor signal.

## Scope

- Dissolve `ap2/components/verifier_judge/`: move the prose-bullet judge callable
  back into the core verify runner (`ap2/verify.py`) so `verify_task` calls it
  directly, still resolving the backend via `select_adapter("verifier_judge", cfg)`;
  delete the `registry.verifier_judge()` accessor.
- Dissolve `ap2/components/validator_judge/`: move the dep-coherence judge into the
  core briefing-validation runner (`ap2/briefing_validators.py`) so
  `_validate_briefing_structure` calls it directly via
  `select_adapter("validator_judge", cfg)`; delete `registry.briefing_validators()`
  and the validator_judge `hook_points` symbol-exposure block; rewire
  `ap2/tools.py` + `ap2/doctor.py` to plain imports.
- Preserve both off-switches as plain config knobs (NOT component env_flags):
  `AP2_VERIFY_JUDGE_DISABLED` keeps shell-only verification working;
  `AP2_VALIDATOR_JUDGE_DISABLED` keeps structural-only briefing validation working.
- Update the import-direction CI gate, the every-component-disabled test config,
  and docs (howto / architecture) to drop both component names.

## Design

- `select_adapter(kind, cfg)` already exists (`ap2/adapters/select.py:88`) and
  BOTH judges already use it for backend resolution: `ap2/verify.py` resolves
  `select_adapter("verifier_judge", cfg)` and
  `ap2/components/validator_judge/impl.py:452` resolves
  `select_adapter("validator_judge", cfg)`. The adapter seam is already the
  backend layer — this task removes the redundant *component* wrapper, not the
  adapter.
- verifier_judge component today: `ap2/components/verifier_judge/{manifest,impl}.py`
  + `registry.verifier_judge()` (`ap2/registry.py:453`), reached by
  `ap2/verify.py:932` via `default_registry().get("verifier_judge").hook_points["prose_judge"]`.
  The prose-bullet judge moves back so `verify.py` owns the call again.
- validator_judge component today: `ap2/components/validator_judge/{manifest,impl}.py`
  + `registry.briefing_validators()` (`ap2/registry.py:413`), appended by
  `ap2/briefing_validators.py:1305` (`pipeline.extend(default_registry().briefing_validators())`);
  plus `ap2/tools.py:178` and `ap2/doctor.py:779` resolve validator-judge symbols
  via `...get("validator_judge").hook_points[...]`. After demotion the core
  briefing-validation runner calls the judge directly behind the
  `AP2_VALIDATOR_JUDGE_DISABLED` config gate.

## Verification

- `! test -d ap2/components/verifier_judge` — the verifier_judge component subpackage is gone.
- `! test -d ap2/components/validator_judge` — the validator_judge component subpackage is gone.
- `! grep -rn 'def verifier_judge' ap2/registry.py` — the bespoke registry.verifier_judge() accessor is removed.
- `! grep -rn 'def briefing_validators' ap2/registry.py` — the bespoke registry.briefing_validators() accessor is removed.
- `grep -rn 'select_adapter("verifier_judge"' ap2/verify.py` — the core verify runner resolves the prose judge via select_adapter directly.
- `grep -rn 'AP2_VALIDATOR_JUDGE_DISABLED' ap2/` — the validator-judge off-switch survives as a plain knob.
- `grep -rn 'AP2_VERIFY_JUDGE_DISABLED' ap2/` — the verifier-judge off-switch survives as a plain knob.
- `uv run pytest -q ap2/tests/` — the full suite passes.
- `ap2/verify.py` Prose: the prose-bullet judge is invoked directly by the core verify runner with no `default_registry().get("verifier_judge")` lookup remaining; judge confirms via Grep/Read.
- `ap2/briefing_validators.py` Prose: the dep-coherence judge is called by the core briefing-validation runner via select_adapter, with no `registry.briefing_validators()` walk remaining; judge confirms via Grep/Read.

## Out of scope

- The generic `contributions(point)` accessor and collapsing the remaining
  channel_adapters / cron_job_handlers methods (TB-387).
- Removing the auto_approve / attention hook_points symbol-pull blocks and the
  dead POST_DISPATCH phase (TB-388).
- Extracting the communication component (TB-389) and the ideation component.