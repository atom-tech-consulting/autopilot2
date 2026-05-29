# Ideation State

_Last updated: 2026-05-29T07:13:33Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 2B / 0P / 205C / 0F. Of last cycle's 4
proposals (TB-332..335) 3 reconciled: TB-332 auto_approve cross-package
landed clean (f1a6176), TB-334 added the `get_core_value` helper +
migrated 11 agent-runtime reads (d4404ef), TB-331 closed validator_judge
component-body (386dd2d). TB-333 verification_failed once (3750f32) on
the LAST bullet only — `\`ap2 doctor --project .\`` was the wrong CLI
arg order (correct: `ap2 --project . doctor`); operator has already
queued an `update` op (uuid f5c8421e at 06:40:41Z) so the briefing fix
will drain on next tick and re-dispatch — no fix-task needed from
ideation. TB-335 (core ideation cluster) still in Backlog blocked on
TB-334; TB-334 just completed so it auto-unblocks and dispatches next
tick. The TB-326 `cfg.get_component_value` + new `get_core_value` pilot
templates carried verbatim across all 3 landed clusters.

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Axis 1 (TB-321 f5b0f0c), 2 (TB-323 a50e686), 3 (TB-322 e38bb38),
      4 (TB-324 bf4168d/2eb899c), 6 (TB-325 2eb899c — tree-render +
      `test_every_config_key_documented`) shipped.
    - Axis 5 component bodies: TB-326..331 closed 6 of 7 components
      (auto_approve, auto_unfreeze, attention, focus_advance, janitor,
      validator_judge); mattermost component-body knobs intentionally
      stay env-only — they're listed in `_KNOBS_STAYING_ENV_ONLY`
      (config_compat.py L207-212: AP2_MM_CHANNELS/BOT_USER_ID/TEAM_ID/
      REPORT_CHANNEL/MENTION) as 12-factor exempts.
    - Axis 5 cross-package landed: TB-332 (auto_approve, f1a6176)
      + TB-334 core agent-runtime (d4404ef adds `Config.get_core_value`
      + migrates 11 reads in daemon.py / verify.py / status_report.py /
      janitor/).
  - Gaps:
    - **~8 cross-package strays remaining**, all in
      FLAT_TO_SECTIONED so they have a sectioned home but still call
      `os.environ.get` directly. `grep -rnE "os\.environ\.get\(.AP2_"`
      excluding tests + config.py/env_reload.py bootstrap +
      `_KNOBS_STAYING_ENV_ONLY` (the mattermost 5 + sandbox.py L1056
      `AP2_MM_TEAM_ID`): web.py L214 (`AP2_WEB_DISABLED`) + L226
      (`AP2_WEB_PORT`) → `core.web_*`; goal.py L419
      (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`) + L446
      (`AP2_FOCUS_AUTO_ADVANCE_DISABLED`) →
      `components.focus_advance.*`; doctor.py L374 (`AP2_VERIFY_CMD`)
      + L375 (`AP2_VERIFY_TIMEOUT_S`) → `core.verify_*`; ideation.py
      L845 (`AP2_IDEATION_MAX_TURNS`) — TB-334 was meant to cover this
      per its briefing (per agent-runtime sweep) but the commit only
      hit daemon.py/verify.py/status_report.py; components/attention/
      L234 (`AP2_AUTO_APPROVE_COST_APPROACH_PCT`) — cross-COMPONENT
      read of an auto_approve knob from attention's body.
    - **No `[core.*]` ConfigKey schema declared.** TB-334 added
      `Config.core_config: dict[str, Any]` + `get_core_value` helper
      but the [core.*] section has no per-key schema declaration —
      `from_toml` stashes every `[core.<key>]` entry verbatim without
      type/default/description validation. Asymmetric with axis 3's
      per-component `Manifest.config_schema` (TB-322). A typo in a
      `[core.web_port]` TOML key silently returns the default; no
      validator catches it. howto.md L2376-2379 explicitly flags this:
      "schema deferred to a future axis; current round-trip is
      shape-only." 21 known core keys via `FLAT_TO_SECTIONED` (tick_s,
      mm_tick_s, task_timeout_s, control_timeout_s, max_retries,
      verify_cmd, verify_timeout_s, event_context, agent_model/effort,
      task_max_turns, control_max_turns, verify_judge_max_turns, the
      5 ideation_* + project_name + web_port/web_disabled) — a single
      declaration set parallels TB-322's ~25-key component union.
    - **`_KNOBS_STAYING_ENV_ONLY` exempt list has no enforcement
      test.** Comment block at config_compat.py L193-212 documents
      the cut line (Mattermost identity, sandbox `AP2_REAL_SDK`/
      `AP2_DIR`, OAuth) but nothing fails CI when a NEW
      `os.environ.get("AP2_*")` is added outside that set + outside
      the bootstrap path (config.py/env_reload.py). Progress signal
      L401-403 reads "documented in a single comment block ... and
      is clearly minimal" — the comment block exists; the gate that
      keeps it minimal does not. Parallel to TB-305 docs-drift gate
      for env-knob documentation and TB-325 docs-drift gate for
      config keys.
  - Status: `in-progress`

## Non-goal risk check

None. All 3 proposals are read-path swap or test-gate addition that
goal.md L384-389 explicitly green-lights ("does this migrate a
previously-env-only knob into the config schema without losing
back-compat?"). No env renames; no API stability commitments; no
behavior changes; the new test (TB-338) reuses the
`test_every_env_knob_documented` / `test_every_config_key_documented`
pattern operator has already approved twice (TB-305, TB-325).

## Considered & deferred this cycle

- **TB-333 failure remediation as a new fix-task**. Failure-review
  classification: edit-briefing (only the LAST shell bullet failed
  with `exit=2` from CLI arg-order confusion — `ap2 doctor --project .`
  parses `--project` as an unknown flag on the `doctor` subparser;
  correct shape is `ap2 --project . doctor`). Operator already queued
  an `update` op (events.jsonl 06:40:41Z uuid=f5c8421e) so the
  briefing fix will drain on next tick and re-dispatch — no ideation
  fix-task needed. Note in "Decisions needed from operator" below
  flagged unnecessary since the action is already in flight.
- **AP2_IDEATION_MAX_TURNS straggler as its own task**. Folded into
  TB-336 (1 read; would be sub-trivial standalone).
- **Mattermost component-body migration**. Verified OFF the migration
  table: all 5 MM knobs (channels/bot_user_id/mention/team_id/
  report_channel) appear verbatim in `_KNOBS_STAYING_ENV_ONLY` at
  config_compat.py L207-212. Don't re-propose.
- **Howto.md `## Configuration knobs` flat-list deprecation**. The
  L1424 flat env-var list coexists with the L2358 `## Config keys
  (TOML)` tree-render by design — L2391-2395 documents the
  intentional dual-surface ("flat surface stays read-supported
  indefinitely; TOML surface is forward-canonical"). Deferred as a
  no-op.
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes TB-185/184 (utility unaligned with focus / parallel
  surface eroding goal.md authority), TB-175 (premature aggregation),
  TB-231/240 (symptom-patching / validator whack-a-mole), TB-172
  (linter whack-a-mole). None of the 3 proposals match — each is
  direct axis-5 build-out or axis-1/-6 completion with a named
  progress-signal anchor and proven pilot template.

## Cycle observations

- TB-330's "migration walk surfaces latent bugs" pattern recurred on
  TB-333 — except this time the latent bug was IN THE BRIEFING
  (CLI arg-order on a verification bullet), not in the implementation.
  Pattern still valuable but slot shift: when proposing briefings that
  invoke ap2 subcommands, surface the canonical
  `ap2 --project <path> <verb>` arg order as a briefing-side check
  before queue-append. Promote to a Considered-and-deferred candidate
  if it recurs.

## Decisions needed from operator

(none — TB-336/337/338 are direct goal-anchored axis-5 tail + axis-1
schema completion + progress-signal-6 gate work; standard `ap2 approve
TB-N` review-gate path applies. TB-333 operator update already in
queue.)

## Proposals this cycle

- TB-336 (axis 5 — remaining ~8 cross-package + cross-component
  strays): web.py + goal.py + doctor.py + ideation.py:845 +
  components/attention.py:234 reads → `cfg.get_core_value` /
  `cfg.get_component_value`, completing the axis-5 migration tail
  outside the documented 12-factor exempt set.
- TB-337 (axis 1 completion — `[core.*]` ConfigKey schema): declare
  the 21 known core keys as ConfigKey entries paralleling TB-322's
  per-component schema; extend the registry validator + docs-drift
  gate to walk the core schema; close the asymmetry between component
  and core validation.
- TB-338 (progress signal 6 — exempt-list enforcement gate): add a
  test that walks `os.environ.get("AP2_*")` callsites and asserts
  each is either in `_KNOBS_STAYING_ENV_ONLY`, in the bootstrap path
  (config.py/env_reload.py), or has a TB-N fix; ensure
  FLAT_TO_SECTIONED ∩ _KNOBS_STAYING_ENV_ONLY = ∅.