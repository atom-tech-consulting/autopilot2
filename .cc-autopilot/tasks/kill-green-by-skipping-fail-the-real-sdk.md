# Kill green-by-skipping: fail the real-SDK smoke run when codex variants skip despite AP2_REAL_SDK + a codex credential present

Tags: #autopilot #agent-adapter #codex #tests #smoke #ci-honesty #axis-7

## Goal

This advances **Current focus: codex support through an agent adaptor
layer** by making the codex parity smokes *honest*. Today the smoke suite
converts a transient/credential/transport error into `pytest.skip` (via
`ap2/tests/smoke/_transient.py:call_with_transient_retry`) and the codex
variants `importorskip("openai_codex")` (`_adapter.py:gate_backend`). So if
codex auth lapses, the SDK breaks, or the bridge regresses, the codex
smokes **skip silently** while the 6h `real-sdk-smoke` cron
(`ap2/smoke_runner.py`) still reports `smoke_check_passed`. Coverage erosion
is indistinguishable from passing — which is precisely how the phantom-SDK
bug hid for weeks (the old `importorskip("codex_sdk")` always skipped, so a
backend that targeted a nonexistent API looked "green").

Add a guard so that **expected-but-skipped codex coverage is a hard
failure**, while a legitimately-absent backend (no creds / handle) still
skips quietly.

Why now: parity smokes for judges, control agents, and real-work are being
added as siblings; without this guard each can silently regress to
"skipped = green" the moment a credential expires, and the cron would never
surface it. This is the cheapest, highest-leverage reliability fix in the
parity set. Operator-directed 2026-06-04.

## Scope

- **Detect "codex expected to run".** Define the condition: `AP2_REAL_SDK`
  set AND `openai_codex` importable AND a codex credential present
  (`OPENAI_API_KEY`, OR `$CODEX_HOME`/`~/.codex/auth.json` with
  `auth_mode: chatgpt` — reuse the same presence check the daemon-start
  auth gate uses; do NOT read token contents).
- **Fail on expected-but-skipped.** Under that condition, the smoke run
  must FAIL (not pass-with-skips) if any codex-parametrized smoke variant
  was skipped — i.e. codex was supposed to run and didn't. Implement via a
  session-scoped check (e.g. a `pytest_terminal_summary` / session-finish
  conftest hook in `ap2/tests/smoke/` that inspects skipped reports, or an
  explicit "minimum codex variants ran" assertion), so the failure surfaces
  at the run level regardless of which file skipped.
- **Surface it on the cron.** `ap2/smoke_runner.py:run_smoke_check` must
  treat an expected-but-skipped codex run as a smoke FAILURE — it must NOT
  emit `smoke_check_passed`; emit a distinct failure/alarm event naming the
  skipped codex coverage.
- **Stay quiet when codex is legitimately absent.** When `openai_codex`
  isn't installed or no codex credential is present, skips are expected and
  the guard must not fire (a Claude-only box still passes).
- **Tests** (`ap2/tests/`): (a) guard FAILS when codex handle+credential
  are present (faked) but a codex variant reported skipped; (b) guard
  PASSES when the codex variants ran; (c) guard is QUIET (no failure) when
  the codex handle/credential is absent.

## Design

- **Distinguish "absent" from "present-but-didn't-run".** The signal that
  hid the phantom SDK was a skip that read as a pass. The guard's entire job
  is to make that one case — codex *could* and *should* have run, but a
  variant skipped — loud, while leaving the genuinely-absent case quiet.
- **Run-level, not per-test.** A single skipped variant must fail the whole
  smoke run / cron check, so partial silent erosion can't accumulate.
- **Reuse the existing credential presence check** (the daemon-start auth
  gate's helper) — one source of truth for "a codex credential is present,"
  presence-only, no token contents read or logged.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new guard tests.
- `grep -rqE "skip|skipped" ap2/tests/smoke/conftest.py` — a session-level skip-inspection guard exists in the smoke harness. (If implemented elsewhere, the prose bullet below pins it.)
- `ap2/smoke_runner.py` Prose: `run_smoke_check` treats a run in which codex was expected (AP2_REAL_SDK set, `openai_codex` importable, a codex credential present) but a codex-parametrized smoke variant skipped as a smoke FAILURE — it does not emit `smoke_check_passed` and emits a distinct alarm event naming the skipped codex coverage; a run with codex legitimately absent still passes. Judge confirms via Read.
- `ap2/tests/smoke/` Prose: a session-scoped guard fails the run when codex handle + credential are present but a codex variant reported `skipped`, passes when the codex variants ran, and stays quiet when codex is absent — pinned by hermetic tests that fake credential/handle presence and a skipped report. Judge confirms via Read.

## Out of scope

- Running the live smokes themselves (operator-owned; `AP2_REAL_SDK=1` + real credentials).
- Changing the per-test `call_with_transient_retry` semantics for a genuinely transient single-call hiccup — a one-off transient retry-then-skip stays as is; this guard operates at the run level on the codex-expected condition.
- The sibling parity smokes (judges / control agents / real-work) — separate tasks.
