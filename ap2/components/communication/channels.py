"""Internal channel registry for the communication component (TB-389).

This module is the communication component's PRIVATE channel surface —
the registry of delivery channels (Mattermost today; Slack / email
later) the component owns. Core never sees it: the import-direction gate
(TB-311) forbids `core → ap2.components.*`, and the daemon / watchdog /
smoke_runner reach the channel surface only indirectly (inbound via the
registry-resolved `poll_inbound` hook, outbound via the
`ap2.notify` queue the component drains). Demoting channel multiplicity
out of the kernel — `registry.channel_adapters()` and the `inbound_poll`
hook_point are both gone from core — is the whole point of TB-389.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from ap2.channel import ChannelAdapter


@dataclass
class Channel:
    """One delivery channel: an outbound adapter + optional inbound poll.

    `adapter` is the `ChannelAdapter` the outbound tick posts through.
    `inbound`, when set, is a `(cfg) -> list[dict]` poll the daemon's
    communication loop drives for new operator messages. A send-only
    channel (e.g. a generic webhook) leaves `inbound=None`.
    """

    name: str
    adapter: ChannelAdapter
    inbound: Optional[Callable] = None


def channel_registry(cfg=None) -> list[Channel]:
    """Build the communication component's internal channel registry.

    Each entry pairs an outbound `ChannelAdapter` with an optional
    inbound poll fn. Channels are gated by their own activation config:
    the Mattermost channel is present only when `AP2_MM_CHANNELS` is set
    — the env knob that used to toggle the mattermost *component* is now
    channel-level config (TB-389). Future Slack / email channels slot in
    here behind their own knobs.

    Deterministic order (Mattermost first today) keeps both inbound
    concatenation and outbound delivery reproducible across daemon
    restarts. `cfg` is accepted for forward-compat (a channel factory
    may want per-cfg knobs) but unused today — channels read their env
    lazily so a hot-reloaded env (TB-271) applies on the next pass.
    """
    chans: list[Channel] = []
    # Mattermost — demoted from a top-level component (TB-389) to a
    # channel adapter the communication component owns. Imported from the
    # sibling `mattermost` package (component → component, allowed by the
    # import-direction gate, which only fences core → component). Late
    # import so the package loads lazily on first delivery / poll.
    from ap2.components.mattermost import (
        MattermostChannelAdapter,
        check_new_messages,
    )

    if os.environ.get("AP2_MM_CHANNELS", "").strip():
        chans.append(
            Channel(
                name="mattermost",
                adapter=MattermostChannelAdapter(),
                inbound=check_new_messages,
            )
        )
    return chans
