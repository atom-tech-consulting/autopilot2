# Fix `ap2 config get/list` core-value resolution + agent_model default drift

Tags: #autopilot #config #bug #config-introspect #core-schema

## Goal

`ap2 config get core.agent_model` returns `(unset)` even though the
value is resolvable — both the live env override
(`AP2_AGENT_MODEL=claude-opus-4-8[1m]`) and, when unset, the
canonical default (`claude-opus-4-7`) are available to the runtime.
`ap2 config list` shows the same contradiction: `core.agent_model`
renders value `(unset)` with source `env-override` — the source
detector correctly sees `AP2_AGENT_MODEL` in `os.environ`, but the
value column reads empty. `core.agent_effort` has the identical
symptom; `core.control_timeout_s` and other keys that ARE
materialized Config dataclass attributes resolve fine.

Root cause is a divergence between how the runtime resolves core
values and how `config_introspect` does:

1. **Value-read divergence.** `config_introspect.collect_rows`
   (`ap2/config_introspect.py:193`) reads core values via
   `getattr(cfg, field_name, None)`. That works for keys overlaid
   onto the `Config` dataclass (`control_timeout_s`, `verify_cmd`,
   `tick_interval_s`, …) but returns `None` for keys that are ONLY
   resolved lazily through `cfg.get_core_value(...)` and never
   materialized as attributes — `agent_model`, `agent_effort`. The
   runtime reads these via `cfg.get_core_value("agent_model",
   default="claude-opus-4-7")` (daemon.py:226/903, verify.py:599,
   janitor `__init__.py`/`impl.py`); `getattr(cfg, "agent_model")`
   has no such attribute, so the introspection shows `(unset)`.
   Verified: `cfg.get_core_value("agent_model")` →
   `'claude-opus-4-8[1m]'`; `getattr(cfg, "agent_model", '<none>')`
   → `'<none>'`.

2. **Default drift.** `CORE_CONFIG_SCHEMA` declares `agent_model`
   with `default=""` (`ap2/core_config_schema.py` ~L235-238), but the
   real call-site default is `claude-opus-4-7`. `get_core_value`'s
   TB-337 contract (config.py:493-499) states the schema is meant to
   be "the single source of truth for default values," yet the four
   call sites hardcode `default="claude-opus-4-7"` while the schema
   says `""`. So even after fixing (1), a truly-unset `agent_model`
   would display `""` rather than its real default — still "does not
   retrieve the default value properly."

Fix both: make `config_introspect` resolve core values the same way
the runtime does (via `get_core_value`), and reconcile the
`agent_model` schema default to the canonical value so the schema is
genuinely the single source of truth.

Why now: the structured-config focus (TB-321→339) shipped
`ap2 config get/list` as the operator-facing discovery surface; a
key that silently misreports its most important tunable (the agent
model) as `(unset)` undermines exactly the discoverability that
focus delivered. Small, contained, regression-pinnable. Operator-
reported 2026-05-30; meta-infra, roadmap parked →
`--skip-goal-alignment`.

## Scope

- `ap2/config_introspect.py` `collect_rows` — change the core-field
  value read (currently `value = getattr(cfg, field_name, None)`,
  ~L193) to resolve through the runtime path:
  `value = cfg.get_core_value(field_name)`. This makes the displayed
  value identical to what the daemon actually uses — env override
  first, then TOML, then the schema default. Leave the source-
  attribution logic (`_attribute_source`) unchanged; it's correct.
  Preserve the `(unset)` rendering for a genuinely empty/None
  resolved value (e.g. a key with no env, no TOML, and an empty/None
  schema default).

- `ap2/core_config_schema.py` — set the `agent_model` ConfigKey
  `default` from `""` to `"claude-opus-4-7"` (the canonical call-site
  default), so `get_core_value`'s schema-default fallback (TB-337)
  yields the real default and the four call sites can rely on it.

- The four `cfg.get_core_value("agent_model", default="claude-opus-4-7")`
  call sites (`ap2/daemon.py` task-agent + control-agent dispatch,
  `ap2/verify.py` prose judge, `ap2/components/janitor/`) — drop the
  now-redundant explicit `default=` so the schema is the single
  source of truth (per TB-337's stated design). Behavior-preserving:
  the schema default now supplies the same `claude-opus-4-7`.

- `agent_effort`: leave its schema `default=""` AS-IS unless the
  janitor/verify-judge effort fallback chain proves it has a single
  canonical default — its `""` appears intentional (it falls through
  to per-job effort then `"high"` downstream). Document the decision
  in a one-line comment rather than changing it speculatively.

- Regression test (extend the existing config-introspect / cli_config
  test module, or add one): assert that `collect_rows` resolves
  `core.agent_model` to the env value when `AP2_AGENT_MODEL` is set
  (monkeypatched), and to `claude-opus-4-7` when all of
  sectioned-env / flat-env / TOML are absent — i.e. never `(unset)`
  / empty for a key with a non-empty default.

## Design

- **Single resolution path.** The bug is two readers of the same
  data disagreeing. `get_core_value` is the canonical resolver
  (env → TOML → schema default); `config_introspect` must use it
  rather than peeking at dataclass attributes that only some keys
  have. After the fix, `config get`/`list` shows exactly what a
  dispatch site would receive.

- **Schema as single source of truth (finish TB-337).** TB-337
  declared the intent but left `agent_model` at `default=""` with the
  real default duplicated inline at call sites. Setting the schema
  default + dropping the inline duplicates completes that design and
  removes the drift. Confirm no call site relies on `""`-vs-
  `claude-opus-4-7` distinction (none should — env is set in this
  project, and the prior inline default WAS `claude-opus-4-7`).

- **No behavior change to the daemon.** Agents currently resolve
  `agent_model` from `AP2_AGENT_MODEL` (env wins over both inline and
  schema default), so dispatch is unaffected. The change only fixes
  what `ap2 config get/list` *displays* and removes default duplication.

## Verification

- `uv run --extra dev pytest -q ap2/tests/` — full suite passes
  (project's canonical gate; `--extra dev` per the env's verify cmd).
- `grep -q "get_core_value(field_name)" ap2/config_introspect.py`
  — the core-value read now routes through `get_core_value`.
- `! grep -nE "getattr\(cfg, field_name" ap2/config_introspect.py`
  — the old `getattr`-based core value read is gone.
- `grep -q "claude-opus-4-7" ap2/core_config_schema.py`
  — the agent_model schema default is set to the canonical value.
- `! grep -rnE "get_core_value\(.agent_model., default=" ap2/daemon.py ap2/verify.py ap2/components/janitor/`
  — the redundant inline `default=` for agent_model is dropped at the
  call sites (schema is now the single source).
- `ap2/config_introspect.py` Prose: `collect_rows` resolves each
  `core.<field>` value via `cfg.get_core_value(field_name)` (not
  `getattr`), so an env-set or schema-defaulted key displays its
  resolved value rather than `(unset)`; source attribution is
  unchanged. Judge confirms via Read.
- `ap2/tests/` Prose: a regression test asserts `collect_rows` (or
  the `config get` path) resolves `core.agent_model` to the
  monkeypatched `AP2_AGENT_MODEL` env value, and to `claude-opus-4-7`
  when env + TOML are absent — never `(unset)`. Judge confirms via
  Read.

## Out of scope

- Reworking the source-attribution labels (`file` / `env-override` /
  `default`) — they're correct.
- Migrating remaining `getattr`-resolvable core keys to typed schema
  entries, or any broader config-schema expansion (TB-337 already
  declared the 21 core keys).
- Changing `agent_effort`'s `""` default (left intentional unless
  proven otherwise; see Scope).
- `ap2 config set` behavior — this fix is read-path only.
