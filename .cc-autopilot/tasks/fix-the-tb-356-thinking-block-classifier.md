# Fix the inert thinking-block recovery path: classifier match, auto-approve breaker exemption, and gated-head queue jam

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

**Third defect — a gated Backlog head jams everything behind it.** The
tick's auto-promote step (`ap2/daemon.py` "3. Next Ready task", ~L2328)
picks ONE candidate via `board.next_dispatchable("Backlog")` (the
topmost task whose blockers are all Complete), runs the auto-approve
gate on it, and on a paused-auto-approved skip sets `backlog = None` and
ends the tick — it never advances to the next dispatchable candidate. So
when an ideation-auto-approved task sits at the Backlog head while the
window is paused, it is skipped every tick and **every task behind it is
frozen too — including operator/human-authored (`ap2 add`) and
operator-approved (`ap2 approve`) tasks the pause was explicitly designed
NOT to gate.** Observed 2026-05-31: this exact task (TB-361, human-
authored, never auto-approved) could not promote despite a free Active
slot, because TB-359/TB-360 (auto-approved, paused) sat ahead of it and
nulled the candidate each tick. The daemon comment at ~L2330 asserts
operator-added work "must always drain" — this ordering bug defeats that.

Fix all three so the thinking-block-400 class is detected on the real
failure shape, downshifts effort on retry, does not halt the auto-approve
window, AND a non-gated task behind a gated one still dispatches.

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
- **Skip past a gated Backlog head instead of halting the tick**
  (`ap2/daemon.py` auto-promote step, ~L2328). When the selected
  `next_dispatchable` candidate is skipped by the auto-approve gate
  (paused-auto-approved, token-cap, or noisy-validator halt), the
  promoter must advance to the NEXT dispatchable candidate rather than
  setting `backlog = None` and ending the tick — so a candidate that the
  gate does NOT halt (operator-added / operator-approved / not-auto-
  approved) still dispatches from behind a gated one. Preserve the
  invariants: still at most one promotion per tick; auto-approved tasks
  stay held while paused (only NON-halted candidates dispatch); bounded
  iteration over the Backlog (no infinite loop); keep emitting the
  `auto_approve_skipped` / `auto_approve_paused` observability event for
  each task the gate holds. Effectively: iterate dispatchable Backlog
  candidates in order, skip the ones the gate halts, promote the first
  one it doesn't.
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
- **Skip-past restores the documented invariant.** The pause was only
  ever meant to halt the auto-approved layer (daemon.py:2330: operator
  work "must always drain"). Advancing to the next dispatchable
  candidate when the head is gated makes the implementation match that
  intent, without weakening the pause for auto-approved tasks. It is
  independent of the classifier/breaker fixes (it helps any pause cause,
  not just thinking-block), but belongs here because it's the third leg
  of the same "paused window wedges the board" failure.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new real-shape classifier + bump + breaker-exemption tests.
- `grep -nE "text_preview|last_messages|result" ap2/daemon.py` — the classifier (or its call site) now matches against the serialized message-tail content, not a bare object repr.
- `ap2/daemon.py` Prose: `_is_thinking_block_corruption` returns True when given a real thinking-block `task_error`'s `last_messages` payload (a list of message-summary dicts whose `text_preview` contains `API Error: 400 … thinking … cannot be modified`) — the exact shape that returns False before this change — and the failure path calls `_handle_failure(..., thinking_block_corruption=True)`, bumping the per-task downshift level and emitting an `effort_downshift` event. Judge confirms via Read.
- `ap2/components/auto_approve/impl.py` Prose: a `task_error` carrying the thinking-block classification does NOT pause the auto-approve window nor count toward the consecutive-freeze threshold, while a generic `task_error` still pauses it. Judge confirms via Read.
- New test file asserts `_is_thinking_block_corruption` is True on a fixture built from a real recorded `last_messages` payload (not a hand-simplified string), pinning the production shape.
- `ap2/daemon.py` Prose: the auto-promote step, when its `next_dispatchable` Backlog candidate is halted by the auto-approve gate (paused-auto-approved / token-cap / noisy), advances to the next dispatchable candidate and promotes the first one the gate does NOT halt, instead of nulling `backlog` and ending the tick — at most one promotion per tick, bounded iteration. Judge confirms via Read.
- New test: a Backlog ordered `[auto-approved task, operator-added task]` with the auto-approve window paused promotes the operator-added task (the gated auto-approved one stays in Backlog with its skip event), proving a gated head no longer freezes non-gated work behind it.

## Out of scope

- The upstream bundled-CLI thinking-block bug itself (not patchable from ap2).
- The effort ladder / base-effort default / `_resolve_task_effort` (correct already).
- Changing the live daemon's runtime state (`AP2_AGENT_EFFORT`, acking/resuming the window) — operator-managed out-of-band; this task only fixes the code paths and ships tests.
- Reordering the Backlog or changing `next_dispatchable`'s ordering rule — the fix is to skip-past gated candidates, not to re-sort the queue.
