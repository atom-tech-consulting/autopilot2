# Ideation State

_Last updated: 2026-05-29T11:51:38Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 0B / 0P / 210C / 0F. All 4 Backlog items
from prior cycle landed: TB-333 (cross-package auto_unfreeze +
validator_judge, 3750f32, briefing-bullet fix from operator update
06:40:41Z), TB-336 (axis-5 tail, 3cf0173 — 8 reads in
web/goal/doctor/ideation/attention), TB-337 (core ConfigKey schema —
21 typed keys + did-you-mean hint, deecdca), TB-338 (12-factor
cut-line CI gate via AST walker + _PENDING_MIGRATION_KNOBS debt set,
2c629a4). Last 4 Completes all drove the structured-config focus — mission alignment intact, no drift into Non-goals (no API commitments, no env renames, no ap2-meta polish).

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Axes 1 + 6: TB-321 (parser+`Config.from_toml`+`Manifest.config_schema`),
      TB-325 (`CONFIG_TEMPLATE`+`test_every_config_key_documented`),
      TB-337 (CORE_CONFIG_SCHEMA closes axis-1 deferred validation).
    - Axes 2 + 3: TB-322 (6-component config_schema), TB-323
      (env-override + FLAT_TO_SECTIONED + `env_deprecated` events).
    - Axis 4: TB-324 (`ap2 config list/get/set/validate` CLI).
    - Axis 5 component bodies: TB-326..TB-331 (auto_approve /
      auto_unfreeze / attention / focus_advance / janitor /
      validator_judge — 6 of 6 closed; mattermost stays env-only per
      `_KNOBS_STAYING_ENV_ONLY` L207-212).
    - Axis 5 cross-package + core: TB-332 (auto_approve), TB-333
      (auto_unfreeze+validator_judge), TB-334 (`get_core_value`+11
      agent-runtime reads), TB-335 (ideation cluster), TB-336
      (axis-5 tail).
    - Progress-signal-6 enforcement: TB-338 (AST cut-line gate,
      FLAT_TO_SECTIONED ∩ _KNOBS_STAYING_ENV_ONLY = ∅ pinned).
  - Gaps:
    - **`_PENDING_MIGRATION_KNOBS` debt set non-empty (2 entries)** —
      AP2_VERIFY_JUDGE_EFFORT (verify.py L588) +
      AP2_STATUS_REPORT_EFFORT (status_report.py L2028). Both wrap a
      `cfg.get_core_value("agent_effort", default=…)` lookup with a
      per-site env override; TB-334 deferred them because the fallback
      value depends on a cfg read. FLAT_TO_SECTIONED already maps
      both (config_compat.py L105-106) but
      `CORE_CONFIG_SCHEMA` doesn't declare them (carve-out documented
      at core_config_schema.py L14-20). Closing this drains the debt
      set to empty + takes Progress signal 4 to ~100%.
  - Status: `in-progress`
  - Reasoning: 5 of 6 axes complete; axis-5 is 95% with only the
      documented 2-knob debt remaining. Post-fix, all 6 progress
      signals are enforced rather than aspirational.

## Non-goal risk check

None. The proposed residual is a read-path swap + schema-declaration
inside the focus charter (goal.md L384-389 explicitly green-lights
read-path swaps that move env-only knobs into schema). No new
operator-visible behavior, no env renames, no API surface change.

## Considered & deferred this cycle

- **Speculative axes-1..6 extensions** (e.g. typed enum schemas,
  per-component CLI verbs, schema diff/migration tooling). Operator
  rejection pattern (TB-185/184 parallel-surface erosion, TB-175
  premature aggregation, TB-231/240 symptom-patching/verifier
  whack-a-mole, TB-172 linter whack-a-mole) — each would dilute the
  focus charter past its scope. Deferred.
- **Mattermost knob migration into TOML.** Stays in
  `_KNOBS_STAYING_ENV_ONLY` per the 12-factor exempt-set rationale
  (Mattermost auth tokens + channel/team/bot identity); TB-338 now
  enforces it as a cut-line invariant. Not a gap.
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes still cluster around symptom-patching (TB-231/240),
  parallel surfaces (TB-185/184), and premature aggregation (TB-175);
  this cycle's proposal sits inside the named focus, follows the
  TB-326 pilot template verbatim, and adds zero new operator-facing
  surface.

## Cycle observations

- TB-336/337/338 all landed within a 90-minute window (10:47 / 11:20
  / 11:44Z), suggesting the axis-5 + axis-1-completion + cut-line-gate
  trio compressed cleanly once the migration template was stable.
  Useful signal that axis-5 follow-ups are very low-risk once the
  ConfigKey + FLAT_TO_SECTIONED mapping is in place — applies to
  this cycle's proposal too.

## Decisions needed from operator

(none — Backlog about to be populated; no escalation surface this
cycle. Next cycle will likely surface the focus-advancement question
once the 2-knob residual lands.)

## Proposals this cycle

1 proposal: TB-339 (drain `_PENDING_MIGRATION_KNOBS` to empty by
declaring `verify_judge_effort` + `status_report_effort` in
CORE_CONFIG_SCHEMA and swapping the 2 call-site env reads). Closes
goal.md L398 Progress signal 4 to ~100% migrated and goal.md
L401-403 Progress signal 6's "clearly minimal" intent to its
strictest reading.