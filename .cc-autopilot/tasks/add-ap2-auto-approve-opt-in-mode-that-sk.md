# Add `AP2_AUTO_APPROVE` opt-in mode that skips `@blocked:review` on ideation-proposed tasks; guard with tag opt-out + cumulative-regression pause

Tags: `#autopilot` `#automation` `#operator-surface` `#regression-pin`

## Goal

Advance the Mission's walk-away promise (Done-when bullet 1: **"an operator can point ap2 at a fresh project, paste a `goal.md`, and walk away for a week without intervention"**) by closing the most-frequently-triggered operator-in-the-loop bottleneck: the `@blocked:review` codespan that ideation appends to every proposed TB. A representative active session on this codebase approves 10-20 tasks; that's not walking away, that's approving constantly. Under the upcoming **Current focus: end-to-end automation**, the testing/docs/reusability/cleanness consolidation work (TB-203 → TB-220) has put the upstream gates in place — briefing structural validation (TB-161 anchor check, TB-164 Why-now check, TB-171 manual-bullet reject), goal-alignment validation, per-task verification, retry budget, rollback — so the operator-approve gate that sits between those upstream gates and dispatch is the relaxable surface.

Why now: the prior focus closed the gates that MAKE this safe in practice. Approving 10-20 tasks per session is the operator overhead that the walk-away promise requires eliminating. The Mission says explicitly "minimal human intervention" + "walk away ... without intervention" — the current default (every task requires `ap2 approve`) contradicts the Mission text. This deliverable is opt-in: the env knob default stays unset (current behavior preserved); the operator who has verified the upstream gates flips the knob to enable autonomy.

## Scope

(1) **New env knob `AP2_AUTO_APPROVE`**:
  - Read once per ideation cycle in `ap2/ideation.py` at the point where a new TB-N row is composed for `TASKS.md`. When set to `1` (or any truthy value per Python's `os.environ.get(...).strip() in {"1", "true", "yes"}` convention — match `ap2/janitor.py`'s pattern), ideation OMITS the `@blocked:review` codespan from the proposed task row.
  - Default unset = current behavior (every proposal carries `@blocked:review`). No behavior change for operators who haven't enabled the knob.
  - The new task row continues to carry all other codespans (`#tags`, optional `@meta:value` codespans) — only the `@blocked:review` codespan is conditionally dropped.

(2) **Tag-based opt-out via `AP2_AUTO_APPROVE_GATE_TAGS`**:
  - Comma-separated list of tag strings (e.g. `#breaking-change,#high-risk,#schema-migration`). Default: `#breaking-change,#high-risk`.
  - When auto-approve is ON but a proposed task carries ANY of the gate-tags, the `@blocked:review` codespan is preserved (the task still requires `ap2 approve`). Operator's escape hatch for the categories of work they don't trust to auto-ship.
  - Read once at the same point as `AP2_AUTO_APPROVE`; cached for the cycle (don't re-read per-task).

(3) **Cumulative-regression pause via `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`**:
  - Integer count, default `3`. The daemon's auto-promote logic in `ap2/daemon.py` reads this and checks the recent `task_complete` events (last N tasks) for the `status=verification_failed` final-state pattern (i.e. retry_exhausted → Frozen).
  - When N consecutive `task_complete` events have `status` in `{verification_failed, blocked, error}` AND end in `retry_exhausted`, the daemon halts auto-promotion of `@blocked:review`-less tasks (effectively pausing the auto-approve mode) until the operator emits an explicit ack (`ap2 ack auto_approve_unfreeze --reason "..."` — uses the existing TB-106 ack pattern). Emit a `decisions needed` entry surfacing the threshold breach.
  - Doesn't pause ALL dispatch — operator-approved tasks (those with `@blocked:review` cleared via `ap2 approve`) continue normally. Only the auto-approved promotion path is paused.

(4) **Audit event `auto_approved`**:
  - Each time ideation omits `@blocked:review` on a new TB, emit `events.append(events_file, "auto_approved", task=TB-N, knob=AP2_AUTO_APPROVE)` so `ap2 logs` and the cron status-report can surface what auto-approval shipped without operator review. The event's `knob=` field captures the env-knob value at the time of the proposal (forensic trail if behavior changes during a daemon's lifetime).

(5) **Documentation**:
  - Add an `## Operator-in-the-loop relaxations` section (or extend an existing relevant section) to `ap2/howto.md` documenting the three new env knobs with their behavior, defaults, and the safety reasoning behind each. Cross-reference goal.md's **Current focus: end-to-end automation** focus's manual-approval-bottleneck axis.
  - Add a docs-drift gate row to `ap2/tests/test_docs_drift.py::test_every_env_knob_documented` registry — the new knobs need to appear in howto.md per the TB-203 pattern.
  - Add coverage-drift rows to `ap2/tests/test_coverage_drift.py::test_every_env_knob_has_test_reference` — the new knobs need real test references (this task's own tests satisfy the gate; no shim shortcut).

(6) **Not in this task** (each named separately so the scope contract is unambiguous):
  - Auto-unfreeze on agent-diagnosed briefing-shape errors (axis 2 of the new focus — separate task).
  - Token-cost ceilings / per-window budgets (axis 3 — separate task).
  - Focus-rotation proposals from ideation (axis 4 — separate task).
  - CLI verb for runtime toggling (`ap2 auto-approve --enable | --disable`) — env-only matches existing knob patterns; a CLI surface adds maintenance without value.
  - Removing or deprecating `ap2 approve` — it stays. Operators can still manually `--blocked review` a specific task or queue an approve on a previously-blocked task.

## Design

The opt-in env-knob shape matches the existing `AP2_REAL_SDK` / `AP2_TASK_MAX_TURNS` / `AP2_IDEATION_MAX_TURNS` pattern — operators tune behavior via `.cc-autopilot/env` without code changes. Conservative defaults preserve current behavior for operators who haven't opted in.

The three knobs form a layered safety model:
- `AP2_AUTO_APPROVE=1` — the master switch. Off by default.
- `AP2_AUTO_APPROVE_GATE_TAGS` — the per-shape opt-out. Operator names tag categories that retain manual review even in auto-approve mode.
- `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` — the systemic-regression circuit-breaker. If N consecutive task failures land in Frozen, auto-promote halts and surfaces a decision (the operator either acks "false alarm, continue" or fixes the upstream cause before unfreezing).

Why the `auto_approved` event (4): operators who turn on auto-approve still benefit from a post-hoc audit trail. The cron status-report (TB-201/202 pattern) surfaces recent activity; including auto-approved tasks in that signal means the operator can monitor occasionally rather than approve continuously. Without the event, auto-approval is invisible — anti-pattern for the "operator should be able to audit what happened" expectation.

Why a CLI toggle is out of scope (6): runtime toggle via CLI introduces drain-vs-immediate semantic questions (does the toggle apply to in-flight queue items? to the next ideation cycle? to ALL future cycles?) and adds a state-mutation surface. Env-only keeps the semantics simple: change the env, restart the daemon, behavior is the new env. The restart is a known operator pattern in this codebase (env knobs only take effect after restart per `Config.load`).

The cumulative-regression threshold (3) and tag-opt-out (2) are operator-tunable, not hardcoded, so different operators with different trust levels can configure their walk-away envelope. The defaults are conservative-but-not-paranoid: 3 consecutive Frozen tasks is rare in steady-state but a real signal when it happens; `#breaking-change` / `#high-risk` tags are the categories ideation itself uses for proposals it judges as elevated-risk (so the defaults align with ideation's existing self-tagging).

Sequencing risk: the cumulative-regression check (3) reads recent events. The events file is append-only and the daemon already reads it for status-report and cron purposes — no new file-IO contract. The check fires at auto-promote time only (not per-event), so it's not a hot path.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0) after the change.
- `grep -nE "AP2_AUTO_APPROVE" ap2/ideation.py` — exit 0; the env knob is read in the proposal-emission path.
- `grep -nE "AP2_AUTO_APPROVE" ap2/daemon.py` — exit 0; the env knob (and at least the freeze-threshold sibling) is read in the auto-promote path.
- `grep -nE "AP2_AUTO_APPROVE_GATE_TAGS|AP2_AUTO_APPROVE_FREEZE_THRESHOLD" ap2/ideation.py ap2/daemon.py` — exit 0 with both knob names appearing (the layered safety knobs are wired).
- `grep -nE "AP2_AUTO_APPROVE" ap2/howto.md` — exit 0; the new knob is documented in the operator howto.
- `grep -nE "auto_approved" ap2/events.py ap2/ideation.py` — exit 0; the new event type is registered AND emitted.
- `[ "$(grep -lE 'AP2_AUTO_APPROVE' ap2/tests/*.py | wc -l)" -ge 1 ]` — at least one test file references the new env knob; ensures real test coverage exists (not just gate-satisfaction shim).
- Prose: the new tests cover at minimum five behavioral pinning cases — (a) unset knob preserves `@blocked:review`, (b) set knob omits `@blocked:review`, (c) gate-tag-matching task retains `@blocked:review` even when knob is set, (d) cumulative-regression threshold halts auto-promote after N consecutive Frozen, (e) operator ack via `ap2 ack auto_approve_unfreeze` resumes auto-promote. Judge confirms via `Read` of the new test file(s).
- Prose: `ap2/howto.md`'s new section names the three env knobs together with their default values, the layered safety reasoning, and the operator-trust framing from the goal.md focus. Judge confirms via `Read` of the new docs section.
- Prose: the `auto_approved` event surfaces in `ap2 logs` output formatting (the events.py event-type formatter has a case for it OR the default case renders it sensibly). Judge confirms via `Read` of any formatter changes plus reading an emitted event from a test.

## Out of scope

- Auto-unfreeze of Frozen tasks (separate axis-2 task in the end-to-end-automation focus).
- Token-cost ceilings / per-window budgets (separate axis-3 task).
- Focus-rotation proposals from ideation (separate axis-4 task).
- A `ap2 auto-approve --enable | --disable` CLI verb — env-only matches existing knob patterns; runtime toggling adds drain-semantic ambiguity without operator value.
- Removing, deprecating, or no-op'ing the `ap2 approve` CLI verb — it stays for operator-curated manual approval of `@blocked:review` tasks.
- Auto-rolling-back tasks that retrospectively classify as `#wasteful` (different mechanism; the existing `ap2 classify` + `ap2 rollback` paths handle that separately).
- Per-tag granular auto-approve (e.g. "auto-approve `#docs` but not `#tests`") — the current opt-out is opt-out-by-tag; opt-in-by-tag is a different shape and not on this task's path.
- A daemon-side allowlist of "auto-approvable task shapes" beyond tags (e.g. word-count limits on title, briefing-size caps, scope-section bullet count) — premature; the tag-based opt-out is the first filter; if it proves insufficient, a future task adds finer-grained gates.
