"""Tests for ap2/mattermost.py — first-poll seed + thread-mention cache."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import mattermost
from ap2.config import Config


@pytest.fixture(autouse=True)
def _mm_env(monkeypatch, tmp_path):
    """Point the module at a test channel + mention, with dummy creds."""
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example/")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch1")
    monkeypatch.setenv("AP2_MM_BOT_USER_ID", "bot-u")
    monkeypatch.setenv("AP2_MM_MENTION", "@bot")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    return cfg_


def _channel_payload(post_ids: list[str], posts: dict) -> dict:
    """Mattermost API shape: order is newest-first."""
    return {"order": post_ids, "posts": posts}


# ---------------- first-poll seed ----------------


def test_first_poll_seeds_cursor_and_returns_empty(cfg, monkeypatch):
    calls = []

    def fake_get(url, token, path):
        calls.append(path)
        return _channel_payload(
            ["p3", "p2", "p1"],
            {
                "p1": {"id": "p1", "user_id": "u1", "message": "@bot hello", "create_at": 100},
                "p2": {"id": "p2", "user_id": "u1", "message": "@bot and again", "create_at": 200},
                "p3": {"id": "p3", "user_id": "u1", "message": "@bot newest", "create_at": 300},
            },
        )

    monkeypatch.setattr(mattermost, "_api_get", fake_get)
    out = mattermost.check_new_messages(cfg)
    assert out == []

    state = json.loads(cfg.mm_state_file.read_text())
    assert state["cursors"]["ch1"] == "p3"
    assert state["cursors"]["ch1:ts"] == 300


def test_second_poll_after_seed_returns_only_new(cfg, monkeypatch):
    # Pre-seed state as if the first poll already ran.
    cfg.mm_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.mm_state_file.write_text(
        json.dumps(
            {
                "cursors": {"ch1": "p1", "ch1:ts": 100},
                "channel_names": {"ch1": "town-square"},
                "users": {"u1": "alice"},
                "thread_mentions": {},
            }
        )
    )

    def fake_get(url, token, path):
        if "/posts" in path and "/thread" not in path:
            return _channel_payload(
                ["p3", "p2", "p1"],
                {
                    "p1": {"id": "p1", "user_id": "u1", "message": "@bot hi", "create_at": 100},
                    "p2": {"id": "p2", "user_id": "u1", "message": "@bot two", "create_at": 200, "channel_id": "ch1"},
                    "p3": {"id": "p3", "user_id": "u1", "message": "@bot three", "create_at": 300, "channel_id": "ch1"},
                },
            )
        # Channel / user lookups for _normalize
        if "/channels/ch1" in path:
            return {"name": "town-square"}
        if "/users/u1" in path:
            return {"username": "alice"}
        return {}

    monkeypatch.setattr(mattermost, "_api_get", fake_get)
    out = mattermost.check_new_messages(cfg)
    ids = [m["id"] for m in out]
    assert ids == ["p2", "p3"]  # oldest-first


# ---------------- thread-mention cache ----------------


def test_thread_mention_cache_hits_on_repeat(monkeypatch):
    calls = []

    def fake_get(url, token, path):
        calls.append(path)
        return {
            "posts": {
                "r1": {"id": "r1", "message": "@bot please look"},
                "r2": {"id": "r2", "message": "sure"},
            }
        }

    monkeypatch.setattr(mattermost, "_api_get", fake_get)
    cache = {}
    assert mattermost._thread_has_mention("u", "t", "root1", "@bot", cache) is True
    assert mattermost._thread_has_mention("u", "t", "root1", "@bot", cache) is True
    assert len(calls) == 1


def test_thread_mention_cache_separate_roots(monkeypatch):
    calls = []

    def fake_get(url, token, path):
        calls.append(path)
        return {"posts": {"p": {"id": "p", "message": "@bot x"}}}

    monkeypatch.setattr(mattermost, "_api_get", fake_get)
    cache = {}
    assert mattermost._thread_has_mention("u", "t", "a", "@bot", cache) is True
    assert mattermost._thread_has_mention("u", "t", "b", "@bot", cache) is True
    assert len(calls) == 2


def test_thread_mention_cache_caches_false(monkeypatch):
    """Non-mention threads are cached too, so we don't re-fetch them every tick."""
    calls = []

    def fake_get(url, token, path):
        calls.append(path)
        return {"posts": {"p": {"id": "p", "message": "no mention here"}}}

    monkeypatch.setattr(mattermost, "_api_get", fake_get)
    cache = {}
    assert mattermost._thread_has_mention("u", "t", "r", "@bot", cache) is False
    assert mattermost._thread_has_mention("u", "t", "r", "@bot", cache) is False
    assert len(calls) == 1


def test_thread_cache_trim(monkeypatch):
    cache = {f"r{i}": True for i in range(5)}
    mattermost._trim_cache(cache, max_size=3)
    # Oldest-insertion-order keys dropped: r0, r1.
    assert set(cache.keys()) == {"r2", "r3", "r4"}


def test_mention_filter_still_works_post_seed(cfg, monkeypatch):
    """After seeding, non-mention messages with no threaded mention are dropped."""
    cfg.mm_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.mm_state_file.write_text(
        json.dumps(
            {
                "cursors": {"ch1": "p_old", "ch1:ts": 50},
                "channel_names": {"ch1": "town-square"},
                "users": {"u1": "alice"},
                "thread_mentions": {},
            }
        )
    )

    def fake_get(url, token, path):
        if "/channels/ch1/posts" in path:
            return _channel_payload(
                ["p_new", "p_old"],
                {
                    "p_new": {
                        "id": "p_new",
                        "user_id": "u1",
                        "message": "chit-chat, no mention",
                        "create_at": 100,
                        "channel_id": "ch1",
                        "root_id": "",
                    },
                    "p_old": {"id": "p_old", "message": "@bot prior", "create_at": 50},
                },
            )
        if "/channels/ch1" in path:
            return {"name": "town-square"}
        if "/users/u1" in path:
            return {"username": "alice"}
        return {}

    monkeypatch.setattr(mattermost, "_api_get", fake_get)
    out = mattermost.check_new_messages(cfg)
    assert out == []
