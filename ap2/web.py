"""Local read-only web UI for ap2 daemon state.

Closes TB-93 (the "console tool for human review" backlog item) in web
form. Pure stdlib (`http.server`), no JS framework, no auth. Bound to
127.0.0.1 by default; only the operator on the box should be reading it.

Read-only by design — every mutation still goes through the `ap2` CLI or
custom MCP tools. The web UI is a window onto state, not a control panel.

Pages:
  /                       overview: daemon status, board counts, last 30 events
  /events                 full event log, filterable by ?type=X&n=N (default 200)
  /tasks                  all tasks grouped by section
  /task/<TB-N>            one task: briefing + per-run links + related events
  /task-run/<run-id>      live SDK debug dumps for one run (TB-129)
  /task-run/<run-id>/stream.json
                          JSON sub-endpoint, ?since=N returns new stream rows
  /pipelines              in-flight + recent pipelines from pipeline_start events
  /insights               insights index — front matter summaries + links
  /insight/<name>         one insight file, full content
  /ideation_state         latest ideation_state.md assessment
  /commits                recent git log (subjects link to /task/TB-N when matched)
  /usage                  TB-181 token / cost dashboard (cost-over-time, model split, etc.)
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import html
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from . import diagnose, events as ev_mod, insights as ins_mod
from ap2._shared import read_pid
from .board import Board
from .config import Config


# TB-130: when the daemon spawns the web UI as part of `ap2 start`, this is
# the default port. Standalone `ap2 web` keeps its historical default
# (7820) so operators who already have a tab pointed at the legacy URL
# don't have to rebookmark. Override either with `AP2_WEB_PORT`.
DEFAULT_DAEMON_WEB_PORT = 8729
DEFAULT_STANDALONE_WEB_PORT = 7820

# TB-155: when the configured start_port is already bound (typically a stale
# daemon, an `ap2 web` standalone, or another project's daemon on the same
# box), `_bind_with_enumeration` walks forward up to this many ports before
# giving up. Bounded so a misconfigured port range can't degenerate into an
# unbounded probe that climbs into the ephemeral range. 10 is enough for the
# realistic conflict case (operator has 1-2 stale processes); beyond that the
# operator should investigate the conflict, not let the daemon paper over it.
DEFAULT_WEB_PORT_MAX_ATTEMPTS = 10


def is_web_disabled() -> bool:
    """True when the operator opted out of the daemon-spawned web UI.

    Centralized so the daemon, the CLI status command, and tests share one
    parsing rule. Accepts the same truthy strings as the rest of ap2's env
    knobs (`1`, `true`, `yes`, case-insensitive).
    """
    return os.environ.get("AP2_WEB_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def daemon_web_port() -> int:
    """Resolve the daemon-spawned web port from env, falling back to default.

    A malformed `AP2_WEB_PORT` (e.g. `"abc"`) falls back to the default
    rather than crashing the daemon at startup — the operator's typo
    shouldn't kill the whole loop.
    """
    raw = os.environ.get("AP2_WEB_PORT", "").strip()
    if not raw:
        return DEFAULT_DAEMON_WEB_PORT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DAEMON_WEB_PORT


# TB-129: terminal event types for a task run. Once one of these lands for the
# task associated with an in-flight run, the live detail page stops polling
# and renders the verdict inline.
_TERMINAL_RUN_EVENT_TYPES = frozenset({
    "task_complete",
    "task_error",
    "task_state_violation",
})

# `<YYYYMMDD>T<HHMMSS>Z-<task_id>` — the debug-dump filename prefix produced by
# `daemon._prep_debug_dumps`. Captured as (compact_ts, task_id) for matching
# back to the originating `task_start` event.
_RUN_ID_RE = re.compile(r"^(\d{8}T\d{6}Z)-(.+)$")


def _debug_dir(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "debug"


def _ts_to_compact(ts: str) -> str | None:
    """Convert event ISO ts (`2026-04-30T17:18:47Z`) to debug compact form.

    Returns None on malformed input so callers can degrade gracefully — the
    web UI must never throw on a single odd event row.
    """
    if not ts:
        return None
    try:
        # Strip the dashes/colons; tolerate fractional seconds defensively.
        core = ts.split(".", 1)[0]
        return core.replace("-", "").replace(":", "")
    except (AttributeError, ValueError):
        return None


def _list_run_ids_for_task(cfg: Config, task_id: str) -> list[str]:
    """All run-ids on disk for `task_id`, oldest first.

    Discovery via filename glob (not events.jsonl) so pruned events with
    surviving debug files still surface, and so we don't double-count when
    the daemon emits multiple `task_start`s for one set of files (retry
    inside the same dispatch).
    """
    d = _debug_dir(cfg)
    if not d.exists():
        return []
    out = []
    for p in d.glob(f"*-{task_id}.stream.jsonl"):
        run_id = p.name[: -len(".stream.jsonl")]
        m = _RUN_ID_RE.match(run_id)
        if m and m.group(2) == task_id:
            out.append(run_id)
    out.sort()
    return out


def _find_run_id_for_event(cfg: Config, ts: str, task_id: str) -> str | None:
    """Map a `task_start` event to its run-id (debug filename prefix).

    The daemon writes the `task_start` event a beat before
    `_prep_debug_dumps` allocates the debug filenames, so the two timestamps
    are usually equal but may differ by ~1s. Strategy: prefer exact compact-ts
    match; otherwise pick the closest run within a small forward window.
    Returns None when no `<run>.stream.jsonl` exists on disk (file pruned, or
    the daemon hadn't created it yet).
    """
    if not task_id:
        return None
    runs = _list_run_ids_for_task(cfg, task_id)
    if not runs:
        return None
    target = _ts_to_compact(ts)
    if target:
        exact = f"{target}-{task_id}"
        if exact in runs:
            return exact
    # Closest within ±60s: the daemon allocates debug files within ~1s of
    # `task_start` but skew tolerance keeps the match robust under clock
    # weirdness (sandbox, replay, etc.).
    try:
        e_dt = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (ValueError, TypeError):
        return runs[-1] if len(runs) == 1 else None
    best: tuple[float, str] | None = None
    for rid in runs:
        m = _RUN_ID_RE.match(rid)
        if not m:
            continue
        try:
            d_dt = _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=_dt.timezone.utc
            )
        except ValueError:
            continue
        delta = (d_dt - e_dt).total_seconds()
        if -2 <= delta <= 60:
            score = abs(delta)
            if best is None or score < best[0]:
                best = (score, rid)
    return best[1] if best else None


def _terminal_event_for_run(
    cfg: Config, run_ts_compact: str, task_id: str
) -> dict | None:
    """First terminal event for `task_id` at-or-after the run's start ts.

    Returns the event dict (with ts + status/commit/etc.) or None if the run
    is still in-flight. We bound the search to events tailing the run start
    so a previous attempt's terminal event doesn't get attributed to this
    run.
    """
    if not task_id:
        return None
    try:
        run_dt = _dt.datetime.strptime(run_ts_compact, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None
    # Pull a generous tail; the live view typically polls within minutes so
    # we don't need the full log.
    for e in ev_mod.tail(cfg.events_file, n=5000):
        if e.get("task") != task_id:
            continue
        typ = e.get("type")
        if typ not in _TERMINAL_RUN_EVENT_TYPES:
            continue
        try:
            e_dt = _dt.datetime.strptime(
                e.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        # `>= run_dt - 2s` to tolerate the same skew window as
        # `_find_run_id_for_event`.
        if (e_dt - run_dt).total_seconds() >= -2:
            return e
    return None


def _read_jsonl(path: Path, *, since: int = 0) -> list[dict]:
    """Read a JSONL file, returning rows with `seq >= since`.

    Tolerant of partial/malformed trailing lines — the daemon appends rows
    while we read them. A half-written final line is silently dropped; the
    next poll picks it up once the writer flushes.
    """
    if not path.exists():
        return []
    out = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(obj.get("seq", -1)) >= since:
                    out.append(obj)
    except OSError:
        return []
    return out


def _is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ------------- HTML helpers -------------

_CSS = """<style>
  * { box-sizing: border-box }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 1rem 1.5rem; max-width: 1400px;
    color: #222; line-height: 1.45;
  }
  h1, h2, h3 { margin: 0.6rem 0 0.4rem }
  h1 { font-size: 22px } h2 { font-size: 18px } h3 { font-size: 15px }
  a { color: #06c; text-decoration: none } a:hover { text-decoration: underline }
  nav { padding: 0.6rem 0; border-bottom: 1px solid #eee; margin-bottom: 1rem }
  nav a { margin-right: 1rem; font-weight: 500 }
  .stats { display: flex; gap: 1.5rem; padding: 0.5rem 0 1rem; flex-wrap: wrap }
  .stat { padding: 0.3rem 0.7rem; background: #f7f7f7; border-radius: 4px; min-width: 80px }
  .stat-label { color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em }
  .stat-value { font-size: 20px; font-weight: 500; font-family: ui-monospace, monospace }
  table { border-collapse: collapse; width: 100%; font-size: 13px }
  td, th { padding: 0.3rem 0.5rem; border-bottom: 1px solid #eee; vertical-align: top; text-align: left }
  th { background: #fafafa; color: #666; font-weight: 500; font-size: 11px; text-transform: uppercase }
  tr.failure { background: #fff7f7 }
  tr.failure td.type { color: #c33 }
  tr.warning { background: #fffbea }
  tr.warning td.type { color: #b87000 }
  tr.lifecycle td.type { color: #2a8 }
  /* TB-148: `frozen` is a deeper-red tint for task_complete rows whose
     status is `retry_exhausted` — the task was abandoned permanently and
     moved to Frozen. Distinct from plain `failure` so the operator can
     tell "tried and gave up" apart from "tried and rolled back / hit
     verification". `neutral` is gray for `task_complete` with unknown
     or missing status — defensive bucket so an unexpected status string
     doesn't quietly inherit the green of `complete`. */
  tr.frozen { background: #f8e0e0 }
  tr.frozen td.type { color: #811; font-weight: 600 }
  tr.neutral { background: #f5f5f5 }
  tr.neutral td.type { color: #888 }
  /* TB-148: legend swatches on the /events page — same palette as the
     row tints so the legend genuinely teaches the row colors. */
  .legend-swatch { display: inline-block; padding: 0.1rem 0.4rem;
                   border-radius: 3px; margin-right: 0.3rem;
                   font-size: 11px; font-family: ui-monospace, monospace }
  .legend-swatch.lifecycle { background: #e0f5e0; color: #2a8 }
  .legend-swatch.warning { background: #fffbea; color: #b87000 }
  .legend-swatch.failure { background: #fff7f7; color: #c33 }
  .legend-swatch.frozen { background: #f8e0e0; color: #811 }
  .legend-swatch.neutral { background: #f5f5f5; color: #888 }
  .ts { color: #888; font-family: ui-monospace, monospace; font-size: 12px; white-space: nowrap }
  .type { font-family: ui-monospace, monospace; font-weight: 500 }
  /* `table-layout: fixed` is what actually makes `<pre>` inside a `<td>`
     wrap. With auto layout the column expands to fit the JSON's longest
     line and `overflow-wrap` never triggers. Fixed layout caps each
     column at its declared/derived width and forces inner content to
     wrap. Combined with the wrap rules below, no row pushes the page
     wider than its container. */
  table { table-layout: fixed }
  td, th { overflow-wrap: anywhere; word-break: break-word }
  .ts { white-space: nowrap; width: 12em }
  .type { width: 14em }
  .summary { color: #444 }
  /* `pre-wrap` preserves newlines (JSON indentation, briefing layout)
     but lets long lines wrap at whitespace; `overflow-wrap: anywhere`
     on the `pre` itself breaks rare unbroken strings (long URL, base64)
     at any character so nothing escapes the cell. */
  pre { background: #f5f5f5; padding: 0.6rem; border-radius: 4px;
        white-space: pre-wrap; overflow-wrap: anywhere;
        font-size: 12px; line-height: 1.4; font-family: ui-monospace, monospace }
  details summary { cursor: pointer; color: #06c; font-size: 12px; user-select: none }
  details[open] summary { margin-bottom: 0.3rem }
  .filter { padding: 0.5rem 0; font-size: 12px }
  .filter a { margin-right: 0.5rem; padding: 0.1rem 0.4rem; border-radius: 3px;
              background: #f0f0f0; color: #555 }
  .filter a.on { background: #06c; color: #fff }
  .meta { color: #888; font-size: 12px }
  .running { color: #2a8; font-weight: 500 } .stopped { color: #c33; font-weight: 500 }
  .paused { color: #c80; font-weight: 500 }
  ul.tasks { list-style: none; padding: 0; margin: 0 }
  ul.tasks li { padding: 0.2rem 0; border-bottom: 1px solid #f5f5f5 }
  .id { font-family: ui-monospace, monospace; color: #06c; font-weight: 500 }
  .tag { background: #eef; color: #338; padding: 0 0.3rem; border-radius: 3px;
         font-size: 11px; font-family: ui-monospace, monospace; margin-left: 0.3rem }
  /* TB-121: amber pill for ideation proposals waiting on `ap2 approve`. */
  .tag.pending-review { background: #fff1d6; color: #8a5a00 }
  /* TB-129: live task-run detail page row tints */
  tr.row-assistant td.type { color: #06c }
  tr.row-tool { background: #f3f8ff } tr.row-tool td.type { color: #048 }
  tr.row-tool-result { background: #f7fff3 } tr.row-tool-result td.type { color: #060 }
  tr.row-tool-result.is-error { background: #fff7f7 } tr.row-tool-result.is-error td.type { color: #c33 }
  tr.row-result { background: #fffaf0 } tr.row-result td.type { color: #b87000; font-weight: 600 }
  tr.row-result.is-success { background: #f0fff0 } tr.row-result.is-success td.type { color: #060 }
  tr.row-system td.type { color: #888 }
  .verdict { padding: 0.6rem 0.8rem; border-radius: 4px; margin: 0.5rem 0;
             font-size: 13px; line-height: 1.5 }
  .verdict.success { background: #f0fff0; border-left: 4px solid #2a8 }
  .verdict.failure { background: #fff7f7; border-left: 4px solid #c33 }
  .verdict.unknown { background: #fffbea; border-left: 4px solid #b87000 }
  .live-banner { padding: 0.4rem 0.8rem; background: #f7f7f7; border-radius: 4px;
                 font-size: 12px; margin: 0.5rem 0; color: #555 }
  .live-banner.in-flight { background: #f0f8ff; color: #048 }
  .live-banner .pulse { display: inline-block; width: 8px; height: 8px;
                        border-radius: 50%; background: #06c; margin-right: 6px;
                        animation: pulse 1.5s ease-in-out infinite }
  @keyframes pulse { 0%, 100% { opacity: 0.3 } 50% { opacity: 1 } }
  .run-link { font-size: 11px; margin-left: 0.4rem;
              padding: 0 0.3rem; background: #eef; border-radius: 3px;
              text-decoration: none }
  .run-link:hover { background: #cce }
  .run-status { font-size: 11px; padding: 0 0.3rem; border-radius: 3px;
                font-family: ui-monospace, monospace; margin-left: 0.4rem }
  .run-status.success { background: #e0f5e0; color: #060 }
  .run-status.failure { background: #fde0e0; color: #c33 }
  .run-status.in-flight { background: #e0f0ff; color: #048 }
  /* TB-158: verification-failed summary block + per-row failed-bullet
     sub-list. The block sits at the top of `/task-run/<run-id>` when the
     latest terminal verdict is `verification_failed`; the sub-list lives
     inline in the `/events` and `/` row's `summary` cell so the failing
     bullet headlines are visible without expanding the raw json `<details>`. */
  .verif-summary { padding: 0.6rem 0.8rem; border-radius: 4px;
                   margin: 0.5rem 0; background: #fffbea;
                   border-left: 4px solid #b87000; font-size: 13px;
                   line-height: 1.5 }
  .verif-summary .counter { font-weight: 600; color: #8a5a00 }
  .verif-summary ul.failed-bullets { list-style: none; padding: 0;
                                     margin: 0.4rem 0 0 0 }
  .verif-summary ul.failed-bullets li { padding: 0.2rem 0;
                                        border-bottom: none }
  .verif-summary .fail-mark { color: #c33; font-weight: 600;
                              font-family: ui-monospace, monospace;
                              margin-right: 0.25rem }
  .verif-summary .bullet-kind { color: #888; font-family: ui-monospace, monospace;
                                font-size: 11px; margin-right: 0.3rem }
  .verif-summary .judge-note { color: #666; font-size: 12px;
                               display: block; margin: 0.1rem 0 0 1.4rem }
  ul.failed-bullets-inline { list-style: none; margin: 0.2rem 0 0 0;
                             padding: 0 }
  ul.failed-bullets-inline li { padding: 0.1rem 0; border-bottom: none;
                                font-size: 12px; color: #555 }
  ul.failed-bullets-inline li .fail-mark { color: #c33; font-weight: 600;
                                           font-family: ui-monospace, monospace;
                                           margin-right: 0.25rem }
  /* TB-162: yellow-tinted card for the pending-operator-queue preamble on
     `/`. Sits above the events table when at least one queued op hasn't
     drained yet; omitted entirely when the queue is empty so the UI never
     shows perpetual `0 pending` noise. The amber palette mirrors the
     existing `.verif-summary` (warning-tier, not failure) since pending
     ops are an "about to happen" signal, not an error. */
  .pending-queue { padding: 0.6rem 0.8rem; border-radius: 4px;
                   margin: 0.5rem 0; background: #fffbea;
                   border-left: 4px solid #b87000; font-size: 13px;
                   line-height: 1.5 }
  .pending-queue .pq-header { font-weight: 600; color: #8a5a00;
                              margin-bottom: 0.3rem }
  .pending-queue ul.pq-entries { list-style: none; padding: 0; margin: 0 }
  .pending-queue ul.pq-entries li { padding: 0.15rem 0;
                                    border-bottom: none;
                                    font-family: ui-monospace, monospace;
                                    font-size: 12px; color: #444 }
  .pending-queue .pq-op { display: inline-block; padding: 0 0.35rem;
                          background: #f5e0b3; color: #6b4400;
                          border-radius: 3px; margin-right: 0.4rem;
                          font-weight: 600 }
  .pending-queue .pq-task { font-weight: 600; color: #06c;
                            margin-right: 0.4rem }
  .pending-queue .pq-meta { color: #888; margin-right: 0.4rem }
  .pending-queue .pq-extra { color: #444 }
  .pending-queue details summary { color: #8a5a00; font-size: 11px }
  /* TB-173 / TB-191: blue-tinted card for the ideator's
     `## Decisions needed from operator` section (renamed from the
     pre-TB-191 "Open questions for operator"). Sits above
     `.pending-queue` on the home page when `parse_operator_decisions`
     returns >0 entries; omitted entirely when the list is empty (no
     perpetual `0 decisions needed` noise). The blue palette is distinct
     from `.pending-queue`'s amber so the operator can tell pending
     operator ops (about to drain) from ideator-surfaced operator
     decisions (need human judgement) at a glance. CSS class name
     `.operator-decisions` matches the parser + schema name; the legacy
     `.open-questions` aliases (oq-header / ul.oq-entries) survive
     unrenamed-internally as `.od-header` / `ul.od-entries` for the
     same reason. */
  .operator-decisions { padding: 0.6rem 0.8rem; border-radius: 4px;
                        margin: 0.5rem 0; background: #eef5ff;
                        border-left: 4px solid #3a6db5; font-size: 13px;
                        line-height: 1.5 }
  .operator-decisions .od-header { font-weight: 600; color: #234e85;
                                   margin-bottom: 0.3rem }
  .operator-decisions ul.od-entries { list-style: disc inside; padding: 0;
                                      margin: 0 }
  .operator-decisions ul.od-entries li { padding: 0.15rem 0;
                                         font-size: 12px; color: #333 }
  /* TB-197: at-a-glance ideation gate-state card on `/`. Always rendered
     (when the daemon's cron-state file exists) so the operator can answer
     "when does ideation next fire?" without grepping `cron_state.json` by
     hand. Five state variants, each with a distinct tint:
       eligible        — green:  about to fire on next tick
       cooldown        — neutral grey: throttled, will fire when timer elapses
       active_running  — yellow: blocked by Active task in flight
       queued_full     — yellow: blocked by Ready+Backlog ≥ threshold
       disabled        — grey:   AP2_IDEATION_DISABLED env opt-out
     Compact 1-2 line shape so always-rendering doesn't crowd the page. */
  .ideation-status { padding: 0.5rem 0.8rem; border-radius: 4px;
                     margin: 0.5rem 0; font-size: 13px; line-height: 1.5;
                     background: #f7f7f7; border-left: 4px solid #888 }
  .ideation-status.is-eligible { background: #f0fff0;
                                 border-left-color: #2a8 }
  .ideation-status.is-cooldown { background: #f5f5f5;
                                 border-left-color: #888 }
  .ideation-status.is-blocked  { background: #fffbea;
                                 border-left-color: #b87000 }
  .ideation-status.is-disabled { background: #f0f0f0;
                                 border-left-color: #aaa; color: #555 }
  .ideation-status .is-header { font-weight: 600; margin-right: 0.4rem }
  .ideation-status.is-eligible .is-header { color: #1a6f2a }
  .ideation-status.is-cooldown .is-header { color: #444 }
  .ideation-status.is-blocked  .is-header { color: #8a5a00 }
  .ideation-status.is-disabled .is-header { color: #666 }
  .ideation-status .is-body { color: #333 }
  .ideation-status .is-meta { color: #888; font-size: 12px;
                              font-family: ui-monospace, monospace;
                              margin-left: 0.4rem }
  /* TB-181: /usage token-cost dashboard.
     Card-style layout — each section sits in a `.usage-card` container
     with a thin border so the chart / table content stays prominent.
     `.usage-summary` reuses the blue-tinted palette from `.operator-decisions`
     to mark "this is the at-a-glance summary" the operator should read
     first. The stat tiles inside reuse the `.stat` shape already used on
     the board overview so the visual vocabulary stays tight. */
  .usage-card { padding: 0.6rem 0.8rem; margin: 0.6rem 0;
                background: #fcfcfc; border: 1px solid #eee;
                border-radius: 4px; line-height: 1.5 }
  .usage-summary { background: #eef5ff; border-left: 4px solid #3a6db5 }
  .usage-chips { font-size: 12px; padding: 0.2rem 0 0.4rem 0; color: #555 }
  .usage-chips a { margin: 0 0.15rem; padding: 0.1rem 0.4rem;
                   border-radius: 3px; background: #f0f0f0; color: #555 }
  .usage-chips a.on { background: #06c; color: #fff }
  .usage-stats { display: flex; gap: 1rem; padding: 0.4rem 0;
                 flex-wrap: wrap }
  .usage-stat { padding: 0.4rem 0.7rem; background: #fff;
                border: 1px solid #eee; border-radius: 4px;
                min-width: 140px }
  .usage-stat-label { color: #888; font-size: 11px;
                      text-transform: uppercase; letter-spacing: 0.05em }
  .usage-stat-value { font-size: 18px; font-weight: 500;
                      font-family: ui-monospace, monospace;
                      margin-top: 0.2rem }
  .usage-stat-small { font-size: 13px; font-weight: 400 }
  .usage-stat-prior { color: #888; font-size: 11px; margin-top: 0.1rem;
                      font-family: ui-monospace, monospace }
  .usage-breakdown, .usage-top-tasks { font-size: 13px;
                                       table-layout: auto }
  .usage-breakdown td, .usage-breakdown th,
  .usage-top-tasks td, .usage-top-tasks th { padding: 0.3rem 0.5rem }
  .usage-sub-table { margin-top: 0.4rem; background: #f5f8fc;
                     border: 1px solid #e0e6ee; border-radius: 3px;
                     table-layout: auto; width: auto }
  .usage-sub-table th { background: #eaf0f8; font-size: 11px }
  .usage-sub-label { font-family: ui-monospace, monospace; color: #555 }
  svg.cost-chart, svg.cache-chart, svg.model-split {
    display: block; max-width: 100%; height: auto;
  }
  /* TB-227: auto-approve / auto-unfreeze loop state card on `/`.
     Mirrors the `.ideation-status` palette (TB-197) so the home page's
     two automation-cards (ideation gate-state + auto-approve state)
     read as one visual cluster. Two tint variants:
       healthy — green:  knob on, no halt, 24h counters non-zero
       paused  — red:    one of the four halt conditions tripped;
                         border highlights the urgency
     Omitted entirely (server-side, not CSS-hidden) when the knob is
     off AND all 24h counters are zero — pre-opt-in projects don't
     see a perpetual `auto-approve: off` card. */
  .automation-status { padding: 0.5rem 0.8rem; border-radius: 4px;
                       margin: 0.5rem 0; font-size: 13px; line-height: 1.5;
                       background: #f7f7f7; border-left: 4px solid #888 }
  .automation-status.is-healthy { background: #f0fff0;
                                  border-left-color: #2a8 }
  .automation-status.is-paused  { background: #fff7f7;
                                  border-left-color: #c33 }
  .automation-status .as-header { font-weight: 600; margin-right: 0.4rem }
  .automation-status.is-healthy .as-header { color: #1a6f2a }
  .automation-status.is-paused  .as-header { color: #8a1818 }
  .automation-status .as-body { color: #333 }
  .automation-status .as-meta { color: #888; font-size: 12px;
                                font-family: ui-monospace, monospace;
                                margin-left: 0.4rem; display: block;
                                margin-top: 0.2rem }
  .automation-status .as-sparkline { display: inline-block;
                                     margin-left: 0.4rem;
                                     vertical-align: middle }
  .automation-status a { color: inherit; text-decoration: underline;
                         text-decoration-color: rgba(0,0,0,0.2) }
</style>"""


def _layout(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)} — ap2</title>"
        f"{_CSS}"
        "</head><body>"
        '<nav><a href="/">overview</a> '
        '<a href="/events">events</a> '
        '<a href="/tasks">tasks</a> '
        '<a href="/pipelines">pipelines</a> '
        '<a href="/insights">insights</a> '
        '<a href="/ideation_state">ideation_state</a> '
        '<a href="/commits">commits</a> '
        '<a href="/usage">usage</a></nav>'
        f"{body}"
        "</body></html>"
    )


# Web-UI-only "warning" tier — events that aren't failures (the task still
# landed in Complete, the daemon is healthy) but the operator should notice.
# Deliberately NOT added to diagnose.FAILURE_EVENT_TYPES: that set drives the
# watchdog's "is the daemon broken?" Mattermost report, where these would be
# false positives.
_WARNING_EVENT_TYPES = frozenset({
    "verification_partial",
})


# TB-148: per-status row class for `task_complete`. The event type alone
# would tint every task_complete row green (lifecycle), hiding the most
# operationally relevant signal — did the task actually pass, fail
# verification, or roll back? Reuses the existing `failure` / `warning`
# classes so the operator's color-memory ("orange = soft warning,
# red = hard fail") carries over from the failure-mode events; adds
# `frozen` (dark red, retry_exhausted) and `neutral` (gray, unknown)
# for the cases that don't map cleanly onto the existing palette.
_TASK_COMPLETE_STATUS_CLASS: dict[str, str] = {
    "complete": "lifecycle",            # green — the happy path
    "pipeline_pending": "lifecycle",    # parked in Pipeline Pending; not a failure
    "verification_failed": "warning",   # orange — committed but didn't verify
    "state_violation": "failure",       # red — rolled back, no useful artifacts
    "timeout": "failure",               # red — defensive (lives as task_timeout today)
    "error": "failure",                 # red — defensive (lives as task_error today)
    "incomplete": "failure",            # red — agent reported partial progress
    "blocked": "failure",               # red — agent hit a blocker
    "failed": "failure",                # red — agent reported outright failure
    "retry_exhausted": "frozen",        # dark red — task abandoned permanently
}


def _row_class(e: dict) -> str:
    """Row CSS class for one event row.

    For most event types the class is type-driven: failure-class events
    (FAILURE_EVENT_TYPES) get red, warning-class events get orange,
    lifecycle events get green, everything else is uncolored. For
    `task_complete` we additionally read `status` so a `complete` row
    differs visually from `verification_failed` / `state_violation` /
    `retry_exhausted` (TB-148). The status-aware tinting reuses the
    existing failure/warning classes where possible to keep the palette
    tight; `frozen` (dark red) and `neutral` (gray) cover the cases that
    don't fit either bucket.
    """
    typ = e.get("type", "")
    if typ == "task_complete":
        status = str(e.get("status") or "").strip().lower()
        return _TASK_COMPLETE_STATUS_CLASS.get(status, "neutral")
    if typ in diagnose.FAILURE_EVENT_TYPES:
        return "failure"
    if typ in _WARNING_EVENT_TYPES:
        return "warning"
    if typ in {"task_start", "cron_start", "cron_complete",
               "ideation_empty_board", "ideation_complete", "daemon_start",
               "daemon_stop", "backlog_auto_promoted"}:
        return "lifecycle"
    return ""


def _event_extra(e: dict) -> str:
    """One-line summary of an event's interesting fields (full text — no truncation)."""
    keys = [k for k in e.keys() if k not in ("ts", "type")]
    parts = []
    for k in keys:
        v = e[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        s = str(v)
        # collapse newlines for the one-line summary; details/json view shows full body
        s = s.replace("\n", " ⏎ ")
        parts.append(f'<span class="meta">{html.escape(k)}=</span>{html.escape(s)}')
    return " ".join(parts)


def _event_token_summary(e: dict) -> str:
    """TB-157: compact token / cost summary cell for one event row.

    Used when the events table is rendered with `?show=tokens` set, and
    inside the row's details for `judge_call` events. Returns "" when
    the event has no usage data.
    """
    u = e.get("usage")
    cost = e.get("total_cost_usd")
    if not isinstance(u, dict) and not isinstance(cost, (int, float)):
        return ""
    bits: list[str] = []
    if isinstance(u, dict):
        inp = int(u.get("input_tokens", 0) or 0)
        outp = int(u.get("output_tokens", 0) or 0)
        cc = int(u.get("cache_creation_input_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        denom = cr + cc + inp
        hit = (cr / denom * 100.0) if denom else 0.0
        bits.append(f"in={inp:,}")
        bits.append(f"out={outp:,}")
        bits.append(f"cc={cc:,}")
        bits.append(f"cr={cr:,}")
        bits.append(f"hit={hit:.1f}%")
    if isinstance(cost, (int, float)):
        bits.append(f"${float(cost):.4f}")
    return " · ".join(bits)


# TB-179 / TB-180: compact one-line rendering for the three usage-carrying
# event types — `judge_call`, `task_run_usage`, `control_run_usage` —
# whose verbose `usage` blob (and `model_usage`, `server_tool_use`, etc.
# nested dicts) drowns the events page when dumped inline via
# `_event_extra`. The shape is: `<identity> · <token-tuple> · <duration>`
# — six numeric fields (in / out / cc / cr / total_cost / duration) plus
# an event-type-specific identity prefix so the operator sees "what
# cost what" without expanding the row.
#
# Verbose nested fields (server_tool_use, model_usage, iterations,
# service_tier, inference_geo, the nested `cache_creation` object, etc.)
# drop from the inline cell entirely; they're still in the row's
# `<details>raw json</details>` toggle (no data loss).
#
# TB-180 re-homed the actual formatting to `ap2.events.summarize_usage_event`
# so `ap2/cli.py::cmd_logs` can render the same compact line for these
# three event types — identical 6-field tuple + identity prefix means an
# operator scanning `ap2 logs` and `/events` sees the same shape and
# muscle-memory scanning works across both surfaces.
_COMPACT_USAGE_EVENT_TYPES = ev_mod._COMPACT_USAGE_EVENT_TYPES


def _compact_usage_row(e: dict) -> str:
    """Inline `<td class="summary">` HTML for the three usage-carrying
    event types (TB-179 / TB-180). Returns "" if `e` is not one of the
    three or has no `usage`/`total_cost_usd`/`duration_s` to summarize.

    The actual compact-string composition lives in
    `events.summarize_usage_event` so the CLI (`ap2 logs`) and the web
    events table render the same 6-field tuple + identity prefix. This
    wrapper just HTML-escapes the result for safe inline embedding in
    the events table cell.
    """
    summary = ev_mod.summarize_usage_event(e)
    if not summary:
        return ""
    return html.escape(summary)


# TB-158: shared `verification_failed` rendering. The per-row inline
# summary lives on the events table; the larger block sits at the top of
# `/task-run/<run-id>` when the latest verdict was a verification fail.
# Both consume the same `events.summarize_verification_failed` helper so
# the formatting is in lockstep with `ap2 logs`.

def _verification_failed_row_summary(e: dict) -> str:
    """Inline `<td class="summary">` content for a `verification_failed` row.

    Renders as:
        5/8 passed · 2 failed · 1 unverified
        ✗ Manual: kick a long-running task on stoch...
        ✗ [shell] grep -qE "..." ap2/web.py — no match

    Failing bullets only. Passing / unverified are summarized into the
    counter to keep the row terse — the full criteria array is one click
    away under the row's `<details>raw json</details>` footer.
    """
    summary = ev_mod.summarize_verification_failed(
        e, max_bullet=240, max_note=400,
    )
    task = str(e.get("task") or "").strip()
    counter = (
        f'{summary["pass_count"]}/{summary["total"]} passed'
        f' · {summary["fail_count"]} failed'
        f' · {summary["unverified_count"]} unverified'
    )
    head = (
        f'<span class="meta">task=</span>{html.escape(task)} · '
        f'<strong>{html.escape(counter)}</strong>'
    )
    failed = summary["failed_bullets"]
    if not failed:
        return head
    items = []
    for fb in failed:
        kind = fb.get("kind") or ""
        bullet = fb.get("bullet") or ""
        kind_html = (
            f'<span class="bullet-kind">[{html.escape(kind)}]</span>'
            if kind else ""
        )
        items.append(
            f'<li><span class="fail-mark">✗</span>{kind_html}'
            f'{html.escape(bullet)}</li>'
        )
    return (
        f"{head}"
        f'<ul class="failed-bullets-inline">'
        + "".join(items)
        + "</ul>"
    )


def _verification_summary_block(e: dict) -> str:
    """Block-level summary for the top of `/task-run/<run-id>` when the
    latest terminal verdict for the task is `verification_failed`.

    Mirrors the row summary but renders bullet + note (truncated longer
    than the row) so an operator arriving from a `task_complete` link
    sees WHY the task failed without scrolling through the SDK stream.
    """
    summary = ev_mod.summarize_verification_failed(
        e, max_bullet=400, max_note=600,
    )
    counter = (
        f'{summary["pass_count"]}/{summary["total"]} passed'
        f', {summary["fail_count"]} failed'
        f', {summary["unverified_count"]} unverified'
    )
    items = []
    for fb in summary["failed_bullets"]:
        kind = fb.get("kind") or ""
        bullet = fb.get("bullet") or ""
        notes = fb.get("notes") or ""
        kind_html = (
            f'<span class="bullet-kind">[{html.escape(kind)}]</span>'
            if kind else ""
        )
        note_html = (
            f'<span class="judge-note">↳ {html.escape(notes)}</span>'
            if notes else ""
        )
        items.append(
            f'<li><span class="fail-mark">✗</span>{kind_html}'
            f'{html.escape(bullet)}{note_html}</li>'
        )
    bullets_html = (
        f'<ul class="failed-bullets">{"".join(items)}</ul>' if items else ""
    )
    return (
        '<div class="verif-summary">'
        f'<span class="counter">Verification: {html.escape(counter)}</span>'
        f"{bullets_html}"
        "</div>"
    )


def _is_verification_fail_terminal(terminal: dict | None) -> bool:
    """True iff the terminal event for a run represents a verification
    failure — either the literal `verification_failed` event or the
    `task_complete` row whose status is `verification_failed`.
    Both shapes land in `events.jsonl` for the same underlying outcome
    (the daemon emits the structured `verification_failed` event AND
    the lifecycle `task_complete` summary), so the verdict block must
    fire on either.
    """
    if not terminal:
        return False
    typ = terminal.get("type")
    if typ == "verification_failed":
        return True
    if typ == "task_complete":
        status = str(terminal.get("status") or "").strip().lower()
        return status == "verification_failed"
    return False


def _latest_verification_failed_for_task(
    cfg: Config, task_id: str, *, run_ts_compact: str | None = None,
) -> dict | None:
    """Find the most recent `verification_failed` event for `task_id`.

    When `run_ts_compact` is supplied, restricts the search to events
    landing at-or-after that run's start (mirroring `_terminal_event_for_run`)
    so a previous attempt's failure isn't attributed to a later run.
    Returns the event dict or `None`.
    """
    if not task_id:
        return None
    cutoff_dt: _dt.datetime | None = None
    if run_ts_compact:
        try:
            cutoff_dt = _dt.datetime.strptime(
                run_ts_compact, "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            cutoff_dt = None
    found: dict | None = None
    for e in ev_mod.tail(cfg.events_file, n=5000):
        if e.get("task") != task_id:
            continue
        if e.get("type") != "verification_failed":
            continue
        if cutoff_dt is not None:
            try:
                e_dt = _dt.datetime.strptime(
                    e.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                continue
            if (e_dt - cutoff_dt).total_seconds() < -2:
                continue
        # `tail` returns oldest-first within the slice, so the last match
        # is the most recent.
        found = e
    return found


def _events_table(
    evts: list[dict],
    *,
    cfg: Config | None = None,
    show_tokens: bool = False,
) -> str:
    """Render an events table; pass `cfg` to enable per-row debug-run links.

    With `cfg`, each `task_start` row gets a small `→ live` link to its
    `/task-run/<run-id>` view if the debug files survive on disk (TB-129).
    Without, the table renders plain — used by callers that already render
    a header pulled from the same dataset.

    TB-157: when `show_tokens=True`, an extra `tokens` column surfaces
    `usage` + `total_cost_usd` per row when present (mostly `judge_call`
    rows today). Opt-in to keep the default rendering uncluttered.
    """
    if not evts:
        return "<p><em>no events</em></p>"
    rows = []
    for i, e in enumerate(evts):
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        cls = _row_class(e)
        full_json = json.dumps(e, indent=2, default=str)
        extra = _event_extra(e)
        run_link = ""
        if cfg is not None and typ == "task_start":
            rid = _find_run_id_for_event(cfg, ts, str(e.get("task") or ""))
            if rid:
                run_link = (
                    f' <a class="run-link" href="/task-run/{html.escape(rid)}" '
                    f'title="live SDK debug stream">→ live</a>'
                )
        # TB-157: surface judge_call usage on the row even when
        # ?show=tokens isn't set — `judge_call` events are tiny and
        # operators looking at the events page for one always want
        # the cost. The opt-in flag adds the column for ALL rows.
        token_cell = ""
        if show_tokens:
            token_cell = (
                f'<td class="tokens">'
                f'{html.escape(_event_token_summary(e))}</td>'
            )
        # TB-158: replace the generic field dump with a pass/fail counter
        # and an inline list of failing-bullet headlines for
        # `verification_failed` rows. Passing / unverified bullets are
        # collapsed into the counter only — operators wanting the raw
        # criteria array still get it via the `<details>raw json</details>`
        # footer below (unchanged).
        if typ == "verification_failed":
            extra = _verification_failed_row_summary(e)
        # TB-179: compact rendering for the three event types whose
        # verbose `usage` / `model_usage` blobs otherwise wrap the row
        # several lines and drown the at-a-glance signal. The compact
        # form keeps identity + 6 numeric fields inline; the full
        # payload still lives in the `<details>raw json</details>`
        # footer below (no data loss).
        elif typ in _COMPACT_USAGE_EVENT_TYPES:
            compact = _compact_usage_row(e)
            if compact:
                extra = compact
        rows.append(
            f'<tr class="{cls}">'
            f'<td class="ts">{html.escape(ts)}</td>'
            f'<td class="type">{html.escape(typ)}{run_link}</td>'
            f'<td class="summary">{extra}'
            f'<details><summary>raw json</summary>'
            f'<pre>{html.escape(full_json)}</pre></details></td>'
            f"{token_cell}"
            f"</tr>"
        )
    head = "<tr><th>ts</th><th>type</th><th>fields</th>"
    if show_tokens:
        head += "<th>tokens</th>"
    head += "</tr>"
    return (
        "<table><thead>"
        + head +
        "</thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _is_pending_review(t) -> bool:
    """True iff `review` is AMONG `t`'s structural blockers.

    Covers both the typical pure-`@blocked:review` case AND
    mixed-blocker cases where the operator's approval still needs to
    be captured even if other dependencies remain.

    TB-121: ideation-proposed tasks land with `@blocked:review` to
    gate on operator approval. The pill rendered next to the task on
    `/tasks` (and the `review:` line on `ap2 status`, and the cron
    status-report's snapshot block) keys off this — surfacing the
    "wants my approval" signal is orthogonal to dispatchability.

    TB-187: previously this used `all(...)`, which hid mixed-blocker
    tasks (e.g. `@blocked:review,TB-5`) from the operator entirely.
    Dispatch semantics are unchanged — `_is_dispatchable` still
    requires every blocker satisfied; approving a mixed-blocker task
    strips just the `review` token via `_approve_review_token` and
    leaves any other blockers to gate auto-promotion naturally.
    """
    blocked_on = getattr(t, "blocked_on", []) or []
    if not blocked_on:
        return False
    return any(b.lower() == "review" for b in blocked_on)


def _tasks_list(
    board: Board,
    section: str,
    *,
    limit: int | None = None,
    only_pending_review: bool = False,
) -> str:
    tasks = list(board.iter_tasks(section=section))
    if only_pending_review:
        tasks = [t for t in tasks if _is_pending_review(t)]
    if limit is not None:
        tasks = tasks[-limit:]
    if not tasks:
        return "<p><em>(empty)</em></p>"
    items = []
    for t in tasks:
        tags = "".join(f'<span class="tag">{html.escape(tg)}</span>' for tg in t.tags)
        desc = f' — <span class="meta">{html.escape(t.description)}</span>' if t.description else ""
        # TB-121: surface the review-gate pill so operators triaging
        # the board can spot ideation proposals without reading the
        # codespan column. Same lightweight `<span class="tag">` shape
        # tags use, just a distinct CSS hook for styling.
        pill = (
            ' <span class="tag pending-review">pending review</span>'
            if _is_pending_review(t)
            else ""
        )
        items.append(
            f'<li><a class="id" href="/task/{html.escape(t.id)}">{html.escape(t.id)}</a> '
            f"<strong>{html.escape(t.title)}</strong>{tags}{pill}{desc}</li>"
        )
    return f'<ul class="tasks">{"".join(items)}</ul>'


# ------------- TB-162: pending operator-queue preamble -------------


def _load_pending_queue_entries(cfg: Config) -> list[dict]:
    """Read `.cc-autopilot/operator_queue.jsonl` and return undrained ops.

    The daemon's drain handler (`tools._compact_operator_queue`) rewrites
    the queue file at end-of-drain to drop fully-applied uuids, so in
    steady state any line on disk is genuinely pending. But there's a
    brief window (between an op landing in `operator_queue_state.json`'s
    applied-set and the compaction running) where an applied uuid can
    still appear in the queue file. Filtering against the applied-set —
    the same shape `tools.operator_queue_pending_count` uses — keeps the
    web view honest in that window.

    Tolerates a missing queue file (return `[]`), a missing/corrupt
    state file (treat applied-set as empty), and individual malformed
    JSON lines (skip them — same defensive parse the events table uses).
    """
    queue_path = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    state_path = cfg.project_root / ".cc-autopilot" / "operator_queue_state.json"
    if not queue_path.exists():
        return []
    applied: set[str] = set()
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            if isinstance(data, dict):
                items = data.get("applied")
                if isinstance(items, list):
                    applied = {str(x) for x in items}
        except (OSError, json.JSONDecodeError):
            applied = set()
    out: list[dict] = []
    try:
        text = queue_path.read_text()
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("uuid") in applied:
            continue
        out.append(rec)
    return out


def _format_pending_queue_extra(op: str, args: dict) -> str:
    """Per-op compact summary of the most operationally relevant arg field.

    Only renders the one bit that the operator actually scans for at-a-
    glance — full payloads belong under the `<details>raw json</details>`
    footer. Returns ready-to-emit HTML — values are escaped here so the
    `title="..."` / `fields=...` wrapper text remains literal in the
    rendered page (rather than getting wrapped-and-escaped at the call
    site, which would mangle the `=` and `"` punctuation).

      add_backlog / add_ready / add_frozen → `title="..."` (≤80 chars)
      update                               → `fields=<csv>`
      ideate                               → `force=<bool>` (TB-159)
      approve / unfreeze / delete /        → "" (task_id is the load-
        move_to_backlog / reject              bearing signal already)
    """
    if op in ("add_backlog", "add_ready", "add_frozen"):
        title = str(args.get("title") or "")
        if not title:
            return ""
        if len(title) > 80:
            title = title[:79] + "…"
        return f'title="{html.escape(title)}"'
    if op == "update":
        fields = args.get("fields") or []
        if isinstance(fields, list) and fields:
            return (
                "fields="
                + html.escape(",".join(str(f) for f in fields))
            )
        return ""
    if op == "ideate":
        # TB-159 hasn't landed yet (no `ideate` op in OPERATOR_QUEUE_OPS
        # at TB-162's time of writing) but the queue handler is generic
        # — if/when an `ideate` op arrives carrying a `force` flag, we
        # render it without needing a follow-up edit here.
        if "force" in args:
            return f"force={bool(args.get('force'))}"
        return ""
    return ""


# Compact `HH:MM:SSZ` from the queue record's full ISO ts. The full
# date is implied (queue typically drains within a tick, ~30s) so the
# hour/minute/second is the part that distinguishes entries.
_QUEUE_TS_RE = re.compile(r"T(\d{2}:\d{2}:\d{2}Z)")


def _format_pending_queue_ts(ts: str) -> str:
    if not ts:
        return ""
    m = _QUEUE_TS_RE.search(str(ts))
    return m.group(1) if m else str(ts)


def _render_operator_decisions(cfg: Config) -> str:
    """Render the ideator's `## Decisions needed from operator` card (TB-173 / TB-191).

    Reads `.cc-autopilot/ideation_state.md` via
    `parse_operator_decisions` and renders each bullet as one `<li>` so
    the operator can scan the list visually. Returns the empty string
    when the helper returns ``[]`` (file or section missing, or
    section empty) so the home renderer can omit the card entirely —
    server-side omission, not CSS-hidden — and fresh projects don't
    see a perpetual "0 decisions needed" card.

    Mirrors the omit-on-empty + plural-aware-header shape of
    `_render_pending_queue`; the visual palette differs (blue, not
    amber) so the operator can tell the two cards apart at a glance.

    TB-191: the agent-internal `## Cycle observations` section is
    structurally excluded by the parser's heading-match regex — even
    if a future schema rewrite repositions the two sections adjacent,
    `parse_operator_decisions` only ever returns bullets from under
    the `## Decisions needed from operator` heading.
    """
    from .ideation import parse_operator_decisions

    entries = parse_operator_decisions(
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if not entries:
        return ""
    rows = "".join(
        f"<li>{html.escape(entry)}</li>" for entry in entries
    )
    plural = "" if len(entries) == 1 else "s"
    return (
        '<div class="operator-decisions">'
        f'<div class="od-header">'
        f'{len(entries)} decision{plural} needed from operator '
        f'(from <a href="/ideation_state">ideation_state.md</a>)'
        f'</div>'
        f'<ul class="od-entries">{rows}</ul>'
        '</div>'
    )


def _render_pending_queue(cfg: Config) -> str:
    """Render the pending-operator-queue card for the `/` index page.

    Returns the empty string when the queue has no undrained ops so the
    home renderer can omit the card entirely (server-side omission, not
    CSS-hidden — fewer bytes, no flicker). When at least one op is
    pending, renders an amber `.pending-queue` card listing each entry
    with op kind, task_id, ts, uuid prefix, and the per-op-kind summary
    from `_format_pending_queue_extra`.
    """
    entries = _load_pending_queue_entries(cfg)
    if not entries:
        return ""
    rows: list[str] = []
    for rec in entries:
        op = str(rec.get("op") or "?")
        args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
        task_id = str(args.get("task_id") or "TB-N/A")
        ts = _format_pending_queue_ts(str(rec.get("ts") or ""))
        # 8-char uuid prefix matches the typical git-short-sha display
        # (briefing scope item) and avoids horizontal overflow on narrow
        # viewports — the full uuid lives one click away in the raw json.
        uuid_str = str(rec.get("uuid") or "")
        uuid_prefix = uuid_str[:8] if uuid_str else ""
        extra = _format_pending_queue_extra(op, args)
        full_json = json.dumps(rec, indent=2, default=str)
        ts_html = (
            f'<span class="pq-meta">ts={html.escape(ts)}</span>' if ts else ""
        )
        uuid_html = (
            f'<span class="pq-meta">uuid={html.escape(uuid_prefix)}</span>'
            if uuid_prefix else ""
        )
        # `extra` is already HTML (escaped values inside literal label
        # text — see `_format_pending_queue_extra`). Don't double-escape.
        extra_html = (
            f'<span class="pq-extra">{extra}</span>' if extra else ""
        )
        rows.append(
            f"<li>"
            f'<span class="pq-op">[{html.escape(op)}]</span>'
            f'<span class="pq-task">{html.escape(task_id)}</span>'
            f'{ts_html}{uuid_html}{extra_html}'
            f'<details><summary>raw json</summary>'
            f'<pre>{html.escape(full_json)}</pre></details>'
            f"</li>"
        )
    plural = "" if len(entries) == 1 else "s"
    return (
        '<div class="pending-queue">'
        f'<div class="pq-header">'
        f'{len(entries)} operator op{plural} pending — '
        f'awaiting daemon drain (next tick)</div>'
        f'<ul class="pq-entries">{"".join(rows)}</ul>'
        '</div>'
    )


# ------------- TB-197: ideation gate-state card on `/` -------------


def _format_cooldown_remaining(seconds: int) -> str:
    """Render `seconds` as a compact human-readable duration.

    Used in the cooldown card so the operator can compute "is this soon?"
    without doing the math themselves. Examples (rounded up to the nearest
    minute, since 30s tick granularity makes sub-minute precision noise):
      seconds=12     → "<1m"
      seconds=120    → "2m"
      seconds=1680   → "28m"
      seconds=3660   → "1h 1m"
      seconds=7200   → "2h"
    """
    if seconds <= 0:
        return "0m"
    if seconds < 60:
        return "<1m"
    minutes = (seconds + 59) // 60  # round up — operator-friendly upper bound
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _ideation_gate_state(cfg: Config) -> dict:
    """Resolve the current ideation gate state for the web overview.

    Mirrors the gate-check sequence in `ideation._maybe_ideate` so the
    rendered card matches the daemon's actual decision logic. The check
    order (post-TB-186) is:

      1. `AP2_IDEATION_DISABLED` env opt-out
      2. Active task in flight (hard gate — sharing the SDK slot is unsafe)
      3. Cooldown — `AP2_IDEATION_COOLDOWN_S` since the last fire
      4. Per-cycle proposal-slot budget — Ready+Backlog ≥ threshold

    Returns a dict whose `gate_status` field is the FIRST blocking gate
    in that order (or `"eligible"` when none of them block). Reporting the
    deepest blocker would be misleading — a blocked gate earlier in the
    chain prevents later checks from being evaluated.

    Reads `_cooldown_s` and `_trigger_task_count` from `ap2.ideation` so
    the env-knob parsing rules stay in lockstep with the daemon. (If those
    helpers ever drift, the comment above and the test fixture both need
    updating in tandem.)

    Tolerates missing/corrupt `cron_state.json` (treats `last_fire` as
    None → "never fired") so the card renders cleanly on a fresh project.
    """
    # Lazy import — `ap2.ideation` pulls in board / events / cron, all of
    # which are already in `ap2.web`'s import graph; the lazy form just
    # mirrors `_render_operator_decisions`'s style and avoids any future
    # cycle if `ap2.ideation` ever picks up a `web` reference.
    from . import ideation as ideation_mod
    from .cron import load_state

    disabled = os.environ.get(
        "AP2_IDEATION_DISABLED", ""
    ).strip().lower() in ("1", "true", "yes")
    threshold = ideation_mod._trigger_task_count()
    cooldown_s = ideation_mod._cooldown_s()

    board = Board.load(cfg.tasks_file)
    active_count = sum(1 for _ in board.iter_tasks(section="Active"))
    queued_count = sum(
        sum(1 for _ in board.iter_tasks(section=s))
        for s in ("Ready", "Backlog")
    )

    state = load_state(cfg.cron_state_file)
    last_fire_unix = state.get(ideation_mod.IDEATION_NAME)
    last_fire_ts: str | None = None
    next_eligible_ts: str | None = None
    seconds_until_eligible = 0
    if last_fire_unix:
        try:
            last_dt = _dt.datetime.fromtimestamp(
                float(last_fire_unix), _dt.timezone.utc
            )
            last_fire_ts = last_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            next_dt = last_dt + _dt.timedelta(seconds=cooldown_s)
            next_eligible_ts = next_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            now_dt = _dt.datetime.now(_dt.timezone.utc)
            delta = (next_dt - now_dt).total_seconds()
            seconds_until_eligible = max(0, int(delta))
        except (ValueError, TypeError, OverflowError, OSError):
            # Corrupt timestamp in the cron state file — treat as never-fired.
            last_fire_ts = None
            next_eligible_ts = None
            seconds_until_eligible = 0

    # Gate-priority order matches the daemon's `_maybe_ideate` flow
    # post-TB-186: disabled → active → cooldown → threshold. Reporting
    # any deeper gate when an earlier one blocks would be misleading
    # (the daemon never reaches the deeper check).
    if disabled:
        gate_status = "disabled"
    elif active_count > 0:
        gate_status = "active_running"
    elif seconds_until_eligible > 0:
        gate_status = "cooldown"
    elif queued_count >= threshold:
        gate_status = "queued_full"
    else:
        gate_status = "eligible"

    return {
        "disabled": disabled,
        "threshold": threshold,
        "cooldown_s": cooldown_s,
        "active_count": active_count,
        "queued_count": queued_count,
        "last_fire_ts": last_fire_ts,
        "next_eligible_ts": next_eligible_ts,
        "seconds_until_eligible": seconds_until_eligible,
        "gate_status": gate_status,
    }


def _render_ideation_status_block(cfg: Config) -> str:
    """Render the ideation gate-state card for the `/` index page.

    Always returns a non-empty string (in contrast to `_render_pending_queue`
    and `_render_operator_decisions` which omit on empty) — the card is
    1-2 lines, and "eligible" / "cooldown" are not noise but the operator's
    answer to "when does ideation next fire?" without grepping
    `cron_state.json` by hand.

    Five state variants map to five tints + headlines:
      eligible        — green   — "will fire on next tick (≤30s)"
      cooldown        — neutral — shows absolute next-eligible ts AND
                                  relative remaining duration so the
                                  operator can answer "is this soon?"
                                  at a glance without doing math.
      active_running  — yellow  — "blocked: Active task in flight"
      queued_full     — yellow  — names the threshold + actual count
                                  (e.g. "5 ≥ threshold 5") so the env
                                  knob is sanity-checkable inline.
      disabled        — grey    — names the env knob verbatim so the
                                  operator can grep their env file.
    """
    state = _ideation_gate_state(cfg)
    status = state["gate_status"]
    if status == "disabled":
        return (
            '<div class="ideation-status is-disabled">'
            '<span class="is-header">Ideation</span>'
            '<span class="is-body">'
            'disabled (<code>AP2_IDEATION_DISABLED</code> set in env)'
            '</span>'
            '</div>'
        )
    if status == "active_running":
        return (
            '<div class="ideation-status is-blocked">'
            '<span class="is-header">Ideation</span>'
            '<span class="is-body">'
            f'blocked: Active task in flight ({state["active_count"]} active)'
            '</span>'
            '</div>'
        )
    if status == "queued_full":
        return (
            '<div class="ideation-status is-blocked">'
            '<span class="is-header">Ideation</span>'
            '<span class="is-body">'
            f'blocked: Ready+Backlog = {state["queued_count"]} '
            f'≥ threshold {state["threshold"]} '
            '(<code>AP2_IDEATION_TRIGGER_TASK_COUNT</code>)'
            '</span>'
            '</div>'
        )
    if status == "cooldown":
        remaining = _format_cooldown_remaining(state["seconds_until_eligible"])
        next_ts = state["next_eligible_ts"] or ""
        return (
            '<div class="ideation-status is-cooldown">'
            '<span class="is-header">Ideation</span>'
            '<span class="is-body">'
            f'cooldown {html.escape(remaining)} remaining '
            f'(next eligible {html.escape(next_ts)})'
            '</span>'
            '</div>'
        )
    # status == "eligible"
    return (
        '<div class="ideation-status is-eligible">'
        '<span class="is-header">Ideation</span>'
        '<span class="is-body">'
        'eligible — will fire on next tick (≤30s)'
        '</span>'
        '</div>'
    )


# ------------- TB-227: auto-approve / auto-unfreeze state card on / -------------


def _hourly_sparkline_buckets(
    cfg: Config, event_type: str, *, now: _dt.datetime | None = None,
) -> list[int]:
    """24 hourly counts for `event_type` ending at `now` (default UTC
    now). Bucket index 0 = oldest hour (23h ago), 23 = newest hour
    (most recent).

    Pure events.jsonl tail-scan; no I/O beyond the 2000-event tail
    (matches the aggregator's read window). Events outside the 24h
    window or with malformed `ts` are dropped silently.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    now_s = now.timestamp()
    buckets = [0] * 24
    if not cfg.events_file.exists():
        return buckets
    tail = ev_mod.tail(cfg.events_file, 2000)
    for e in tail:
        if e.get("type") != event_type:
            continue
        ts = e.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            ts_s = _dt.datetime.fromisoformat(s).timestamp()
        except (ValueError, TypeError):
            continue
        delta_h = (now_s - ts_s) / 3600.0
        if delta_h < 0 or delta_h >= 24:
            continue
        # Oldest hour (23h ago) → bucket 0; newest hour (0h ago) → bucket 23.
        idx = 23 - int(delta_h)
        if 0 <= idx < 24:
            buckets[idx] += 1
    return buckets


def _render_sparkline_svg(
    buckets: list[int], *, color: str, width: int = 80, height: int = 16,
) -> str:
    """Hand-rolled D3-free `<polyline>` sparkline. Returns the empty
    string when every bucket is zero so the home page doesn't get a
    flat line of noise — matches the omit-on-empty rendering shape of
    `_render_operator_decisions` and friends.

    The SVG coordinate system has y=0 at the top, so `peak - v` flips
    the visual so larger counts sit higher. `peak or 1` guards the
    divide when every bucket is zero (already short-circuited above
    but defensively kept for the assertion-style read).
    """
    if not any(buckets):
        return ""
    peak = max(buckets) or 1
    n = len(buckets)
    if n < 2:
        return ""
    step = width / (n - 1)
    pts = " ".join(
        f"{i * step:.1f},{(peak - v) / peak * height:.1f}"
        for i, v in enumerate(buckets)
    )
    return (
        f'<svg class="as-sparkline" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
        f'points="{pts}"/>'
        '</svg>'
    )


def _render_automation_card(cfg: Config) -> str:
    """Render the auto-approve / auto-unfreeze state card for the home
    page (TB-227).

    Always rendered when `AP2_AUTO_APPROVE` is truthy OR any 24h counter
    is non-zero; omitted entirely on fresh / pre-opt-in projects so the
    UI never grows a perpetual "auto-approve: off, 0 / 0 / 0" line.

    Two tint variants:
      - `is-paused`  — red border, urgent — at least one halt
                       condition (TB-223 freeze / TB-224 cap / task_error)
                       is in effect since its respective ack.
      - `is-healthy` — green border — knob on, no halt; counters
                       summarized + sparkline.

    Counter rows link to `/events?type=auto_approved` etc. so an
    operator can drill into individual events without leaving the home
    page.
    """
    from . import automation_status

    state = automation_status.collect_auto_approve_state(cfg)

    enabled = state["auto_approve_enabled"]
    # TB-241: dry-run 24h activity is part of the render-block decision
    # — an operator who flipped `AP2_AUTO_APPROVE_DRY_RUN` /
    # `AP2_AUTO_UNFREEZE_DRY_RUN` against an otherwise quiet board
    # should see the readiness signal here (the dry-run on-ramp's
    # whole purpose is to observe the loop's decisions on-demand
    # without flipping live dispatch). Pre-TB-241 the bucket counted
    # only real-mode activity, so dry-run-only state fell through and
    # the card stayed omitted after the knob flip.
    counters_total = (
        state["auto_approved_count_24h"]
        + state["auto_unfreeze_applied_count_24h"]
        + state["auto_unfreeze_skipped_count_24h"]
        + state["would_auto_approve_count_24h"]
        + state["would_auto_unfreeze_count_24h"]
    )
    if not enabled and counters_total == 0:
        return ""

    # TB-241: `[dry-run]` badge next to the header when either dry-run
    # knob is on. One badge regardless of which knob (or both) is on
    # — the on-axis rows below differentiate which side is in
    # monitor mode. Omitted entirely when both knobs are off so the
    # default-on header stays byte-identical to TB-227.
    aa_dry_run = state["dry_run_enabled"]
    au_dry_run = state["auto_unfreeze_dry_run_enabled"]
    dry_run_badge = (
        ' <span class="as-dry-run-badge">[dry-run]</span>'
        if (aa_dry_run or au_dry_run)
        else ''
    )

    if state["auto_approve_paused"]:
        klass = "automation-status is-paused"
        header = "Auto-approve — PAUSED"
        reason = state["pause_reason"] or "unknown"
        body = (
            f'reason={html.escape(str(reason))}; '
            f'{state["consecutive_freezes"]} consecutive freezes / '
            f'threshold {state["freeze_threshold"]} — '
            '<code>ap2 ack auto_approve_window_resume</code>'
        )
    else:
        klass = "automation-status is-healthy"
        header = "Auto-approve"
        body = "enabled — circuit healthy"

    approved_buckets = _hourly_sparkline_buckets(cfg, "auto_approved")
    unfrozen_buckets = _hourly_sparkline_buckets(cfg, "auto_unfreeze_applied")
    approved_spark = _render_sparkline_svg(approved_buckets, color="#1a6f2a")
    unfrozen_spark = _render_sparkline_svg(unfrozen_buckets, color="#3a6db5")

    window_cap = state["window_token_cap"]
    window_used = state["window_tokens_used"]
    if window_cap is not None:
        cap_line = (
            f'window tokens: {window_used:,} / {window_cap:,}'
        )
    elif window_used:
        cap_line = f'window tokens: {window_used:,} (cap unset)'
    else:
        cap_line = ""

    rows: list[str] = [
        f'<a href="/events?type=auto_approved">'
        f'{state["auto_approved_count_24h"]} auto-approved (24h)</a>'
        f'{approved_spark}',
        f'<a href="/events?type=auto_unfreeze_applied">'
        f'{state["auto_unfreeze_applied_count_24h"]} auto-unfrozen (24h)</a>'
        f'{unfrozen_spark}',
    ]
    if state["auto_unfreeze_skipped_count_24h"]:
        rows.append(
            f'<a href="/events?type=auto_unfreeze_skipped">'
            f'{state["auto_unfreeze_skipped_count_24h"]} unfreeze skipped (24h)</a>'
        )
    # TB-241: per-axis would-* rows surface the dry-run readiness
    # counters, linking to the events drilldown so the operator can
    # inspect individual `would_auto_approve` / `would_auto_unfreeze`
    # events without leaving the home page. Each row is gated on the
    # corresponding dry-run knob (not the count) so an operator who
    # just flipped the knob with no events yet still sees the "0
    # (24h)" baseline confirming the surface is wired — same shape
    # as TB-227's auto-approved / auto-unfrozen rows.
    if aa_dry_run:
        rows.append(
            f'<a href="/events?type=would_auto_approve">'
            f'{state["would_auto_approve_count_24h"]} '
            f'would-approved (24h)</a>'
        )
    if au_dry_run:
        rows.append(
            f'<a href="/events?type=would_auto_unfreeze">'
            f'{state["would_auto_unfreeze_count_24h"]} '
            f'would-unfrozen (24h)</a>'
        )
    if cap_line:
        rows.append(html.escape(cap_line))

    meta = (
        '<span class="as-meta">'
        + ' · '.join(rows)
        + '</span>'
    )

    return (
        f'<div class="{klass}">'
        f'<span class="as-header">{html.escape(header)}{dry_run_badge}</span>'
        f'<span class="as-body">{body}</span>'
        f'{meta}'
        '</div>'
    )


# ------------- TB-181: /usage token-cost dashboard -------------


# Three event types carry usage / cost data: per-task agent runs,
# per-control agent runs (cron / mattermost / ideation), and per-bullet
# judge calls. Anything else lacks a `total_cost_usd` and is excluded
# from the dashboard's aggregation pass.
_USAGE_EVENT_TYPES: frozenset[str] = frozenset({
    "task_run_usage",
    "control_run_usage",
    "judge_call",
})

# URL `?window=` accepted values. Anything outside the set falls back
# to the default `7d` so a typo'd query string doesn't 500 the page.
_USAGE_WINDOWS = ("24h", "7d", "30d", "all")
_DEFAULT_USAGE_WINDOW = "7d"
_DEFAULT_USAGE_STACK = "event_type"

# Stable colors for the event-type stack. Mirrors the row-tint logic's
# semantic palette: blue for task work, green for control-plane work,
# amber for judges (operationally a "warning-tier" cost spike when
# hot). Kept in lockstep with `.legend-swatch` colors below for visual
# consistency between the events page and the dashboard.
_EVENT_TYPE_COLORS = {
    "task_run_usage": "#3a6db5",
    "control_run_usage": "#2a8a4a",
    "judge_call": "#b87000",
}


def _normalize_usage_window(value: str | None) -> str:
    """Map a raw `?window=` query value onto a known one. Empty / typo /
    out-of-range falls back to the default so the page never 500s on
    bad input."""
    v = (value or "").strip().lower()
    return v if v in _USAGE_WINDOWS else _DEFAULT_USAGE_WINDOW


def _normalize_usage_stack(value: str | None) -> str:
    """Map `?stack=` onto `event_type` or `model`; default to event_type."""
    v = (value or "").strip().lower()
    return v if v in ("event_type", "model") else _DEFAULT_USAGE_STACK


def _usage_window_seconds(window: str) -> float | None:
    """Window string → seconds. `all` → None (no filter)."""
    return {
        "24h": 86400.0,
        "7d": 7 * 86400.0,
        "30d": 30 * 86400.0,
    }.get(window)


def _usage_window_chart_days(window: str) -> int:
    """How many daily bars the cost chart renders for each window. For
    `all`, we still cap at 30 — the chart is for trend-spotting, not a
    full historical archive (events.jsonl is the export)."""
    return {"24h": 1, "7d": 7, "30d": 30}.get(window, 30)


def _parse_event_dt(ts: str) -> _dt.datetime | None:
    """Parse an event row's `ts` field. Tolerates missing / malformed
    so a single bad line doesn't break the aggregation pass."""
    if not ts:
        return None
    try:
        return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _load_usage_events(cfg: Config) -> list[dict]:
    """Single-pass scan of events.jsonl, keeping only the three usage-
    bearing types. Pure read; no mutation. Tolerates a missing file
    (fresh project) and individual malformed lines."""
    if not cfg.events_file.exists():
        return []
    out: list[dict] = []
    try:
        with cfg.events_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in _USAGE_EVENT_TYPES:
                    out.append(obj)
    except OSError:
        return []
    return out


def _event_cost(e: dict) -> float:
    c = e.get("total_cost_usd")
    return float(c) if isinstance(c, (int, float)) else 0.0


def _event_token_breakdown(e: dict) -> tuple[int, int, int, int]:
    """Returns (input, output, cache_creation, cache_read). Zero on missing."""
    u = e.get("usage")
    if not isinstance(u, dict):
        return 0, 0, 0, 0
    return (
        int(u.get("input_tokens", 0) or 0),
        int(u.get("output_tokens", 0) or 0),
        int(u.get("cache_creation_input_tokens", 0) or 0),
        int(u.get("cache_read_input_tokens", 0) or 0),
    )


def _event_total_tokens(e: dict) -> int:
    inp, out, cc, cr = _event_token_breakdown(e)
    return inp + out + cc + cr


def _event_subtype(e: dict) -> str:
    """The "subtype" used by the breakdown table's expand-row.
    `task_run_usage` → `status`; `control_run_usage` → `label`
    (mm-handler post-ids collapsed); `judge_call` → `verdict`."""
    typ = e.get("type")
    if typ == "task_run_usage":
        return str(e.get("status") or "unknown")
    if typ == "control_run_usage":
        label = str(e.get("label") or "unknown")
        # Collapse `MM-<post-id>` into the `mm-handler` bucket per
        # briefing — operators don't care about each post-id, they
        # care about aggregate mattermost-handler cost.
        if label.startswith("MM-") or label.startswith("mm-handler"):
            return "mm-handler"
        return label
    if typ == "judge_call":
        return str(e.get("verdict") or "unknown")
    return "unknown"


def _aggregate_usage_by_day(
    events: list[dict],
    *,
    days: int,
    end: _dt.datetime,
) -> list[dict]:
    """Per-day totals for the `days` UTC days ending at `end`'s date.
    Days with no events still appear (zero-filled) so the chart
    renders N bars even on a sparse fixture. Each entry:
        {date, by_event_type, by_model, total_cost,
         input_tokens, cache_creation, cache_read}.
    Returned oldest first.
    """
    end_day = end.date()
    start_day = end_day - _dt.timedelta(days=max(0, days - 1))
    buckets: dict[_dt.date, dict] = {}
    for i in range(days):
        d = start_day + _dt.timedelta(days=i)
        buckets[d] = {
            "date": d.isoformat(),
            "by_event_type": {t: 0.0 for t in _USAGE_EVENT_TYPES},
            "by_model": {},
            "total_cost": 0.0,
            "input_tokens": 0,
            "cache_creation": 0,
            "cache_read": 0,
        }
    for e in events:
        edt = _parse_event_dt(e.get("ts", ""))
        if edt is None:
            continue
        d = edt.date()
        if d not in buckets:
            continue
        b = buckets[d]
        cost = _event_cost(e)
        typ = e.get("type", "")
        b["total_cost"] += cost
        if typ in b["by_event_type"]:
            b["by_event_type"][typ] += cost
        mu = e.get("model_usage")
        if isinstance(mu, dict):
            for model, mu_e in mu.items():
                if not isinstance(mu_e, dict):
                    continue
                mc = mu_e.get("costUSD")
                if isinstance(mc, (int, float)):
                    b["by_model"][model] = b["by_model"].get(model, 0.0) + float(mc)
        inp, _out, cc, cr = _event_token_breakdown(e)
        b["input_tokens"] += inp
        b["cache_creation"] += cc
        b["cache_read"] += cr
    return [buckets[k] for k in sorted(buckets.keys())]


def _aggregate_usage_by_event_type(events: list[dict]) -> dict:
    """Per-event-type aggregates with a nested per-subtype breakdown.

    Returns {type: {count, total_cost, total_tokens, avg_cost,
    cache_hit_pct, input_tokens, cache_creation, cache_read,
    by_subtype: {sub: {count, total_cost, total_tokens, avg_cost,
    cache_hit_pct, input_tokens, cache_creation, cache_read}}}}.
    """
    out: dict[str, dict] = {}
    for e in events:
        typ = e.get("type", "")
        if typ not in _USAGE_EVENT_TYPES:
            continue
        bucket = out.setdefault(typ, {
            "count": 0, "total_cost": 0.0, "total_tokens": 0,
            "input_tokens": 0, "cache_creation": 0, "cache_read": 0,
            "by_subtype": {},
        })
        cost = _event_cost(e)
        toks = _event_total_tokens(e)
        inp, _out, cc, cr = _event_token_breakdown(e)
        bucket["count"] += 1
        bucket["total_cost"] += cost
        bucket["total_tokens"] += toks
        bucket["input_tokens"] += inp
        bucket["cache_creation"] += cc
        bucket["cache_read"] += cr
        sub = _event_subtype(e)
        sb = bucket["by_subtype"].setdefault(sub, {
            "count": 0, "total_cost": 0.0, "total_tokens": 0,
            "input_tokens": 0, "cache_creation": 0, "cache_read": 0,
        })
        sb["count"] += 1
        sb["total_cost"] += cost
        sb["total_tokens"] += toks
        sb["input_tokens"] += inp
        sb["cache_creation"] += cc
        sb["cache_read"] += cr

    def _finish(b: dict) -> None:
        denom = b["input_tokens"] + b["cache_creation"] + b["cache_read"]
        b["cache_hit_pct"] = (b["cache_read"] / denom * 100.0) if denom else 0.0
        b["avg_cost"] = b["total_cost"] / b["count"] if b["count"] else 0.0

    for bucket in out.values():
        _finish(bucket)
        for sb in bucket["by_subtype"].values():
            _finish(sb)
    return out


def _aggregate_usage_by_subtype(events: list[dict], event_type: str) -> dict:
    """Subtype-only breakdown for a single event type. Same shape as
    the `by_subtype` value inside `_aggregate_usage_by_event_type`'s
    output, but keyed for callers (e.g. tests) that only care about
    one type's split."""
    out = _aggregate_usage_by_event_type(
        [e for e in events if e.get("type") == event_type]
    )
    return out.get(event_type, {}).get("by_subtype", {})


def _top_n_expensive_tasks(events: list[dict], n: int = 10) -> list[dict]:
    """Top-N tasks by total cost across `task_run_usage` + `judge_call`.
    Each row: {task, run_count, task_run_count, judge_count,
    total_cost, last_seen}. Sorted by total_cost desc."""
    by_task: dict[str, dict] = {}
    for e in events:
        typ = e.get("type")
        if typ not in ("task_run_usage", "judge_call"):
            continue
        task = str(e.get("task") or "").strip()
        if not task:
            continue
        b = by_task.setdefault(task, {
            "task": task,
            "run_count": 0,
            "task_run_count": 0,
            "judge_count": 0,
            "total_cost": 0.0,
            "last_seen": "",
        })
        b["run_count"] += 1
        if typ == "task_run_usage":
            b["task_run_count"] += 1
        else:
            b["judge_count"] += 1
        b["total_cost"] += _event_cost(e)
        ts = str(e.get("ts") or "")
        if ts > b["last_seen"]:
            b["last_seen"] = ts
    rows = sorted(by_task.values(), key=lambda r: r["total_cost"], reverse=True)
    return rows[:n]


def _aggregate_by_model(events: list[dict]) -> dict[str, float]:
    """Sum `model_usage[m].costUSD` across all events. Returns {model: cost}."""
    out: dict[str, float] = {}
    for e in events:
        mu = e.get("model_usage")
        if not isinstance(mu, dict):
            continue
        for model, mu_e in mu.items():
            if not isinstance(mu_e, dict):
                continue
            cost = mu_e.get("costUSD")
            if isinstance(cost, (int, float)):
                out[model] = out.get(model, 0.0) + float(cost)
    return out


def _model_color(model: str, idx: int) -> str:
    """Stable color for a model name. Opus = purple, Haiku = teal,
    Sonnet = blue, anything else cycles through a small fallback
    palette by `idx`. Same model always renders the same color across
    page reloads (the function is pure)."""
    m = model.lower()
    if "opus" in m:
        return "#6a4ca8"
    if "haiku" in m:
        return "#2a8a8a"
    if "sonnet" in m:
        return "#3a6db5"
    palette = ["#9a3f70", "#3a8a4a", "#b06030", "#5a5a5a", "#406070"]
    return palette[idx % len(palette)]


def _render_cost_chart_svg(
    daily_costs: list[dict],
    *,
    width: int = 720,
    height: int = 240,
    stack_by: str = "event_type",
) -> str:
    """Render N daily stacked bars as inline SVG. Stacks by event type
    (default) or by model (`stack_by="model"`). Each segment carries
    a `<title>` child so the browser shows date + series + dollar
    value on hover with no JS.
    """
    if not daily_costs:
        return (
            f'<svg class="cost-chart" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="12">'
            f'no token-usage events recorded yet</text></svg>'
        )

    pad_l, pad_r, pad_t, pad_b = 56, 12, 16, 48
    chart_w = max(1, width - pad_l - pad_r)
    chart_h = max(1, height - pad_t - pad_b)

    max_total = max((float(d.get("total_cost") or 0.0) for d in daily_costs), default=0.0)
    if max_total <= 0:
        # Flat-axis no-events case — still render the axis frame so the
        # `<svg>` element + axis pin the layout. No bars to draw.
        max_total = 1.0

    n = len(daily_costs)
    slot_w = chart_w / max(n, 1)
    bar_w = max(1.0, slot_w * 0.85)

    if stack_by == "model":
        models: set[str] = set()
        for d in daily_costs:
            models.update((d.get("by_model") or {}).keys())
        series = sorted(models)
        color_map = {m: _model_color(m, i) for i, m in enumerate(series)}
    else:
        series = ["task_run_usage", "control_run_usage", "judge_call"]
        color_map = {s: _EVENT_TYPE_COLORS[s] for s in series}

    parts: list[str] = []
    parts.append(
        f'<svg class="cost-chart" data-stack="{html.escape(stack_by)}" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )

    # Y gridlines + axis labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + chart_h * (1.0 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" '
            f'x2="{pad_l + chart_w}" y2="{y:.1f}" '
            f'stroke="#eee" stroke-width="1"/>'
        )
        v = max_total * frac
        parts.append(
            f'<text x="{pad_l - 4}" y="{y + 3:.1f}" text-anchor="end" '
            f'fill="#888" font-family="sans-serif" font-size="10">'
            f'${v:.2f}</text>'
        )
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + chart_h:.1f}" '
        f'x2="{pad_l + chart_w}" y2="{pad_t + chart_h:.1f}" '
        f'stroke="#888" stroke-width="1"/>'
    )

    # Bars (stacked)
    for i, d in enumerate(daily_costs):
        x = pad_l + slot_w * i + (slot_w - bar_w) / 2.0
        y_cursor = pad_t + chart_h
        stack_dict = (
            d.get("by_model") if stack_by == "model" else d.get("by_event_type")
        ) or {}
        for s in series:
            v = float(stack_dict.get(s, 0.0))
            if v <= 0:
                continue
            seg_h = chart_h * (v / max_total)
            y_cursor -= seg_h
            color = color_map.get(s, "#888")
            parts.append(
                f'<rect class="cost-seg" data-series="{html.escape(s)}" '
                f'x="{x:.1f}" y="{y_cursor:.1f}" '
                f'width="{bar_w:.1f}" height="{seg_h:.1f}" '
                f'fill="{color}">'
                f'<title>{html.escape(str(d.get("date") or ""))} · '
                f'{html.escape(s)}: ${v:.4f}</title>'
                f'</rect>'
            )

    # X labels: first / middle / last so a 30-bar chart doesn't pile
    # text on top of itself. Operators wanting a per-day value hover
    # the bar for the SVG `<title>` tooltip.
    label_indices = sorted({0, n // 2, n - 1}) if n > 0 else []
    for i in label_indices:
        if i < 0 or i >= n:
            continue
        x = pad_l + slot_w * (i + 0.5)
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t + chart_h + 14:.1f}" '
            f'text-anchor="middle" fill="#888" '
            f'font-family="sans-serif" font-size="10">'
            f'{html.escape(str(daily_costs[i].get("date") or ""))}</text>'
        )

    # Legend below
    legend_y = height - 14
    legend_x = pad_l
    for s in series:
        color = color_map.get(s, "#888")
        parts.append(
            f'<rect class="legend-swatch-svg" data-series="{html.escape(s)}" '
            f'x="{legend_x}" y="{legend_y - 9}" width="10" height="10" '
            f'fill="{color}"/>'
        )
        parts.append(
            f'<text x="{legend_x + 14}" y="{legend_y}" '
            f'fill="#444" font-family="sans-serif" font-size="11">'
            f'{html.escape(s)}</text>'
        )
        legend_x += 14 + max(8, len(s)) * 6.5 + 12
    parts.append("</svg>")
    return "".join(parts)


def _render_cache_chart_svg(
    daily_hit_ratios: list[float],
    *,
    width: int = 720,
    height: int = 120,
) -> str:
    """Render daily cache-hit ratio as a sparkline-style line chart with
    per-point dots (each carrying a `<title>` tooltip)."""
    if not daily_hit_ratios:
        return (
            f'<svg class="cache-chart" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="12">'
            f'no token-usage events recorded yet</text></svg>'
        )
    pad_l, pad_r, pad_t, pad_b = 44, 12, 12, 24
    chart_w = max(1, width - pad_l - pad_r)
    chart_h = max(1, height - pad_t - pad_b)
    n = len(daily_hit_ratios)

    parts: list[str] = []
    parts.append(
        f'<svg class="cache-chart" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + chart_h * (1.0 - frac)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" '
            f'x2="{pad_l + chart_w}" y2="{y:.1f}" '
            f'stroke="#eee" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 4}" y="{y + 3:.1f}" text-anchor="end" '
            f'fill="#888" font-family="sans-serif" font-size="10">'
            f'{frac * 100:.0f}%</text>'
        )

    def _x(i: int) -> float:
        if n <= 1:
            return pad_l + chart_w / 2.0
        return pad_l + (chart_w / (n - 1)) * i

    points = []
    for i, ratio in enumerate(daily_hit_ratios):
        x = _x(i)
        y = pad_t + chart_h * (1.0 - max(0.0, min(1.0, ratio)))
        points.append(f"{x:.1f},{y:.1f}")
    parts.append(
        f'<polyline class="cache-line" points="{" ".join(points)}" '
        f'fill="none" stroke="#3a6db5" stroke-width="2"/>'
    )
    for i, ratio in enumerate(daily_hit_ratios):
        x = _x(i)
        y = pad_t + chart_h * (1.0 - max(0.0, min(1.0, ratio)))
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#3a6db5">'
            f'<title>day {i + 1}: {ratio * 100:.1f}%</title>'
            f'</circle>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _render_model_split_svg(
    model_costs: dict[str, float],
    *,
    width: int = 720,
    height: int = 60,
) -> str:
    """Horizontal stacked bar of total cost percentages by model.
    Sorted by cost desc; each segment + legend entry carries a
    `<title>` tooltip."""
    total = sum(v for v in model_costs.values() if isinstance(v, (int, float)))
    if not model_costs or total <= 0:
        return (
            f'<svg class="model-split" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="12">'
            f'no token-usage events recorded yet</text></svg>'
        )
    pad_l, pad_r = 12, 12
    bar_w = max(1, width - pad_l - pad_r)
    bar_y = 8
    bar_h = 20

    items = sorted(model_costs.items(), key=lambda kv: kv[1], reverse=True)

    parts: list[str] = []
    parts.append(
        f'<svg class="model-split" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )
    x = float(pad_l)
    for i, (model, cost) in enumerate(items):
        seg_w = bar_w * (cost / total)
        color = _model_color(model, i)
        pct = cost / total * 100.0
        parts.append(
            f'<rect class="model-seg" data-model="{html.escape(model)}" '
            f'x="{x:.1f}" y="{bar_y}" width="{seg_w:.1f}" height="{bar_h}" '
            f'fill="{color}">'
            f'<title>{html.escape(model)}: ${cost:.4f} ({pct:.1f}%)</title>'
            f'</rect>'
        )
        x += seg_w

    legend_y = bar_y + bar_h + 16
    legend_x = float(pad_l)
    for i, (model, cost) in enumerate(items):
        color = _model_color(model, i)
        pct = cost / total * 100.0
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 9}" width="10" height="10" '
            f'fill="{color}"/>'
        )
        label = f'{model} ({pct:.1f}%)'
        parts.append(
            f'<text x="{legend_x + 14}" y="{legend_y}" '
            f'fill="#444" font-family="sans-serif" font-size="11">'
            f'{html.escape(label)}</text>'
        )
        legend_x += 14 + max(8, len(label)) * 6.5 + 14
    parts.append("</svg>")
    return "".join(parts)


def _render_usage(
    cfg: Config,
    *,
    window: str | None = None,
    stack_by: str | None = None,
    now: _dt.datetime | None = None,
) -> str:
    """TB-181: token-cost dashboard.

    Reads events.jsonl once per page load, aggregates the three usage-
    bearing event types, and renders cost-over-time + breakdowns +
    top-N tasks + model-split + cache analysis as inline SVG + HTML.

    URL config: `?window=24h|7d|30d|all` (default 7d) and
    `?stack=event_type|model` (default event_type). Out-of-range values
    fall back to the default — no persistent state, the URL is the
    only configuration surface.

    `now` is injected for tests; the URL handler uses the current UTC
    time (the daemon's wall clock).
    """
    window = _normalize_usage_window(window)
    stack_by = _normalize_usage_stack(stack_by)
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)

    events_all = _load_usage_events(cfg)

    window_seconds = _usage_window_seconds(window)
    if window_seconds is None:
        window_events = list(events_all)
        prior_events: list[dict] = []
    else:
        cutoff = now - _dt.timedelta(seconds=window_seconds)
        prior_cutoff = cutoff - _dt.timedelta(seconds=window_seconds)
        window_events = []
        prior_events = []
        for e in events_all:
            edt = _parse_event_dt(e.get("ts", ""))
            if edt is None:
                continue
            if edt >= cutoff and edt <= now:
                window_events.append(e)
            elif edt >= prior_cutoff and edt < cutoff:
                prior_events.append(e)

    chart_days = _usage_window_chart_days(window)

    # ---- Header summary ------------------------------------------
    total_window = sum(_event_cost(e) for e in window_events)
    total_prior = sum(_event_cost(e) for e in prior_events)
    if total_prior > 0 and window != "all":
        delta_pct = (total_window - total_prior) / total_prior * 100.0
        arrow = "↑" if delta_pct >= 0 else "↓"
        trend_html = (
            f'{arrow} {abs(delta_pct):.0f}% vs prior {html.escape(window)}'
        )
    elif window == "all":
        trend_html = '(all-time view)'
    elif total_window > 0:
        trend_html = '(no prior-window comparison)'
    else:
        trend_html = '—'

    inp_w = sum(_event_token_breakdown(e)[0] for e in window_events)
    cc_w = sum(_event_token_breakdown(e)[2] for e in window_events)
    cr_w = sum(_event_token_breakdown(e)[3] for e in window_events)
    cache_denom_w = inp_w + cc_w + cr_w
    cache_hit_w = (cr_w / cache_denom_w * 100.0) if cache_denom_w else 0.0

    cc_p = sum(_event_token_breakdown(e)[2] for e in prior_events)
    cr_p = sum(_event_token_breakdown(e)[3] for e in prior_events)

    top_window = _top_n_expensive_tasks(window_events, n=1)
    if top_window:
        t = top_window[0]
        most_exp_html = (
            f'<a href="/task/{html.escape(t["task"])}">'
            f'{html.escape(t["task"])}</a> · ${t["total_cost"]:.2f}'
        )
    else:
        most_exp_html = '—'

    # Window / stack chips
    chip_lines: list[str] = ['<div class="usage-chips">window:']
    for w in _USAGE_WINDOWS:
        cls = "on" if w == window else ""
        chip_lines.append(
            f' <a href="/usage?window={w}&amp;stack={stack_by}" '
            f'class="{cls}">{w}</a>'
        )
    chip_lines.append(' &middot; stack:')
    for s in ("event_type", "model"):
        cls = "on" if s == stack_by else ""
        chip_lines.append(
            f' <a href="/usage?window={window}&amp;stack={s}" '
            f'class="{cls}">{s}</a>'
        )
    chip_lines.append('</div>')
    chips_html = "".join(chip_lines)

    summary_card = (
        '<div class="usage-card usage-summary">'
        f'{chips_html}'
        '<div class="usage-stats">'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">total cost ({html.escape(window)})</div>'
        f'<div class="usage-stat-value">${total_window:.2f}</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">trend</div>'
        f'<div class="usage-stat-value usage-stat-small">{trend_html}</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">cache hit</div>'
        f'<div class="usage-stat-value">{cache_hit_w:.1f}%</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">most expensive</div>'
        f'<div class="usage-stat-value usage-stat-small">{most_exp_html}</div></div>'
        '</div></div>'
    )

    # ---- Cost chart ----------------------------------------------
    # When `window=all` we still cap the chart at the most recent 30
    # days (`_usage_window_chart_days` returns 30 for `all`); the
    # underlying full event list is what feeds aggregation so older
    # days still tick the model/breakdown sections.
    daily = _aggregate_usage_by_day(events_all, days=chart_days, end=now)
    cost_chart = _render_cost_chart_svg(daily, stack_by=stack_by)

    # ---- Breakdown by event type ---------------------------------
    by_type = _aggregate_usage_by_event_type(window_events)
    sorted_types = sorted(
        by_type.items(), key=lambda kv: kv[1]["total_cost"], reverse=True
    )
    if sorted_types:
        rows = []
        for typ, agg in sorted_types:
            sub_rows = []
            for sub, sa in sorted(
                agg["by_subtype"].items(),
                key=lambda kv: kv[1]["total_cost"],
                reverse=True,
            ):
                sub_rows.append(
                    f'<tr class="usage-sub">'
                    f'<td class="usage-sub-label">↳ {html.escape(str(sub))}</td>'
                    f'<td>{sa["count"]}</td>'
                    f'<td>${sa["total_cost"]:.4f}</td>'
                    f'<td>{sa["total_tokens"]:,}</td>'
                    f'<td>${sa["avg_cost"]:.4f}</td>'
                    f'<td>{sa["cache_hit_pct"]:.1f}%</td>'
                    f'</tr>'
                )
            sub_block = (
                f'<details><summary>{html.escape(typ)} '
                f'<span class="meta">— {len(sub_rows)} subtype'
                f'{"s" if len(sub_rows) != 1 else ""}</span></summary>'
                f'<table class="usage-sub-table">'
                f'<thead><tr><th>subtype</th><th>count</th>'
                f'<th>total $</th><th>tokens</th><th>avg $/event</th>'
                f'<th>cache hit</th></tr></thead>'
                f'<tbody>{"".join(sub_rows)}</tbody></table></details>'
            )
            rows.append(
                f'<tr><td>{sub_block}</td>'
                f'<td>{agg["count"]}</td>'
                f'<td>${agg["total_cost"]:.4f}</td>'
                f'<td>{agg["total_tokens"]:,}</td>'
                f'<td>${agg["avg_cost"]:.4f}</td>'
                f'<td>{agg["cache_hit_pct"]:.1f}%</td></tr>'
            )
        breakdown_html = (
            '<table class="usage-breakdown"><thead>'
            '<tr><th>event type</th><th>count</th>'
            '<th>total $</th><th>tokens</th>'
            '<th>avg $/event</th><th>cache hit</th></tr>'
            '</thead><tbody>' + "".join(rows) + '</tbody></table>'
        )
    else:
        breakdown_html = (
            '<p class="meta">no token-usage events recorded yet '
            'in this window.</p>'
        )

    # ---- Top-10 expensive tasks ----------------------------------
    top_tasks = _top_n_expensive_tasks(window_events, n=10)
    if top_tasks:
        try:
            board = Board.load(cfg.tasks_file)
        except OSError:
            board = None
        rows = []
        for t in top_tasks:
            tb = t["task"]
            title = ""
            if board is not None:
                task_obj = board.get(tb)
                if task_obj is not None:
                    title = task_obj.title[:80]
            mix = (
                f'{t["task_run_count"]} run'
                f'{"s" if t["task_run_count"] != 1 else ""}'
                f' + {t["judge_count"]} judge'
                f'{"s" if t["judge_count"] != 1 else ""}'
            )
            rows.append(
                f'<tr><td><a href="/task/{html.escape(tb)}">'
                f'{html.escape(tb)}</a></td>'
                f'<td>{html.escape(title)}</td>'
                f'<td>{html.escape(mix)}</td>'
                f'<td>${t["total_cost"]:.4f}</td>'
                f'<td class="ts">{html.escape(t["last_seen"])}</td></tr>'
            )
        top_html = (
            '<table class="usage-top-tasks"><thead>'
            '<tr><th>task</th><th>title</th>'
            '<th>runs</th><th>total $</th><th>last seen</th></tr>'
            '</thead><tbody>' + "".join(rows) + '</tbody></table>'
        )
    else:
        top_html = (
            '<p class="meta">no task-attributable events recorded yet '
            'in this window.</p>'
        )

    # ---- Model split ---------------------------------------------
    model_costs = _aggregate_by_model(window_events)
    model_html = _render_model_split_svg(model_costs)

    # ---- Cache analysis ------------------------------------------
    daily_hit = []
    for d in daily:
        denom = d["input_tokens"] + d["cache_creation"] + d["cache_read"]
        ratio = (d["cache_read"] / denom) if denom else 0.0
        daily_hit.append(ratio)
    cache_chart = _render_cache_chart_svg(daily_hit)

    cache_callouts = (
        '<div class="usage-stats">'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">cache creation</div>'
        f'<div class="usage-stat-value">{cc_w:,}</div>'
        f'<div class="usage-stat-prior">prior {html.escape(window)}: {cc_p:,}</div></div>'
        f'<div class="usage-stat">'
        f'<div class="usage-stat-label">cache read</div>'
        f'<div class="usage-stat-value">{cr_w:,}</div>'
        f'<div class="usage-stat-prior">prior {html.escape(window)}: {cr_p:,}</div></div>'
        '</div>'
    )

    body = (
        f'<h1>usage <span class="meta">— window: {html.escape(window)}, '
        f'stack: {html.escape(stack_by)}'
        f' · {len(events_all):,} usage event(s) on file'
        f'</span></h1>'
        f'{summary_card}'
        '<h2>cost over time</h2>'
        f'<div class="usage-card">{cost_chart}</div>'
        '<h2>breakdown by event type</h2>'
        f'<div class="usage-card">{breakdown_html}</div>'
        '<h2>top-10 expensive tasks</h2>'
        f'<div class="usage-card">{top_html}</div>'
        '<h2>model split</h2>'
        f'<div class="usage-card">{model_html}</div>'
        '<h2>cache analysis</h2>'
        f'<div class="usage-card">{cache_callouts}{cache_chart}</div>'
    )
    return _layout("usage", body)


# ------------- page renderers -------------


def _render_home(cfg: Config) -> str:
    pid = read_pid(cfg)
    running = _is_alive(pid)
    paused = cfg.pause_flag.exists()
    board = Board.load(cfg.tasks_file)
    counts = {s: sum(1 for _ in board.iter_tasks(section=s))
              for s in ("Active", "Ready", "Backlog", "Pipeline Pending",
                        "Complete", "Frozen")}
    evts = ev_mod.tail(cfg.events_file, n=30)
    evts.reverse()  # newest first

    if running:
        status = f'<span class="running">running</span> (pid {pid})'
    else:
        status = '<span class="stopped">stopped</span>'
    if paused:
        status += ' <span class="paused">[paused]</span>'

    # TB-173 / TB-191: ideator-surfaced "Decisions needed from
    # operator" preamble. The helper returns "" when
    # `.cc-autopilot/ideation_state.md` has no `## Decisions needed
    # from operator` section (or it's empty), so the card is omitted
    # entirely on the steady-state happy path. When non-empty, sits
    # ABOVE `_render_pending_queue` since ideator decisions tend to
    # ask for goal.md edits / focus-item rotations / approve-or-reject
    # calls (operator-judgement work) while pending ops are mechanical
    # and imminent — surfacing the judgement work first reflects
    # priority.
    operator_decisions_html = _render_operator_decisions(cfg)
    # TB-162: pending-operator-queue preamble. The helper returns "" when
    # the queue file is empty / fully drained, so the card is omitted
    # entirely on the steady-state happy path; non-empty queues get a
    # yellow card listing each pending op above the events table.
    pending_html = _render_pending_queue(cfg)
    # TB-197: ideation gate-state card — always rendered (compact 1-2
    # line shape) so the operator can answer "when does ideation next
    # fire?" without grepping `cron_state.json`. Sits below the
    # operator-decisions / pending-queue cards (which are demand-driven
    # and therefore higher-priority when present) and above the events
    # table (which is the historical record, lower priority than the
    # forward-looking gate-state read).
    ideation_status_html = _render_ideation_status_block(cfg)
    # TB-227: auto-approve / auto-unfreeze loop state card. Sits
    # alongside the ideation gate-state card (visual sibling — both
    # are "what's the daemon's automation doing right now?" surfaces).
    # Omitted entirely on pre-opt-in projects (knob off + no 24h
    # activity); see `_render_automation_card` for the contract.
    automation_html = _render_automation_card(cfg)

    body = (
        f"<h1>ap2 — {html.escape(cfg.project_root.name)}</h1>"
        f'<div class="meta">{html.escape(str(cfg.project_root))}</div>'
        f"<h2>daemon</h2><p>{status}</p>"
        f"<h2>board</h2>"
        f'<div class="stats">'
        + "".join(
            f'<div class="stat"><div class="stat-label">{s}</div>'
            f'<div class="stat-value">{counts[s]}</div></div>'
            for s in ("Active", "Ready", "Backlog", "Pipeline Pending",
                      "Complete", "Frozen")
        )
        + "</div>"
        f"{operator_decisions_html}"
        f"{pending_html}"
        f"{ideation_status_html}"
        f"{automation_html}"
        f'<h2>events <span class="meta">— last 30, newest first '
        f'(<a href="/events">all</a>)</span></h2>'
        f"{_events_table(evts, cfg=cfg)}"
    )
    return _layout(cfg.project_root.name, body)


def _render_events(
    cfg: Config, *, typ: str | None, n: int, show_tokens: bool = False
) -> str:
    # Pull a generous tail and post-filter so type-filter pages always show n
    # matches even when the type is rare in the recent window.
    pull = max(n * 20, n) if typ else n
    evts = ev_mod.tail(cfg.events_file, n=pull)
    if typ:
        evts = [e for e in evts if e.get("type") == typ]
    evts = evts[-n:]
    evts.reverse()

    # Quick-filter buttons for the most common types.
    # TB-157: include `judge_call` so operators can isolate prose-judge
    # cost spikes without grepping events.jsonl by hand.
    quick = ["task_complete", "task_error", "cron_complete", "cron_error",
             "ideation_empty_board", "ideation_complete", "ideation_error",
             "verification_failed", "verification_partial",
             "backlog_auto_promoted", "daemon_start", "judge_call"]
    filt = '<div class="filter">filter:'
    filt += f' <a href="/events?n={n}" class="{"on" if not typ else ""}">all</a>'
    for k in quick:
        cls = "on" if typ == k else ""
        filt += f' <a href="/events?type={k}&n={n}" class="{cls}">{k}</a>'
    filt += "</div>"

    # TB-148: tiny legend so the row tints are self-documenting on first
    # visit. Hidden behind a `<details>` so it doesn't crowd the filter
    # bar — operators who already know the palette never see it expanded.
    legend = (
        '<details class="filter"><summary>row colors</summary>'
        '<div style="padding:0.4rem 0;font-size:12px;line-height:1.6">'
        '<span class="meta">task_complete tints by status:</span> '
        '<span class="legend-swatch lifecycle">complete</span> '
        '<span class="legend-swatch warning">verification_failed</span> '
        '<span class="legend-swatch failure">state_violation / error / timeout / '
        'incomplete / blocked / failed</span> '
        '<span class="legend-swatch frozen">retry_exhausted</span> '
        '<span class="legend-swatch neutral">unknown</span>'
        '</div></details>'
    )

    body = (
        f"<h1>events <span class=\"meta\">"
        f"— {len(evts)} shown{', filter: ' + html.escape(typ) if typ else ''}</span></h1>"
        f"{legend}"
        f"{filt}"
        f"{_events_table(evts, cfg=cfg, show_tokens=show_tokens)}"
    )
    return _layout("events", body)


def _render_tasks(cfg: Config, *, filter_kind: str | None = None) -> str:
    """Tasks page. `filter_kind="pending-review"` restricts every
    section to tasks gated on the `review` scheme (TB-121). The default
    rendering shows everything.
    """
    board = Board.load(cfg.tasks_file)
    only_review = filter_kind == "pending-review"

    # Filter bar: link to the unfiltered view, link to pending-review
    # only. TB-121: this is the operator's "what's in my review queue"
    # surface — separate from `/events` and the home page so the
    # board-state read is one click and one URL.
    pending_total = sum(
        1 for t in board.iter_tasks() if _is_pending_review(t)
    )
    filt_parts = ['<div class="filter">filter:']
    cls_all = "" if only_review else "on"
    cls_review = "on" if only_review else ""
    filt_parts.append(f' <a href="/tasks" class="{cls_all}">all</a>')
    filt_parts.append(
        f' <a href="/tasks?filter=pending-review" class="{cls_review}">'
        f"pending review ({pending_total})</a>"
    )
    filt_parts.append("</div>")
    filt = "".join(filt_parts)

    sections_html = []
    for s, limit in (("Active", None), ("Ready", None), ("Backlog", None),
                     ("Pipeline Pending", None),
                     ("Complete", 30), ("Frozen", None)):
        # In pending-review filter mode, only Backlog can plausibly
        # carry the gate (ideation only adds there) — but we don't
        # short-circuit other sections; if a `@blocked:review` token
        # somehow ends up in Active/Ready it's worth showing.
        section_iter = board.iter_tasks(section=s)
        if only_review:
            tasks_in_section = [t for t in section_iter if _is_pending_review(t)]
            count = len(tasks_in_section)
            # Skip empty sections in filter mode to keep the page focused.
            if count == 0:
                continue
        else:
            count = sum(1 for _ in board.iter_tasks(section=s))
        label = f"{s} <span class=\"meta\">({count} total)</span>"
        if limit is not None and not only_review:
            label += f" <span class=\"meta\">— last {limit}</span>"
        sections_html.append(
            f"<h2>{label}</h2>"
            f"{_tasks_list(board, s, limit=None if only_review else limit, only_pending_review=only_review)}"
        )
    if only_review and not sections_html:
        sections_html.append(
            "<p><em>(no tasks pending review — ideation proposals are "
            "either approved, deleted, or none have been authored yet)</em></p>"
        )
    body = (
        "<h1>tasks"
        + (
            ' <span class="meta">— filter: pending review</span>'
            if only_review
            else ""
        )
        + f"</h1>{filt}"
        + "".join(sections_html)
    )
    return _layout("tasks", body)


def _render_task(cfg: Config, tb_id: str) -> str:
    board = Board.load(cfg.tasks_file)
    task = board.get(tb_id)
    if task is None:
        return _layout(
            f"{tb_id} not found",
            f"<h1>{html.escape(tb_id)}</h1>"
            f'<p>Not on the board. <a href="/tasks">All tasks</a></p>',
        )
    # Briefing: Task.briefing is a relative path string OR None
    briefing_html = ""
    if task.briefing:
        path = (cfg.project_root / task.briefing).resolve()
        try:
            text = path.read_text()
            briefing_html = (
                f"<h2>briefing <span class=\"meta\">— {html.escape(str(path.relative_to(cfg.project_root)))}</span></h2>"
                f"<pre>{html.escape(text)}</pre>"
            )
        except OSError as e:
            briefing_html = f"<h2>briefing</h2><p class=\"meta\">(could not read: {html.escape(str(e))})</p>"

    # Related events: any event with task=<tb_id> or that mentions tb_id in its
    # JSON body. The latter catches things like cron summaries that reference
    # the task id without a structured field.
    pull = 5000
    evts = ev_mod.tail(cfg.events_file, n=pull)
    related = [
        e for e in evts
        if e.get("task") == tb_id or tb_id in json.dumps(e, default=str)
    ]
    related.reverse()

    # TB-129: per-task "Runs" section. One row per debug-dump set on disk —
    # operators chasing a flaky retry loop want quick access to every prior
    # attempt's prompt + stream without grepping events.jsonl by hand. Most
    # recent first so the live attempt (if any) sits at the top.
    runs_html = _render_task_runs_section(cfg, tb_id)

    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in task.tags)
    desc = f"<p>{html.escape(task.description)}</p>" if task.description else ""
    body = (
        f'<h1><span class="id">{html.escape(task.id)}</span> {html.escape(task.title)}</h1>'
        f'<div class="meta">section: <strong>{html.escape(task.section)}</strong>'
        f'{" — checked" if task.checked else ""}{tags}</div>'
        f"{desc}"
        f"{briefing_html}"
        f"{runs_html}"
        f"<h2>related events <span class=\"meta\">— {len(related)} shown</span></h2>"
        f"{_events_table(related, cfg=cfg)}"
    )
    return _layout(task.id, body)


def _render_task_runs_section(cfg: Config, task_id: str) -> str:
    """Per-task list of debug runs with live links + terminal status badges.

    Sourced from disk (`_list_run_ids_for_task`) rather than events.jsonl so
    we surface runs whose `task_start` event has rolled off the tail. Each
    row links to `/task-run/<run-id>` and shows the matching terminal event
    (status, commit prefix) when present, or an `in-flight` badge otherwise.
    """
    run_ids = _list_run_ids_for_task(cfg, task_id)
    if not run_ids:
        return (
            "<h2>runs <span class=\"meta\">— none on disk</span></h2>"
            "<p class=\"meta\">No SDK debug dumps for this task in "
            "<code>.cc-autopilot/debug/</code>. Either the task hasn't run "
            "yet, or its dumps were pruned.</p>"
        )
    rows = []
    for rid in reversed(run_ids):  # newest first
        m = _RUN_ID_RE.match(rid)
        if not m:
            continue
        compact_ts = m.group(1)
        terminal = _terminal_event_for_run(cfg, compact_ts, task_id)
        badge_html = _run_status_badge(terminal)
        terminal_summary = ""
        if terminal:
            commit = str(terminal.get("commit") or "")[:8]
            extras = []
            if commit:
                extras.append(f"commit <code>{html.escape(commit)}</code>")
            summary = str(terminal.get("summary") or "")[:200]
            if summary:
                extras.append(html.escape(summary))
            terminal_summary = (
                f' <span class="meta">— {" · ".join(extras)}</span>'
                if extras else ""
            )
        rows.append(
            f"<tr>"
            f'<td class="ts">{html.escape(compact_ts)}</td>'
            f'<td><a class="id" href="/task-run/{html.escape(rid)}">'
            f'{html.escape(rid)}</a></td>'
            f"<td>{badge_html}{terminal_summary}</td>"
            f"</tr>"
        )
    table = (
        "<table><thead><tr><th>started</th><th>run-id</th><th>status</th></tr>"
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
    )
    return (
        f"<h2>runs <span class=\"meta\">— {len(run_ids)} attempt(s), "
        f"newest first</span></h2>{table}"
    )


def _run_status_badge(terminal: dict | None) -> str:
    """Render a one-word badge for a run's terminal verdict (or in-flight)."""
    if terminal is None:
        return '<span class="run-status in-flight">in-flight</span>'
    typ = terminal.get("type")
    status = str(terminal.get("status") or "")
    if typ == "task_complete" and status == "complete":
        return '<span class="run-status success">complete</span>'
    if typ == "task_complete":
        # incomplete / blocked / failed / verification_failed — all non-success
        return f'<span class="run-status failure">{html.escape(status or typ)}</span>'
    return f'<span class="run-status failure">{html.escape(typ)}</span>'


# ------------- task-run live detail page (TB-129) -------------


def _classify_row(row: dict) -> tuple[str, str]:
    """Return (css_class, display_label) for one stream row.

    Rows are color-coded by their semantic role rather than the raw envelope
    type: the operator cares whether they're looking at the model's text, a
    tool dispatch, the tool's reply, or the final ResultMessage. Sub-classes
    (`is-error`, `is-success`) layer success/failure tinting onto the base.
    """
    typ = row.get("type") or "?"
    if typ == "AssistantMessage":
        if row.get("tool_calls"):
            return "row-tool", "tool-call"
        return "row-assistant", "assistant"
    if typ == "UserMessage":
        results = row.get("tool_results") or []
        any_err = any(r.get("is_error") for r in results)
        return ("row-tool-result is-error" if any_err else "row-tool-result",
                "tool-result")
    if typ == "ResultMessage":
        sub = row.get("subtype") or ""
        if sub == "success":
            return "row-result is-success", f"result/{sub}"
        return "row-result", f"result/{sub}" if sub else "result"
    if typ == "SystemMessage":
        sub = row.get("subtype") or ""
        return "row-system", f"system/{sub}" if sub else "system"
    return "row-system", typ


def _format_tool_call(tc: dict) -> str:
    name = html.escape(str(tc.get("name") or "?"))
    args = tc.get("args_preview") or ""
    return (
        f"<div><strong>{name}</strong>"
        f' <span class="meta">{html.escape(str(args))}</span></div>'
    )


def _format_tool_result(tr: dict) -> str:
    err = "❌ " if tr.get("is_error") else ""
    preview = str(tr.get("preview") or "")
    return (
        f'<div>{err}<span class="meta">{html.escape(str(tr.get("tool_use_id") or ""))[:12]}…</span>'
        f"<pre>{html.escape(preview)}</pre></div>"
    )


def _row_summary_html(row: dict) -> str:
    """Compact body cell for a stream row."""
    parts = []
    text_preview = row.get("text_preview")
    if text_preview:
        parts.append(f"<pre>{html.escape(str(text_preview))}</pre>")
    tcs = row.get("tool_calls") or []
    for tc in tcs:
        parts.append(_format_tool_call(tc))
    trs = row.get("tool_results") or []
    for tr in trs:
        parts.append(_format_tool_result(tr))
    if row.get("type") == "ResultMessage":
        cost = row.get("total_cost_usd")
        turns = row.get("num_turns")
        stop = row.get("stop_reason") or ""
        sub = row.get("subtype") or ""
        bits = []
        if sub:
            bits.append(f"subtype=<code>{html.escape(str(sub))}</code>")
        if stop:
            bits.append(f"stop_reason=<code>{html.escape(str(stop))}</code>")
        if turns is not None:
            bits.append(f"turns={turns}")
        if cost is not None:
            try:
                bits.append(f"cost=<code>${float(cost):.4f}</code>")
            except (TypeError, ValueError):
                bits.append(f"cost={html.escape(str(cost))}")
        if bits:
            parts.append("<div>" + " · ".join(bits) + "</div>")
    model = row.get("model")
    if model:
        parts.append(f'<div class="meta">model: {html.escape(str(model))}</div>')
    if not parts:
        parts.append('<span class="meta">(no preview)</span>')
    return "".join(parts)


def _row_full_body_html(row_full: dict | None) -> str:
    """`<details>` block rendering the full message body from messages.jsonl."""
    if row_full is None:
        return '<div class="meta">(full body unavailable)</div>'
    pretty = json.dumps(row_full, indent=2, default=str)
    return (
        f"<details><summary>full body</summary>"
        f"<pre>{html.escape(pretty)}</pre></details>"
    )


def _compute_run_usage_totals(rows: list[dict]) -> dict:
    """TB-157: aggregate token / cache / cost across a run's stream rows.

    Walks every row carrying a `usage` dict (typically the trailing
    ResultMessage; some sessions emit multiple ResultMessages on
    multi-turn loops, so we sum across all of them). Returns
    ``{total_messages_with_usage, input_tokens, output_tokens,
    cache_creation, cache_read, hit_rate, total_cost_usd}``.

    `hit_rate` is `cache_read / (cache_read + cache_creation +
    input_tokens)` — the fraction of input that didn't pay the
    fresh-prompt token rate. Returns an empty dict when no row has
    usage data (legacy runs from before TB-157 capture).
    """
    inp = out = cc = cr = 0
    cost = 0.0
    n = 0
    for r in rows:
        u = r.get("usage")
        if isinstance(u, dict):
            n += 1
            inp += int(u.get("input_tokens", 0) or 0)
            out += int(u.get("output_tokens", 0) or 0)
            cc += int(u.get("cache_creation_input_tokens", 0) or 0)
            cr += int(u.get("cache_read_input_tokens", 0) or 0)
        c = r.get("total_cost_usd")
        if isinstance(c, (int, float)):
            cost += float(c)
    if n == 0:
        return {}
    denom = cr + cc + inp
    hit_rate = (cr / denom) if denom else 0.0
    return {
        "total_messages_with_usage": n,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation": cc,
        "cache_read": cr,
        "hit_rate": hit_rate,
        "total_cost_usd": cost,
    }


def _render_run_usage_footer(rows: list[dict]) -> str:
    """Render the TB-157 token/usage totals footer for the per-task-run
    detail page. Returns "" when no usage data is present (legacy runs).
    """
    t = _compute_run_usage_totals(rows)
    if not t:
        return ""
    pct = f"{t['hit_rate'] * 100:.1f}%"
    return (
        '<h2>usage <span class="meta">— totals across this run\'s '
        'ResultMessages</span></h2>'
        '<table class="usage-totals"><tbody>'
        f'<tr><th>messages with usage</th>'
        f'<td>{t["total_messages_with_usage"]}</td></tr>'
        f'<tr><th>input tokens</th><td>{t["input_tokens"]:,}</td></tr>'
        f'<tr><th>output tokens</th><td>{t["output_tokens"]:,}</td></tr>'
        f'<tr><th>cache creation</th><td>{t["cache_creation"]:,}</td></tr>'
        f'<tr><th>cache read</th><td>{t["cache_read"]:,}</td></tr>'
        f'<tr><th>cache hit rate</th><td>{html.escape(pct)}</td></tr>'
        f'<tr><th>total cost (USD)</th>'
        f'<td>${t["total_cost_usd"]:.4f}</td></tr>'
        '</tbody></table>'
    )


def _render_run_rows_html(
    rows: list[dict], full_by_seq: dict[int, dict]
) -> str:
    """Render a list of stream rows as `<tr>`s for the live detail table."""
    out = []
    for r in rows:
        seq = r.get("seq", "?")
        cls, label = _classify_row(r)
        full = full_by_seq.get(int(seq)) if isinstance(seq, int) else None
        out.append(
            f'<tr class="{cls}" data-seq="{html.escape(str(seq))}">'
            f'<td class="ts">#{html.escape(str(seq))}</td>'
            f'<td class="type">{html.escape(label)}</td>'
            f'<td>{_row_summary_html(r)}{_row_full_body_html(full)}</td>'
            f"</tr>"
        )
    return "".join(out)


def _render_task_run(cfg: Config, run_id: str) -> str:
    """Live SDK debug stream for one run. The page polls a JSON sub-endpoint
    every ~3s while the run is in-flight (no terminal event yet) and stops
    once the verdict lands.

    Triple-file backing (TB-85): `<run>.prompt.md` (full system+user prompt),
    `<run>.stream.jsonl` (compact summaries — what we render in the table),
    `<run>.messages.jsonl` (full bodies — surfaced under per-row `<details>`).
    Path traversal: `run_id` must match `_RUN_ID_RE` AND not contain `/` —
    rejected before any disk access.
    """
    # Path-traversal guard: only the `<compact_ts>-<task_id>` shape is valid,
    # and `task_id` may not contain a path separator. Files outside the debug
    # dir cannot be referenced even via crafted run_ids.
    safe = Path(run_id).name
    if safe != run_id or not _RUN_ID_RE.match(run_id):
        return _layout(
            "task-run",
            "<h1>task-run</h1>"
            f'<p>invalid run-id: <code>{html.escape(run_id)}</code></p>',
        )
    m = _RUN_ID_RE.match(run_id)
    assert m  # narrowed by the regex check above
    compact_ts, task_id = m.group(1), m.group(2)
    d = _debug_dir(cfg)
    prompt_p = d / f"{run_id}.prompt.md"
    stream_p = d / f"{run_id}.stream.jsonl"
    messages_p = d / f"{run_id}.messages.jsonl"
    if not stream_p.exists():
        return _layout(
            "task-run",
            f"<h1>task-run <code>{html.escape(run_id)}</code></h1>"
            f'<p class="meta">No stream.jsonl on disk; debug files may have '
            f'been pruned. <a href="/task/{html.escape(task_id)}">'
            f"back to {html.escape(task_id)}</a></p>",
        )

    rows = _read_jsonl(stream_p)
    full_rows = _read_jsonl(messages_p)
    full_by_seq = {int(r.get("seq", -1)): r for r in full_rows
                   if isinstance(r.get("seq"), int)}
    next_seq = (max((int(r.get("seq", -1)) for r in rows), default=-1) + 1)

    terminal = _terminal_event_for_run(cfg, compact_ts, task_id)
    in_flight = terminal is None

    # Verdict / liveness banner
    if in_flight:
        live_banner = (
            '<div class="live-banner in-flight">'
            '<span class="pulse"></span>in-flight — polling every 3s; '
            'page will stop refreshing once a terminal event lands.'
            "</div>"
        )
        verdict_html = ""
    else:
        live_banner = (
            '<div class="live-banner">terminal event received '
            f'at <code>{html.escape(str(terminal.get("ts") or ""))}</code>; '
            "live polling stopped.</div>"
        )
        verdict_html = _render_run_verdict(terminal)

    # TB-158: when the terminal verdict is a verification fail, surface a
    # block at the top of the page calling out which bullets failed and
    # the judge's notes. Operators arriving from a `task_complete` link
    # see WHY immediately without scrolling through the SDK stream.
    verif_summary_html = ""
    if not in_flight and _is_verification_fail_terminal(terminal):
        vf_event = _latest_verification_failed_for_task(
            cfg, task_id, run_ts_compact=compact_ts,
        )
        if vf_event is not None:
            verif_summary_html = _verification_summary_block(vf_event)

    # Prompt block (collapsed by default — full prompts are long)
    prompt_html = ""
    if prompt_p.exists():
        try:
            prompt_text = prompt_p.read_text()
            prompt_html = (
                "<h2>prompt</h2>"
                f'<details><summary>show full prompt '
                f'({len(prompt_text):,} chars)</summary>'
                f"<pre>{html.escape(prompt_text)}</pre></details>"
            )
        except OSError as e:
            prompt_html = f'<p class="meta">(prompt unreadable: {html.escape(str(e))})</p>'

    rows_html = _render_run_rows_html(rows, full_by_seq)
    # TB-157: usage / token / cost totals footer. Empty string when no
    # row carries `usage` (pre-TB-157 runs already on disk).
    usage_footer = _render_run_usage_footer(rows)

    # Auto-refresh script: only emitted when in-flight. Polls the JSON
    # sub-endpoint with `since=<next_seq>`, appends new rows, and re-checks
    # `in_flight` each tick — flips to "stopped" and tears down the timer
    # the first time the daemon writes a terminal event for this task.
    script = ""
    if in_flight:
        script = _render_live_refresh_script(run_id, next_seq)

    body = (
        f"<h1>task-run <code>{html.escape(run_id)}</code></h1>"
        f'<div class="meta">'
        f'task: <a href="/task/{html.escape(task_id)}">{html.escape(task_id)}</a>'
        f' · started: {html.escape(compact_ts)}'
        f' · stream: {len(rows)} rows · messages: {len(full_rows)} bodies'
        f"</div>"
        f"{live_banner}"
        f"{verdict_html}"
        f"{verif_summary_html}"
        f"{prompt_html}"
        f"<h2>stream</h2>"
        f'<table id="stream-table"><thead>'
        "<tr><th>seq</th><th>type</th><th>body</th></tr>"
        f'</thead><tbody id="stream-body">{rows_html}</tbody></table>'
        f"{usage_footer}"
        f"{script}"
    )
    return _layout(f"run {run_id}", body)


def _render_run_verdict(terminal: dict) -> str:
    """Inline banner showing the run's final status."""
    typ = terminal.get("type") or "?"
    status = str(terminal.get("status") or "")
    cls = "unknown"
    if typ == "task_complete" and status == "complete":
        cls = "success"
    elif typ in _TERMINAL_RUN_EVENT_TYPES:
        cls = "failure"
    bits = [f"<strong>{html.escape(typ)}</strong>"]
    if status:
        bits.append(f"status=<code>{html.escape(status)}</code>")
    commit = str(terminal.get("commit") or "")
    if commit:
        bits.append(f"commit=<code>{html.escape(commit[:12])}</code>")
    summary = str(terminal.get("summary") or "")
    summary_html = (
        f'<div class="meta" style="margin-top:0.3rem">'
        f"{html.escape(summary)}</div>"
        if summary else ""
    )
    return (
        f'<div class="verdict {cls}">'
        + " · ".join(bits)
        + summary_html
        + "</div>"
    )


def _render_live_refresh_script(run_id: str, next_seq: int) -> str:
    """Tiny vanilla-JS poller: 3s `fetch` → append new rows → stop on terminal.

    Pure stdlib HTML; no framework. The endpoint contract is documented in
    `_render_task_run_stream_json`. We escape the run_id into the JSON payload
    via `json.dumps` so a hostile filename couldn't break out of the string
    literal (defense-in-depth — `_render_task_run` already path-checks).
    """
    rid_js = json.dumps(run_id)
    return f"""
<script>
(function() {{
  var runId = {rid_js};
  var since = {int(next_seq)};
  var tbody = document.getElementById('stream-body');
  var banner = document.querySelector('.live-banner');
  var timer = null;
  function appendRow(r) {{
    var tr = document.createElement('tr');
    tr.className = r.css_class || '';
    tr.setAttribute('data-seq', String(r.seq));
    tr.innerHTML = '<td class="ts">#' + r.seq + '</td>'
                 + '<td class="type">' + r.label + '</td>'
                 + '<td>' + r.body_html + '</td>';
    tbody.appendChild(tr);
  }}
  function tick() {{
    fetch('/task-run/' + encodeURIComponent(runId) + '/stream.json?since=' + since)
      .then(function(r) {{ return r.json(); }})
      .then(function(j) {{
        if (j.rows && j.rows.length) {{
          j.rows.forEach(appendRow);
          since = j.next_since;
        }}
        if (!j.in_flight) {{
          if (timer) {{ clearInterval(timer); timer = null; }}
          if (banner) {{
            banner.className = 'live-banner';
            banner.textContent = 'terminal event received; live polling stopped — refresh for verdict.';
          }}
        }}
      }})
      .catch(function(e) {{ /* transient — next tick will retry */ }});
  }}
  timer = setInterval(tick, 3000);
}})();
</script>
"""


def _render_task_run_stream_json(
    cfg: Config, run_id: str, since: int
) -> tuple[int, bytes]:
    """JSON sub-endpoint feeding the live detail page's auto-refresh.

    Contract:
      Request:  GET /task-run/<run-id>/stream.json?since=<int>
      Response: 200 application/json
        {
          "run_id":     "<echo>",
          "in_flight":  true|false,
          "terminal":   null | {ts, type, status, commit, ...},
          "rows":       [{seq, css_class, label, body_html}, ...],
          "next_since": <max_seq + 1>,
        }
      Errors: 400 on invalid run-id; 404 if stream.jsonl missing on disk.

    Returning HTML fragments (`body_html`) rather than JSON-typed row data
    keeps the JS dumb — a tiny `appendChild` loop with no client-side
    rendering logic to maintain.
    """
    safe = Path(run_id).name
    if safe != run_id or not _RUN_ID_RE.match(run_id):
        return 400, json.dumps({"error": "invalid run-id"}).encode()
    m = _RUN_ID_RE.match(run_id)
    assert m
    compact_ts, task_id = m.group(1), m.group(2)
    d = _debug_dir(cfg)
    stream_p = d / f"{run_id}.stream.jsonl"
    messages_p = d / f"{run_id}.messages.jsonl"
    if not stream_p.exists():
        return 404, json.dumps({"error": "stream.jsonl missing"}).encode()

    new_rows = _read_jsonl(stream_p, since=since)
    full_rows = _read_jsonl(messages_p, since=since)
    full_by_seq = {int(r.get("seq", -1)): r for r in full_rows
                   if isinstance(r.get("seq"), int)}
    rendered = []
    max_seq = since - 1
    for r in new_rows:
        seq = r.get("seq")
        if not isinstance(seq, int):
            continue
        max_seq = max(max_seq, seq)
        cls, label = _classify_row(r)
        body_html = _row_summary_html(r) + _row_full_body_html(full_by_seq.get(seq))
        rendered.append({
            "seq": seq,
            "css_class": cls,
            "label": label,
            "body_html": body_html,
        })

    terminal = _terminal_event_for_run(cfg, compact_ts, task_id)
    payload = {
        "run_id": run_id,
        "in_flight": terminal is None,
        "terminal": terminal,
        "rows": rendered,
        "next_since": max_seq + 1,
    }
    return 200, json.dumps(payload, default=str).encode()


# ------------- pipelines / insights / ideation_state / commits -------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _render_pipelines(cfg: Config) -> str:
    """Latest 50 pipeline_start events with liveness + log-size + tail link.

    Discovery via events.jsonl (not directory scan) so we see the command,
    validation task, and started_at — the directory only has logs.
    """
    evts = ev_mod.tail(cfg.events_file, n=20000)
    pipes = [e for e in evts if e.get("type") == "pipeline_start"]
    pipes.reverse()
    pipes = pipes[:50]
    if not pipes:
        body = "<h1>pipelines</h1><p><em>no pipeline_start events on file</em></p>"
        return _layout("pipelines", body)

    rows = []
    for e in pipes:
        pid = e.get("pid")
        alive = isinstance(pid, int) and _pid_alive(pid)
        log_path = e.get("log", "")
        log_size = ""
        log_mtime = ""
        if log_path:
            p = Path(log_path)
            if p.exists():
                st = p.stat()
                log_size = f"{st.st_size:,} B"
                import datetime as _dt
                log_mtime = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = '<span class="running">alive</span>' if alive else '<span class="meta">dead/exited</span>'
        validation = e.get("validation", "")
        validation_html = (
            f'<a href="/task/{html.escape(validation)}">{html.escape(validation)}</a>'
            if validation else "—"
        )
        cmd = e.get("command", "")
        rows.append(
            f"<tr>"
            f'<td class="ts">{html.escape(e.get("ts",""))}</td>'
            f'<td class="type">{html.escape(str(e.get("name","?")))}</td>'
            f'<td class="meta">{pid if pid is not None else "?"} ({status})</td>'
            f'<td>{validation_html}</td>'
            f'<td><span class="meta">{html.escape(log_size)} · {html.escape(log_mtime)}</span><br>'
            f'<span class="meta">{html.escape(log_path)}</span></td>'
            f'<td><pre style="margin:0;white-space:pre-wrap">{html.escape(cmd)}</pre></td>'
            f"</tr>"
        )
    table = (
        "<table><thead>"
        "<tr><th>started</th><th>name</th><th>pid</th><th>validation</th>"
        "<th>log</th><th>command</th></tr>"
        "</thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    body = f"<h1>pipelines <span class=\"meta\">— {len(pipes)} most recent</span></h1>{table}"
    return _layout("pipelines", body)


def _render_insights(cfg: Config) -> str:
    dir_ = ins_mod.insights_dir(cfg)
    if not dir_.exists():
        return _layout(
            "insights",
            "<h1>insights</h1><p><em>no insights dir</em></p>",
        )
    files = ins_mod._list_insight_files(dir_)
    if not files:
        return _layout(
            "insights",
            f"<h1>insights</h1>"
            f'<p class="meta">{html.escape(str(dir_.relative_to(cfg.project_root)))}/ — '
            f"empty</p>",
        )
    summaries = sorted(
        (ins_mod._summarize_file(f) for f in files),
        key=lambda s: s.updated or "",
        reverse=True,
    )
    rows = []
    for s in summaries:
        cite_str = ", ".join(s.cites) if s.cites else "—"
        date = (s.updated or "").split("T")[0] or "?"
        rows.append(
            f"<tr>"
            f'<td><a href="/insight/{html.escape(s.filename)}">{html.escape(s.filename)}</a></td>'
            f"<td>{html.escape(s.tldr or '(no tldr)')}</td>"
            f'<td class="meta">{html.escape(s.updated_by or "?")}</td>'
            f'<td class="ts">{html.escape(date)}</td>'
            f'<td class="meta">{html.escape(cite_str)}</td>'
            f"</tr>"
        )
    table = (
        "<table><thead>"
        "<tr><th>file</th><th>tldr</th><th>by</th><th>updated</th><th>cites</th></tr>"
        "</thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    body = (
        f"<h1>insights <span class=\"meta\">— {len(summaries)} files</span></h1>"
        f'<p class="meta">{html.escape(str(dir_.relative_to(cfg.project_root)))}/</p>'
        f"{table}"
    )
    return _layout("insights", body)


def _render_insight(cfg: Config, name: str) -> str:
    # Defend against path traversal — only basename, must be under insights dir.
    safe = Path(name).name
    if safe != name:
        return _layout("insight", "<p>invalid name</p>")
    path = ins_mod.insights_dir(cfg) / safe
    if not path.is_file():
        return _layout(
            f"insight {name}",
            f"<h1>{html.escape(safe)}</h1>"
            f'<p>not found. <a href="/insights">all insights</a></p>',
        )
    text = path.read_text()
    body = (
        f"<h1>{html.escape(safe)}</h1>"
        f'<p class="meta">{html.escape(str(path.relative_to(cfg.project_root)))}</p>'
        f"<pre>{html.escape(text)}</pre>"
    )
    return _layout(safe, body)


def _render_ideation_state(cfg: Config) -> str:
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    # Most recent ideation_complete summary — agent's per-cycle wrap-up.
    evts = ev_mod.tail(cfg.events_file, n=2000)
    last_complete = None
    for e in reversed(evts):
        if e.get("type") == "ideation_complete":
            last_complete = e
            break
    last_updated = None
    for e in reversed(evts):
        if e.get("type") == "ideation_state_updated":
            last_updated = e
            break
    summary_html = ""
    if last_complete:
        summary_html = (
            f"<h2>last ideation_complete summary "
            f"<span class=\"meta\">— {html.escape(last_complete.get('ts',''))}</span></h2>"
            f"<pre>{html.escape(str(last_complete.get('summary','(no summary)')))}</pre>"
        )
    if last_updated:
        summary_html += (
            f'<p class="meta">last ideation_state_updated: '
            f'{html.escape(last_updated.get("ts",""))} '
            f'({last_updated.get("bytes","?")} bytes)</p>'
        )

    if not path.exists():
        body = (
            "<h1>ideation state</h1>"
            f'<p class="meta">{html.escape(str(path.relative_to(cfg.project_root)))} '
            f"— not yet written</p>{summary_html}"
        )
        return _layout("ideation_state", body)
    text = path.read_text()
    body = (
        "<h1>ideation state</h1>"
        f'<p class="meta">{html.escape(str(path.relative_to(cfg.project_root)))}'
        f" ({len(text):,} chars)</p>"
        f"{summary_html}"
        f"<h2>full assessment</h2>"
        f"<pre>{html.escape(text)}</pre>"
    )
    return _layout("ideation_state", body)


_TB_PREFIX_RE = re.compile(r"^(TB-\d+)[: ]")


def _render_commits(cfg: Config) -> str:
    """`git log --oneline -50` with TB-N subjects linked to /task/<id>.

    Read-only: shells out to git rather than touching .git internals so the
    output matches what an operator would see at the terminal.
    """
    if not (cfg.project_root / ".git").exists():
        return _layout("commits", "<h1>commits</h1><p><em>not a git repo</em></p>")
    try:
        out = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(cfg.project_root),
             "log", "--oneline", "-50"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _layout("commits", "<h1>commits</h1><p>git log timed out</p>")
    if out.returncode != 0:
        return _layout(
            "commits",
            f"<h1>commits</h1><p>git log failed</p>"
            f"<pre>{html.escape(out.stderr)}</pre>",
        )
    rows = []
    for line in out.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        subject_html = html.escape(subject)
        m = _TB_PREFIX_RE.match(subject)
        if m:
            tb = m.group(1)
            subject_html = subject_html.replace(
                tb, f'<a href="/task/{tb}">{tb}</a>', 1,
            )
        rows.append(
            f'<tr><td class="type">{html.escape(sha)}</td>'
            f'<td>{subject_html}</td></tr>'
        )
    table = (
        "<table><thead><tr><th>sha</th><th>subject</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table>'
    )
    body = f"<h1>commits <span class=\"meta\">— last 50</span></h1>{table}"
    return _layout("commits", body)


# ------------- HTTP handler -------------


class _Handler(http.server.BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802
        try:
            url = urlsplit(self.path)
            qs = parse_qs(url.query)
            path = url.path or "/"
            if path == "/":
                body = _render_home(self.cfg)
            elif path == "/events":
                typ = qs.get("type", [None])[0]
                try:
                    n = int(qs.get("n", ["200"])[0])
                except ValueError:
                    n = 200
                n = max(1, min(n, 5000))
                # TB-157: ?show=tokens renders an extra column per row
                # surfacing usage / cost for every event that carries it
                # (chiefly judge_call rows today, and any future
                # event types that grow a usage payload).
                show_tokens = (
                    qs.get("show", [""])[0] == "tokens"
                )
                body = _render_events(
                    self.cfg, typ=typ, n=n, show_tokens=show_tokens,
                )
            elif path == "/tasks":
                # TB-121: ?filter=pending-review narrows to ideation
                # proposals awaiting operator approval.
                f_kind = qs.get("filter", [None])[0]
                body = _render_tasks(self.cfg, filter_kind=f_kind)
            elif path.startswith("/task/"):
                tb_id = path[len("/task/"):]
                body = _render_task(self.cfg, tb_id)
            elif path.startswith("/task-run/"):
                rest = path[len("/task-run/"):]
                # Two routes share the same prefix:
                #   /task-run/<run-id>            → HTML page
                #   /task-run/<run-id>/stream.json → JSON poll endpoint
                if rest.endswith("/stream.json"):
                    rid = rest[: -len("/stream.json")]
                    try:
                        since = int(qs.get("since", ["0"])[0])
                    except ValueError:
                        since = 0
                    status, data = _render_task_run_stream_json(
                        self.cfg, rid, max(0, since)
                    )
                    self.send_response(status)
                    self.send_header(
                        "Content-Type", "application/json; charset=utf-8"
                    )
                    self.send_header("Content-Length", str(len(data)))
                    # Live polling endpoint — disable caching so a stale
                    # 304 doesn't strand the operator on an empty page
                    # while the daemon writes new rows.
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                body = _render_task_run(self.cfg, rest)
            elif path == "/pipelines":
                body = _render_pipelines(self.cfg)
            elif path == "/insights":
                body = _render_insights(self.cfg)
            elif path.startswith("/insight/"):
                name = path[len("/insight/"):]
                body = _render_insight(self.cfg, name)
            elif path == "/ideation_state":
                body = _render_ideation_state(self.cfg)
            elif path == "/commits":
                body = _render_commits(self.cfg)
            elif path == "/usage":
                # TB-181: token/cost dashboard. URL is the only config
                # surface; out-of-range / missing values fall back to
                # the defaults inside `_render_usage`.
                window = qs.get("window", [None])[0]
                stack = qs.get("stack", [None])[0]
                body = _render_usage(
                    self.cfg, window=window, stack_by=stack,
                )
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"500: {type(e).__name__}: {e}".encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Quiet by default — the daemon's events.jsonl is the audit trail,
        # not stdout from a debug HTTP server.
        return


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    """`ThreadingTCPServer` with `allow_reuse_address` flipped on by default.

    Without this, restarting the daemon (or switching from `ap2 web` to
    daemon-spawned mode) trips a `OSError: [Errno 48] Address already in
    use` on the port for ~60s while the kernel waits out TIME_WAIT.
    Daemon threads on the request handlers so a stuck request can't keep
    `srv.shutdown()` blocked when the operator wants out.
    """

    allow_reuse_address = True
    daemon_threads = True


def _bind_with_enumeration(
    host: str, start_port: int, max_attempts: int,
) -> tuple[socket.socket, int]:
    """Bind a TCP listening socket on `host`, walking forward from `start_port`.

    TB-155: silently retry the next port (start_port+1, start_port+2, ..., up
    to `start_port + max_attempts - 1`) when the configured `start_port` is
    already bound — typically by a stale daemon, an `ap2 web` standalone, or
    another project's daemon on the same machine. Returns the bound socket
    and the actually-bound port; callers include the resolved port in their
    `web_start` event payload so post-mortem can pair "requested 8729, bound
    8730" with the conflict.

    `EADDRINUSE` is the only error treated as "try the next port" — any other
    `OSError` (permissions, bad host, etc.) propagates immediately because
    walking forward wouldn't help. After exhausting `max_attempts`, raises a
    single `OSError(EADDRINUSE, ...)` whose message names the range tried so
    the operator's hunt for the offending pid is one log line away.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1 (got {max_attempts})")
    last_err: OSError | None = None
    for offset in range(max_attempts):
        port = start_port + offset
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return sock, port
        except OSError as e:
            sock.close()
            if e.errno != errno.EADDRINUSE:
                raise
            last_err = e
    end_port = start_port + max_attempts - 1
    raise OSError(
        errno.EADDRINUSE,
        f"no free port in range {start_port}..{end_port} "
        f"(tried {max_attempts}); investigate with "
        f"`lsof -iTCP:{start_port} -sTCP:LISTEN`"
        + (f" — last EADDRINUSE: {last_err}" if last_err else ""),
    )


def _build_server(
    cfg: Config,
    host: str,
    start_port: int,
    max_attempts: int = DEFAULT_WEB_PORT_MAX_ATTEMPTS,
) -> tuple[_ThreadingTCPServer, int]:
    """Bind the read-only HTTP server with TB-155 port enumeration.

    Returns `(srv, bound_port)` so callers can log the actually-bound port
    rather than the one they asked for. The HTTP server uses our pre-bound
    socket (`bind_and_activate=False` skips TCPServer's own bind) so we
    don't double-bind and the enumeration result is authoritative.
    """
    sock, bound_port = _bind_with_enumeration(host, start_port, max_attempts)
    handler_cls = type("Handler", (_Handler,), {"cfg": cfg})
    srv = _ThreadingTCPServer(
        (host, bound_port), handler_cls, bind_and_activate=False,
    )
    # Replace the unbound socket TCPServer just allocated with our pre-bound
    # one, then call `server_activate()` so the kernel starts queuing
    # connections. `socketserver` keeps a reference to `srv.socket` for
    # `server_close()`, so swapping it here is the supported path.
    srv.socket.close()
    srv.socket = sock
    srv.server_activate()
    return srv, bound_port


def serve(
    cfg: Config,
    host: str = "127.0.0.1",
    port: int = DEFAULT_STANDALONE_WEB_PORT,
    *,
    max_attempts: int = DEFAULT_WEB_PORT_MAX_ATTEMPTS,
) -> None:
    """Start the read-only web UI. Blocks until SIGINT.

    Default bind is 127.0.0.1 deliberately — there's no auth and the page
    surfaces full event payloads (briefing text, prompt dump paths,
    Mattermost message bodies, etc.) that should never leave the box.

    TB-155: when `port` is already bound, walks forward up to `max_attempts`
    times before giving up. The "bound on" line printed below reflects the
    resolved port so the operator can copy/paste the URL even after a
    silent enumeration. `port` keeps its argparse-friendly name (so the CLI
    flag stays `--port`) but functions as the ENUMERATION START.
    """
    srv, bound_port = _build_server(cfg, host, port, max_attempts=max_attempts)
    with srv:
        if bound_port != port:
            print(
                f"ap2 web: port {port} busy; bound to {bound_port} instead "
                f"(range {port}..{port + max_attempts - 1})"
            )
        print(
            f"ap2 web: http://{host}:{bound_port}/  (project={cfg.project_root})"
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nap2 web: stopped")


async def serve_async(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    start_port: int = DEFAULT_DAEMON_WEB_PORT,
    max_attempts: int = DEFAULT_WEB_PORT_MAX_ATTEMPTS,
    on_bind: "Callable[[str, int], None] | None" = None,
    port: int | None = None,
) -> None:
    """Run the read-only web UI as an awaitable, cooperatively cancellable.

    Companion to the blocking `serve()` (which `ap2 web` still uses for the
    standalone case). Used by the daemon's `main_loop` so `ap2 start`
    brings up both daemon + web in one process — no second terminal, no
    risk of leaving the UI pointed at a stale events.jsonl after the
    daemon was restarted (TB-130).

    Lifecycle:
      - Bind the server on the calling event loop's thread (with TB-155
        port enumeration starting at `start_port`), then run
        `serve_forever` in a background daemon thread (the stdlib HTTP
        handler is sync; `serve_forever` blocks).
      - If provided, fire `on_bind(host, bound_port)` synchronously before
        parking — that's how `_web_loop_for_daemon` learns the resolved
        port for its `web_start` event payload.
      - Block this coroutine indefinitely on `Event.wait()`. Cancellation
        (the daemon's teardown path) lands as `CancelledError`, which
        triggers `srv.shutdown()` to wake `serve_forever`.
      - Re-raises the bind `OSError` so the caller can decide whether
        `EADDRINUSE` means "already running" (skip) or "real error" (log).

    `port=` is accepted as a backwards-compatible alias for `start_port=`
    so callers (and tests) written before TB-155 keep working.
    """
    if port is not None:
        # Pre-TB-155 callers passed `port=`; treat it as `start_port=` so
        # the auto-enumeration shape is opt-in via the new keyword without
        # silently breaking existing kwargs.
        start_port = port
    srv, _bound_port = _build_server(
        cfg, host, start_port, max_attempts=max_attempts,
    )
    if on_bind is not None:
        on_bind(host, _bound_port)
    server_thread = threading.Thread(
        target=srv.serve_forever, name="ap2-web", daemon=True,
    )
    server_thread.start()
    try:
        # `asyncio.Event().wait()` is the textbook "park forever, wake on
        # cancel" pattern — cleaner than a poll loop, and unaffected by
        # `RUNNING` (which the daemon flips on signals; we get
        # `CancelledError` from the parent's `task.cancel()` call instead).
        await asyncio.Event().wait()
    finally:
        # `shutdown()` is idempotent and safe from any thread; it sets the
        # internal flag, then waits for the request loop to notice on its
        # next poll. `server_close()` releases the listening socket so a
        # subsequent restart can bind. The thread is `daemon=True` so a
        # stuck handler can't keep the process alive.
        srv.shutdown()
        srv.server_close()
        server_thread.join(timeout=5)
