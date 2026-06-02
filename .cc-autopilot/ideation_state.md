# Ideation State

_Last updated: 2026-06-02T15:59Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-370 (codex ChatGPT-login OAuth accepted in the
backend-aware auth gate, bca1fef), TB-369 (codex-handle daemon-start gate,
f8824c3), TB-368 (backend-aware SDK-availability gate, e3d1faa), TB-367
(mixed `ideation=claude`/`task=codex` e2e, e927df5), TB-359 (parity suite +
gated codex real-SDK smoke). No mission drift: each completes the AgentAdapter
seam — the "pluggable agent backend" Constraint (goal.md L578-587) and the
structural prerequisite for the downstream OSS-distribution shape (Mission
L18-20). NEW this cycle: operator re-engaged (operator_log 2026-06-02T15:55Z)
with concrete direction — the codex backend is code-complete but not yet
runnable live — then forced an ideate (15:58Z), breaking the 6-cycle
`roadmap_complete` ideation halt (18:28Z 06-01 → 14:31Z 06-02).

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: all 7 axes shipped as code — axis1 `TB-353`, axis2
    `TB-354`, axis3 `TB-355`, axis4 `TB-357` (CodexAdapter), axis5 `TB-358` +
    `TB-368` + `TB-369` + `TB-370` (per-kind `[agent_backends]` map + all three
    backend-aware daemon-start gates), axis6 all six dispatch migrations
    (`TB-360`/`TB-362`/`TB-363`/`TB-364`/`TB-365`/`TB-366`), axis7 `TB-359`
    (parity + gated codex smoke); mixed-config e2e `TB-367`.
  - Gaps: the codex backend is code-complete but NOT runnable live (operator,
    operator_log 2026-06-02T15:55Z). `codex_sdk` (distribution `codex-sdk`) is
    nowhere declared as an installable dependency — `pyproject.toml`'s
    `[project.optional-dependencies]` carries only `dev`; `claude-agent-sdk` is
    a hard dep but the codex handle is not. So the lazy `import codex_sdk` in
    `load_codex_sdk` (`TB-369`), the daemon-start codex-handle gate that calls
    it (`TB-369`), and the smoke's `pytest.importorskip("codex_sdk")` (`TB-359`)
    are all dead on every environment — there is no supported way to install the
    second backend. Until the extra exists, axis-7's real-SDK smoke and the
    mixed-config Progress signal are never exercised against the real codex
    backend, so the focus delete-test ("a second backend actually drives an
    agent kind") is unmet despite all axes being coded.
  - Status: `in-progress`
  - Reasoning: operator named a concrete, code-complete-but-unshipped gap
    (installability) and forced ideation against it — not exhaustion.

## Non-goal risk check

none. Nothing drifts into a third backend or per-message routing (respects
goal.md L127-128); the packaging proposal changes no agent prompt / tool policy
/ verification semantics (respects L129-131).

## Considered & deferred this cycle

- **"Run the codex real-SDK smoke live in the daemon"** (the second half of the
  operator's 15:55Z direction): NOT proposed as a task — it requires real
  OpenAI/codex creds + `AP2_REAL_SDK` on the 6h `real-sdk-smoke` cron and cannot
  be verified unattended (would be a forbidden `Manual:` bullet, the TB-122
  trap). Operator-owned; surfaced under Decisions needed instead.
- **Prior cycle's deferred "Codex real-SDK smoke promotion / hardening"**: now
  SUPERSEDED by explicit operator direction — it is no longer speculative.
- **Manufacturing 3-4 more proposals to fill the 5 slots**: deferred — goal.md
  L46-48 forbids slot-filling and the focus delete-test (L198-202) refuses
  scaffolding without a migrated/runnable caller.
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a) symptom-patch
  without root cause (TB-231) and (b) speculative enumerated-case validators
  guarding unobserved failures (TB-240/172/175). This cycle's single proposal is
  neither — it is operator-requested packaging that makes a fully-coded backend
  installable. Pattern re-noted so future cycles stay clear.

## Cycle observations

- The "code-complete" lens missed installability: prior cycles marked the focus
  `exhausted-needs-operator` because every axis had a Complete TB-N, but a
  backend whose handle has no install path can never run (`importorskip` always
  skips; the TB-369 gate can never pass). Future cycles should treat
  "installable + runnable", not "has a TB", as the gap dimension for a backend.

## Decisions needed from operator

- Operator input required: after TB-371 lands and `codex-sdk` is installed in the
  daemon's environment, run the codex real-SDK smoke live —
  `AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/test_codex_real_sdk.py` (or let
  the 6h `real-sdk-smoke` cron fire with `AP2_REAL_SDK` set) — to validate
  CodexAdapter end-to-end against the real `codex_sdk`. This is the operator-owned
  half of the 2026-06-02T15:55Z direction (a task agent cannot run a live,
  credentialed backend unattended). Unblock-condition: a green live smoke
  confirms axis-7 against the real backend.

## Proposals this cycle

- TB-371 — Declare the codex backend as an installable optional extra
  (`autopilot2[codex]`) in `pyproject.toml`, document it in howto, align the
  daemon-start gate hint, and pin it with a hermetic packaging test. Addresses
  the installability gap above.