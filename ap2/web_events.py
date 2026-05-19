"""Events / tasks / commits / pipelines / ideation_state route group.

TB-265: extracted from `ap2/web.py` as part of the route-group split.

Pages owned by this module:
  - `/events`               — `_render_events` (TB-148 / TB-157 / TB-158)
  - `/tasks`                — `_render_tasks`  (TB-121 pending-review filter)
  - `/task/<TB-N>`          — `_render_task`   + per-task `runs` section
  - `/pipelines`            — `_render_pipelines`
  - `/ideation_state`       — `_render_ideation_state`
  - `/commits`              — `_render_commits`
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
import subprocess
from pathlib import Path

from . import events as ev_mod
from .board import Board
from .config import Config
from .web_chrome import (
    _RUN_ID_RE,
    _events_table,
    _is_pending_review,
    _layout,
    _list_run_ids_for_task,
    _tasks_list,
    _terminal_event_for_run,
)
from .web_home import _WebRouter


router = _WebRouter()
router.add("/events")
router.add("/tasks")
router.add("/task/{task_id}")
router.add("/pipelines")
router.add("/ideation_state")
router.add("/commits")


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


# ------------- pipelines / ideation_state / commits -------------


def _pid_alive(pid: int) -> bool:
    import os
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
