## Goal

Close five of the eight event-type coverage-debt names that TB-208's `ap2/tests/test_coverage_drift.py` docstring (L391-399) explicitly enumerates as a follow-up to TB-208's drift-gate landing. The five daemon-emitted events — `auto_diagnose_error`, `classify_record_unreadable`, `cron_bootstrap`, `cron_error`, `pipeline_pending_sweep_error` — fire from `ap2/daemon.py` (verified emitter lines: 961/996/2169/2349/2401/2411/2494/2659 cross-reference and `tools.py:2659`) but the only substring reference under `ap2/tests/` is the comment-block shim itself (`Grep ap2/tests/` returns one file: `test_coverage_drift.py`). The substring drift gate passes by gate-satisfaction shim, not by any real assertion of the emitter contract. Direct mirror of TB-205's approved pattern (4 SDK-cost env knobs → focused per-name parse/default/override + invalid contract tests) and TB-210's closure shape (4 env knobs + comment-block shim removal). Goal anchor: **Current focus: code quality** (goal.md L38) — Testing coverage axis (goal.md L58-63: "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path"; the event-type axis is the symmetric companion that TB-208's drift gate brought under coverage).

Why now: TB-208's docstring names this gap as a closed-set follow-up at exactly 8 entries that bifurcate cleanly along emitter module — 5 daemon-side (this TB) + 3 mattermost-side (sibling TB). Without real assertions, every future refactor of `daemon._run_cron_job` (emits `cron_error`/`cron_bootstrap`), `daemon._auto_diagnose` (emits `auto_diagnose_error`), `tools.do_classify` (emits `classify_record_unreadable`), or `daemon._pipeline_pending_sweep` (emits `pipeline_pending_sweep_error`) can silently break the emitter contract while the drift gate stays green via the shim. Replacing the shim with real tests closes the testing-axis gap that TB-208 explicitly tagged as "coverage debt, not exemptions" (`test_coverage_drift.py` L416-419) and removes a structurally-fragile gate-satisfaction shortcut in one shot.

## Scope

- Add `ap2/tests/test_tb211_event_types.py` (or extend an existing event-shape test module — implementer's call; the substring drift gate doesn't care which file). For each of the five daemon-emitted events, add at least:
  - One happy-path test asserting the event fires with the expected payload shape (call the emitter site or its closest seam, then read `events.jsonl`/the test capture and assert the event type + at least one payload field documented at the emitter site).
  - One error / branch-coverage test (e.g. `cron_error` carries `stderr_tail`/`prompt_dump` paths per architecture.md L240; `classify_record_unreadable` fires when the record file is malformed JSON — pin that the malformed-input branch emits the event rather than crashing the caller).
- Remove the 5 matching rows from the discovered-at-landing comment block in `ap2/tests/test_coverage_drift.py` (currently L392-395 + L399). Leave the 3 mattermost-emitted rows untouched — separate sibling TB closes those.
- Do NOT change the emitter sites' contracts; this is pure test addition + shim-row removal.

## Design

- Mirror `ap2/tests/test_env_knobs.py` / `test_tb210_env_knobs.py`'s layout: one `def test_<event_name>_<aspect>():` function per happy-path / error-path bullet, using `tmp_path` for `events_file`, calling `events.append(...)` indirectly via the public emitter seam (preferred: invoke the daemon helper that wraps the emit), then `events.read_jsonl(events_file)` to assert.
- For `cron_bootstrap`: emitted from `daemon.py:2169` during first-run cron.yaml seed. Use the existing test harness pattern (see `test_daemon_recovery.py` for daemon-state seam tests). Assert the event type and `path=<cron.yaml path>` payload.
- For `cron_error`: emitted at `daemon.py:961`/`996`/`2401` with `error=<exc_type>: <msg>` payload. Trigger via a stub SDK call that raises; assert the event fires once with `error` field populated. The `stderr_tail` / `prompt_dump` fields are auxiliary — at minimum pin the `error` field shape.
- For `auto_diagnose_error`: emitted at `daemon.py:2494` when the auto-diagnose path raises. Stub `diagnose.run` to raise; assert the event fires.
- For `classify_record_unreadable`: emitted at `tools.py:2659` when a classify record's JSON parse fails. Write a malformed `.cc-autopilot/ideation_proposals/<TB-N>.json` file; invoke `do_classify(...)` or the classify-record loader; assert the event fires and the caller doesn't crash.
- For `pipeline_pending_sweep_error`: emitted at `daemon.py:2411` when the pipeline-pending sweep raises. Stub the sweep target to raise; assert the event fires.
- Test-function naming convention: `def test_<event_name>_<aspect>(...)` — e.g. `test_cron_bootstrap_fires_on_first_run`, `test_cron_error_carries_error_field`, `test_classify_record_unreadable_on_malformed_json`. This convention is what the auto-verify bullets grep for.
- Removing the 5 comment-block rows is a 5-line deletion in `test_coverage_drift.py` L392-395 + L399 (keep mattermost lines L396-398). The drift gate continues to pass because the new test module references the event names; the gate's substring check (`name in blob`) doesn't care which file mentions them.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes after the change (exit 0).
- `uv run pytest -q ap2/tests/test_coverage_drift.py` — drift gate stays green; the 5 events still resolve (now via the new test module, not the comment block).
- `[ "$(grep -hE 'def test_(auto_diagnose_error|classify_record_unreadable|cron_bootstrap|cron_error|pipeline_pending_sweep_error)' ap2/tests/test_tb211_event_types.py ap2/tests/test_events.py ap2/tests/test_daemon_events.py 2>/dev/null | wc -l)" -ge 5 ]` — at least 5 test functions exist across candidate test files (one happy-path per event minimum), regardless of which file the implementer chose. `2>/dev/null` swallows missing-file errors.
- `[ "$(grep -lE '(auto_diagnose_error|classify_record_unreadable|cron_bootstrap|cron_error|pipeline_pending_sweep_error)' ap2/tests/*.py | wc -l)" -ge 2 ]` — at least 2 test files reference these 5 event names (test_coverage_drift.py + the new/extended module).
- `[ "$(grep -cE '^#\s+- (auto_diagnose_error|classify_record_unreadable|cron_bootstrap|cron_error|pipeline_pending_sweep_error)' ap2/tests/test_coverage_drift.py)" -le 0 ]` — the 5 comment-block shim rows have been removed from `test_coverage_drift.py` (the `^#  - <name>` line pattern matches the existing shim format at L392-399).
- `grep -q "mattermost_error" ap2/tests/test_coverage_drift.py` — the 3 mattermost-emitted rows are deliberately left in the shim (sibling TB closes those); their presence confirms only the daemon subset was removed.
- Prose: the new tests assert on actual emitter-site behavior — not just synthetic `events.append(...)` calls in test code — for each of the five events. Judge confirms by reading the new test bodies and checking that each test invokes a daemon/tools seam (e.g. `daemon._run_cron_job`, `tools.do_classify`, `daemon._auto_diagnose`, `daemon._pipeline_pending_sweep`) or a documented stub-point thereof, rather than directly appending a fake event to the events file and re-reading it.

## Out of scope

- The 3 mattermost-emitted event-type rows (`mattermost_error`, `mattermost_timeout`, `mm_poll_error` — L396-398) — sibling TB closes those.
- The 12 CLI-verb coverage-debt rows (L401-413) — separate follow-up TBs deferred per this cycle's ideation_state.md.
- Tightening the substring drift gate to AST-walk semantics — TB-208's docstring defers this until the substring gate is observed missing a real pro-forma gap.
- Adding new event types OR changing emitter contracts — pure test additions + shim-row removal; no source-of-truth changes.
- Refactoring `test_env_knobs.py` / `test_tb210_env_knobs.py` — leave untouched.
## Attempts

### 2026-05-14 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] Prose: the new tests assert on actual emitter-site behavior — not just synthetic `events.append(...)` calls in test code
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260514T013558Z-TB-211.prompt.md`, `stream: .cc-autopilot/debug/20260514T013558Z-TB-211.stream.jsonl`, `messages: .cc-autopilot/debug/20260514T013558Z-TB-211.messages.jsonl`
