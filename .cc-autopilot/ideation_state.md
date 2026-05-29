# Ideation State

_Last updated: 2026-05-29T02:19:18Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 198C / 0F. The three prior-cycle
proposals all landed (with one fix-forward each): TB-324 (axis 4 — `ap2
config list / get / set / validate` CLI, bf4168d → 2ebe1a6 closing the
exit-code-on-unknown-path bullet gap), TB-325 (axis 6 — `CONFIG_TEMPLATE`
+ `test_every_config_key_documented`, 2eb899c), TB-326 (axis 5 pilot —
`auto_approve` cluster via `Config.get_component_value("auto_approve",
<key>)`, b3eba54 → 60bdb1f closing two pre-existing latent bugs the
migration's walk surfaced). End-of-arc gate held: pytest green after each
fix-forward. The axis-5 pilot template (option 2 from TB-326 briefing —
helper on `Config`) is now proven; the 5 remaining component clusters
named in TB-326 Out-of-scope are the natural long-tail seed this cycle.

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Axis 1 shipped (TB-321, f5b0f0c): tomllib parser, `ConfigKey`,
      `aggregate_schemas`, `Config.from_toml`, `Manifest.config_schema`,
      daemon-start `validate_config` fail-fast gate.
    - Axis 2 shipped (TB-323, a50e686): `config_compat.py`
      FLAT_TO_SECTIONED (62 entries) + `_KNOBS_STAYING_ENV_ONLY`
      12-factor exemption + sectioned-env > flat-env > TOML precedence +
      one-shot `env_deprecated` event + TOML mtime hot-reload.
    - Axis 3 shipped (TB-322, e38bb38): `config_schema` declared on all
      6 remaining component manifests; 25-entry union pinned.
    - Axis 4 shipped (TB-324, bf4168d + 2ebe1a6): `ap2 config list / get
      / set / validate` CLI with source attribution (file / env-override
      / default), operator-queue-routed writes, audit events.
    - Axis 6 shipped (TB-325, 2eb899c): `CONFIG_TEMPLATE` rendered from
      `aggregate_schemas` at module-import (25 keys / 7 components, all
      commented-out at defaults), `_CONFIG_TEMPLATE_EXEMPT_KEYS` analogue,
      `test_every_config_key_documented` docs-drift gate.
    - Axis 5 pilot shipped (TB-326, b3eba54 + 60bdb1f): 3 auto_approve
      env reads in `ap2/components/auto_approve/` switched to
      `Config.get_component_value("auto_approve", <key>)`; pattern doc'd
      in the manifest; two pre-existing latent bugs exposed by the
      migration's walk also closed.
  - Gaps:
    - **Axis 5 long tail — 5 component clusters unmigrated** (goal.md
      L353-364). FLAT_TO_SECTIONED enumerates the per-component knobs:
      `auto_unfreeze` (5 knobs), `attention` (4 knobs), `focus_advance`
      (2 knobs), `janitor` (4 knobs), `validator_judge` (5 knobs). Each
      component body still calls `os.environ.get("AP2_<CLUSTER>_*")`
      directly even though TB-322 declared the schemas and TB-323 plumbed
      the override layer. Goal.md L398-399 progress signal "≥80% of
      source-side `os.environ.get('AP2_*')` calls migrated to
      `cfg.<path>.<key>` reads" sits at ~3/N migrated post-pilot.
    - **Core (non-component) cluster — ~20 `AP2_*` core knobs
      unmigrated** (goal.md L353-364 "auto_approve, auto_unfreeze,
      attention, etc."). FLAT_TO_SECTIONED maps `AP2_TICK_S`,
      `AP2_AGENT_MODEL`, `AP2_AGENT_EFFORT`, `AP2_TASK_MAX_TURNS`, etc.
      to `core.<key>`. Read paths in `ap2/config.py` / `ap2/daemon.py` /
      `ap2/verify.py` still consult `os.environ.get` directly. Lower
      priority than the per-component long tail this cycle — fewer
      readers per knob, no schema sprawl gain, and the pilot's component
      template doesn't transfer directly (core knobs have no
      `get_component_value("core", ...)` equivalent — needs a sibling
      accessor or use of `cfg.core.<key>` field access already plumbed).
  - Status: `in-progress`

## Non-goal risk check

None. All 5 proposed tasks sit squarely in goal.md L353-364 (axis 5
migration of existing knobs) and follow the TB-326 pilot template
verbatim. Pure read-path swaps, bit-identical observable behavior, env
back-compat preserved per TB-323. No drift into Non-goals at L405-447
(no env-knob renames, no behavior deletions, no API-stability
commitments).

## Considered & deferred this cycle

- **Core (non-component) knob cluster migration**: ~20 `AP2_TICK_S`,
  `AP2_AGENT_MODEL`, etc. reads in `ap2/config.py` / `ap2/daemon.py`. Real
  scope, but the read-shape differs from the per-component pilot
  (component pattern uses `Config.get_component_value("auto_approve",
  key)`; core knobs need either a `get_core_value` sibling or use of the
  existing `cfg.<field>` dataclass attributes). Defer one cycle — let the
  5 per-component cluster migrations land first to surface any shape
  issues; the core cluster benefits from any helper extracted post-batch.
- **Mattermost cluster migration**: ZERO scope. All `AP2_MM_*` knobs are
  in `_KNOBS_STAYING_ENV_ONLY` per TB-323 (deployment identity, channel
  identity, secrets — true 12-factor). No briefing to write; goal.md
  L400-403 explicitly carves these out.
- **Per-cluster Verification redundancy**: each migration brief reuses
  the TB-326 verification shape (regression-gate pytest + cluster-scoped
  grep-walk + new behavioral test). Considered consolidating into a
  single "migrate all 5 clusters" task; rejected because (a) goal.md L361
  says "one TB-N per logical cluster" verbatim and (b) per-cluster
  isolation lets the agent ship one and verify before touching the next.
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes TB-185/184 (meta-polish unconnected to focus), TB-175
  (premature aggregation), TB-231/240 (symptom-patching / validator
  whack-a-mole). None of the 5 proposed tasks match — each is direct
  goal.md axis-5 build-out with a named delete-test and a proven pilot
  template. Pattern carried so future cycles re-verify alignment as the
  long tail drains.

## Cycle observations

- The TB-326 fix-forward (60bdb1f closed "two pre-existing latent bugs
  the migration's walk exposed") confirms the migration walk has
  diagnostic value beyond the read-path swap — the agent's
  cfg-attribution audit surfaces real defects. Carry into per-cluster
  briefings as an expected side-effect (no scope change; just don't be
  surprised if a follow-up commit closes an unrelated gap).
- The axis-5 long tail is now mechanically near-identical per cluster.

## Decisions needed from operator

(none — the 5 proposed tasks are direct build-out of operator-authored
goal.md L353-364 axis-5 long tail with explicit delete-tests and a
proven pilot template (TB-326). Operator approval via `ap2 approve
TB-N` is the standard review-gate path; auto-approve will likely fire
for these per the `#axis-5` `#migration` tag pattern that auto-approved
TB-326.)

## Proposals this cycle

- TB-327 (axis 5 — auto_unfreeze cluster): migrate 5
  `AP2_AUTO_UNFREEZE_*` env reads in
  `ap2/components/auto_unfreeze/` to cfg-based reads via the TB-326
  `Config.get_component_value` helper.
- TB-328 (axis 5 — attention cluster): migrate 4 `AP2_ATTENTION_*` /
  `AP2_TASK_STUCK_THRESHOLD_S` / `AP2_TASK_FROZEN_RECENCY_S` reads in
  `ap2/components/attention/`.
- TB-329 (axis 5 — focus_advance cluster): migrate 2
  `AP2_FOCUS_AUTO_ADVANCE_DISABLED` / `AP2_FOCUS_ADVANCE_EMPTY_CYCLES`
  reads in `ap2/components/focus_advance/`.
- TB-330 (axis 5 — janitor cluster): migrate 4 `AP2_JANITOR_*` reads
  in `ap2/components/janitor/`.
- TB-331 (axis 5 — validator_judge cluster): migrate 5
  `AP2_VALIDATOR_JUDGE_*` reads in
  `ap2/components/validator_judge/`.