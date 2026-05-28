"""Mattermost component manifest (TB-312, axes 3 + 5 bundled).

Declares the mattermost component's registry-visible shape:

  - env_flag        — `AP2_MM_CHANNELS`. Truthy (any non-empty value)
                      ENABLES delivery + polling; absent/empty leaves
                      Mattermost wholly inactive (goal.md L64-67 pins
                      the env-knob name verbatim — DO NOT RENAME this
                      env key without operator-visible migration).
  - default_enabled — `False`. Mattermost is opt-in per project; a
                      fresh `ap2 init` clone has no MM credentials and
                      should not attempt to post.
  - hook_points     — three slots used by core call sites:
      `channel_adapter`     — `MattermostChannelAdapter` instance used
                              by `_deliver(text, **meta)` in daemon.py
                              + watchdog.py (the three rewired call
                              sites: `daemon.py:1919` attention push,
                              `watchdog.py:90,130` auto-diagnose +
                              pending-review reminder).
      `mcp_tool_reply`      — `do_mattermost_reply` handler. The MCP
                              server (`ap2.tools.build_mcp_server`)
                              looks this up at server-construction
                              time rather than `from
                              ap2.components.mattermost import …` so
                              the import-direction gate (TB-311) stays
                              green.
      `mcp_tool_thread_read`— `do_mattermost_thread_read` handler
                              (TB-149 thread-context fetch).
      `inbound_poll`        — `check_new_messages`. The daemon's
                              `_mm_loop` polls this on
                              `AP2_MM_TICK_S` — looked up via the
                              registry so the daemon does not
                              statically import the component.

Polarity note (operator-facing): pre-TB-312 the `_first_mm_channel()`
helpers in daemon.py + watchdog.py both treated an unset
`AP2_MM_CHANNELS` as "no destination, suppress with sticky warning."
The manifest-level `env_flag` polarity follows the same convention:
"`AP2_MM_CHANNELS` unset → component disabled, no warnings; set →
component enabled, watchdog warnings re-arm if a specific channel
later becomes unreachable." Operators who had Mattermost wired pre-
TB-312 see no behavior change; the per-call `*_no_destination` audit
event family stays for the in-band channel-lookup-failed case.
"""
from __future__ import annotations

from ap2.registry import Manifest

from . import (
    MattermostChannelAdapter,
    check_new_messages,
    do_mattermost_reply,
    do_mattermost_thread_read,
)


MANIFEST = Manifest(
    name="mattermost",
    env_flag="AP2_MM_CHANNELS",
    default_enabled=False,
    hook_points={
        # axis (3) — daemon.py + watchdog.py route through this when
        # `_deliver(text, **meta)` walks `registry.channel_adapters(cfg)`.
        # Registered as a class so the registry's accessor instantiates
        # a fresh adapter per call (cheap; the adapter is stateless
        # apart from the module-level `_TEAM_CACHE` in the component).
        "channel_adapter": MattermostChannelAdapter,
        # axis (5) — the MCP server discovers these handlers via the
        # registry so `ap2/tools.py` doesn't statically import the
        # component. The MCP-tool name registered on the server side
        # stays `mattermost_reply` / `mattermost_thread_read` per
        # goal.md L184-186's "tool keeps its registered name" pin.
        "mcp_tool_reply": do_mattermost_reply,
        "mcp_tool_thread_read": do_mattermost_thread_read,
        # Inbound-message polling — replaces the pre-TB-312 direct
        # `from .mattermost import check_new_messages` in
        # `ap2/daemon.py`. The daemon's `_mm_loop` looks this up via
        # the registry; when the mattermost component is disabled
        # (`AP2_MM_CHANNELS` unset), the lookup returns no
        # `inbound_poll` hook and the daemon's poll path no-ops.
        "inbound_poll": check_new_messages,
    },
    dependencies=[],
)
