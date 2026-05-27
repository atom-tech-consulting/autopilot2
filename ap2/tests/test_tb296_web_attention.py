"""TB-296: regression pins for the `/attention` pull-surface.

Companion to the status-report cron's push-surface render of TB-282's
`## Attention needed` bullets — both pages consume the same
`attention.detect_attention_conditions(cfg)` entrypoint so push and
pull never disagree about what's currently active.

Pins:
  1. `/attention` route registers in `make_app().routes`.
  2. The page renders the explicit empty-state when zero detectors fire
     (rather than a blank page — operator must be able to distinguish
     "loaded, nothing wrong" from "page broken").
  3. A synthetic per-task `AttentionCondition` (monkeypatched detector
     entrypoint) renders one bullet matching the documented shape:
     warn-glyph ⚠ + bold TB-N + em-dash + detector summary.
  4. Singleton-detector conditions (no `extras['task']`) render bare
     `⚠ <summary>` bullets — no orphaned TB-N markup.
  5. Nav bar HTML on `/` contains an `/attention` link.
  6. `/events` row for an `attention_raised` event renders an
     `/attention` link tied to the TB-N anchor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import events as ev_mod, web
from ap2.attention import AttentionCondition
from ap2.config import Config


# --------- (1) Route registration -----------------------------------------


def test_attention_route_registers_in_make_app():
    """The `/attention` route must appear in `make_app().routes` — that
    list is what the verification gate + introspection tooling walks to
    discover the public surface."""
    app = web.make_app()
    paths = [r.path for r in app.routes]
    assert "/attention" in paths, paths


# --------- (2) Empty-state branch -----------------------------------------


def test_attention_empty_state_when_no_conditions(project: Config, monkeypatch):
    """Zero attention conditions → explicit empty-state phrasing, NOT a
    blank page. Distinguishes "page loaded, nothing wrong" from "page
    broken" — the briefing's load-bearing UX contract.
    """
    # `project` fixture seeds a clean board with no Active task; pin the
    # detector to empty so the test isn't sensitive to future detectors.
    monkeypatch.setattr(
        "ap2.web_attention._attention.detect_attention_conditions",
        lambda cfg, **_kwargs: [],
    )
    html = web._render_attention(project)
    assert "<!DOCTYPE html>" in html
    assert "No attention conditions currently active." in html
    # And no orphaned bullet markup.
    assert "<ul" not in html or "attention-conditions" not in html


# --------- (3) Per-task condition rendering -------------------------------


def test_attention_renders_per_task_bullet(project: Config, monkeypatch):
    """A synthetic `task_stuck`-shaped condition renders one bullet with
    warn-glyph + bold TB-N + em-dash + summary — the documented shape
    that matches `status_report.render_attention_section`'s output."""
    cond = AttentionCondition(
        type="task_stuck",
        key="task_stuck:TB-99",
        summary="TB-99 Active for 4.2h since 2026-05-27T02:00:00Z",
        ts="2026-05-27T02:00:00Z",
        extras={
            "task": "TB-99",
            "title": "Synthetic stuck task",
            "age_s": 15120,
            "start_ts": "2026-05-27T02:00:00Z",
            "threshold_s": 3600,
        },
    )
    monkeypatch.setattr(
        "ap2.web_attention._attention.detect_attention_conditions",
        lambda cfg, **_kwargs: [cond],
    )
    html = web._render_attention(project)
    assert "⚠" in html
    # Bold TB-N anchor present.
    assert "<strong>TB-99</strong>" in html
    # Em-dash separator + detector-supplied summary text.
    assert "—" in html
    assert "Active for 4.2h" in html
    # Header shows the active-condition counter so the page has a
    # human-readable headline.
    assert "1 condition active" in html


# --------- (4) Singleton-detector condition rendering ---------------------


def test_attention_renders_singleton_bullet(project: Config, monkeypatch):
    """A singleton detector (no `extras['task']`) renders a bare
    `⚠ <summary>` bullet — no orphaned `<strong>` or em-dash sitting
    next to empty markup."""
    cond = AttentionCondition(
        type="validator_judge_noisy",
        key="validator_judge_noisy",
        summary=(
            "validator-judge noisy: 3+2=5 fails+timeouts in last 24h "
            "(threshold 5); see `ap2 status` or /usage"
        ),
        ts="2026-05-27T05:00:00Z",
        extras={
            "fail_count_24h": 3,
            "timeout_count_24h": 2,
            "threshold": 5,
            "window_s": 86400,
        },
    )
    monkeypatch.setattr(
        "ap2.web_attention._attention.detect_attention_conditions",
        lambda cfg, **_kwargs: [cond],
    )
    html = web._render_attention(project)
    assert "⚠" in html
    assert "validator-judge noisy" in html
    # No orphan bold TB-N — singleton condition has no task anchor.
    assert "<strong>TB-" not in html
    # Header counter still present.
    assert "1 condition active" in html


# --------- (5) Nav bar link ----------------------------------------------


def test_nav_bar_contains_attention_link(project: Config):
    """The shared nav (in `web_chrome._layout`) must list `/attention` so
    the page is one click away from every other page in the UI — that's
    the whole pull-surface promise."""
    html = web._render_home(project)
    assert 'href="/attention"' in html


# --------- (6) /events row link for attention_raised ----------------------


def test_events_row_for_attention_raised_links_to_attention(
    project: Config,
):
    """When an `attention_raised` event is in the tail, the `/events`
    row's summary cell must contain an `/attention` link tied to the
    TB-N anchor so an operator clicking through from the event log
    lands on the current-state surface (TB-296 briefing scope #5)."""
    ev_mod.append(
        project.events_file,
        "attention_raised",
        attention_type="task_stuck",
        key="task_stuck:TB-77",
        summary="TB-77 Active for 2.0h since 2026-05-27T04:00:00Z",
        task="TB-77",
        title="Synthetic stuck task",
        age_s=7200,
        start_ts="2026-05-27T04:00:00Z",
        threshold_s=3600,
    )
    html = web._render_events(project, typ="attention_raised", n=50)
    assert "attention_raised" in html
    assert 'href="/attention"' in html
    # The TB-N is anchored inside the link so an operator scans for the
    # task id and follows it to the pull-surface in one click.
    assert "TB-77" in html


def test_events_row_singleton_attention_links_to_attention(
    project: Config,
):
    """Singleton-detector `attention_raised` rows (no `task` field —
    e.g. `validator_judge_noisy`, `cost_cap_approach`) still link to
    `/attention` via the detector-type discriminator so the
    pull-surface entry-point isn't lost just because the condition has
    no per-task anchor."""
    ev_mod.append(
        project.events_file,
        "attention_raised",
        attention_type="validator_judge_noisy",
        key="validator_judge_noisy",
        summary="validator-judge noisy: 3+2=5 fails+timeouts in last 24h",
        fail_count_24h=3,
        timeout_count_24h=2,
        threshold=5,
        window_s=86400,
    )
    html = web._render_events(project, typ="attention_raised", n=50)
    assert "attention_raised" in html
    assert 'href="/attention"' in html
    # No `task=TB-...` field in the payload — link still surfaces via
    # the attention_type discriminator.
    assert "validator_judge_noisy" in html


# --------- HTTP-level smoke (status + content-type) -----------------------


def test_attention_get_returns_200_text_html(project: Config):
    """End-to-end: GET `/attention` returns 200 + content-type
    `text/html`. Drives the real `_Handler` so the route + dispatcher
    + render are exercised together."""
    import socket as _sock
    import threading
    import urllib.request

    # Use the same `_bind_with_enumeration` + `_ThreadingTCPServer`
    # plumbing the daemon uses, but block-on-port locally so we don't
    # contend with the test session's ambient daemon (if any).
    s = _sock.socket()
    try:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    finally:
        s.close()

    srv, bound = web._build_server(project, "127.0.0.1", port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{bound}/attention", timeout=5.0,
        )
        status = resp.status
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read().decode()
        resp.close()
        assert status == 200
        assert ctype.startswith("text/html")
        assert "<!DOCTYPE html>" in body
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
