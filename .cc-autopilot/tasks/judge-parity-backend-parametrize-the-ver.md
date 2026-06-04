# Judge parity: backend-parametrize the verifier + validator real-SDK smokes over codex and add a janitor-judge smoke

Tags: #autopilot #agent-adapter #codex #tests #smoke #parity #judges #axis-7

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** toward full backend parity. The judge kinds are backend-selectable
(`verifier_judge`, `validator_judge`, `janitor_judge` are in
`ap2/adapters/select.py:AGENT_KINDS`, settable via
`AP2_AGENT_BACKEND_<KIND>`), but no live smoke proves codex produces a
correct verdict for any of them: `test_prose_judge_real_sdk.py`
(verifier_judge) and `test_validator_judge_real_sdk.py` are **claude-only**,
and `janitor_judge` has **no** real-SDK smoke on either backend. Judges gate
verification, so a codex judge that mis-verdicts would slip through silently.

Parametrize the judge smokes over both backends and add the missing
janitor-judge smoke, so each judge kind is proven to return the correct
pass/fail verdict on **both** claude and codex.

Why now: TB-374 parametrized the task-agent tool smokes but explicitly
deferred the judge smokes — leaving codex live-validated for only 1 of 9
agent kinds. Closing judges is required for parity (operator-directed
2026-06-04: "the goal is not done until full parity"). Builds on TB-374's
`_adapter.py` backend-parametrization helpers (`BACKENDS`, `gate_backend`,
`force_backend`).

## Scope

- **Parametrize `test_prose_judge_real_sdk.py` (verifier_judge) over
  `BACKENDS`**: route the judge call through the adapter seam for the
  selected backend (`select_adapter("verifier_judge", cfg)` / the verifier's
  judge path under `force_backend(..., "verifier_judge", backend)`), gating
  the codex variant with `gate_backend`. Keep the existing assertions: a diff
  that obviously satisfies a prose bullet → `status="pass"`; one that
  obviously contradicts → `status="fail"` — now for BOTH backends.
- **Parametrize `test_validator_judge_real_sdk.py` over `BACKENDS`**
  likewise (the dep-coherence validator), preserving its existing
  assertions for both backends.
- **Add a janitor-judge real-SDK smoke** (new file, e.g.
  `test_janitor_judge_real_sdk.py`), parametrized over both backends,
  asserting the janitor judge returns the correct verdict on a representative
  obviously-pass and obviously-fail input via `select_adapter("janitor_judge", cfg)`.
- **Preserve the opt-in posture**: keep the module-level `AP2_REAL_SDK`
  skip marker and the `gate_backend` codex `importorskip` so the default
  `pytest` run (and CI) skips these and they run on the 6h `real-sdk-smoke`
  cron.

## Design

- **One judge test, both backends, through the seam.** Expressing each judge
  smoke as parametrized-over-`select_adapter(<judge_kind>)` is the literal
  parity contract: the same verdict assertions run on whichever backend the
  kind selects.
- **Reuse TB-374's harness.** `_adapter.py`'s `BACKENDS` / `gate_backend` /
  `force_backend` already encode the parametrization + codex opt-in gate;
  these smokes use them rather than re-deriving the gate.
- **Verdict correctness, not just dispatch.** Unlike the tool smokes
  (which assert a tool *call*), judge parity must assert the *verdict value*
  (pass vs fail) is correct on both backends — a judge that dispatches but
  mis-verdicts is the failure mode that matters.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes; smokes still skip by default (confirms the refactor didn't break collection/imports).
- `grep -qE "BACKENDS|parametrize" ap2/tests/smoke/test_prose_judge_real_sdk.py` — the verifier-judge smoke is backend-parametrized.
- `grep -qE "BACKENDS|parametrize" ap2/tests/smoke/test_validator_judge_real_sdk.py` — the validator-judge smoke is backend-parametrized.
- `test -f ap2/tests/smoke/test_janitor_judge_real_sdk.py` — a janitor-judge real-SDK smoke exists.
- `ap2/tests/smoke/test_prose_judge_real_sdk.py` and `test_validator_judge_real_sdk.py` Prose: each routes its judge call through `select_adapter(<judge_kind>)` / the adapter seam, is parametrized over the `claude` and `codex` backends, and asserts the correct pass/fail verdict for an obviously-satisfying and an obviously-contradicting input on BOTH backends; the codex variant skips cleanly when `AP2_REAL_SDK` is unset or the codex handle is unavailable. Judge confirms via Read.
- `ap2/tests/smoke/test_janitor_judge_real_sdk.py` Prose: a new janitor-judge real-SDK smoke is adapter-routed and backend-parametrized, asserting the correct verdict on both backends with the same opt-in gating. Judge confirms via Read.

## Out of scope

- Running the live smokes against either backend (operator-owned; `AP2_REAL_SDK=1` + real credentials).
- The skip-masking run-level guard (sibling task) and the task-real-work / control-agent parity smokes (sibling tasks).
- Changing judge logic, the AgentAdapter contract, or production dispatch.
