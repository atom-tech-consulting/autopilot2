# TB-203 — Documentation drift coverage gate for `ap2/howto.md` + `ap2/architecture.md`

Tags: `#autopilot` `#docs` `#code-quality` `#operator-surface` `#regression-pin`

## Goal

Close the documentation-drift failure mode on the new **current focus: code quality** focus's (2) **Operator-facing documentation** axis (goal.md L65-72): every operator-facing surface — MCP tool names registered in `CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` / `MM_HANDLER_TOOLS` (ap2/tools.py), every `AP2_*` env knob referenced in `ap2/*.py`, and every event-type string passed to `events.append(...)` — must be referenced (by exact name) in `ap2/howto.md` (and/or `ap2/architecture.md` for the MCP-tools enumeration). Add a regression-pinning test so a future source addition (new env knob, new MCP tool, new event type) fails CI until docs catch up, AND update the docs to close today's already-drifted set.

Why now: today an operator reading `ap2/howto.md`'s `## Configuration knobs` (L447-466) sees 10 env knobs while source uses ~25 (`AP2_VERIFY_JUDGE_EFFORT`, `AP2_VERIFY_JUDGE_MAX_TURNS`, `AP2_STATUS_REPORT_EFFORT`, `AP2_WEB_PORT`, `AP2_WEB_DISABLED`, `AP2_JANITOR_*`, `AP2_AUTO_DIAGNOSE_*`, `AP2_AGENT_EFFORT`, `AP2_AGENT_MODEL`, `AP2_MM_*`, `AP2_REAL_SDK`, `AP2_EVENT_CONTEXT`, `AP2_CONTROL_MAX_TURNS`, `AP2_IDEATION_MAX_TURNS`, `AP2_VERIFY_TIMEOUT_S` missing or partially named), the `## Custom MCP tools (reference)` (L357-392) names ~7 control tools while source registers 11 plus `report_result` still listed with a `cron` field TB-123 dropped, `ap2/architecture.md`'s `CONTROL_AGENT_TOOLS` literal (L175-194) is missing `git_log_grep`/`operator_log_append`/`mattermost_thread_read`/`status_report_run`, and the `## Event schema` section (L394-414) omits ~10 newer event types (`ideation_proposal_recorded`/`_reconciled`, `judge_call`, `task_run_usage`, `control_run_usage`, `goal_updated`, `task_updated`, `web_start`/`_stop`/`_error`, `ideation_skipped`, `ideation_forced`, `task_deleted`). Without this work, the operator surface has no test net catching docs drift on the next refactor — the same failure mode goal.md L49-54 names.

## Scope

(1) Add `ap2/tests/test_docs_drift.py` with four tests:
  - `test_every_mcp_tool_documented`
  - `test_every_env_knob_documented`
  - `test_every_event_type_documented`
  - `test_architecture_md_control_agent_tools_complete`

(2) Update `ap2/howto.md`'s `## Configuration knobs`, `## Custom MCP tools (reference)`, and `## Event schema` sections so all four tests pass.

(3) Update `ap2/architecture.md`'s `CONTROL_AGENT_TOOLS` + `TASK_AGENT_TOOLS` literal blocks (L175-194) to match current source.

(4) Don't add a new `ap2 check` warning (parallel surface); the test gate is the authority. A future mirror into `ap2/check.py` can come later if drift becomes a frequent friction point.

## Design

Source-of-truth derivation, mirrored across the four tests:

- **MCP tools**: import `CONTROL_AGENT_TOOLS`, `TASK_AGENT_TOOLS`, `MM_HANDLER_TOOLS` from `ap2.tools`; strip the `mcp__autopilot__` prefix to get the short name set. Built-ins like `Read`/`Glob`/`Grep`/`Bash`/`Edit`/`Write` are excluded by an explicit allow-list (they're Claude built-ins, not autopilot MCP tools — same set the existing toolset prompts already treat as "broad reads").
- **Env knobs**: walk every `*.py` under `ap2/` excluding `ap2/tests/` and `ap2/__pycache__/`; regex `AP2_[A-Z_][A-Z_0-9]*` over each file's text; union into a set. Default-value docstring comments that happen to mention an env knob still count as in-source.
- **Event types**: walk the same set of `*.py` files; regex `events\.append\(\s*[^,]+,\s*["\']([a-z_][a-z_0-9]*)["\']` over each file's text (the second positional arg to `events.append(events_file, "<type>", ...)`); union into a set. Dynamic f-string types (rare today; grep-friendly) gain an opt-in `_DOCS_DRIFT_EXEMPT` allow-list, initially empty.
- **Docs check**: read each docs file once; for each surface name, do a literal `name in text` check (env knobs additionally require a backtick-fenced literal to enforce the rendered-list shape). Failure message names the specific surface and its source file/line, so the next contributor sees the gap immediately.
- **architecture.md**: parse the fenced ```python``` block containing `CONTROL_AGENT_TOOLS = [` and assert every short name in `ap2.tools.CONTROL_AGENT_TOOLS` appears inside that block as a substring. Same for `TASK_AGENT_TOOLS`. (Substring, not exact-match, so the `mcp__autopilot__` prefix is tolerated in either direction.)

The four tests share a tiny helper module-local set of constants but otherwise stay independent — a future env-knob addition fails exactly one test with a precise diff, not a cascade.

## Verification

- `uv run pytest -q ap2/tests/test_docs_drift.py` — new test module passes (all 4 tests).
- `uv run pytest -q ap2/tests/` — full regression suite green.
- `grep -nE "AP2_VERIFY_JUDGE_EFFORT|AP2_STATUS_REPORT_EFFORT|AP2_WEB_PORT|AP2_JANITOR_JUDGE_EFFORT|AP2_AUTO_DIAGNOSE_COOLDOWN_S" ap2/howto.md` — exit 0 (each newly-documented env knob present by name).
- `grep -nE "mattermost_thread_read|operator_log_append|cron_propose|status_report_run|git_log_grep" ap2/howto.md` — exit 0 (each newly-documented MCP tool present).
- `grep -nE "ideation_proposal_recorded|judge_call|task_run_usage|control_run_usage|goal_updated|task_updated" ap2/howto.md` — exit 0 (each newly-documented event type present).
- `! grep -nE 'report_result\(.*cron' ap2/howto.md` — exit 0 (stale TB-123 `cron` field removed from the MCP-tools reference).
- `grep -nE "operator_log_append|mattermost_thread_read|git_log_grep|status_report_run" ap2/architecture.md` — exit 0 (architecture.md CONTROL_AGENT_TOOLS block updated).
- Prose: new test module `ap2/tests/test_docs_drift.py` exists and contains four test functions named exactly `test_every_mcp_tool_documented`, `test_every_env_knob_documented`, `test_every_event_type_documented`, `test_architecture_md_control_agent_tools_complete` (judge confirms via `Grep`/`Read` against the working tree).

## Out of scope

- Mirroring the drift gate into `ap2/check.py` (defer; one surface per cycle).
- Decomposing `ap2/howto.md` itself (it's 503 lines but operator-readable as one page; no boundary signal yet).
- Auto-generating the docs from source (would risk drift in the other direction — docs lose the WHY commentary goal.md L70-72 names as the failure mode for paraphrased docs).
## Attempts

### 2026-05-12 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** 1
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260512T190940Z-TB-203.prompt.md`, `stream: .cc-autopilot/debug/20260512T190940Z-TB-203.stream.jsonl`, `messages: .cc-autopilot/debug/20260512T190940Z-TB-203.messages.jsonl`
