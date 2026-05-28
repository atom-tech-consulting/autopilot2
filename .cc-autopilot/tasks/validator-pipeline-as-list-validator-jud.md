## Goal

Current focus: refactor features into opt-in components — restructure
the briefing-validator pipeline from an inline-call chain into a list
of `BriefingValidator` callables, and migrate the
SDK-call-bearing LLM dep-coherence check (today inline in
`ap2/validator_judge.py`) into a `validator_judge/` subpackage that
registers itself as a `briefing_validator` hook via the component
registry. Goal.md L218 explicitly conjoins axis 4 ("validator
pipeline as a list") and axis 5's `validator_judge/` migration
("(4) gates on (5)'s `validator_judge` migration") — they ship as
one task, matching the TB-312 axes-3+5 bundling model. After this
lands, the deterministic structural checks (sections, goal-anchor,
why-now, no-manual-bullets, no-fenced-paths-in-scope) stay in
core and always run; the LLM judge becomes opt-in via the
component's env flag. Operators who don't want the SDK call on every
queue-append can disable just that component without touching core;
default behavior is preserved.

Why now: 4 of 6 axes have shipped; axis 4 is the last unstarted
non-migration axis, and the `validator_judge/` migration is the
explicit gate per goal.md L218. Bundling them ships both halves of
the dependency in one commit and avoids a half-state where the
pipeline is a list but the judge is still hardcoded.

## Scope

- Add `BriefingValidator` typedef (e.g. `Callable[[BriefingContext], str | None]`)
  to `ap2/briefing_validators.py`; refactor `_validate_briefing_structure`
  into a thin orchestrator that walks a list of validator callables in
  canonical order. Each existing structural check (sections-present,
  goal-anchor, why-now, no-manual-bullets, no-fenced-paths-in-scope)
  becomes a top-level callable taking a shared `BriefingContext`
  dataclass / namedtuple carrying `text`, `goal_md_path`,
  `skip_goal_alignment`, `description`, `blocked_csv`, `events_file`,
  `dep_judge_fn`. Preserve the exact error-message strings and
  return-on-first-failure semantics so existing tests stay green.
- `git mv ap2/validator_judge.py ap2/components/validator_judge/__init__.py`
  and add `ap2/components/validator_judge/manifest.py` declaring the
  component's `env_flag` (suggest `AP2_VALIDATOR_JUDGE_DISABLED` —
  default-enabled, env_flag suppresses) and a `briefing_validator`
  hook entry in `hook_points` mapping to the dep-coherence check
  wrapper. Match the existing migration shapes from prior commits
  6b4fcea / 73f5a52.
- Extend the `Registry` API with `briefing_validators()` (sibling to
  the existing `tick_hooks(phase)` / `channel_adapters(cfg)` accessors)
  that walks enabled manifests' `hook_points["briefing_validator"]`
  and returns the callables in name-sorted order.
- Rewire the three flat-import callers of `validator_judge.py`
  (`ap2/tools.py`, `ap2/briefing_validators.py`, `ap2/doctor.py`) so
  they resolve `_check_dependency_coherence` via the registry's
  `briefing_validators()` (or via
  `default_registry().get("validator_judge").hook_points[...]` for
  doctor's direct probe) rather than `from .validator_judge import
  ...`. Core must not statically import from `ap2/components/` per
  the import-direction gate.
- Update `_validate_briefing_structure`'s orchestrator to fold the
  registry-walked validators into the canonical list (deterministic
  checks first; registry-walked checks last so the existing
  ordering stays intact).
- Add `ap2/tests/test_tb316_validator_pipeline.py` (regression pin,
  ~12 tests) covering: pipeline-as-list shape, BriefingContext
  payload, validator order stability, validator_judge subpackage
  structural shape, manifest hook_points registers
  briefing_validator, registry.briefing_validators() walk,
  env-knob preservation (`AP2_VALIDATOR_JUDGE_*` verbatim), the
  dep-coherence check still fires by default, disabling the
  component via env flag suppresses the check, three flat-import
  call sites rewired (doctor + tools + briefing_validators), full
  briefing-validator regression suite still green.

## Design

Two structurally-independent refactors land together because goal.md
L218 conjoins them:

1. **Pipeline-as-list (axis 4)**: introduce `BriefingContext`
   dataclass + `BriefingValidator = Callable[[BriefingContext], str | None]`.
   Refactor `_validate_briefing_structure` into:

       ctx = BriefingContext(text=..., goal_md_path=..., ...)
       for validator in _CORE_VALIDATORS + registry.briefing_validators():
           err = validator(ctx)
           if err: return err
       return None

   The five existing checks extract verbatim into top-level
   functions; their error messages stay byte-identical to keep the
   existing 100+ briefing-validator tests green.

2. **validator_judge/ subpackage (axis 5)**: `git mv` the flat
   module; rewrite the manifest with the TB-313/TB-314 shape; expose
   `_check_dependency_coherence` (and any other symbol the three
   flat-import callers reach for) in `hook_points`. The manifest's
   `env_flag` is `AP2_VALIDATOR_JUDGE_DISABLED` (suppress-style,
   default-enabled to preserve current behavior). Doctor's direct
   probe of `_check_dependency_coherence` resolves via
   `default_registry().get("validator_judge").hook_points[...]`.

The registry's new `briefing_validators()` accessor mirrors
`tick_hooks(phase)` / `channel_adapters(cfg)` exactly — walks
`manifest.hook_points.get("briefing_validator")` for every enabled
manifest, name-sorted, returns the callables. The dep-coherence
wrapper adapts `_check_dependency_coherence`'s call signature to the
`(ctx) -> str | None` shape.

## Verification

- `uv run pytest -q ap2/tests/test_tb316_validator_pipeline.py` — new regression-pin module passes
- `uv run pytest -q ap2/tests/test_briefing_validators.py` — existing briefing-validator suite still green (no error-message drift)
- `uv run pytest -q ap2/tests/test_core_import_direction.py` — import-direction gate still green
- `uv run pytest -q ap2/tests/test_tb269_validator_judge_timeout_calibration.py ap2/tests/test_tb270_validator_judge_payload_slice.py` — TB-269 / TB-270 pins still green after the subpackage move
- `uv run pytest -q ap2/tests/` — full suite passes
- `test -f ap2/components/validator_judge/__init__.py` — subpackage body present
- `test -f ap2/components/validator_judge/manifest.py` — manifest present
- `test ! -f ap2/validator_judge.py` — flat module removed
- `! grep -rqE 'from ap2 import validator_judge\b|from ap2\.validator_judge\b|import ap2\.validator_judge\b' ap2/tools.py ap2/briefing_validators.py ap2/doctor.py` — three flat-import callers rewired through registry
- `grep -q "briefing_validators" ap2/registry.py` — registry exposes briefing_validators() accessor
- `grep -q "briefing_validator" ap2/components/validator_judge/manifest.py` — manifest declares briefing_validator hook
- `ap2/briefing_validators.py` Prose: `_validate_briefing_structure` walks a list of `BriefingValidator` callables (the five core checks + the registry-walked validators) rather than calling each check inline; judge confirms via Read
- `ap2/components/validator_judge/manifest.py` Prose: declares `env_flag=AP2_VALIDATOR_JUDGE_DISABLED` (suppress-style; default-enabled preserves current behavior); judge confirms via Read

## Out of scope

- Changing any of the five deterministic structural checks' behavior
  or error messages (purely structural extract — operator-facing
  surface stays byte-identical).
- Adding new validator kinds (goal.md non-goal; whack-a-mole rejection
  pattern per operator_log 2026-05-05 + 2026-05-16).
- Tuning the SDK timeout / payload-slice behavior (separate concerns;
  the check moves verbatim).
- Renaming `AP2_VALIDATOR_JUDGE_*` env knobs (goal.md L64-67).
- `attention/` migration (separate axis-5 task this cycle).
- `auto_approve/` migration (goal.md L196-197 sequences LAST).