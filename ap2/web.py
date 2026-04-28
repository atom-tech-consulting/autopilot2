"""Local read-only web UI for ap2 daemon state.

Closes TB-93 (the "console tool for human review" backlog item) in web
form. Pure stdlib (`http.server`), no JS framework, no auth. Bound to
127.0.0.1 by default; only the operator on the box should be reading it.

Read-only by design — every mutation still goes through the `ap2` CLI or
custom MCP tools. The web UI is a window onto state, not a control panel.

Pages:
  /                  overview: daemon status, board counts, last 30 events
  /events            full event log, filterable by ?type=X&n=N (default 200)
  /tasks             all tasks grouped by section
  /task/<TB-N>       one task: briefing + related events + raw line
  /pipelines         in-flight + recent pipelines from pipeline_start events
  /insights          insights index — front matter summaries + links
  /insight/<name>    one insight file, full content
  /ideation_state    latest ideation_state.md assessment
  /commits           recent git log (subjects link to /task/TB-N when matched)
"""
from __future__ import annotations

import html
import http.server
import json
import os
import re
import socketserver
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import diagnose, events as ev_mod, insights as ins_mod
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
  tr.warning { background: #fffbea }
  tr.warning td.type { color: #b87000 }
  tr.lifecycle td.type { color: #2a8 }
  .ts { color: #888; font-family: ui-monospace, monospace; font-size: 12px; white-space: nowrap }
  .type { font-family: ui-monospace, monospace; font-weight: 500 }
  /* Tables fill the viewport and never horizontally scroll. Auto layout
     allocates column widths from content; `overflow-wrap: anywhere` on
     every cell wraps long unbroken strings (URLs, base64, json blobs)
     at any character so the cell stays within its share. `.ts` opts out
     so timestamps stay on one line. Combined with the `pre` rules below,
     no row pushes the page wider than its container. */
  td, th { overflow-wrap: anywhere; word-break: break-word }
  .ts { white-space: nowrap }
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
