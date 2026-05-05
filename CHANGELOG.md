# Changelog

Notable operator-facing changes to ap2. Formatted loosely after [Keep a Changelog](https://keepachangelog.com/) — grouped by date and impact-shape rather than semver releases (ap2 ships continuously). Internal refactors, test scaffolding, and pure observability instrumentation are summarized rather than itemized.

## 2026-05-05

### Added — operator verbs

- **`ap2 ideate [--force]`** (TB-159) — manually trigger an ideation cycle. Bypasses the cooldown / `AP2_IDEATION_DISABLED` / non-empty-Ready-or-Backlog gates. Refuses if a task is currently Active unless `--force` (concurrent task-agent + control-agent SDK runs are risky). Forced runs still call `mark_run` so the next natural cooldown clock resets.
- **`ap2 reject TB-N [--reason "..."]`** (TB-152) — reject an ideation-proposed task (Backlog + `@blocked:review` only). Drops the row + briefing AND captures the rejection reason in `.cc-autopilot/operator_log.md` so ideation Step 0 stops re-proposing it. For non-proposals use `ap2 delete`.
- **`ap2 update TB-N [...flags]`** (TB-153) — in-place edit of a queued task's title / tags / description / blocked codespan / briefing. Briefing path is slug-stable so git history of the briefing file stays contiguous. Hard-refused on tasks in Active or Pipeline Pending.
- **`ap2 add --skip-goal-alignment`** and **`ap2 update --skip-goal-alignment`** (TB-170) — operator escape hatch to bypass the TB-161 goal-anchor and TB-164 Why-now validators while still running every other check (TB-154 canonical sections, TB-138 auto-verifiable bullets, TB-134 single-line title, TB-135 briefing-required). Operator-CLI-only — ideation and the MM handler do NOT have access to the bypass.

### Added — env knobs

- **`AP2_IDEATION_TRIGGER_TASK_COUNT`** (default `3`, TB-160) — fire ideation when the Ready+Backlog count is BELOW this threshold (Active is still a hard gate). Set to `1` for the legacy "fire only when the working queue is fully empty" behavior; raise it (e.g. `5`) for projects with very fluid scope.

### Added — briefing validators (queue-append-time, before TB-N allocation)

- **Canonical structure (TB-154).** Briefings missing any of the five `##`-level sections — `## Goal`, `## Scope`, `## Design`, `## Verification`, `## Out of scope` — are rejected. Section order is free; extension is allowed; renaming is not.
- **Goal-anchor cite (TB-161).** The `## Goal` body must reference (substring match) one of the project `goal.md`'s `## Current focus` heading titles or one of its `## Done when` bullets. Validator error message lists available anchors.
- **`Why now:` rationale (TB-164).** The `## Goal` body must include a line-anchored `Why now:` paragraph (regex `(?im)^\s*why now[\s:]`) of at least 40 chars after the marker. Trivial passes (`Why now: yes`) fail the length check.
- **`Manual:` bullets in Verification (TB-171).** The `## Verification` section is rejected if any bullet starts with `Manual:` — the verifier runs unattended and cannot observe out-of-band actions. Convert to an e2e test or move the criterion to `## Out of scope`.

### Added — observability events

- **`task_run_usage`** (TB-165) — emitted on every task-agent terminal path with per-run token totals (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`), `total_cost_usd`, `num_turns`, `model`, `model_usage`, `duration_s`, `status`, and a `run_id` matching the on-disk debug-dump filename prefix. Persistent in `events.jsonl` regardless of success/failure.
- **`control_run_usage`** (TB-166) — same shape as `task_run_usage`, with a `label` field naming the control-agent run kind (`"ideation"`, `"cron-status-report"`, `"MM-<post-id>"`). Emitted on every control-agent terminal path.
- **`task_updated`** (TB-153) — emitted when `ap2 update` mutates a queued task. Fields: `task`, `fields` (the changed-field csv list).

### Changed — defaults and surfaces

- **`ap2 add` default section is now `Backlog`** (was `Ready`, TB-167). Operator-filed tasks land in triage alongside ideation proposals; the daemon auto-promotes when capacity opens. Old behavior with explicit `-s Ready`.
- **Task-agent debug dumps retained on success** (TB-165). Previously `prompt.md` / `stream.jsonl` / `messages.jsonl` were deleted on a clean complete; now retained for both success and failure so per-run token usage and message detail survive for cache-tuning / prompt-iteration work.
- **Control-agent debug dumps now include stream + messages** (TB-166). Prior to TB-166, `_run_control_agent` only wrote `prompt.md` and discarded the SDK message stream; now writes `<run_id>.stream.jsonl` and `<run_id>.messages.jsonl` alongside the prompt for every ideation / cron / MM-handler invocation.
- **Verification-failure rendering in `ap2 logs` and web** (TB-158) — `verification_failed` rows now show per-bullet pass/fail/unverified counts inline with failed-bullet headlines + judge notes. `--json` path is unchanged (regression-pinned).
- **`ap2 status` now surfaces** pending operator queue ops, pending-review TB-Ns (TB-151), and the latest "Open questions for operator" from `ideation_state.md` (TB-173) in addition to the existing daemon liveness + board counts + cron jobs.
- **Pending operator queue ops surfaced in web view** (TB-162) — `/` index page renders a card listing each queued op (kind + task_id + uuid prefix + per-op summary) above the events table. Card omitted when the queue is empty.
- **Ideation prompt signal-density trims**:
  - Snapshot block (TB-168): drops `board:` counts and `recent commits` for ideation-only invocations; keeps `now:` (the agent's only clock). Status-report cron unaffected.
  - Events block (TB-169): filters to a curated 9-type allowlist (`task_complete`, `verification_failed`, `verification_partial`, `retry_exhausted`, `task_state_violation`, `ideation_approved`, `task_deleted`, `task_updated`, `cron_proposed`) for ideation; drops noise like `judge_call`, `status_report`, daemon-lifecycle events. Status-report cron continues to receive the unfiltered tail.
  - "Recent operator rejections" block (TB-163) injected into the control-prompt header so pattern-level rejection signal reaches the ideator at proposal time, complementing the per-cycle ideation_state.md memory.

### Internal — token cost tuning

- Judge diff cap lowered (TB-156): per-bullet diff truncation from 100KB → 30KB. Reduces judge call cost without measurable verdict impact.
- Per-call-site effort knobs (TB-156): `AP2_VERIFY_JUDGE_EFFORT` (default `high`) and `AP2_STATUS_REPORT_EFFORT` (default `medium`) lowered from the global `xhigh`. Token instrumentation (TB-157) measures the impact via the new `task_run_usage` / `control_run_usage` events.

### Notes

The `--reason` flag on `ap2 reject` is captured verbatim into `operator_log.md` and rendered as a "Recent operator rejections" block at the top of the next ideation prompt (TB-163). Quality reasons compound across cycles — terse "wack-a-mole fix that doesn't generalize" reasons are exactly the signal ideation can learn from. `(no reason given)` is a valid placeholder when the operator wants to reject quickly; itself a signal that the proposal was off enough to not warrant articulation.

Daemon must be restarted (`ap2 stop && ap2 start && ap2 resume`) to pick up changes that touch the running Python modules — env-var tweaks, prompt assembly, control-agent plumbing. Briefings authored against the new validators only need a fresh `ap2 add` invocation; no daemon restart required.
