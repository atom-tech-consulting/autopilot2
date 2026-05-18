## Goal

This task targets the Current focus: end-to-end automation focus, axis 1 (Manual-approval bottleneck), specifically the load-bearing "upstream gates already make this safe in practice" mechanic named in goal.md L82-85. The dep-coherence validator-judge that gates briefings on `add_*` / `update` queue-append is currently failing on essentially every recent operator queue-append: 6 `validator_judge_timeout` events in 25h, ALL hitting the 20s `asyncio.wait_for` ceiling wrapping the 15s `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default (`ap2/tools.py:670` constant + `:1056` worker-timeout error string `validator judge worker exceeded {timeout_s + 5:.0f}s`). The TB-243 fail-open masks the cost from the user-facing path but the gate the operator's trust is supposed to flow through is silently disabled — `auto_approved` decisions today bypass `@blocked:review` against a gate that fires zero gating verdicts.

TB-247 (`64e760b`) added strict-JSON prompt + raw-response dumps for the parse-failure / non-dict branches, but timeouts have no response to dump, so root cause is invisible: prompt-too-heavy, max_turns-too-tight (TB-249 set default 2), network flake, or cold-start? Need a TB-253-shape investigation artifact to characterize the failure mode before scoping a calibration patch.

Why now: 6/6 recent operator queue-appends timed out the dep-coherence judge in the last 25h, but the fail-open hides it from the symptom surface — the operator's safety floor for axis-1 auto-approve (goal.md L106-110) sits on a gate that fires zero gating decisions today, so a TB-253-shape categorized artifact must precede any calibration to avoid blind tuning.

## Scope

1. Time-box: ~30-min wall-clock investigation, no production code edits. Pure read + measurement + artifact.

2. Enumerate every `validator_judge_timeout` event in `.cc-autopilot/events.jsonl` for the last 7 days: timestamp, `error` string, `timeout_s` field, and the operator-queue op that triggered each (cross-reference the preceding `operator_queue_append` event by uuid → op + task_id). Capture the briefing-body byte size of each triggering brief if recoverable from `.cc-autopilot/tasks/`.

3. Inspect any `validator_judge_fail` raw dumps under `.cc-autopilot/debug/` matching the TB-247 naming pattern (`*validator-judge-response*`) — characterize the prompt size + typical response shape when the judge does respond, to estimate the cost-per-call baseline.

4. Run a manual measurement: pick one timing-out briefing body from step 2, invoke `_judge_dep_coherence_default` directly (`uv run python -c '...'` driving an asyncio loop) with a stopwatch around the SDK call, and record the wall-clock + token count. Repeat 2-3× to characterize variance.

5. Write the artifact to `.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` with YAML front-matter (`tldr:`, `updated:`, `updated_by: TB-256`, `cites:` list referencing event timestamps or relevant `ap2/tools.py` line numbers).

6. Categorize the dominant contributing factor as one of: `prompt-too-heavy`, `max_turns-too-tight`, `timeout-too-tight`, `sdk-cold-start`, `network-flake`, or `investigate-further`. Provide a one-paragraph rationale per applicable category and a recommended next-task shape for the operator to pick from.

7. Add a small `ap2/tests/test_tb256_validator_judge_timeout_artifact.py` pinning the artifact's structural shape (file exists, YAML front-matter parses, ≥1 categorized factor enumerated, ≥5 enumerated timeout rows in the body). Mirror `ap2/tests/test_tb_investigate_suite_slow_artifact.py` shape.

## Design

Pure investigation; no production code touched. Artifact + artifact-shape pin test, mirroring TB-253 (`eeeb23f`). The artifact is the deliverable; calibration tasks come later. Date-anchored filename (`-2026-05-18`) follows the `test-suite-slowness-2026-05-17.md` precedent.

## Verification

- `compgen -G ".cc-autopilot/insights/validator-judge-timeout-2026-05-18.md"` — investigation artifact written at the conventional path.
- `grep -q '^updated_by:' .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` — YAML front-matter present with `updated_by:` field.
- `grep -q 'TB-256' .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` — artifact cites this TB-N in the front-matter or body.
- `grep -qE 'prompt-too-heavy|max_turns-too-tight|timeout-too-tight|sdk-cold-start|network-flake|investigate-further' .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` — at least one categorized factor enumerated in the artifact body.
- `grep -q 'validator_judge_timeout' .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` — artifact references the event-type under investigation.
- `[ "$(wc -l < .cc-autopilot/insights/validator-judge-timeout-2026-05-18.md)" -ge 40 ]` — artifact has substantive body (≥40 lines).
- `uv run pytest -q ap2/tests/test_tb256_validator_judge_timeout_artifact.py` — artifact-shape pin module passes.
- `uv run pytest -q ap2/tests/` — full suite green (regression gate).

## Out of scope

- Calibration patches to `AP2_VALIDATOR_JUDGE_TIMEOUT_S`, `AP2_VALIDATOR_JUDGE_MAX_TURNS`, or the dep-coherence prompt body — a follow-up TB scopes against this artifact's findings.
- Changes to `_judge_dep_coherence_default` / `_judge_prose_bullet` / the validator-judge SDK call signature.
- Re-enablement of `AP2_AUTO_APPROVE` or any operator-knob flip.
- Web surface or status-report digest changes for timeout data (TB-243 / TB-245 already cover the user-facing surfaces).
