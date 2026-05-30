"""DEPRECATED shim → `ap2 logs --follow` (TB-352).

The canonical entrypoint is now **`ap2 logs --follow`** (project-aware
via the global `--project`, version-pinned with the daemon, and
test-covered). The operator-interest `KEEP` allowlist, the compact
`_format_event` one-line formatter, and the `tail -F` follow loop now
live in the package at `ap2/event_monitor.py` — edit THERE, not here.

This script is retained ONLY so an existing Claude Code `Monitor` watch
targeting `python3 -u scripts/monitor_events.py` keeps working
unchanged. It parses the historical `[project]` / `--events` argv and
delegates to `ap2.event_monitor.follow`. Repoint the watch at
`ap2 logs --follow` whenever convenient.

Usage (unchanged from the pre-fold script):

    # Default — tails .cc-autopilot/events.jsonl in the current cwd:
    python3 -u scripts/monitor_events.py

    # Explicit project path:
    python3 -u scripts/monitor_events.py /path/to/project

    # Explicit events.jsonl path (overrides project resolution):
    python3 -u scripts/monitor_events.py --events /path/to/events.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `ap2` importable when this script is run directly from a source
# checkout (sys.path[0] is scripts/, not the repo root) and ap2 isn't
# pip-installed. Harmless when `ap2` is already importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    p = argparse.ArgumentParser(
        description="DEPRECATED shim → `ap2 logs --follow`. Tails "
        ".cc-autopilot/events.jsonl, filters to the operator-interest "
        "allowlist, and prints one compact line per match; delegates to "
        "ap2.event_monitor.",
    )
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

    from ap2.event_monitor import _resolve_events_path, follow

    path = _resolve_events_path(args.project, args.events)
    return follow(path)


if __name__ == "__main__":
    raise SystemExit(main())
