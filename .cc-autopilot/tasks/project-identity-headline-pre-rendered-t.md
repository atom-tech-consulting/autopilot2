## Goal

Make the periodic Mattermost status-report post identify the project
in its headline AND render task references with titles + 1-line
outcomes instead of bare TB-Ns + freeform agent prose. Closes the
first failure mode goal.md names under `Current focus: operator-legible reporting and monitoring`
("Context-poor content … reports identify work by bare TB-N + counts,
assuming the reader holds the board in their head") and directly
satisfies the Done-when bullet "A status report identifies tasks by
title + one-line summary (never bare TB-N alone) and leads with the
project name — a reader who hasn't seen the project since the last
report understands it without a follow-up or opening the repo."

Why now: the multi-project operator the new focus targets cannot
identify a report's source project from the current `**Autopilot
Status Report**` headline (no project-name surface exists in
`Config`, no `AP2_PROJECT_NAME` env knob, no `project_name`
reference anywhere in `ap2/status_report.py`) and must alt-tab to
the repo to translate every TB-N the agent emits as `TB-N +
1-line outcome + short SHA` (prompt L741-744) into a task title.
Today's report indexes; with this change it informs.

## Scope

(1) Add `project_name: str` field to `Config` in `ap2/config.py`
with default `project_root.name` and env override
`AP2_PROJECT_NAME`; thread through `Config.load()` matching the
existing `AP2_*` knob handling pattern. Add the knob to the
hot-reload tunable set in `ap2/env_reload.py` (name changes
should not require daemon restart).

(2) Update `STATUS_REPORT_PROMPT` (`ap2/status_report.py`
L710-864) so the headline contract changes from `**Autopilot
Status Report** — <now>` to `**[<project_name>] Autopilot Status
Report** — <now>`. Substitute `cfg.project_name` at prompt-build
time using the existing snapshot-block formatting path.

(3) Add a daemon-rendered `## Recent task activity` digest section
helper `render_recent_task_activity_section(cfg, *, since_event_idx, tail)`
alongside the existing `render_automation_loop_activity_section` /
`render_focus_rotation_activity_section` pattern. Walks the
inter-report window (events at indices > the previous
`cron_complete job=status-report`) and emits one bullet per
terminal task event (`task_complete`, `task_failed`,
`verification_failed`, `retry_exhausted`) shaped as
`- **TB-N** — <title>: <one-line outcome>` where `<title>` is
resolved via `Board.find(task_id).title` with fallback to the
event's `summary` field on lookup miss. Status-report prompt
forwards this section VERBATIM (same contract as the TB-228 /
TB-244 / TB-245 / TB-258 / TB-259 sub-blocks already documented);
the agent no longer composes bare TB-N bullets for events the
daemon already pre-rendered.

(4) Regression-pin module `ap2/tests/test_tb280_status_report_project_identity.py`
covers: `Config.project_name` default + env override +
invalid-value fallback (mirrors TB-205 / TB-210 env-knob test
shape); headline contract substring in `STATUS_REPORT_PROMPT`;
section-renderer output shape on a synthetic event window; section
absent when the window has zero terminal task events
(omit-on-empty parity with TB-228).

## Design

Mirrors the existing daemon-renders / agent-forwards pattern that
TB-228 / TB-244 / TB-245 / TB-258 / TB-259 sub-blocks already use:
the daemon owns structural rendering, the agent owns prose that
sits outside the pre-rendered sections. `project_name` lives on
`Config` (not on a Routine-scoped struct) so the same field is
available to web home, `ap2 status`, and any future push surface
that wants to prefix the project identity uniformly.

## Verification

- `grep -q "project_name" ap2/config.py` — Config field added.
- `grep -q "AP2_PROJECT_NAME" ap2/config.py` — env knob handled.
- `grep -q "project_name" ap2/status_report.py` — prompt + renderer reference the field.
- `grep -Eq "render_recent_task_activity|Recent task activity" ap2/status_report.py` — digest helper + section heading wired.
- `test -f ap2/tests/test_tb280_status_report_project_identity.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_tb280_status_report_project_identity.py` — module passes.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Web UI / `ap2 status` text rendering changes (this task is the
  cron Mattermost-post push surface only; the pull surfaces are
  evolved separately).
- Cross-project aggregation, registry, or shared dashboards
  (goal.md Non-goal L248-250: each daemon's reports stand alone).
- Per-bullet language polish beyond title + outcome resolution
  (the agent still composes the 1-line outcome string; the
  daemon supplies title + structural shape only).
## Attempts

### 2026-05-23 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260523T014229Z-TB-280.prompt.md`, `stream: .cc-autopilot/debug/20260523T014229Z-TB-280.stream.jsonl`, `messages: .cc-autopilot/debug/20260523T014229Z-TB-280.messages.jsonl`
