# Fix `ap2 status` text rendering: "auto-approve: enabled" prints when knob is OFF if validator-judge has 24h activity (TB-243 regression)

Tags: `#autopilot` `#bug` `#operator-surface` `#observability` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis by fixing the `ap2 status` text-rendering regression introduced by TB-243 (`647b771`). At `ap2/cli.py:482-495`, the conditional `if a["auto_approve_enabled"] or _has_24h_activity:` ALWAYS prints `auto-approve: enabled (24h: ...)`, even when `auto_approve_enabled` is false and the only reason `_has_24h_activity` is truthy is the new `validator_judge_fail_count_24h` field TB-243 added to the activity check. JSON output is correct (`auto_approve_enabled: false` per `automation_status.collect_auto_approve_state`); only the text rendering misrepresents state. Observed today: with no `AP2_AUTO_*` env vars set, `ap2 status` printed `auto-approve: enabled (24h: 0 approved, 0 auto-unfrozen)` while `ap2 status --json` showed `auto_approve_enabled: false`. This false positive misled an operator (me) into a false-alarm investigation — the exact operator-trust erosion that observability surfaces are meant to PREVENT, not cause.

Why now: TB-243 just shipped (intended: surface validator-judge fail/timeout counts so operators notice silent-degradation hazards like TB-235's broken `--max-tokens` arg). The fix is correct in spirit — adding validator-judge counts to `_has_24h_activity` is the right move, since judge failures ARE activity worth showing. But the text-rendering branch needs to distinguish three states cleanly: (a) auto-approve enabled, (b) auto-approve disabled but there's activity worth surfacing, (c) auto-approve disabled and no activity (suppress entirely). Today's code conflates (a) and (b).

## Scope

(1) **Fix `ap2/cli.py:482-495` rendering branch**: split the existing single-line print into three branches based on the actual state:

  - **State A — auto-approve enabled**: `auto-approve: enabled (24h: N approved, M auto-unfrozen)` — current text, unchanged. Operator sees the enabled-state line + counts.
  - **State A-paused — auto-approve enabled but paused**: `auto-approve: PAUSED (reason=..., ...)` — current paused-branch text, unchanged.
  - **State B — auto-approve disabled, validator-judge activity present**: `auto-approve: disabled (validator-judge 24h: N fail, M timeout)` — NEW. Surfaces the activity that justified printing the block, without falsely claiming the knob is on. Only this row's text changes; the block-suppress condition stays.
  - **State C — auto-approve disabled, no activity**: SUPPRESS the block entirely (existing behavior — outer `if` evaluates false).

(2) **Decoupling the line from the block presence**: the existing line below the auto-approve line (the dry-run readiness sub-line at TB-241) and the focus-rotation line (TB-242) should continue rendering as today. The fix is local to the auto-approve line text — sibling lines unaffected.

(3) **Tests** (extend `ap2/tests/test_tb_status_render.py` if exists; create otherwise):
  - `test_status_text_auto_approve_enabled_renders_enabled`: set `AP2_AUTO_APPROVE=1` via monkeypatch; assert text output contains `auto-approve: enabled`.
  - `test_status_text_auto_approve_disabled_with_validator_failures_renders_disabled`: unset `AP2_AUTO_APPROVE`; seed events.jsonl with N `validator_judge_fail` events in window; assert text output contains `auto-approve: disabled` AND `validator-judge 24h: N fail`.
  - `test_status_text_auto_approve_disabled_no_activity_suppresses_block`: unset all `AP2_AUTO_*`; events.jsonl empty; assert text output does NOT contain `auto-approve:` line at all.
  - `test_status_text_auto_approve_paused_renders_paused`: simulate the paused state via the existing pause-event chain; assert text output contains `auto-approve: PAUSED`.
  - `test_status_json_auto_approve_enabled_unchanged_under_disabled`: regression-pin — the JSON output's `auto_approve_enabled: false` doesn't change based on `_has_24h_activity` (the bug existed only in text rendering; verify JSON path stayed correct).

(4) **No env knob changes, no surface additions**: pure rendering fix in cli.py + a regression-pin test surface. The bug is local to one branch's text; don't restructure the surrounding observability.

## Design

The conflation in TB-243 was a missed test case during implementation — the unit tests covered the JSON path (which stayed correct) and the auto-approve-enabled text path (which was already correct), but not the new "validator-judge activity surfaces the block when auto-approve is off" code path. Adding regression-pin tests on each of the three text branches (enabled / disabled-with-activity / disabled-no-activity / paused) closes the test coverage gap that let the regression ship.

**Why three text branches not two**: collapsing (B) and (C) into "always print when activity present, conditional text" works for today's signal set (auto_approved counts + validator_judge counts) but pre-bakes the assumption that "any of these counts > 0 means auto-approve-related activity." Future fields added to `_has_24h_activity` (e.g. focus-rotation events) might not be auto-approve-related at all — in which case the auto-approve line should suppress entirely while the other observability stays. Three branches makes the rule explicit: text content matches the actual state, block presence matches whether ANY activity is worth surfacing.

**Why fix is local to text rendering, not the activity check**: the activity check (`_has_24h_activity`) is correct as-is — validator-judge fails ARE activity worth surfacing. The bug is the text BELIEVES that "activity present" implies "knob is on," which isn't true. Fix the belief, not the input signal.

**Goal-anchor**: "an operator can point ap2 at a fresh project, paste a `goal.md`, and walk away for a week without intervention" requires that the operator's primary observation surfaces (`ap2 status`, web home, Mattermost digest) accurately represent state. A status surface that prints "enabled" when disabled is worse than no surface at all — it creates false confidence.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q -k "status_text"` — at minimum 5 tests in the status-text rendering area pass (the 4 new branches + the existing JSON regression-pin).
- Prose: with `AP2_AUTO_APPROVE` unset AND validator-judge fails in the 24h window, `ap2 status` text rendering MUST contain the literal substring `auto-approve: disabled` AND MUST NOT contain `auto-approve: enabled` AND MUST contain `validator-judge 24h:` somewhere in the block. Judge confirms by invoking `ap2 status` post-fix and reading the rendered lines.
- Prose: `ap2 status --json` output's `auto_approve.auto_approve_enabled` value is unchanged across the fix (regression-pin — the bug never affected JSON; the fix preserves that). Judge confirms by reading the JSON path and verifying no branching off `_has_24h_activity` exists in the collector.

## Out of scope

- Restructuring the `_has_24h_activity` aggregator — the aggregator is doing the right job; only the text-rendering interpretation was wrong.
- Adding a new "validator-judge" top-level CLI section separate from the auto-approve block — the validator IS part of the auto-approve quality surface; keeping the rendering grouped is correct.
- Suppressing the auto-approve block entirely when only validator-judge activity is present (no auto-approve counts) — see Design "Why three branches": the activity IS worth surfacing on the operator's primary observability surface; just label it honestly.
- Backfilling text-rendering tests for sibling observability surfaces (dry-run line, focus line) — those weren't regressed; this fix is scoped to the one observed defect.
- Adding `ap2 doctor` check for "auto-approve text rendering matches JSON" — over-engineering for a per-cycle test surface, not a runtime check.
## Attempts

### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T084645Z-TB-250.prompt.md`, `stream: .cc-autopilot/debug/20260517T084645Z-TB-250.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T084645Z-TB-250.messages.jsonl`
