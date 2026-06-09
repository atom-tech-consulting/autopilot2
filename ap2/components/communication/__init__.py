"""communication component package shim (TB-389).

Re-exports the component's public surface so peer components / tests can
`from ap2.components.communication import X`. The component owns the
channel surface in both directions (inbound polling + outbound delivery)
as tick-phase work and holds its channel adapters in an internal
registry (`channels.py`) that core cannot see.
"""
from .channels import Channel, channel_registry
from .impl import channel_adapters, poll_inbound, run_outbound_tick

__all__ = [
    "Channel",
    "channel_registry",
    "channel_adapters",
    "poll_inbound",
    "run_outbound_tick",
]
