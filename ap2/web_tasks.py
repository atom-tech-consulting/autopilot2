"""Live task-run detail page + stream JSON endpoint (TB-129).

TB-265: extracted from `ap2/web.py` as part of the route-group split.

Pages owned by this module:
  - `/task-run/<run-id>`               — `_render_task_run`
  - `/task-run/<run-id>/stream.json`   — `_render_task_run_stream_json`

The TB-129 auto-refresh JS poller stays paired with these endpoints —
the stream JSON contract documented in `_render_task_run_stream_json`
is consumed only by the inline script emitted by
`_render_live_refresh_script`.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .config import Config
from .web_chrome import (
    _RUN_ID_RE,
    _TERMINAL_RUN_EVENT_TYPES,
    _debug_dir,
    _is_verification_fail_terminal,
    _latest_verification_failed_for_task,
    _layout,
    _read_jsonl,
    _terminal_event_for_run,
    _verification_summary_block,
)
from .web_home import _WebRouter


router = _WebRouter()
router.add("/task-run/{run_id}")
router.add("/task-run/{run_id}/stream.json")


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
