"""Mattermost integration for the daemon.

`check_new_messages` is a non-blocking one-shot fetch: returns new messages
since the last-seen id, advances the cursor in `mm_state.json`, and filters by
mention. Designed for the tick loop — unlike `mm_poll.py` (which long-polls),
this returns immediately.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from .config import Config


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
