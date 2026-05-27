"""Attention page (`/attention`) — pull-surface for current attention
conditions (TB-296).

Sibling to the status-report cron's push-surface render of
`## Attention needed` bullets (TB-282 + the five condition detectors
shipped in TB-282 / TB-287 / TB-288 / TB-289 / TB-290): both consume
the SAME `attention.detect_attention_conditions(cfg)` entrypoint so
the push and pull surfaces can never disagree about what's currently
active. The status-report cron forwards its render verbatim every 2h;
this page renders on every request so a walk-away operator returning
mid-cycle doesn't have to wait for the next post (or `ap2 status`
text-pull) to see currently-active conditions.

Closes TB-282's `## Out of scope` L123-125 ("web `/attention` page —
operator-named in 2026-05-27T06:33:52Z rewind reason") on the
pull-surface side. The status-report cron remains the push surface
and the home-page automation cards remain the per-axis state cards
(focus, ideation, auto-approve) — distinct visual roles, one shared
detector layer.

Bullet shape mirrors `status_report.render_attention_section` (TB-282
status_report.py L914-998): warn-glyph ⚠, bold TB-N when
`extras['task']` is set, em-dash, detector-supplied `summary`. The
zero-conditions branch renders an explicit empty-state ("No
attention conditions currently active.") rather than a blank page so
an operator can distinguish "page loaded, nothing wrong" from "page
broken".

Out of scope (deferred until a concrete consumer surfaces):
  - JSON sub-endpoint `/attention.json` for external monitoring.
  - Auto-refresh / polling on the page.
  - `attention_cleared` event class.
  - Modifying the detector module itself.
"""
from __future__ import annotations

import html

from . import attention as _attention
from .config import Config
from .web_chrome import _layout
from .web_home import _WebRouter


router = _WebRouter()
router.add("/attention")


_EMPTY_STATE = "No attention conditions currently active."


def _render_attention(cfg: Config) -> str:
    """Render `/attention` — operator-legible per-condition bullets, or
    an explicit empty-state when zero detectors fire.

    Calls `attention.detect_attention_conditions(cfg)` (the SAME
    detector entrypoint the status-report cron's
    `render_attention_section` consumes) so the push and pull
    surfaces stay in lockstep — a detector that fires on the
    cron-post side fires here too, with the identical
    operator-legible `summary` text.

    Bullet shape (per condition):

        ⚠ **TB-N** — <detector summary>

    The TB-N prefix is omitted when the detector's `extras['task']`
    is absent (singleton detectors: `validator_judge_noisy`,
    `auto_approve_paused`, `cost_cap_approach`); those render as a
    bare `⚠ — <summary>`. The em-dash + bold-TB-N + warn-glyph
    pattern matches the status-report renderer's output verbatim so
    the operator's muscle-memory ("orange triangle → attention
    bullet") carries across both surfaces.

    Defensive fallback: a detector exception is swallowed and
    rendered as a tinted notice — the page must never 500 just
    because one detector errored. Mirrors the
    `render_attention_section` swallow-on-error contract.
    """
    try:
        conditions = _attention.detect_attention_conditions(cfg)
    except Exception as e:  # noqa: BLE001 — never break the page
        body = (
            "<h1>attention</h1>"
            '<div class="verif-summary">'
            '<span class="counter">'
            f"detector error: {html.escape(type(e).__name__)}: "
            f"{html.escape(str(e))}"
            "</span></div>"
        )
        return _layout("attention", body)

    if not conditions:
        body = (
            "<h1>attention</h1>"
            f'<p><em>{html.escape(_EMPTY_STATE)}</em></p>'
        )
        return _layout("attention", body)

    items: list[str] = []
    for cond in conditions:
        task_id = (cond.extras.get("task") or "").strip()
        # Mirror `render_attention_section`'s shape: bold TB-N + em-dash
        # when a per-task detector populates `extras['task']`; bare
        # `⚠ <summary>` otherwise (singleton detectors). The detector's
        # pre-rendered `summary` is operator-legible by construction
        # (TB-282 contract) — render it as-is after escaping.
        if task_id:
            items.append(
                "<li>"
                '<span class="att-glyph">⚠</span> '
                f'<strong>{html.escape(task_id)}</strong> — '
                f"{html.escape(cond.summary)}"
                "</li>"
            )
        else:
            items.append(
                "<li>"
                '<span class="att-glyph">⚠</span> '
                f"{html.escape(cond.summary)}"
                "</li>"
            )
    plural = "" if len(conditions) == 1 else "s"
    body = (
        "<h1>attention "
        f'<span class="meta">— {len(conditions)} condition{plural} '
        "active</span></h1>"
        f'<ul class="attention-conditions">{"".join(items)}</ul>'
    )
    return _layout("attention", body)
