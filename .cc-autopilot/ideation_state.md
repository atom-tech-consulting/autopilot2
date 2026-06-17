# Ideation State

_Last updated: 2026-06-17T02:05Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-412 (default-install conservative-posture release
gate + dev/codex extras pin), TB-411 (README License → PolyForm + the
`.cc-autopilot/` self-management note), TB-410 (MANIFEST.in grafts skills/ + docs
into the sdist), TB-409 (json_extract.py path scrub + pyproject author/repo-URL
coherence), TB-408 (verbatim PolyForm Noncommercial 1.0.0 LICENSE + pyproject
license/classifiers). All five are distribution-focus work and land Complete —
recent output directly serves the Mission's "future distribution shapes
(including a public source-available cut)."

## Current focus assessment

- **Current focus: cut a public source-available distribution (PolyForm
  Noncommercial 1.0.0)** (goal.md L101)
  - Progress so far:
    - axis 1 (license + identity scrub): LICENSE verbatim PolyForm + pyproject
      license/classifiers (TB-408); json_extract.py path leak + pyproject
      author/repo-URL placeholders (TB-409); MANIFEST.in ships skills/ + docs in
      the sdist with a hermetic parse test (TB-410); README License + the
      `.cc-autopilot/` note (TB-411). The two remaining concrete axis-1 gaps were
      PROPOSED last cycle and are pending operator review: TB-415 (test-tree path
      leak + recursive sandbox-path gate, Progress signal 1) and TB-416
      (license/metadata coherence gate, Progress signal 2).
    - axis 2 (posture + extras): conservative fresh-install posture + all-disabled
      config load pinned, dev/codex extras + the no-`[mattermost]`-extra decision
      pinned (TB-412).
  - Gaps:
    - The two concrete regression-pin gaps (Progress signals 1 & 2) are already
      covered by TB-415 + TB-416, both awaiting `ap2 approve`.
    - (deferred) sdist BUILD smoke + a real clean-room extras-resolution install
      test — both non-hermetic (network / isolated build env); unsuitable as
      per-task verifier bullets, better for operator/CI.
    - Remaining steps include operator-only work (set the real repo URL/author, push).
  - Status: `in-progress`
  - Reasoning: has Complete TB-Ns (408-412) and the two remaining concrete gaps
    are already proposed + awaiting review, not unaddressed.

## Non-goal risk check

TB-413 (operator-added) removed the flat `AP2_*` override path + retired the
`config_compat` shim — which cuts against the structured-config Done-when bullet
"existing `AP2_*` env names continue to work as overrides for one full release
cycle" (goal.md L92-95). But it is operator-directed (filed `--skip-goal-alignment`)
and operator-owned, so it is not ideation drift. None of this cycle's reasoning
touches behavior removal, push, real-URL invention, or goal mutation. none (re:
ideation's own actions).

## Considered & deferred this cycle

- **sdist build smoke / extras-resolution install test**: deferred again —
  non-hermetic (network, isolated build env); cannot confirm a `--no-isolation`
  hermetic path without Bash; better suited to operator/CI. TB-410's MANIFEST.in
  directive-presence test already covers the static sdist surface.
- **Broader sandbox-identity-string linter** (usernames/emails/hostnames beyond
  absolute paths): NOT proposed — matches the operator's repeatedly-vetoed
  wack-a-mole pattern (TB-172/231/240: "enumerate limited cases, generalize
  poorly"). TB-415's recursive absolute-path gate already pins the one concrete
  invariant named in Progress signal 1.
- **README quickstart/install polish**: deferred — the focus ships without it; goal.md requires only an accurate License section +
  `.cc-autopilot/` note, both delivered by TB-411.
- **Splitting/remediating TB-413** (retry-exhausted → Frozen): NOT auto-proposed —
  operator-owned (`--skip-goal-alignment`, no goal anchor), and a split has no
  honest current-focus anchor. Surfaced to operator instead (see Decisions needed).
- **Recurring operator-rejection pattern**: vetoes target speculative
  enumerated-case linters (TB-172/231/240) and duplicate/out-of-sequence axis work
  (TB-384). 0 proposals this cycle so nothing to rank against it; carried forward
  as the bar for next cycle.

## Cycle observations

- TB-413 retry-exhausted → Frozen; commit 829f228 landed on attempt 3, but the
  project-wide verify (`uv run --extra dev pytest ... --ignore=smoke`) timed out
  (exit=None, 1800s) vs TB-412's 285s for the IDENTICAL command. The ~6× blowup
  signals the config-loader change introduced a suite hang/regression, not merely
  oversized scope (two prior agent-wall-clock timeouts at 3600s each compounded it).
- TB-414 (`@blocked:TB-413`) is transitively stranded while TB-413 is Frozen.
- Both project insights are stale (>30d: validator-judge-timeout 2026-05-18,
  test-suite-slowness 2026-05-17). The test-suite-slowness insight is freshly
  relevant given TB-413's 1800s suite timeout, but it has no tldr and was not
  re-measured — not actionable without grounding.

## Decisions needed from operator

- Decision needed: TB-413 is retry-exhausted (Frozen) with commit 829f228 on disk,
  but the full test suite now times out (1800s vs the usual ~285s), indicating its
  config-loader change introduced a regression. Recommend the operator either
  re-scope/split it into smaller slices (allowlist definition vs flat-override
  removal vs compat-shim retirement vs read migration) OR investigate the suite
  hang in 829f228 before `ap2 unfreeze TB-413`. Unblock condition: until decided,
  TB-414 (`@blocked:TB-413`) stays stranded and ideation cannot anchor a
  remediation (the task is operator-owned with no goal anchor).

## Proposals this cycle

Backlog already populated; no proposals this cycle. The two concrete remaining
axis-1 gaps (Progress signals 1 & 2) are already covered by TB-415 + TB-416, both
pending operator review; remaining candidates are non-hermetic (deferred) or match
a vetoed wack-a-mole pattern.