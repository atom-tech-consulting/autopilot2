"""TB-299: regression pins for the home-page `_render_attention_card`
sibling to the TB-296 `/attention` page.

Companion to TB-296 (`/attention` pull surface), TB-282 (status-report
push), TB-297 (immediate Mattermost push), and TB-298 (`ap2 status`
text/JSON render) — all five operator-facing surfaces consume the SAME
`attention.detect_attention_conditions(cfg)` entrypoint, so the home
card's bullet shape and empty-state discipline must match the
established cross-surface contract.

Pins:
  (a) home page omits the card entirely when `detect_attention_conditions`
      returns [] — no heading, no body, no zero-noise.
  (b) home page renders the card with operator-legible bullets when
      conditions fire (warn-glyph ⚠ + bold TB-N + em-dash + summary).
  (c) bullets cap at 3 with `(+M more — see /attention)` link-tail
      when more than 3 conditions fire (mirrors TB-298's CLI cap).
  (d) per-task bullets link to `/task/<TB-N>` so the operator can
      click through to the detail page from the home summary.
  (e) detector exception is swallowed and rendered as a tinted notice
      rather than 500-ing the home page (mirror of
      `web_attention._render_attention`'s contract).
  (f) card sits BETWEEN the focus card (TB-242) and the automation
      card (TB-227) in the rendered HTML — the operator-attention
      cluster orders by urgency (attention is the most actionable).
"""
from __future__ import annotations

import pytest

from ap2 import web
from ap2.components.attention import AttentionCondition
from ap2.config import Config


# --------- helpers --------------------------------------------------------


def _per_task_cond(task_id: str, summary: str | None = None) -> AttentionCondition:
    """Shape-match the `_detect_task_stuck` / `_detect_task_frozen`
    output: per-task detector with `extras['task']` populated."""
    return AttentionCondition(
        type="task_stuck",
        key=f"task_stuck:{task_id}",
        summary=summary or f"{task_id} Active for 4.0h since 2026-05-27T02:00:00Z",
        ts="2026-05-27T02:00:00Z",
        extras={
            "task": task_id,
            "title": "synthetic stuck task",
            "age_s": 14400,
            "start_ts": "2026-05-27T02:00:00Z",
            "threshold_s": 3600,
        },
    )


def _singleton_cond(detector_type: str, summary: str) -> AttentionCondition:
    """Shape-match the `_detect_validator_judge_noisy` /
    `_detect_auto_approve_paused` / `_detect_cost_cap_approach`
    output: singleton detector with no `extras['task']`."""
    return AttentionCondition(
        type=detector_type,
        key=detector_type,
        summary=summary,
        ts="2026-05-27T05:00:00Z",
        extras={},
    )


def _stub_detect(conds: list[AttentionCondition]):
    """Build a `(cfg, **_kwargs) -> conds` stub. The home renderer
    calls `_attention.detect_attention_conditions(cfg)` positionally —
    `**_kwargs` keeps the stub resilient if the call site ever grows
    `tail=` / `now=` injection points (today only the daemon's debounce
    pass passes those)."""

    def _stub(cfg, **_kwargs):  # noqa: ARG001 — accept and ignore cfg
        return conds

    return _stub


# --------- (a) Omit-on-empty discipline ------------------------------------


def test_home_omits_attention_card_when_no_conditions(
    project: Config, monkeypatch,
):
    """Zero conditions → the attention card is omitted entirely (no
    heading, no body) so a quiet project's home page stays clean.
    Mirrors `_render_focus_card` / `_render_automation_card`'s
    omit-on-empty discipline."""
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _stub_detect([]),
    )
    html_out = web._render_home(project)
    assert "<!DOCTYPE html>" in html_out
    # No card markers at all — neither the heading nor the wrapper div.
    assert "attention-card" not in html_out
    assert "<h2>Attention" not in html_out


# --------- (b) Bullet rendering shape --------------------------------------


def test_home_renders_attention_card_with_bullets(
    project: Config, monkeypatch,
):
    """Conditions fire → card renders with the documented bullet shape:
    warn-glyph ⚠ + bold TB-N + em-dash + detector-supplied summary.
    Mirrors `web_attention._render_attention`'s per-task bullet shape
    so the home summary and the dedicated page agree byte-for-byte
    on bullet text (within the link-wrapping difference)."""
    cond = _per_task_cond(
        "TB-77", "TB-77 Active for 4.2h since 2026-05-27T02:00:00Z",
    )
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _stub_detect([cond]),
    )
    html_out = web._render_home(project)
    assert 'class="attention-card"' in html_out
    # Heading shape: `<h2>Attention <span class="meta">(N)</span></h2>`.
    assert "<h2>Attention" in html_out
    assert "(1)" in html_out
    # Bullet shape: warn-glyph + bold TB-N + em-dash + summary.
    assert "⚠" in html_out
    assert "<strong>" in html_out
    assert "TB-77" in html_out
    assert "—" in html_out
    assert "Active for 4.2h" in html_out


def test_home_attention_card_singleton_renders_bare_bullet(
    project: Config, monkeypatch,
):
    """A singleton detector (no `extras['task']`) renders a bare
    `⚠ <summary>` bullet — no orphaned `<strong>` or TB-N markup
    sitting next to empty content. Mirrors `web_attention`'s
    singleton-bullet shape."""
    cond = _singleton_cond(
        "validator_judge_noisy",
        "validator-judge noisy: 3+2=5 fails+timeouts in last 24h "
        "(threshold 5); see `ap2 status` or /usage",
    )
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _stub_detect([cond]),
    )
    html_out = web._render_home(project)
    # Card present.
    assert 'class="attention-card"' in html_out
    assert "validator-judge noisy" in html_out
    # No orphan bold TB-N inside the card — the singleton has no
    # per-task anchor. Scope to the attention card body so a TB-N
    # mention elsewhere on the home page (e.g. events table) doesn't
    # false-positive.
    card_start = html_out.find('class="attention-card"')
    card_end = html_out.find("</div>", card_start)
    card_body = html_out[card_start:card_end]
    assert "<strong>TB-" not in card_body, card_body


# --------- (c) Inline cap + (+M more) link-tail ---------------------------


def test_home_attention_card_caps_bullets_at_three(
    project: Config, monkeypatch,
):
    """More than 3 conditions → first 3 render as bullets, remainder
    collapse into a `(+M more — see /attention)` link-tail. Mirror of
    TB-298's CLI cap (3 inline) so an operator alternating between
    `ap2 status` and the home page sees consistent shape across both
    summary surfaces."""
    conds = [
        _per_task_cond(f"TB-{i}", f"TB-{i} Active for 4.0h")
        for i in range(1, 6)  # 5 conditions
    ]
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _stub_detect(conds),
    )
    html_out = web._render_home(project)
    # Heading counter reflects the FULL count (5), not the truncated 3.
    assert "(5)" in html_out
    # First 3 TB-Ns are bullet text.
    for i in (1, 2, 3):
        assert f"TB-{i}" in html_out
    # Link-tail surfaces the remainder count + the /attention drilldown.
    assert "(+2 more" in html_out
    assert 'href="/attention"' in html_out
    # The tail-li is dedicated markup so we can spot it visually.
    assert 'class="att-more"' in html_out


# --------- (d) Per-task bullet link-through to /task/<TB-N> ---------------


def test_home_attention_card_per_task_bullet_links_to_task(
    project: Config, monkeypatch,
):
    """Per-task bullets wrap the bold TB-N in an `/task/<TB-N>` anchor
    so the operator can click through from the home summary to the
    detail page in one step. Mirror of the TB-296 `/events` row
    link-through pattern (event rows for `attention_raised` link
    through to `/attention`)."""
    cond = _per_task_cond("TB-42")
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _stub_detect([cond]),
    )
    html_out = web._render_home(project)
    assert 'href="/task/TB-42"' in html_out
    # Confirm the link wraps the TB-N inside the bold span — not just
    # the link itself but its position inside `<strong>...</strong>`.
    assert "<strong><a href=\"/task/TB-42\">TB-42</a></strong>" in html_out


# --------- (e) Detector exception is swallowed (no 500) -------------------


def test_home_attention_card_detector_exception_renders_notice(
    project: Config, monkeypatch,
):
    """A detector exception is swallowed and rendered as a tinted
    notice — the home page must never 500 because one detector
    errored. Mirrors `web_attention._render_attention`'s
    swallow-on-error contract."""

    def _raise(cfg, **_kwargs):  # noqa: ARG001
        raise RuntimeError("synthetic detector explosion")

    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _raise,
    )
    html_out = web._render_home(project)
    # Page rendered — no exception propagated.
    assert "<!DOCTYPE html>" in html_out
    # Tinted notice surfaces the failure mode so the operator can
    # debug without checking server logs.
    assert "detector error" in html_out
    assert "RuntimeError" in html_out
    assert "synthetic detector explosion" in html_out
    # The notice uses the existing `automation-status is-paused`
    # palette (reused via `attention-card-error`) so the surface
    # error is part of the same visual "needs attention" cluster.
    assert "attention-card-error" in html_out


# --------- (f) Relative-order assertion in the rendered HTML --------------


def test_home_attention_card_sits_between_focus_and_automation(
    project: Config, monkeypatch,
):
    """The attention card sits BETWEEN the focus card (TB-242) and the
    automation card (TB-227) in the rendered HTML. The operator-
    attention cluster orders by urgency: attention conditions name a
    specific condition needing eyes (most actionable); focus and
    automation are state cards."""
    # Force the focus card to render — needs a `## Current focus:`
    # heading in goal.md.
    (project.project_root / "goal.md").write_text(
        "# Project Goals\n\n"
        "## Mission\n\n"
        "Drive the project.\n\n"
        "## Done when\n\n"
        "- top-level done.\n\n"
        "## Current focus: bootstrap\n\n"
        "bootstrap body.\n\n"
        "## Non-goals\n\n"
        "- none.\n",
    )
    # Force the automation card to render — `AP2_AUTO_APPROVE=1`
    # flips `state["auto_approve_enabled"]` truthy so the card is
    # not omitted-on-empty.
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_APPROVE_ENABLED", "1")
    # Force the attention card to render with at least one bullet.
    cond = _per_task_cond("TB-9", "TB-9 Active for 2.0h")
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _stub_detect([cond]),
    )

    html_out = web._render_home(project)

    # All three cards present.
    pos_focus = html_out.find(">Focus<")
    pos_attention = html_out.find('class="attention-card"')
    pos_automation = html_out.find(">Auto-approve<")

    assert pos_focus >= 0, "focus card missing from rendered HTML"
    assert pos_attention >= 0, "attention card missing from rendered HTML"
    assert pos_automation >= 0, "automation card missing from rendered HTML"

    # Relative order: focus < attention < automation.
    assert pos_focus < pos_attention, (
        f"focus card ({pos_focus}) must appear before "
        f"attention card ({pos_attention})"
    )
    assert pos_attention < pos_automation, (
        f"attention card ({pos_attention}) must appear before "
        f"automation card ({pos_automation})"
    )
