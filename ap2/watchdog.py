"""Idle watchdog (TB-71) — auto-diagnose-fired summary composition
and idle-window detection.

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) decides WHEN to run the watchdog tick; this module
owns the idle-window detection + Mattermost post + pending-review
reminder + per-process state file (`auto_diagnose_state.json`).

Public surface (all re-exported from `ap2/daemon.py` so existing test
imports — `daemon._maybe_auto_diagnose`, `daemon._first_mm_channel`,
`daemon._load_diagnose_state`, `daemon._save_diagnose_state` — keep
resolving):

  - `_maybe_auto_diagnose(cfg, *, now=None)`: the orchestrator hook.
    Inspects `events.jsonl` for the most recent meaningful event; when
    the gap exceeds `cfg.auto_diagnose_idle_threshold_s` AND the
    cooldown window has elapsed, posts a Mattermost digest (or a
    softer pending-review reminder when the board is
    wholly-pending-review per `diagnose.is_wholly_pending_review`).
  - `_first_mm_channel()`: parses the first entry from
    `AP2_MM_CHANNELS`. Mirrors `mattermost._channels_to_watch` so the
    watchdog and the inbound poller agree on the project's channel.
  - `_load_diagnose_state` / `_save_diagnose_state`: the per-process
    state file the watchdog uses to dedupe `last_fired` /
    `warned_no_destination`. Defensive JSON parse — a corrupt file
    returns `{}` rather than raising.

The state file (`auto_diagnose_state.json`) is gitignored — it's
ephemeral runtime state. The watchdog must not re-fire on daemon restart
just because the file got rebuilt.
"""
from __future__ import annotations

import json
import os
import time

from . import diagnose, events, notify
from .config import Config


def _maybe_auto_diagnose(cfg: Config, *, now: float | None = None) -> None:
    """Idle-watchdog hook (TB-71). See `_tick` step 5 for context.

    Inspects events.jsonl for the most recent meaningful event. If the gap
    exceeds `cfg.auto_diagnose_idle_threshold_s` AND we haven't fired
    within `cfg.auto_diagnose_cooldown_s`, ENQUEUE a diagnose digest (or a
    softer pending-review reminder when the board is wholly-pending-review)
    onto the `ap2.notify` outbound queue. The communication component's
    `Phase.COMMUNICATION` tick pass delivers the queued notification to its
    internal channels — the watchdog never walks a channel-adapter list
    nor references `AP2_MM_CHANNELS` (TB-389: core no longer references
    channels; outbound delivery is event-driven). Updates persistent state
    in `cfg.auto_diagnose_state_file`.

    The cooldown now keys on the ENQUEUE time (`last_fired`): a digest is
    queued at most once per `auto_diagnose_cooldown_s`. A digest enqueued
    while no channel is configured stays pending and delivers once a
    channel appears (the communication component's delivery policy), so the
    pre-TB-389 sticky `warned_no_destination` bookkeeping is gone.

    `now` parameter exists so tests can drive a fake clock; production uses
    `time.time()`.
    """
    if now is None:
        now = time.time()

    state = _load_diagnose_state(cfg)
    report = diagnose.build_report(cfg, now=now)

    # No meaningful events yet (fresh daemon) → can't be idle. Skip.
    if report.since_last_activity_s is None:
        return

    if report.since_last_activity_s < cfg.auto_diagnose_idle_threshold_s:
        return

    if now - state.get("last_fired", 0.0) < cfg.auto_diagnose_cooldown_s:
        return

    # TB-121: when every Backlog task is review-gated and nothing else is
    # in flight, the daemon is correctly idle — operator approval is the
    # only thing that can move work forward, so a "daemon idle, here's
    # the diagnose dump" alert misdescribes the state. Queue a softer
    # one-liner reminder instead and reuse the diagnose cooldown so the
    # operator isn't spammed.
    if diagnose.is_wholly_pending_review(report):
        pending = report.board_health.get("pending_review") or []
        ids_str = ", ".join(pending[:10])
        reminder = (
            f"**ap2 pending review** — `{cfg.project_root.name}` has "
            f"{len(pending)} ideation proposal"
            f"{'s' if len(pending) != 1 else ''} awaiting operator "
            f"approval ({ids_str}). Run `ap2 approve TB-N` to dispatch, "
            f"or `ap2 delete TB-N --force` to discard."
        )
        # TB-389: enqueue onto the outbound notification queue; the
        # communication component delivers it on its tick pass. No
        # channel is specified — the Mattermost channel adapter resolves
        # its own default destination (`AP2_MM_CHANNELS[0]`).
        notify.enqueue(cfg, reminder, kind="pending_review_reminder")
        events.append(
            cfg.events_file,
            "pending_review_reminder",
            pending=pending,
            idle_s=report.since_last_activity_s,
        )
        state["last_fired"] = now
        _save_diagnose_state(cfg, state)
        return

    text = diagnose.render_markdown(report)
    # TB-389: enqueue the digest; the communication tick delivers it.
    notify.enqueue(cfg, text, kind="auto_diagnose")
    events.append(
        cfg.events_file,
        "auto_diagnose_fired",
        idle_s=report.since_last_activity_s,
        report_summary=text[:500],
    )
    state["last_fired"] = now
    _save_diagnose_state(cfg, state)


def _first_mm_channel() -> str:
    """Return the first channel id from `AP2_MM_CHANNELS`, or empty string.

    Mirrors `mattermost._channels_to_watch` parsing so the watchdog and the
    inbound poller agree on which env var defines "the project's channel(s)".
    """
    raw = os.environ.get("AP2_MM_CHANNELS", "").strip()
    for c in raw.split(","):
        c = c.strip()
        if c:
            return c
    return ""


def _load_diagnose_state(cfg: Config) -> dict:
    if not cfg.auto_diagnose_state_file.exists():
        return {}
    try:
        data = json.loads(cfg.auto_diagnose_state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_diagnose_state(cfg: Config, state: dict) -> None:
    cfg.auto_diagnose_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.auto_diagnose_state_file.write_text(json.dumps(state, indent=2, sort_keys=True))
