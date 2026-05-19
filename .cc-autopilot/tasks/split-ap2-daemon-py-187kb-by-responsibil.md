# Split `ap2/daemon.py` (187KB) by responsibility: orchestrator stays, lift auto-unfreeze / auto-approve / state-commit / watchdog to siblings

Tags: #autopilot #refactor #modularity #agent-friendliness #regression-pin

## Goal

`ap2/daemon.py` is currently 187KB — the second-largest module after `tools.py`. It mixes the core orchestrator (`main_loop`, `_tick`, MM loop) with at least four distinct policy axes:

- `_maybe_auto_unfreeze` and the TB-225 BriefingFix sweep (allowlist, caps, dry-run plumbing).
- `_maybe_auto_approve` policy (token caps, freeze threshold, validator-judge integration) — note some of this already lives in `automation_status.py`.
- `_commit_state_files` and path-allowlist logic (TB-126 narrow-commit machinery).
- Watchdog (`auto_diagnose_fired` summary composition + idle-window detection).

The orchestrator's job is "drive the tick loop and dispatch agents." Each of these policy axes is independently testable and rarely co-modified with the orchestrator itself. Co-locating them costs context budget on every daemon-touching task and obscures the tick loop's actual shape.

Goal anchor: serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." Large `daemon.py` means tasks touching auto-unfreeze, auto-approve, or watchdog logic load the full orchestrator. Reducing per-module context load directly reduces TB-247-shape retry-exhaustion exposure on policy-axis tasks.

Why now: this is the second-largest module and the next biggest agent-friendliness lift after `tools.py`. The four policy axes named above are stable enough (each has shipped TBs against it) that lifting them out is mechanical rather than design work.

## Scope

- Keep `ap2/daemon.py` as the orchestrator: `main_loop`, `_tick`, MM loop, cron orchestration, retry dispatch, state-file I/O glue.
- Lift policy / sweep logic to focused sibling modules at the flat `ap2/` level. Suggested split (agent picks exact division):
  - `ap2/auto_unfreeze.py` — `_maybe_auto_unfreeze` + BriefingFix sweep + allowlist parsing + cap helpers.
  - `ap2/auto_approve.py` — `_maybe_auto_approve` + threshold/cap helpers. (Some of this overlaps `automation_status.py` — consolidate if the natural boundary lands there.)
  - `ap2/state_commit.py` — `_commit_state_files` + path-allowlist logic (TB-126).
  - `ap2/watchdog.py` — `auto_diagnose_fired` summary composition + idle-window logic.
- Preserve every existing public symbol via re-export from `ap2/daemon.py` OR full call-site update.
- Update import sites across `cli.py`, `web.py`, `verify.py`, tests as needed.

## Design

- Flat structure only — NO `ap2/daemon/` subpackage. Each lift becomes a sibling module at `ap2/`.
- Daemon stays the conductor — orchestrator logic does not move. Only the per-axis policy implementations lift out. The tick loop in `daemon._tick` still calls `auto_unfreeze.maybe_run(cfg, ...)`, `auto_approve.maybe_run(cfg, ...)`, etc.
- Pass state through `Config` / `_shared.py` helpers, not via cross-imports between the new sibling modules.
- Mechanical move only — symbol names, signatures, behaviors stay identical. The point is context-budget reduction, not redesign.
- Watchdog idle-window state currently lives in `auto_diagnose_state.json` (per the earlier grep); the new `watchdog.py` owns reading/writing that file.

## Verification

- `uv run pytest -q` — full project suite passes.
- `wc -c ap2/daemon.py | awk '$1 < 90000 { exit 0 } { exit 1 }'` — `daemon.py` reduced to under 90KB after the split.
- `ls ap2/auto_unfreeze.py ap2/watchdog.py 2>/dev/null | wc -l | awk '$1 >= 2 { exit 0 } { exit 1 }'` — at minimum two of the suggested sibling modules exist.
- `python3 -c "import ap2.daemon; from ap2.daemon import main_loop"` exits 0 — orchestrator surface intact.
- prose: `ap2/daemon.py`'s remaining contents focus on orchestration (main_loop, _tick, MM loop, dispatch); the four policy axes named in `## Scope` have moved to sibling modules. Each sibling has a clear single-responsibility module docstring.
- prose: every `auto_unfreeze_*` / `auto_approve_*` / `auto_diagnose_*` event still fires with identical payload — the `events.jsonl` schema is unchanged. Tests covering these events still pass without modification.
- prose: the BriefingFix sweep (TB-225) still parses, validates, applies, and fires `auto_unfreeze_applied` / `auto_unfreeze_skipped` events identically. `test_tb225_auto_unfreeze.py` and `test_tb233_auto_unfreeze_dry_run.py` pass without modification (or with mechanical import-path updates only).

## Out of scope

- Subpackage creation — flat-structure principle.
- Behavior changes — pure mechanical move; same events, same caps, same allowlists.
- Consolidation of `automation_status.py` policy code with the new `auto_approve.py` — if the natural seam lands there, do it; if not, leave for a follow-up.
- Refactoring `main_loop` or `_tick` structure — orchestrator stays as-is.
- Splitting `tools.py` / `cli.py` / `web.py` — separate TBs in this batch.
- Changing retry orchestration or state-violation rollback semantics — pure refactor only.
