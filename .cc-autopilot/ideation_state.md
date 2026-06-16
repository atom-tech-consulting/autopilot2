I'll analyze this document and remove sentences asserting exhaustion or completion of the focus/axes.

Let me identify the sentences that should be deleted:

1. "This focus is the cash-out." - asserts the focus is essentially met/exhausted
2. "These finished the 'consolidate the operator manual into cross-runtime skills' focus, now marked Shipped" - asserts focus completion
3. "No focus-scoped TB-N is Complete, so status is necessarily `in-progress`." - conditional exhaustion claim (implying eventual completion state)

All other sentences are factual observations, gap descriptions, status lines, or structural elements that should be preserved.

---

# Ideation State

_Last updated: 2026-06-16T20:10Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-407 (repoint/remove stale howto.md refs post-retire),
TB-406 (retire ap2/howto.md as a file), TB-405 (carve components-enumeration into
ap2-observability — last domain carve), TB-403 (carve goal/focus authoring into
ap2-ideation-goals), TB-402 (carve failure-recovery skill). The operator has now opened the
**distribution** focus those arcs (component model TB-386/387/389/391/392, codex
TB-372, skills TB-397–407) were leading toward — recent work still serves the
Mission.

## Current focus assessment

- **Current focus: cut a public source-available distribution (PolyForm
  Noncommercial 1.0.0)** (goal.md L101)
  - Progress so far: none shipped — focus added today (operator_log update_goal
    2026-06-16T19:23/19:25/19:50/19:58Z; forced ideate 20:09Z). Structural
    prerequisites all Complete: component model (TB-386/387/388/389/391/392),
    structured config, codex backend (TB-372), skills consolidation
    (TB-405/406/407).
  - Gaps (both axes, fully greenfield):
    - axis 1 license: LICENSE still declares "All rights reserved" (LICENSE L1-3)
      and pyproject `license = { text = "All Rights Reserved" }` (pyproject L7),
      no classifiers — needs verbatim PolyForm Noncommercial 1.0.0 + license
      field/classifiers (no OSI).
    - axis 1 identity scrub: the named absolute-path leak survives at
      `ap2/json_extract.py:22` (`/Users/claude-agent/repos/post-train/...`);
      source needs a sweep + pyproject author/repo-URL coherence.
    - axis 1 sdist: setuptools build has no MANIFEST.in and package-data is only
      `ap2/*.md,*.yaml`, so the committed top-level `skills/` + docs ship in
      neither sdist nor wheel — the delete-test's "sdist omits skills/docs".
    - axis 1 README: README License section still says "All rights reserved — see
      LICENSE" (README L108-110); needs PolyForm + the `.cc-autopilot/`
      self-management note.
    - axis 2 posture+extras: the conservative default posture (Progress signal 3)
      and all-disabled-config green are not pinned as a release gate; extras
      ([codex]/base) are pinned by test_packaging.py (TB-371) but [dev]
      resolution + the [mattermost] decision are not.
  - Status: `in-progress`
  - Reasoning: focus opened today with zero Complete TB-Ns; every axis is
    unstarted and concretely actionable.

## Non-goal risk check

Distribution focus explicitly preserves "no behavior removal" (goal L142) and
keeps push/real-URL/goal-mutation operator-only (goal L120). This cycle's
proposals touch only LICENSE/pyproject/README/MANIFEST + a posture/extras test —
no feature deletion, no push, no goal mutation, no OSI relicensing (Non-goal
L295). The posture gate is goal-mandated (L167-172), not a speculative validator.
none.

## Considered & deferred this cycle

- **Live `uv sync --extra` network resolution smoke**: deferred into the extras
  task's Out-of-scope — network resolution is non-hermetic/flaky and the project
  packaging gate (TB-371 test_packaging.py) is deliberately hermetic; a live
  smoke is operator/CI, not a per-task verifier gate.
- **CHANGELOG / release-notes task**: deferred — lower leverage than the
  license/scrub/posture core and not named in either axis; revisit after the
  license + posture land.
- **codex session-isolation (the FROZE TB-408 idea)**: out of scope (codex
  backend already Shipped) and operator-blocked on the CODEX_HOME approach
  (operator_log 2026-06-15T21:32Z); not re-proposed.
- **Recurring operator-rejection pattern**: vetoes target out-of-sequence /
  duplicate-axis work (TB-384) and speculative enumerated-case validators/linters
  (TB-172/231/240). This cycle's 5 proposals are each a distinct deliverable
  within the two named axes, with no enumerated-case linter — clear of both.

## Cycle observations

- Both project insights sit at/near the 30-day staleness line (validator-judge-
  timeout 2026-05-18, test-suite-slowness 2026-05-17) and neither bears on the
  distribution focus; not re-measured this cycle and not escalated (no actionable
  operator decision attaches).
- pyproject uses the setuptools backend with package-data limited to
  `ap2/*.md,*.yaml`; the top-level `skills/` tree is not a Python package, so
  sdist inclusion needs MANIFEST.in specifically — grounds the sdist task's
  mechanism choice.

## Decisions needed from operator

None this cycle — the focus is freshly opened and the 5 proposals cover both
axes; advancing them is routine `ap2 approve`, surfaced mechanically by
`ap2 status` / the cron status-report.

## Proposals this cycle

5 proposals (full slot budget), mapped to the gaps above:
- TB-408 — license wiring (LICENSE PolyForm text + pyproject license/classifiers) [axis 1]
- TB-409 — source identity/path scrub + pyproject author/repo-URL coherence [axis 1]
- TB-410 — sdist ships skills/ + docs (MANIFEST.in + hermetic test) [axis 1]
- TB-411 — README accuracy (License section + .cc-autopilot note), blocked on TB-408 [axis 1]
- TB-412 — default-install release gate (posture test + extras pin) [axis 2]
(IDs predicted from current high-water mark; allocator assigns the actual TB-Ns.)