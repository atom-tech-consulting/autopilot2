"""E2E tests for the idle watchdog (TB-71) — `daemon._maybe_auto_diagnose`.

Drives the watchdog with an injected fake `_mm_post` so no network is touched.
Uses a manually-constructed `now` parameter for deterministic clock control
rather than the e2e `clock` fixture (which monkeypatches `cron.time`).
"""
from __future__ import annotations

import time
from pathlib import Path

from ap2 import daemon, events, tools
# TB-389: the watchdog is now event-driven — `_maybe_auto_diagnose`
# ENQUEUES a digest onto the `ap2.notify` queue and the communication
# component's outbound tick (`run_outbound_tick`) delivers it. Tests
# drive the tick explicitly then assert on the captured `_mm_post` calls.
from ap2.components.communication import run_outbound_tick


def _seed_meaningful_event(cfg, *, ts_offset_s: float, now: float) -> None:
    """Append a fake `task_complete` event whose ts is `ts_offset_s` ago.

    `events.append` always uses real-time `now()`, so we patch the file
    directly to inject the desired timestamp.
    """
    import datetime as dt
    import json

    iso = (
        dt.datetime.fromtimestamp(now - ts_offset_s, tz=dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    line = json.dumps({
        "ts": iso,
        "type": "task_complete",
        "task": "TB-1",
        "status": "complete",
    })
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with cfg.events_file.open("a") as f:
        f.write(line + "\n")


def _capture_posts(monkeypatch) -> list[tuple[str, str]]:
    """Replace `tools._mm_post` with a capture function. Returns the list."""
    captured: list[tuple[str, str]] = []

    def fake_post(channel: str, text: str, thread_id: str = "") -> str:
        captured.append((channel, text))
        return f"fake-post-{len(captured)}"

    monkeypatch.setattr(tools, "_mm_post", fake_post)
    return captured


def test_watchdog_fires_after_threshold(e2e_project, monkeypatch):
    """Past-threshold idle → digest ENQUEUED + `auto_diagnose_fired`; the
    communication tick then delivers it to the configured channel."""
    cfg = e2e_project()
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")
    posts = _capture_posts(monkeypatch)

    now = time.time()
    # Last meaningful event was 4 hours ago; threshold is 3h default.
    _seed_meaningful_event(cfg, ts_offset_s=4 * 3600, now=now)

    daemon._maybe_auto_diagnose(cfg, now=now)
    # TB-389: enqueue happened; the communication tick delivers it. The
    # Mattermost channel adapter resolves its destination from
    # `AP2_MM_CHANNELS[0]` since the notification carries no channel hint.
    run_outbound_tick(cfg)

    assert len(posts) == 1
    assert posts[0][0] == "ch_aaa"
    assert "ap2 watchdog" in posts[0][1]

    evts = events.tail(cfg.events_file, 30)
    fired = [e for e in evts if e["type"] == "auto_diagnose_fired"]
    assert len(fired) == 1
    assert fired[0]["idle_s"] >= 4 * 3600


def test_watchdog_respects_cooldown(e2e_project, monkeypatch):
    """A second tick within the cooldown window does NOT re-post."""
    cfg = e2e_project()
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")
    posts = _capture_posts(monkeypatch)

    now = time.time()
    _seed_meaningful_event(cfg, ts_offset_s=4 * 3600, now=now)

    # First fire at t=now; second tick 1 hour later (cooldown is 6h) —
    # the second call is inside the cooldown, so only ONE digest is
    # enqueued. Delivering the queue yields exactly one post.
    daemon._maybe_auto_diagnose(cfg, now=now)
    daemon._maybe_auto_diagnose(cfg, now=now + 3600)
    run_outbound_tick(cfg)

    assert len(posts) == 1


def test_watchdog_re_fires_after_cooldown(e2e_project, monkeypatch):
    """A third tick past the cooldown re-posts when still idle."""
    cfg = e2e_project()
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")
    posts = _capture_posts(monkeypatch)

    now = time.time()
    _seed_meaningful_event(cfg, ts_offset_s=4 * 3600, now=now)

    daemon._maybe_auto_diagnose(cfg, now=now)
    # 7 hours later — past the 6h cooldown, still idle (no new meaningful events).
    daemon._maybe_auto_diagnose(cfg, now=now + 7 * 3600)
    # Two digests enqueued (past-cooldown re-fire) → two delivered posts.
    run_outbound_tick(cfg)

    assert len(posts) == 2


def test_watchdog_skip_when_no_channels(e2e_project, monkeypatch):
    """No AP2_MM_CHANNELS → the digest is enqueued but the communication
    tick finds no channel, so it delivers NOTHING (the notification stays
    pending for a channel that may be configured later). TB-389 dropped
    the watchdog's sticky `auto_diagnose_no_destination` warning — the
    no-destination concern moved to the communication component, which
    simply holds the backlog."""
    cfg = e2e_project()
    # AP2_MM_CHANNELS is scrubbed by the e2e fixture, so it's already absent.
    posts = _capture_posts(monkeypatch)

    now = time.time()
    _seed_meaningful_event(cfg, ts_offset_s=4 * 3600, now=now)

    daemon._maybe_auto_diagnose(cfg, now=now)
    daemon._maybe_auto_diagnose(cfg, now=now + 60)
    daemon._maybe_auto_diagnose(cfg, now=now + 120)
    run_outbound_tick(cfg)

    # No channel configured → no post. The watchdog no longer emits a
    # no-destination warning.
    assert posts == []
    evts = events.tail(cfg.events_file, 30)
    nodest = [e for e in evts if e["type"] == "auto_diagnose_no_destination"]
    assert nodest == [], "TB-389 removed the no-destination warning"


def test_watchdog_resets_on_meaningful_event(e2e_project, monkeypatch):
    """A fresh meaningful event (e.g. task_complete) re-baselines idle time
    so the watchdog doesn't fire on the next tick."""
    cfg = e2e_project()
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")
    posts = _capture_posts(monkeypatch)

    now = time.time()
    # Meaningful event 1 minute ago — well under threshold.
    _seed_meaningful_event(cfg, ts_offset_s=60, now=now)

    daemon._maybe_auto_diagnose(cfg, now=now)

    assert posts == []  # not idle long enough
    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "auto_diagnose_fired" for e in evts)


def test_watchdog_does_not_fire_on_first_tick_after_resume(e2e_project, monkeypatch):
    """Backward-compat for stoch: a paused daemon resuming with only a
    `daemon_start` event in events.jsonl must not trip the watchdog, even if
    the previous session's last task_complete is hours old.

    Scenario: events.jsonl has `task_complete` from 1 day ago + a fresh
    `daemon_start` from "now". `daemon_start` is in the meaningful set, so
    `since_last_activity_s` resolves to ~0, well under threshold.
    """
    cfg = e2e_project()
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")
    posts = _capture_posts(monkeypatch)

    now = time.time()
    # Old work day-ago + fresh resume marker.
    _seed_meaningful_event(cfg, ts_offset_s=86400, now=now)
    import datetime as dt
    import json
    iso_now = (
        dt.datetime.fromtimestamp(now - 5, tz=dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    with cfg.events_file.open("a") as f:
        f.write(json.dumps({"ts": iso_now, "type": "daemon_start", "pid": 1}) + "\n")

    daemon._maybe_auto_diagnose(cfg, now=now)

    assert posts == []
    evts = events.tail(cfg.events_file, 30)
    assert all(e["type"] != "auto_diagnose_fired" for e in evts)


def test_watchdog_recovers_after_destination_set(e2e_project, monkeypatch):
    """A digest enqueued while no channel is configured stays PENDING and
    delivers once a channel appears (TB-389 communication delivery
    policy). So the backlog is not lost when the operator wires up
    `AP2_MM_CHANNELS` after a fresh install."""
    cfg = e2e_project()
    posts = _capture_posts(monkeypatch)

    now = time.time()
    _seed_meaningful_event(cfg, ts_offset_s=4 * 3600, now=now)

    # First tick with no destination → digest enqueued, nothing delivered.
    daemon._maybe_auto_diagnose(cfg, now=now)
    run_outbound_tick(cfg)
    assert posts == []

    # Operator sets the channel; the next communication tick delivers the
    # still-pending backlog digest. The second `_maybe_auto_diagnose` is
    # inside the cooldown so it enqueues nothing new — the delivered post
    # is the digest queued on the first tick.
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")
    daemon._maybe_auto_diagnose(cfg, now=now + 60)
    run_outbound_tick(cfg)

    assert len(posts) == 1


def test_watchdog_handles_post_failure(e2e_project, monkeypatch):
    """TB-389: a delivery failure happens in the communication tick, not
    the watchdog. The watchdog still ENQUEUES (emitting
    `auto_diagnose_fired`); the failing delivery emits
    `notification_error` and LEAVES the notification pending so the next
    tick retries (a second delivery attempt → a second error)."""
    cfg = e2e_project()
    monkeypatch.setenv("AP2_MM_CHANNELS", "ch_aaa")

    def boom(channel, text, thread_id=""):
        raise RuntimeError("network down")

    monkeypatch.setattr(tools, "_mm_post", boom)

    now = time.time()
    _seed_meaningful_event(cfg, ts_offset_s=4 * 3600, now=now)

    daemon._maybe_auto_diagnose(cfg, now=now)
    run_outbound_tick(cfg)

    evts = events.tail(cfg.events_file, 30)
    err = [e for e in evts if e["type"] == "notification_error"]
    fired = [e for e in evts if e["type"] == "auto_diagnose_fired"]
    assert len(err) == 1
    # The digest WAS queued (auto_diagnose_fired fires at enqueue time).
    assert len(fired) == 1
    # The notification stayed pending (delivery failed) → a second
    # delivery attempt retries and fails again.
    run_outbound_tick(cfg)
    err2 = [e for e in events.tail(cfg.events_file, 60)
            if e["type"] == "notification_error"]
    assert len(err2) == 2
