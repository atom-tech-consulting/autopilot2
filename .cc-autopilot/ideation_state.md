# Ideation State

_Last updated: 2026-05-29T09:49:26Z by ideation cron_

## Mission alignment

Cycle entry: board 0A / 0R / 4B / 0P / 206C / 0F. The 3 proposals from
the 07:13Z cycle (TB-336/337/338) all landed in Backlog and auto-approved
(`auto-approve: 24h: 20 approved`). TB-335 (core ideation cluster from
the cycle before) closed clean at 09:49Z (df35bc1) — migrated 4 ideation
knobs via `cfg.get_core_value` + Config-kwarg-+-TypeError-guard helpers,
59-test regression pin, suite 2718 passed. TB-333's verification-bullet
fix is queued (operator `update` op f5c8421e at 06:40:41Z) — drains next
tick, re-dispatches automatically; no ideation fix-task needed. Backlog
now carries 4 workable axis-5 / axis-1 / progress-signal-6 items, all on
the structured-config focus.

## Current focus assessment

- **Current focus: structured config (env → TOML)**
  - Progress so far:
    - Axes 1 (TB-321), 2 (TB-323), 3 (TB-322), 4 (TB-324), 6 (TB-325)
      shipped.
    - Axis 5 component bodies: TB-326..331 closed 6 of 7 components
      (auto_approve, auto_unfreeze, attention, focus_advance, janitor,
      validator_judge); mattermost stays env-only per
      `_KNOBS_STAYING_ENV_ONLY` (config_compat.py L207-212).
    - Axis 5 cross-package: TB-332 (auto_approve, f1a6176) + TB-334
      (core agent-runtime + `get_core_value` helper, d4404ef) +
      TB-335 (core ideation cluster, df35bc1).
  - Gaps (all covered by current Backlog):
    - **Cross-package strays (~8 reads)** — covered by **TB-336**:
      web.py L214/L226, goal.py L419/L446, doctor.py L374/L375,
      ideation.py L845 (TB-334 straggler), components/attention/
      L234 (cross-COMPONENT auto_approve read).
    - **`[core.*]` ConfigKey schema missing** — covered by **TB-337**:
      Config.core_config stays an untyped `dict[str, Any]` after
      TB-334; asymmetric with axis-3 per-component
      `Manifest.config_schema`. 21 known core keys via
      `FLAT_TO_SECTIONED`; howto.md L2376-2379 flags as deferred.
    - **`_KNOBS_STAYING_ENV_ONLY` cut-line has no enforcement test**
      — covered by **TB-338**: comment block at config_compat.py
      L193-212 documents the exempt set but no CI gate keeps new
      `os.environ.get("AP2_*")` reads outside it; parallels TB-305
      (env-knob docs-drift) and TB-325 (config-key docs-drift).
    - **TB-333 cross-package auto_unfreeze + validator_judge tail**
      — covered by **TB-333** (Backlog, blockers TB-327+TB-331 both
      complete; operator update queued to fix the last
      verification bullet's CLI arg order).
  - Status: `in-progress`

## Non-goal risk check

None. All 4 Backlog items are read-path swap or test-gate addition that
goal.md L384-389 explicitly green-lights. No env renames; no API
stability commitments; no behavior changes.

## Considered & deferred this cycle

- **Any new axis-5 / axis-1 / progress-signal-6 proposal**. Backlog
  already covers all 3 named gaps + the TB-333 cross-package tail.
- **TB-333 fix-task remediation**. The failed last bullet
  (`ap2 doctor --project .` — wrong CLI arg order; correct is
  `ap2 --project . doctor`) is already in flight via operator's
  queued `update` op (events.jsonl 06:40:41Z, uuid f5c8421e).
- **Mattermost component-body migration**. All 5 MM knobs (channels/bot_user_id/mention/team_id/report_channel)
  stay verbatim in `_KNOBS_STAYING_ENV_ONLY` at config_compat.py L207-212.
- **Howto.md `## Configuration knobs` flat-list deprecation**.
  Deferred per L2391-2395 ("flat surface stays
  read-supported indefinitely; TOML surface is forward-canonical").
- **Recurring rejection-pattern check (carried, re-justified)**:
  operator vetoes TB-185/184 (utility unaligned / parallel surface
  eroding goal.md authority), TB-175 (premature aggregation),
  TB-231/240 (symptom-patching / verifier whack-a-mole), TB-172
  (linter whack-a-mole).

## Cycle observations

- TB-330's "migration walk surfaces latent bugs" pattern recurred on
  TB-333 — except this time the latent bug was IN THE BRIEFING
  (CLI arg-order on the last verification bullet), not in the
  implementation. When authoring briefings that
  invoke ap2 subcommands, prefer `ap2 --project <path> <verb>`
  arg order.

## Decisions needed from operator

(none — Backlog covers every named gap; TB-333 operator update
already drained-pending; no escalation surface this cycle.)

## Proposals this cycle

Backlog already populated. 4 workable items
(TB-333 / TB-336 / TB-337 / TB-338) cover all gaps identified above.