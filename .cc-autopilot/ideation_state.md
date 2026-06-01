# Ideation State

_Last updated: 2026-06-01T06:05:12Z by ideation cron_

## Mission alignment

5 most recent Completes — TB-367 (mixed-config `ideation=claude`/`task=codex`
e2e through the adapter, e927df5), TB-366 (residual `claude_agent_sdk` imports
relocated behind `ap2/adapters/` + AST import-direction gate, f2edcf4), TB-365
(shared `_run_control_agent` adapter-routed; "last direct sdk.query removed from
daemon.py", cbcc137), TB-364 (`run_task` adapter-routed with full MCP toolset,
18107a9), TB-363 (validator-judge + janitor-judge adapter-routed, 54e278d). No
mission drift: every ship moves a dispatch concept behind the AgentAdapter seam —
the literal codex-support focus.

## Current focus assessment

- **codex support through an agent adaptor layer** (7 axes)
  - Progress so far: axis1 `TB-353` (AgentAdapter ABC +
    ClaudeCodeAdapter), axis2 `TB-354` (normalized AgentOptions/AgentUsage),
    axis3 `TB-355` (MCP tools through adapter), axis4 `TB-357` (CodexAdapter),
    axis5 `TB-358` (per-kind selection + backend-aware *credential* gate), axis6
    six dispatch-site migrations (`TB-360` scrub, `TB-362` verifier-judge,
    `TB-363` validator+janitor judge, `TB-364` run_task, `TB-365`
    _run_control_agent), axis7 `TB-359` (parity suite + gated codex real-SDK
    smoke). Both prior-cycle gaps closed today: import-consolidation L205-206
    (`TB-366`, pinned by `test_sdk_import_boundary.py`) and mixed-config e2e
    L209-211 (`TB-367`).
  - Gaps: ONE non-trivial edge case remains. `main_loop` (daemon.py:1926/1936)
    calls `_import_sdk_or_die()` + `load_claude_sdk()` UNCONDITIONALLY at startup,
    so a pure-codex agent-backend map (every kind → codex) still hard-fails
    without `claude_agent_sdk` installed. `TB-358` made only the *credential*
    gate (cli_daemon.py, walks `cfg.get_agent_backend(kind)` over `AGENT_KINDS`)
    backend-aware; the SDK-*availability* gate was left unconditional. The focus's
    own constraint ("daemon-start gate requires creds for each backend the map
    references") implies the install/availability gate should mirror it.
  - Status: `in-progress`
  - Reasoning: one concrete, workable gap remains (backend-aware SDK-availability
    gate); a real focus-paying next step exists.

## Non-goal risk check

none. The proposal stays inside the focus: it makes the daemon-start gate
backend-aware (mirrors `TB-358`) without changing any agent's prompt/tool
policy/verification semantics (respects L130-131) and adds no third backend or
per-message routing (respects L127-128).

## Considered & deferred this cycle

- **Greenfield ap2-meta polish to fill the 4 unused slots**: deferred. Manufacturing 4 more proposals to fill slots would
  drift into the "ap2-meta polish / scope creep" goal.md L46-48 + the focus
  delete-test (L198-202) explicitly forbid. One real gap → one proposal.
- **Abstract the tool-DEFINITION mechanism (`@tool` schema) for Codex**: still
  deferred (carried): `TB-355`/`TB-366` only relocate the `tool` import;
  genuinely abstracting tool-definition for a non-Claude backend has no caller
  yet (CodexAdapter tool-wiring from `TB-357` covers registration). Re-propose
  only when a concrete Codex tool-definition need surfaces.
- **Symmetric codex-CLI-presence check at daemon start**: deferred into TB-368's
  Out-of-scope — `TB-358` already gates the `OPENAI_API_KEY` cred for codex
  kinds; CLI-binary presence is a thinner follow-up, kept out to hold TB-368
  narrow (avoid the >7-criteria split anti-pattern).
- **Operator-rejection pattern (recurring)**: vetoes cluster on (a) symptom-patch
  remediations without root-cause (TB-231) and (b) speculative enumerated-case
  validators guarding unobserved failures (TB-240, TB-172, TB-175). The SDK-gate
  proposal is neither — it closes an *observed* startup hard-fail for a config
  the focus explicitly supports. Pattern noted so future cycles stay clear.

## Cycle observations

- `TB-358`'s backend-aware gate is *credential*-only; the SDK-import
  availability check is a separate, still-unconditional gate — easy to conflate
  (both are "daemon-start gates"). Flagged so next cycle doesn't assume axis-5
  covered both.

## Decisions needed from operator

- Decision needed: the codex-adapter focus is one task (TB-368) from needing operator direction —
  all 7 axes have shipped. The goal.md Mission repeatedly points at a downstream
  OSS-distribution focus. Define the next focus via `ap2 update-goal` before TB-368 lands, else the next ideation cycle has
  no in-progress focus to propose against.

## Proposals this cycle

- TB-368 — backend-aware daemon-start SDK-availability gate: only require
  `claude_agent_sdk` when a kind resolves to `claude`; closes the pure-codex
  startup hard-fail edge case (Progress-signal-adjacent gap; mirrors `TB-358`).