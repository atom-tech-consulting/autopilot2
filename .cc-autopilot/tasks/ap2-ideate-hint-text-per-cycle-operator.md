# `ap2 ideate --hint "<text>"` — per-cycle operator intent forwarded into ideation prompt

## Goal

Let an operator who runs `ap2 ideate` (force-bypass past natural
gates including TB-174's focus-exhausted gate) attach a single-line
intent hint that the resulting ideation cycle's prompt header
surfaces verbatim. This addresses goal.md's "Current focus: ideation
quality" focus item (specifically the "Gap-covering without drift"
sub-clause) by giving the operator an in-band knob to nudge the next
forced cycle toward a specific slice of the focus item without
rotating goal.md or mutating `.cc-autopilot/ideation_state.md`.

Why now: the 2026-05-06T16:11:19Z `applied operator-queued ideate →
(forced)` event fired one second after `ideation_skipped reason=
focus_exhausted` (16:11:18Z) — the operator pushed past the
TB-174 gate but the resulting cycle inherited a stale
`exhausted-needs-operator` assessment with zero in-band signal
about which slice of the focus item they wanted forward motion on.
Today the only ways to give that signal are heavyweight (rotate
`goal.md ## Current focus`, manually rewrite ideation_state.md, or
wait for the next natural cycle). A `--hint` carries operator
intent forward at zero refactor cost and produces an auditable
event trail for the "is forced-ideation cadence a goal-rotation
signal?" question.

## Scope

(1) `ap2 ideate` CLI gains `--hint <text>` (single-line, length-
capped to 280 chars; reject newline/CR per TB-134's single-line
policy). Without `--hint`, behavior unchanged.

(2) The hint is queued via the operator queue (alongside the
existing `ideate (forced)` op) and persisted on the
`do_operator_queue_append` payload. The drain-side handler stores
the hint into a transient slot (e.g. an entry in
`operator_queue_state.json` consumed exactly once on the next
`_maybe_ideate` invocation) so the hint survives the gap between
queue drain and ideation dispatch but does not bleed into
subsequent cycles.

(3) When `_maybe_ideate` runs a forced cycle that carries a hint,
inject a `## Operator hint (this cycle only)` block into the
ideation prompt header (built by `prompts.build_control_prompt`),
between `## Current state` and `## Recent operator rejections`.
Block contents: the verbatim hint plus a one-liner reminder that
the hint is per-cycle and does NOT modify goal.md.

(4) Emit a structured event (`ideation_forced_with_hint` or extend
the existing `ideation_run` payload with a `hint` field —
implementer's call) so the audit trail records what the operator
nudged toward. Surface the hint in the `/events` web row and
`ap2 logs` output via existing rendering paths.

(5) MM handler chat parity (TB-176): `@claude-bot ideate hint:
<text>` routes through the same queue payload, mirroring the
existing `ideate force` chat-verb pattern.

## Design

The hint travels through the existing operator-queue path (no new
fence work; `operator_queue.jsonl` is already gitignored and
already accepts arbitrary `args`). Per-cycle persistence reuses
`operator_queue_state.json` (TB-131-era) — add a `pending_ideation_hint`
field that the drain pops and `_maybe_ideate` consumes. Prompt-
header injection reuses `state_extras` — the same plumbing TB-183
used for `proposal slots this cycle: N`. Renderer change is local
to `prompts.build_control_prompt`. Cost change is bounded by the
hint length (≤280 chars) — negligible vs. the existing ~10K-char
control header.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes
- `grep -nE "args\.hint|--hint" ap2/cli.py` — hint flag wired into
  `cmd_ideate`
- `grep -nE "Operator hint" ap2/prompts.py` — header injection
  block present in `build_control_prompt`
- `grep -nE "hint" ap2/ideation.default.md` — ideator briefing
  documents the hint surface (e.g. instructs the ideator to treat
  it as a per-cycle nudge that does NOT modify goal.md)
- `test -f ap2/tests/test_ideate_hint.py` — dedicated test module
  exists
- New unit tests in `ap2/tests/test_ideate_hint.py` pin: CLI
  rejects multi-line/CR hints (TB-134 parity); CLI rejects hints
  exceeding the 280-char cap; operator-queue payload carries the
  hint string; drain stores `pending_ideation_hint` in
  `operator_queue_state.json`; `_maybe_ideate` consumes the slot
  exactly once and emits the audit event with the verbatim hint;
  `prompts.build_control_prompt` injects the `## Operator hint
  (this cycle only)` block when the slot is non-empty and omits
  the block cleanly when absent
- New e2e test in `ap2/tests/e2e/test_tb184_ideate_hint.py`
  drives the full path: `ap2 ideate --hint "..."` → drain → next
  tick's `_maybe_ideate` → captured prompt contains the hint
  block → audit event present with the verbatim hint

## Out of scope

- Persisting hints across multiple cycles (intentionally per-cycle
  — multi-cycle persistence is what `goal.md` is for).
- Auto-generated hints from prior cycle state (would re-introduce
  the operator-judgment-replacement non-goal).
- Web-UI input field for hints (operator-side CLI + chat verbs are
  enough for the walk-away workflow; can follow as a separate task
  later if usage justifies it).
- Hint-aware behavior for natural (non-forced) cycles (the hint
  channel is paired with the `ap2 ideate` force-bypass surface
  specifically; natural cycles have goal.md as their authority).
