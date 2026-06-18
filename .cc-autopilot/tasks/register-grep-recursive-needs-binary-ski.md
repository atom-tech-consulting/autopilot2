# Register grep_recursive_needs_binary_skip auto-unfreeze fix-shape (grep -rn -> grep -rnI) so the daemon self-heals binary-pyc false-fail bullets

Tags: #autopilot #auto-unfreeze #fix-shapes #briefing #verification #robustness

## Goal

Add `grep_recursive_needs_binary_skip` as a recognized auto-unfreeze fix-shape so the
daemon can self-heal the recurring "recursive `grep -rn` false-fails on binary
`__pycache__/*.pyc`" verification-bullet defect: parse the agent's structured
`BriefingFix:` hint and rewrite the named briefing line's `grep -rn` to `grep -rnI`,
gated by the existing `AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist exactly like the four
bootstrap shapes. Operator-filed meta-infra robustness fix; no goal.md focus anchor
(filed `--skip-goal-alignment`).

Why now: the auto-unfreeze machinery (`ap2/components/auto_unfreeze/impl.py` +
`ap2._shared.parse_blocked_summary_fix_shape`) already parses `BriefingFix:` lines
and auto-applies allowlisted fix-shapes, and the task agent already EMITS
`BriefingFix: grep_recursive_needs_binary_skip at <file>:<line>: grep -rn -> grep -rnI`
when it hits this defect — but that shape isn't in the registry, so the hint is inert
and the task freezes for a manual operator edit (observed multiple times). Registering
the shape closes the loop: the daemon recognizes the hint and self-heals (when the
operator has allowlisted it), no manual briefing edit.

## Scope

- Add `grep_recursive_needs_binary_skip` to the fix-shape registry that
  `ap2._shared.parse_blocked_summary_fix_shape` recognizes (alongside the existing
  bootstrap shapes `grep_missing_r_on_dir` / `literal_backtick_in_shell_bullet` /
  `bare_python_to_uv_run` / `bare_path_to_test_f`), with its transform: on the named
  briefing line, rewrite `grep -rn ` → `grep -rnI ` (add the binary-skip flag; do not
  touch other text on the line).
- Ensure the auto-unfreeze applier (`ap2/components/auto_unfreeze/impl.py`) applies
  the new shape through the SAME allowlist + dry-run + per-task/per-day cap path as
  the bootstrap shapes — it is opt-in via `AP2_AUTO_UNFREEZE_FIX_SHAPES` /
  `[components.auto_unfreeze] fix_shapes`, off by default.
- Idempotent: applying to a line already containing `grep -rnI` is a no-op (don't
  double-insert the flag).

## Design

- Mirror the existing bootstrap-shape pattern exactly (parser entry + transform +
  allowlist gating); this is an additive registry entry, not a new mechanism.
- The transform is a minimal token edit on the specific briefing line named in the
  `BriefingFix:` hint (`<file>:<line>`), matching how the agent describes the fix.
- Keep the feature opt-in and off-by-default (the allowlist is the trust contract);
  this task only makes the shape RECOGNIZABLE, not auto-enabled.
- **Execution discipline.** Run verification in the FOREGROUND; do NOT
  `run_in_background` + poll. Iterate against targeted tests; the daemon verifier runs
  the full suite after you report. Keep tool calls bounded.

## Verification

- `grep -rqn "grep_recursive_needs_binary_skip" ap2/_shared.py ap2/components/auto_unfreeze/impl.py` — the new shape is registered in the fix-shape parser/applier.
- `uv run --extra dev pytest -q ap2/tests/test_autounfreeze_grep_binary_skip.py` — a new test asserts: `parse_blocked_summary_fix_shape` recognizes a `BriefingFix: grep_recursive_needs_binary_skip at f.md:64: grep -rn -> grep -rnI` line; the transform rewrites a sample briefing line's `grep -rn ` to `grep -rnI ` (and is a no-op if already `-rnI`); and application is gated by the `fix_shapes` allowlist.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full suite stays green.
- `ap2/_shared.py` + `ap2/components/auto_unfreeze/impl.py` Prose: `grep_recursive_needs_binary_skip` is a recognized fix-shape whose transform rewrites the named briefing line's `grep -rn` to `grep -rnI`, applied only through the existing opt-in `fix_shapes` allowlist + caps (like the bootstrap shapes), idempotent on already-fixed lines; judge confirms via Read.

## Out of scope

- The ideation-prompt preventive guidance (sibling task).
- Auto-ENABLING the shape (it stays opt-in via `AP2_AUTO_UNFREEZE_FIX_SHAPES`; this
  task only registers it as recognizable).
- New fix-shapes beyond this one; broader auto-unfreeze behavior changes.
