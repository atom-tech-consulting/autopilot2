# Re-fence operator_queue.jsonl; exclude from violation check

## Goal

Refine TB-141. TB-141 dropped `.cc-autopilot/operator_queue.jsonl` from `TASK_AGENT_FENCED_PATHS` entirely to fix the false-positive `task_state_violation` class (operator writes during a task run mutated the file and triggered TB-110's post-hoc hash check). That worked, but it conflated two distinct purposes the fenced list serves:

1. **Defense layers** — prompt header tells agents "don't write here"; SDK `disallowed_tools` adds `Edit(<path>)`/`Write(<path>)` to the reject list.
2. **TB-110 post-hoc snapshot check** — hash-compare these files at run end; any diff → `state_violation` + rollback.

`operator_queue.jsonl` shouldn't trigger (1)→(2)'s combination only because (2) false-positives. The fix is the two-tier separation that already exists for `events.jsonl`: keep it in the defense list, exclude it from the snapshot-check subset.

## Why

`events.jsonl` already follows this pattern. `FENCED_PATHS_FOR_VIOLATION_CHECK` is defined (`rollback.py:53-55`) as `TASK_AGENT_FENCED_PATHS minus events.jsonl` because events.jsonl is meant to grow during task runs (daemon appends to it) and a snapshot diff would always fire. The two-tier model is the right shape; TB-141 just chose the simpler one-list path.

`operator_queue.jsonl` is in the exact same bucket: daemon/operator-write-only, agents have no path to it, but its writes during task runs are expected. Generalizing the violation-check exclusion list to cover both files restores defense-in-depth (well-behaved agents see "don't write here" in the prompt header + SDK rejects `Edit`) without re-introducing the false-positive.

## Scope

(1) `tools.py:TASK_AGENT_FENCED_PATHS`: add `.cc-autopilot/operator_queue.jsonl` back into the tuple. Keep the comment block but rewrite to reflect the new design (defense layers apply, violation check exempt — same as events.jsonl).

(2) `rollback.py:FENCED_PATHS_FOR_VIOLATION_CHECK`: generalize the exclusion list to include both `events.jsonl` and `operator_queue.jsonl`. A small constant `_VIOLATION_CHECK_EXCLUDED_PATHS = ("events.jsonl", "operator_queue.jsonl")` keeps the rationale explicit.

(3) Update the prose in `prompts._TASK_HEADER` (or wherever the fenced-files reminder for task agents lives) so `operator_queue.jsonl` is listed alongside the other don't-touch paths.

(4) Update the SDK `disallowed_tools` set in `daemon.py:run_task` so `Edit(operator_queue.jsonl)` and `Write(operator_queue.jsonl)` are rejected the same way as the other fenced paths.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `python3 -c "from ap2.tools import TASK_AGENT_FENCED_PATHS; assert '.cc-autopilot/operator_queue.jsonl' in TASK_AGENT_FENCED_PATHS"` — operator_queue.jsonl back in `TASK_AGENT_FENCED_PATHS`.
- `python3 -c "from ap2.rollback import FENCED_PATHS_FOR_VIOLATION_CHECK; assert '.cc-autopilot/operator_queue.jsonl' not in FENCED_PATHS_FOR_VIOLATION_CHECK"` — excluded from violation-check (alongside events.jsonl).
- New unit test in `test_rollback.py`: `FENCED_PATHS_FOR_VIOLATION_CHECK` does NOT contain `.cc-autopilot/operator_queue.jsonl`. Pins the exclusion alongside events.jsonl.
- New unit test in `test_rollback.py`: simulating an operator writing a fresh record to `operator_queue.jsonl` between `snapshot_fenced_files` and `detect_fenced_violations` returns an empty violation list (regression — TB-139 scenario).
- New unit test in `test_tools.py`: `TASK_AGENT_FENCED_PATHS` DOES contain `.cc-autopilot/operator_queue.jsonl`. Pins the defense-layer presence.
- New unit test in `test_prompts.py`: the fenced-files reminder rendered into the task-agent prompt mentions `operator_queue.jsonl`.
- New unit test in `test_daemon_recovery.py` (or wherever the SDK options are pinned): the task-agent SDK call's `disallowed_tools` includes `Edit(.cc-autopilot/operator_queue.jsonl)` and `Write(.cc-autopilot/operator_queue.jsonl)`.

## Out of scope

- Any change to the existing TB-141 fix for CLAUDE.md (deferred bump still applies — that's a separate problem from defense layering).
- TB-120's kernel-level fence (still frozen; this is application-layer refinement only).
- Adding more files to the violation-check exclusion list speculatively — only events.jsonl and operator_queue.jsonl have the "expected to grow during runs" property today.
## Attempts

### 2026-05-01 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -qE '"\\.cc-autopilot/operator_queue\\.jsonl"' ap2/tools.py` — operator_queue.jsonl back in `TASK_AGENT_FENCED_PAT; [fail] `grep -qE 'operator_queue\\.jsonl' ap2/rollback.py` — explicitly named in the violation-check exclusion list (alongside 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260501T040058Z-TB-143.prompt.md`, `stream: .cc-autopilot/debug/20260501T040058Z-TB-143.stream.jsonl`, `messages: .cc-autopilot/debug/20260501T040058Z-TB-143.messages.jsonl`
