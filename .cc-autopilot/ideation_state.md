# Ideation State

_Last updated: 2026-05-29T00:10:00Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 195C / 0F. The three
prior-cycle proposals all landed cleanly inside ~3h: TB-321
(axis 1 — TOML schema + parser + `Config.from_toml` +
`Manifest.config_schema`, f5b0f0c), TB-322 (axis 3 — `config_schema`
filled on the 6 remaining component manifests, e38bb38), TB-323
(axis 2 — env-override layer + `config_compat.py` FLAT_TO_SECTIONED
map of 62 knobs + `env_deprecated` one-shot + config.toml mtime
watch, a50e686). End-of-arc gate held: 2386/2386 pytest green
after TB-323. The three parallelizable downstream axes (4, 5, 6) per goal.md L378-382
are now all unblocked. This cycle moves to the operator-facing
surface (axis 4) + the docs-drift sibling (axis 6) + the pilot
cluster of the long-tail axis 5 migration.

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Axis 1 shipped: `ap2/config_loader.py` (tomllib parser,
      `ConfigKey` dataclass, `aggregate_schemas`, `validate_config`
      with named-path error, `Config.from_toml`) + `Manifest`
      gained `config_schema` field + janitor canary + daemon
      fail-fast validation gate at `main_loop` start (TB-321,
      f5b0f0c).
    - Axis 3 shipped: `config_schema` declared on all 6 remaining
      component manifests (mattermost, attention, focus_advance,
      auto_unfreeze, auto_approve, validator_judge); 25-entry
      union pinned by `test_tb322_component_schemas.py` (TB-322,
      e38bb38).
    - Axis 2 shipped: `ap2/config_compat.py` FLAT_TO_SECTIONED
      map (62 entries) + `_KNOBS_STAYING_ENV_ONLY` 12-factor
      exemption + sectioned-env > flat-env > TOML precedence +
      one-shot `env_deprecated` event per flat knob per process +
      `env_reload.py` watching `.cc-autopilot/config.toml` mtime
      (TB-323, a50e686).
  - Gaps:
    - **Axis 4 — CLI surface missing** (goal.md L342-351). No
      `ap2 config` subcommand exists (Grep confirms zero matches
      for `"ap2 config"` across `ap2/cli*.py`). Operators still
      have to read `.cc-autopilot/config.toml` + `ap2/howto.md`
      directly to enumerate knobs; the delete-test at L349-351
      says "if not shipped, the new config surface is present but
      not operator-discoverable." `ap2 config list / get / set /
      validate` is the discoverability surface goal.md's Progress
      signals at L394-395 explicitly require.
    - **Axis 6 — docs-drift gate + `CONFIG_TEMPLATE` missing**
      (goal.md L366-376). No `CONFIG_TEMPLATE` sibling to
      `ENV_TEMPLATE` in `ap2/init.py` (only `ENV_TEMPLATE` at
      L259); no `test_every_config_key_documented` test exists
      (Grep). The 25-key schema union from TB-322 is now the
      surface the gate can assert against.
    - **Axis 5 — zero clusters migrated** (goal.md L353-364). The
      62-entry FLAT_TO_SECTIONED map is in HEAD, but no component
      body has actually switched its `os.environ.get("AP2_FOO")`
      reads to `cfg.components.<name>.<key>` reads. Per goal.md
      L361 "one TB-N per logical cluster"; the long tail starts
      now. auto_approve is the natural pilot (9 knobs per
      FLAT_TO_SECTIONED — the largest single cluster — and the
      most operator-facing component).
  - Status: `in-progress`
  - Reasoning: the
    three remaining axes are independently startable, and axis 4
    + axis 6 + axis-5-pilot are the highest-impact / lowest-risk
    next slice that progresses all three in parallel without
    burning future flexibility (axis 5 cluster pattern needs a
    pilot to validate before queueing 6 more identical-shape
    tasks).

## Non-goal risk check

None. All three proposed tasks sit squarely in goal.md L342-376
(axes 4 + 6) and L353-364 (axis 5). Axis 4 adds a discoverability
surface; axis 6 adds a regression gate; axis 5 pilot is a pure
read-path swap with bit-identical behavior. No drift into the
Non-goals at L405-447 (no multi-tenancy, no goal.md auto-rotation,
no API-stability commitments on `ap2/core/`, no behavior change
beyond `cfg`-routing of an existing read).

## Considered & deferred this cycle

- **Axis 5 cluster 2-7 (attention, focus_advance, auto_unfreeze,
  mattermost, validator_judge, janitor, core)**: each is a clean
  ~30-line read-swap TB-N once auto_approve pilot validates the
  template, but queueing 7 identical-shape tasks before the
  pilot lands risks 7 identical fixups if the pilot surfaces a
  template gap (e.g. test-fixture pattern, kill-switch handling,
  `env_deprecated` interaction). Defer to a post-pilot cycle.
- **`ap2 config edit` interactive flow / TOML-aware editor**:
  goal.md L342-351 enumerates `list / get / set / validate` only;
  an `edit` verb would extend scope without operator-stated need.
  Defer until/unless operator surfaces the need.
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes TB-185/184 (meta-polish unconnected to focus),
  TB-175 (premature aggregation), TB-231 (symptom-patching),
  TB-240 (validator whack-a-mole). None of the three proposed
  tasks match those shapes — each is direct goal.md axis build-out
  with a named delete-test. Pattern carried so future cycles
  re-verify alignment as axes drain.

## Cycle observations

- 110-call sweep observation from last cycle (axis 5 sizing
  estimate) is now superseded by the more precise 62-entry
  FLAT_TO_SECTIONED in TB-323's HEAD — drop. The migration tail
  is bounded by FLAT_TO_SECTIONED keys, not the broader
  `os.environ.get("AP2_*")` count (some of those reads will stay
  env-only per `_KNOBS_STAYING_ENV_ONLY`).
- New observation worth carrying once: axes 4 + 5 + 6 are now
  fully parallelizable per goal.md L378-382; the axis-5 long tail
  may be the right place for the operator to seed ~5 cluster TB-Ns
  at once (one per component) once the pilot template is proven,
  rather than ideation drip-feeding them one cycle at a time. Flag
  for re-evaluation after TB-326 (auto_approve pilot) lands.

## Decisions needed from operator

(none — the three proposed tasks are direct build-out of
operator-authored goal.md axes 4 / 5 / 6 with explicit delete-tests
and Progress signals already specified; operator approval via
`ap2 approve TB-N` is the standard review-gate path. The pilot
vs bulk-queue choice for axis 5 will surface as a real decision
only after TB-326 lands — not this cycle.)

## Proposals this cycle

- TB-324 (axis 4): `ap2 config list / get / set / validate` CLI
  surface — operator-queue-routed writes, audit events, source
  attribution (file / env-override / default).
- TB-325 (axis 6): `CONFIG_TEMPLATE` sibling in `ap2/init.py` +
  `test_every_config_key_documented` docs-drift gate
  (TB-305-parallel for config keys).
- TB-326 (axis 5 pilot): migrate the auto_approve knob cluster
  (9 keys) from `os.environ.get("AP2_AUTO_APPROVE_*")` to
  `cfg.components.auto_approve.<key>` reads — pilot for the
  remaining 6 component clusters.