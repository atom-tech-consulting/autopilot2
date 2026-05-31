# Dynamically step down agent effort on retry when a task hits the thinking-block-400 failure class

Tags: #autopilot #reliability #retry #effort #thinking-block #graceful-degradation

## Goal

Long, thinking-heavy task runs intermittently fail with a bundled-CLI
bug: Claude Code empties a prior thinking block's text but keeps its
signature during a context-management/summarization pass, then replays
it, and the API rejects it with `400 ... thinking or redacted_thinking
blocks in the latest assistant message cannot be modified`. ap2 surfaces
this as `task_error` with error `Exception: Claude Code returned an error
result: success` (the real 400 is in the run's `last_messages`).
Debugged on TB-353 (2026-05-30): `num_turns=42`, 13 thinking blocks all
emptied, error at `messages.1.content.13`. It is load-correlated — higher
`AP2_AGENT_EFFORT` makes each thinking block larger, so `xhigh` runs trip
it most. There is no upstream fix (still open across Claude-code dupes
#63147/#13012/#20938; SDK 0.2.87 is the latest; `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
does not propagate to the agent subprocess).

The reliable mitigation is fewer/smaller thinking blocks → lower effort.
But globally lowering effort sacrifices quality on every task, and a
blind same-effort retry just re-trips the bug. The right shape is
**graceful degradation**: run the first attempt at full effort, and only
when a task fails with *this specific* 400 signature, **step its effort
down one tier on the automatic retry** (xhigh → high → medium → low,
floored). Other failure classes (verification_failed, generic
task_error) retry at unchanged effort.

Why now: the active codex-adaptor focus is full of investigation-heavy
refactor tasks (axis-1 TB-353 already hit this), which are exactly the
long high-effort runs that trip the bug — so without auto-downshift they
false-fail and pause auto-approve repeatedly, stalling the focus and
forcing manual operator intervention each time. Operator-directed
2026-05-30; meta-infra daemon reliability with no focus anchor, so
`--skip-goal-alignment`.

## Scope

- **Classify the failure** at the `run_task` error path (`ap2/daemon.py`,
  the `task_error` emission ~L276–306 where `last_messages =
  stream_log[-10:]` is in hand). Add a helper, e.g.
  `_is_thinking_block_corruption(stream_log_or_error) -> bool`, that
  matches the signature: the substring "cannot be modified" together
  with "thinking" / "redacted_thinking" / "blocks in the latest
  assistant message". Keep it narrow so unrelated errors don't match.
- **Persist a per-task downshift level** in the retry state
  (`cfg.retry_state_file`, alongside the existing attempt counter that
  `retry.bump_attempt` / `retry.reset_attempt` manage). Increment it
  only when a failure classifies as thinking-block-corruption; leave it
  untouched for other failures; clear it when the task succeeds
  (wherever `retry.reset_attempt` is called, ~`daemon.py:598`).
- **Resolve effort with the downshift** at dispatch (`run_task` effort
  resolution, `daemon.py:231`): `effort = _step_down_effort(base,
  level)` where `base = cfg.get_core_value("agent_effort",
  default="xhigh")` and the ladder is `xhigh → high → medium → low`,
  clamped at a `low` floor. Level 0 = base (unchanged). This only
  affects the **task** agent dispatch; control agents are out of scope.
- **Emit an observability event** when a downshift is applied, e.g.
  `effort_downshift` with `task`, `from`, `to`, `reason="thinking_block_corruption"`,
  so `ap2 logs --follow` / status can show it.
- **Kill switch** `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED` (default
  enabled), read via the config layer like other knobs (env-not-code,
  hot-reloadable). When set, behavior is exactly as today (constant
  effort, blind retry).
- **Tests** (`ap2/tests/`): (a) the classifier returns True on a
  stream/last_messages carrying the 400 thinking-block text and False on
  a generic failure or a verification_failed; (b) `_step_down_effort`
  ladder: level 0→xhigh, 1→high, 2→medium, 3+→low (floor); (c) a
  thinking-block-corruption failure bumps the per-task level while a
  non-matching failure does not; (d) success resets the level; (e) with
  the kill switch set, effort stays at base across retries.

## Design

- **Graceful degradation, not blanket reduction.** First attempt keeps
  full `xhigh` quality; only the specific recurring bug triggers a
  one-tier drop per occurrence. A task that never hits the bug never
  loses quality; a task that hits it repeatedly walks down to a tier
  where the thinking blocks are small enough to avoid the corruption
  path, then completes.
- **Keyed to the exact 400 signature.** Verification failures (real test
  failures) and transient/other `task_error`s must NOT downshift — the
  agent needs full capability to fix a real failure, and dropping effort
  there would hurt, not help. The classifier is deliberately narrow.
- **Floor, not zero.** The ladder stops at `low` so a task can't be
  degraded below a useful tier; if even `low` still trips the bug the
  task exhausts retries and Freezes exactly as today — no infinite
  quality loss, no new failure mode.
- **Reuses existing retry state.** The downshift level rides alongside
  the attempt counter in `retry_state.json`; no new state file, and it
  clears on the same success path that resets attempts.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new classifier + ladder + state tests.
- `grep -rnE "thinking_block_corruption|_step_down_effort|effort_downshift" ap2/daemon.py ap2/*.py` — the classifier, ladder, and downshift event exist in source.
- `grep -qE "AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED" ap2/core_config_schema.py ap2/config_compat.py` — the kill-switch knob is registered in the config layer.
- `ap2/daemon.py` Prose: `run_task` resolves the task agent's effort as a step-down of the base `agent_effort` keyed to a per-task downshift level (ladder xhigh→high→medium→low, floored at low); the `task_error` path classifies the thinking-block-immutability 400 signature and bumps that level only for that class (not for verification_failed or other errors); an `effort_downshift` event is emitted on drop; the level resets on task success; the `AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED` kill switch restores constant-effort behavior. Judge confirms via Read.

## Out of scope

- Fixing the upstream bundled-CLI thinking-block bug (not patchable from ap2).
- Downshifting effort for any failure class other than the thinking-block-400 signature.
- Changing the base `AP2_AGENT_EFFORT` default or control-agent / judge effort.
- The lean-investigation prompt-scaffold nudge (separate, complementary task).
