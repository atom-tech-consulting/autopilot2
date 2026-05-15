---
title: "Monitor-only auto-unfreeze mode: `AP2_AUTO_UNFREEZE_DRY_RUN=1` emits `would_auto_unfreeze` events without mutating briefings or queueing unfreeze ops"
tags: ["#autopilot", "#automation", "#operator-surface", "#trust-building", "#dry-run", "#failure-recovery", "#regression-pin"]
---

## Goal

Add a monitor-only on-ramp to the axis-2 auto-unfreeze loop, sibling to TB-232's `AP2_AUTO_APPROVE_DRY_RUN=1` for axis-1 auto-approve. The end-to-end automation **Current focus: end-to-end automation** (goal.md L38-151, axis 2 "Failure-recovery operator dependency", L90-100) currently has TB-225's auto-unfreeze loop fully active OR fully off — no monitor-only path. When `AP2_AUTO_UNFREEZE_FIX_SHAPES` is set AND `AP2_AUTO_UNFREEZE_DRY_RUN=1`, `_maybe_auto_unfreeze` (ap2/daemon.py:3137) runs the entire guard chain (allowlist match + per-task cap + per-day cap + briefing-line match) and, instead of calling `_apply_auto_unfreeze_patch`, emits a `would_auto_unfreeze` event with the same payload shape (task, shape, file, line, from, to). The board stays untouched; no operator-queue ops are appended. Operator observes the decisions in `ap2 logs --type would_auto_unfreeze` (and the status-report's `## Automation loop activity` digest from TB-228) for a window, gains confidence, then flips dry-run off.

Why now: TB-225 shipped the auto-unfreeze loop and TB-229 taught the `BriefingFix:` emitter prefix, so the loop is technically ready to deploy — but `_auto_unfreeze_allowlist` (daemon.py:2914) is opt-in only, and an operator's first flip from "unset" to "shapes-listed" runs against the LIVE Frozen set with no monitor-only history. TB-232 closes the symmetric gap for axis 1 by adding `AP2_AUTO_APPROVE_DRY_RUN=1`; without this sibling for axis 2, an operator confident enough to enable BOTH automations has uneven on-ramp surfaces — axis 1 has trust-building, axis 2 is cold-start. The walk-away promise (goal.md L9-16) names automatic failure recovery as a core requirement; an axis-2 path that can only be enabled cold-start is exactly the "every relaxation is operator-curated" pattern goal.md L60-69 calls out as load-bearing. Closes the binary-cliff on-ramp gap for axis 2.

## Scope

1. Add `_auto_unfreeze_dry_run() -> bool` helper in `ap2/daemon.py` near `_auto_unfreeze_allowlist` (daemon.py:2914), parsing `AP2_AUTO_UNFREEZE_DRY_RUN` as a boolean (`"1"` / `"true"` / `"yes"` / case-insensitive → True; anything else / unset → False). Mirror the parse shape of any existing boolean env knob in daemon.py for consistency.
2. In `_maybe_auto_unfreeze` (daemon.py:3137), after all guard skips have been emitted and just before the `_apply_auto_unfreeze_patch` call at line 3248-3250, branch on `_auto_unfreeze_dry_run()`: when True, emit `would_auto_unfreeze` (event payload: `task=task.id`, `shape=fix["shape"]`, `file=fix["file"]`, `line=fix["line"]`, `from_=fix["from"]`, `to=fix["to"]` — same shape as the existing `auto_unfreeze_applied` payload minus the success/skip-reason field), then `continue` to the next Frozen task. The per-day-count counter does NOT increment in dry-run (no real application). The per-task-prior-count does NOT increment either.
3. Add `would_auto_unfreeze` to `ap2/events.py`'s event-type registry (whatever registration mechanism exists for it — match the pattern used by `would_auto_approve` once TB-232 lands, or sibling to existing `auto_unfreeze_applied`).
4. Add `would_auto_unfreeze` to `ap2/ideation.py`'s `IDEATION_RELEVANT_EVENT_TYPES` allowlist (TB-169) so dry-run decisions surface in the ideation prompt's events block.
5. Update `ap2/howto.md`'s env-knobs reference section to document `AP2_AUTO_UNFREEZE_DRY_RUN` alongside `AP2_AUTO_UNFREEZE_FIX_SHAPES` / `AP2_AUTO_UNFREEZE_MAX_PER_TASK` / `AP2_AUTO_UNFREEZE_MAX_PER_DAY`. Single sentence; cross-link to the sibling auto-approve dry-run knob if TB-232 has landed by then.
6. Add `ap2/tests/test_tb233_auto_unfreeze_dry_run.py` with at minimum: (a) `AP2_AUTO_UNFREEZE_DRY_RUN=1` + populated allowlist + Frozen task with a matching `BriefingFix:` summary → asserts `would_auto_unfreeze` event emitted, asserts no `auto_unfreeze_applied` event, asserts briefing file content unchanged, asserts no operator-queue ops appended; (b) dry-run + per-task-cap-reached → asserts the existing `auto_unfreeze_skipped reason=per_task_cap` event still fires AND no `would_auto_unfreeze` for that task (skip wins over dry-run, same precedence as the non-dry-run path); (c) dry-run + per-day-cap-reached → asserts the systemic-regression `## Decisions needed from operator` bullet is NOT appended in dry-run (board/state untouched). Mirror the structural shape of `ap2/tests/test_tb225_auto_unfreeze.py`.
7. The dry-run check happens AFTER all skip-emission so dry-run is always observable in its applies-on-real-run flavor: the operator can see "X tasks would be applied, Y tasks would be skipped for reason Z" in the same window.

## Design

- The dry-run env knob is a runtime check, not a startup gate — daemon doesn't refuse to start when both `AP2_AUTO_UNFREEZE_FIX_SHAPES` and `AP2_AUTO_UNFREEZE_DRY_RUN` are set. Operator flips dry-run on/off mid-run by re-exporting the env and (optionally) restarting the daemon; the loop picks up the new value on the next `_maybe_auto_unfreeze` call (per-tick env read, same as the cap helpers).
- `would_auto_unfreeze` event payload uses `from_` (trailing underscore) for the patched-from-line field — `from` is a Python reserved word and JSONL-emit downstream readers (the ideation events block, the web /events page) tolerate the key-renaming convention used elsewhere in `events.py`. If `from` is already used unrenamed in the existing `auto_unfreeze_applied` event, follow that convention instead — consistency wins over Python-keyword-avoidance.
- Per-day-cap halt logic (`return` at daemon.py:3247 after the bullet append) intentionally short-circuits the WHOLE tick today. In dry-run, this short-circuit semantics is preserved — the per-day-cap halt is itself the right signal that the auto-unfreeze allowlist is generating more applications than the safety floor allows. Dry-run users get the same halt signal pre-flight.
- Default-off: when `AP2_AUTO_UNFREEZE_DRY_RUN` is unset, behavior is byte-identical to today. Existing tests in `test_tb225_auto_unfreeze.py` continue to pass without modification.

## Verification

- `uv run pytest -q ap2/tests/test_tb233_auto_unfreeze_dry_run.py` — new test module exists and all behavioral cases pass.
- `uv run pytest -q ap2/tests/` — full regression suite green (no break in test_tb225_auto_unfreeze.py from the new branch).
- `grep -nE "AP2_AUTO_UNFREEZE_DRY_RUN" ap2/daemon.py ap2/howto.md` — env knob mentioned in both files.
- `grep -nE "would_auto_unfreeze" ap2/daemon.py ap2/events.py ap2/ideation.py` — event type registered + ideation-allowlisted + emitted from daemon.
- `grep -nE "_auto_unfreeze_dry_run\(\)" ap2/daemon.py` — helper exists, at least 2 hits (definition + call site in `_maybe_auto_unfreeze`).
- `ap2/tests/test_tb233_auto_unfreeze_dry_run.py` Prose: the test file contains at least three test functions covering the three behavioral cases named in Scope item 6 (apply-shape case, per-task-cap skip-precedence case, per-day-cap halt+bullet-absence case); judge confirms via Read of the file.

## Out of scope

- `AP2_AUTO_APPROVE_DRY_RUN` work — that's TB-232's scope and is the sibling on the axis-1 side.
- Per-cycle metric aggregation across `would_auto_unfreeze` events into a "% of dry-run decisions that would have been correct" rate — premature without observed data.
- Doctor pre-flight check that warns when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is set but `AP2_AUTO_UNFREEZE_DRY_RUN` is also set (or vice versa) — leave the combination as legitimate operator intent; no auto-suggest path.
