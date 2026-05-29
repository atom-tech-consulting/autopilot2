## Goal

Current focus: structured config (env → TOML). Close axis (1)'s
deferred `[core.*]` schema deliverable: goal.md L307-310 specifies
"The schema is sectioned: a `[core.*]` group for non-component
tunables (verifier, ideation, control-agent, cron) and
`[components.<name>]` sub-tables for each component-owned knob." TB-322
shipped the per-component half (`Manifest.config_schema` declared on
7 components; ~25 keys validated via the registry's
`aggregate_schemas` / `validate_config` walk). The core half was
deferred — howto.md L2376-2379 explicitly notes "schema deferred to a
future axis; current round-trip is shape-only" — and TB-334 (d4404ef)
added `Config.core_config: dict[str, Any]` + `get_core_value` helper
that stashes every `[core.<key>]` TOML entry verbatim without
type / default / description validation. The asymmetry shows up as: a
typo in `[core.web_port]` (e.g. `[core.web_prot]`) silently returns
the default; a non-numeric `tick_s = "thirty"` doesn't fail
daemon-start; the docs-drift gate has no signal on core key coverage.

Why now: TB-334 just made `core_config` consumable via a helper, so
more readers will migrate into it (TB-336 will add at least 5 more
core readers). Declaring the schema before the consumer count grows
avoids a retroactive validation rollout. The shape is well-pinned —
21 known core keys with sectioned homes already in
`FLAT_TO_SECTIONED` (config_compat.py L89-115): `tick_s`, `mm_tick_s`,
`task_timeout_s`, `control_timeout_s`, `max_retries`, `verify_cmd`,
`verify_timeout_s`, `event_context`, `agent_model`, `agent_effort`,
`task_max_turns`, `control_max_turns`, `verify_judge_max_turns`,
`ideation_disabled`, `ideation_trigger_task_count`,
`ideation_cooldown_s`, `ideation_max_turns`, `ideation_scrub_model`,
`project_name`, `web_port`, `web_disabled`. Same delete-test as TB-322:
without it, every typo in a core TOML key is silent.

## Scope

- New `ap2/core_config_schema.py` declaring a
  `CORE_CONFIG_SCHEMA: dict[str, ConfigKey]` mapping each of the 21
  keys above to a `ConfigKey(name, type, default, description,
  hot_reloadable)` entry. Mirror the per-component manifest shape
  from TB-322; types follow today's runtime cast (int for `tick_s` /
  `task_timeout_s` / `max_retries` / etc.; str for `verify_cmd` /
  `agent_model` / `agent_effort` / `ideation_scrub_model`; bool for
  `ideation_disabled` / `web_disabled`); defaults match the current
  in-source defaults (`DEFAULT_TICK_INTERVAL_S`,
  `DEFAULT_TASK_TIMEOUT_S`, etc.); `hot_reloadable` mirrors
  `env_reload.HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` membership
  (`AP2_WEB_PORT`, `AP2_WEB_DISABLED` ∈ FIXED_KNOBS →
  `hot_reloadable=False`; the others → True).
- Wire the schema into the registry's existing `aggregate_schemas` /
  `validate_config` walk: extend it so the validation pass also
  walks `CORE_CONFIG_SCHEMA` and rejects unknown `[core.*]` keys
  with a clear error ("unknown config key `core.web_prot` (did you
  mean `core.web_port`?)"). Validation fires at daemon-start in the
  same place TB-321 wired axis-3's validator.
- Extend `Config.get_core_value` to optionally validate against
  `CORE_CONFIG_SCHEMA` — when a key is read with `default=None` and
  the schema declares a default, use the schema's default (single
  source of truth). Existing call sites that pass an explicit
  `default=...` still win for back-compat.
- Add a `### [core]` subsection to howto.md `## Config keys (TOML)`
  (L2358) enumerating the 21 keys with their type, default, and
  description (one bullet per key, matching the existing
  per-component subsection shape at L2403+).
- Extend the `test_every_config_key_documented` gate
  (ap2/tests/test_docs_drift.py) to walk `CORE_CONFIG_SCHEMA` in
  addition to the per-component schemas.
- Extend `ap2.init.CONFIG_TEMPLATE` rendering so `ap2 init` writes the
  21 core keys as commented-out defaults in a `[core]` section header
  block at the top of the generated config.toml.
- New tests in `ap2/tests/test_tb337_core_schema.py` cover: (a) every
  expected core key is declared in `CORE_CONFIG_SCHEMA`; (b) unknown
  `[core.<typo>]` in a test config.toml raises the validator with a
  clear error naming the bad key path; (c) a deliberate non-numeric
  `[core.tick_s] = "thirty"` raises a type-validation error;
  (d) `get_core_value` without an explicit `default` falls back to
  the schema's declared default; (e) `ap2 init` writes a config.toml
  whose `[core]` block contains all 21 commented keys.

## Design

The schema declaration lives in its own module (`ap2/core_config_schema.py`) rather than `ap2/config.py` to avoid an import cycle with `ConfigKey` (declared in `ap2/config.py`). The registry's `aggregate_schemas` function gains a `core_schema: dict[str, ConfigKey]` parameter (defaulting to the new `CORE_CONFIG_SCHEMA` constant) that contributes a top-level `[core.*]` validation namespace alongside the existing `[components.<name>.*]` ones. Validation error messages name the full sectioned path (`core.<key>` not bare `<key>`) so operators get the right TOML edit target.

For hot-reloadability, the schema's `hot_reloadable` flag is advisory (reported by `ap2 config list --hot-reloadable`); the actual reload behavior stays under `env_reload.HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` for compatibility with existing tests. A docstring on `CORE_CONFIG_SCHEMA` documents the parity contract and links to `env_reload.py`.

The howto.md docs subsection (L2358+) is structured as one `### [core]` block listing the 21 keys in stable order, then the existing per-component blocks unchanged. The `test_every_config_key_documented` walk extends to assert both surfaces; no test renames since the existing one already covers the component half.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb337_core_schema.py` — new core-schema test passes.
- `uv run pytest -q ap2/tests/test_docs_drift.py::test_every_config_key_documented` — extended docs-drift gate passes against the new core subsection.
- `test -f ap2/core_config_schema.py` — new schema module exists.
- `grep -cE "ConfigKey\(" ap2/core_config_schema.py` — schema module declares ≥21 ConfigKey entries.
- `grep -nE "^### \[core\]" ap2/howto.md` — howto.md Config keys (TOML) section has a new core subsection.
- `uv run python -m ap2 --project . status` exits 0 (sanity that the schema wiring didn't break daemon-start).
- `ap2/core_config_schema.py` Prose: declares CORE_CONFIG_SCHEMA with at least the 21 keys (tick_s, mm_tick_s, task_timeout_s, control_timeout_s, max_retries, verify_cmd, verify_timeout_s, event_context, agent_model, agent_effort, task_max_turns, control_max_turns, verify_judge_max_turns, ideation_disabled, ideation_trigger_task_count, ideation_cooldown_s, ideation_max_turns, ideation_scrub_model, project_name, web_port, web_disabled); judge confirms via Read.
- `ap2/init.py` Prose: CONFIG_TEMPLATE rendering includes a core block emitting commented-out defaults for every key in CORE_CONFIG_SCHEMA; judge confirms via Read.

## Out of scope

- Migrating call sites to read via the new schema-validated path (TB-336 + TB-335 already migrate the remaining cfg readers; this task adds the schema, not new readers).
- Renaming or deprecating any existing flat env name — back-compat via FLAT_TO_SECTIONED is preserved.
- Adding new core knobs that don't already exist in FLAT_TO_SECTIONED.
- Validating _KNOBS_STAYING_ENV_ONLY cut-line (separate task; progress signal 6).
