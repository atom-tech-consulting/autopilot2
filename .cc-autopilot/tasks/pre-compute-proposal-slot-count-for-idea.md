# Pre-compute proposal slot count for ideation, eliminate hardcoded "3" from prompt body

## Goal

This task is anchored in goal.md's `## Current focus: ideation quality` section. The "structural guards are now in place" sentence credits TB-121 (review gate), TB-138 (auto-verifiable bullets), and TB-154 (canonical structural validator) as the mechanical scaffolding; this task closes one specific drift gap in that scaffolding — a hardcoded magic number in the ideation prompt body that should be derived from the env knob instead.

Today's `ap2/ideation.default.md:21` says verbatim: "Propose new tasks ONLY if Backlog has fewer than 3 workable items." The number `3` is hardcoded. Meanwhile TB-160 introduced `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3) which controls the DAEMON-side gate (whether ideation fires at all). Two surfaces, same number, no shared source — and the operator's `.cc-autopilot/env` for autopilot2 currently has `AP2_IDEATION_TRIGGER_TASK_COUNT=5`. The daemon fires when Ready+Backlog count is below 5; the prompt then tells the agent to propose only if Backlog is below 3. **The drift is live today.** The agent reads "fewer than 3" and self-throttles even when the operator's env knob authorizes proposals up to 5 items.

This task moves the threshold to a single source of truth (the env knob via the existing `_trigger_task_count()` helper), pre-computes the per-cycle proposal-slot count in the daemon, passes it via the existing `state_extras` mechanism (TB-151, already used by TB-163 for the rejections block) into the snapshot header, and updates the ideation prompt body to read the slot count from the snapshot rather than hardcoding "3."

Why now: the drift is actively biting — the operator just bumped the env knob to 5 specifically to give ideation more re-evaluation chances against rejection-reason signal (per the env file's inline comment), but the prompt's hardcoded "<3" silently overrides that intent. Filing this fix now restores the operator's intended behavior and prevents future env-knob tuning from drifting away from prompt behavior.

## Scope

- `ap2/ideation.py::_maybe_ideate` — after the existing gate checks pass and before `build_control_prompt` is called, compute `slots = max(0, _trigger_task_count() - workable_count)`. Pass into `build_control_prompt` via the `state_extras` kwarg (the same mechanism TB-151 introduced for status-report and TB-163 used for rejections). Format as a single line: `- proposal slots this cycle: N` (where N is the integer slot count).
- `ap2/ideation.py` — if `slots <= 0`, return early without invoking the SDK (cost-saving — there's no point firing ideation if the agent has zero slots to fill). Emit a new `ideation_skipped_no_slots` event so the operator can see the skip in events.jsonl. This is a small additive optimization on top of the existing gate logic.
- `ap2/ideation.default.md` — replace the line at the top of the file (currently "Propose new tasks ONLY if Backlog has fewer than 3 workable items.") with a reference to the slot count from the snapshot block. Concrete replacement: "Propose at most N new tasks this cycle, where N is the `proposal slots this cycle` value in the `## Current state` block above. If N is 0, do not propose any tasks (the queue is already at the operator's configured threshold)."
- `ap2/tests/test_ideation*.py` — pin the slot-count math, the state_extras forwarding, the early-skip path with the new event, and the absence of the hardcoded "fewer than 3" phrase in the prompt body.

## Design

### Why state_extras and not a new prompt-assembly arg

The `state_extras` mechanism in `build_control_prompt` (`ap2/prompts.py:482`) was deliberately designed as a list of pre-formatted strings appended inside the `## Current state` snapshot block. It already serves two consumers (status-report's pending-review line via TB-151; ideation's rejections block via TB-163). Adding a third consumer (proposal slots) keeps the architectural surface stable — no new kwargs, no new prompt sections.

The slot count joins the existing snapshot fields (`now:`, optionally `board:`, optionally `recent commits:` post-TB-168) under the same `## Current state` header. The agent reads it as a single line in a familiar location.

### Why daemon-side early-skip when slots=0

The existing gate (`workable >= _trigger_task_count()` → return per TB-160) handles the typical case. But there's a subtle window where the cooldown elapses AND `workable` is below threshold AND another tick stage adds a task to Ready/Backlog AND the `_maybe_ideate` then proceeds anyway (the gate snapshot was taken earlier in the same tick). Re-checking the slot count at the moment of prompt assembly catches this race; emitting the early-skip event makes the no-op visible.

Concrete numbers: with `AP2_IDEATION_TRIGGER_TASK_COUNT=5` and a board of `1A / 2R / 2B / 0P / ...`, slots = max(0, 5 - 4) = 1 — fire ideation, ask the agent to propose at most 1. With board `0A / 2R / 3B`, slots = max(0, 5 - 5) = 0 — skip.

### Why the prompt body uses "at most N" not "fewer than N"

The hardcoded "fewer than 3" framing in today's prompt is a daemon-side gate phrasing leaking into agent guidance. The agent doesn't decide whether to fire — the daemon already decided that by getting to this prompt. The agent decides HOW MANY to propose, given the slot count the daemon allocated. "Propose at most N" matches the agent's actual job.

### Backwards compatibility

- Existing `AP2_IDEATION_TRIGGER_TASK_COUNT` env knob unchanged — same env var, same default 3, same parsing.
- Existing daemon gate unchanged — the early-skip is additive.
- Existing prompt body sections (everything after the new slot-count instruction) unchanged.
- `state_extras` kwarg shape unchanged — just gains a third caller.
- ideation_state.md schema unchanged — the slot count is read by the agent at proposal time, not persisted to the assessment file.

### Why this is goal-aligned

Goal.md's "Current focus: ideation quality" Push-for-progress section says: "When two paths to the same outcome exist, pick the one that creates fewer follow-up tasks, not the one that's faster to execute." The hardcoded magic-3 is exactly the kind of follow-up-creating debt this task removes — every future env-knob adjustment without this fix would require a prompt edit to keep them in sync. One source of truth = no follow-up tasks for future env tuning.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes.
- `grep -nE "fewer than 3 workable|fewer than [0-9]+ workable" ap2/ideation.default.md` — should return ZERO matches (validates the hardcoded magic number is gone).
- `grep -nE "proposal slots this cycle" ap2/ideation.default.md ap2/ideation.py` — present in BOTH the prompt body (the instruction referencing the snapshot value) AND `ap2/ideation.py` (the daemon's state_extras line construction).
- `grep -nE "ideation_skipped_no_slots" ap2/ideation.py` — new event type is wired in the early-skip path.
- prose: a test in `test_ideation*.py` synthesizes a fixture board with 2 Ready + 1 Backlog (`workable=3`) and `AP2_IDEATION_TRIGGER_TASK_COUNT=5`, calls `_maybe_ideate` with a stubbed SDK that captures the prompt sent; asserts (a) the captured prompt's `## Current state` block contains `- proposal slots this cycle: 2` (5 - 3 = 2), AND (b) the prompt body references "at most N" or "proposal slots this cycle" (not the hardcoded `3`).
- prose: a test pins the early-skip path — fixture with workable=5 and threshold=5 (slots=0); calls `_maybe_ideate`; asserts (a) the SDK is NOT invoked (capture spy), (b) an `ideation_skipped_no_slots` event lands in events.jsonl, (c) `mark_run` is called so the cooldown advances normally.
- prose: a test pins the no-double-decrement edge case — workable=6 and threshold=5 (slots = max(0, -1) = 0); same skip-with-event behavior as above. The `max(0, ...)` guard prevents negative slot counts.
- prose: a test pins backwards-compat — when `AP2_IDEATION_TRIGGER_TASK_COUNT` is unset (default 3) AND workable=0, slots=3, prompt receives "proposal slots this cycle: 3" — i.e., the default behavior matches today's hardcoded prompt instruction.

## Out of scope

- Changing the daemon-side gate logic itself (workable < threshold). TB-160 owns that; this task is purely about plumbing the slot count into the prompt and tightening the early-skip case.
- Allowing per-task-kind slot counts (e.g., 3 slots for #fix-briefing tasks but 1 slot for greenfield). Single slot count is enough; ideation's ranking logic already handles task-kind prioritization.
- Persisting slot-count history into ideation_state.md. The slot count is per-cycle context, not cross-cycle memory; no need to write it to the assessment file.
- Updating MM handler / cron / status-report prompts to receive a slot count. Those don't propose new tasks and don't need the field.
- Adding a separate `AP2_IDEATION_PROPOSAL_CAP` env knob distinct from `AP2_IDEATION_TRIGGER_TASK_COUNT`. The two semantics (when to fire vs. how many to propose) are conflated by design — same threshold, different views. Splitting them is over-design for v1.
- Surfacing the slot count in `ap2 status` or web home. The metric is internal-to-ideation; operators don't need a separate view.
- Backfilling old `ideation_state.md` files. Next ideation cycle reads the new prompt; no migration step.
- Renaming the `state_extras` kwarg to better reflect its growing list of consumers. Naming consistency is fine; rename if a fourth consumer makes the name confusing.
