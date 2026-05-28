"""TB-298: behavioral pinning for the `ap2 status` `attention:` cluster
line (text branch) + the `attention` block (JSON branch).

Companion to TB-282 (`render_attention_section` for the status-report
cron push), TB-296 (`web_attention._render_attention` for the
`/attention` pull page), and TB-297 (immediate-MM push). All four
surfaces consume the same `attention.detect_attention_conditions(cfg)`
detector entrypoint so a walk-away operator polling `ap2 status` from
a terminal sees the same conditions the web page / chat post /
status-report cron digest carry.

Pinned shape (mirrors the cross-surface contract):

  (a) Text branch omits the `attention:` line entirely when zero
      conditions are active (omit-on-empty discipline; quiet
      projects don't grow a zero-noise row — mirrors TB-258
      `audit:` / TB-260 `env stale` / TB-177 `janitor:`).
  (b) Text branch renders `attention:  N condition(s) — ...` when
      one to three conditions fire (cap is 3 inline).
  (c) Text branch caps at 3 bullets with `(+M more — ap2 web
      /attention)` suffix when more than three conditions are
      active (mirrors TB-151's pending-review-line truncation
      precedent; helper lives in `status_report.py`).
  (d) JSON branch ALWAYS carries an `attention` key with
      `{count, conditions}` shape — even when zero conditions
      fire — for parser stability (mirrors the TB-227
      `auto_approve` / TB-258 `audit` / TB-260 `env_stale` /
      TB-242 `active_focus` parser-stability promise).
  (e) JSON branch's `conditions` list is the FULL unfiltered
      detector output (no truncation — the cap is a text-render
      concern only).
  (f) The shared truncation helper `_format_attention_status_line`
      exists in `status_report.py` and is callable from outside the
      module so the CLI text render uses it directly.

Fixtures mirror TB-242 / TB-250 — `init_project` + a `cfg` pytest
fixture + monkeypatching the detector entrypoint with synthetic
`AttentionCondition` records so the test is insensitive to detector
internals.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import status_report
from ap2.components.attention import AttentionCondition
from ap2.config import Config
from ap2.init import init_project


# Direct reference to the shared helper so a refactor that moves /
# renames it surfaces cleanly on import (briefing scope item 4 +
# verification bullet 4).
_SHARED_HELPER = status_report._format_attention_status_line


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Fresh ap2 project scaffold — same shape as TB-242 / TB-250."""
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


def _stub_detect(conds: list[AttentionCondition]):
    """Build a (cfg, **_kwargs) -> conds stub so the monkeypatched
    detector ignores test-time invocation args (the CLI calls it as
    `_attention_mod.detect_attention_conditions(cfg)` — positional cfg,
    no kwargs — but a defensive **_kwargs keeps the stub resilient if
    the call site grows a `tail=` / `now=` injection later)."""

    def _stub(cfg, **_kwargs):  # noqa: ARG001 — accept and ignore cfg
        return conds

    return _stub


def _per_task(task_id: str, summary: str) -> AttentionCondition:
    """Shape-match what `_detect_task_stuck` / `_detect_task_frozen`
    emit: per-task detector with `extras['task']` populated."""
    return AttentionCondition(
        type="task_stuck",
        key=f"task_stuck:{task_id}",
        summary=summary,
        ts="2026-05-27T02:00:00Z",
        extras={
            "task": task_id,
            "title": "synthetic",
            "age_s": 3600,
            "start_ts": "2026-05-27T01:00:00Z",
            "threshold_s": 1800,
        },
    )


def _singleton(detector_type: str, summary: str) -> AttentionCondition:
    """Shape-match what `_detect_validator_judge_noisy` /
    `_detect_auto_approve_paused` / `_detect_cost_cap_approach` emit:
    singleton detector with no `extras['task']`."""
    return AttentionCondition(
        type=detector_type,
        key=detector_type,
        summary=summary,
        ts="2026-05-27T05:00:00Z",
        extras={},
    )


# ===========================================================================
# (a) Text branch omits the `attention:` line when zero conditions fire.
# ===========================================================================


def test_text_omits_attention_line_when_no_conditions(
    cfg: Config, capsys, monkeypatch,
):
    """Quiet project (zero detector hits) → no `attention:` line at all
    in the text output. Pins the omit-on-empty discipline (mirrors
    TB-258 `audit:` / TB-260 `env stale` / TB-177 `janitor:`)."""
    from ap2 import cli_daemon

    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect([]),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "attention:" not in out, out


# ===========================================================================
# (b) Text branch renders `attention:  N condition(s) — ...` for 1..3.
# ===========================================================================


def test_text_renders_attention_line_for_single_condition(
    cfg: Config, capsys, monkeypatch,
):
    """One condition → text-render line is
    `attention:  1 condition — <bullet>`. Singular form ("condition",
    not "conditions") gates on N==1 so the prose stays grammatical."""
    from ap2 import cli_daemon

    cond = _per_task(
        "TB-77",
        "TB-77 Active for 2.0h since 2026-05-27T04:00:00Z",
    )
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect([cond]),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "attention:" in out, out
    assert "1 condition —" in out, out
    # The per-task bullet leads with TB-N (no orphan summary text).
    assert "TB-77" in out, out
    # And the cap-suffix MUST NOT leak for single-condition input.
    assert "more — ap2 web /attention" not in out, out


def test_text_renders_attention_line_for_two_conditions(
    cfg: Config, capsys, monkeypatch,
):
    """Two conditions → text-render uses plural "conditions" and joins
    bullets with `; `. Mix per-task + singleton so the bullet shape
    contract carries both flavors."""
    from ap2 import cli_daemon

    conds = [
        _per_task(
            "TB-77",
            "TB-77 Active for 2.0h since 2026-05-27T04:00:00Z",
        ),
        _singleton(
            "validator_judge_noisy",
            "validator-judge noisy: 3+2=5 fails+timeouts in last 24h",
        ),
    ]
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect(conds),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "attention:" in out, out
    assert "2 conditions —" in out, out
    assert "TB-77" in out, out
    assert "validator-judge noisy" in out, out
    # Two-bullet input never trips the cap suffix.
    assert "more — ap2 web /attention" not in out, out


def test_text_renders_attention_line_for_three_conditions(
    cfg: Config, capsys, monkeypatch,
):
    """Three conditions sit exactly at the cap → all three render
    inline AND no `(+M more)` suffix leaks (the suffix triggers
    strictly above the cap)."""
    from ap2 import cli_daemon

    conds = [
        _per_task("TB-1", "TB-1 Active for 1.0h since X"),
        _per_task("TB-2", "TB-2 Active for 2.0h since Y"),
        _per_task("TB-3", "TB-3 Active for 3.0h since Z"),
    ]
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect(conds),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 conditions —" in out, out
    assert "TB-1" in out and "TB-2" in out and "TB-3" in out, out
    assert "more — ap2 web /attention" not in out, out


# ===========================================================================
# (c) Text branch caps at 3 with `(+M more — ap2 web /attention)`.
# ===========================================================================


def test_text_caps_attention_at_three_with_more_suffix(
    cfg: Config, capsys, monkeypatch,
):
    """Five conditions → the line shows the count (5), the first three
    bullets inline, and a `(+2 more — ap2 web /attention)` suffix
    pointing at the web pull-surface for the rest. Mirrors TB-151's
    pending-review-line truncation pattern."""
    from ap2 import cli_daemon

    conds = [
        _per_task("TB-1", "TB-1 Active for 1.0h since X"),
        _per_task("TB-2", "TB-2 Active for 2.0h since X"),
        _per_task("TB-3", "TB-3 Active for 3.0h since X"),
        _per_task("TB-4", "TB-4 Active for 4.0h since X"),
        _per_task("TB-5", "TB-5 Active for 5.0h since X"),
    ]
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect(conds),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "5 conditions —" in out, out
    # First three inline.
    assert "TB-1" in out and "TB-2" in out and "TB-3" in out, out
    # Last two suppressed from the text (truncated body — they only
    # surface via the web `/attention` page or the JSON branch).
    assert "TB-4" not in out, out
    assert "TB-5" not in out, out
    # And the cap-suffix carries the operator to the pull-surface.
    assert "(+2 more — ap2 web /attention)" in out, out


# ===========================================================================
# (d) JSON branch always carries the `attention` block (parser stability).
# ===========================================================================


def test_json_carries_attention_block_when_zero_conditions(
    cfg: Config, capsys, monkeypatch,
):
    """Zero conditions → JSON still carries `attention: {count: 0,
    conditions: []}` so machine consumers can pluck `.attention.count`
    without a `None` guard. Mirrors the TB-227 `auto_approve` /
    TB-258 `audit` / TB-260 `env_stale` parser-stability contract."""
    from ap2 import cli_daemon

    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect([]),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "attention" in payload
    assert payload["attention"] == {"count": 0, "conditions": []}


def test_json_attention_block_shape_for_per_task_condition(
    cfg: Config, capsys, monkeypatch,
):
    """A per-task condition surfaces in JSON with all four contracted
    keys (`task`, `type`, `key`, `summary`); `task` is the TB-N
    string."""
    from ap2 import cli_daemon

    cond = _per_task(
        "TB-77",
        "TB-77 Active for 2.0h since 2026-05-27T04:00:00Z",
    )
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect([cond]),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["attention"]
    assert block["count"] == 1
    assert len(block["conditions"]) == 1
    entry = block["conditions"][0]
    assert entry["task"] == "TB-77"
    assert entry["type"] == "task_stuck"
    assert entry["key"] == "task_stuck:TB-77"
    assert "TB-77 Active" in entry["summary"]


def test_json_attention_block_shape_for_singleton_condition(
    cfg: Config, capsys, monkeypatch,
):
    """Singleton detectors (no `extras['task']`) surface in JSON with
    `task: null` so machine consumers can branch on the discriminator
    without a missing-key fallback."""
    from ap2 import cli_daemon

    cond = _singleton(
        "validator_judge_noisy",
        "validator-judge noisy: 3+2=5 fails+timeouts in last 24h",
    )
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect([cond]),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    entry = payload["attention"]["conditions"][0]
    assert entry["task"] is None
    assert entry["type"] == "validator_judge_noisy"
    assert entry["key"] == "validator_judge_noisy"


# ===========================================================================
# (e) JSON branch's `conditions` list is unfiltered (no truncation).
# ===========================================================================


def test_json_conditions_list_is_unfiltered(
    cfg: Config, capsys, monkeypatch,
):
    """Five conditions → JSON carries all five entries in `conditions`,
    even though the text-render cap is 3. Truncation is a text-render
    concern only; JSON consumers may have their own rendering
    preferences (web cards, external monitors, etc.) and must see the
    full list."""
    from ap2 import cli_daemon

    conds = [_per_task(f"TB-{i}", f"TB-{i} Active for {i}.0h") for i in range(1, 6)]
    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions",
        _stub_detect(conds),
    )
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["attention"]
    assert block["count"] == 5
    assert len(block["conditions"]) == 5
    # Order preserved end-to-end (detector → CLI → JSON), so the first
    # and last TB-Ns line up where the detector put them.
    assert block["conditions"][0]["task"] == "TB-1"
    assert block["conditions"][-1]["task"] == "TB-5"


# ===========================================================================
# (f) Shared truncation helper exists + is callable from outside.
# ===========================================================================


def test_shared_helper_is_importable_and_callable():
    """`_format_attention_status_line` must exist in `status_report.py`
    and be callable from outside the module so the CLI text render uses
    it directly. The CLI's verification grep
    (`grep -Eq _format_attention_status_line ap2/cli_daemon.py`) pins
    the call-site; this test pins the helper's existence + signature so
    a refactor that renames it surfaces here loudly."""
    # Module-level reference loaded at import time — the assert here
    # documents the contract for the reader (callable, accepts a
    # condition list).
    assert callable(_SHARED_HELPER)
    # Empty input → empty string (caller decides whether to suppress
    # the wrapping prefix).
    assert _SHARED_HELPER([]) == ""


def test_shared_helper_empty_returns_empty_string():
    """Empty input → empty string. Caller-side suppression contract."""
    assert _SHARED_HELPER([]) == ""


def test_shared_helper_per_task_bullet_prefixes_with_tb_n():
    """A per-task condition renders as `TB-N <summary>` (no glyph, no
    em-dash — the CLI text-render uses the helper's body verbatim and
    wraps the `attention:  N condition(s) — ` prefix itself)."""
    cond = _per_task("TB-42", "TB-42 Active for 1.5h since X")
    body = _SHARED_HELPER([cond])
    # The bullet body leads with the TB-N anchor; the helper does NOT
    # double-prefix when the summary already mentions TB-N (the
    # summary is the operator-legible phrasing the detector chose).
    assert body.startswith("TB-42")


def test_shared_helper_singleton_bullet_is_bare_summary():
    """A singleton condition renders as just the summary — no TB-N
    anchor (the detector left `extras['task']` unset)."""
    cond = _singleton(
        "cost_cap_approach",
        "auto-approve cost cap approach: 7500 tokens used in last 24h",
    )
    body = _SHARED_HELPER([cond])
    assert body.startswith("auto-approve cost cap approach")
    assert "TB-" not in body


def test_shared_helper_respects_cap_with_more_suffix():
    """Above-cap input truncates with the `(+M more — ap2 web
    /attention)` suffix. The helper drives the truncation; the CLI
    text branch consumes the body verbatim."""
    conds = [_per_task(f"TB-{i}", f"summary {i}") for i in range(1, 6)]
    body = _SHARED_HELPER(conds, cap=3)
    # Three bullets present; last two absent from the truncated body.
    assert "TB-1" in body
    assert "TB-3" in body
    assert "TB-4" not in body
    assert "TB-5" not in body
    assert "(+2 more — ap2 web /attention)" in body


# ===========================================================================
# Cross-surface no-drift pin — both CLI surfaces consult the same
# detector entrypoint as TB-282 / TB-296 / TB-297.
# ===========================================================================


def test_cli_consumes_shared_detector_entrypoint(
    cfg: Config, capsys, monkeypatch,
):
    """Sanity: the CLI text branch goes through
    `attention.detect_attention_conditions` (the same entrypoint the
    status-report cron, web `/attention`, and immediate-MM push all
    consume). Pinning the call shape prevents a future refactor that
    inlines a separate detector copy here from drifting silently."""
    from ap2 import cli_daemon

    called: dict[str, int] = {"n": 0}

    def _spy(cfg, **_kwargs):  # noqa: ARG001
        called["n"] += 1
        return []

    monkeypatch.setattr(
        "ap2.components.attention.detect_attention_conditions", _spy,
    )
    # Run both branches — both must invoke the shared entrypoint.
    cli_daemon.cmd_status(cfg, Namespace(json=False))
    capsys.readouterr()
    cli_daemon.cmd_status(cfg, Namespace(json=True))
    capsys.readouterr()
    assert called["n"] == 2, called
