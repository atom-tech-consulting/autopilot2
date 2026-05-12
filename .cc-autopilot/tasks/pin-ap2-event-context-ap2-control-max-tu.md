# TB-205 — Pin four untested SDK-cost-shaping env knobs with happy + error path tests

Tags: `#autopilot` `#tests` `#code-quality` `#env-knobs` `#sdk-cost`

## Goal

Close one concrete instance of the testing-coverage failure mode goal.md L58-63 names — "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path" — on the new **current focus: code quality** focus's (1) **Testing coverage** axis. Four env knobs (`AP2_EVENT_CONTEXT`, `AP2_CONTROL_MAX_TURNS`, `AP2_IDEATION_MAX_TURNS`, `AP2_AGENT_MODEL`) currently have ZERO references in `ap2/tests/`; each affects either SDK token cost or agent behavior, so a future refactor could silently break the parse/default/override contract without any test signal.

Why now: ran `grep -rE "AP2_EVENT_CONTEXT|AP2_CONTROL_MAX_TURNS|AP2_IDEATION_MAX_TURNS|AP2_AGENT_MODEL" ap2/tests/` → empty. Compare with `AP2_AGENT_EFFORT` (covered in test_status_report_skip.py L1211-1334, test_verify_retry_diff.py L685-815 with explicit happy/override/invalid paths) and `AP2_VERIFY_TIMEOUT_S` (covered in e2e/test_verify.py L97-110). The four uncovered knobs are structurally identical to the covered ones — same `os.environ.get(...)` parse, same default-fallback path, same int-or-fallback-for-invalid shape. Goal.md's delete-test ("would a regression risk become invisible if this test were deleted?") answers YES here: today a typo flipping `AP2_AGENT_MODEL`'s default from claude-sonnet-4-5 to something else, or an off-by-one on `AP2_CONTROL_MAX_TURNS`'s int fallback, would ship silently with no test red.

## Scope

Add unit tests covering each of the four env knobs. Target tests:

(1) `AP2_EVENT_CONTEXT`: parses to an int controlling the daemon's recent-events block size; defaults to its in-source default (look up the literal in `ap2/prompts.py` or wherever it's read); invalid (non-int, ≤0) falls back to default. Tests pin: default, env override with valid positive int, env override with `"abc"` falls back, env override with `"-3"` falls back (if `_pos_int_env`-style helper) OR documents the actual fallback rule.

(2) `AP2_CONTROL_MAX_TURNS`: per-control-agent `max_turns` cap passed to `ClaudeAgentOptions`; defaults to its in-source default. Tests pin: default surfaces to the SDK options dict, env override with `"30"` flows through, invalid falls back.

(3) `AP2_IDEATION_MAX_TURNS`: same shape, ideation-agent specific. Tests pin: precedence over the generic `AP2_CONTROL_MAX_TURNS` (per-site env wins).

(4) `AP2_AGENT_MODEL`: passed to `ClaudeAgentOptions` as `model`; defaults to its in-source default. Tests pin: default model name flows through, env override flows through, empty-string and whitespace-only do NOT silently override (document and pin whichever behavior is current).

Place the new tests in the file that already covers the sibling env knob — e.g. `AP2_CONTROL_MAX_TURNS` and `AP2_IDEATION_MAX_TURNS` tests near the existing `AP2_AGENT_EFFORT` precedence tests in `test_status_report_skip.py` if they exercise the same `_run_control_agent` path; `AP2_EVENT_CONTEXT` near its consumer's existing tests (e.g. `test_prompts.py` if the event block is built in `prompts.py`); `AP2_AGENT_MODEL` wherever the SDK-options-builder is tested. New file only if no natural home exists.

## Design

For each env knob: locate the read site in source (e.g. `int(os.environ.get("AP2_CONTROL_MAX_TURNS", "<default>"))`), identify the parse helper (one of `_int_env`/`_pos_int_env`/inline `int(...)` with fallback), and pin the parse contract end-to-end (env → SDK options dict / built block), not just the helper in isolation. End-to-end matters because the failure mode that escaped TB-186 (slot-check fired before cooldown gate) was an end-to-end ordering bug not catchable by helper-level tests alone.

Use `monkeypatch.setenv` / `monkeypatch.delenv` (the existing fixture-pattern in `test_status_report_skip.py` and `test_verify_retry_diff.py`) for env manipulation. Stub the SDK boundary with the same `FakeSDK` pattern existing tests use; assert against the options-dict captured at the SDK boundary.

For `AP2_AGENT_MODEL`'s invalid-empty-string case, the test pins the CURRENT behavior (whatever it is) — if the source today silently falls through to default on `""`, the test pins that; if it raises, the test pins that. The point isn't to choose semantics, it's to make the current contract testable so a refactor surface materializes.

Precedence test for `AP2_IDEATION_MAX_TURNS` vs `AP2_CONTROL_MAX_TURNS`: set both, assert the ideation-specific knob wins for the ideation control-agent run path; assert the generic `AP2_CONTROL_MAX_TURNS` still wins for the status-report path. (Same shape as TB-156's `AP2_VERIFY_JUDGE_EFFORT` vs `AP2_AGENT_EFFORT` precedence tests.)

## Verification

- `uv run pytest -q ap2/tests/ -k "event_context or control_max_turns or ideation_max_turns or agent_model"` — new tests pass (expected ≥8 new test functions: 2 each × 4 knobs minimum).
- `uv run pytest -q ap2/tests/` — full regression suite green.
- `grep -rnE "AP2_EVENT_CONTEXT" ap2/tests/` — exit 0 (at least one test exercises this knob).
- `grep -rnE "AP2_CONTROL_MAX_TURNS" ap2/tests/` — exit 0.
- `grep -rnE "AP2_IDEATION_MAX_TURNS" ap2/tests/` — exit 0.
- `grep -rnE "AP2_AGENT_MODEL" ap2/tests/` — exit 0.
- `[ "$(grep -rcE 'AP2_EVENT_CONTEXT' ap2/tests/ | awk -F: '{s+=$2} END {print s+0}')" -ge 2 ]` — at least 2 happy/error-path test cases for AP2_EVENT_CONTEXT (sanity bound).
- Prose: each new test exercises BOTH a happy-path branch (env set to a valid value, behavior flows through) AND an error/fallback branch (invalid or unset, fallback path taken) — judge confirms by reading the new test functions against the source read-site for each of the four knobs and verifying the assertions cover both branches.
- Prose: `AP2_IDEATION_MAX_TURNS` precedence test exists and asserts the per-site env wins over the generic `AP2_CONTROL_MAX_TURNS` (same shape as the existing `AP2_AGENT_EFFORT` vs `AP2_VERIFY_JUDGE_EFFORT` precedence tests in `test_status_report_skip.py` / `test_verify_retry_diff.py`).

## Out of scope

- Auditing every uncovered env knob in source (would dilute the focused scope; this proposal narrows to the four with zero current coverage AND clear SDK-cost/behavior impact).
- Adding new env knobs or changing existing knob defaults (this is a coverage task, not a behavior task).
- `AP2_JANITOR_*` knobs (already covered in `test_janitor.py`).
- `AP2_VERIFY_JUDGE_*` knobs (already covered in `test_verify_retry_diff.py`).
- `AP2_MM_*` knobs (operator-config rather than SDK-cost-shaping; covered separately by `test_mattermost.py` and out of focus for this cycle's testing-axis triage).
