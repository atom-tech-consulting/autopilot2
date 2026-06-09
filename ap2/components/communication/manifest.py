"""Communication component manifest (TB-389).

Declares the communication component's registry-visible shape. The
component owns the channel surface in BOTH directions as tick-phase
work, and holds its channel adapters (Mattermost today; Slack / email
later) in an internal registry (`channels.channel_registry`) that core
never sees:

  - env_flag        — `None`: the communication component is always-on
                      (mirroring `attention`). Channel MULTIPLICITY is no
                      longer a kernel concern — whether any given channel
                      is active is channel-level config (e.g. the
                      Mattermost channel activates on `AP2_MM_CHANNELS`),
                      resolved inside `channels.channel_registry`, not via
                      a component-level env toggle.
  - tick_hooks      — `(Phase.COMMUNICATION, run_outbound_tick)`: the
                      OUTBOUND delivery pass the daemon walks each tick.
                      It drains the `ap2.notify` queue (core call sites
                      append to it instead of posting synchronously) and
                      delivers each undelivered notification to the
                      internal channels. Delivery is event-driven.
  - hook_points     —
      `poll_inbound`        — the INBOUND tick-phase work: the daemon's
                              communication loop resolves this via the
                              registry and dispatches each returned
                              message. Replaces the pre-TB-389
                              `inbound_poll` hook_point that core walked
                              directly.
      `outbound_tick`       — `run_outbound_tick`, also exposed as a
                              hook_point for direct invocation (tests,
                              future cron).
      `mcp_tool_reply`      — `do_mattermost_reply`, delegated from the
                              Mattermost channel so the MCP server
                              (`ap2.tools.build_mcp_server`) discovers the
                              `mattermost_reply` handler under the
                              communication component now that mattermost
                              is no longer a top-level component.
      `mcp_tool_thread_read`— `do_mattermost_thread_read` (TB-149
                              thread-context fetch).
  - config_schema   — the Mattermost channel's knobs become channel-level
                      config owned by the communication component:
                      `channels` (`AP2_MM_CHANNELS`), `bot_user_id`
                      (`AP2_MM_BOT_USER_ID`), `mention` (`AP2_MM_MENTION`).
"""
from __future__ import annotations

from ap2.config_loader import ConfigKey
from ap2.registry import Manifest, Phase

from ap2.components.mattermost import (
    do_mattermost_reply,
    do_mattermost_thread_read,
)

from .impl import poll_inbound, run_outbound_tick


MANIFEST = Manifest(
    name="communication",
    env_flag=None,
    default_enabled=True,
    hook_points={
        # INBOUND tick-phase work — the daemon's communication loop
        # resolves this via the registry (NOT a static import) and
        # dispatches each returned message to a handler agent.
        "poll_inbound": poll_inbound,
        # OUTBOUND tick-phase work — also exposed as a hook_point for
        # direct invocation; the daemon walks it via `tick_hooks` below.
        "outbound_tick": run_outbound_tick,
        # Mattermost MCP-tool handlers — discovered under the
        # communication component now that mattermost is a channel
        # adapter rather than a top-level component. The external tool
        # names (`mattermost_reply` / `mattermost_thread_read`) stay
        # stable (goal.md L184-186).
        "mcp_tool_reply": do_mattermost_reply,
        "mcp_tool_thread_read": do_mattermost_thread_read,
    },
    tick_hooks=[(Phase.COMMUNICATION, run_outbound_tick)],
    dependencies=[],
    config_schema={
        "channels": ConfigKey(
            name="channels",
            type=str,
            default="",
            description=(
                "Comma-separated Mattermost channel IDs the communication "
                "component polls for inbound mentions and posts outbound "
                "messages to. Unset / empty leaves the Mattermost channel "
                "inactive (the channel-level activation knob, TB-389). "
                "Mirrors `AP2_MM_CHANNELS`; listed in "
                "`env_reload.FIXED_KNOBS` so a change requires "
                "`ap2 stop && ap2 start` (the subscription set is bound "
                "once at daemon-start)."
            ),
            hot_reloadable=False,
        ),
        "bot_user_id": ConfigKey(
            name="bot_user_id",
            type=str,
            default="",
            description=(
                "Mattermost user ID for the bot account; used to filter "
                "the bot's own posts out of the inbound poll. Mirrors "
                "`AP2_MM_BOT_USER_ID`; not in `HOT_RELOADABLE_KNOBS`, so "
                "conservative-default `hot_reloadable=False`."
            ),
            hot_reloadable=False,
        ),
        "mention": ConfigKey(
            name="mention",
            type=str,
            default="@claude-bot",
            description=(
                "Mention token (e.g. `@claude-bot`) the bot recognizes as "
                "addressing it in poll content. Mirrors `AP2_MM_MENTION`; "
                "not in `HOT_RELOADABLE_KNOBS`, so conservative-default "
                "`hot_reloadable=False`."
            ),
            hot_reloadable=False,
        ),
    },
)
