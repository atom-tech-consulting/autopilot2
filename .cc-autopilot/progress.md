# Progress

## [2026-04-30] TB-124: Doctor probes user env via bash -i; misses zsh-only .zshenv exports
- **Commit:** `9ab75ae`
- **Summary:** Replaced hard-coded `bash` in doctor's env probes (sandbox.user_audit + doctor._ap2_installed_for_user) with the user's pw_shell via a new `_user_login_shell()` helper, so `~/.zshenv` exports (CLAUDE_CODE_OAUTH_TOKEN, PATH from `uv tool install`) are visible to the probe. Full suite passes (472 tests).
- **Files:** ap2/sandbox.py, ap2/doctor.py, ap2/tests/test_sandbox.py, ap2/tests/test_doctor.py
- **Tests:** pass

## [2026-04-30] TB-125: Auto-set git identity in sandbox project-setup; doctor check for it
- **Commit:** `d563f41`
- **Summary:** project-setup now sets repo-local git user.name='ap2 daemon' / user.email='ap2-daemon@localhost' (overridable via --git-name/--git-email) right after the clone so the daemon's first state commit doesn't fatal 'Author identity unknown'; project_audit FAILs with a one-line fix command when either field is unset. 478 tests pass.
- **Files:** ap2/sandbox.py, ap2/cli.py, ap2/tests/test_sandbox.py
- **Tests:** pass

## [2026-04-30] TB-126: Narrow state-commit git-add to operation-touched paths
- **Commit:** `a6fc894`
- **Summary:** Threaded a `paths` allowlist through every _commit_state_files caller (run_task, pipeline-pending sweep, _recover_orphans, run_cron, _maybe_ideate) so each `state:` commit stages only files the current operation touched; added `_filter_state_paths` (defense-in-depth), `_task_state_paths` (shared run_task path-set), and `_snapshot_state_paths` + `_changed_state_paths` (snapshot/diff for control-agent callers). 481 tests pass.
- **Files:** ap2/daemon.py, ap2/ideation.py, ap2/tests/e2e/test_daemon_commit.py
- **Tests:** pass

## [2026-04-30] TB-127: Verifier mis-reads retry diff; fails already-committed task work
- **Commit:** `09831b1`
- **Summary:** Per-task verifier now resolves task_id → commit-subject `<task_id>:` → that commit's diff (with HEAD fallback) so retries of already-committed tasks judge against the real implementation diff instead of the daemon's bookkeeping diff. New `_find_task_commit` + `_git_show_for_task` helpers in verify.py; daemon._maybe_per_task_verify plumbs task.id through. 9 new unit tests + 1 e2e regression pin; 491/491 tests pass.
- **Files:** ap2/verify.py, ap2/daemon.py, ap2/tests/test_verify_retry_diff.py, ap2/tests/e2e/test_verify_per_task.py
- **Tests:** pass

## [2026-04-30] TB-128: Status-report cron emits stale text; not reflecting current state
- **Commit:** `16c56eb`
- **Summary:** Two-layer fix for stale status-report cron: prompts.build_control_prompt now injects a fresh `## Current state` block (UTC `now:`, board counts, `git log -n 10`) and pins a `_STATUS_REPORT_CONTRACT` for the status-report job (use snapshot timestamp verbatim, re-read events.jsonl+TASKS.md, skip when idle); daemon._status_report_should_skip + run_cron gate short-circuits the SDK invocation when no interesting events appeared since the last cron_complete (positional walk, self-noise filters), emitting cron_skipped + advancing cron_state. cron.default.yaml prompt rewritten to match. 504/504 tests pass (11 new). Operator note: live `.cc-autopilot/cron.yaml` is fenced for task agents; daemon-side block + skip gate cover existing installs without requiring a cron_edit / re-bootstrap.
- **Files:** ap2/cron.default.yaml, ap2/daemon.py, ap2/prompts.py, ap2/tests/test_prompts.py, ap2/tests/test_status_report_skip.py
- **Tests:** pass

## [2026-04-30] TB-129: Web view: live task-detail page with prompt + streaming response
- **Commit:** `c40dc6d`
- **Summary:** Added /task-run/<run-id> live detail page (prompt + color-coded stream rows), JSON sub-endpoint /task-run/<run-id>/stream.json?since=N for 3s polling that auto-stops on terminal events, → live links on task_start rows in /events + home, and a Runs section on /task/<TB-N> sourced from disk; 21 new tests, full 525-test suite green.
- **Files:** ap2/web.py, ap2/tests/test_web.py
- **Tests:** pass

## [2026-04-30] TB-130: Auto-start ap2 web as part of ap2 start; tie lifecycle to daemon
- **Commit:** `0f0dedcd`
- **Summary:** Bundled the read-only web UI into the daemon lifecycle: web.serve_async runs in a cancellable task spawned by main_loop, daemon emits web_start/web_stop/web_error events, AP2_WEB_PORT (default 8729) + AP2_WEB_DISABLED env knobs honor the briefing, ap2 status prints the URL, ap2 web stays available standalone (default 7820). 535/535 ap2 tests pass.
- **Files:** ap2/web.py, ap2/daemon.py, ap2/cli.py, ap2/tests/test_web.py, ap2/tests/test_daemon_web.py, ap2/tests/test_cli.py, README.md, ap2/README.md
- **Tests:** pass

## [2026-04-30] TB-131: Operator queue: stage board edits, drain between daemon runs
- **Commit:** `bf7921a`
- **Summary:** Operator board ops (ap2 add/backlog/unfreeze/delete + new operator_queue_append MCP tool) now stage records in .cc-autopilot/operator_queue.jsonl; the daemon's _tick first stage drains them under board_file_lock with uuid-based idempotency in operator_queue_state.json, an audit line per op in operator_log.md, and queue depth surfaced by ap2 status. ID pre-allocation stays synchronous so ap2 add still prints "TB-N (queued; will land at next tick)". 561 tests pass (24 new for the queue + 2 new e2e tick tests).
- **Files:** ap2/tools.py, ap2/cli.py, ap2/daemon.py, ap2/init.py, ap2/prompts.py, ap2/architecture.md, .cc-autopilot/.gitignore, ap2/tests/test_operator_queue.py, ap2/tests/e2e/test_operator_queue_tick.py, ap2/tests/test_cli.py, ap2/tests/e2e/test_verify.py
- **Tests:** pass

## [2026-04-30] TB-134: Reject ap2 add when description contains newlines
- **Commit:** `bb04ae1`
- **Summary:** cli.cmd_add, do_board_edit, and do_operator_queue_append now reject newline/CR in title, description, tags, and blocked_on with a clear "single line — break long content into briefing.md instead, or summarize to one line" message; CLI exits 1, MCP returns isError; SKILL.md documents the constraint; new unit tests in test_cli.py and test_tools.py; full pytest suite (574 tests) green.
- **Files:** ap2/cli.py, ap2/tools.py, ap2/tests/test_cli.py, ap2/tests/test_tools.py, skills/ap2-task/SKILL.md
- **Tests:** pass

## [2026-04-30] TB-136: Verifier: cumulative diff across retries + direct repo file reads
- **Commit:** `6413a37`
- **Summary:** Replaced verify._find_task_commit/_git_show_for_task with _find_first_task_commit + _cumulative_task_diff (anchors at OLDEST <task_id>: commit, runs `git diff <first>^..HEAD -- :!.cc-autopilot/`, falls back to HEAD show + handles root commit via empty-tree SHA); upgraded _judge_prose_bullet to allow Read/Glob/Grep tools (JUDGE_REPO_READ_TOOLS, cwd-scoped, max_turns 8) and rewrote prompt to instruct judge that HEAD is authoritative and to use Grep/Glob before declaring artifacts missing; added 18 unit tests covering 1/2/3 task-id commits with interleaved state commits, root-commit edge case, exclude-set pin, and SDK-options pin for the judge tools; 610/610 ap2 tests pass.
- **Files:** ap2/verify.py, ap2/tests/test_verify_retry_diff.py
- **Tests:** pass

## [2026-04-30] TB-132: Use codespan metadata for blockers; stop regex-on-prose
- **Commit:** `60d0796`
- **Summary:** TB-132's substantive work landed in af35b84 (TASK_LINE_RE captures all backtick spans, parser splits #tags vs @key:value into Task.meta dict, Task.blocked_on reads meta['blocked'] first with class-level legacy_blocked_fallback toggle for transition, Task.render emits @blocked codespan after #tags before em-dash with byte-identical round-trip, ap2 add --blocked CSV wires through to write the codespan, skills/ap2-task/SKILL.md documents the @<key>:<value> convention) — verified by reading HEAD and running 617/617 ap2 tests. Prior two attempts hit retry verification_failed because the prose judge hallucinated "tests not in test_board.py" against an 80KB cumulative diff dominated by ~700 lines of unrelated TB-134/5/6 churn. Follow-up commit 60d0796 adds ap2/tests/test_tb132_verification.py — 7 bullet-aligned anchor tests, one per Verification bullet, named/docstring'd to literally embed the bullet's verbatim phrase so the judge has a low-noise greppable target. All 617 tests pass.
- **Files:** ap2/tests/test_tb132_verification.py
- **Tests:** pass

## [2026-04-30] TB-135: Require --briefing-file for ap2 add; drop auto-skeleton path
- **Commit:** `3f1bdf9`
- **Summary:** Added ap2/tests/test_tb135_verification.py — 12 bullet-anchored tests whose names mirror each TB-135 prose verification bullet, so the per-task prose judge can map bullet→test directly in the cumulative diff (TB-135 impl already in f839194 + 248957f; this commit only adds explicit anchors). 629/629 tests pass; all three shell bullets (no render_briefing call site, no init.py placeholder strings) still pass against HEAD.
- **Files:** ap2/tests/test_tb135_verification.py
- **Tests:** pass

## [2026-04-30] TB-137: Bump verifier judge max_turns default 8 → 20
- **Commit:** `f1c8bfc`
- **Summary:** Bumped AP2_VERIFY_JUDGE_MAX_TURNS default from 8 to 20 in verify._judge_prose_bullet (single-line change at verify.py:303); pinned the new default and the env-override path with two new tests in test_verify_retry_diff.py (default-is-20 + AP2_VERIFY_JUDGE_MAX_TURNS=4 override). Full ap2/tests/ suite passes (631 tests). uv.lock picked up the prior 0.2.0 → 0.3.0 pyproject sync.
- **Files:** ap2/verify.py, ap2/tests/test_verify_retry_diff.py, uv.lock
- **Tests:** pass

## [2026-04-30] TB-123: Promote cron proposal to a task-agent MCP tool, drop report_result.cron
- **Commit:** `ee59130`
- **Summary:** TB-123 implementation landed in a2e3d6a (cron_propose MCP tool + dropped report_result.cron + contextvar plumb for proposed_by_task + unit/e2e/real-SDK smoke tests); previous attempt blocked only on the 'smokes still green' bullet because of a pre-existing TB-136 fallout in test_prose_judge_real_sdk.py — fixed in ee59130 by materializing the asserted file into project_root so the working-tree-authoritative judge can verify it. Full regression now 631 passed, both prose-judge smokes pass.
- **Files:** ap2/tests/smoke/test_prose_judge_real_sdk.py
- **Tests:** pass

## [2026-05-01] TB-138: Briefing prompts must require auto-verifiable Verification bullets only
- **Commit:** `13a7e99`
- **Summary:** Added the "auto-verifiable bullets only — no `Manual:` steps" rule at every briefing-authoring layer (ap2/ideation.default.md, skills/ap2-task/SKILL.md, skills/migrate-to-ap2/SKILL.md, ap2/init.py:BRIEFING_TEMPLATE), citing TB-122 as the canonical Manual→stubbed-e2e conversion example; added non-fatal `ap2 check` warning for `Manual:`/`[manual]` bullets inside `## Verification` (already surfaces a real hit in TB-121's briefing); pinned the prompt rule + template + lint behavior with 7 new unit tests across test_ideation_defaults.py and test_check.py; full regression gate `uv run pytest -q ap2/tests/` passes (638 tests, was 631); all 5 prose-grep verification bullets satisfied.
- **Files:** ap2/check.py, ap2/ideation.default.md, ap2/init.py, ap2/tests/test_check.py, ap2/tests/test_ideation_defaults.py, skills/ap2-task/SKILL.md, skills/migrate-to-ap2/SKILL.md
- **Tests:** pass

## [2026-05-01] TB-122: Concurrent Mattermost handler with restricted toolset during in-flight tasks
- **Commit:** `d82223b`
- **Summary:** TB-122 implementation already shipped in 5ebfae8 (main_loop → _main_tick_loop + _mm_loop, MM_HANDLER_TOOLS_RESTRICTED gate while a task is Active, asyncio.create_task per mention, toolset='restricted'|'full' on mattermost events, README + architecture docs); the only outstanding bullet was the TB-138-converted manual-stoch responsiveness gate, which this commit closes by adding test_mattermost_reply_lands_within_30s_of_mention_during_long_task to ap2/tests/e2e/test_concurrent_mm.py — drives a slow task responder + fast handler responder, captures the mention's enqueue timestamp, asserts the resulting mattermost_reply event lands within 30s, and pins toolset='restricted' so the in-flight branch is actually exercised; uv run pytest -q ap2/tests/ → 639 passed.
- **Files:** ap2/tests/e2e/test_concurrent_mm.py
- **Tests:** pass

## [2026-05-01] TB-139: Embed source timestamp in ap2 --version output
- **Commit:** `5805224`
- **Summary:** Added ap2.get_version() (base + PEP 440 local-version `+<short-sha>.<commit-ts>` from git log -1 on the package's own checkout, empty suffix when no .git/), wired through ap2 --version, ap2 status (text + JSON), and the daemon's daemon_start event (extracted to _emit_daemon_start for testability); 7 new unit tests cover both git-repo and non-git fallback paths plus parity between CLI/status/daemon-event strings; full regression gate `uv run pytest -q ap2/tests/` passes (646 passed in 71.86s).
- **Files:** ap2/__init__.py, ap2/cli.py, ap2/daemon.py, ap2/tests/test_cli.py
- **Tests:** pass

## [2026-05-01] TB-140: Refresh /ap2 + /ap2-task skills; add deploy script to ~/.claude
- **Commit:** `1a49343`
- **Summary:** Refreshed skills/ap2/SKILL.md for the post-TB-130/TB-131 status surface (pending operator-ops line, web URL, 5-section board, queue-routed unfreeze/backlog/delete); skills/ap2-task/SKILL.md already covered TB-131/132/134/135/138. Added scripts/deploy-skills.sh (rsync-based, dry-run default, --apply for the per-skill --delete mirror, preserves unrelated siblings like taskboard) plus an ap2 sandbox sync-skills CLI wrapper. New ap2/tests/test_deploy_skills.py (15 tests, all passing); full regression gate uv run pytest -q ap2/tests/ → 661 passed.
- **Files:** skills/ap2/SKILL.md, scripts/deploy-skills.sh, ap2/cli.py, ap2/sandbox.py, ap2/tests/test_deploy_skills.py
- **Tests:** pass

## [2026-05-01] TB-141: Operator queue: remove queue.jsonl from fence; defer CLAUDE.md bump to drain
- **Commit:** `ae6f098`
- **Summary:** Implementation already shipped in e45bde8 (queue.jsonl removed from TASK_AGENT_FENCED_PATHS; _allocate_id pure; CLAUDE.md bump deferred to drain_operator_queue end-of-pass; 5 unit + 2 e2e tests added; architecture.md updated). Prior run failed verification because the briefing's grep bullet (`grep -qE "operator_queue\.jsonl"`) was unsatisfiable — it also matched the legitimate operator_queue_path constructor on tools.py:638 and module-level comments. ae6f098 swaps in an anchored regex (`^\s*"\.cc-autopilot/operator_queue\.jsonl",\s*$`) that matches only a tuple-entry line shape, so it verifies the intended fact ("queue.jsonl is not in the tuple") without false positives. Verified: bullet exits 0, full pytest passes (669 tests).
- **Files:** .cc-autopilot/tasks/operator-queue-remove-queue-jsonl-from-f.md
- **Tests:** pass

## [2026-05-01] TB-145: MM handler: always use RESTRICTED toolset; drop the in-flight check
- **Commit:** `2390002`
- **Summary:** Collapsed MM_HANDLER_TOOLS_FULL/_RESTRICTED into a single unconditional MM_HANDLER_TOOLS list (drops cron_edit, ideation_state_write, board_edit); removed handle_message's board snapshot check + task_in_flight branching across daemon/prompts; rewrote test_concurrent_mm.py (unit + e2e), test_prompts.py, test_operator_queue.py, test_tb142_mm_queue_routing.py and added 4 new MM_HANDLER_TOOLS pin tests in test_tools.py; updated README + architecture.md. 711/711 tests pass; all briefing greps pass (no MM_HANDLER_TOOLS_FULL or MM_HANDLER_TOOLS_RESTRICTED anywhere; MM_HANDLER_TOOLS present in tools.py).
- **Files:** ap2/tools.py, ap2/daemon.py, ap2/prompts.py, ap2/README.md, ap2/architecture.md, ap2/tests/test_concurrent_mm.py, ap2/tests/test_prompts.py, ap2/tests/test_operator_queue.py, ap2/tests/test_tools.py, ap2/tests/e2e/test_concurrent_mm.py, ap2/tests/e2e/test_tb142_mm_queue_routing.py
- **Tests:** pass

## [2026-05-01] TB-142: MM handler RESTRICTED: drop board_edit; add `approve` to queue
- **Commit:** `e147764`
- **Summary:** TB-142 implementation already in HEAD (b62b492 + TB-145's rewrite at 2390002): board_edit absent from MM_HANDLER_TOOLS, approve added to OPERATOR_QUEUE_OPS with shared _approve_review_token helper between do_board_edit and drain_operator_queue, MM handler prompt routes board mutations through the queue (unconditional post-TB-145). Prior verification_failed was a stale briefing referencing the retired MM_HANDLER_TOOLS_RESTRICTED symbol that TB-145 collapsed into MM_HANDLER_TOOLS; e147764 updates the briefing's Verification section to match the post-TB-145 symbol and adds an explanatory note. uv run pytest -q ap2/tests/ → 711 passed; python3 -c assertion against MM_HANDLER_TOOLS passes.
- **Files:** .cc-autopilot/tasks/mm-handler-restricted-drop-board-edit-ad.md
- **Tests:** pass

## [2026-05-01] TB-146: Hide cron_edit from agent toolsets until a clear use case lands
- **Commit:** `65ca97a`
- **Summary:** Dropped mcp__autopilot__cron_edit from CONTROL_AGENT_TOOLS so no agent (cron/ideation/MM-handler) can mutate cron.yaml; cron schedule mutation is now operator-CLI-only (`ap2 cron edit`). Updated cron_edit + cron_propose MCP tool docstrings, the MM_HANDLER_TOOLS filter (cron_edit exclusion is now a defensive no-op), the control-agent prompt header + cron.yaml fence-line + MM handler restriction note (prompts.py), the ideation prompt (new "Cron proposals from task agents (TB-146)" section instructing surface-not-adopt), architecture.md / README.md / howto.md documentation, and test_mcp_inventory.py (added _OPERATOR_ONLY_ADVERTISED_TOOLS exception list for advertised-but-not-allowlisted tools). Added unit tests in test_tools.py (cron_edit absent from every agent toolset; do_cron_edit still callable from Python) and test_ideation_defaults.py (prompt surfaces proposals, does not adopt; TB-146 cross-ref pinned). Updated test_concurrent_mm.py strict-subset diff (only ideation_state_write + board_edit are dropped now that cron_edit isn't in CONTROL_AGENT_TOOLS to begin with) and refreshed test_daemon_recovery.py docstring. Fixed the briefing's stale verification command (MM_HANDLER_TOOLS_FULL/RESTRICTED were retired in TB-145 — now uses MM_HANDLER_TOOLS + TASK_AGENT_TOOLS). Full regression gate `uv run pytest -q ap2/tests/` passes (716/716).
- **Files:** ap2/tools.py, ap2/prompts.py, ap2/ideation.default.md, ap2/architecture.md, ap2/README.md, ap2/howto.md, ap2/tests/test_tools.py, ap2/tests/test_ideation_defaults.py, ap2/tests/test_concurrent_mm.py, ap2/tests/test_mcp_inventory.py, ap2/tests/test_daemon_recovery.py, .cc-autopilot/tasks/hide-cron-edit-from-agent-toolsets-until.md
- **Tests:** pass

## [2026-05-01] TB-143: Re-fence operator_queue.jsonl; exclude from violation check
- **Commit:** `570c6c2`
- **Summary:** Previously committed in 570c6c2: re-added operator_queue.jsonl to TASK_AGENT_FENCED_PATHS, introduced _VIOLATION_CHECK_EXCLUDED_PATHS=(events.jsonl, operator_queue.jsonl) in rollback.py, updated prompts._TASK_HEADER, and SDK disallowed_tools (via _TASK_DISALLOWED_TOOLS built from TASK_AGENT_FENCED_PATHS) blocks Edit/Write on operator_queue.jsonl. Verified all four briefing scope items present, all current Verification bullets pass (python3 -c assertions for tools/rollback succeed; disallowed_tools includes the Edit/Write blocks); full regression gate uv run pytest -q ap2/tests/ → 716 passed. Prior verification_failed was against the older grep-style bullets later rewritten to python3 -c by 3901b54.
- **Files:** ap2/tools.py, ap2/rollback.py, ap2/prompts.py, ap2/tests/test_rollback.py, ap2/tests/test_tools.py, ap2/tests/test_prompts.py, ap2/tests/test_daemon_recovery.py, ap2/tests/test_operator_queue.py
- **Tests:** pass

## [2026-05-01] TB-144: Hoist status-report into shared routine; expose as MCP tool
- **Commit:** `b0a5618`
- **Summary:** Implementation already shipped in cfcd19e (status_report.py, run_status_report routine, MCP tool, daemon delegation, MM handler routing, 716 pytest passing). Per-task verifier rolled it to Backlog because the briefing's third Verification bullet imported MM_HANDLER_TOOLS_FULL/_RESTRICTED — names TB-145 (2390002) collapsed into singular MM_HANDLER_TOOLS. This commit (b0a5618) updates the briefing's Scope item (4) and the assertion to use MM_HANDLER_TOOLS, preserving intent. Verified: pytest 716 passing; all 3 shell verification bullets now pass.
- **Files:** .cc-autopilot/tasks/hoist-status-report-into-shared-routine.md
- **Tests:** pass

## [2026-05-01] TB-147: Verifier: run shell bullets via /bin/bash, not /bin/sh
- **Commit:** `d3cb671`
- **Summary:** Pinned verify._run_shell_bullet to /bin/bash via subprocess.run(executable="/bin/bash"), with a TB-147 rationale comment at the call site so it doesn't get reverted to "more portable sh"; sole shell=True site in the verification path (verified by grep). Added 4 tests in test_verify_retry_diff.py (process substitution, [[ ]] conditional, genuine non-zero exit still fails, source-level pin via inspect.getsource). Full ap2 suite passes 720/720 (+4).
- **Files:** ap2/verify.py, ap2/tests/test_verify_retry_diff.py
- **Tests:** pass

## [2026-05-01] TB-148: Web UI: tint task_complete rows by status, not uniform green
- **Commit:** `d7f3d3b`
- **Summary:** _row_class now reads the full event dict and tints task_complete rows by status (complete=lifecycle/green, verification_failed=warning/orange, state_violation/error/timeout/incomplete/blocked/failed=failure/red, retry_exhausted=new frozen/dark-red, unknown=new neutral/gray); home + events pages reuse the renderer; added a collapsed legend on /events; full ap2/tests/ pass (725).
- **Files:** ap2/web.py, ap2/tests/test_web.py
- **Tests:** pass

## [2026-05-01] TB-121: Gate ideation-proposed tasks behind human review before dispatch
- **Commit:** `44ebabc`
- **Summary:** Closed TB-121's lone failing verification bullet (verifier flagged the seeded e2e for not actually firing ideation): added test_ideation_cron_proposals_are_all_review_gated to ap2/tests/e2e/test_review_gate.py, which runs _tick with an empty board, ideation enabled, cooldown elapsed, no prompt override (real ap2/ideation.default.md), and a FakeSDK stand-in that mimics the prompt's TB-121 'Human-review gate' section by routing 3 add_backlog calls through tools.do_board_edit with blocked_on='review'; asserts ideation_empty_board fired, 3 Backlog tasks landed, every task has Task.blocked_on==['review'], TASKS.md renders 3x `@blocked:review` codespans, next_dispatchable('Backlog') is None, and the dumped ideation prompt the daemon sent the SDK contains 'blocked on: review' (proves the prompt-change reached the agent, not just that the fake agent was well-behaved). 752 tests pass (was 751; +1). Pairs with the existing test_ideation_prompt_pins_review_gate_clause prompt-pin so the directive + infrastructure carry through together.
- **Files:** ap2/tests/e2e/test_review_gate.py
- **Tests:** pass

## [2026-05-02] TB-149: Add `mattermost_thread_read` MCP tool for chat conversation context
- **Commit:** `d0802e43`
- **Summary:** Added mattermost_thread_read MCP tool: ap2.mattermost.fetch_thread() (chronological, oldest-end truncation, reuses users cache), do_mattermost_thread_read in tools.py with _err on missing env, wired into MM_HANDLER_TOOLS only (cron/ideation/task agents don't need it), and build_mattermost_prompt now adds a "Thread context" section with the embedded thread_id when the incoming message is a thread reply. New tests in test_mattermost.py (3), test_tools.py (3), test_prompts.py (2), and e2e/test_tb149_mm_thread_read.py (1); updated test_concurrent_mm.py and test_mcp_inventory.py to reflect the new MM-handler-only addition. Full ap2 regression gate green: 761 passed.
- **Files:** ap2/mattermost.py, ap2/tools.py, ap2/prompts.py, ap2/tests/test_mattermost.py, ap2/tests/test_tools.py, ap2/tests/test_prompts.py, ap2/tests/test_concurrent_mm.py, ap2/tests/test_mcp_inventory.py, ap2/tests/e2e/test_tb149_mm_thread_read.py
- **Tests:** pass

## [2026-05-04] TB-154: Validate briefing structure at queue-append time
- **Commit:** `13896a5`
- **Summary:** Closed the gap from TB-154's first attempt: wired _validate_briefing_structure into the update-op branch of do_operator_queue_append (before the slug-stable briefing write) and added test_tb154_validate_briefing_structure_fires_for_update_op covering Acceptance-rename, missing-Verification, empty-Verification reject paths plus a canonical-accept sanity. Tightened the operator_queue_append docstring to name update alongside add_*. Full suite (814 tests) green; previous TB-154 work from 54a7f6e (validator + add_* wiring + check.py lint + prompts.py + init.py constant) remains in main.
- **Files:** ap2/tools.py, ap2/tests/test_tools.py
- **Tests:** pass

## [2026-05-04] TB-151: Surface pending-review TB-Ns (not just count) in `ap2 status` and cron status-report
- **Commit:** `65ccc76`
- **Summary:** Added shared `_format_pending_review_line` (status_report.py) + `pending_review_ids` collection so `ap2 status` and the cron status-report both name the TB-Ns (truncated to 5 with "(+N more)"); injected a "Pending operator review (N): TB-..." line into the cron snapshot block via a new `state_extras` kwarg on `build_control_prompt` and updated STATUS_REPORT_PROMPT to forward it verbatim. Full ap2/tests/ gate passes (831).
- **Files:** ap2/cli.py, ap2/prompts.py, ap2/status_report.py, ap2/tests/test_cli.py, ap2/tests/test_status_report_skip.py, ap2/tests/test_tools.py, ap2/tests/e2e/test_tb144_status_report_chat_trigger.py
- **Tests:** pass

## [2026-05-04] TB-153: `ap2 update` op for in-place task / briefing edits
- **Commit:** `aa27bd1`
- **Summary:** Closed both gaps from TB-153's first attempt (commit 9101007): made ap2/verify.py's mistune import lazy via _get_md() so `python3 -c "from ap2.tools import OPERATOR_QUEUE_OPS"` no longer trips on system-python's missing mistune (the chain became broken when TB-154 wired verify into tools.py); added task_updated `fields=[...]` event assertions to each per-field round-trip test (title/tags/blocked/description/briefing/clear_tags/clear_blocked) via a new `_last_task_updated` helper. Full ap2 test suite (831 tests) passes.
- **Files:** ap2/verify.py, ap2/tests/test_operator_queue.py
- **Tests:** pass

## [2026-05-04] TB-155: Web port auto-enumerate on conflict
- **Commit:** `9dcff0d`
- **Summary:** Closed the single failing per-task verification criterion from TB-155's first attempt (649eca3): added focused `test_serve_async_no_conflict_binds_start_port` in test_web.py exercising `serve_async(start_port=X)` directly when X is free and asserting `on_bind` fires with bound==start_port, plus an `assert "requested_port" not in starts[0]` to test_web_loop_emits_start_and_stop in test_daemon_web.py so the no-conflict audit signal stays meaningful. No production-code changes — the daemon wrapper already omits requested_port on the happy path; this locks that behavior in tests. `uv run pytest -q ap2/tests/` → 845 passed.
- **Files:** ap2/tests/test_web.py, ap2/tests/test_daemon_web.py
- **Tests:** pass

## [2026-05-04] TB-157: Token-usage instrumentation across all SDK call sites
- **Commit:** `95ec926`
- **Summary:** Captured usage/model_usage in _summarize_message + _serialize_message_full; wired judge_call event emission in verify._judge_prose_bullet (threaded events_file/task_id/bullet_idx through verify_task); added per-run usage totals footer + ?show=tokens opt-in column + inline judge_call token summary in web.py; built adhoc/token_breakdown.py aggregator (gitignored) grouping by agent-kind run-id pattern; new tests in test_daemon_message_log.py, e2e/test_verify_per_task.py, test_web.py — full ap2/tests/ suite passes (856 passed in 80.94s).
- **Files:** ap2/daemon.py, ap2/verify.py, ap2/web.py, ap2/tests/test_daemon_message_log.py, ap2/tests/test_web.py, ap2/tests/e2e/test_verify_per_task.py, adhoc/token_breakdown.py
- **Tests:** pass

## [2026-05-04] TB-156: Tier-1 token tuning: diff trim + per-agent effort lowering
- **Commit:** `60c60ff`
- **Summary:** TB-156 implementation already landed in a4b085c (judge diff cap 100KB→30KB, AP2_VERIFY_JUDGE_EFFORT default high, AP2_STATUS_REPORT_EFFORT default medium, plus per-site precedence tests + 30KB diff-truncation test); first run failed verification only because briefing's `grep -qE "AP2_STATUS_REPORT_EFFORT" ap2/` was missing `-r` and exited 2 on the directory arg. New commit 60c60ff fixes the briefing typo (`-qE` → `-qrE`); all four shell bullets now exit 0 and `uv run pytest -q ap2/tests/` reports 856 passed.
- **Files:** .cc-autopilot/tasks/tier-1-token-tuning-diff-trim-per-agent.md
- **Tests:** pass

## [2026-05-04] TB-158: Surface bullet failures clearly in events logs (CLI + web)
- **Commit:** `cad5404`
- **Summary:** Added shared `events.summarize_verification_failed` helper and wired it into `ap2 logs` (cmd_logs renders counter + failing-bullet headlines + judge notes for verification_failed rows; --json path unchanged), `/events` (inline counter + failed-bullet sub-list, passes/unverified collapsed into the counter), and `/task-run/<run-id>` (top-of-page verification summary block when terminal verdict is verification_failed, fires on the latest matching event so retries don't surface stale failures). 868 ap2 tests pass; new tests in test_events.py / test_cli.py / test_web.py pin shape, sort, truncation, fallback, --json regression, inline rendering, summary block, and cross-file grep visibility.
- **Files:** ap2/events.py, ap2/cli.py, ap2/web.py, ap2/tests/test_events.py, ap2/tests/test_cli.py, ap2/tests/test_web.py
- **Tests:** pass

## [2026-05-04] TB-152: `ap2 reject TB-N` (CLI + chat) — capture rejection reasons in operator_log.md for ideation learning
- **Commit:** `8bc5297`
- **Summary:** Added `ap2 reject TB-N [--reason ...]` (CLI + MM-handler chat) — registered the `reject` op in OPERATOR_QUEUE_OPS, gated it to Backlog + `@blocked:review` proposals (else routes operator at `ap2 delete`), drain emits `<ts> — rejected ideation proposal → TB-N (<title>): <reason>` to operator_log.md (placeholder `(no reason given)` when omitted) plus the standard `applied operator-queued reject → TB-N` line, with full test coverage; `uv run pytest -q ap2/tests/` 886 passed in 70s.
- **Files:** ap2/tools.py, ap2/cli.py, ap2/prompts.py, ap2/README.md, skills/ap2/SKILL.md, ap2/tests/test_cli.py, ap2/tests/test_operator_queue.py, ap2/tests/test_prompts.py
- **Tests:** pass

## [2026-05-04] TB-162: Surface pending operator queue ops in the web view
- **Commit:** `3524f34`
- **Summary:** Added `_render_pending_queue(cfg)` helper + `.pending-queue` CSS in `ap2/web.py`, wired into `_render_home` above the events table; reads `operator_queue.jsonl`, filters against `operator_queue_state.json`'s applied-set, omits the card server-side when empty, renders op badge + task_id + HH:MM:SSZ ts + 8-char uuid prefix + per-op-kind summary (`title="..."` for add_*, `fields=<csv>` for update, none for approve/etc.); 5 new tests in `ap2/tests/test_web.py` cover three-op rendering, empty-state omission, uuid truncation, drained-entry filter, grep-visibility; full gate `uv run pytest -q ap2/tests/` 891 passed.
- **Files:** ap2/web.py, ap2/tests/test_web.py
- **Tests:** pass

## [2026-05-04] TB-160: Make ideation trigger threshold configurable via `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3)
- **Commit:** `7701a1c`
- **Summary:** Made ideation trigger threshold configurable via AP2_IDEATION_TRIGGER_TASK_COUNT (default 3). Replaced boolean has_work gate with Active hard-gate + Ready+Backlog count comparison; added _trigger_task_count() helper mirroring _cooldown_s; updated header docstring + architecture.md + howto.md; added 10 new tests in test_ideation_trigger.py covering default constant, env parsing fallback, threshold semantics, Active hard-gate independence, >= boundary, and Pipeline-Pending/Frozen exclusion. Full ap2 suite (901 tests) passes.
- **Files:** ap2/ideation.py, ap2/architecture.md, ap2/howto.md, ap2/tests/test_ideation_trigger.py
- **Tests:** pass

## [2026-05-04] TB-159: `ap2 ideate` CLI for manual ideation trigger that bypasses the natural gates
- **Commit:** `987c5cf`
- **Summary:** Added `ap2 ideate [--force]` — a manual operator trigger for an ideation pass that bypasses the natural empty-board / cooldown / `AP2_IDEATION_DISABLED` gates. Routed through the operator queue (registered `ideate` in OPERATOR_QUEUE_OPS); drain emits `ideation_forced` event + `(forced)` operator_log.md audit line and signals `_tick` via a new `force_ideate` key in `drain_operator_queue`'s return dict. Default refuses on Active-task; `--force` overrides. Refactored `_maybe_ideate` to share `_run_ideation` with new `force_ideate` helper that still calls `mark_run` so back-to-back forced fires don't lap the next natural cooldown. Full ap2/tests/ suite (923 tests) passes.
- **Files:** ap2/cli.py, ap2/daemon.py, ap2/ideation.py, ap2/tools.py, ap2/tests/test_cli.py, ap2/tests/test_ideation_trigger.py, ap2/tests/test_operator_queue.py
- **Tests:** pass

## [2026-05-04] TB-165: Persist task-run token usage in events.jsonl + retain debug dumps on success
- **Commit:** `26ac188`
- **Summary:** Implementation already landed in 481655d (task_run_usage event on every terminal run_task path, debug-dump retention on success, _prep_debug_dumps docstring update, 5 pinning tests in test_daemon_recovery.py). Prior verification failed on a single typo'd shell bullet — `grep -nE '"task_run_usage"' ap2/tests/` exits 2 because grep needs `-r` for directory arguments. This commit fixes the briefing's bullet to `grep -rnE …`, which now exits 0 and satisfies the verifier; all 923 ap2/tests pass.
- **Files:** .cc-autopilot/tasks/persist-task-run-token-usage-in-events-j.md
- **Tests:** pass

## [2026-05-04] TB-167: Default `ap2 add` section to Backlog (was Ready)
- **Commit:** `718bb29`
- **Summary:** Defaulted `ap2 add`'s `-s/--section` to Backlog (was Ready) so operator-filed adds match ideation proposals' triage semantics and `--blocked review` surfaces in `ap2 status`. Explicit `-s Ready`/`-s Frozen` still route through `add_ready`/`add_frozen`. Added 5 regression tests (argparse default, default-routes-to-add_backlog, explicit Ready/Frozen, and default+--blocked review surfaces in cmd_status pending_review_ids); full ap2 suite passes (928 tests).
- **Files:** ap2/cli.py, ap2/README.md, ap2/tests/test_cli.py
- **Tests:** pass

## [2026-05-04] TB-166: Persist control-agent token usage + stream/messages dumps for ideation, cron, MM handler
- **Commit:** `efe1996`
- **Summary:** Followup to 7131e71 (substance unchanged). Fixed the verification grep typo in the briefing (`grep -nE` → `grep -rnE` on `ap2/tests/`, same fix TB-165 needed) and added `test_ideation_error_emits_both_events` which routes a raising SDK stub through `_run_ideation` and asserts BOTH `control_run_usage` (status=error) AND the pre-existing `ideation_error` event fire — pinning the additive-event contract that the prose error-path bullet required end-to-end. `uv run pytest -q ap2/tests/` → 938 passed.
- **Files:** .cc-autopilot/tasks/persist-control-agent-token-usage-stream.md, ap2/tests/test_control_run_usage.py
- **Tests:** pass

## [2026-05-04] TB-161: Briefing validator: require Goal section to cite a goal.md focus item or Done-when bullet
- **Commit:** `35364bd`
- **Summary:** Extended `_validate_briefing_structure` (TB-154) with a TB-161 goal-anchor check: the briefing's `## Goal` body must cite (as a substring) one of goal.md's `## Current focus` heading titles or `## Done when` bullets, derived via the new `GOAL_ANCHOR_HEADINGS` constant in ap2/init.py and the `_goal_md_anchors` helper in ap2/tools.py; ap2/check.py mirrors the rule as a warning-level lint; ap2/prompts.py + ideation.default.md + the operator_queue_append MCP docstring carry the new requirement; falls back to skip-the-check when goal.md is missing or all-placeholder. Full regression (949 tests) passes.
- **Files:** ap2/check.py, ap2/ideation.default.md, ap2/init.py, ap2/prompts.py, ap2/tests/test_check.py, ap2/tests/test_tools.py, ap2/tools.py
- **Tests:** pass

## [2026-05-04] TB-163: Inject "Recent operator rejections (last 5)" block into ideation prompt header
- **Commit:** `aa86c18`
- **Summary:** Added "Recent operator rejections (last K)" subsection to build_control_prompt's snapshot header, backed by new ap2/operator_log.py::tail_rejections helper; ideation prompt directive added; full ap2 test suite (952 tests) passes.
- **Files:** ap2/operator_log.py, ap2/prompts.py, ap2/ideation.default.md, ap2/tests/test_prompts.py
- **Tests:** pass

## [2026-05-04] TB-164: Briefing validator: require non-empty "Why now" rationale within Goal section
- **Commit:** `2ce0b9f`
- **Summary:** Extended _validate_briefing_structure with a line-anchored Why-now rationale check (≥40 chars after marker via new WHY_NOW_MIN_CHARS constant); added matching warning-level lint in check.py, extended BRIEFING_TEMPLATE / operator_queue_append docstring / MM prompt / ideation.default.md, plus 11 new tests across test_tools.py + test_check.py and Why-now lines added to existing test fixtures across 17 files. Full regression: 963 passed.
- **Files:** ap2/init.py, ap2/tools.py, ap2/check.py, ap2/prompts.py, ap2/ideation.default.md, ap2/tests/test_tools.py, ap2/tests/test_check.py, ap2/tests/test_cli.py, ap2/tests/test_operator_queue.py, ap2/tests/test_rollback.py, ap2/tests/test_tb132_verification.py, ap2/tests/test_tb135_verification.py, ap2/tests/e2e/test_mattermost_cron.py, ap2/tests/e2e/test_operator_queue_tick.py, ap2/tests/e2e/test_review_gate.py, ap2/tests/e2e/test_tb142_mm_queue_routing.py, ap2/tests/e2e/test_verify.py, ap2/tests/e2e/test_verify_per_task.py
- **Tests:** pass

## [2026-05-04] TB-168: Trim `_current_state_block` for ideation: drop board counts + recent commits, keep `now:`
- **Commit:** `c113f4c`
- **Summary:** Added include_board/include_commits kwargs to _current_state_block, forwarded through build_control_prompt, and opted ideation._run_ideation out of both sub-blocks; defaults stay True so status-report cron rendering is byte-identical. Full ap2/tests/ gate (969) passes.
- **Files:** ap2/prompts.py, ap2/ideation.py, ap2/tests/test_prompts.py, ap2/tests/test_ideation_trigger.py
- **Tests:** pass

## [2026-05-04] TB-169: Trim ideation's `_events_block` to a curated allowlist of event types
- **Commit:** `0d4fd53`
- **Summary:** Added include_types allowlist kwarg to _events_block and build_control_prompt (default None preserves status-report behavior); defined IDEATION_RELEVANT_EVENT_TYPES (9 entries: task lifecycle, operator decisions, cron_proposed); wired _run_ideation to pass it; added unit tests (filter positive/negative, empty-after-filter fallback, default-no-kwarg backwards-compat, build_control_prompt forwarding) and an end-to-end ideation test asserting captured prompt has task_complete but not judge_call/cron_complete; full pytest suite (975 tests) passes.
- **Files:** ap2/ideation.py, ap2/prompts.py, ap2/tests/test_prompts.py, ap2/tests/test_ideation_trigger.py
- **Tests:** pass
