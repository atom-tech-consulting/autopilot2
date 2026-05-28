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

from . import diagnose, events
from .config import Config
from .registry import default_registry


def _maybe_auto_diagnose(cfg: Config, *, now: float | None = None) -> None:
    """Idle-watchdog hook (TB-71). See `_tick` step 5 for context.

    Inspects events.jsonl for the most recent meaningful event. If the gap
    exceeds `cfg.auto_diagnose_idle_threshold_s` AND we haven't fired within
    `cfg.auto_diagnose_cooldown_s`, post `diagnose.render_markdown` through
    the channel-adapter list (TB-312) — the default registered adapter for
    a Mattermost-wired project is `MattermostChannelAdapter`, which posts
    to `AP2_MM_CHANNELS[0]` via the same path pre-TB-312 used directly.
    Updates persistent state in `cfg.auto_diagnose_state_file`.

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
    # the diagnose dump" alert misdescribes the state. Post a softer
    # one-liner reminder instead and reuse the diagnose cooldown so the
    # operator isn't spammed.
    if diagnose.is_wholly_pending_review(report):
        channel = _first_mm_channel()
        pending = report.board_health.get("pending_review") or []
        ids_str = ", ".join(pending[:10])
        reminder = (
            f"**ap2 pending review** — `{cfg.project_root.name}` has "
            f"{len(pending)} ideation proposal"
            f"{'s' if len(pending) != 1 else ''} awaiting operator "
            f"approval ({ids_str}). Run `ap2 approve TB-N` to dispatch, "
            f"or `ap2 delete TB-N --force` to discard."
        )
        # TB-312: walk registered channel adapters instead of calling
        # `tools._mm_post` directly. When no adapter is registered (or
        # mattermost is the only adapter and AP2_MM_CHANNELS is unset),
        # `channel_adapters(cfg)` returns [] and the reminder simply
        # lands as an audit event with `post_id=None` — same observable
        # shape as pre-TB-312's `channel=""` branch.
        adapters = default_registry().channel_adapters(cfg)
        post_id: str | None = None
        for adapter in adapters:
            try:
                outcome = adapter.post(reminder, channel=channel)
            except Exception as e:  # noqa: BLE001
                events.append(
                    cfg.events_file,
                    "auto_diagnose_post_error",
                    channel=channel,
                    error=f"{type(e).__name__}: {e}",
                )
                return
            if isinstance(outcome, dict):
                post_id = outcome.get("post_id") or post_id
        events.append(
            cfg.events_file,
            "pending_review_reminder",
            channel=channel,
            post_id=post_id,
            pending=pending,
            idle_s=report.since_last_activity_s,
        )
        state["last_fired"] = now
        state["warned_no_destination"] = False
        _save_diagnose_state(cfg, state)
        return

    channel = _first_mm_channel()
    adapters = default_registry().channel_adapters(cfg)
    if not adapters:
        # No registered adapter (or mattermost component disabled
        # because AP2_MM_CHANNELS is empty). Warn ONCE per run; the
        # flag is sticky in state so we don't fill events.jsonl with
        # the same line every tick.
        if not state.get("warned_no_destination"):
            events.append(
                cfg.events_file,
                "auto_diagnose_no_destination",
                reason="AP2_MM_CHANNELS unset",
                idle_s=report.since_last_activity_s,
            )
            state["warned_no_destination"] = True
            _save_diagnose_state(cfg, state)
        return

    text = diagnose.render_markdown(report)
    post_id: str | None = None
    for adapter in adapters:
        try:
            outcome = adapter.post(text, channel=channel)
        except Exception as e:  # noqa: BLE001
            events.append(
                cfg.events_file,
                "auto_diagnose_post_error",
                channel=channel,
                error=f"{type(e).__name__}: {e}",
            )
            return
        if isinstance(outcome, dict):
            post_id = outcome.get("post_id") or post_id

    events.append(
        cfg.events_file,
        "auto_diagnose_fired",
        channel=channel,
        post_id=post_id,
        idle_s=report.since_last_activity_s,
        report_summary=text[:500],
    )
    state["last_fired"] = now
    state["warned_no_destination"] = False  # reset — destination is back
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
