## Goal

Advance `Current focus: extract the remaining core subsystems into components`
by extending the registry's tick-phase vocabulary and proving the new shape on
the most-isolated subsystem: extract the **pipeline** sweep into
`ap2/components/pipeline/` behind the registry. goal.md axis (1) names this the
prerequisite "Extended phase/hook vocabulary + canary (pipeline)": add to the
existing `Phase` enum the stages the remaining extractions need (pipeline-sweep,
cron-dispatch, ideation), and move `ap2/pipeline_sweep.py::_sweep_pipeline_pending`
+ the `pipeline_task_start` MCP tool + the Pipeline Pending board state + the
`pipeline_*` events into a component subpackage. Purely structural — observable
behavior and every `AP2_*` knob name preserved bit-for-bit (goal.md L110-111).

Why now: goal.md's axis-(1) delete-test says "if the new tick-stage shape isn't
pinned in one converted subsystem, every later extraction re-invents it" —
pipeline is the most isolated subsystem, so converting it first de-risks axes 2
and 4 before they touch higher-blast-radius code.

## Scope

- Add the new `Phase` enum members the remaining tick stages need (a
  pipeline-sweep phase, plus the cron-dispatch and ideation phases axes 2 and 4
  will consume) to `ap2/registry.py`, documented like the existing
  PRE_DISPATCH/POST_CRON members.
- Create `ap2/components/pipeline/` (`impl.py` holding the relocated
  `_sweep_pipeline_pending` body, `manifest.py` exposing a `MANIFEST` whose
  `tick_hooks` registers the sweep on the new pipeline phase, thin `__init__.py`
  re-export) following the `impl.py` / `manifest.py` / `__init__.py` subpackage
  shape the existing components under `ap2/components/` already use.
- Relocate the `pipeline_task_start` MCP tool registration and the `pipeline_*`
  event emissions with the subsystem; keep the Pipeline Pending board section
  semantics unchanged.
- Make `daemon._tick` dispatch the sweep by walking
  `default_registry().tick_hooks(<pipeline phase>)` instead of calling
  `_sweep_pipeline_pending` directly; core must not statically import
  `ap2/pipeline_sweep`.
- Preserve the component's env knobs and default behavior; add a `config_schema`
  mirroring the existing manifests if the subsystem owns tunables.

## Design

A component is a subpackage with `impl.py` (the relocated subsystem body), a
thin `__init__.py` re-export, and a `manifest.py` exposing a module-level
`MANIFEST = Manifest(...)`; `ap2/components/janitor/` is a concrete reference.
The registry discovers it filesystem-side via `pkgutil.iter_modules` (no
registry-side edit). Register the sweep on a new `Phase` member so
`daemon._tick` dispatches it via `registry.tick_hooks(<phase>)` exactly as it
already walks PRE_DISPATCH hooks (the `iscoroutine()`-aware dispatch loop). The
`pipeline_task_start` MCP tool moves with the body and is registered through the
existing tool-registration path; `pipeline_*` events keep their current
`type`/`summary` shape so the events allowlist + web rendering are untouched.
The cron-dispatch and ideation `Phase` members are declared now (documented,
unused) so axes 2 and 4 consume a ready vocabulary rather than re-extending the
enum. Keep the change behavior-neutral: the sweep's ordering relative to other
`_tick` stages is preserved by choosing the phase whose walk fires at the same
point the inline call fires today.

## Verification

- `uv run pytest -q` — full suite passes.
- `test -f ap2/components/pipeline/manifest.py` — the pipeline component
  subpackage exists with a registry manifest.
- `uv run pytest -q ap2/tests/e2e/test_pipeline_pending.py ap2/tests/e2e/test_pipeline.py` — Pipeline Pending dispatch behavior is preserved post-extraction.
- `! grep -nE '_sweep_pipeline_pending' ap2/daemon.py` — core no longer calls the
  sweep directly; it is reached via the registry walk.
- `ap2/registry.py` Prose: the `Phase` enum gains the tick stages the remaining
  extractions need — a pipeline-sweep phase plus cron-dispatch and ideation
  phases; judge confirms via Read.
- Prose: a regression test in `ap2/tests/test_tb310_tick_hook_protocol.py` (or a
  new sibling) pins that `default_registry().tick_hooks(<pipeline phase>)`
  returns the pipeline component's sweep hook; judge confirms the assertion
  exists via Read.

## Out of scope

- Extracting cron (axis 2), ideation (axis 4), auto-approve (axis 3), or the
  prose-judge (axis 5) — separate tasks.
- Any behavior change to the pipeline sweep, the `pipeline_task_start` tool
  contract, or the Pipeline Pending board semantics.