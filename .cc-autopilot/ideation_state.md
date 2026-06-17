# Ideation State

_Last updated: 2026-06-17T06:36Z by ideation cron_

## Mission alignment

5 most recent clean Completes — TB-413 (config.toml is now the sole source for
behavioral tunables; env restricted to a secrets + deployment-identity allowlist),
TB-412 (release-gate the default install: conservative-posture test + install-extras
pin), TB-411 (README License → PolyForm + the `.cc-autopilot/` self-management note),
TB-410 (MANIFEST.in grafts skills/ + docs into the sdist), TB-409 (json_extract.py
path scrub + pyproject author/repo-URL coherence). Recent output directly serves the
Mission's "future distribution shapes (including a public source-available cut)" and
the structured-config durable criteria. NOTE: TB-413 — which last cycle was Frozen
(retry-exhausted) with an undiagnosed 1800s verifier-timeout risk sitting in HEAD —
was operator-unfrozen (2026-06-17T05:05:56Z), re-run, and landed clean at HEAD
edcc68f. Root cause is now known and closed (see Current focus > Gaps).

## Current focus assessment

- **Current focus: cut a public source-available distribution (PolyForm
  Noncommercial 1.0.0)** (goal.md L101)
  - Progress so far:
    - axis 1 (license wiring + identity scrub): LICENSE verbatim PolyForm + pyproject
      license/classifiers (TB-408); json_extract.py path leak + pyproject
      author/repo-URL placeholders (TB-409); MANIFEST.in ships skills/ + docs in the
      sdist with a hermetic parse test (TB-410); README License + the `.cc-autopilot/`
      note (TB-411); env→config.toml restriction (TB-413). The two remaining concrete
      axis-1 regression-pin gaps are proposed and pending operator review: TB-415
      (test-tree path leak + recursive sandbox-path gate, Progress signal 1) and TB-416
      (license/metadata coherence gate, Progress signal 2).
    - axis 2 (posture + extras): conservative fresh-install posture + all-disabled
      config load pinned, dev/codex extras + the no-`[mattermost]`-extra decision
      pinned (TB-412).
  - Gaps:
    - The two concrete regression-pin gaps (Progress signals 1 & 2) are covered by
      TB-415 + TB-416 (pending review); the env-file-template doc (TB-414) is now
      UNBLOCKED — its `@blocked:TB-413` blocker is satisfied since TB-413 is Complete.
    - RESOLVED (was last cycle's only new gap): TB-413's attempt-3 project-wide verify
      timed out at 1800s vs TB-412's ~285s. Root cause is now known and closed — a
      test-side conftest shield still used the removed flat `AP2_VALIDATOR_JUDGE_DISABLED`
      name, so the dep-coherence judge stayed enabled and fired real SDK calls across
      the unit suite; edcc68f migrates the shields to sectioned
      `AP2_CORE_*`/`AP2_COMPONENTS_*` names. NOT a genuine suite hang — a real-but-
      test-only regression. The project-wide verify gate that runs after every task is
      healthy again, so TB-415/TB-416 (once approved) and downstream work are unblocked.
    - (deferred) sdist BUILD smoke + a real clean-room extras-resolution install test —
      both non-hermetic (network / isolated build env); unsuitable as per-task verifier
      bullets, better for operator/CI.
    - Remaining steps include operator-only work (set the real repo URL/author, push).
  - Status: `in-progress`

## Non-goal risk check

TB-413 (operator-added, `--skip-goal-alignment`) removed the flat `AP2_*` override
path + retired the `config_compat` shim — cutting against the now-Shipped
structured-config Done-when bullet "existing `AP2_*` env names continue to work as
overrides for one full release cycle" (goal.md L92-95). It is operator-directed and
operator-owned, so it is not ideation drift; but goal.md's text now contradicts
shipped reality, which I re-flag here every cycle. Surfaced under Decisions needed so
the contradiction can be reconciled once rather than re-discovered.

## Considered & deferred this cycle

- **Any new greenfield proposal**: NOT proposed — proposal slots = 1, and the Backlog
  already holds ≥1 workable item (TB-414 now unblocked + TB-417, both operator-filed),
  plus TB-415/TB-416 pending review covering the two concrete axis-1 Progress signals.
- **sdist build smoke / extras-resolution install test**: deferred again —
  non-hermetic (network, isolated build env); cannot confirm a `--no-isolation`
  hermetic path without Bash; better suited to operator/CI. TB-410's MANIFEST.in
  directive-presence test already covers the static sdist surface.
- **Broader sandbox-identity-string linter** (usernames/emails/hostnames beyond
  absolute paths): NOT proposed — matches the operator's repeatedly-vetoed
  wack-a-mole pattern (TB-172/231/240: "enumerate limited cases, generalize poorly").
  TB-415's recursive absolute-path gate already pins the one concrete invariant named
  in Progress signal 1.
- **Recurring operator-rejection pattern**: vetoes target speculative enumerated-case
  linters (TB-172/231/240) and duplicate/out-of-sequence axis work (TB-384, the most
  recent rejection). Neither shape is in this cycle's candidate set.

## Cycle observations

- TB-413's 1800s verifier timeout (last cycle's open "genuine hang vs environmental
  fluke?" question) resolved as a third option: a real-but-test-only regression — a
  conftest shield referencing a removed flat env name left the dep-coherence judge
  firing real SDK calls suite-wide. Closed at edcc68f; the post-every-task verify gate
  is healthy. Kept because it retires the prior cycle's carried concern and confirms
  the distribution gate is sound.
- Both project insights remain >30d stale (validator-judge-timeout 2026-05-18,
  test-suite-slowness 2026-05-17). Neither blocks ranking this cycle — the
  distribution focus is packaging/defaults/hygiene, not metric-driven — and the
  TB-413 timeout that briefly made test-suite-slowness look fresh was a config-name
  bug, not genuine suite slowness, so the insight is not actually relevant.

## Decisions needed from operator

- Decision needed: TB-413 (operator-filed `--skip-goal-alignment`) removed the flat
  `AP2_*` override path and retired the `config_compat` shim, which contradicts the
  now-Shipped structured-config Done-when bullet at goal.md L92-95 ("existing `AP2_*`
  env names continue to work as overrides for one full release cycle"). Please edit
  goal.md to mark that bullet superseded by TB-413 (or delete it) via
  `ap2 update-goal`. Unblock condition: until reconciled, ideation's per-cycle
  non-goal-risk check keeps re-flagging this as an apparent regression each cycle;
  the edit makes the check clean and removes the recurring false signal.

## Proposals this cycle

Backlog already populated; no proposals this cycle. The two concrete remaining axis-1
gaps (Progress signals 1 & 2) are covered by TB-415 + TB-416 (pending review); TB-414
(now unblocked by TB-413's completion) + TB-417 are workable operator-filed items
already in Backlog; the prior cycle's only new gap (TB-413's HEAD suite-hang risk) is
resolved at edcc68f; remaining candidates are non-hermetic (deferred) or match a
vetoed wack-a-mole pattern.