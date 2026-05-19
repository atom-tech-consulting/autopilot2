"""Insights pages (`/insights` + `/insight/<name>`).

TB-265: extracted from `ap2/web.py` as part of the route-group split.

Pages owned by this module:
  - `/insights`           — `_render_insights`
  - `/insight/<name>`     — `_render_insight`
"""
from __future__ import annotations

import html
from pathlib import Path

from . import insights as ins_mod
from .config import Config
from .web_chrome import _layout
from .web_home import _WebRouter


router = _WebRouter()
router.add("/insights")
router.add("/insight/{name}")


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
