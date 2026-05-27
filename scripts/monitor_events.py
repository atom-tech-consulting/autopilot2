"""Stream operator-interesting events from `.cc-autopilot/events.jsonl`.

Tails the events file directly (no external pipe needed), filters to a
hard-coded allowlist of event types that operators care about during an
active arc, and emits one compact line per matching event. Designed as
the target of a `Monitor` tool watch so each kept line becomes one
notification.

Usage:

    # Default — tails .cc-autopilot/events.jsonl in the current cwd:
    python3 -u scripts/monitor_events.py

    # Explicit project path:
    python3 -u scripts/monitor_events.py /path/to/project

    # Explicit events.jsonl path (overrides project resolution):
    python3 -u scripts/monitor_events.py --events /path/to/events.jsonl

Output shape per line:

    HH:MM:SS | <event_type> | key=val key=val ... | summary=<truncated>

The allowlist (`KEEP`) covers ideation lifecycle, validation + queue,
task lifecycle, focus/attention/watchdog/daemon events. Other event
types (task_run_usage, control_run_usage, mattermost_reply, cron_*,
goal_updated, etc.) are filtered out — they'd be noise for tracking
active arcs. Edit `KEEP` to widen or narrow coverage.

Tailing strategy: spawns `tail -F <path>` as a subprocess and reads its
stdout. `-F` (capital) follows-by-name with retry, so the watch
survives daemon log rotation / file deletion / file creation.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

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


def _resolve_events_path(project: str | None, events: str | None) -> Path:
    if events:
        return Path(events).expanduser().resolve()
    base = Path(project).expanduser().resolve() if project else Path.cwd()
    return base / ".cc-autopilot" / "events.jsonl"


def _format_event(e: dict) -> str | None:
    t = e.get("type", "")
    if t not in KEEP:
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
    s = str(e.get("summary", ""))[:140]
    if s:
        bits.append(f"summary={s}")
    return " | ".join(bits)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "project",
        nargs="?",
        help="Project root containing .cc-autopilot/events.jsonl "
        "(default: current directory).",
    )
    p.add_argument(
        "--events",
        help="Explicit path to an events.jsonl file. Overrides project "
        "resolution.",
    )
    args = p.parse_args()

    path = _resolve_events_path(args.project, args.events)
    if not path.is_file():
        print(
            f"monitor_events: events file not found: {path}",
            file=sys.stderr,
        )
        return 1

    proc = subprocess.Popen(
        ["tail", "-F", "-n", "0", str(path)],
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
            out = _format_event(e)
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


if __name__ == "__main__":
    raise SystemExit(main())
