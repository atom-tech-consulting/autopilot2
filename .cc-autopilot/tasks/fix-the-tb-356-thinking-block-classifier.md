# Fix the TB-356 thinking-block classifier (never matches the real failure) and exempt that class from the auto-approve breaker

Tags: #autopilot #bug #reliability #retry #effort #thinking-block #auto-approve

## Goal

TB-356 shipped a thinking-block-400 effort-downshift, but in production
it **never fires**. Evidence on this repo as of 2026-05-31:
- `grep -c '"effort_downshift"' .cc-autopilot/events.jsonl` → **0**,
  despite TB-354 (09:33Z) and TB-358 (18:04Z) both failing with the
  thinking-block-immutability 400.
- `.cc-autopilot/retry_state.json` has only attempt counters
  (`{"TB-358": 1, ...}`); no `…#downshift` key was ever written, so the
  per-task downshift level stayed 0 and every retry re-ran at the base
  `xhigh` — re-tripping the bug and re-wedging the window.

**Root cause — the classifier inputs don't contain the signature.** At
the call site (`ap2/daemon.py` ~L329-331) `_is_thinking_block_corruption`
(defined ~L751) is handed two things, neither of which carries the 400
text:
1. the raw in-memory `stream_log` (a list of SDK *message objects*) —
   the classifier does `json.dumps(stream_log, default=str)`, which on
   those objects yields `default=str` reprs (e.g. `<AssistantMessage …>`)
   that do NOT include the inner `text`/`result` where
   `API Error: 400 … thinking … cannot be modified` lives; and
2. the wrapping exception string `f"{type(e).__name__}: {e}"`, which is
   just `Exception: Claude Code returned an error result: success` — the
   real 400 is in the message tail, not the exception.

So both inputs miss, `_is_thinking_block_corruption` returns False,
`_handle_failure` is called with `thinking_block_corruption=False`, and
nothing downshifts. Note the irony: the `task_error` *event* records
`last_messages=stream_log[-10:]` in a SERIALIZED form whose
`text_preview` DOES contain the signature — i.e. the data is right
there, the classifier just isn't looking at the serialized shape.

**Second defect — the class still wedges the window.** Even once
classified, a thinking-block `task_error` trips the auto-approve
circuit breaker (`task_error` path in
`ap2/components/auto_approve/impl.py`'s `_auto_approve_check_violations`
/ pause logic), pausing the whole window until a manual
`ap2 ack auto_approve_window_resume`. Since this class is meant to be
*handled* (retry-with-downshift), it should NOT pause the window;
genuine task errors still should.

Fix both so the thinking-block-400 class is detected on the real failure
shape, downshifts effort on retry, and does not halt the auto-approve
window.

Why now: the codex-adaptor focus's investigation-heavy tasks keep
hitting this 400 (TB-354, TB-357→358 cascade); with the safeguard inert,
each occurrence wedges the board and needs a manual operator approve.
The fix turns the existing-but-dead TB-356 machinery into a working
autonomous recovery path. Operator-directed 2026-05-31; meta-infra
reliability, no focus anchor → `--skip-goal-alignment`.

## Scope

- **Make the classifier match the real failure shape**
  (`ap2/daemon.py`): feed `_is_thinking_block_corruption` the same
  serialized content the `task_error` event records (the `last_messages`
  shape with `text_preview` / `result` fields), not the raw object list
  whose `default=str` repr drops the text. Either serialize the
  stream_log the way the event does before matching, or have the
  classifier walk each message and pull its text/`result`/`text_preview`
  content. Keep the narrow signature (`cannot be modified` co-occurring
  with `thinking` / `redacted_thinking` / `blocks in the latest assistant
  message`).
- **Confirm the failure → bump → dispatch wiring** end-to-end:
  a matched classification must call `_handle_failure(...,
  thinking_block_corruption=True)`, which bumps `retry.bump_downshift`
  and emits the `effort_downshift` event, so the next
  `_resolve_task_effort` returns the stepped-down tier. (The downshift
  ladder + `_resolve_task_effort` themselves are correct — the bug is
  upstream at classification.)
- **Exempt the class from the auto-approve breaker**
  (`ap2/components/auto_approve/impl.py`): record the thinking-block
  classification on the failure event (e.g. a
  `thinking_block_corruption=true` field on the `task_error` /
  `task_complete` failure event), and make the breaker's `task_error`
  trip (`_auto_approve_check_violations` and/or the consecutive-failure
  `_auto_approve_paused` scan) skip events carrying that flag, so a
  thinking-block failure neither pauses nor counts toward the freeze
  threshold. A genuine `task_error` still pauses as today.
- **Regression tests** (`ap2/tests/`): the load-bearing one must use the
  REAL recorded shape — build the fixture from an actual thinking-block
  `task_error`'s `last_messages` payload (see
  `.cc-autopilot/debug/20260531T002008Z-TB-353.messages.jsonl` and the
  `task_error` events for TB-354 / TB-358 in `events.jsonl`), and assert
  `_is_thinking_block_corruption` returns True on it (this is the exact
  shape that returns False today). Add: (a) the same payload as raw
  stream_log objects also classifies True; (b) a thinking-block failure
  bumps `downshift_level` and emits `effort_downshift`; (c) the
  auto-approve breaker does NOT pause on a thinking-block-flagged
  `task_error` but DOES on a generic one; (d) a generic `task_error` /
  `verification_failed` still classifies False (no downshift, no
  exemption).

## Design

- **The data was always there.** The `task_error` event already stores
  the signature inside `last_messages[*].text_preview`; the only bug is
  the classifier matching against a lossy `default=str` object repr
  instead. Aligning the classifier's input with the event's serialized
  shape is the core fix, and the regression test pins it to the real
  payload so it can't silently regress to the lossy shape again.
- **Exemption keyed on the same classifier.** One source of truth: the
  failure event carries the `thinking_block_corruption` flag the
  classifier produced; both the downshift (retry) and the breaker
  (window) read that one flag. No second detector.
- **Conservative breaker change.** Only this one narrowly-classified
  class is exempted; every other `task_error` still pauses the window,
  so a genuine infrastructure failure is still caught.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new real-shape classifier + bump + breaker-exemption tests.
- `grep -nE "text_preview|last_messages|result" ap2/daemon.py` — the classifier (or its call site) now matches against the serialized message-tail content, not a bare object repr.
- `ap2/daemon.py` Prose: `_is_thinking_block_corruption` returns True when given a real thinking-block `task_error`'s `last_messages` payload (a list of message-summary dicts whose `text_preview` contains `API Error: 400 … thinking … cannot be modified`) — the exact shape that returns False before this change — and the failure path calls `_handle_failure(..., thinking_block_corruption=True)`, bumping the per-task downshift level and emitting an `effort_downshift` event. Judge confirms via Read.
- `ap2/components/auto_approve/impl.py` Prose: a `task_error` carrying the thinking-block classification does NOT pause the auto-approve window nor count toward the consecutive-freeze threshold, while a generic `task_error` still pauses it. Judge confirms via Read.
- New test file asserts `_is_thinking_block_corruption` is True on a fixture built from a real recorded `last_messages` payload (not a hand-simplified string), pinning the production shape.

## Out of scope

- The upstream bundled-CLI thinking-block bug itself (not patchable from ap2).
- The effort ladder / base-effort default / `_resolve_task_effort` (correct already).
- Changing the live daemon's current `AP2_AGENT_EFFORT` or resuming the paused window — deliberately left as-is by the operator so TB-358 remains a live reproduction; this task only fixes the code paths.
