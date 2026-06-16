# Ideation State

_Last updated: 2026-06-16T23:24Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-412 (default-install conservative-posture release
gate + dev/mattermost extras pins), TB-411 (README License → PolyForm + the
`.cc-autopilot/` self-management note), TB-410 (MANIFEST.in grafts skills/ + docs
into the sdist), TB-409 (json_extract.py absolute-path scrub + pyproject
author/repo-URL coherence), TB-408 (verbatim PolyForm Noncommercial 1.0.0 LICENSE
+ pyproject license/classifiers). All five are last cycle's distribution-focus
proposals, all landed Complete — recent work directly serves the Mission's
"future distribution shapes (including a public source-available cut)."

## Current focus assessment

- **Current focus: cut a public source-available distribution (PolyForm
  Noncommercial 1.0.0)** (goal.md L101)
  - Progress so far:
    - axis 1 (license + identity scrub): LICENSE is verbatim PolyForm + pyproject
      license/classifiers (TB-408), json_extract.py path leak scrubbed + pyproject
      author/repo-URL (TB-409), MANIFEST.in ships skills/ + docs in the sdist with
      a hermetic text-parse test (TB-410), README License + `.cc-autopilot` note
      (TB-411).
    - axis 2 (posture + extras): test_default_posture.py pins the conservative
      fresh-install posture + all-disabled config loads (TB-412); test_packaging.py
      pins codex/dev extras + the no-`[mattermost]`-extra decision (TB-371/TB-412).
  - Gaps (concrete, actionable):
    - **Progress signal 1 still VIOLATED**: `ap2/tests/test_json_extract_util.py:250`
      hard-codes `/Users/claude-agent/repos/post-train/.cc-autopilot/debug/...` — a
      sandbox-local absolute path baked into shipped source (`ap2.tests` is a
      declared package, pyproject L50). TB-409's sweep covered `ap2/*.py` (a
      NON-recursive glob) and missed the test tree. No regression gate pins "no
      sandbox-specific paths or identity baked into source."
    - **No regression gate pins Progress signal 2**: nothing fails if a future edit
      reintroduces "All Rights Reserved", drops the PolyForm LICENSE text, adds a
      `License :: OSI Approved` classifier, or regresses the README License section.
      The delete-test condition is true today but unprotected.
    - (deferred) sdist BUILD smoke: the axis-1 gate text-parses MANIFEST.in but
      never runs `python -m build --sdist`; the delete-test "the sdist omits the
      skills/docs" is proven by directive presence, not by asserting a built
      tarball's contents.
  - Status: `in-progress`
  - Reasoning: has Complete TB-Ns (408-412) with actionable gaps remaining.

## Non-goal risk check

This cycle's 2 proposals touch only test files + two regression-gate modules — no
behavior removal (Non-goal L285), no push / real-URL invention (focus split L120,
kept operator-only), no goal mutation, no OSI relicensing (Non-goal L295). The gates
pin goal.md-named delete-test conditions, not speculative policy. none.

## Considered & deferred this cycle

- **sdist build smoke** (`python -m build --sdist` + tarball-contents assertion):
  deferred again — `build` defaults to an isolated env (network fetch of
  setuptools/wheel) so it is a flaky/non-hermetic per-task verifier gate; the
  MANIFEST.in directive-presence test (TB-410) already covers the static surface.
  Better suited to operator/CI. Revisit only if a `--no-isolation` hermetic path is
  confirmed available in the dev venv.
- **CHANGELOG content / release-notes task**: deferred — not named in either axis
  and fails the focus delete-test (a clean checkout installs + is documented
  without a populated changelog); CHANGELOG.md already exists and is grafted into
  the sdist (MANIFEST.in L16).
- **Splitting operator task TB-413** (timed out once): NOT proposed — TB-413 is
  operator-added and operator-owned; it is in retry budget after one timeout, not
  retry-exhausted. Splitting another author's in-flight task is out of bounds (see
  Cycle observations).
- **Recurring operator-rejection pattern**: vetoes target speculative
  enumerated-case linters/validators (TB-172/231/240 — "wack-a-mole that only
  enumerate limited cases, generalize poorly") and duplicate/out-of-sequence axis
  work (TB-384). Both proposals this cycle pin ONE concrete invariant each, both
  literally named in goal.md's Progress signals / delete-test — the
  regression-pin shape the operator HAS repeatedly approved (TB-203, TB-205,
  TB-161/164), distinct from open-ended linters.

## Cycle observations

- TB-413 (operator-added config-simplification: collapse flat `AP2_*` override →
  config.toml-sole) TIMED OUT at 3600s on its first attempt (2026-06-16T22:22Z) —
  large multi-part scope (env allowlist + remove flat override + retire compat shim
  + migrate reads + update tests). Still in retry budget, operator-owned. If it
  retry-exhausts, the right disposition is a split — but that is the operator's
  call on their own task, not an ideation proposal.
- TB-412's briefing (mine) carried the redundant full-suite bullet
  `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke`; it false-failed
  once (exit 1, 2026-06-16T22:01Z) before re-verifying green — re-confirms the
  operator's 2026-06-11 rule to DROP that bullet. This cycle's briefings use only
  targeted pytest bullets.
- TB-409's commit summary said it "swept shipped ap2/*.py" — that glob is
  non-recursive, so `ap2/tests/*.py` was never scanned; grounds proposal #1's
  recursive gate over the whole shipped distribution surface.
- Both project insights are stale (>30d: validator-judge-timeout 2026-05-18,
  test-suite-slowness 2026-05-17) and neither bears on the distribution focus; not
  re-measured, no actionable operator decision attaches.

## Decisions needed from operator

None this cycle — both proposals advance via routine `ap2 approve`, surfaced
mechanically by `ap2 status` / the cron status-report. TB-413's timeout does not
yet require an operator action (first attempt, in retry budget).

## Proposals this cycle

2 of 3 slots used (the 3rd candidate, the sdist build smoke, is deferred on
hermetic-build grounds above):
- TB-415 — scrub the residual sandbox-path leak in `ap2/tests/test_json_extract_util.py`
  + add a regression gate against sandbox-local absolute paths in shipped source [axis 1, Progress signal 1]
- TB-416 — license/metadata coherence regression gate (LICENSE verbatim PolyForm +
  pyproject license + no OSI classifier + README accuracy) [axis 1, Progress signal 2]
(IDs predicted from the current high-water mark; allocator assigns the actual TB-Ns.)