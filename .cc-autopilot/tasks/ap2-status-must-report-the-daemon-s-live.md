# `ap2 status` must report the daemon's live effective config, not a locally re-resolved one

Tags: #autopilot #bug #status #observability #config #reliability

## Goal

`ap2 status` reports component on/off and knob state by **independently
re-resolving config in the CLI process** (this shell's env layered over
`.cc-autopilot/env`), NOT by reading what the running daemon actually
resolved. So status diverges from the daemon whenever the two processes'
environments differ.

Observed live 2026-06-08: the daemon (pid 20941) was launched from a shell
that exported `AP2_AUTO_APPROVE=1`. `env_reload`'s "existing env vars win"
rule means a later edit of `.cc-autopilot/env` to `AP2_AUTO_APPROVE=0` could
NOT override the shell-pinned value, so the daemon kept auto-approve **armed**.
But `ap2 status`, run from a different shell with no such export, re-resolved
the knob from the file and printed `auto_approve: off (AP2_AUTO_APPROVE=0)` —
flatly wrong about the daemon. The web UI (which runs *inside* the daemon and
reads its live `os.environ`) correctly showed it ON. An operator trusting
`ap2 status` would believe auto-approve was disabled while it was still live.

`ap2 status` is accurate for board / events / focus because those come from
shared **state files** (`TASKS.md`, `events.jsonl`) that are the single source
of truth. The component/knob display is the lone surface that re-resolves
process-local env instead of reading shared state — that is the bug. Fix it so
status reflects the daemon's actual effective config.

Why now: this footgun directly caused an operator (this session) to believe
auto-approve was off when it was armed; a status command that misreports the
daemon's safety-gate state is a reliability hazard. Meta-infra observability
fix with no focus anchor → `--skip-goal-alignment`.

## Scope

- **Daemon writes an effective-config snapshot** to a daemon-owned state
  file under `.cc-autopilot/` (e.g. `effective_config.json`), refreshed each
  tick (and/or on `env_reload`). It records what the daemon actually
  resolved: each component's enabled state, the relevant knob values
  (`AP2_AUTO_APPROVE`, dry-run, gate tags, etc.), plus the daemon `pid` and a
  write timestamp. This matches ap2's file-state-only model — the snapshot is
  shared state the same way the board is.
- **`ap2 status` reads that snapshot** for the component/knob lines instead
  of re-resolving env locally, so it shows the daemon's live state. The
  board/events/focus lines are unchanged (already file-backed).
- **Stale / absent snapshot fallback**: when the snapshot is missing or its
  `pid` is not a live daemon (daemon stopped), status falls back to local
  re-resolution and labels it clearly — e.g. `(daemon not running — showing
  local config)` — so the divergence can never silently mislead again.
- **No new IPC / socket / HTTP control channel** — respect the single-process,
  file-state-only constraint. The daemon already runs a read-only web UI;
  this is a state file, not a control API.
- **Surface a divergence hint (optional but preferred)**: when the daemon
  snapshot disagrees with what a local resolution would produce (i.e. a knob
  is shell-pinned in the daemon), status may note it, so the operator learns
  the file edit didn't take.

## Design

- **Extend the file-state-as-truth pattern to config.** ap2 already treats
  on-disk files as the single source of truth and `ap2 status` already reads
  the board/events from them. The daemon's resolved config is just one more
  piece of state it should publish; status reads it rather than guessing from
  its own environment. No IPC, consistent with "read files, resume."
- **Fail loud, not silent.** The original bug was a confident-but-wrong
  "off". The fallback label + optional divergence hint convert a silent
  misreport into an explicit "I'm not reading the daemon" signal.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new snapshot-read tests.
- New test: given an effective-config snapshot declaring `auto_approve` enabled with a live `pid`, `ap2 status` reports auto-approve ON **even when the local env/file resolves to OFF** — proving status reads the daemon snapshot, not local re-resolution (the exact divergence observed 2026-06-08).
- New test: with no snapshot, or a snapshot whose `pid` is not running, `ap2 status` falls back to local resolution and emits the "(daemon not running)" label.
- `grep -rqE "effective_config|effective.config" ap2/daemon.py ap2/cli*.py` — the daemon writes the snapshot and the status path references it.
- `ap2/daemon.py` + the status renderer Prose: the daemon writes a per-tick effective-config snapshot (component enabled-states + knob values + pid + timestamp) to a `.cc-autopilot/` state file, and `ap2 status` renders the component/knob lines from that snapshot when the daemon is live, falling back to a clearly-labelled local resolution when it is not. Judge confirms via Read.

## Out of scope

- Changing `env_reload`'s "existing env vars win" precedence, or making a `.cc-autopilot/env` edit override a shell-exported knob — that is a separate concern (a real footgun worth its own task/doc note); this task only makes `ap2 status` *report* the daemon's state truthfully.
- Any socket / HTTP / RPC control API for the daemon (file-state-only constraint).
- Reworking the web UI (it already reads live in-process state correctly).
