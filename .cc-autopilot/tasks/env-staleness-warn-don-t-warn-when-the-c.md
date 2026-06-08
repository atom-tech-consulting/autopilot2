# Env-staleness WARN: don't warn when the changed knobs are hot-reloadable, and fix the mislabeled "daemon start" timestamp

Tags: #autopilot #bug #status #observability #env-reload #ux

## Goal

`ap2 status` emits a stale-env WARN — *"`.cc-autopilot/env` modified at X
(after daemon start at Y) — restart with `ap2 stop && ap2 start` to apply
changes"* — whenever the env file's mtime is newer than the mtime the daemon
captured at startup (`env_file_mtime_at_start` in `daemon_state.json`). Two
problems, both observed live 2026-06-08:

1. **It false-alarms on hot-reloadable knobs.** Editing
   `AP2_IDEATION_DISABLED` (which is in `env_reload.HOT_RELOADABLE_KNOBS`)
   triggered the "restart to apply" WARN — but that knob applies
   automatically on the next tick's `env_reload`; **no restart is needed**.
   Telling the operator to restart for a hot-reloadable change is wrong and,
   worse, a restart would needlessly kill any in-flight task. The WARN should
   only fire when a change genuinely requires a restart — i.e. when at least
   one changed knob is NOT hot-reloadable (a FIXED knob).

2. **The timestamp is mislabeled.** The message prints *"after daemon start
   at Y"*, but `Y` is `env_file_mtime_at_start` — the **env file's mtime
   captured when the daemon loaded it**, NOT the daemon's start time.
   Observed: the WARN said "daemon start at 2026-06-08T17:37:34Z" while the
   daemon had actually started at 20:06:25Z. The comparison it makes is valid
   ("env changed since the daemon loaded it"); only the wording conflates
   "env last loaded" with "daemon start."

Why now: this WARN just misled an operator into thinking a restart was
required to enable ideation when a hot-reload would (and did) suffice on the
next tick; a status warning that cries "restart" on hot-reloadable edits
trains operators to over-restart (and to kill running tasks). Meta-infra
observability fix, no focus anchor → `--skip-goal-alignment`.

## Scope

- **Suppress the WARN when every changed knob is hot-reloadable.** Determine
  the set of knobs that differ between the env file's current contents and
  what the daemon loaded at start, and classify each via
  `env_reload.HOT_RELOADABLE_KNOBS`. If ALL changed knobs are hot-reloadable,
  do NOT emit the restart WARN — they apply on the next tick. Optionally emit
  a low-key info note instead (e.g. "env changed; hot-reloadable knobs apply
  next tick").
- **Warn only when a FIXED (non-hot-reloadable) knob changed**, and name the
  offending knob(s) in the message so the operator knows exactly why a
  restart is needed. Treat an unrecognized/unknown changed key conservatively
  as fixed (warn).
- **Fix the wording**: stop labeling the timestamp "daemon start at" — phrase
  it as the env file having been modified since the daemon loaded it (e.g.
  "`.cc-autopilot/env` modified at X, after the daemon loaded it at Y"), so
  the displayed time isn't mistaken for the daemon's start time.
- **Knowing which knobs changed**: the daemon currently stashes only
  `env_file_mtime_at_start`. To classify changes it must know the *values*
  the daemon loaded — stash the loaded `AP2_*` knob set (values or a
  per-knob hash) in `daemon_state.json` at start so `ap2 status` can diff the
  current file against it. (If the sibling TB-379 effective-config snapshot
  lands first, reuse it as the source of the daemon's loaded values rather
  than adding a second stash.)
- **No change to `env_reload` behavior** — this task only fixes what the WARN
  *reports*, not how knobs reload.

## Design

- **Match the warning to reality.** A restart WARN should mean "a restart is
  actually required." Hot-reloadable edits don't require one, so warning about
  them is a false positive that erodes trust in the warning and provokes
  unnecessary restarts. Gating on `HOT_RELOADABLE_KNOBS` makes the WARN mean
  what it says.
- **Name the cause.** When a restart IS needed, naming the fixed knob(s) tells
  the operator what forced it (vs a bare "something changed").
- **Honest labels.** `env_file_mtime_at_start` is not the daemon start time;
  the message should describe the env-load event it actually compares against.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new WARN-classification tests.
- New test: when the env file differs from the daemon's loaded values ONLY in hot-reloadable knobs, `ap2 status` emits NO restart WARN (and the suite asserts the restart string is absent).
- New test: when a FIXED (non-hot-reloadable) knob differs, the WARN fires and names that knob.
- `grep -q "HOT_RELOADABLE_KNOBS" ap2/cli_daemon.py` — the status WARN path consults the hot-reloadable set to classify changes.
- `! grep -q "daemon start at" ap2/cli_daemon.py` — the misleading "daemon start at" label is gone from the WARN.
- `ap2/cli_daemon.py` Prose: the stale-env WARN fires only when at least one changed knob is not in `env_reload.HOT_RELOADABLE_KNOBS` (naming the fixed knob), is suppressed when all changed knobs are hot-reloadable, and its wording describes the env file being modified since the daemon loaded it rather than labeling the env-mtime as the daemon's start time. Judge confirms via Read.

## Out of scope

- The `env_reload` hot-reload mechanism itself, and the "existing env vars win" / shell-pinned-knob behavior (separate footgun — a file edit can't override a shell-exported knob; not this task).
- TB-379's separate fix making `ap2 status` render component/knob lines from the daemon's live effective config (sibling task; this task may reuse its snapshot as the source of loaded values if it lands first).
- Any new daemon control/IPC channel.
