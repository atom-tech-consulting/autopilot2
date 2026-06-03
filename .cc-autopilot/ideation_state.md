# Ideation State

_Last updated: 2026-06-03T23:14Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-372 (repoint codex backend off Cleanlab's wrong
`codex-sdk` + fabricated `CodexOptions/run_streamed` API onto OpenAI's real
`openai-codex` SDK; CodexAdapter rebuilt against `AsyncCodex().thread_start→
thread.turn→turn.stream()`, aac3da9, full suite 2954 passed), TB-371 (codex
optional extra, corrected by TB-372 to `openai-codex`), TB-370 (codex
ChatGPT-login OAuth in the auth gate), TB-369 (codex-handle daemon-start gate),
TB-368 (backend-aware SDK gate). All complete the AgentAdapter seam (Constraint
L578-587) + the pluggable-backend prerequisite for the downstream
OSS-distribution shape (Mission L18-20). No mission drift. No new Completes
since TB-372 (17:45Z); the operator's 17:10Z `update_goal` only corrected
goal.md's SDK references to `openai-codex` (operator_log 2026-06-03T17:10Z) —
same focus, no new scope, board empty.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: all 7 axes shipped and now implemented against the real
    `openai-codex` SDK. axis1 `TB-353`, axis2 `TB-354`, axis3 `TB-355`, axis4
    `TB-357`→repointed `TB-372`, axis5 `TB-358`+`TB-368`+`TB-369`+`TB-370`,
    axis6 all six dispatch migrations (`TB-360`/`TB-362`/`TB-363`/`TB-364`/
    `TB-365`/`TB-366` — last direct `sdk.query` gone + import-direction gate),
    axis7 `TB-359`→rebuilt `TB-372` + installable extra `TB-371`→`TB-372`;
    mixed-config e2e `TB-367`. All 5 goal.md Progress signals (L204-215) met
    hermetically; full suite 2954 passed at TB-372.
  - Gaps: the only remaining step is operator-owned and not auto-verifiable —
    the codex real-SDK smoke (`ap2/tests/smoke/test_codex_real_sdk.py`, rebuilt
    by `TB-372`) has never run LIVE against actual `openai-codex` creds. The
    hermetic stubs encode TB-372's understanding of `AsyncCodex().thread_start→
    thread.turn→turn.stream()`; only a credentialed live smoke can confirm that
    understanding matches the installed package. Needs `autopilot2[codex]` +
    OpenAI/codex creds + `AP2_REAL_SDK` on the 6h `real-sdk-smoke` cron — a
    `Manual:`-shaped check ideation cannot turn into a verifiable task (TB-122
    trap).

## Non-goal risk check

none. Nothing drifts toward a third backend or per-message routing (respects
L127-128); no agent prompt / tool policy / verification semantics changed
(respects L129-131). Zero proposals = zero scope-creep risk.

## Considered & deferred this cycle

- **codex real-SDK live smoke as a task**: NOT proposed — requires live
  OpenAI/codex creds + `AP2_REAL_SDK`; unverifiable unattended (forbidden
  `Manual:` bullet, TB-122 trap). Operator-owned; surfaced under Decisions
  needed.
- **`openai-codex` API-surface conformance test (install package, assert
  `AsyncCodex`/`thread_start`/`turn`/`stream` symbols exist)**: deferred again —
  substitutes a validation mechanism the operator did not ask for (operator
  named the LIVE smoke, operator_log 2026-06-02T15:55Z); a
  package-availability-gated test that *skips* when `openai-codex` isn't
  installed in the verifier env is a weak gate. Matches the rejection pattern
  below.
- **Manufacturing proposals to fill the 5 slots**: refused — goal.md L46-48
  forbids slot-filling; the focus delete-test (L198-202) refuses scaffolding
  with no migrated caller, and no un-migrated dispatch site remains (axis6
  complete).
- **Operator-rejection pattern (recurring)**: the two most-recent vetoes
  (TB-231 retry-on-malformed-JSON, TB-240 speculative file-path validator) both
  punish symptom-patches / speculative enumerated-case validators guarding
  unobserved failures. The deferred API-conformance idea above is exactly that
  shape, so declining it keeps this cycle clear of the pattern.

## Cycle observations

- (Carried, re-justified) "has a Complete TB per axis" / "is installable" are
  weaker done-signals than "the adapter's API assumptions match the real SDK" —
  the operator caught the whole-backend wrong-SDK defect (Cleanlab `codex-sdk` +
  fabricated `CodexOptions/run_streamed`) via TB-372 after the 06-02 cycle
  missed it. Carrying because the focus's only open gap is exactly the
  match-real-SDK check (the live smoke), so the lesson keeps me from
  re-declaring "done, no gaps" instead of "code-complete, live-validation
  pending."

## Decisions needed from operator

- Operator input required: run the codex real-SDK smoke LIVE to validate
  TB-372's repoint — `pip install 'autopilot2[codex]'`, provide OpenAI/codex
  creds (`OPENAI_API_KEY` or `~/.codex/auth.json`), then `AP2_REAL_SDK=1 uv run
  pytest ap2/tests/smoke/test_codex_real_sdk.py` (or let the 6h `real-sdk-smoke`
  cron fire with `AP2_REAL_SDK` set). A task agent cannot run a credentialed
  live backend unattended. Unblock-condition: a green live smoke confirms
  `AsyncCodex().thread_start→thread.turn→turn.stream()` matches the installed
  `openai-codex` API, closing the focus's last gap.
- Decision needed: with the codex focus code-complete and no `## Current focus`
  left in goal.md, should the operator arm the next focus — the downstream
  OSS-distribution shape (Mission L18-20) — via `ap2 update-goal`, or is the
  roadmap intentionally parked? Unblock-condition: a new focus heading gives the
  next ideation cycle workable gaps instead of another `roadmap_complete` skip.

## Proposals this cycle

Backlog is empty and the focus is code-complete; the only open gap is
operator-owned (live smoke). No proposals this cycle.