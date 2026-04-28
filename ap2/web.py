"""Local read-only web UI for ap2 daemon state.

Closes TB-93 (Frozen "console tool for human review") in web form. Pure
stdlib (`http.server`), no JS framework, no auth. Bound to 127.0.0.1 by
default; only the operator on the box should be reading it.

Read-only by design — every mutation still goes through the `ap2` CLI or
custom MCP tools. The web UI is a window onto state, not a control panel.

Pages:
  /            overview: daemon status, board counts, last 30 events
  /events      full event log, filterable by ?type=X&n=N (default 200)
  /tasks       all tasks grouped by section
  /task/<TB-N> one task: briefing + related events + raw line
"""
from __future__ import annotations

import html
import http.server
import json
import os
import socketserver
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import diagnose, events as ev_mod
from .board import Board
from .config import Config


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
  tr.lifecycle td.type { color: #2a8 }
  .ts { color: #888; font-family: ui-monospace, monospace; font-size: 12px; white-space: nowrap }
  .type { font-family: ui-monospace, monospace; font-weight: 500 }
  .summary { color: #444; word-break: break-word }
  pre { background: #f5f5f5; padding: 0.6rem; border-radius: 4px; overflow-x: auto;
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
        '<a href="/tasks">tasks</a></nav>'
        f"{body}"
        "</body></html>"
    )


def _row_class(typ: str) -> str:
    if typ in diagnose.FAILURE_EVENT_TYPES:
        return "failure"
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


def _events_table(evts: list[dict]) -> str:
    if not evts:
        return "<p><em>no events</em></p>"
    rows = []
    for i, e in enumerate(evts):
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        cls = _row_class(typ)
        full_json = json.dumps(e, indent=2, default=str)
        extra = _event_extra(e)
        rows.append(
            f'<tr class="{cls}">'
            f'<td class="ts">{html.escape(ts)}</td>'
            f'<td class="type">{html.escape(typ)}</td>'
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
              for s in ("Active", "Ready", "Backlog", "Complete", "Frozen")}
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
            for s in ("Active", "Ready", "Backlog", "Complete", "Frozen")
        )
        + "</div>"
        f'<h2>events <span class="meta">— last 30, newest first '
        f'(<a href="/events">all</a>)</span></h2>'
        f"{_events_table(evts)}"
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
             "verification_failed", "backlog_auto_promoted", "daemon_start"]
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
        f"{_events_table(evts)}"
    )
    return _layout("events", body)


def _render_tasks(cfg: Config) -> str:
    board = Board.load(cfg.tasks_file)
    sections_html = []
    for s, limit in (("Active", None), ("Ready", None), ("Backlog", None),
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

    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in task.tags)
    desc = f"<p>{html.escape(task.description)}</p>" if task.description else ""
    body = (
        f'<h1><span class="id">{html.escape(task.id)}</span> {html.escape(task.title)}</h1>'
        f'<div class="meta">section: <strong>{html.escape(task.section)}</strong>'
        f'{" — checked" if task.checked else ""}{tags}</div>'
        f"{desc}"
        f"{briefing_html}"
        f"<h2>related events <span class=\"meta\">— {len(related)} shown</span></h2>"
        f"{_events_table(related)}"
    )
    return _layout(task.id, body)


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


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 7820) -> None:
    """Start the read-only web UI. Blocks until SIGINT.

    Default bind is 127.0.0.1 deliberately — there's no auth and the page
    surfaces full event payloads (briefing text, prompt dump paths,
    Mattermost message bodies, etc.) that should never leave the box.
    """
    handler_cls = type("Handler", (_Handler,), {"cfg": cfg})
    with socketserver.TCPServer((host, port), handler_cls) as srv:
        srv.allow_reuse_address = True
        print(f"ap2 web: http://{host}:{port}/  (project={cfg.project_root})")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nap2 web: stopped")
