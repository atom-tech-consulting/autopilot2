# Surface stale `.cc-autopilot/env` (mtime > daemon-start) in `ap2 status` + cron status-report digest + watchdog

Tags: #autopilot #observability #env #operator-surface #regression-pin

## Goal

Today, edits to `.cc-autopilot/env` only take effect after a daemon restart, but neither `ap2 status` nor the watchdog / cron status-report tells the operator a restart is needed. Concrete cost: TB-255 hit `verification_failed` at `duration_s=600.01s` on 2026-05-18T17:38Z against the old 600s default, ~26h after `AP2_VERIFY_TIMEOUT_S` had been bumped to 1800s in the env file (TB-249/TB-245-driven bump, env file comment timestamps it 2026-05-17). The daemon hadn't restarted in between (next `daemon_stop`/`daemon_start` pair was 2026-05-18T18:58Z), so the in-memory `Config` still held the old 600s ceiling. Result: `retry_exhausted` → Frozen → operator manually unfroze → re-ran and completed cleanly against the same already-committed `891c406`.

Goal anchor: this directly serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." The TB-255 sequence (verification fails → retries exhaust → operator manually unfreezes) is exactly the failure-recovery-not-automatic gap that bullet calls out. The proximate cause was a silently-stale env; the fix is to make that staleness visible at the operator surface so the operator's restart-when-bumping mental model gets a loud reminder when they forget.

Why now: without this surface, every future env bump risks an identical TB-255-shape silent-window between knob edit and daemon restart. Two bumps have already landed on this env file in the past 60 days (`AP2_VERIFY_TIMEOUT_S` 600 → 1800, `AP2_CONTROL_TIMEOUT_S` 300 → 1800 per the env-file comments), and the operator hit a real cost on one of them. Future tuning of validator-judge / ideation / task budgets will keep editing this file, and each edit re-opens the silent window until ship.

## Scope

- `ap2/daemon.py` — on startup, capture `env_file_mtime_at_start = (project_root / ".cc-autopilot/env").stat().st_mtime` and stash it on the daemon-state surface (state file or module var consumable by `cmd_status`).
- `ap2/cli.py:cmd_status` — read `env_file_mtime_at_start` and current env file mtime; when current > at-start, emit a WARN line in the text output and an `env_stale` field in the `--json` output.
- `ap2/automation_status.py` or wherever the cron status-report context block is composed — surface the env-stale flag so the agent's prompt context includes it and the resulting Mattermost digest can carry the warning.
- `ap2/daemon.py` watchdog (`auto_diagnose_fired` summary composition) — include env-stale flag in the diagnose summary block.
- `ap2/tests/` — new regression-pin test module `test_tb_<N>_env_mtime_stale_*.py`.

## Design

- Capture-on-startup, not per-tick: the `Config` dataclass is built once at daemon start and threaded everywhere; making env values effectively-live silently would surprise the operator. Explicit "needs restart" is the right contract.
- Persistence shape: write `env_file_mtime_at_start` to `.cc-autopilot/daemon_state.json` (or whichever state file the daemon currently uses for runtime-introspection facts) so `cmd_status` from a separate process can read it without going through the daemon's PID.
- WARN message shape: `WARN: .cc-autopilot/env modified at <iso-ts> (after daemon start at <iso-ts>) — restart with 'ap2 stop && ap2 start' to apply changes`. Keep the remediation command in the message so the operator doesn't need to look it up.
- `--json` mode: add `env_stale: bool` and `env_file_mtime: <iso-ts>` fields to the existing status JSON payload.
- Cron status-report integration: thread the env-stale flag through the same context block path that surfaces other walk-away-relevant flags (the cron agent already reads board + recent events + audit count post-TB-258; this is a sibling fact).
- Watchdog integration: when `auto_diagnose_fired` composes its summary, include a one-line `env-stale: yes (modified <ts>)` block when applicable.
- No auto-reload: the design explicitly does NOT re-source `.cc-autopilot/env` per tick. That's a different, more invasive change (Config rebuild + ensuring no caller holds a stale value) and is punted to a future TB if warn-and-restart proves insufficient.

## Verification

- `uv run pytest -q ap2/tests/` — full project suite passes.
- prose: `ap2/cli.py:cmd_status` reads the env file's current mtime, compares against the captured daemon-start mtime, and emits a WARN line in stdout when the current mtime is later.
- prose: a regression-pin test module (`test_tb_<N>_env_mtime_stale_*.py`) covers at minimum (a) fresh daemon-start with env unchanged → no warn line, (b) env file touched after daemon start → warn line present in `ap2 status` text output, (c) same condition surfaces an `env_stale: true` field in `ap2 status --json` output.
- prose: the cron status-report context-block composer (in `ap2/automation_status.py` or wherever the cron-prompt context is assembled) includes the env-stale flag in its rendered context so the agent's posted digest can carry the warning.
- prose: the watchdog (`auto_diagnose_fired`) summary composition includes a line surfacing env-stale state when applicable.
- `grep -rE 'env_file_mtime|env_stale' ap2/ --include='*.py' | grep -v test_ | wc -l | awk '$1 > 0 { exit 0 } { exit 1 }'` — implementation symbol exists in non-test code.

## Out of scope

- Auto-reloading `.cc-autopilot/env` at runtime — Config dataclass is built once and threaded everywhere; making env live silently could surprise the operator. Explicit operator restart is the right contract for this TB; revisit only if warn-and-restart proves insufficient in practice.
- Per-tick re-source of `.cc-autopilot/env` — different design, more invasive (Config rebuild + audit of all dataclass-cached values); punted to a future TB.
- Mattermost direct-mention notification on every detection — debounce concerns punted; the cron status-report digest's once-per-cycle surface is enough for now.
- Retroactive root-cause analysis of why the verify suite is growing past 600s (the env-file comment defers this to "a follow-up TB"). This TB is about the operator-surface gap, not the underlying suite-duration question.
