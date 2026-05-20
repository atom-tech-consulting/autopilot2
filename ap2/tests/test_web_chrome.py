"""TB-267: chrome helpers tests — mirror of `ap2/web_chrome.py`.

Relocated from `ap2/tests/test_web.py` by the TB-267 split. Each test body
is byte-identical to its pre-TB-267 original; only the module home and the
shared `project` fixture + `_seed_run` helper's location (now
`ap2/tests/conftest.py`) changed.

Covers low-level helpers owned by `ap2/web_chrome.py`:
  - `_row_class` status-aware tinting (TB-148).
  - `_event_extra` event-detail rendering.
  - `_find_run_id_for_event` / `_terminal_event_for_run` debug-file lookup.
  - `_read_jsonl` partial-line tolerance + `since=` cursor.
  - `_events_table` compact-usage row formatting (TB-179 / TB-180).
  - `summarize_verification_failed` cross-surface grep visibility (TB-158).
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from ap2 import web, events as ev_mod
from ap2.config import Config
from ap2.tests.conftest import _seed_run


# --------- TB-148: status-aware tinting for task_complete rows ---------


def test_row_class_task_complete_status_aware():
    """`task_complete` row class reads `status` so the operator can tell
    a passing run from a failure-mode at a glance, without expanding the
    row. `complete` keeps the green lifecycle class today's UI already
    uses; failure modes reuse the existing `failure` (red) and `warning`
    (orange) classes; `retry_exhausted` and unknown/missing statuses get
    their own dedicated `frozen` and `neutral` classes."""
    # Happy path → green (lifecycle).
    assert web._row_class({"type": "task_complete", "status": "complete"}) \
        == "lifecycle"
    # Pipeline-pending is a parked-not-failed state; treat as lifecycle.
    assert web._row_class(
        {"type": "task_complete", "status": "pipeline_pending"}
    ) == "lifecycle"
    # Soft warning — committed but verification didn't pass.
    assert web._row_class(
        {"type": "task_complete", "status": "verification_failed"}
    ) == "warning"
    # Hard failures — distinct from soft warning, distinct from happy path.
    for s in ("state_violation", "error", "timeout",
              "incomplete", "blocked", "failed"):
        assert web._row_class({"type": "task_complete", "status": s}) \
            == "failure", s
    # Retry-exhausted — task abandoned permanently. Its own dark-red tint.
    assert web._row_class(
        {"type": "task_complete", "status": "retry_exhausted"}
    ) == "frozen"
    # Defensive: unknown / missing status falls into a neutral gray bucket
    # so an unexpected string can't quietly inherit `complete`'s green.
    assert web._row_class({"type": "task_complete", "status": "unknown"}) \
        == "neutral"
    assert web._row_class({"type": "task_complete", "status": ""}) \
        == "neutral"
    assert web._row_class({"type": "task_complete"}) == "neutral"
    assert web._row_class(
        {"type": "task_complete", "status": "wat-no-such-status"}
    ) == "neutral"


def test_row_class_three_task_complete_statuses_distinct():
    """The briefing's gating one-liner — `complete`, `verification_failed`,
    `state_violation` must produce three different row classes so the
    operator sees three different colors at a glance."""
    a = web._row_class({"type": "task_complete", "status": "complete"})
    b = web._row_class({"type": "task_complete", "status": "verification_failed"})
    c = web._row_class({"type": "task_complete", "status": "state_violation"})
    assert a != b and b != c and a != c


def test_row_class_non_task_complete_unaffected():
    """The status-aware branch must not affect rows whose type isn't
    `task_complete` — failure-class events still get red regardless of
    any (irrelevant) status field they happen to carry."""
    assert web._row_class({"type": "task_error", "status": "ignored"}) \
        == "failure"
    assert web._row_class({"type": "verification_partial"}) == "warning"
    assert web._row_class({"type": "daemon_start"}) == "lifecycle"
    assert web._row_class({"type": "mattermost_reply"}) == ""


# --------- _event_extra rendering ---------


def test_event_extra_handles_dict_and_list(project: Config):
    """Nested values — common shape for stderr_tail, last_messages,
    files_changed — render as JSON not raw repr, and don't break HTML."""
    e = {
        "ts": "2026-04-28T12:00:00Z",
        "type": "task_error",
        "task": "TB-2",
        "stderr_tail": "line1\nline2",
        "last_messages": [{"role": "assistant", "text": "hi"}],
    }
    out = web._event_extra(e)
    assert "stderr_tail" in out
    assert "last_messages" in out
    # newlines collapsed for the one-line summary
    assert "\n" not in out


# --------- TB-129: helpers underlying the live task-run page ---------


def test_find_run_id_for_event_exact_match(project: Config):
    """Compact-ts derivation matches debug filename exactly when daemon and
    event ts agree (the common case — `_prep_debug_dumps` runs immediately
    after the task_start append)."""
    _seed_run(project, run_id="20260430T120000Z-TB-7", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
    ])
    rid = web._find_run_id_for_event(project, "2026-04-30T12:00:00Z", "TB-7")
    assert rid == "20260430T120000Z-TB-7"


def test_find_run_id_for_event_skew_tolerated(project: Config):
    """Debug-file ts is allowed to lag the task_start event by a few
    seconds — the daemon writes the event first, allocates files second.
    `_find_run_id_for_event` picks the closest run within a 60s window."""
    _seed_run(project, run_id="20260430T120003Z-TB-8", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
    ])
    rid = web._find_run_id_for_event(project, "2026-04-30T12:00:00Z", "TB-8")
    assert rid == "20260430T120003Z-TB-8"


def test_find_run_id_for_event_returns_none_when_pruned(project: Config):
    """No `.stream.jsonl` on disk → `None`. Operators don't get dead links
    pointing at debug dumps the gitignore swept away."""
    rid = web._find_run_id_for_event(project, "2026-04-30T12:00:00Z", "TB-99")
    assert rid is None


def test_terminal_event_for_run_in_flight(project: Config):
    """No matching task_complete/task_error/task_state_violation after the
    run start → `None` (in-flight)."""
    ev_mod.append(project.events_file, "task_start", task="TB-50",
                  title="In flight one")
    out = web._terminal_event_for_run(
        project, "20260430T130000Z", "TB-50",
    )
    assert out is None


def test_terminal_event_for_run_picks_matching_terminal(project: Config):
    """First terminal event with `task=<id>` and `ts >= run_start` wins."""
    ev_mod.append(project.events_file, "task_start", task="TB-51",
                  title="t")
    ev_mod.append(project.events_file, "task_complete", task="TB-51",
                  status="complete", commit="deadbee", summary="done")
    out = web._terminal_event_for_run(
        project, "20260430T120000Z", "TB-51",
    )
    assert out is not None
    assert out["type"] == "task_complete"
    assert out["status"] == "complete"


def test_terminal_event_ignores_prior_attempts(project: Config):
    """A terminal event for an earlier attempt should NOT attach to a
    later run — important for retry loops where the same task id has
    multiple complete cycles."""
    ev_mod.append(project.events_file, "task_complete", task="TB-52",
                  status="failed", commit="oldbeef")
    # Later run starts after that prior terminal.
    out = web._terminal_event_for_run(
        project, "20990101T000000Z", "TB-52",
    )
    assert out is None


# --------- _read_jsonl partial-line tolerance + since-cursor ---------


def test_read_jsonl_tolerates_partial_trailing_line(project: Config, tmp_path):
    """The daemon appends to .stream.jsonl while we read — a half-written
    final line must NOT make `_read_jsonl` raise. The next poll picks it up."""
    p = tmp_path / "partial.jsonl"
    p.write_text(
        '{"seq": 0, "type": "SystemMessage"}\n'
        '{"seq": 1, "type": "Assistant"}\n'
        '{"seq": 2, "type": "Result"'  # truncated, no closing brace
    )
    rows = web._read_jsonl(p)
    assert [r["seq"] for r in rows] == [0, 1]


def test_read_jsonl_filters_by_since(project: Config, tmp_path):
    """`since` cursor: rows with seq < since are skipped — what the live
    poller relies on to avoid re-rendering history every tick."""
    p = tmp_path / "since.jsonl"
    p.write_text(
        '{"seq": 0, "type": "SystemMessage"}\n'
        '{"seq": 1, "type": "Assistant"}\n'
        '{"seq": 2, "type": "Result"}\n'
    )
    rows = web._read_jsonl(p, since=2)
    assert [r["seq"] for r in rows] == [2]


# --------- TB-157: events-table show-tokens column ---------


def test_events_table_show_tokens_adds_column(project: Config):
    """`?show=tokens` opt-in surfaces token / cost per row (TB-157).
    Defaults stay clean (no extra column) — the briefing's stated
    contract is opt-in.
    """
    # Inject a judge_call with usage so the column has something to show.
    with project.events_file.open("a") as f:
        f.write(_json.dumps({
            "ts": "2026-04-30T22:00:00Z", "type": "judge_call",
            "task": "TB-99", "bullet_idx": 0, "verdict": "pass",
            "model": "claude-opus-4-7", "total_cost_usd": 0.042,
            "usage": {
                "input_tokens": 8200,
                "output_tokens": 90,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 7800,
            },
        }) + "\n")
    # Default rendering: no `tokens` column header.
    default_h = web._render_events(project, typ=None, n=50)
    assert "<th>tokens</th>" not in default_h
    # judge_call row's prepended summary still surfaces the in/out
    # numbers inline (a friendlier rendering of the row's payload).
    assert "judge_call" in default_h
    # show_tokens=True: the explicit column appears.
    show_h = web._render_events(project, typ=None, n=50, show_tokens=True)
    assert "<th>tokens</th>" in show_h
    assert "in=8,200" in show_h
    assert "$0.0420" in show_h


# ---------------------------------------------------------------------------
# TB-158: summarize_verification_failed helper grep-visibility.


def test_summarize_verification_failed_shared_helper_is_grep_visible():
    """TB-158 verification gate: `summarize_verification_failed` is
    referenced by name in events.py, cli_diagnostic.py (was cli.py
    pre-TB-264 — the `ap2 logs` rendering moved with `cmd_logs` to
    the diagnostic sibling), AND somewhere across the `web*.py` family
    (the TB-265 split routed it to `web_chrome.py`). The briefing's
    `grep -qE` bullet pins this so a refactor that drops the call from
    either surface would silently break the consistent rendering."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    for fname in ("events.py", "cli_diagnostic.py"):
        text = (root / fname).read_text()
        assert "summarize_verification_failed" in text, fname
    # TB-265: helper was lifted into `web_chrome.py` when web.py was
    # split by route group; the test now requires it appear somewhere
    # in the `web*.py` family (web.py + sibling modules).
    web_family_text = "\n".join(
        p.read_text() for p in sorted(root.glob("web*.py"))
    )
    assert "summarize_verification_failed" in web_family_text


# ---------------------------------------------------------------------------
# TB-179: compact `usage` blob rendering on the events table.
#
# Three event types — `judge_call`, `task_run_usage`, `control_run_usage` —
# carry a verbose `usage` (and often `model_usage`) dict that, when
# `_event_extra` dumps it inline, wraps the row across several lines and
# drowns the at-a-glance signal. The compact rendering keeps an identity
# prefix + 6 numeric fields (in / out / cc / cr / total_cost / duration)
# in the inline cell; the full payload still lives in the row's
# `<details>raw json</details>` toggle (no data loss).


import json as _tb179_json


def _full_judge_call_payload(*, task: str, bullet_idx: int) -> dict:
    """Return a `judge_call` event dict modelled on a real today's-events
    payload — same nested shape inside `usage` and `model_usage` so the
    test pins both the inclusion of the 6 compact fields AND the
    exclusion of the verbose nested keys."""
    return {
        "ts": "2026-05-04T19:11:38Z",
        "type": "judge_call",
        "task": task,
        "bullet_idx": bullet_idx,
        "bullet_kind": "prose",
        "verdict": "pass",
        "duration_s": 8.002,
        "model": "claude-opus-4-7",
        "num_turns": 2,
        "total_cost_usd": 0.146176,
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 6,
            "cache_creation_input_tokens": 17016,
            "cache_read_input_tokens": 42310,
            "output_tokens": 287,
            "server_tool_use": {
                "web_search_requests": 0,
                "web_fetch_requests": 0,
            },
            "service_tier": "standard",
            "cache_creation": {"ephemeral_5m_input_tokens": 17016},
            "iterations": 1,
        },
        "model_usage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 7636,
                "outputTokens": 22,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.006605,
                "contextWindow": 200000,
                "inference_geo": "us",
            },
        },
    }


def _split_summary_cell(rows_block: str, type_marker: str) -> str:
    """Return the part of the row before its `<details>raw json</details>`
    block — i.e. the inline summary cell that displays by default. Keyed
    off the substring `type_marker` (the event-type token) so we can pick
    the right row out of a multi-row table."""
    chunks = []
    for chunk in rows_block.split("<tr "):
        if type_marker not in chunk:
            continue
        before_details = chunk.split("<details>", 1)[0]
        chunks.append(before_details)
    return "\n".join(chunks)


def _seed_event(cfg: Config, payload: dict) -> None:
    """Append a pre-shaped event to `events.jsonl`. Bypasses
    `ev_mod.append` because the helper auto-stamps `ts` and we want to
    pin a specific value for the test."""
    with cfg.events_file.open("a") as f:
        f.write(_tb179_json.dumps(payload) + "\n")


def test_events_table_renders_judge_call_compactly(project: Config):
    """TB-179: a `judge_call` row's inline summary is the 6-field compact
    form, NOT the verbose `usage`/`model_usage` dict dump. The full
    payload still lands in the `<details>raw json</details>` footer."""
    _seed_event(
        project,
        _full_judge_call_payload(task="TB-165", bullet_idx=7),
    )
    h = web._render_events(project, typ="judge_call", n=10)
    rows_block = h.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    summary = _split_summary_cell(rows_block, "judge_call")

    # Identity prefix.
    assert "task=" in summary
    assert "TB-165" in summary
    assert "bullet=" in summary
    assert "7/prose" in summary
    assert "pass" in summary

    # All 6 numeric fields surface inline.
    assert "in=6" in summary           # input_tokens
    assert "out=287" in summary        # output_tokens
    assert "cc=17,016" in summary      # cache_creation_input_tokens
    assert "cr=42,310" in summary      # cache_read_input_tokens
    assert "$0.1462" in summary        # total_cost_usd, 4dp
    assert "8.0s" in summary           # duration_s

    # Verbose nested fields do NOT leak into the inline cell.
    assert "server_tool_use" not in summary
    assert "iterations" not in summary
    assert "service_tier" not in summary
    assert "inference_geo" not in summary
    # The nested `cache_creation` object key (distinct from the
    # `cache_creation_input_tokens` scalar that DOES surface).
    assert "ephemeral_5m_input_tokens" not in summary
    # `model_usage` (the per-model breakdown) is omitted from the
    # inline cell per the briefing's v1 scope.
    assert "model_usage" not in summary

    # The escape-hatch — full raw payload survives in the row's details.
    full_block = next(
        chunk for chunk in rows_block.split("<tr ") if "judge_call" in chunk
    )
    assert "<details>" in full_block
    raw_json = full_block.split("<details>", 1)[1]
    assert "server_tool_use" in raw_json
    assert "iterations" in raw_json
    assert "service_tier" in raw_json
    assert "model_usage" in raw_json
    assert "ephemeral_5m_input_tokens" in raw_json


def test_events_table_renders_task_run_usage_compactly(project: Config):
    """TB-179: `task_run_usage` rows use the same compact form, with a
    `task=` / `status` / `run=` identity prefix instead of the
    `judge_call` bullet shape."""
    _seed_event(project, {
        "ts": "2026-05-05T22:28:54Z",
        "type": "task_run_usage",
        "task": "TB-176",
        "run_id": "20260505T230301Z-TB-176",
        "status": "complete",
        "duration_s": 611.106,
        "total_cost_usd": 2.066962,
        "num_turns": 41,
        "model": "claude-opus-4-7",
        "usage": {
            "input_tokens": 50,
            "cache_creation_input_tokens": 113811,
            "cache_read_input_tokens": 2026862,
            "output_tokens": 13403,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
        "model_usage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 6793,
                "costUSD": 0.006888,
            },
        },
    })
    h = web._render_events(project, typ="task_run_usage", n=10)
    rows_block = h.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    summary = _split_summary_cell(rows_block, "task_run_usage")

    # Identity prefix specific to task_run_usage.
    assert "task=" in summary
    assert "TB-176" in summary
    assert "complete" in summary
    assert "run=" in summary
    assert "20260505T230301Z-TB-176" in summary

    # 6 numeric fields.
    assert "in=50" in summary
    assert "out=13,403" in summary
    assert "cc=113,811" in summary
    assert "cr=2,026,862" in summary
    assert "$2.0670" in summary
    assert "611.1s" in summary

    # Verbose fields stay out of the inline cell.
    assert "server_tool_use" not in summary
    assert "service_tier" not in summary
    assert "model_usage" not in summary


def test_events_table_renders_control_run_usage_compactly(project: Config):
    """TB-179: `control_run_usage` rows use the `label=` identity prefix
    (cron / mattermost / ideation runs don't have a TB-id)."""
    _seed_event(project, {
        "ts": "2026-05-05T22:28:54Z",
        "type": "control_run_usage",
        "label": "cron-status-report",
        "run_id": "20260505T222829Z-cron-status-report",
        "status": "complete",
        "duration_s": 24.639,
        "total_cost_usd": 0.4321735,
        "num_turns": 5,
        "usage": {
            "input_tokens": 12,
            "cache_creation_input_tokens": 57834,
            "cache_read_input_tokens": 61340,
            "output_tokens": 1407,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
        "model_usage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 4726,
                "costUSD": 0.004806,
            },
        },
    })
    h = web._render_events(project, typ="control_run_usage", n=10)
    rows_block = h.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    summary = _split_summary_cell(rows_block, "control_run_usage")

    # Identity prefix specific to control_run_usage.
    assert "label=" in summary
    assert "cron-status-report" in summary
    assert "complete" in summary
    assert "run=" in summary
    assert "20260505T222829Z-cron-status-report" in summary

    # 6 numeric fields.
    assert "in=12" in summary
    assert "out=1,407" in summary
    assert "cc=57,834" in summary
    assert "cr=61,340" in summary
    assert "$0.4322" in summary
    assert "24.6s" in summary

    # Verbose fields stay out of the inline cell.
    assert "server_tool_use" not in summary
    assert "service_tier" not in summary
    assert "model_usage" not in summary


def test_events_table_compact_path_is_opt_in_by_event_type(project: Config):
    """TB-179: events not in the compact-usage type set continue to
    render via the existing generic field-dump path. The new compact
    path is opt-in by event type, NOT a global rewrite — legacy events
    without a `usage` blob (e.g. `daemon_start`, `cron_complete`) keep
    their familiar key=value shape."""
    # Synthetic non-target event type with structurally similar fields
    # — same `task=` / `status=` keys but NOT `task_run_usage`.
    _seed_event(project, {
        "ts": "2026-05-04T19:00:00Z",
        "type": "task_complete",
        "task": "TB-300",
        "status": "complete",
        "commit": "deadbeef",
        "summary": "legacy lifecycle row",
    })
    h = web._render_events(project, typ="task_complete", n=10)
    rows_block = h.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    summary = _split_summary_cell(rows_block, "task_complete")
    # The generic dump format renders the lifecycle fields verbatim —
    # exactly the legacy shape, NOT the compact `task=TB-N complete run=...`
    # form that the new path produces.
    assert "TB-300" in summary
    assert "deadbeef" in summary
    assert "legacy lifecycle row" in summary
    # Sanity: the new compact-form bits do NOT appear because this row
    # didn't go through `_compact_usage_row`. (No 6-field tuple was
    # synthesized for an event without a `usage` dict.)
    assert "in=" not in summary
    assert "cr=" not in summary


def test_event_token_summary_helper_survives_refactor():
    """TB-179 regression check: TB-157's `_event_token_summary` helper
    still exists and is callable. The compact rendering wraps it; a
    refactor that deletes it would silently break both the
    `?show=tokens` column and the `_compact_usage_row` token tuple."""
    from ap2.web import _event_token_summary
    assert callable(_event_token_summary)


def test_compact_usage_event_types_referenced_in_web_module():
    """TB-179 verification gate: all three event types are name-referenced
    somewhere in the `web*.py` family — TB-265 split the events-table
    rendering into `web_chrome.py`, which now owns the
    `verification_failed` special-case branch in `_events_table`. The
    briefing's `grep -nE` bullet still pins this so a refactor that
    drops the three types silently breaks the row rendering."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    text = "\n".join(p.read_text() for p in sorted(root.glob("web*.py")))
    for typ in ("judge_call", "task_run_usage", "control_run_usage"):
        assert typ in text, typ


def test_summarize_usage_event_helper_consumed_by_web_module():
    """TB-180 verification gate: the shared compact formatter
    `summarize_usage_event` lives in `ap2/events.py` AND is consumed by
    the web UI (post-TB-265 split: `web_chrome.py`) plus
    `ap2/cli_diagnostic.py` (was `ap2/cli.py` pre-TB-264; `cmd_logs`
    moved with the diagnostic sibling). Pinning by name on both
    surfaces catches a refactor that drops one and silently
    de-syncs the rendering between `ap2 logs` and `/events`."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    for fname in ("events.py", "cli_diagnostic.py"):
        text = (root / fname).read_text()
        assert "summarize_usage_event" in text, fname
    # TB-265: web.py consumer moved into web_chrome.py with the split;
    # check across all web*.py siblings.
    web_family_text = "\n".join(
        p.read_text() for p in sorted(root.glob("web*.py"))
    )
    assert "summarize_usage_event" in web_family_text


def test_compact_usage_row_html_byte_identical_post_extraction(project: Config):
    """TB-180 byte-identical pin: after extracting the formatting helper
    to `events.summarize_usage_event`, the inline summary cell rendered
    for a usage-carrying event must still contain the same canonical
    substrings (identity prefix tokens, the 6 numeric fields, no verbose
    nested keys). The web renderer wraps the surface-agnostic string in
    `html.escape`; a post-refactor regression that swaps separator
    characters or drops fields would surface here."""
    _seed_event(
        project,
        _full_judge_call_payload(task="TB-179", bullet_idx=2),
    )
    h = web._render_events(project, typ="judge_call", n=10)
    rows_block = h.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    summary = _split_summary_cell(rows_block, "judge_call")

    # Identity prefix tokens.
    assert "task=TB-179" in summary
    assert "bullet=2/prose" in summary
    assert "pass" in summary

    # 6 numeric fields, separator preserved.
    assert "in=6" in summary
    assert "out=287" in summary
    assert "cc=17,016" in summary
    assert "cr=42,310" in summary
    assert "$0.1462" in summary
    assert "8.0s" in summary

    # Verbose nested keys still excluded.
    for forbidden in (
        "server_tool_use", "iterations", "service_tier",
        "inference_geo", "ephemeral_5m_input_tokens", "model_usage",
    ):
        assert forbidden not in summary, forbidden
