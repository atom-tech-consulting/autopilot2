# Add a --follow live event-monitor mode to ap2 logs (fold in scripts/monitor_events.py)

Tags: #autopilot #cli #logs #operator-ux #monitoring #refactor

## Goal

`scripts/monitor_events.py` is an operator aid that lives loose in
`scripts/`: it tails `.cc-autopilot/events.jsonl`, filters to a curated
allowlist of operator-interesting event types, and emits one compact
line per match (`HH:MM:SS | type | k=v ... | summary=...`). It's
designed as the target of a Claude Code `Monitor` tool watch so each
kept line becomes one notification during an active arc. Because it's a
loose script it isn't project-aware (no `--project` resolution), isn't
version-pinned with the daemon, isn't discoverable from `ap2 --help`,
and has no test coverage.

Fold it into the existing `ap2 logs` verb as a `--follow` / `-f` mode.
Today `ap2 logs` is a one-shot dump (`-n N`, `--json`); `--follow`
turns it into a live tail that, by default, filters to the
operator-interest allowlist and prints the compact one-line format.
Move the shared logic (the `KEEP` allowlist + the `_format_event`
formatter + the tail loop) into the `ap2` package so it's tested and
version-pinned, and reduce `scripts/monitor_events.py` to a thin shim
that delegates to the packaged implementation (so existing `Monitor`
watches pointing at the script path keep working unchanged).

Why now: the operator currently runs this monitor from a loose script
in their live Claude Code session and it's proven its worth across this
week's arc (it's how every task-lifecycle event reached the operator);
promoting it to a first-class, project-aware, test-covered `ap2 logs
--follow` removes the loose-script foot-gun (path drift, no
`--project`, no version pin) right as the codebase is being tightened
for the OSS cut. Operator-directed 2026-05-30; meta-infra CLI ergonomics
with no active focus, so `--skip-goal-alignment`.

## Scope

- **Locate the `logs` implementation** (the `logs` subparser is
  registered in `ap2/cli.py`; its handler reads the project's events
  log read-only — likely in `ap2/cli_daemon.py`). Add a
  `--follow` / `-f` store_true flag to the `logs` subparser.
- **Move the monitor core into the package.** Lift `KEEP`,
  `_format_event`, and the `tail -F`-based tail loop out of
  `scripts/monitor_events.py` into the package (e.g. a new
  `ap2/event_monitor.py`, or alongside the existing `logs` handler).
  This becomes the single source of truth for the allowlist + compact
  format.
- **Wire `--follow` behavior** on `ap2 logs`:
  - Default (`ap2 logs --follow`): live-tail the project's
    `events.jsonl`, filter to the `KEEP` allowlist, emit the compact
    `HH:MM:SS | type | k=v ... | summary=<trunc>` line per match.
    Resolve the events path from the global `--project` flag (replacing
    the script's ad-hoc `project` / `--events` args — `--project`
    already covers this).
  - `--all` (new, only meaningful with `--follow`): disable the
    allowlist so every event type streams (debug escape hatch).
  - `--json` + `--follow`: emit the raw JSON line per kept event
    instead of the compact format (compose with `--all` for an
    unfiltered raw stream).
  - One-shot `ap2 logs` (no `--follow`) behavior is UNCHANGED —
    `-n N` / `--json` keep their current contract. `--follow` ignores
    `-n` (it starts at end-of-file, like `tail -F -n 0`).
- **Reduce `scripts/monitor_events.py` to a shim.** Keep the file (the
  operator's live Monitor watch targets `python3 -u
  scripts/monitor_events.py`), but have it import + call the packaged
  entrypoint so there is no duplicated `KEEP`/format logic. Preserve its
  existing argv contract (`[project]`, `--events`) by mapping them onto
  the packaged function. A header comment should point at
  `ap2 logs --follow` as the canonical entrypoint.
- **Tests** (`ap2/tests/`): unit-test the pure pieces — `_format_event`
  filters to `KEEP` (a kept type formats, a non-kept type returns
  None), extracts the documented key=val fields, and truncates
  `summary` to the cap; events-path resolution honors `--project`. Add
  a CLI test that the `logs` parser accepts `--follow` / `--all` and
  that one-shot `logs` is unchanged. Do not attempt to unit-test the
  live `tail -F` subprocess loop — factor it so the format/filter layer
  is testable without spawning `tail`.
- **Help/docs drift:** update the `logs` help text to mention
  `--follow`; if `ap2/howto.md` or a docs-drift test enumerates `logs`'
  options or the monitor script, update those references to the folded
  form. (No new verb is added, so the CLI-verb set is unchanged.)

## Design

- **Fold, don't fork.** Reusing `logs` keeps the CLI surface small and
  avoids adding a verb (no CLI-verb docs-drift-gate change). `logs`
  already owns "show me events"; `--follow` is the live variant of the
  same concept.
- **Allowlist-by-default in follow mode.** The curated `KEEP` set is
  the whole point of the monitor (operators want lifecycle signal, not
  `task_run_usage` noise); `--all` is the explicit opt-out for
  debugging. One-shot `logs` keeps showing everything so its contract
  doesn't change.
- **`tail -F` retained.** Capital-`F` follows-by-name with retry, so the
  watch survives daemon log rotation / events-file recreation — keep
  that robustness rather than reimplementing a pure-Python tail.
- **Shim, not deletion.** Removing `scripts/monitor_events.py` would
  break the operator's currently-running Monitor watch; a delegating
  shim keeps that working while the package becomes the source of
  truth, and the operator can repoint the watch at `ap2 logs --follow`
  on their own schedule.

## Verification

- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new follow-mode format/filter tests.
- `ap2 logs --help 2>&1 | grep -qE "\-\-follow"` — the `--follow` flag is registered on the `logs` subcommand.
- `ap2 logs --help 2>&1 | grep -qE "\-\-all"` — the `--all` unfiltered escape hatch is registered.
- `ap2 --project . logs -n 1 2>&1 | grep -qE "."` — one-shot `logs` still renders (back-compat; global `--project` precedes the subcommand).
- `! grep -qE "^KEEP = \{" scripts/monitor_events.py` — the allowlist constant no longer lives in the loose script (it moved into the package; the script is now a shim).
- `grep -rqE "ideation_skipped" ap2/event_monitor.py ap2/cli_daemon.py ap2/cli.py` — the operator-interest allowlist now lives in the package. (Path may differ; the judge bullet below confirms the structural claim.)
- `ap2/` Prose: the `KEEP` allowlist + `_format_event` compact formatter + the `tail -F` follow loop now live in the `ap2` package and back `ap2 logs --follow`; follow mode filters to `KEEP` by default, `--all` disables the filter, `--json` emits raw kept lines, and one-shot `ap2 logs` is unchanged. Judge confirms via Read.
- `scripts/monitor_events.py` Prose: the script is now a thin shim that imports and calls the packaged entrypoint (no duplicated `KEEP` / `_format_event` definitions) while preserving its `[project]` / `--events` argv contract, so an existing `Monitor` watch on the script path keeps working. Judge confirms via Read.

## Out of scope

- Repointing the operator's live `Monitor` watch from the script to
  `ap2 logs --follow` — an operator action, done whenever convenient
  (the shim keeps the old path working until then).
- Changing the `KEEP` allowlist membership or the compact line format —
  this task relocates them verbatim, it doesn't re-curate them.
- Adding a one-shot `--filter`/allowlist option to non-follow `logs` —
  possible follow-up; not needed for the monitor use case.
