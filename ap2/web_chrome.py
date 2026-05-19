"""Shared HTML chrome + helpers for the ap2 web UI route-group sibling modules.

TB-265 split the original `ap2/web.py` (179KB) into route-group siblings:
`web_home`, `web_events`, `web_tasks`, `web_stats`, `web_insights`,
`web_usage`. `web.py` retains the FastAPI-style app construction + HTTP
dispatcher; this module holds the chrome that more than one sibling needs:

  - `_CSS`, `_layout` — page shell.
  - `_row_class`, `_event_extra`, `_events_table` and friends — event
    table rendering. Consumed by `/`, `/events`, `/task/<id>`,
    `/pipelines`, etc.
  - `_read_jsonl`, `_is_alive`, debug-dir helpers — disk-state probes
    used by the home/events/tasks renderers.
  - `_tasks_list`, `_is_pending_review` — board task rendering shared
    by `/` and `/tasks`.
  - `_verification_failed_row_summary`, `_verification_summary_block`,
    `_is_verification_fail_terminal`, `_latest_verification_failed_for_task`
    — TB-158 shared rendering between `/events` and `/task-run/<id>`.

This module imports only from stdlib + sibling `ap2.*` plumbing
(`events`, `diagnose`, `board`, `config`). It does NOT import from any
sibling `web_*.py` — those depend on this module, not the other way.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
from pathlib import Path

from . import diagnose, events as ev_mod
from .board import Board
from .config import Config


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
  /* TB-256: knob OFF + 24h activity present (typically TB-243
     validator-judge fail / timeout counts) — grey-tinted so the
     operator visually distinguishes "card visible but auto-approve
     is off / informational" from the green-flag healthy state. Sits
     between the green is-healthy and red is-paused tints in
     urgency. */
  .automation-status.is-disabled-but-active { background: #f4f4f4;
                                              border-left-color: #888 }
  .automation-status .as-header { font-weight: 600; margin-right: 0.4rem }
  .automation-status.is-healthy .as-header { color: #1a6f2a }
  .automation-status.is-paused  .as-header { color: #8a1818 }
  .automation-status.is-disabled-but-active .as-header { color: #555 }
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
  /* TB-243: warn-tint for the "Validator judge (24h)" row when
     (fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD (default 5).
     Uses the same amber palette as the cost-dashboard's warning-tier
     row class so the operator's color-memory ("orange = soft
     warning") carries over. Below-threshold rows keep the default
     `.as-meta` palette so a single transient blip doesn't tint the
     card. */
  .automation-status .as-vj-noisy { color: #b87000; font-weight: 600 }
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
        '<a href="/usage">usage</a> '
        '<a href="/stats">stats</a></nav>'
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
