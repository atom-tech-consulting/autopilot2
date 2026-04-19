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
from pathlib import Path

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
        last_ts = cursors.get(ch + ":ts", 0)
        new: list[dict] = []
        for pid in order:
            if pid == last_id:
                break
            p = posts.get(pid, {})
            # Fallback to timestamp if cursor id no longer in window.
            if last_id and last_id not in order and p.get("create_at", 0) <= last_ts:
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
                if not (root and _thread_has_mention(url, token, root, mention)):
                    continue
            new.append(p)

        for p in reversed(new):  # oldest-first in output
            out.append(_normalize(url, token, p, name_cache, user_cache))

        cursors[ch] = newest
        cursors[ch + ":ts"] = newest_ts

    if out:
        _save_state(cfg, state)
    else:
        _save_state(cfg, state)  # persist new cursor even if no relevant msgs
    return out


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


def _thread_has_mention(url: str, token: str, root_id: str, mention: str) -> bool:
    try:
        data = _api_get(url, token, f"/api/v4/posts/{root_id}/thread")
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    for p in data.get("posts", {}).values():
        if mention in p.get("message", ""):
            return True
    return False
