# Ideation State

_Last updated: 2026-06-01T08:14:00Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-368 (backend-aware daemon-start SDK-availability
gate; pure-codex map skips the Claude SDK import, e3d1faa), TB-367 (mixed-config
`ideation=claude`/`task=codex` e2e through the adapter, e927df5), TB-366
(residual `claude_agent_sdk` imports relocated behind `ap2/adapters/` +
import-direction gate, f2edcf4), TB-365 (`_run_control_agent` adapter-routed,
cbcc137), TB-364 (`run_task` adapter-routed, 18107a9). No mission drift: every
ship moves a dispatch concept (or a daemon-start gate) behind the AgentAdapter
seam — the literal codex-support focus.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: axis1 `TB-353` (AgentAdapter ABC + ClaudeCodeAdapter),
    axis2 `TB-354` (normalized AgentOptions/AgentResult/usage), axis3 `TB-355`
    (MCP tools through adapter), axis4 `TB-357` (CodexAdapter against the codex
    CLI), axis5 `TB-358` (per-kind `[agent_backends]` map + backend-aware
    *credential* gate) and `TB-368` (backend-aware Claude-SDK-*availability*
    gate), axis6 all six dispatch-site migrations (`TB-360` scrub, `TB-362`
    verifier-judge, `TB-363` validator+janitor judge, `TB-364` run_task, `TB-365`
    _run_control_agent), axis7 `TB-359` (parity suite + gated codex real-SDK
    smoke).
  - Gaps: ONE concrete edge case remains, the codex MIRROR of `TB-368`. The
    daemon-start gate `daemon._load_claude_sdk_if_referenced(cfg)` only probes
    the CLAUDE handle: a pure-/mixed-codex map passes the credential gate
    (`OPENAI_API_KEY`, `cli_daemon.py` L129) and the SDK gate (claude not
    referenced → returns `None`, `test_tb368...py` L45-60), but NOTHING verifies
    the codex handle (`codex_sdk`, lazily imported in `CodexAdapter._get_codex()`
    at `adapters/codex.py` L248-252) is importable. A codex deployment with
    `OPENAI_API_KEY` set but `codex_sdk` not installed starts cleanly, then
    hard-fails with a cryptic `ImportError` at FIRST dispatch — axis-5's own
    delete-test failure mode ("codex hard-fails the OAuth-only gate") in mirror
    form. Symmetric availability check is the close.
  - Status: `in-progress`
  - Reasoning: one concrete, workable, fresh gap remains (codex-side daemon-start
    availability gate, the direct symmetric follow-up to the just-shipped
    `TB-368`); a real focus-paying next step exists.

## Non-goal risk check

none. The proposal stays inside the focus: it completes axis-5's daemon-start
gate symmetrically (mirrors `TB-368`/`TB-358`) without changing any agent's
prompt/tool policy/verification semantics (respects L129-131) and adds no third
backend or per-message routing (respects L127-128).

## Considered & deferred this cycle

- **Greenfield ap2-meta polish to fill the 4 unused slots**: deferred.
  Manufacturing 4 more proposals to fill slots drifts into the
  "ap2-meta polish / scope creep" goal.md L46-48 forbids and fails the focus
  delete-test (L198-202). One real gap → one proposal.
- **Abstract the tool-DEFINITION mechanism (`@tool` schema) for Codex**: still
  deferred (carried): `TB-355`/`TB-357` cover tool registration through the
  adapter; genuinely abstracting tool-definition for a non-Claude backend has no
  concrete caller yet. Re-propose only when a real Codex tool-definition need
  surfaces.
- **Codex real-SDK smoke promotion / hardening**: deferred. `TB-359` already
  ships the gated codex real-SDK smoke on the 6h cron; no observed failure
  signal motivates further hardening this cycle (would be speculative — the
  TB-240/TB-172/TB-175 veto shape).
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a) symptom-patch
  remediations without root-cause (TB-231) and (b) speculative enumerated-case
  validators guarding unobserved failures (TB-240, TB-172, TB-175). This cycle's
  proposal is NEITHER: it is not a validator and not speculative — it closes an
  *observed, deterministic* startup gap (codex handle missing → late cryptic
  ImportError) and is the exact mirror of the operator-APPROVED `TB-368`. Pattern
  noted so future cycles stay clear.

## Cycle observations

- The daemon-start backend-aware gate decomposes into THREE concerns: credentials
  (`TB-358`, both backends), Claude-SDK availability (`TB-368`, claude only), and
  codex-handle availability (this cycle's proposal, the missing third).

## Decisions needed from operator

- Decision needed: The goal.md Mission (L18-20, L68-70) repeatedly points
  at a downstream OSS-distribution focus that is not yet a `## Current focus`.
  Define that next focus via `ap2 update-goal` so the next ideation cycle has an
  in-progress focus to propose against; without it the next cycle must declare
  whole-goal exhaustion and park ideation.

## Proposals this cycle

- TB-369 — backend-aware daemon-start codex-handle-availability gate: when the
  agent-backend map references `codex`, verify the codex handle is importable at
  startup and fail fast with a clear diagnostic; mirrors `TB-368`'s Claude-side
  gate. Closes the one remaining axis-5 edge case (Progress-signal-adjacent gap).