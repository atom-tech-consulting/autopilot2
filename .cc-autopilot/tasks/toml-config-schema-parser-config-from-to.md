## Goal

Land axis (1) of the **Current focus: structured config (env → TOML)**
focus (goal.md L304-315): ship the TOML parser, the schema-registry
shape, the `Config.from_toml(path)` constructor, and a daemon-start
schema validator, proven end-to-end via one canary component
(`janitor/`) declaring its `config_schema`. This is the prerequisite
axis — every subsequent axis (per-component schemas, env-var override
layer, CLI surface, knob migration, docs-drift gate) has nothing to
read against without it.

Why now: zero TB-Ns have shipped against this focus and Backlog is
empty; goal.md L378-381 explicitly names axis (1) as the prerequisite
— "(1) is the prerequisite for everything else. (2) and (3) are
parallelizable once (1) lands." Without this slice the parallelizable
follow-ups (TB-322, TB-323) cannot ship in their natural shape, and
the daemon-start validator that catches schema mismatches at
fail-fast time has no engine.

## Scope

(1) `ap2/config_loader.py` — new module containing:
  - `tomllib`-based parser (Python 3.11 stdlib; no new dependency).
  - `ConfigKey` typed dataclass (name, type, default, description,
    hot_reloadable: bool) declaring the per-key schema shape.
  - `Config.from_toml(path)` constructor that returns the same
    `Config` dataclass `Config.load()` already returns (drop-in
    shape compatibility).
  - `validate_config(loaded_toml, registry)` daemon-start validator
    that walks `default_registry()`'s manifests, aggregates every
    `config_schema` entry, and validates the loaded TOML against
    the union — fails fast with a clear path-naming error
    (e.g. `[components.janitor] disabled = "yes": expected bool,
    got str`).

(2) Extend the `Manifest` dataclass in `ap2/registry.py` with a new
    `config_schema: dict[str, ConfigKey]` field (default empty
    dict so the six other component manifests don't break).

(3) Janitor canary: `ap2/components/janitor/manifest.py` gains a
    `config_schema={"disabled": ConfigKey(...)}` entry covering
    the existing `AP2_JANITOR_DISABLED` knob — proves the
    end-to-end parse → schema → registry walk works.

(4) Plumb `from_toml` into `Config.load()` as an opt-in branch: if
    `.cc-autopilot/config.toml` exists, prefer it; else fall back
    to today's env-only `Config.load()` path. Zero behavior change
    for existing installs (no config.toml exists → env path runs).

(5) New regression-pin module `ap2/tests/test_tb321_toml_config.py`:
    parser round-trip on a fixture TOML (written to `tmp_path`),
    schema validator rejects bad types with the right error
    message, janitor canary schema integrates with the registry
    walk, fallback-to-env-path when no config.toml exists.

## Design

`tomllib.loads(text)` returns a dict; the parser is a thin wrapper
that walks the top-level keys (`[core.*]` and `[components.<name>]`
are the only valid sections per goal.md L307-310) and returns a
typed shape ready for `validate_config`.

`ConfigKey` schema declarations live on the manifest, NOT in a
central registry — each component owns the truth for its own knobs.
The registry's `aggregate_schemas()` accessor walks
`default_registry().manifests()` and returns `dict[str, dict[str,
ConfigKey]]` keyed by component name. `validate_config` uses this
union to confirm every loaded key has a declared schema entry (extra
keys = fail) and every value matches the declared type.

`Config.from_toml(path)` returns a `Config.load()`-compatible
instance: walks the validated TOML, maps `[core.<key>]` to existing
`Config` dataclass fields by name, and stashes
`[components.<name>.<key>]` values on a per-component sub-dict
(per-component reads from `cfg.<path>.<key>` are axis-5 follow-ups
— this TB lays the read paths only). Returning the same `Config`
instance shape means daemon code reading `cfg.tick_interval_s` etc.
continues unchanged.

Daemon-start integration: `daemon.main_loop` calls `validate_config`
exactly once at startup, before the tick loop begins. On schema
mismatch the daemon refuses to start and prints the validator error
to stderr — operator-fix-first shape, no auto-correction (goal.md
L312-313). The error message names the bad key path so the operator
can grep their config file directly.

Manifest field default: `config_schema: dict[str, ConfigKey] =
field(default_factory=dict)` so the six other component manifests
(mattermost, attention, focus_advance, auto_unfreeze, auto_approve,
validator_judge) continue to load without immediate per-component
declarations — TB-322 fills them in as the axis-3 follow-up.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the new
  module + manifest extension + canary land.
- `uv run pytest -q ap2/tests/test_tb321_toml_config.py` — new
  regression-pin module passes.
- `test -f ap2/config_loader.py` — new module exists at the
  expected path.
- `grep -q "config_schema" ap2/registry.py` — `Manifest` dataclass
  gains the new field.
- `grep -q "class ConfigKey" ap2/config_loader.py` — typed schema
  dataclass is declared.
- `grep -q "config_schema" ap2/components/janitor/manifest.py` —
  janitor canary fills in a real schema entry.
- `grep -q "from_toml" ap2/config_loader.py` — `Config.from_toml`
  constructor is implemented.
- `! grep -qE "^from ap2\.components" ap2/config.py` — the env-path
  config module stays free of direct component imports (registry-
  walked discovery only; TB-311 import-direction-gate parity).
- `! grep -qE "^from ap2\.components" ap2/config_loader.py` — the
  new TOML config layer also avoids static component imports;
  registry walk is the cross-reference path.
- `ap2/components/janitor/manifest.py` Prose: the janitor manifest's
  `config_schema` declaration covers the `AP2_JANITOR_DISABLED`
  knob with type `bool`, default matching the in-source default,
  and a non-empty description; judge confirms via Read of the
  manifest body.
- `ap2/config_loader.py` Prose: `validate_config` walks
  `default_registry().manifests()`, aggregates every manifest's
  `config_schema`, and rejects bad-type values with an error
  message naming the path (e.g. `[components.janitor] disabled =
  ...: expected bool, got <actual>`); judge confirms via Read of
  the validator body and the regression-pin test that exercises
  the error message.

## Out of scope

- Per-component `config_schema` declarations for the six remaining
  components (mattermost, attention, focus_advance, auto_unfreeze,
  auto_approve, validator_judge) — TB-322 (axis 3).
- Env-var override layer + `ap2/config_compat.py` back-compat map
  + `env_deprecated` event vocabulary — TB-323 (axis 2).
- `ap2 config list / get / set / validate` CLI verbs — axis 4,
  deferred to a future cycle once read paths exist.
- Migration of any `os.environ.get("AP2_*")` reads to
  `cfg.<path>.<key>` shape — axis 5, per-cluster long-tail
  follow-ups.
- TB-305-style docs-drift gate for config-schema keys — axis 6,
  premature until per-component schemas land (TB-322).
- Writing `.cc-autopilot/config.toml` from `ap2 init` — axis 6
  fresh-init concern, deferred.
- Migration of any actual env-knob behavior — this TB lands the
  read paths; behavior preservation is the bit-identical
  guarantee (goal.md L439-442).
