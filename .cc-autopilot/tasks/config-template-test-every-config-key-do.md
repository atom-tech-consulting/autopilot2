## Goal

Ship axis (6) of the **Current focus: structured config (env →
TOML)** (goal.md L266 / L366-376): pin the new config-key surface
against documentation drift before more keys land. Goal.md's
delete-test at L375-376 says "if not shipped, the docs-drift
class TB-305 closed reopens against the new surface." TB-305
shipped `test_every_env_knob_documented` +
`_TEMPLATE_EXEMPT_KNOBS` against the flat `AP2_*` set; this task
ships the structurally-parallel sibling against the
component-schema-declared config keys (the 25-entry union
TB-322 produced + the core schema from TB-321). It also lands
the `CONFIG_TEMPLATE` sibling to `ENV_TEMPLATE` so `ap2 init`
writes a fresh `.cc-autopilot/config.toml` for new projects —
the goal.md L392-393 Progress signal "`.cc-autopilot/config.toml`
exists as the fresh-project default, written by `ap2 init`".

Why now: TB-322 just placed 25 config keys across 7 component
schemas, so the gate has real surface to assert against — last
cycle this was deferred as premature ("with zero schema keys
declared, the gate would pass vacuously"); that's no longer
true. Every cluster migrated under axis 5 will add 1-6 more
config keys; without the gate landing first, axis-5 migrations
ship key declarations whose howto.md entry can silently lag
behind — the exact class of drift TB-305 closed for env knobs.

## Scope

- `ap2/init.py`: new `CONFIG_TEMPLATE` constant — a TOML-rendered
  tree of every key declared in `aggregate_schemas(default_
  registry())` + the core schema, with each key shown with its
  default value commented above. Convention parallel to
  `ENV_TEMPLATE` at L259-356; lives next to it. `_ensure_file(
  autopilot_dir / "config.toml", CONFIG_TEMPLATE)` is added to
  the init path (mirrors L668's env-template install).
- `ap2/tests/test_docs_drift.py` (or a sibling — author's
  choice): new `test_every_config_key_documented` test that walks
  `aggregate_schemas(default_registry())` + the core schema and
  asserts every key path is either (a) referenced verbatim in
  `ap2/howto.md`'s `## Configuration knobs` section OR (b)
  present in a new `_CONFIG_TEMPLATE_EXEMPT_KEYS` frozenset.
- `ap2/init.py`: new `_CONFIG_TEMPLATE_EXEMPT_KEYS` frozenset
  (lives next to `_TEMPLATE_EXEMPT_KNOBS` for parallelism); any
  config keys legitimately absent from howto.md (deprecated,
  test-only, undocumented-on-purpose) go here with an inline
  comment naming the reason.
- `ap2/howto.md`: extend `## Configuration knobs` (or add a
  parallel `## Config keys (TOML)` block) with a tree-rendered
  table of config paths → defaults → descriptions, sourced from
  the schema declarations.

## Design

`CONFIG_TEMPLATE` renders the schema union as one `[section.
subsection]` block per component, with each `ConfigKey`
producing a `# <description>` line followed by `key = <default>`
(commented-out so the operator can uncomment to override).
Generation runs at module-import time (same shape as
`ENV_TEMPLATE`'s f-string interpolation) so the template stays
in lock-step with the schema; tests pin that every declared key
appears in `CONFIG_TEMPLATE`.

The drift gate matches TB-305's idiom verbatim: walk every key
in the schema union, assert each is documented OR exempt;
emitting a list of missing keys in the failure message so the
fix is obvious. The exempt frozenset is intentionally tiny at
launch (likely empty — TB-322 already populated descriptions
for every key); future deprecations land entries here rather
than removing the howto.md row, keeping the audit trail.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run pytest -q ap2/tests/test_docs_drift.py::test_every_config_key_documented`
  — the new gate passes against current HEAD (every key declared
  by `aggregate_schemas(default_registry())` + core schema is
  either in howto.md or in `_CONFIG_TEMPLATE_EXEMPT_KEYS`).
- `grep -q "CONFIG_TEMPLATE" ap2/init.py` — the template
  constant exists.
- `grep -q "_CONFIG_TEMPLATE_EXEMPT_KEYS" ap2/init.py` — the
  exempt frozenset exists.
- `grep -q "test_every_config_key_documented" ap2/tests/test_docs_drift.py`
  — the docs-drift gate test exists.
- `grep -qE "config\.toml" ap2/init.py` — the init path writes
  `.cc-autopilot/config.toml` from `CONFIG_TEMPLATE`.
- `ap2/howto.md` Prose: includes a tree-rendered list (or
  table) of TOML config-key paths covering every component
  schema TB-322 declared (mattermost / attention /
  focus_advance / auto_unfreeze / auto_approve /
  validator_judge / janitor); judge confirms via Read.
- `ap2/init.py` Prose: `CONFIG_TEMPLATE` body interpolates from
  the schema union rather than being a hand-maintained constant;
  judge confirms via Read that the template generation reads
  from `aggregate_schemas(default_registry())` or equivalent.

## Out of scope

- Migrating component-body reads to `cfg.<path>.<key>` (axis 5
  — separate TB-N per cluster).
- The `ap2 config` CLI verbs (axis 4 — TB-324).
- Pruning howto.md's existing `## Configuration knobs` env-knob
  table (it stays during the migration; will be cleaned up once
  axis 5 fully drains the env-only set down to
  `_KNOBS_STAYING_ENV_ONLY`).
- Auto-generating howto.md content from the schema (the test
  asserts content presence, not source-of-truth shift; defer
  generator work to a post-axis-5 cycle).
