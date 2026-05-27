# Bump default status-report cron interval from 2h to 8h

Tags: #autopilot #cron #status-report #defaults

## Goal

`ap2/cron.default.yaml` ships `interval: 2h` for the status-report
job, written verbatim into fresh projects' `.cc-autopilot/cron.yaml`
on `ap2 init`. This project's own `.cc-autopilot/cron.yaml` (tracked
in git) currently uses the same 2h default. Pre-TB-282 / TB-297 the
2h cadence was the ONLY operator push surface — every time-sensitive
condition (`auto_approve_paused`, `cost_cap_approach`, `task_stuck`)
had to wait up to 2h to surface, so the cadence had to be tight.
After TB-297 immediate-Mattermost-push (opt-in
`AP2_ATTENTION_IMMEDIATE_PUSH`) those time-sensitive conditions
reach the operator in one tick (≤30s); the routine status-report's
role narrows to "rhythm-of-the-day digest," which is comfortably an
8h cadence (3 posts/day). Combined with the TB-128 idle + TB-281
dedup skip-gate the effective cadence is "8h ceiling, post when
something has changed" — not a strict drumbeat.

Bump the default from `2h` to `8h` so the goal.md "Current focus:
operator-legible reporting and monitoring" walk-away contract no
longer over-spends the operator's signal budget (and the SDK cost
window) on a cadence that pre-dates the immediate-push channel.

Why now: TB-297 closed the time-sensitive-conditions gap that
originally justified the tight 2h cadence; the rationale moved but
the cadence itself hasn't followed. The bump is a single-line
default change plus one test-math fix; deferring it keeps every
fresh ap2 project on a noisier-than-needed default.

## Scope

- `ap2/cron.default.yaml` — change `interval: 2h` → `interval: 8h`
  for the `status-report` job entry. Update the two `2h cron post`
  prose mentions inside this file's prompt-stub comment block to
  read `8h cron post`. Do NOT touch the prompt body itself (TB-144
  the stub has no runtime effect — but the prose is operator-facing
  via `ap2 cron list`).

- `.cc-autopilot/cron.yaml` (this project's tracked cron config) —
  change `interval: 2h` → `interval: 8h` so this project's own
  daemon picks up the new cadence on next restart. The cron-job
  interval is wired at daemon-start (not hot-reloaded), so the
  effective cadence shifts only on the next `ap2 stop && ap2 start`.

- `ap2/tests/test_diagnose.py::test_cron_status_overdue_detection`
  — the test asserts `last_fired = fake_now - 5h` is overdue against
  `2 * 2h = 4h`. Update the `last_fired` offset to `fake_now -
  (20 * 3600)` (20h, overdue against the new `2 * 8h = 16h`
  threshold) and refresh the two inline comments (`# 5h > 2h*2 = 4h
  → overdue` and `# 5h > 4h (2 * 2h)`) to reflect the new
  arithmetic.

## Design

- `cron.default.yaml` is the single source-of-truth for the
  fresh-project default; `_ensure_cron_file` (`ap2/cron.py:94-108`)
  copies it verbatim. No other code path defaults the interval —
  `parse_interval` just decodes whatever string is in the file.

- The skip-gate `_status_report_skip_decision`
  (`ap2/status_report.py:1726`) is cadence-agnostic — it suppresses
  based on event-window emptiness and content fingerprint, not on
  time elapsed. An 8h-cadence project that's actively moving still
  posts whenever the structural fingerprint changes; the cap is
  "no more than once per 8h," not "exactly once per 8h."

- The `test_cron_status_overdue_detection` test uses hardcoded
  arithmetic (5h literal vs `2 * 2h` threshold). Pinning the bump
  in the test is the minimum fix. A more general refactor (compute
  the threshold from the loaded cron config rather than hardcoding
  the multiplier) is out of scope — the test is asserting one
  scenario, not the cadence semantics.

- The ~25 prose mentions of "2h" scattered across `ap2/howto.md` /
  `ap2/architecture.md` / `ap2/status_report.py` docstrings /
  `ap2/config.py` comments / `ap2/events.py` / `ap2/automation_status.py`
  / several test docstrings are stale-on-bump but NOT runtime-affecting.
  Scrubbing them is a follow-up task (a docs-drift gate or a one-off
  prose sweep). Keep this briefing tightly scoped to the cadence
  change + the one breaking test.

## Verification

- `uv run pytest -q ap2/tests/test_diagnose.py` — the
  cron-status-overdue test passes against the new 16h threshold.

- `uv run pytest -q ap2/tests/` — full suite still passes
  (regression pin against any other test that consumed the 2h
  literal that I didn't surface in Scope).

- `grep -q "^    interval: 8h$" ap2/cron.default.yaml` — the
  status-report job's interval line reads `interval: 8h`.

- `grep -q "^    interval: 8h$" .cc-autopilot/cron.yaml` — this
  project's own cron.yaml is updated in lock-step.

- `! grep -qE "^    interval: 2h$" ap2/cron.default.yaml` — the
  old `2h` interval line no longer appears in the default.

- `! grep -qE "^    interval: 2h$" .cc-autopilot/cron.yaml` — and
  not in this project's tracked cron.yaml either.

- `ap2/cron.default.yaml` Prose: the file's `status-report` entry
  carries `interval: 8h` (not `2h`), and the two prompt-stub prose
  references inside the entry consistently say `8h cron post` (not
  `2h cron post`). Judge confirms via Read.

- `ap2/tests/test_diagnose.py` Prose: the
  `test_cron_status_overdue_detection` test uses a `last_fired`
  offset of `fake_now - (20 * 3600)` (or equivalent overdue-against-16h
  math) and the inline comments reflect the new 8h-interval
  arithmetic. Judge confirms via Read.

## Out of scope

- Scrubbing the ~25 prose mentions of "2h" that remain in
  `ap2/howto.md` / `ap2/architecture.md` / docstrings / test
  comments. Those are stale-on-bump but runtime-inert; a follow-up
  prose-sweep task can clean them.

- Refactoring the cadence value-agnostic in the test (reading the
  interval from `cron_default_yaml` rather than hardcoding the
  multiplier).

- Adding a runtime knob (`AP2_STATUS_REPORT_INTERVAL`) to make the
  cadence env-tunable without editing `cron.yaml`. The cron.yaml is
  already the per-project tunable surface; another knob duplicates
  that.

- Bumping the cadence on already-initialized non-autopilot2 projects
  (their `cron.yaml` is operator-owned post-init; this briefing
  changes only the fresh-project default + this one project).

- Re-running the daemon (`ap2 stop && ap2 start`) to pick up the new
  cadence on this project. The interval change in `.cc-autopilot/cron.yaml`
  is a FIXED_KNOB-class change wired at daemon-start; the operator
  restarts the daemon when convenient.
## Attempts

### 2026-05-27 — incomplete
Bumped ap2/cron.default.yaml status-report interval 2h→8h (incl. two "2h cron post" prose mentions in the stub) and pinned three downstream tests (test_diagnose::test_cron_status_overdue_detection last_fired 5h→20h; test_cron_defaults::test_default_cron_intervals_are_sane upper bound 4*3600→8*3600; test_status_report_skip::test_cron_default_status_report_prompt_is_stub interval_s 7200→28800). Full suite 2176/2176 passes. The briefing's `.cc-autopilot/cron.yaml` bullets cannot be satisfied from inside a task agent — that file is triply fenced (SDK denies Edit on the .cc-autopilot/ dir; CLAUDE.md forbids `sed -i`/`echo >` workarounds; `ap2 cron edit update status-report --interval 8h` CLI refuses while a task is active with `state_violation` rollback warning). Operator follow-up: run `ap2 cron edit update status-report --interval 8h` once this task lands, then `ap2 stop && ap2 start` to pick up the new cadence (the briefing already noted the restart is out-of-scope for the task). The fresh-project default is correct; only the already-initialized project-local cron.yaml needs the operator-side bump.
- **commit:** e3ca933
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260527T194132Z-TB-306.prompt.md`, `stream: .cc-autopilot/debug/20260527T194132Z-TB-306.stream.jsonl`, `messages: .cc-autopilot/debug/20260527T194132Z-TB-306.messages.jsonl`
### 2026-05-27 — blocked
Prior commit e3ca933 covers fresh-project default (ap2/cron.default.yaml 2h→8h + two prose mentions) + three downstream test pins (test_diagnose::test_cron_status_overdue_detection, test_cron_defaults::test_default_cron_intervals_are_sane, test_status_report_skip::test_cron_default_status_report_prompt_is_stub). Full suite 2176/2176 still passes this turn. The remaining briefing bullet — `.cc-autopilot/cron.yaml` 2h→8h with 4-space indent preserved — cannot be satisfied from any task-agent path: (a) Edit/Write are SDK-fenced on `.cc-autopilot/` (re-confirmed this turn: 'File is in a directory that is denied by your permission settings'); (b) `cron_edit` MCP succeeds but PyYAML safe_dump reformats to 2-space indent, breaking the briefing's strict `grep -q "^    interval: 8h$"` (4-space) verifier — I exercised this path then `git restore`d to keep the working tree clean; (c) sed/echo bash workarounds are CLAUDE.md-forbidden; (d) `ap2 cron edit update status-report --interval 8h` CLI refuses while a task is active with state_violation (per prior attempt). None of the four published `AP2_AUTO_UNFREEZE_FIX_SHAPES` (grep_missing_r_on_dir / bare_python_to_uv_run / literal_backtick_in_shell_bullet / bare_path_to_test_f) covers this whitespace-strict-grep pattern, so no BriefingFix line. Operator follow-up: surgical 4-space-preserving edit of `.cc-autopilot/cron.yaml` outside the daemon (or `ap2 cron edit` once task is Frozen and no longer Active) followed by `ap2 stop && ap2 start` to pick up the new cadence; OR relax the briefing's two `^    interval: …$` greps to `^\s*interval: …$` and re-dispatch.
- **commit:** e3ca933
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260527T194920Z-TB-306.prompt.md`, `stream: .cc-autopilot/debug/20260527T194920Z-TB-306.stream.jsonl`, `messages: .cc-autopilot/debug/20260527T194920Z-TB-306.messages.jsonl`
### 2026-05-27 — blocked
Third dispatch reproduces the same block as the second attempt; no new commit needed. Prior commit e3ca933 covers everything reachable from a task agent: ap2/cron.default.yaml interval 2h→8h plus both "8h cron post" prose mentions, and three downstream test pins (test_diagnose::test_cron_status_overdue_detection 5h→20h, test_cron_defaults::test_default_cron_intervals_are_sane upper bound 4*3600→8*3600, test_status_report_skip::test_cron_default_status_report_prompt_is_stub interval_s 7200→28800). Full suite 2176/2176 still passes; test_diagnose alone re-verified this turn (17 passed). Remaining gap is .cc-autopilot/cron.yaml interval: 2h→8h. All four edit paths fail: (a) Edit/Write SDK-fenced on .cc-autopilot/ — re-confirmed this turn with "File is in a directory that is denied by your permission settings"; (b) sed -i / echo > path CLAUDE.md-forbidden; (c) ap2 cron edit CLI refuses with state_violation while task is Active; (d) mcp__autopilot__cron_edit succeeds but ap2/cron.py:114-119 calls yaml.safe_dump without an indent= override, producing 2-space output that breaks the briefing's strict `grep -q "^    interval: 8h$"` (4-space) verifier — confirmed via `python3 -c "import yaml; print(yaml.safe_dump(...))"`. None of the four published AP2_AUTO_UNFREEZE_FIX_SHAPES (grep_missing_r_on_dir, bare_python_to_uv_run, literal_backtick_in_shell_bullet, bare_path_to_test_f) covers a whitespace-strict-grep pattern, so no BriefingFix line. Operator follow-up (one of): (1) surgical 4-space-preserving edit of .cc-autopilot/cron.yaml outside the daemon then `ap2 stop && ap2 start`; (2) `ap2 cron edit update status-report --interval 8h` once TB-306 is Frozen and no longer Active (file becomes 2-space but is semantically correct — relax-grep follow-up needed); (3) relax the briefing's two `^    interval: …$` greps to `^\s*interval: …$` and re-dispatch.
- **commit:** e3ca933
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260527T195622Z-TB-306.prompt.md`, `stream: .cc-autopilot/debug/20260527T195622Z-TB-306.stream.jsonl`, `messages: .cc-autopilot/debug/20260527T195622Z-TB-306.messages.jsonl`
