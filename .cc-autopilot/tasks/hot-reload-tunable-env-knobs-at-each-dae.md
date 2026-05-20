# Hot-reload tunable env knobs at each daemon tick (re-source .cc-autopilot/env), removing the restart requirement TB-260 only warns about

Tags: #autopilot #daemon #config #env #operator-surface #regression-pin

## Goal

The daemon reads `.cc-autopilot/env` exactly once, at startup (`config.py:95` → `load_project_env`), and freezes the resulting `Config` for its lifetime. Env changes therefore require a full `ap2 stop && ap2 start` to take effect. TB-260 shipped a warning when the env file is newer than daemon-start but deliberately punted the actual reload ("Per-tick re-source of .cc-autopilot/env — different design, more invasive; punted to a future TB"). This is that follow-up.

Concrete cost — the restart friction has bitten three times in two days: (1) TB-255 ran against the old 600s verify ceiling for ~26h because `AP2_VERIFY_TIMEOUT_S` had been bumped to 1800s but the daemon hadn't restarted; (2 and 3) on 2026-05-19 the operator twice tried `ap2 stop && ap2 start` to activate a re-enabled `AP2_IDEATION_DISABLED` knob, and both were no-ops because the daemon won't die mid-task — so `ap2 start` saw the still-live pid and refused.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Manual-restart-to-apply-a-knob is exactly the operator-in-the-loop friction the walk-away promise rules out — a bumped timeout or re-enabled ideation should take effect on the next tick, not require the operator to babysit a restart.

Why now: the friction recurred three times in two days (TB-255 plus two failed restarts on 2026-05-19). TB-260 made the staleness visible; this removes it. With ideation now back on and operators tuning validator-judge / ideation / auto-approve knobs against live behavior, env edits will be frequent — each one currently re-opens the silent-stale window until a restart.

## Scope

- At the top of each daemon tick (the `_tick` entry in `daemon.py`, BEFORE operator-queue drain / MM / cron / ideation / task dispatch), re-source `.cc-autopilot/env` and refresh the tunable subset of `Config` so knob changes take effect without a restart.
- Handle the os.environ-precedence gotcha: `load_project_env` (`config.py:136`) skips any key already in `os.environ` ("existing env vars win"), so a naive re-call picks up NOTHING on a second invocation. The reload path must re-read the file and update the file-sourced values without clobbering genuine shell-export overrides. The `applied` dict that `load_project_env` already returns identifies which keys were file-sourced — refresh only those.
- Define the tunable set that hot-reloads vs the fixed set that still needs a restart:
  - Hot-reloadable (tunables): `AP2_TASK_TIMEOUT_S`, `AP2_CONTROL_TIMEOUT_S`, `AP2_VERIFY_TIMEOUT_S`, `AP2_VALIDATOR_JUDGE_TIMEOUT_S`, `AP2_TASK_MAX_TURNS`, `AP2_CONTROL_MAX_TURNS`, `AP2_IDEATION_MAX_TURNS`, `AP2_AGENT_MODEL`, `AP2_AGENT_EFFORT`, `AP2_MAX_RETRIES`, `AP2_IDEATION_DISABLED`, `AP2_IDEATION_TRIGGER_TASK_COUNT`, auto-approve / auto-unfreeze thresholds, `AP2_VERIFY_CMD`, tick intervals.
  - Fixed (still require restart — document why): `project_root` and file paths (identity), web binding (`AP2_WEB_PORT` / `AP2_WEB_DISABLED` — the web server is already bound; rebinding needs the web task to restart), `AP2_MM_CHANNELS` (MM loop subscription is set up at startup).
- Emit an `env_reloaded` event (with the changed keys) when a tick detects and applies an env-file change, so the operator sees hot-reloads in events.jsonl / web.
- Interaction with TB-260: the stale-warning becomes moot for hot-reloadable knobs but stays relevant for the fixed set. At minimum, TB-260's warning must NOT false-warn for a knob that just hot-reloaded.

## Design

- Re-source at tick-top, before any tick work, so all downstream reads in that tick see fresh values.
- mtime-gated: the reload reads the env file's current mtime and only re-parses + refreshes when mtime changed since the last reload — a cheap no-op on the common unchanged-file tick (avoids parsing the file every 30s).
- Refresh mechanism: rebuild the tunable `Config` fields from the freshly-parsed file values, preserving structural fields (project_root, paths) unchanged. Prefer constructing a new `Config` with structural fields copied + tunables re-read over in-place mutation of a frozen dataclass.
- os.environ precedence: when re-sourcing, overwrite `os.environ` ONLY for keys the original `load_project_env` reported as file-applied (its return dict). Keys set by a genuine shell export (never file-applied) keep shell precedence — preserving the documented "shell export wins" contract while letting file edits propagate.
- Knobs read directly from `os.environ` at use-time (e.g. `AP2_TASK_MAX_TURNS` at `daemon.py:208`, `AP2_AGENT_MODEL`, effort) hot-reload for free once `os.environ` is refreshed; the real work is the Config-dataclass-cached tunables (timeouts, `verify_cmd`, tick intervals).
- Lifecycle knobs (web binding, MM channels) are explicitly NOT refreshed; document the restart requirement for them where TB-260's warning lives.

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes.
- prose: the daemon tick path (`_tick` in `daemon.py`) re-sources `.cc-autopilot/env` at tick-top and refreshes the tunable `Config` fields before operator-queue drain / cron / ideation / task dispatch. The judge confirms by reading the tick entry and the new reload helper.
- prose: a regression-pin test sets a tunable knob (e.g. `AP2_VERIFY_TIMEOUT_S`) in the env file, simulates a tick, and asserts the new value is live without a from-scratch Config rebuild / restart — exercising the exact TB-255 failure shape. The judge confirms the test exists and asserts the new value is in effect.
- prose: the reload handles the os.environ-precedence gotcha — a regression test asserts that a file-sourced key is refreshed (its os.environ value updated) on reload while a key set only by a shell export is NOT clobbered. The judge confirms by reading the test.
- prose: lifecycle knobs (`AP2_WEB_PORT`, `AP2_MM_CHANNELS`) are documented as still requiring a restart and are NOT hot-reloaded — the tunable-vs-fixed split is explicit in code comments or a test.
- `grep -rnE 'env_reloaded|reload_env|hot.?reload' ap2/*.py | grep -v test_ | wc -l | awk '$1 > 0 { exit 0 } { exit 1 }'` — the reload implementation exists in non-test code.
- prose: an `env_reloaded` (or similarly named) event with the changed keys is emitted when a tick applies an env-file change, and is registered in `events.py`. The judge confirms the registration plus the emission site.

## Out of scope

- Hot-reloading lifecycle knobs that require re-binding / re-subscribing: `AP2_WEB_PORT`, `AP2_WEB_DISABLED` (web server binding), `AP2_MM_CHANNELS` (MM subscription). These still need a restart; document it.
- Changing structural `Config` (project_root, file paths) — identity, never reloads.
- Removing TB-260's stale-warning entirely — keep it for the fixed-knob set; only ensure it does not false-warn for hot-reloaded knobs.
- A file-watch / inotify mechanism — an mtime-check at tick-top is sufficient and simpler; no filesystem watchers.
- Reloading `goal.md` or other operator-owned files — scoped to `.cc-autopilot/env` only.
- Per-knob granular reload events beyond a single `env_reloaded` carrying the changed-keys list.
