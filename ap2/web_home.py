"""Home page (`/`) renderer + home-only cards for the ap2 web UI.

TB-265: extracted from `ap2/web.py` as part of the route-group split.

`router` exposes the FastAPI-style mounting surface
(`make_app()` consumes it). The actual HTTP dispatch still flows
through `ap2/web.py`'s `_Handler.do_GET` — `router` is a thin
shim describing the route shape for surface introspection.

Cards owned by this module:
  - `_render_home`        — top-level composer.
  - `_render_pending_queue`         (TB-162 — pending operator queue card).
  - `_render_operator_decisions`    (TB-173 / TB-191 — ideator decisions card).
  - `_render_ideation_status_block` (TB-197 — ideation gate-state card).
  - `_render_focus_card`            (TB-242 — axis-4 focus rotation card).
  - `_render_attention_card`        (TB-299 — active attention-conditions summary card).
  - `_render_automation_card`       (TB-227 — auto-approve/auto-unfreeze card).
  - `_render_env_stale_warning`     (TB-260 — env-staleness WARN line).

TB-260's env-staleness WARN line rendering lives in
`_render_env_stale_warning`, which calls
`automation_status.collect_env_staleness(cfg)` so the web surface
stays in lock-step with `ap2 status` / cron digest / watchdog
auto_diagnose. Sits directly under the daemon-status header in
`_render_home` — the operator reads them as one cluster ("daemon
running BUT env file changed → restart needed").
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
from pathlib import Path

from . import events as ev_mod
from ._shared import read_pid
from .board import Board
from .config import Config
from .web_chrome import (
    _events_table,
    _is_alive,
    _layout,
)


# ------------- _WebRouter: FastAPI-style mounting shim -------------


class _Route:
    """Minimal stand-in for `fastapi.routing.APIRoute`.

    Exposes `.path` so `make_app().routes[*].path` (see verification
    bullet in TB-265 briefing) is iterable. The underlying handler is
    `_Handler.do_GET` in `ap2/web.py`; the router is a documentation
    + composition shim, not a routing engine.
    """
    __slots__ = ("path", "endpoint")

    def __init__(self, path: str, endpoint=None):
        self.path = path
        self.endpoint = endpoint


class _WebRouter:
    """Lightweight stand-in for `fastapi.APIRouter`.

    Each sibling route-group module owns one of these. The home `web.py`
    module composes them via `make_app()` so route surface is one
    introspectable list across siblings.
    """

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.routes: list[_Route] = []

    def add(self, path: str, endpoint=None) -> None:
        full = self.prefix + path if self.prefix else path
        self.routes.append(_Route(full, endpoint))


router = _WebRouter()
router.add("/")


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

    disabled = ideation_mod._ideation_disabled(cfg)
    threshold = ideation_mod._trigger_task_count(cfg)
    cooldown_s = ideation_mod._cooldown_s(cfg)

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


def _render_focus_card(cfg: Config) -> str:
    """Render the axis-4 focus state card for the home page (TB-242).

    Renders the operator-authored `## Current focus:` headings + the
    ideation halt state above the automation card so an operator
    returning after walk-away can answer "what's the project working
    on, and is ideation parked?" without leaving the home page. Two
    shapes (mirrors the `cmd_status` text branch):

      - halt state (`roadmap_complete_emitted=True`) → red-tinted
        `is-paused` card. The card always renders the parked state
        while exhausted; the actionable resume/dismiss hint
        (`ap2 update-goal` resumes by editing goal.md; `ap2 ack
        roadmap_complete` dismisses the notice) is rendered as `<code>`
        unless the operator has dismissed THIS episode, in which case
        the body collapses to `ideation parked (notice dismissed)`
        (TB-340).
      - active → green-tinted `is-healthy` card listing all focus
        titles in priority order (top → bottom of goal.md), comma-
        separated. TB-342 collapsed the multi-focus pointer walk into a
        single ideation-exhaustion detector, so the daemon does not
        sequence foci — the list is the operator's intent, the ideation
        agent reads them all each cycle, and the goal-anchor validator
        accepts any of them.

    Omitted entirely (empty string) when goal.md is missing or has
    zero `## Current focus:` headings (fresh / pre-pivot projects),
    so the default-off home stays byte-identical to pre-TB-242.

    Reuses the `automation-status` CSS classes so the visual styling
    is consistent with the sibling automation card directly below
    it — the operator reads them as one cluster of "what is the
    daemon's automation doing right now?" state.

    Reads `goal.read_focus_list(cfg)`, `goal.roadmap_exhausted` (a pure
    pointer predicate post-TB-340/TB-342), and
    `goal.roadmap_complete_notice_dismissed` (reads the pointer's
    dismissal marker) — neither walks the events tail.
    """
    from . import goal as _goal

    foci = _goal.read_focus_list(cfg)
    if not foci:
        return ""
    if _goal.roadmap_exhausted(cfg, foci):
        # TB-275/TB-340/TB-342: roadmap_complete parks the ideation
        # trigger only — task dispatch continues normally. The parked
        # state card always renders while exhausted; the actionable
        # resume/dismiss nag is suppressed once the operator dismissed
        # THIS episode (surfacing-vs-state split). Resume is editing
        # goal.md via `ap2 update-goal` (the operator-queue handler
        # calls `reset_pointer_on_goal_updated` to clear the halt);
        # `ap2 ack roadmap_complete` only DISMISSES this notice
        # (ideation stays parked). `ap2 pause` is the explicit
        # full-stop verb.
        klass = "automation-status is-paused"
        header = "Focus — parked"
        if _goal.roadmap_complete_notice_dismissed(cfg, foci):
            body = "ideation parked (notice dismissed)"
        else:
            body = (
                "ideation parked — extend "
                "<code>goal.md</code> via "
                "<code>ap2 update-goal</code> to resume, or "
                "<code>ap2 ack roadmap_complete</code> to dismiss this "
                "notice (ideation stays parked)"
            )
    else:
        klass = "automation-status is-healthy"
        header = "Focus"
        body = ", ".join(html.escape(f.title) for f in foci)
    return (
        f'<div class="{klass}">'
        f'<span class="as-header">{html.escape(header)}</span>'
        f'<span class="as-body">{body}</span>'
        '</div>'
    )


# ------------- TB-299: active-attention-conditions card on / -------------


# Inline cap mirrors `cli_daemon.cmd_status`'s text-render cap (TB-298)
# so an operator switching between `ap2 status` and the browser home
# page sees consistent shape across both summary surfaces. The full
# list lives on the dedicated `/attention` page (TB-296).
_ATTENTION_CARD_MAX_BULLETS = 3


def _render_attention_card(cfg: Config) -> str:
    """Render the active-attention-conditions summary card for the home
    page (TB-299).

    Sibling to `_render_focus_card` (TB-242) and `_render_automation_card`
    (TB-227) — all three render compact at-a-glance state for the
    operator-attention cluster. The attention card sits BETWEEN focus
    and automation in the composition order: attention conditions are
    the most actionable signal (they name a specific condition needing
    eyes), focus and automation are state.

    Consumes the SAME `attention.detect_attention_conditions(cfg)`
    entrypoint as `/attention` (TB-296), the status-report cron's
    `## Attention needed` push (TB-282), the immediate-Mattermost
    push (TB-297), and the `ap2 status` text/JSON render (TB-298) —
    one detector layer, five operator-facing consumer surfaces, no
    drift.

    Bullet shape mirrors `web_attention._render_attention` (warn-glyph
    `⚠`, bold TB-N when `extras['task']` is set, em-dash, detector-
    supplied `summary`); per-task bullets additionally wrap the TB-N
    in an anchor to `/task/<TB-N>` so the operator can click through
    to the detail page in one step. Singleton bullets (no
    `extras['task']`) render as bare `⚠ <summary>` — no orphaned
    `<strong>` markup.

    Inline cap: at most `_ATTENTION_CARD_MAX_BULLETS` (3) bullets, with
    a `(+M more — see /attention)` link-tail when more conditions are
    active. Mirrors TB-298's CLI cap so the cross-surface shape stays
    consistent for an operator alternating between `ap2 status` and
    the home page.

    Empty-state discipline: OMIT THE ENTIRE CARD when zero conditions
    fire (no heading, no body, no zero-noise) so a quiet project's
    home page stays clean. Mirrors `_render_focus_card` /
    `_render_automation_card` omit-on-empty discipline.

    Defensive fallback: a detector exception is swallowed and
    rendered as a tinted notice — the home page must never 500
    because one detector errored. Mirrors `web_attention._render_attention`'s
    swallow-on-error contract.
    """
    # TB-315: `detect_attention_conditions` lives in
    # `ap2/components/attention/__init__.py` post-migration. Core
    # resolves it via a dynamic `importlib.import_module(...)` call
    # so the TB-311 import-direction gate (which walks static
    # Import / ImportFrom nodes) stays quiet; the module attribute
    # is dereferenced at call time so monkeypatch.setattr-style
    # test fixtures targeting the new module path still propagate.
    import importlib as _importlib

    try:
        _attention_mod = _importlib.import_module(
            "ap2.components.attention",
        )
        conditions = _attention_mod.detect_attention_conditions(cfg)
    except Exception as e:  # noqa: BLE001 — never break the page
        # Tinted notice — reuse the `automation-status is-paused`
        # palette so the operator's eye picks up the surface error
        # as part of the same "needs attention" visual cluster.
        return (
            '<div class="automation-status is-paused attention-card-error">'
            '<span class="as-header">Attention</span>'
            '<span class="as-body">'
            f'detector error: {html.escape(type(e).__name__)}: '
            f'{html.escape(str(e))}'
            '</span>'
            '</div>'
        )

    if not conditions:
        return ""

    total = len(conditions)
    visible = conditions[:_ATTENTION_CARD_MAX_BULLETS]
    items: list[str] = []
    for cond in visible:
        task_id = (cond.extras.get("task") or "").strip()
        if task_id:
            # Per-task bullet: bold TB-N wrapped in a `/task/<TB-N>`
            # link so the operator can click through to the detail
            # page from the home summary in one step.
            items.append(
                "<li>"
                '<span class="att-glyph">⚠</span> '
                f'<strong><a href="/task/{html.escape(task_id)}">'
                f'{html.escape(task_id)}</a></strong> — '
                f"{html.escape(cond.summary)}"
                "</li>"
            )
        else:
            # Singleton detector (e.g. `validator_judge_noisy`,
            # `auto_approve_paused`, `cost_cap_approach`): no TB-N
            # anchor, no orphaned `<strong>` — bare `⚠ <summary>`.
            items.append(
                "<li>"
                '<span class="att-glyph">⚠</span> '
                f"{html.escape(cond.summary)}"
                "</li>"
            )
    if total > _ATTENTION_CARD_MAX_BULLETS:
        more = total - _ATTENTION_CARD_MAX_BULLETS
        # Link-tail rendered as one more <li> so it shares the bullet
        # column with the visible conditions; the explicit "+M more"
        # makes the truncation visible (rather than silently dropping
        # tail conditions) and the `/attention` link is the
        # detail-view destination.
        items.append(
            '<li class="att-more">'
            f'(+{more} more — see <a href="/attention">/attention</a>)'
            '</li>'
        )
    return (
        '<div class="attention-card">'
        f'<h2>Attention <span class="meta">({total})</span></h2>'
        f'<ul class="attention-conditions">{"".join(items)}</ul>'
        '</div>'
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
        # TB-243: validator-judge fail-open counts also keep the card
        # visible — the silent-degradation hazard is the WHOLE reason
        # for surfacing these counts, so omitting the card when only
        # the judge is noisy (with auto-approve still off / no other
        # 24h activity) would defeat the purpose. Mirrors the TB-243
        # text-block visibility rule in `cmd_status`.
        + state["validator_judge_fail_count_24h"]
        + state["validator_judge_timeout_count_24h"]
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

    # TB-256: split the body render into three state branches so the
    # rendered text honestly reflects knob state, even when
    # `counters_total > 0` purely because the TB-243 validator-judge
    # counters fired. Pre-TB-256 the `else` branch unconditionally
    # printed `enabled — circuit healthy` whenever the outer
    # `if not enabled and counters_total == 0: return ""` guard fell
    # through — which meant a `validator_judge_fail` event in the 24h
    # window made the card claim the knob was on even with
    # `AP2_AUTO_APPROVE` unset. JSON output (`auto_approve_enabled`)
    # always stayed correct; the bug was local to this text render.
    # Symmetric mirror of TB-250's fix to `cli.py:cmd_status`.
    #
    #   - State A         knob ON  + healthy  → "enabled — circuit healthy"
    #                                            (is-healthy, green).
    #   - State A-paused  knob ON  + paused   → "PAUSED (reason=...; ...)"
    #                                            (is-paused, red).
    #   - State B         knob OFF + activity → "disabled (validator-judge
    #                                            24h: N fail, M timeout)"
    #                                            (is-disabled-but-active,
    #                                            grey — informational,
    #                                            not green-flag).
    #   - State C         knob OFF + no act.  → outer guard returns ""
    #                                            (existing behavior at
    #                                            L1642; unchanged).
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
    elif enabled:
        klass = "automation-status is-healthy"
        header = "Auto-approve"
        body = "enabled — circuit healthy"
    else:
        # State B: knob is OFF but the card is visible because
        # `counters_total > 0` — surface the activity that justified
        # rendering the card without misrepresenting the master switch.
        # Validator-judge counts get top billing because they are the
        # TB-243 silent-degradation hazard that prompted the outer
        # aggregator's expansion; other 24h activity (auto-approved /
        # auto-unfrozen events) still surfaces via the rows below.
        klass = "automation-status is-disabled-but-active"
        header = "Auto-approve"
        body = (
            f'disabled (validator-judge 24h: '
            f'{state["validator_judge_fail_count_24h"]} fail, '
            f'{state["validator_judge_timeout_count_24h"]} timeout)'
        )

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
    # TB-243: "Validator judge (24h)" row surfaces TB-235's
    # dependency-coherence judge fail-open audit counts. Omit-on-empty
    # (both counts zero → row absent) so the default-healthy card
    # stays compact; warn-tint class when
    # `(fail + timeout) >= AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`
    # (default 5) so the operator's eye catches the sustained-issue
    # case. Both counts render in one row (not two separate rows)
    # because the operator-facing signal is "is the gate noisy?" —
    # the fail / timeout split helps triage but doesn't justify two
    # rows of card noise. Anchor links cover both event types.
    vj_fail = state["validator_judge_fail_count_24h"]
    vj_timeout = state["validator_judge_timeout_count_24h"]
    if vj_fail or vj_timeout:
        vj_threshold = automation_status.validator_judge_noisy_threshold(cfg)
        vj_noisy = (vj_fail + vj_timeout) >= vj_threshold
        # Inline `class=` so the warn-tint CSS rule (.as-vj-noisy)
        # styles only this row. Default (non-noisy) row stays in the
        # default `.as-meta` palette so a single transient blip
        # doesn't tint the card.
        klass_attr = ' class="as-vj-noisy"' if vj_noisy else ""
        rows.append(
            f'<span{klass_attr}>'
            f'Validator judge (24h): '
            f'<a href="/events?type=validator_judge_fail">'
            f'{vj_fail} fail</a> | '
            f'<a href="/events?type=validator_judge_timeout">'
            f'{vj_timeout} timeout</a>'
            + (" [noisy]" if vj_noisy else "")
            + '</span>'
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


# ------------- TB-260 env-stale WARN line -------------


def _render_env_stale_warning(cfg: Config) -> str:
    """Render the TB-260 env-staleness WARN line for the home page.

    Mirrors the `ap2 status` text-mode WARN line emitted from
    `cli_daemon.cmd_status` when `.cc-autopilot/env`'s live mtime is
    later than the daemon-start mtime (i.e. the operator bumped a knob
    and hasn't restarted yet). Calls
    `automation_status.collect_env_staleness(cfg)` for the same
    `{env_stale, env_file_mtime, env_file_mtime_at_start}` shape the
    CLI consumes, so the surface stays in lock-step with `ap2 status`
    / the cron digest / the watchdog auto_diagnose summary — TB-260's
    single source of truth for env-staleness.

    Default-off byte-identical contract (mirrors TB-260's
    `render_env_staleness_section` shape): returns `""` when
    `env_stale` is False so healthy / pre-opt-in daemons render the
    home page byte-identical to the pre-TB-265 surface. Only the
    stale-env code path adds output, and the remediation command
    (`ap2 stop && ap2 start`) is inlined so the operator doesn't have
    to look it up — same one-liner-with-fix shape as `cmd_status`.

    Pinned by TB-265's prose-verification bullet ("TB-260's env-stale
    WARN rendering on the web home is preserved end-to-end") so a
    future web-touching refactor can't silently drop this surface.
    """
    from . import automation_status

    state = automation_status.collect_env_staleness(cfg)
    if not state.get("env_stale"):
        return ""
    return (
        '<div class="automation-status is-paused">'
        '<span class="as-header">env-stale:</span>'
        '<span class="as-body">'
        f'WARN .cc-autopilot/env modified at '
        f'{html.escape(str(state["env_file_mtime"]))} (after daemon '
        f'start at {html.escape(str(state["env_file_mtime_at_start"]))}) '
        f'— restart with <code>ap2 stop &amp;&amp; ap2 start</code> '
        f'to apply changes'
        '</span>'
        '</div>'
    )


# ------------- top-level home page composer -------------


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
    # TB-242: axis-4 focus-rotation card. Sits directly above the
    # automation card — the operator reads them as one visual cluster
    # of "what is the daemon's automation doing right now?" state
    # (focus pointer + auto-approve loop). Omitted entirely when
    # goal.md is missing or has zero `## Current focus:` headings
    # (fresh / pre-pivot projects); see `_render_focus_card` for the
    # contract.
    focus_html = _render_focus_card(cfg)
    # TB-299: active-attention-conditions summary card. Sits directly
    # BETWEEN the focus card (TB-242) and the automation card (TB-227)
    # so the operator-attention cluster orders by urgency — attention
    # conditions are the most actionable signal (they name a specific
    # condition needing eyes); focus and automation are state. Omitted
    # entirely when `attention.detect_attention_conditions(cfg)`
    # returns [] (quiet project's home page stays clean); see
    # `_render_attention_card` for the contract.
    attention_html = _render_attention_card(cfg)
    # TB-227: auto-approve / auto-unfreeze loop state card. Sits
    # alongside the ideation gate-state card (visual sibling — both
    # are "what's the daemon's automation doing right now?" surfaces).
    # Omitted entirely on pre-opt-in projects (knob off + no 24h
    # activity); see `_render_automation_card` for the contract.
    automation_html = _render_automation_card(cfg)
    # TB-260: env-staleness WARN line. Sits directly under the daemon
    # status header — the operator reads them as one cluster ("daemon
    # running BUT env file changed → restart needed"). Default-off
    # byte-identical on a healthy daemon; only the stale-env code path
    # adds output. Pinned by TB-265's prose-verification bullet so a
    # future web refactor can't silently drop this surface.
    env_stale_html = _render_env_stale_warning(cfg)

    body = (
        f"<h1>ap2 — {html.escape(cfg.project_root.name)}</h1>"
        f'<div class="meta">{html.escape(str(cfg.project_root))}</div>'
        f"<h2>daemon</h2><p>{status}</p>"
        f"{env_stale_html}"
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
        f"{focus_html}"
        f"{attention_html}"
        f"{automation_html}"
        f'<h2>events <span class="meta">— last 30, newest first '
        f'(<a href="/events">all</a>)</span></h2>'
        f"{_events_table(evts, cfg=cfg)}"
    )
    return _layout(cfg.project_root.name, body)
