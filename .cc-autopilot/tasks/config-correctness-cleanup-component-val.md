# Config-correctness cleanup: component-value resolution + inline-default consistency + auto_diagnose schema coverage

Tags: #autopilot #config #bug #config-introspect #core-schema #cleanup

## Goal

Three related config-correctness issues surfaced auditing
`ap2 config get/list` after TB-344:

1. **Component-value resolution bug (the TB-344 twin).**
   `config_introspect._resolve_component_value` reads
   `cfg.components_config[comp_name][key_name]` directly and falls
   back to `spec.default` — it NEVER calls
   `cfg.get_component_value(...)`. So `ap2 config get/list` misses
   lazily-resolved env overrides for component keys, displaying the
   schema default instead. Confirmed live: with
   `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=100000000` and
   `AP2_AUTO_APPROVE=1` set, `get_component_value("auto_approve",
   "window_token_cap")` correctly returns `'100000000'` and
   `("auto_approve","enabled")` returns `'1'`, but `config get
   components.auto_approve.window_token_cap` reports `0` and
   `.enabled` reports `False` (the schema defaults), each with source
   `env-override` — the same value/source contradiction TB-344 fixed
   for core keys. TB-344 repaired the core branch of `collect_rows`
   (`getattr` → `get_core_value`) but left the component branch
   (`_resolve_component_value`) with the identical defect. Runtime is
   correct; only `config get/list` display lies.

2. **Inline-default inconsistency.** Two core keys are read with
   conflicting inline defaults across call sites: `ideation_disabled`
   (`default=""` at one site, `default=None` at another) and
   `ideation_scrub_model` (`default=""` vs `default=None`). Harmless
   today (the schema-default backstop + falsy-equivalence absorb it)
   but untidy and a latent footgun — per TB-337 the schema is meant to
   be the single source of truth for defaults.

3. **Schema-coverage gap.** `auto_diagnose_cooldown_s` and
   `auto_diagnose_idle_threshold_s` are in `FLAT_TO_SECTIONED` (so
   `ap2 config list` shows them) but absent from `CORE_CONFIG_SCHEMA`,
   so they list with no description/type and no centrally-declared
   default. They resolve fine (materialized Config attrs); this is a
   metadata completeness gap, not a resolution bug.

Fix all three: route component-value introspection through
`get_component_value` (mirroring TB-344's core fix), make the two
inline defaults consistent (prefer relying on the schema default per
TB-337), and add the two `auto_diagnose_*` keys to the core schema.

Why now: `ap2 config get/list` is the operator-facing config-discovery
surface the structured-config focus delivered; a component key that
misreports its env-overridden value (e.g. the auto-approve token cap
showing `0` instead of `100000000`) directly undermines that
discoverability — the same correctness class as the bug TB-344 just
closed for core keys. Operator-reported 2026-05-30; meta-infra,
roadmap parked → `--skip-goal-alignment`. Builds on TB-344's
config_introspect changes and TB-345's same-neighborhood edits →
`@blocked:TB-345`.

## Scope

1. **`ap2/config_introspect.py` `_resolve_component_value`** — resolve
   the value via `cfg.get_component_value(comp_name, key_name)` (the
   same env→TOML→schema-default path the runtime uses), instead of
   reading `cfg.components_config[...]` + `spec.default`. Preserve the
   never-set→schema-default display: if `get_component_value` already
   falls back to the schema default when unset, a bare call suffices;
   otherwise pass `default=spec.default`. This is the exact mirror of
   TB-344's core-branch fix (line 193).

2. **`ideation_disabled` + `ideation_scrub_model` inline defaults** —
   make the call sites consistent. Prefer dropping the redundant
   inline `default=` so both rely on the `CORE_CONFIG_SCHEMA` default
   (single source of truth, per TB-337): `ideation_disabled` → schema
   default `False`; `ideation_scrub_model` → schema default
   `DEFAULT_IDEATION_SCRUB_MODEL`. Behavior MUST be unchanged — verify
   each consumer resolves the same value for both the set and unset
   cases before and after (the scrub model must still resolve to the
   haiku default when unset; ideation_disabled must still be falsy
   when unset). If dropping the inline default would change resolved
   behavior for either, instead align both sites to the same explicit
   default and note why.

3. **`ap2/core_config_schema.py`** — add `auto_diagnose_cooldown_s`
   (int, default = the current `DEFAULT_AUTO_DIAGNOSE_COOLDOWN_S` /
   observed 21600) and `auto_diagnose_idle_threshold_s` (int, default
   = `DEFAULT_AUTO_DIAGNOSE_IDLE_THRESHOLD_S` / observed 10800) as
   `CORE_CONFIG_SCHEMA` entries with description + type, so `config
   get/list` shows their metadata and the default is declared
   centrally. Pull the default from the existing `DEFAULT_*` constant
   rather than hardcoding the literal.

## Design

- **Single resolution path (parts 1).** The bug is two readers of the
  same data disagreeing — the runtime resolver (`get_component_value`,
  env-first) vs the introspection reader (`components_config` dict +
  `spec.default`). After the fix, `config get/list` shows exactly what
  a component's runtime call would receive, for both env-set and
  unset keys. Source attribution (`_attribute_source`) is already
  correct and unchanged.

- **Schema as single source of truth (part 2).** TB-337 established
  the schema-default-fallback contract; these two keys still carry
  redundant (and inconsistent) inline defaults. Dropping them
  completes that intent. Gate strictly on behavior-equivalence — this
  is a tidy-up, not a behavior change.

- **No runtime/behavior change anywhere.** Part 1 is display-only
  (runtime already resolves correctly). Part 2 is behavior-preserving
  by construction (verify before/after). Part 3 adds schema metadata;
  the keys already resolve via their materialized Config attributes,
  so resolution is unaffected.

## Verification

- `uv run --extra dev pytest -q ap2/tests/` — full suite passes.
- `! grep -nE "components_config.*\[.*key_name.*\]|spec\.default" ap2/config_introspect.py` — `_resolve_component_value` no longer reads the components_config dict / bare spec.default (it routes through get_component_value). (If a spec.default reference legitimately remains as the get_component_value fallback arg, keep this bullet's pattern tight to the OLD dict-read shape — adjust to match what's actually removed.)
- `grep -q "get_component_value" ap2/config_introspect.py` — component values now resolve via get_component_value.
- `grep -q "auto_diagnose_cooldown_s" ap2/core_config_schema.py` — the cooldown key is now in the core schema.
- `grep -q "auto_diagnose_idle_threshold_s" ap2/core_config_schema.py` — the idle-threshold key is now in the core schema.
- New regression test: with `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` monkeypatched to a non-default value, `collect_rows` (or the `config get` path) reports that value for `components.auto_approve.window_token_cap` — never the schema default — AND reports the schema default when the env is absent. Mirrors TB-344's core regression test for the component branch.
- `ap2/config_introspect.py` Prose: `_resolve_component_value` resolves through `cfg.get_component_value(comp_name, key_name)` so an env-overridden component key displays its resolved value (matching the runtime) rather than the schema default; never-set keys still display the schema default. Judge confirms via Read.
- `ap2/core_config_schema.py` Prose: `auto_diagnose_cooldown_s` and `auto_diagnose_idle_threshold_s` are declared in `CORE_CONFIG_SCHEMA` with type, description, and a default sourced from the existing `DEFAULT_*` constant. Judge confirms via Read.
- Prose: the `ideation_disabled` and `ideation_scrub_model` call sites use a single consistent default (the schema default unless behavior-equivalence required keeping an explicit one), and a test or the existing suite confirms their resolved value is unchanged for set + unset cases. Judge confirms via Read.

## Out of scope

- Any change to `get_core_value` / `get_component_value` resolution
  precedence — they're correct; this fixes the introspection reader
  to USE them.
- The `focus_advance` → core merge / knob rename (TB-345) — separate;
  this task blocks on it to avoid overlapping `config_introspect` /
  core-schema edits.
- Dropping the redundant `AP2_IDEATION_MAX_TURNS=100` operator-env
  override (it equals the default) — that's an operator `.cc-autopilot/env`
  edit, not a code change.
- Source-attribution label changes — `_attribute_source` is correct.
