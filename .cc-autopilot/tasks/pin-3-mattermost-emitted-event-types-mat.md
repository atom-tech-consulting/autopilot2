## Goal

Close three of the eight event-type coverage-debt names that TB-208's `ap2/tests/test_coverage_drift.py` docstring (L391-399) explicitly enumerates as a follow-up to TB-208's drift-gate landing. The three mattermost-emitted events — `mattermost_error`, `mattermost_timeout`, `mm_poll_error` — fire from the daemon's MM loop (verified emitter lines: `ap2/daemon.py:747` mattermost_timeout, `:754` mattermost_error, `:2349` mm_poll_error) but the only substring reference under `ap2/tests/` is the comment-block shim itself (`Grep ap2/tests/` returns one file: `test_coverage_drift.py`). The substring drift gate passes by gate-satisfaction shim, not by any real assertion of the emitter contract. Sibling task to TB-211 (daemon-subset event types); same TB-205/TB-210 closure shape. Goal anchor: **Current focus: code quality** (goal.md L38) — Testing coverage axis (goal.md L58-63: "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path").

Why now: TB-208's docstring names this gap as a closed-set follow-up at exactly 8 entries that bifurcate cleanly along emitter module — 3 mattermost-side (this TB) + 5 daemon-side (TB-211). The MM loop's error/timeout/poll-error events are the three signals operators rely on when @claude-bot stops responding — without real assertions, every future refactor of `daemon._mm_loop`, the MM handler dispatch path, or `mattermost.poll(...)` can silently break the emitter contract while the drift gate stays green via the shim. Replacing the shim with real tests closes the testing-axis gap that TB-208 explicitly tagged as "coverage debt, not exemptions" on a high-operator-visibility surface in one shot.

## Scope

- Add `ap2/tests/test_tb212_mm_event_types.py` (or extend an existing MM test module — implementer's call; the substring drift gate doesn't care which file). For each of the three mattermost-emitted events, add at least:
  - One happy-path / branch-trigger test asserting the event fires with the expected payload shape (call the emitter seam with the failure condition, then read `events.jsonl`/the test capture and assert the event type + at least one payload field documented at the emitter site).
  - One additional assertion per event (payload shape pin OR adjacent branch — e.g. that the `mattermost_timeout` event carries the elapsed-time field if present, OR that the handler-task cancellation doesn't double-emit).
- Remove the 3 matching rows from the discovered-at-landing comment block in `ap2/tests/test_coverage_drift.py` (currently L396-398). Leave the 5 daemon-emitted rows untouched — sibling TB-211 closes those.
- Do NOT change the emitter sites' contracts; pure test addition + shim-row removal.

## Design

- Mirror `ap2/tests/test_env_knobs.py` / `test_tb210_env_knobs.py`'s layout: one `def test_<event_name>_<aspect>():` function per assertion, using `tmp_path` for `events_file`, calling the emitter seam (preferred: the daemon helper that wraps the emit), then `events.read_jsonl(events_file)` to assert.
- For `mattermost_timeout` (`daemon.py:747`): emitted when an MM handler SDK call exceeds its per-handler timeout. Stub the SDK call to sleep past the timeout (or use an `asyncio.wait_for` with a tiny timeout against an awaitable that never resolves); assert the event fires.
- For `mattermost_error` (`daemon.py:754`): emitted when an MM handler SDK call raises. Stub the SDK call to raise; assert the event fires with `error=<exc_type>: <msg>` payload (same shape as `cron_error`'s `error` field per architecture.md L240).
- For `mm_poll_error` (`daemon.py:2349`): emitted when the MM poll loop's underlying poll call raises. Stub `mattermost.poll(...)` to raise; assert the event fires once per poll-loop iteration that errors (don't double-emit on retry).
- Test-function naming convention: `def test_<event_name>_<aspect>(...)` — e.g. `test_mattermost_timeout_fires_on_handler_timeout`, `test_mattermost_error_carries_error_field`, `test_mm_poll_error_on_poll_exception`. This convention is what the auto-verify bullets grep for.
- Removing the 3 comment-block rows is a 3-line deletion in `test_coverage_drift.py` L396-398 (keep daemon-subset lines L392-395 + L399). The drift gate continues to pass because the new test module references the event names; the gate's substring check (`name in blob`) doesn't care which file mentions them.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the change (exit 0).
- `uv run pytest -q ap2/tests/test_coverage_drift.py` — drift gate stays green; the 3 events still resolve (now via the new test module, not the comment block).
- `[ "$(grep -hE 'def test_(mattermost_timeout|mattermost_error|mm_poll_error)' ap2/tests/test_tb212_mm_event_types.py ap2/tests/test_mattermost.py ap2/tests/test_daemon_mm.py 2>/dev/null | wc -l)" -ge 3 ]` — at least 3 test functions exist across candidate test files (one happy-path per event minimum), regardless of which file the implementer chose. `2>/dev/null` swallows missing-file errors.
- `[ "$(grep -lE '(mattermost_error|mattermost_timeout|mm_poll_error)' ap2/tests/*.py | wc -l)" -ge 2 ]` — at least 2 test files reference these 3 event names (test_coverage_drift.py + the new/extended module).
- `[ "$(grep -cE '^#\s+- (mattermost_error|mattermost_timeout|mm_poll_error)' ap2/tests/test_coverage_drift.py)" -le 0 ]` — the 3 mattermost comment-block shim rows have been removed from `test_coverage_drift.py` (the `^#  - <name>` line pattern matches the existing shim format at L396-398).
- `grep -q "auto_diagnose_error" ap2/tests/test_coverage_drift.py` — the 5 daemon-emitted rows are deliberately left in the shim (sibling TB-211 closes those); their presence confirms only the mattermost subset was removed.
- Prose: the new tests assert on actual emitter-site behavior — not just synthetic `events.append(...)` calls in test code — for each of the three events. Judge confirms by reading the new test bodies and checking that each test invokes a daemon/MM seam (e.g. `daemon._run_mm_handler`, the MM poll-loop body, or a documented stub-point thereof) rather than directly appending a fake event to the events file and re-reading it.

## Out of scope

- The 5 daemon-emitted event-type rows (`auto_diagnose_error`, `classify_record_unreadable`, `cron_bootstrap`, `cron_error`, `pipeline_pending_sweep_error` — L392-395 + L399) — sibling TB-211 closes those.
- The 12 CLI-verb coverage-debt rows (L401-413) — separate follow-up TBs deferred per this cycle's ideation_state.md.
- Tightening the substring drift gate to AST-walk semantics — TB-208's docstring defers this.
- Live Mattermost server integration — all tests stub the SDK / poll seam; no network IO required.
- Adding new event types OR changing emitter contracts — pure test additions + shim-row removal.
