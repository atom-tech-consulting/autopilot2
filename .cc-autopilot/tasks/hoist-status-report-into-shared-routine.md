# Hoist status-report into shared routine; expose as MCP tool

## Goal

Extract the status-report agent invocation into a shared callable that both the cron tick and the Mattermost handler can use, so on-demand status replies have the same shape, audit trail, and freshness/skip semantics as scheduled cron reports. Today the status-report logic lives entangled with the cron job: the cron prompt + `_STATUS_REPORT_CONTRACT` (TB-128) + skip-gate (`_status_report_should_skip`) all assume cron is the only caller. The MM handler can compose a status-shaped reply ad-hoc but it doesn't share the contract, doesn't bump cron_state, doesn't emit `cron_start`/`cron_complete` events, and doesn't get TB-128's freshness instructions automatically.

## Why

Two concrete drivers:

(1) **Operator says `@claude-bot status` mid-day** — currently the handler reads events.jsonl + TASKS.md + git log on its own and posts a freeform reply. Format drifts from the canonical cron report; if the contract changes (e.g. new format, new fields), the chat path doesn't follow. Single source of truth eliminates the drift.

(2) **Audit consistency** — operator-triggered reports should leave the same trail in events.jsonl as cron-triggered ones (`cron_start`/`cron_complete`/`status_report` events). Otherwise post-mortems can't distinguish "no cron fired" from "cron fired but operator pre-empted with chat" from "neither happened."

(3) **TB-128 freshness contract / skip-if-idle** — should apply uniformly. If the operator triggers a status report when nothing has changed since the last one, the handler should respond with "no new activity since <last>" instead of generating a duplicate report. That's exactly the skip-gate logic, which is currently cron-only.

## Scope

(1) New module `ap2/status_report.py` (or fold into existing `ap2/daemon.py` if preferred — a small `run_status_report(cfg, sdk, mcp_server, *, trigger: Literal["cron","chat"], reason: str | None = None) -> StatusReportResult`):
   - Builds the same prompt the cron uses today (move the cron's prompt body from `cron.default.yaml` into a Python constant; the cron job's prompt becomes "see status_report.STATUS_REPORT_PROMPT").
   - Applies `prompts._current_state_block(cfg)` + `_STATUS_REPORT_CONTRACT`.
   - Runs `_run_control_agent` with the same allowed_tools the cron path uses.
   - On entry, runs the existing `_status_report_should_skip(cfg)` gate; emits `cron_skipped` (or a new `status_report_skipped` event with `trigger=...` field) and returns without invoking the SDK if the skip gate fires.
   - On non-skip, emits `cron_start` (with `trigger=...` field) before the SDK call and `cron_complete` after, mirroring today's cron event shape so existing dashboards still work.

(2) Cron tick (`run_cron` in `daemon.py`, when `job.name == "status-report"`) calls `run_status_report(cfg, sdk, mcp_server, trigger="cron")` instead of doing the inline SDK invocation. Net no behavior change for the cron path; just a call-site swap.

(3) New MCP tool `mcp__autopilot__status_report_run(reason: str)`:
   - Validates that the daemon is not paused (deferring otherwise — same semantics as cron, where paused daemons skip due jobs).
   - Calls `run_status_report(cfg, sdk, mcp_server, trigger="chat", reason=reason)` and returns a one-line `_ok` summary with the resulting event ts (so the handler can include it in its mattermost_reply).
   - Tool docstring tells the handler: "Use when the operator explicitly asks for a status report. Don't call repeatedly — the routine has its own skip-if-idle gate; calling more often than that won't get you a fresher report." This nudges the handler away from triggering on every casual status mention.

(4) Add `mcp__autopilot__status_report_run` to `MM_HANDLER_TOOLS_FULL` and `MM_HANDLER_TOOLS_RESTRICTED` (both — operator should be able to ask for status whether a task is in flight or not).

(5) MM handler prompt updates: when the operator's message asks for a status report (recognize phrases like "status", "what's going on", etc.), the handler invokes `status_report_run` instead of composing its own reply. Pin in `test_prompts.py`.

(6) Optional: a `cron_state_advance` parameter on the routine. If trigger="chat", we may NOT want to advance `cron_state[status-report].last_run` (otherwise an operator-triggered report at 11:00 silences the scheduled noon cron). Default trigger="chat" → skip cron_state advance; trigger="cron" → advance. Pin behavior with tests.

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE "run_status_report\\(" ap2/daemon.py` — cron path now delegates to the shared routine.
- `grep -qE "status_report_run" ap2/tools.py` — new MCP tool registered.
- `grep -qE "mcp__autopilot__status_report_run" <(python3 -c "from ap2.tools import MM_HANDLER_TOOLS_FULL, MM_HANDLER_TOOLS_RESTRICTED; print('\n'.join(MM_HANDLER_TOOLS_FULL + MM_HANDLER_TOOLS_RESTRICTED))")` — tool appears in both MM handler toolsets.
- New unit test in `test_status_report_skip.py` (extends existing pin): `run_status_report(trigger="chat")` honors the skip-if-idle gate and emits a `cron_skipped` (or `status_report_skipped`) event without invoking the SDK.
- New unit test: `run_status_report(trigger="cron")` advances `cron_state[status-report].last_run`; `run_status_report(trigger="chat")` does NOT advance it (cron schedule unaffected by chat-triggered reports).
- New unit test in `test_tools.py`: the MCP tool `status_report_run({"reason":"operator asked"})` returns `_ok` and emits a `cron_start` event with `trigger="chat"` and the supplied reason in the payload.
- New unit test in `test_prompts.py`: the MM handler prompt instructs the agent to use `status_report_run` for status-shaped operator queries (pin the recognition pattern).
- New e2e test: simulate `@claude-bot status` arriving at the MM handler; assert the handler invokes `status_report_run`, the routine runs the SDK once, and a `cron_complete` (or new event type) lands in events.jsonl with `trigger="chat"`.

## Out of scope

- Generalizing this to other shared agent routines (ideation, watchdog) — file separately if the pattern proves valuable beyond status-report.
- A `cron_run_now` mechanism for arbitrary cron jobs — broader scope, separate task.
- Changing the status-report's prompt content or schedule — purely a refactor + new entry point.
- Recognition of natural-language status queries beyond keyword matching — handler's existing prompt-matching surface is enough for V1.
