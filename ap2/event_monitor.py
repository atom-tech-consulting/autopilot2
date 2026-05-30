"""Live event-monitor core backing `ap2 logs --follow` (TB-352).

Folds in the former loose `scripts/monitor_events.py`: this module owns
the operator-interest `KEEP` allowlist, the compact `_format_event`
one-line formatter, and the `tail -F`-based `follow` loop. Promoting
them out of the loose script into the package makes the allowlist +
format **version-pinned with the daemon** and **unit-testable** (the
pure `_format_event` / `_resolve_events_path` pieces have no subprocess
dependency), and discoverable from `ap2 logs --help`.

`scripts/monitor_events.py` is now a thin shim that imports + calls
`follow` here, so an existing Claude Code `Monitor` watch targeting
`python3 -u scripts/monitor_events.py` keeps working unchanged while
this module is the single source of truth.

Compact line shape (one per kept event):

    HH:MM:SS | <event_type> | key=val key=val ... | summary=<truncated>

The `KEEP` allowlist covers ideation lifecycle, validation + queue,
task lifecycle, and focus/attention/watchdog/daemon events. Other types
(`task_run_usage`, `control_run_usage`, `mattermost_reply`, `cron_*`,
`goal_updated`, etc.) are filtered out by default — they'd be noise for
tracking an active arc. `ap2 logs --follow --all` disables the filter
(debug escape hatch); one-shot `ap2 logs` keeps showing everything.

Tailing strategy: `follow` spawns `tail -F <path>` as a subprocess and
reads its stdout. Capital `-F` follows-by-name with retry, so the watch
survives daemon log rotation / events-file deletion / recreation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Operator-interest allowlist — relocated verbatim from
# `scripts/monitor_events.py` (TB-352 relocates, it does not re-curate;
# changing membership is explicitly out of scope). Edit here to widen or
# narrow follow-mode coverage.
KEEP = {
    # Ideation lifecycle (entry, exit, proposals, scrub, skip)
    "ideation_empty_board",
    "ideation_complete",
    "ideation_cycle_summary",       # TB-300: agent's no-proposal exit marker
    "ideation_skipped",
    "ideation_proposal_recorded",
    "ideation_state_scrubbed",      # TB-284: scrub fired with diff
    "ideation_state_scrub_error",   # TB-294: scrub error / timeout audit
    # Validation + queue
    "validator_judge_passed",
    "validator_judge_failed",
    "operator_queue_append",
    "operator_queue_drained",
    # Task lifecycle (promotion → run → verify → complete/fail)
    "backlog_auto_promoted",
    "task_start",
    "task_complete",
    "task_failed",
    "verify_passed",
    "verify_failed",
    "verification_failed",
    "retry_exhausted",
    # Focus + attention + watchdog + daemon
    "focus_advanced",
    "attention_raised",
    "daemon_start",
    "daemon_stop",
    "auto_diagnose_fired",
}

# Cap applied to the rendered `summary=` field so a long summary doesn't
# blow out the one-line shape. Relocated verbatim from the loose script.
SUMMARY_CAP = 140


def _resolve_events_path(project: str | None, events: str | None) -> Path:
    """Resolve the events.jsonl path the way the loose script did, so the
    shim can preserve its `[project]` / `--events` argv contract.

    Precedence: an explicit `--events` path wins; otherwise resolve
    `<project>/.cc-autopilot/events.jsonl` (default project = cwd). In the
    packaged `ap2 logs --follow` path the events file comes straight from
    `cfg.events_file` (already resolved from the global `--project`), so
    this helper is only the shim's compatibility shim.
    """
    if events:
        return Path(events).expanduser().resolve()
    base = Path(project).expanduser().resolve() if project else Path.cwd()
    return base / ".cc-autopilot" / "events.jsonl"


def _format_event(e: dict, *, allow_all: bool = False) -> str | None:
    """Render one event dict as a compact one-line string, or `None` when
    the event type isn't in `KEEP` (the allowlist filter).

    `allow_all=True` disables the allowlist so every type formats — this
    backs `ap2 logs --follow --all`. The default (`allow_all=False`)
    preserves the loose script's filter-by-`KEEP` semantics.
    """
    t = e.get("type", "")
    if not allow_all and t not in KEEP:
        return None
    ts = e.get("ts", "")[11:19]
    bits = [ts, t]
    for k in (
        "task",
        "reason",
        "trigger",
        "status",
        "applied",
        "removed_chars",
        "from",
        "to",
        "op",
    ):
        v = e.get(k)
        if v not in (None, ""):
            bits.append(f"{k}={v}")
    s = str(e.get("summary", ""))[:SUMMARY_CAP]
    if s:
        bits.append(f"summary={s}")
    return " | ".join(bits)


def follow(
    events_path: Path,
    *,
    allow_all: bool = False,
    as_json: bool = False,
) -> int:
    """Live-tail `events_path` via `tail -F -n 0` and stream matching events.

    - Default: filter to the `KEEP` allowlist and print the compact
      `_format_event` one-liner per kept event.
    - `allow_all=True`: disable the allowlist (every type streams).
    - `as_json=True`: print the raw JSON line per kept event instead of
      the compact format (compose with `allow_all` for an unfiltered raw
      stream).

    Returns a process exit code (0 on clean EOF / Ctrl-C, 1 if the events
    file is missing). The `tail -F` subprocess loop is intentionally NOT
    unit-tested — the testable surface is `_format_event` /
    `_resolve_events_path` above.
    """
    if not events_path.is_file():
        print(
            f"ap2 logs --follow: events file not found: {events_path}",
            file=sys.stderr,
        )
        return 1

    proc = subprocess.Popen(
        ["tail", "-F", "-n", "0", str(events_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=1,  # line-buffered
        text=True,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not allow_all and e.get("type", "") not in KEEP:
                continue
            if as_json:
                print(line, flush=True)
                continue
            out = _format_event(e, allow_all=allow_all)
            if out is not None:
                print(out, flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0
