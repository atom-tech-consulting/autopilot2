"""Mattermost channel adapter (TB-312 origins; TB-389 demotion).

TB-389 DEMOTED mattermost from a top-level loop-participant component to
a CHANNEL ADAPTER owned by the `communication` component. This package no
longer ships a `manifest.py` / `MANIFEST`, so the registry does NOT
discover it as a component — it is not a loop participant. The
`communication` component instantiates `MattermostChannelAdapter` (and
wires `check_new_messages` for inbound) inside its internal channel
registry (`ap2/components/communication/channels.py`), and re-exports the
two MCP-tool handlers (`do_mattermost_reply`, `do_mattermost_thread_read`)
on the communication manifest. `AP2_MM_CHANNELS` is now channel-level
config (whether the Mattermost channel is active), not a component toggle.

Mattermost integration for the daemon.

`check_new_messages` is a non-blocking one-shot fetch: returns new messages
since the last-seen id, advances the cursor in `mm_state.json`, and filters by
mention. Designed for the tick loop — unlike `mm_poll.py` (which long-polls),
this returns immediately.

Pre-TB-312 lived at `ap2/mattermost.py`; the git-move into
`ap2/components/mattermost/__init__.py` is the axis-(5) "Mattermost
HTTP client, channel/team/bot env knobs, and the `mattermost_reply`
MCP tool all move together" cleavage (goal.md L184-186). All call
sites in core now route through `ap2.channel.ChannelAdapter` (the
ABC) — concrete posts go through `MattermostChannelAdapter.post`,
registered on this component's manifest under
`hook_points["channel_adapter"]`.

This module additionally exposes the two MCP-tool handlers
`do_mattermost_reply` and `do_mattermost_thread_read` (moved here
from `ap2/tools.py`) so the import-direction gate (TB-311) stays
green — core's `ap2.tools.build_mcp_server` looks them up via the
registry's `hook_points["mcp_tool_reply"]` / `["mcp_tool_thread_read"]`
slots rather than via a static `from ap2.components.mattermost import …`.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from ap2.channel import ChannelAdapter
from ap2.config import Config


def _api_get(url: str, token: str, path: str) -> dict | list:
    req = urllib.request.Request(
        f"{url}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        return json.loads(resp.read())


def _load_state(cfg: Config) -> dict:
    if not cfg.mm_state_file.exists():
        return {}
    try:
        return json.loads(cfg.mm_state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(cfg: Config, state: dict) -> None:
    cfg.mm_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.mm_state_file.write_text(json.dumps(state, indent=2))


def _channels_to_watch() -> list[str]:
    """Channels come from `AP2_MM_CHANNELS` env (comma-separated channel IDs)."""
    raw = os.environ.get("AP2_MM_CHANNELS", "").strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


def _bot_user_id() -> str:
    return os.environ.get("AP2_MM_BOT_USER_ID", "")


def _mention() -> str:
    return os.environ.get("AP2_MM_MENTION", "@claude-bot")


def check_new_messages(cfg: Config) -> list[dict]:
    """Fetch new messages across watched channels since last check.

    Returns a list of normalized message dicts:
        {"id", "channel_id", "channel_name", "user_id", "user", "text",
         "thread_id", "create_at"}

    Silently returns [] if mattermost env/config is missing — the daemon is
    usable without mattermost.
    """
    url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    channels = _channels_to_watch()
    if not (url and token and channels):
        return []

    bot_id = _bot_user_id()
    mention = _mention()

    state = _load_state(cfg)
    cursors: dict = state.setdefault("cursors", {})
    name_cache: dict = state.setdefault("channel_names", {})
    user_cache: dict = state.setdefault("users", {})
    thread_cache: dict = state.setdefault("thread_mentions", {})
    out: list[dict] = []

    for ch in channels:
        try:
            data = _api_get(url, token, f"/api/v4/channels/{ch}/posts?per_page=60")
        except Exception:
            continue
        order = data.get("order", [])
        posts = data.get("posts", {})
        last_id = cursors.get(ch)
        if not order:
            continue
        newest = order[0]
        newest_ts = posts[newest]["create_at"] if newest in posts else 0
        # First poll for this channel: seed cursor to newest and skip replay.
        # Agents should only see messages posted after the daemon came up.
        if last_id is None:
            cursors[ch] = newest
            cursors[ch + ":ts"] = newest_ts
            continue
        last_ts = cursors.get(ch + ":ts", 0)
        new: list[dict] = []
        for pid in order:
            if pid == last_id:
                break
            p = posts.get(pid, {})
            # Fallback to timestamp if cursor id no longer in window.
            if last_id not in order and p.get("create_at", 0) <= last_ts:
                continue
            if bot_id and p.get("user_id") == bot_id:
                continue
            if p.get("type", "").startswith("system_"):
                continue
            text = p.get("message", "")
            root = p.get("root_id", "")
            # Mention gate: only keep messages mentioning the bot, or thread replies
            # where the root or a sibling mentioned the bot.
            if mention not in text:
                if not (root and _thread_has_mention(url, token, root, mention, thread_cache)):
                    continue
            new.append(p)

        for p in reversed(new):  # oldest-first in output
            out.append(_normalize(url, token, p, name_cache, user_cache))

        cursors[ch] = newest
        cursors[ch + ":ts"] = newest_ts

    _trim_cache(thread_cache, max_size=500)
    _save_state(cfg, state)
    return out


def _trim_cache(cache: dict, max_size: int) -> None:
    """Drop oldest entries (insertion order) if the cache exceeds `max_size`."""
    excess = len(cache) - max_size
    if excess <= 0:
        return
    for key in list(cache.keys())[:excess]:
        del cache[key]


def _normalize(url: str, token: str, post: dict, name_cache: dict, user_cache: dict) -> dict:
    ch_id = post.get("channel_id", "")
    ch_name = name_cache.get(ch_id)
    if not ch_name:
        try:
            ch = _api_get(url, token, f"/api/v4/channels/{ch_id}")
            ch_name = ch.get("name", ch_id)
            name_cache[ch_id] = ch_name
        except Exception:
            ch_name = ch_id
    user_id = post.get("user_id", "")
    user = user_cache.get(user_id)
    if not user:
        try:
            u = _api_get(url, token, f"/api/v4/users/{user_id}")
            user = u.get("username", user_id)
            user_cache[user_id] = user
        except Exception:
            user = user_id
    return {
        "id": post.get("id"),
        "channel_id": ch_id,
        "channel_name": ch_name,
        "user_id": user_id,
        "user": user,
        "text": post.get("message", ""),
        "thread_id": post.get("root_id", ""),
        "create_at": post.get("create_at", 0),
    }


def fetch_thread(cfg: Config, thread_id: str, *, max_messages: int = 50) -> list[dict]:
    """Fetch all posts in a Mattermost thread (root + replies).

    TB-149: lets the MM handler agent read the rest of a thread it was
    invoked on. The single message that lands in `check_new_messages`
    carries `thread_id` + `text` only — a thread-reply like "yes" to a
    prompt the bot asked 10 minutes ago has no context. This helper
    fills that gap by hitting `/api/v4/posts/{post_id}/thread` (the same
    endpoint `_thread_has_mention` already uses) and normalizing the
    response into chronologically-ordered dicts the agent can reason
    about.

    Returns a list of `{user, text, create_at, post_id}` dicts ordered
    oldest-first. user_ids are resolved to display names via the same
    `users` cache `check_new_messages` populates (so repeat reads in the
    same daemon process avoid re-fetching the user record).

    `max_messages` (default 50) bounds the returned list at the OLDEST
    end — i.e. the most-recent N posts are kept, deepest history is
    dropped first. Operator threads in practice are well under 50 posts;
    bigger threads can be summarized server-side in a follow-up tool.

    Returns `[]` if mattermost env is missing — matches `check_new_messages`'s
    skip behavior so the daemon stays usable without mattermost wired up.
    The caller (`do_mattermost_thread_read`) translates the unconfigured
    case into an `_err` for the MCP surface so the agent gets a
    distinguishable failure rather than an empty list.
    """
    url = os.environ.get("MATTERMOST_URL", "").rstrip("/")
    token = os.environ.get("MATTERMOST_TOKEN", "")
    if not (url and token):
        return []
    if not thread_id:
        return []

    state = _load_state(cfg)
    user_cache: dict = state.setdefault("users", {})

    try:
        data = _api_get(url, token, f"/api/v4/posts/{thread_id}/thread")
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    posts = data.get("posts", {}) or {}
    order = data.get("order", []) or []
    # `order` is API-defined as oldest-first for the thread endpoint, but
    # be defensive: sort by create_at to guarantee chronological order
    # regardless of server quirks. Fall back to `posts` keys if `order`
    # is missing.
    pids = list(order) if order else list(posts.keys())
    enriched: list[tuple[int, str, dict]] = []
    for pid in pids:
        p = posts.get(pid)
        if not isinstance(p, dict):
            continue
        enriched.append((int(p.get("create_at", 0) or 0), pid, p))
    enriched.sort(key=lambda t: (t[0], t[1]))

    # `max_messages` truncates from the OLDEST end (drop deepest history
    # first) so the agent always sees the most-recent context.
    if max_messages > 0 and len(enriched) > max_messages:
        enriched = enriched[-max_messages:]

    out: list[dict] = []
    for _ts, pid, post in enriched:
        user_id = post.get("user_id", "")
        user = user_cache.get(user_id)
        if not user and user_id:
            try:
                u = _api_get(url, token, f"/api/v4/users/{user_id}")
                user = u.get("username", user_id)
                user_cache[user_id] = user
            except Exception:
                user = user_id
        out.append({
            "post_id": post.get("id", pid),
            "user": user or user_id,
            "text": post.get("message", ""),
            "create_at": post.get("create_at", 0),
        })

    # Persist any newly-resolved usernames so repeat reads avoid re-fetching.
    _save_state(cfg, state)
    return out


def _thread_has_mention(
    url: str,
    token: str,
    root_id: str,
    mention: str,
    cache: dict,
) -> bool:
    """Return True if any post in `root_id`'s thread mentions the bot.

    Cached by root_id to avoid O(N) thread fetches. Edge case: if a thread is
    first seen without a mention and a later post adds one, non-mention replies
    on that thread will be missed until the cache is cleared. The alternative
    (no False caching) would re-fetch every non-mention reply on every tick.
    """
    if root_id in cache:
        return cache[root_id]
    try:
        data = _api_get(url, token, f"/api/v4/posts/{root_id}/thread")
    except Exception:
        # Don't cache transient API errors — retry on next poll.
        return False
    if not isinstance(data, dict):
        cache[root_id] = False
        return False
    hit = any(mention in p.get("message", "") for p in data.get("posts", {}).values())
    cache[root_id] = hit
    return hit


# ---------------- TB-312: HTTP post + MCP-tool handlers ----------------
#
# Pre-TB-312 these lived in `ap2/tools.py` and call sites in
# `ap2/daemon.py` + `ap2/watchdog.py` reached them via `tools._mm_post`.
# Per goal.md L184-186 ("Mattermost HTTP client, channel/team/bot env
# knobs, and the `mattermost_reply` MCP tool all move together"), the
# HTTP client + MCP handlers move into this component so the axis-(6)
# import-direction gate stays green (core may not statically import
# from `ap2/components/`).
#
# Backwards-compat for tests that monkeypatched `tools._mm_post`: the
# `ap2.tools` module retains a thin re-export shim that defers to this
# component's `_mm_post`. The shim resolves the function dynamically at
# call-time via the registry so a fresh `default_registry()` (post
# `_reset_default_registry()`) picks up the live implementation.


_TEAM_CACHE: str | None = None


def _mm_post(channel: str, text: str, thread_id: str = "") -> str:
    """POST `text` to Mattermost `channel` (id or name). Returns the
    post id on success; raises on HTTP / config failure.

    Resolves a channel NAME (e.g. `town-square`) to an id via
    `_mm_lookup_channel` when `channel` doesn't look like a 26-char
    base32 id. The id form is what the Mattermost API requires for
    `POST /api/v4/posts`; supporting names at the call boundary keeps
    operators from having to memorize ids in their env files.
    """
    url = os.environ.get("MATTERMOST_URL")
    token = os.environ.get("MATTERMOST_TOKEN")
    if not url or not token:
        raise RuntimeError("MATTERMOST_URL and MATTERMOST_TOKEN must be set")
    # Resolve channel name → id if needed (names start without alnum restriction,
    # but IDs are 26-char base32). Best-effort: treat 26-char as id.
    channel_id = channel if len(channel) == 26 and channel.isalnum() else _mm_lookup_channel(url, token, channel)
    body = {"channel_id": channel_id, "message": text}
    if thread_id:
        body["root_id"] = thread_id
    req = urllib.request.Request(
        f"{url}/api/v4/posts",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("id", "")


def _mm_lookup_channel(url: str, token: str, name: str) -> str:
    name = name.lstrip("#")
    # Need a team id; we pick the user's first team as a default.
    team_id = _mm_user_team(url, token)
    req = urllib.request.Request(
        f"{url}/api/v4/teams/{team_id}/channels/name/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read())["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"channel {name!r} not found: {e}") from e


def _mm_user_team(url: str, token: str) -> str:
    global _TEAM_CACHE
    if _TEAM_CACHE:
        return _TEAM_CACHE
    req = urllib.request.Request(
        f"{url}/api/v4/users/me/teams",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        teams = json.loads(resp.read())
    if not teams:
        raise RuntimeError("user has no mattermost teams")
    _TEAM_CACHE = teams[0]["id"]
    return _TEAM_CACHE


class MattermostChannelAdapter(ChannelAdapter):
    """Concrete `ChannelAdapter` (TB-312, axis (3)) wrapping the
    Mattermost HTTP client.

    Resolves the destination channel from `meta["channel"]` when
    provided; otherwise reads the first entry from `AP2_MM_CHANNELS`
    (preserving the watchdog's `_first_mm_channel` convention so a
    legacy caller without a channel arg still routes correctly).
    Optional `meta["thread_id"]` rides through to `_mm_post` so the
    reply MCP tool can target a thread root.

    Returns `None` (no destination) without raising when
    `AP2_MM_CHANNELS` is empty — the caller's `_deliver(...)` helper
    treats `None` as "this adapter is unconfigured, try the next one"
    rather than as a hard failure. Hard HTTP failures still raise so
    the caller's `*_error` audit-event path fires.
    """

    name = "mattermost"

    def post(self, text: str, **meta) -> dict | None:
        channel = (meta.get("channel") or "").strip()
        if not channel:
            raw = os.environ.get("AP2_MM_CHANNELS", "").strip()
            for c in raw.split(","):
                c = c.strip()
                if c:
                    channel = c
                    break
        if not channel:
            return None
        thread_id = (meta.get("thread_id") or "").strip()
        # TB-312: route through `ap2.tools._mm_post` (a shim that
        # defers to this module's `_mm_post`) so pre-TB-312 tests
        # that monkeypatched `tools._mm_post` keep working. The late
        # `from ap2 import tools` import ensures the monkeypatched
        # attribute (not the import-time bound name) is what runs.
        from ap2 import tools
        post_id = tools._mm_post(channel, text, thread_id)
        return {
            "adapter": self.name,
            "channel": channel,
            "post_id": post_id,
            "thread_id": thread_id,
        }


# ---------------- MCP-tool handlers ----------------
#
# `do_mattermost_reply` and `do_mattermost_thread_read` previously
# lived in `ap2/tools.py`. They moved here because they call the
# component-local `_mm_post` / `fetch_thread`; keeping them in core
# would force core to `from ap2.components.mattermost import …`,
# violating the import-direction gate (TB-311). The MCP server in
# `ap2.tools.build_mcp_server` discovers them via
# `default_registry().hook("mcp_tool_reply", component="mattermost")`
# (and the matching `mcp_tool_thread_read` slot).


def _ok(text: str, **fields):
    """Local copy of `tools._ok` (TB-312). Duplicated to avoid a
    component → core import of `_ok`; the shape (text + optional
    fields → MCP-content dict) is the canonical MCP-tool success
    envelope used by every handler in the server.
    """
    body = {"message": text}
    body.update(fields)
    return {
        "content": [{"type": "text", "text": json.dumps(body)}],
    }


def _err(text: str):
    """Local copy of `tools._err` (TB-312). Same reasoning as `_ok`."""
    return {
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
        "isError": True,
    }


def do_mattermost_reply(cfg: Config, args: dict) -> dict:
    """MCP-tool handler — post a reply to a Mattermost channel/thread.

    Pre-TB-312 lived in `ap2/tools.py`. Moved here as part of the
    axis-(5) `mattermost/` migration so the import-direction gate
    stays green.
    """
    # Local import — avoids a circular import at module load time
    # (`ap2.tools` re-exports a small set of symbols pre-TB-312 callers
    # expect to find on it).
    from ap2 import events

    channel = args.get("channel") or ""
    text = args.get("text") or ""
    thread_id = args.get("thread_id") or ""
    if not channel or not text:
        return _err("channel and text are required")
    try:
        # TB-312: route through `ap2.tools._mm_post` shim so pre-TB-312
        # tests that monkeypatched `tools._mm_post` keep working (same
        # pattern as `MattermostChannelAdapter.post`).
        from ap2 import tools
        post_id = tools._mm_post(channel, text, thread_id)
        events.append(
            cfg.events_file,
            "mattermost_reply",
            channel=channel,
            thread_id=thread_id,
            post_id=post_id,
            summary=text[:200],
        )
        return _ok(f"posted to {channel}", post_id=post_id)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def do_mattermost_thread_read(cfg: Config, args: dict) -> dict:
    """MCP-tool handler — fetch a Mattermost thread's posts (TB-149).

    Pre-TB-312 lived in `ap2/tools.py`. Moved here so the
    component-local `fetch_thread` is reachable without a core →
    component import (axis-(6) gate).
    """
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _err("thread_id is required")

    raw_max = args.get("max_messages")
    if raw_max in (None, ""):
        max_messages = 50
    else:
        try:
            max_messages = int(raw_max)
        except (TypeError, ValueError):
            return _err(f"max_messages must be an int, got {raw_max!r}")
    if max_messages <= 0:
        max_messages = 50

    if not (os.environ.get("MATTERMOST_URL") and os.environ.get("MATTERMOST_TOKEN")):
        return _err("mattermost not configured")

    try:
        posts = fetch_thread(cfg, thread_id, max_messages=max_messages)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")

    return _ok(
        f"fetched {len(posts)} thread post(s)",
        thread_id=thread_id,
        count=len(posts),
        posts=posts,
    )
