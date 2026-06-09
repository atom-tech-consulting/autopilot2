"""Communication component (TB-389) — the channel surface in both directions.

Extracts the channel surface — previously split across core's
`registry.channel_adapters()` (outbound) and the `inbound_poll`
hook_point (inbound), with `mattermost` as a top-level loop participant
— into a single component that owns BOTH directions as tick-phase work
and holds its channel adapters in an internal registry (`channels.py`)
invisible to core.

Two directions, both owned here:

  - INBOUND  — `poll_inbound(cfg)` walks the internal channel registry,
    concatenating each inbound-capable channel's new messages. The
    daemon's communication loop (`daemon._check_inbound_messages`)
    resolves this via the registry's `poll_inbound` hook and dispatches
    each message to a handler agent — core never references a channel.

  - OUTBOUND — `run_outbound_tick(cfg, sdk)` is registered as a
    `Phase.COMMUNICATION` tick hook the daemon walks each tick. It
    drains the `ap2.notify` queue (which core call sites — the watchdog,
    the smoke runner — append to instead of posting synchronously) and
    delivers each undelivered notification to the internal channels.
    Delivery is therefore EVENT-DRIVEN: a core call site enqueues a
    notification event; the component's tick pass delivers it.

`channel_adapters(cfg)` is the component's own outbound surface for a
peer component that needs an IMMEDIATE synchronous push (the attention
immediate-push, TB-297 — immediacy is its whole point). Core never calls
it; core enqueues via `ap2.notify` and lets the tick deliver.
"""
from __future__ import annotations

from ap2 import events, notify
from ap2.channel import ChannelAdapter
from ap2.config import Config

from .channels import channel_registry


def channel_adapters(cfg=None) -> list[ChannelAdapter]:
    """Outbound channel adapters from the internal channel registry.

    The communication component's own surface. A PEER component (today
    only `attention`, for its opt-in immediate Mattermost push) reaches
    it for a synchronous post; core does NOT — core enqueues via
    `ap2.notify` and the outbound tick delivers. Returns the adapters in
    the internal registry's deterministic order.
    """
    return [c.adapter for c in channel_registry(cfg)]


def poll_inbound(cfg: Config) -> list[dict]:
    """Poll every inbound-capable channel for new messages (TB-389).

    The communication component's inbound tick-phase work. The daemon's
    communication loop calls this (resolved via the registry's
    `poll_inbound` hook, NOT a static import) and dispatches each
    returned message. Walks the INTERNAL channel registry — core has no
    visibility into which channels exist. Today only the Mattermost
    channel polls inbound; a future Slack / email channel joins by
    declaring its own inbound poll in `channels.channel_registry`.
    """
    out: list[dict] = []
    for chan in channel_registry(cfg):
        if chan.inbound is None:
            continue
        msgs = chan.inbound(cfg)
        if msgs:
            out.extend(msgs)
    return out


def run_outbound_tick(cfg: Config, sdk=None) -> None:
    """Deliver queued outbound notifications to the internal channels.

    The communication component's outbound tick-phase hook (registered
    on `Phase.COMMUNICATION`, walked by `daemon._tick`). Drains
    `ap2.notify.pending(cfg)` and posts each undelivered notification to
    every channel adapter in the internal registry.

    Delivery policy (preserves observable behavior while staying
    event-driven):
      - No channel configured yet → leave everything PENDING so a
        later-configured channel still gets the backlog (e.g. an
        operator sets `AP2_MM_CHANNELS` after a fresh install). Bounded
        in practice by the enqueue-side cooldowns (watchdog / smoke).
      - A post raises → emit `notification_error` and leave that
        notification PENDING so the next tick retries (mirrors the
        pre-TB-389 watchdog's "post failed → don't advance, retry").
      - A post succeeds (or returns a no-op `None`) → emit
        `notification_delivered` and mark the notification delivered so
        the queue stays bounded.

    `sdk` is unused (the uniform `(cfg, sdk)` tick-hook signature).
    """
    pend = notify.pending(cfg)
    if not pend:
        return
    channels = channel_registry(cfg)
    if not channels:
        # No destination yet — keep the backlog for a later channel.
        return

    delivered: list[str] = []
    for rec in pend:
        ok = True
        for chan in channels:
            try:
                outcome = chan.adapter.post(
                    rec.get("text", ""),
                    channel=rec.get("channel", ""),
                    thread_id=rec.get("thread_id", ""),
                )
            except Exception as e:  # noqa: BLE001
                ok = False
                events.append(
                    cfg.events_file,
                    "notification_error",
                    kind=rec.get("kind", ""),
                    channel=rec.get("channel", ""),
                    error=f"{type(e).__name__}: {e}",
                )
                continue
            post_id = (
                outcome.get("post_id", "") if isinstance(outcome, dict) else ""
            )
            resolved_channel = rec.get("channel", "") or (
                outcome.get("channel", "") if isinstance(outcome, dict) else ""
            )
            events.append(
                cfg.events_file,
                "notification_delivered",
                kind=rec.get("kind", ""),
                channel=resolved_channel,
                post_id=post_id,
            )
        if ok:
            delivered.append(rec.get("uuid", ""))

    notify.mark_delivered(cfg, delivered)
