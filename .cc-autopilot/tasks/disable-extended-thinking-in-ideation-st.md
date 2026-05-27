# Disable extended thinking in ideation_state scrub; emit error/timeout audit event

Tags: #autopilot #ideation #scrub #latency #regression-pin #bug

## Goal

Fix the silent-timeout failure mode of `ideation_scrub.scrub_exhaustion_language`
by passing `thinking={"type": "disabled"}` in its `ClaudeAgentOptions`. Add an
`ideation_state_scrub_error` event so timeouts / SDK errors emit an audit
record instead of the current silent fail-open path. Closes the goal.md
`## Done when` failure mode "Ideation reliably proposes goal-aligned next
steps that substantively advance the goal (not just goal-shaped pro-forma
compliance)" — the scrub mechanism shipped in TB-284 has been non-functional
in production for the ideation cycles it ran in (every call hit the 60s
timeout silently), allowing exhaustion-asserting sentences to persist in
`ideation_state.md` across cycles and prime ideation toward declaring the
focus done; the silent failure mode also meant the operator had zero
visibility into the broken scrub, so the bug compounded undetected until
the 2026-05-26 false-advance investigation surfaced it.

Why now: the 2026-05-26 investigation traced the silent timeouts to Haiku
4.5's auto-engagement of extended thinking on the scrub's prompt shape
(strict DELETE / KEEP rules + dense per-sentence classification on mixed
factual+verdict markdown). Instrumented diagnostic revealed: real-content
8KB input produces a `ThinkingBlock` containing 22439 characters of
internal reasoning at `t=105.27s`, followed by a 7.4KB scrubbed
`TextBlock` at `t=111.49s` — total 110s, well past the 60s timeout. With
`thinking={"type": "disabled"}` in `ClaudeAgentOptions`, the same scrub
on the same content completes in 23.60s and emits the SAME 7.4KB scrubbed
output with the SAME sentences removed (the L128 stale roadmap-complete
verdict bullet + a fresh exhaustion-anticipation sentence from this
cycle). Validator-judge (`ap2/validator_judge.py`) uses the same Haiku 4.5
model and same SDK shape but doesn't hit this because its prompt shape
asks for a short JSON verdict — Haiku's thinking phase still engages but
the small output keeps total latency bounded. Scrub's per-sentence
classification with a full markdown rewrite is the pathological case.
Without this fix, scrub is dead code; with it, TB-284's design intent
(remove self-confirming verdict language post-write) is delivered.

## Scope

(1) `ap2/ideation_scrub.py` `_run_scrub`: add
`thinking={"type": "disabled"}` to the `sdk.ClaudeAgentOptions(...)`
call inside the inner `_ask` coroutine (currently around L201-205).
Place alongside the existing `permission_mode`, `max_turns`, `model`
kwargs. No other call-shape changes — preserve the existing
`max_turns=_SCRUB_MAX_TURNS` (=2), `permission_mode="bypassPermissions"`,
and model selection.

(2) `ap2/ideation_scrub.py` + `ap2/ideation.py`: add an
`ideation_state_scrub_error` event that fires when
`scrub_exhaustion_language` returns the input unchanged due to an SDK
error / timeout (the current silent fail-open path). Carry payload
fields: `reason` (one of `timeout`, `sdk_error`, `empty_output`),
`duration_s` (wall-clock), and optionally `error` (the exception's
`type(e).__name__` for non-timeout paths). Emit from inside
`_maybe_scrub_ideation_state` in `ap2/ideation.py` — that function
already owns the post-write hook + emits `ideation_state_scrubbed`
on success, so it's the natural place for the error-side audit.

(3) To support (2), `scrub_exhaustion_language` needs to distinguish
success-with-no-diff (model returned input verbatim because nothing
matched the delete criteria — fine, no event needed) from
failure-returning-input (timeout/error/empty — needs an event).
Easiest shape: have `scrub_exhaustion_language` raise a typed
exception (`ScrubTimeoutError`, `ScrubSDKError`, `ScrubEmptyOutputError`)
on failure, and let `_maybe_scrub_ideation_state` catch + emit the
appropriate event. The current return-input-unchanged fail-safe
semantics are preserved: the caller writes nothing back to the file
on exception, just emits the event.

(4) Register `ideation_state_scrub_error` in `ap2/events.py` event
vocabulary alongside the existing `ideation_state_scrubbed` entry,
with a comment naming the three `reason` values.

(5) Regression-pin module
`ap2/tests/test_scrub_disable_thinking.py` covers:
  - `_run_scrub` passes `thinking={"type": "disabled"}` to
    `sdk.ClaudeAgentOptions` (verifiable by patching the SDK options
    constructor with a recording stub).
  - On stubbed SDK timeout, `scrub_exhaustion_language` raises
    `ScrubTimeoutError`; on stubbed SDK error, raises
    `ScrubSDKError`; on stubbed empty output, raises
    `ScrubEmptyOutputError`.
  - `_maybe_scrub_ideation_state` catches each exception type and
    emits `ideation_state_scrub_error` with the correct `reason`
    payload field; the original `ideation_state.md` content is NOT
    overwritten in the error path.
  - On successful no-op (input == scrubbed output), NO event fires
    (steady-state happy path preservation).

(6) Update the existing test module from TB-284
(`ap2/tests/test_scrub_exhaustion_language.py` if it exists, or
whatever module covers the success path) to assert the new
`thinking` kwarg in the options dict, mirroring item (5)'s seam.

## Design

The TB-284 design intent was correct (post-write filter to remove
self-confirming verdict sentences), but the SDK call shape didn't
account for Haiku 4.5's adaptive-thinking behavior on classification
prompts. The fix is a one-kwarg change to disable extended thinking
for this specific call. Justification:

- The scrub task is per-sentence binary classification (DELETE vs
  KEEP). Each rule in the system prompt is concrete and pattern-
  matchable. Extended thinking doesn't improve classification quality
  here — it just produces internal "I'm considering whether sentence
  X matches rule Y..." reasoning chains that aren't reflected in the
  final output.
- Empirical validation showed identical output between `thinking`
  enabled (110s) and `thinking` disabled (23.6s) on the same input.
  Same sentences removed; same structure preserved.
- Latency stays comfortably under the existing 60s
  `_SCRUB_TIMEOUT_S` budget (~40% headroom) without needing to bump
  the timeout, leaving slack for genuinely larger files.

The audit event closes the second-order bug exposed by the
investigation: the existing fail-safe path swallowed errors with no
event emission, so the operator had no signal that the scrub was
broken. With the audit event, an operator watching `events.jsonl` or
`ap2 status`'s automation digest will see `ideation_state_scrub_error
reason=timeout` (or similar) and can react. The audit event sits on
the error path only; the steady-state happy path (scrub ran, no diff
needed) remains silent to avoid emit-noise on every healthy cycle.

Three new exception types are intentional — coarser-grained
discriminators than a single `ScrubError` with a `reason` attribute,
because the test seam is cleaner (each path tested in isolation) and
the operator-facing payload field `reason` mirrors the exception type
1:1.

## Verification

- `grep -q 'thinking.*disabled' ap2/ideation_scrub.py` — disable kwarg wired.
- `grep -q 'ideation_state_scrub_error' ap2/events.py` — event registered.
- `grep -q 'ideation_state_scrub_error' ap2/ideation.py` — event emitted from the post-write hook.
- `grep -qE 'ScrubTimeoutError|ScrubSDKError|ScrubEmptyOutputError' ap2/ideation_scrub.py` — typed exceptions defined.
- `uv run python -c "import inspect, ap2.ideation_scrub; src = inspect.getsource(ap2.ideation_scrub._run_scrub); assert 'thinking' in src and 'disabled' in src, 'thinking=disabled must be wired into _run_scrub'"` — kwarg present in source.
- `test -f ap2/tests/test_scrub_disable_thinking.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_scrub_disable_thinking.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Restructuring the scrub prompt for shorter output / JSON-delete-list
  shape — alternative approach considered (would shift the rewrite to
  client-side post-processing). Rejected for this TB because the
  one-kwarg fix is empirically validated and minimal; the prompt
  restructure is a larger redesign worth its own TB if the per-call
  latency proves problematic on even larger files.
- Bumping `_SCRUB_TIMEOUT_S` — the 60s budget is sufficient with
  thinking disabled (observed 23.6s with 40% headroom). Bumping is a
  fallback if thinking-disabled latency creeps up on larger files in
  practice.
- Switching scrub model from Haiku 4.5 to a different model — the
  cost/latency profile is right for sentence classification; thinking-
  disabled Haiku is the cheapest viable surface.
- Bug 3 (operator pointer rewind not emitting `focus_advanced` event
  for the empty-cycles counter cutoff) — separate TB. Independent
  surface.
- Backfilling historical `ideation_state.md` content with a one-shot
  scrub pass — the next ideation cycle will overwrite the file
  anyway; backfill adds complexity for marginal value.
- Persisting scrub durations for forensic analysis (e.g., emitting
  `duration_s` on the success path) — the error event carries
  duration for failures; success-path duration is observable via
  control-run metrics if needed.
