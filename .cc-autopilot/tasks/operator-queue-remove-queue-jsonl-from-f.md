# Operator queue: remove queue.jsonl from fence; defer CLAUDE.md bump to drain

## Goal

Close the false-positive `task_state_violation` class introduced when an operator runs `ap2 add` (or `unfreeze` / `delete` / `backlog`) while a task agent is mid-flight. TB-139 hit this on 2026-05-01: TB-140 was queued during TB-139's run, the synchronous CLAUDE.md bump and operator_queue.jsonl append both showed up as fenced-file mutations at TB-110's post-hoc hash check, and TB-139 got rolled back despite committing legitimate work.

## Why

Today's TB-131 implementation makes two synchronous writes during `ap2 add`:

1. Bumps `CLAUDE.md` `next_task_id` (so the operator gets the new TB-N printed immediately).
2. Appends a record to `.cc-autopilot/operator_queue.jsonl`.

Both files are in `TASK_AGENT_FENCED_PATHS`. TB-110's violation check hash-snapshots all fenced files at run start and compares at run end; any diff routes the agent to `state_violation` and triggers `git reset --hard <pre_run_head>`.

The check has no way to distinguish operator-driven mutations from agent mutations: same path, same hash diff, same verdict. Result: every `ap2 add` issued during a task run produces a false state_violation on whichever task is currently running.

## Scope

(1) Drop `.cc-autopilot/operator_queue.jsonl` from `TASK_AGENT_FENCED_PATHS` (`tools.py:761`). Justification: agents have no path to mutate the queue file. There's no MCP tool exposing it, no `Edit`/`Write` permission, and even if an agent shells out to `echo >> queue.jsonl`, the daemon wouldn't apply records the agent forged because `do_operator_queue_append` writes records with daemon-issued uuids and the drain-side `operator_queue_state.json` would not match. The fence has zero security value but produces every `ap2 add`-during-run false-positive — pure cost, no benefit.

(2) Defer the `CLAUDE.md` `next_task_id` bump from synchronous (`_allocate_id` writes during `ap2 add`) to drain-time (handler in `_tick`'s queue-drain stage writes once after applying all queued add_* ops in the current drain). Implementation path:

   - `_allocate_id(board, cfg)` becomes pure: returns `max(board_max_id, claude_md_next_id, max_preallocated_id_in_queue) + 1`, no write. The "max_preallocated_id_in_queue" term is computed by reading the queue file's existing records — same scan the drain side already does. That ensures back-to-back `ap2 add` calls allocate sequential IDs without any of them touching CLAUDE.md.
   - `drain_operator_queue` (in `tools.py`) writes CLAUDE.md once at the end of the drain pass, setting `next_task_id` to the highest allocated TB-N + 1. Single write per drain instead of one-per-add.
   - The synchronous side of `ap2 add` still pre-allocates the TB-N (operator UX preserved — `"TB-138 (queued; will land at next tick)"` keeps printing the right number) but does NOT write CLAUDE.md. The number lives in the queue record's `preallocated_task_id` field until drain.

(3) Briefing-file write is similarly synchronous today — leave it that way. Briefings are in `.cc-autopilot/tasks/` which is also fenced, but only briefings the AGENT writes are violations. Operator-written briefings during run aren't; the violation check should already exclude them based on snapshot-after-move_to_active. Verify this assumption holds with a test (see Verification).

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `! grep -qE '^\s*"\.cc-autopilot/operator_queue\.jsonl",\s*$' ap2/tools.py` — confirm queue.jsonl is no longer present as a tuple entry in `TASK_AGENT_FENCED_PATHS`. The anchored regex matches only the exact tuple-line shape (`    ".cc-autopilot/operator_queue.jsonl",`), so it ignores incidental mentions in module docstrings, the `operator_queue_path` path constructor, and explanatory comments — only a real tuple entry trips it.
- New unit test in `test_tools.py`: `_allocate_id` does NOT write CLAUDE.md (open the file pre-call, snapshot mtime, call, mtime unchanged).
- New unit test in `test_tools.py`: two back-to-back `do_operator_queue_append` calls with `add_backlog` allocate sequential TB-N (e.g. TB-100 then TB-101) without any CLAUDE.md mutation; the second call reads the first's preallocated_task_id from the queue file.
- New unit test in `test_tools.py`: `drain_operator_queue` after two queued add_backlog ops correctly bumps CLAUDE.md once at the end (not once per op) to highest_allocated + 1.
- New e2e test in `tests/e2e/test_operator_queue_tick.py` (or similar): seed a fake task in Active, run `do_operator_queue_append({"op":"add_backlog",...})`, take a fenced-files snapshot via `rollback.snapshot_fenced_files`, assert `detect_fenced_violations` returns `[]` — i.e., the operator add did NOT trigger a violation against the snapshot.
- New e2e regression: simulate the TB-139 scenario — a task is in flight (snapshot taken), operator runs `ap2 add`, task completes without any agent-side fenced-file write; assert `task_state_violation` does NOT fire.
- The diff updates the docstrings and comments in `daemon.py` / `tools.py` that previously claimed the queue file is fenced. Pin via grep that the comments match the new reality.

## Out of scope

- TB-120's kernel-level fence (still frozen; this is the cheaper application-layer fix).
- Generalizing the violation check to "ignore any operator-authored mutation" — would require tracking authorship per file change, which is more complex than just narrowing the fenced list.
- Auto-detecting that the agent forged a queue record (the design relies on uuid + state-file matching; a forged record wouldn't drain). If forgery becomes a real concern, file separately.
## Attempts

### 2026-05-01 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `! grep -qE "operator_queue\\.jsonl" ap2/tools.py | head -50` covering the fenced-paths tuple — confirm queue.jsonl is n
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260501T025509Z-TB-141.prompt.md`, `stream: .cc-autopilot/debug/20260501T025509Z-TB-141.stream.jsonl`, `messages: .cc-autopilot/debug/20260501T025509Z-TB-141.messages.jsonl`
