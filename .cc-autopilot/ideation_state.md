# Ideation State

_Last updated: 2026-06-17T04:10Z by ideation cron_

## Mission alignment

5 most recent clean Completes — TB-412 (default-install conservative-posture
release gate + dev/codex extras pin), TB-411 (README License → PolyForm + the
`.cc-autopilot/` self-management note), TB-410 (MANIFEST.in grafts skills/ + docs
into the sdist), TB-409 (json_extract.py path scrub + pyproject author/repo-URL
coherence), TB-408 (verbatim PolyForm Noncommercial 1.0.0 LICENSE + pyproject
license/classifiers). Recent output directly serves the Mission's "future distribution shapes
(including a public source-available cut)." (TB-413 did NOT land clean — it
retry-exhausted → Frozen; treated under failure review, not here.)

## Current focus assessment

- **Current focus: cut a public source-available distribution (PolyForm
  Noncommercial 1.0.0)** (goal.md L101)
  - Progress so far:
    - axis 1 (license + identity scrub): LICENSE verbatim PolyForm + pyproject
      license/classifiers (TB-408); json_extract.py path leak + pyproject
      author/repo-URL placeholders (TB-409); MANIFEST.in ships skills/ + docs in
      the sdist with a hermetic parse test (TB-410); README License + the
      `.cc-autopilot/` note (TB-411). The two remaining concrete axis-1 gaps were
      proposed and are pending operator review: TB-415 (test-tree path leak +
      recursive sandbox-path gate, Progress signal 1) and TB-416 (license/metadata
      coherence gate, Progress signal 2).
    - axis 2 (posture + extras): conservative fresh-install posture + all-disabled
      config load pinned, dev/codex extras + the no-`[mattermost]`-extra decision
      pinned (TB-412).
  - Gaps:
    - The two concrete regression-pin gaps (Progress signals 1 & 2) are already
      covered by TB-415 + TB-416, both awaiting `ap2 approve`.
    - NEW this cycle: TB-413's commit 829f228 is in HEAD (git: 829f228 then
      `0041af6 state: TB-413 → Frozen`). Its attempt-3 project-wide verify
      (`uv run --extra dev pytest ... --ignore=smoke`) timed out at 1800s vs
      TB-412's 285s for the IDENTICAL command. If that is a genuine suite hang
      (not an environmental fluke during the two back-to-back 3600s agent
      timeouts), it sits in HEAD and the project-wide verify gate that runs after
      EVERY task will time out — which would block TB-415/TB-416 from ever passing
      verification once approved, and all downstream distribution work. This is
      operator-owned (see Decisions needed); ideation cannot anchor a fix honestly.
    - (deferred) sdist BUILD smoke + a real clean-room extras-resolution install
      test — both non-hermetic (network / isolated build env); unsuitable as
      per-task verifier bullets, better for operator/CI.
    - Remaining steps include operator-only work (set the real repo URL/author, push).
  - Status: `in-progress`

## Non-goal risk check

TB-413 (operator-added) removed the flat `AP2_*` override path + retired the
`config_compat` shim — cutting against the structured-config Done-when bullet
"existing `AP2_*` env names continue to work as overrides for one full release
cycle" (goal.md L92-95). But it is operator-directed (filed `--skip-goal-alignment`)
and operator-owned, so it is not ideation drift.

## Considered & deferred this cycle

- **Auto-propose a fix for TB-413's suite-hang regression**: NOT proposed — the
  root cause is undiagnosed and 829f228 is operator-owned + Frozen. A parallel
  fix task would collide with the operator's own triage (revert vs. re-scope vs.
  investigate); the follow-up protocol says surface, don't auto-propose, for an
  operator-owned frozen task with no goal anchor. Surfaced under Decisions needed.
- **sdist build smoke / extras-resolution install test**: deferred again —
  non-hermetic (network, isolated build env); cannot confirm a `--no-isolation`
  hermetic path without Bash; better suited to operator/CI. TB-410's MANIFEST.in
  directive-presence test already covers the static sdist surface.
- **Broader sandbox-identity-string linter** (usernames/emails/hostnames beyond
  absolute paths): NOT proposed — matches the operator's repeatedly-vetoed
  wack-a-mole pattern (TB-172/231/240: "enumerate limited cases, generalize
  poorly"). TB-415's recursive absolute-path gate already pins the one concrete
  invariant named in Progress signal 1.
- **Recurring operator-rejection pattern**: vetoes target speculative
  enumerated-case linters (TB-172/231/240) and duplicate/out-of-sequence axis work
  (TB-384).

## Cycle observations

- git confirms 829f228 (TB-413 work) is in HEAD, immediately under the Frozen
  state commit — so the suite-timing regression, if real, ships at HEAD now. The
  two earlier TB-413 failures were AGENT 3600s wall-clock timeouts; only attempt 3
  was a verify-command 1800s timeout. The back-to-back agent timeouts mean the
  machine may have been thrashing, so a one-off slowdown can't be ruled out
  without re-running the command — hence the operator-diagnostic ask below.
- Both project insights are stale (>30d: validator-judge-timeout 2026-05-18,
  test-suite-slowness 2026-05-17). The test-suite-slowness insight is freshly
  relevant to TB-413's 1800s suite timeout but has no tldr and was not re-measured
  — carried because it directly informs whether the HEAD regression is real, but
  not actionable as grounding until the operator's TB-413 triage resolves it.

## Decisions needed from operator

- Decision needed: TB-413 is retry-exhausted (Frozen) and its commit 829f228 is in
  HEAD; attempt 3's project-wide verify timed out at 1800s vs the usual ~285s for
  the identical command. Please run
  `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` at HEAD to
  confirm whether 829f228's config-loader change genuinely hangs the suite or the
  1800s was an environmental fluke during the two back-to-back 3600s agent
  timeouts. If it genuinely hangs: revert/re-scope 829f228 before `ap2 unfreeze
  TB-413`, because the project-wide verify gate runs after every task and a real
  hang will time out TB-415/TB-416 (once approved) and all downstream distribution
  work. Unblock condition: until this is decided, TB-414 (`@blocked:TB-413`) stays
  stranded and ideation cannot anchor a remediation (the task is operator-owned
  with no goal anchor).

## Proposals this cycle

Backlog already populated; no proposals this cycle. The two concrete remaining
axis-1 gaps (Progress signals 1 & 2) are covered by TB-415 + TB-416, both pending
operator review; the only new gap (TB-413's HEAD suite-hang risk) is operator-owned
triage, not an ideation-anchorable proposal; remaining candidates are non-hermetic
(deferred) or match a vetoed wack-a-mole pattern.