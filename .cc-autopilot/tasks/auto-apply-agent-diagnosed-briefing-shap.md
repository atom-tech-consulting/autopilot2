# Auto-apply agent-diagnosed briefing-shape fixes from `task_complete blocked` summaries; operator-curated allowlist via `AP2_AUTO_UNFREEZE_FIX_SHAPES` (axis 2 failure-recovery)

Tags: `#autopilot` `#automation` `#operator-surface` `#failure-recovery` `#regression-pin`

## Goal

Advance **Current focus: end-to-end automation** axis 2 ("Failure-recovery operator dependency", goal.md L90-100) by self-healing the recurring class of retry-exhausted Frozen tasks whose root cause is a briefing-shape regression the agent has already diagnosed in its `task_complete blocked` summary. Goal.md L92-100 names two concrete in-codebase examples ("TB-204's `grep -lE` → `grep -rlE`, TB-207's literal-backtick in shell bullets") where the agent self-diagnosed the briefing-shape fix; the daemon could auto-apply allowlisted shapes and re-dispatch without operator-manual `ap2 unfreeze`. The operator-curated allowlist `AP2_AUTO_UNFREEZE_FIX_SHAPES` names exactly which fix-shapes the daemon is trusted to apply; unknown shapes still require operator review.

Why now: the prior code-quality focus surfaced two real instances of this self-healing-eligible failure shape in a single week (TB-204, TB-207 — both hit briefing-shape pitfalls already catalogued in CLAUDE.md's "Shell-bullet pitfalls to AVOID" section). Goal.md's axis-2 delete-test (L98-100): "if this work didn't ship, every briefing-shape regression cascades into operator-manual unfreeze; with it, the loop self-heals on the recurring class." The class is well-defined, the agent already self-diagnoses, the briefing edit is mechanical. Without this, every recurrence remains an interruption; with it, the walk-away envelope expands by the rate at which briefing-shape regressions arrive (observably non-zero on this codebase).

## Scope

(1) **New env knob `AP2_AUTO_UNFREEZE_FIX_SHAPES`** (default unset = no auto-unfreeze):
  - Comma-separated allowlist of fix-shape tokens the operator trusts the daemon to auto-apply. Recommended bootstrap list (documented in howto, NOT hardcoded): `grep_missing_r_on_dir,bare_python_to_uv_run,literal_backtick_in_shell_bullet,bare_path_to_test_f` (each corresponds to a known pitfall in CLAUDE.md's "Shell-bullet pitfalls to AVOID" section). Default unset means the feature is opt-in; no auto-unfreezing happens without explicit operator opt-in.
  - Read at freeze-handling time in `ap2/daemon.py`; cached once per tick.

(2) **Detection: parse `task_complete status=blocked` summary for a structured fix-shape mention**:
  - New helper `parse_blocked_summary_fix_shape(summary: str) -> dict | None` in `ap2/_shared.py` (extends the established `_shared.py` reuse pattern from TB-217 / TB-218 / TB-220).
  - Returns `{"shape": <token>, "from": <pattern>, "to": <replacement>, "file": <briefing_path>, "line": <int>}` when the agent's summary names one of the known shapes via the canonical structured prefix `BriefingFix: <shape> at <briefing_path>:<line>: <from> -> <to>` (the agent contract). Else `None`.
  - Agent-side: extend `ap2/howto.md`'s agent-self-diagnosis guidance with the canonical `BriefingFix:` prefix format so future task-agent runs emit it consistently. The parser only consumes what the agent emits — no regex-on-prose guessing.

(3) **Apply: daemon edits the briefing file + re-dispatches**:
  - In `ap2/daemon.py`'s freeze handling, when ALL of (a) `AP2_AUTO_UNFREEZE_FIX_SHAPES` is set, (b) the agent's blocked summary parses to a fix-shape in the allowlist, (c) the briefing file's named line literally matches the agent-claimed `from` pattern — the daemon writes the patched briefing via the operator-queue `update` op (TB-153 pattern; routes through the same queue-drain path operator edits use), then re-dispatches the task by moving it back to Backlog.
  - Mismatch on (c) means the agent's diagnosis is stale (e.g. the briefing was operator-edited mid-failure): halt with `auto_unfreeze_skipped reason=briefing_mismatch task=<TB-N>` event and leave the task Frozen for operator review.

(4) **Safety caps**:
  - `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default `1`): max number of auto-unfreeze attempts per task before fallback to manual `ap2 unfreeze`. Prevents oscillation when the patched briefing still fails.
  - `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default `3`): rolling 24h cap on total auto-unfreeze applications across all tasks. When exceeded, halt + emit a decisions-needed entry naming the cap-breach so operator sees a systemic-regression signal not a silent burn.

(5) **Audit events** (registered in `ap2/events.py` event-type list per the TB-208 / TB-211 / TB-212 drift-gate pattern):
  - `auto_unfreeze_applied task=<TB-N> shape=<token> from=<pat> to=<pat>` on success.
  - `auto_unfreeze_skipped task=<TB-N> reason=<token>` on any guarded skip (`briefing_mismatch`, `shape_not_in_allowlist`, `per_task_cap`, `per_day_cap`, `knob_unset`).

(6) **Documentation**:
  - Extend the `## Operator-in-the-loop relaxations` section in `ap2/howto.md` (introduced by TB-223 (5)) with the three new knobs + the canonical `BriefingFix:` agent-prefix format + the recommended bootstrap allowlist + the safety-cap defaults.
  - Add `AP2_AUTO_UNFREEZE_FIX_SHAPES`, `AP2_AUTO_UNFREEZE_MAX_PER_TASK`, `AP2_AUTO_UNFREEZE_MAX_PER_DAY` rows to `ap2/tests/test_docs_drift.py`'s env-knob registry (TB-203 pattern).
  - Add the three new knobs + the two new event types to `ap2/tests/test_coverage_drift.py`'s test-presence registry; this task's own tests satisfy the gate without shim rows (TB-208 / TB-210 pattern).

(7) **Not in this task**:
  - Auto-recovery on non-briefing-shape failures (flaky tests, transient API errors, environmental issues) — out of scope; allowlist is intentionally narrow to briefing-shape mechanical fixes.
  - Auto-creation of follow-up tasks for diagnosed-but-NOT-allowlisted shapes — operator decides; no daemon-side proliferation of remediation tasks.
  - Modifying the verifier's prose-vs-shell classifier — orthogonal (TB-219 covered that axis).
  - Multi-line briefing patches — allowlist is intentionally single-line replacements; multi-line patches are a separate, higher-blast-radius shape.
  - Auto-promotion of new shapes from observed-in-the-wild patterns — operator opens new shapes by editing the env-knob string; daemon never invents fix shapes.

## Design

Why operator-curated allowlist vs. heuristic detection: arbitrary briefing edits by the daemon are blast-radius-unsafe. The allowlist lets the operator audit each fix-shape and approve it specifically; the env-knob string is the trust contract. New shapes can be added without code changes (just edit the env value); shapes can be removed instantly if one misfires.

Why structured agent prefix (`BriefingFix:`): regex-on-prose recurs as a brittle pattern in this codebase (TB-119 / TB-121 history of board_malformed_line from prose-regex collisions). The structured prefix is parser-cheap (line-anchored substring + small structured tail) and agent-prompt-cheap (one section in howto.md teaches the format). Mismatch / malformed-prefix cases fall through to `None` and behave as if the agent didn't diagnose anything — operator-manual unfreeze, identical to today.

Why the line-literal match check (3c): the agent's diagnosis may be stale if the briefing was operator-edited between failure and freeze-handling (e.g. the operator hand-edited the briefing trying to fix it themselves). Verifying the `from` pattern is literally present on the named line before patching closes the data-race window. Mismatch emits `briefing_mismatch` and leaves the task Frozen — fail-safe.

Why per-task cap default 1, per-day cap default 3: a single auto-unfreeze on a task is the typical recurrence pattern (briefing shape fixed once, task succeeds on retry); >1 indicates the patched form ALSO failed and the operator should see it. Per-day cap=3 bounds the worst-case "systemic regression cascades through 10 tasks before operator notices" failure mode. Both caps are operator-tunable for trust-upgrade.

Why route the patch through the operator-queue `update` op (TB-153) rather than direct file-write: operator-queue is the single authoritative path for any board / briefing mutation between ticks (TB-131 lineage). Going through the queue means the patch lands in `operator_log.md` (audit trail), commits atomically with the re-dispatch, and survives the same rollback paths operator-applied updates do.

Independence from TB-223: this task touches the freeze-handling path; TB-223 touches the auto-promote path. Their code paths are disjoint. This task can land before, after, or alongside TB-223 — no @blocked dependency.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0) after the change.
- `grep -nE "AP2_AUTO_UNFREEZE_FIX_SHAPES" ap2/daemon.py` — fix-shapes allowlist knob is read in the freeze-handling path.
- `grep -nE "AP2_AUTO_UNFREEZE_MAX_PER_TASK" ap2/daemon.py` — per-task cap knob is read.
- `grep -nE "AP2_AUTO_UNFREEZE_MAX_PER_DAY" ap2/daemon.py` — per-day cap knob is read.
- `grep -nE "parse_blocked_summary_fix_shape" ap2/_shared.py` — helper exists in the shared module.
- `grep -nE "parse_blocked_summary_fix_shape" ap2/daemon.py` — helper is consumed from daemon.
- `grep -nE "auto_unfreeze_applied|auto_unfreeze_skipped" ap2/events.py` — both new event types registered.
- `grep -nE "BriefingFix:" ap2/howto.md` — canonical agent prefix documented in howto.
- `grep -nE "AP2_AUTO_UNFREEZE_FIX_SHAPES" ap2/howto.md` — fix-shapes knob documented.
- `grep -rnE "parse_blocked_summary_fix_shape" ap2/tests/` — at least one test references the parser helper.
- `[ "$(grep -rlE 'AP2_AUTO_UNFREEZE_FIX_SHAPES' ap2/tests/ | wc -l)" -ge 1 ]` — at least one test file references the allowlist knob.
- `[ "$(grep -rlE 'auto_unfreeze_applied' ap2/tests/ | wc -l)" -ge 1 ]` — at least one test references the success event.
- `[ "$(grep -rlE 'auto_unfreeze_skipped' ap2/tests/ | wc -l)" -ge 1 ]` — at least one test references the skip event.
- Prose: new tests cover at minimum seven behavioral pinning cases — (a) unset allowlist = no auto-unfreeze; (b) allowlisted shape + structured `BriefingFix:` prefix + briefing-line-literal-match = patch applied + `auto_unfreeze_applied` event + task re-dispatched; (c) allowlisted shape + briefing-line-mismatch = skip with `briefing_mismatch` reason + task stays Frozen; (d) non-allowlisted shape = skip with `shape_not_in_allowlist` reason; (e) per-task cap exceeded = skip with `per_task_cap` reason + fallback to manual unfreeze; (f) per-day cap exceeded = halt with `per_day_cap` reason + decisions-needed entry; (g) malformed / missing `BriefingFix:` prefix = parser returns `None` + behaves identically to today's manual-unfreeze path. Judge confirms via `Read` of new test files.
- Prose: `ap2/howto.md` documents the four canonical bootstrap shapes (`grep_missing_r_on_dir`, `bare_python_to_uv_run`, `literal_backtick_in_shell_bullet`, `bare_path_to_test_f`) with their from/to patterns AND the canonical `BriefingFix: <shape> at <path>:<line>: <from> -> <to>` agent prefix format. Judge confirms via `Read` of the howto section.

## Out of scope

- Auto-recovery on non-briefing-shape failures (flaky tests, transient API errors, environmental issues).
- Auto-creation of follow-up tasks for diagnosed-but-NOT-allowlisted shapes — operator decides.
- Modifying the verifier's prose-vs-shell classifier — orthogonal (TB-219 covered).
- Multi-line briefing patches — single-line replacements only.
- Auto-promotion of new fix shapes from observed-in-the-wild patterns — operator opens new shapes via env-knob edit.
- A `ap2 auto-unfreeze` CLI verb — env-only matches the same pattern as TB-223's `AP2_AUTO_APPROVE`.
- Cross-task fix-shape generalization (applying a fix learned from TB-A's failure to TB-B's similar briefing) — out of scope; each task's fix is applied to its own briefing only.
