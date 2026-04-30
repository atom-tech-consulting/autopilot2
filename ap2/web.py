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
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import html
import http.server
import json
import os
import re
import socketserver
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import diagnose, events as ev_mod, insights as ins_mod
from .board import Board
from .config import Config


# TB-130: when the daemon spawns the web UI as part of `ap2 start`, this is
# the default port. Standalone `ap2 web` keeps its historical default
# (7820) so operators who already have a tab pointed at the legacy URL
# don't have to rebookmark. Override either with `AP2_WEB_PORT`.
DEFAULT_DAEMON_WEB_PORT = 8729
DEFAULT_STANDALONE_WEB_PORT = 7820


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


def _read_pid(cfg: Config) -> int | None:
    if not cfg.pid_file.exists():
        return None
    try:
        return int(cfg.pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


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
        '<a href="/commits">commits</a></nav>'
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


def _row_class(typ: str) -> str:
    if typ in diagnose.FAILURE_EVENT_TYPES:
        return "failure"
    if typ in _WARNING_EVENT_TYPES:
        return "warning"
    if typ in {"task_start", "task_complete", "cron_start", "cron_complete",
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


def _events_table(evts: list[dict], *, cfg: Config | None = None) -> str:
    """Render an events table; pass `cfg` to enable per-row debug-run links.

    With `cfg`, each `task_start` row gets a small `→ live` link to its
    `/task-run/<run-id>` view if the debug files survive on disk (TB-129).
    Without, the table renders plain — used by callers that already render
    a header pulled from the same dataset.
    """
    if not evts:
        return "<p><em>no events</em></p>"
    rows = []
    for i, e in enumerate(evts):
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        cls = _row_class(typ)
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
        rows.append(
            f'<tr class="{cls}">'
            f'<td class="ts">{html.escape(ts)}</td>'
            f'<td class="type">{html.escape(typ)}{run_link}</td>'
            f'<td class="summary">{extra}'
            f'<details><summary>raw json</summary>'
            f'<pre>{html.escape(full_json)}</pre></details></td>'
            f"</tr>"
        )
    return (
        "<table><thead>"
        "<tr><th>ts</th><th>type</th><th>fields</th></tr>"
        "</thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _tasks_list(board: Board, section: str, *, limit: int | None = None) -> str:
    tasks = list(board.iter_tasks(section=section))
    if limit is not None:
        tasks = tasks[-limit:]
    if not tasks:
        return "<p><em>(empty)</em></p>"
    items = []
    for t in tasks:
        tags = "".join(f'<span class="tag">{html.escape(tg)}</span>' for tg in t.tags)
        desc = f' — <span class="meta">{html.escape(t.description)}</span>' if t.description else ""
        items.append(
            f'<li><a class="id" href="/task/{html.escape(t.id)}">{html.escape(t.id)}</a> '
            f"<strong>{html.escape(t.title)}</strong>{tags}{desc}</li>"
        )
    return f'<ul class="tasks">{"".join(items)}</ul>'


# ------------- page renderers -------------


def _render_home(cfg: Config) -> str:
    pid = _read_pid(cfg)
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
        f'<h2>events <span class="meta">— last 30, newest first '
        f'(<a href="/events">all</a>)</span></h2>'
        f"{_events_table(evts, cfg=cfg)}"
    )
    return _layout(cfg.project_root.name, body)


def _render_events(cfg: Config, *, typ: str | None, n: int) -> str:
    # Pull a generous tail and post-filter so type-filter pages always show n
    # matches even when the type is rare in the recent window.
    pull = max(n * 20, n) if typ else n
    evts = ev_mod.tail(cfg.events_file, n=pull)
    if typ:
        evts = [e for e in evts if e.get("type") == typ]
    evts = evts[-n:]
    evts.reverse()

    # Quick-filter buttons for the most common types.
    quick = ["task_complete", "task_error", "cron_complete", "cron_error",
             "ideation_empty_board", "ideation_complete", "ideation_error",
             "verification_failed", "verification_partial",
             "backlog_auto_promoted", "daemon_start"]
    filt = '<div class="filter">filter:'
    filt += f' <a href="/events?n={n}" class="{"on" if not typ else ""}">all</a>'
    for k in quick:
        cls = "on" if typ == k else ""
        filt += f' <a href="/events?type={k}&n={n}" class="{cls}">{k}</a>'
    filt += "</div>"

    body = (
        f"<h1>events <span class=\"meta\">"
        f"— {len(evts)} shown{', filter: ' + html.escape(typ) if typ else ''}</span></h1>"
        f"{filt}"
        f"{_events_table(evts, cfg=cfg)}"
    )
    return _layout("events", body)


def _render_tasks(cfg: Config) -> str:
    board = Board.load(cfg.tasks_file)
    sections_html = []
    for s, limit in (("Active", None), ("Ready", None), ("Backlog", None),
                     ("Pipeline Pending", None),
                     ("Complete", 30), ("Frozen", None)):
        label = f"{s} <span class=\"meta\">({sum(1 for _ in board.iter_tasks(section=s))} total)</span>"
        if limit is not None:
            label += f" <span class=\"meta\">— last {limit}</span>"
        sections_html.append(f"<h2>{label}</h2>{_tasks_list(board, s, limit=limit)}")
    body = "<h1>tasks</h1>" + "".join(sections_html)
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
        f"{prompt_html}"
        f"<h2>stream</h2>"
        f'<table id="stream-table"><thead>'
        "<tr><th>seq</th><th>type</th><th>body</th></tr>"
        f'</thead><tbody id="stream-body">{rows_html}</tbody></table>'
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
                body = _render_events(self.cfg, typ=typ, n=n)
            elif path == "/tasks":
                body = _render_tasks(self.cfg)
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


def _build_server(
    cfg: Config, host: str, port: int
) -> _ThreadingTCPServer:
    """Bind the read-only HTTP server. Subclass of TCPServer so `_Handler`'s
    base class assumptions still hold."""
    handler_cls = type("Handler", (_Handler,), {"cfg": cfg})
    return _ThreadingTCPServer((host, port), handler_cls)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = DEFAULT_STANDALONE_WEB_PORT) -> None:
    """Start the read-only web UI. Blocks until SIGINT.

    Default bind is 127.0.0.1 deliberately — there's no auth and the page
    surfaces full event payloads (briefing text, prompt dump paths,
    Mattermost message bodies, etc.) that should never leave the box.
    """
    with _build_server(cfg, host, port) as srv:
        print(f"ap2 web: http://{host}:{port}/  (project={cfg.project_root})")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nap2 web: stopped")


async def serve_async(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_DAEMON_WEB_PORT,
) -> None:
    """Run the read-only web UI as an awaitable, cooperatively cancellable.

    Companion to the blocking `serve()` (which `ap2 web` still uses for the
    standalone case). Used by the daemon's `main_loop` so `ap2 start`
    brings up both daemon + web in one process — no second terminal, no
    risk of leaving the UI pointed at a stale events.jsonl after the
    daemon was restarted (TB-130).

    Lifecycle:
      - Bind the server on the calling event loop's thread, then run
        `serve_forever` in a background daemon thread (the stdlib HTTP
        handler is sync; `serve_forever` blocks).
      - Block this coroutine indefinitely on a sleep loop. Cancellation
        (the daemon's teardown path) lands as `CancelledError`, which
        triggers `srv.shutdown()` to wake `serve_forever`.
      - Re-raises the bind `OSError` so the caller can decide whether
        `EADDRINUSE` means "already running" (skip) or "real error" (log).
    """
    srv = _build_server(cfg, host, port)
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
