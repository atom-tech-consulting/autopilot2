## Goal

Axis (3) of the **Current focus: structured config (env → TOML)**
focus (goal.md L331-340): walk the six existing component manifests
(mattermost, attention, focus_advance, auto_unfreeze, auto_approve,
validator_judge) and fill in their `config_schema` declarations
covering every tunable knob each component currently consumes via
`os.environ.get("AP2_*")`. The registry's startup-validator (landed
by TB-321) consumes the union of these schemas; without per-
component declarations the validator surface is vacuous, the
axis-6 docs-drift gate has nothing to walk, and the axis-5
per-knob migration tasks have no schema to migrate against.

Why now: TB-321 lands the `Manifest.config_schema` field plus the
janitor canary but only the canary fills it in. Without the six
follow-up declarations the validator surface is vacuous and the
axis-6 docs-drift gate has nothing to assert against. This is the
meat of axis (3) per goal.md L335 — "Each existing component gains
a config_schema field on its Manifest" — and unblocks the axis-5
per-cluster migration tasks that need schema defaults to migrate
their reads against.

## Scope

For each of the six components, edit
`ap2/components/<name>/manifest.py` to add a `config_schema={...}`
entry declaring every knob the component currently reads from
`os.environ.get`. The authoritative knob list per component is the
`os.environ.get("AP2_*")` call sites inside each subpackage — do
not invent new knobs, do not drop existing ones (preserve every
knob name verbatim per goal.md L444-447's non-removal-during-
extraction constraint).

(1) `mattermost/` — declare entries for every `AP2_MM_*` knob the
    subpackage reads (channels / team / bot user / host / poll
    cadence). The Grep audit `os\.environ\.get\("AP2_` over
    `ap2/components/mattermost/` is the source of truth.

(2) `attention/` — `AP2_TASK_STUCK_THRESHOLD_S`,
    `AP2_ATTENTION_DEBOUNCE_S`, `AP2_TASK_FROZEN_RECENCY_S`,
    `AP2_AUTO_APPROVE_COST_APPROACH_PCT`,
    `AP2_ATTENTION_IMMEDIATE_PUSH` (defaults in
    `ap2/config.py` L69-123).

(3) `focus_advance/` — `AP2_FOCUS_AUTO_ADVANCE_DISABLED` plus any
    other knobs the Grep audit reveals (e.g. empty-cycles
    threshold per TB-292).

(4) `auto_unfreeze/` — `AP2_AUTO_UNFREEZE_DISABLED` (TB-320),
    `AP2_AUTO_UNFREEZE_FIX_SHAPES`, plus its sibling tunables.

(5) `auto_approve/` — `AP2_AUTO_APPROVE`, `AP2_AUTO_APPROVE_DRY_RUN`,
    `AP2_AUTO_APPROVE_WINDOW_S`, `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`,
    `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`, and any other
    `AP2_AUTO_APPROVE_*` knobs the Grep audit reveals.

(6) `validator_judge/` — `AP2_VALIDATOR_JUDGE_DISABLED`,
    `AP2_VALIDATOR_JUDGE_TIMEOUT_S`,
    `AP2_VALIDATOR_JUDGE_MODEL`.

Each declaration uses the `ConfigKey(name, type, default,
description, hot_reloadable)` shape from TB-321's
`ap2/config_loader.py`. Defaults must match the in-source default
the component reads today; descriptions are short (1 line each).
`hot_reloadable=True` for any knob already listed in
`env_reload.HOT_RELOADABLE_KNOBS`, False otherwise.

New regression-pin module
`ap2/tests/test_tb322_component_schemas.py`:
- For each of the 7 manifests (6 + janitor canary from TB-321),
  assert `config_schema` is non-empty.
- Parity test
  (`test_every_env_get_has_matching_schema_key`): walk each
  component subpackage's `os.environ.get("AP2_*")` calls, assert
  every unique key has a matching `config_schema` entry on the
  same component's manifest. Catches future knob additions that
  forget the schema declaration.
- Hot-reloadable parity test: assert each `ConfigKey`'s
  `hot_reloadable` flag matches membership in
  `env_reload.HOT_RELOADABLE_KNOBS`.
- Aggregation test: `aggregate_schemas()` (landed by TB-321)
  returns the union of all 7 component schemas with no name
  collisions across components.

## Design

The schema declaration is purely informational at this layer: the
component code keeps calling `os.environ.get(...)` for now (axis-5
migrates those reads to `cfg.<path>.<key>` per cluster). The value
this task delivers is the declarative surface — the registry has a
walkable schema, the docs-drift gate (axis 6) can assert each key
is documented, and the validator (TB-321) catches typos in the
TOML config at daemon-start.

The audit for each component is mechanical: `grep -rE
'os\.environ\.get."AP2_' ap2/components/<name>/` gives the call
sites; each unique `AP2_*` constant maps 1:1 to a `ConfigKey`
entry. The parity test in `test_tb322_component_schemas.py`
encodes this audit as code so future knob additions either
declare the schema entry or break CI.

`hot_reloadable` flag source of truth: `env_reload.HOT_RELOADABLE_KNOBS`
already enumerates which knobs the operator can edit between ticks
without restarting the daemon. The schema declarations mirror that
set — the test enforces parity so the two surfaces can't drift.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the six
  manifest edits + new regression pin land.
- `uv run pytest -q ap2/tests/test_tb322_component_schemas.py` —
  new regression-pin module passes.
- `uv run pytest -q ap2/tests/test_tb322_component_schemas.py::test_every_env_get_has_matching_schema_key`
  — parity test passes (every `AP2_*` env read in each component
  subpackage has a matching `config_schema` entry).
- `grep -q "config_schema" ap2/components/mattermost/manifest.py`
  — mattermost manifest declares its schema.
- `grep -q "config_schema" ap2/components/attention/manifest.py`
  — attention manifest declares its schema.
- `grep -q "config_schema" ap2/components/focus_advance/manifest.py`
  — focus_advance manifest declares its schema.
- `grep -q "config_schema" ap2/components/auto_unfreeze/manifest.py`
  — auto_unfreeze manifest declares its schema.
- `grep -q "config_schema" ap2/components/auto_approve/manifest.py`
  — auto_approve manifest declares its schema.
- `grep -q "config_schema" ap2/components/validator_judge/manifest.py`
  — validator_judge manifest declares its schema.
- `ap2/tests/test_tb322_component_schemas.py` Prose: the parity
  test `test_every_env_get_has_matching_schema_key` walks every
  component subpackage's `os.environ.get("AP2_*")` calls via
  Grep and asserts every unique key maps to a declared
  `config_schema` entry on that component's manifest; judge
  confirms via Read of the test body.

## Out of scope

- Migrating the `os.environ.get` reads themselves to
  `cfg.<path>.<key>` shape — axis 5 per-cluster long-tail tasks.
- Adding any new tunable knobs the components don't already
  consume.
- Removing or renaming any existing `AP2_*` env-knob names
  (goal.md L444-447's bit-identical-behavior constraint).
- Env-var override layer / `ap2/config_compat.py` back-compat
  shim (TB-323, axis 2).
- TB-305-sibling docs-drift gate for config-key documentation
  (axis 6, follow-up once schemas exist).
- `ap2 config list/get/set/validate` CLI verbs (axis 4,
  follow-up).
