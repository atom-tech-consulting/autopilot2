from __future__ import annotations

import json

from ap2 import events


def test_append_and_tail(tmp_path):
    f = tmp_path / "events.jsonl"
    for i in range(10):
        events.append(f, "test", i=i)
    last = events.tail(f, n=5)
    assert len(last) == 5
    assert [e["i"] for e in last] == [5, 6, 7, 8, 9]


def test_tail_missing_file(tmp_path):
    assert events.tail(tmp_path / "nope.jsonl", n=10) == []


def test_format_for_prompt(tmp_path):
    f = tmp_path / "events.jsonl"
    events.append(f, "task_start", task="TB-1")
    events.append(f, "task_complete", task="TB-1", status="complete")
    out = events.format_for_prompt(events.tail(f, 10))
    assert "task_start" in out
    assert "task_complete" in out
    assert "TB-1" in out


def test_append_atomic_enough_under_load(tmp_path):
    """Appends should all produce valid JSON lines, even under rapid writes."""
    f = tmp_path / "events.jsonl"
    for i in range(50):
        events.append(f, "burst", i=i)
    for line in f.read_text().splitlines():
        d = json.loads(line)
        assert d["type"] == "burst"


# ---------------------------------------------------------------------------
# TB-158: summarize_verification_failed shared helper. Both `ap2 logs` (CLI)
# and `ap2/web.py` (events table + task-run detail page) consume this so
# the per-bullet summary, sort order, and truncation rules stay in lockstep.


def _vf_event(criteria: list[dict], **extra) -> dict:
    """Synthesize the criteria-bearing shape the daemon emits for the
    per-task verifier (TB-69)."""
    return {
        "ts": "2026-04-30T22:00:49Z",
        "type": "verification_failed",
        "task": "TB-99",
        "kind": "per_task",
        "overall": "fail",
        "criteria": criteria,
        **extra,
    }


def test_summarize_verification_failed_returns_expected_shape():
    """The helper returns the dict shape the CLI + web both render from:
    summary_line, failed_bullets list, pass_count, fail_count,
    unverified_count, total."""
    e = _vf_event([
        {"kind": "shell", "status": "pass", "bullet": "a", "notes": ""},
        {"kind": "shell", "status": "pass", "bullet": "b", "notes": ""},
        {"kind": "prose", "status": "fail", "bullet": "manual claim",
         "notes": "no evidence"},
    ])
    out = events.summarize_verification_failed(e)
    assert set(out.keys()) >= {
        "summary_line", "failed_bullets", "pass_count",
        "fail_count", "unverified_count", "total",
    }
    assert out["pass_count"] == 2
    assert out["fail_count"] == 1
    assert out["unverified_count"] == 0
    assert out["total"] == 3
    # Counter line names the totals operators should see at a glance.
    assert "2/3 passed" in out["summary_line"]
    assert "1 failed" in out["summary_line"]
    assert "0 unverified" in out["summary_line"]
    # Only failing bullets are surfaced as headlines.
    assert len(out["failed_bullets"]) == 1
    fb = out["failed_bullets"][0]
    assert fb["kind"] == "prose"
    assert fb["bullet"] == "manual claim"
    assert fb["notes"] == "no evidence"


def test_summarize_verification_failed_sort_and_filter():
    """Failed bullets land in source order (matches briefing order); pass
    and unverified bullets do NOT appear in the failed_bullets list."""
    e = _vf_event([
        {"kind": "shell", "status": "pass", "bullet": "p1", "notes": ""},
        {"kind": "shell", "status": "fail", "bullet": "first failure",
         "notes": "n1"},
        {"kind": "prose", "status": "unverified",
         "bullet": "manual; need evidence", "notes": "skipped"},
        {"kind": "shell", "status": "fail", "bullet": "second failure",
         "notes": "n2"},
        {"kind": "shell", "status": "pass", "bullet": "p2", "notes": ""},
    ])
    out = events.summarize_verification_failed(e)
    assert out["pass_count"] == 2
    assert out["fail_count"] == 2
    assert out["unverified_count"] == 1
    assert out["total"] == 5
    # Source order preserved among failures.
    assert [fb["bullet"] for fb in out["failed_bullets"]] == [
        "first failure", "second failure",
    ]


def test_summarize_verification_failed_truncates_long_text():
    """Bullet + note are truncated per the configured max lengths so a
    pathological judge note can't blow up the renderer's row width."""
    long_bullet = "x" * 500
    long_note = "y" * 800
    e = _vf_event([
        {"kind": "prose", "status": "fail",
         "bullet": long_bullet, "notes": long_note},
    ])
    out = events.summarize_verification_failed(
        e, max_bullet=120, max_note=200,
    )
    fb = out["failed_bullets"][0]
    assert len(fb["bullet"]) <= 120
    assert len(fb["notes"]) <= 200
    # Truncation marker present (non-strict — implementation may use … or ...).
    assert fb["bullet"].endswith("…") or fb["bullet"].endswith("...")
    assert fb["notes"].endswith("…") or fb["notes"].endswith("...")


def test_summarize_verification_failed_legacy_event_falls_back():
    """An event without the `criteria` field (very old events.jsonl, or a
    project-wide gate failure that doesn't carry per-bullet structure)
    returns the empty-fallback shape rather than raising."""
    legacy = {"type": "verification_failed", "task": "TB-1"}
    out = events.summarize_verification_failed(legacy)
    assert out["pass_count"] == 0
    assert out["fail_count"] == 0
    assert out["unverified_count"] == 0
    assert out["total"] == 0
    assert out["failed_bullets"] == []


def test_summarize_verification_failed_project_wide_synthesizes_bullet():
    """A project-wide gate failure carries `command` + `exit_code` +
    `stderr_tail` but no `criteria`. The helper synthesizes a single
    failed bullet so the CLI / web row still has something concrete to
    render."""
    e = {
        "type": "verification_failed",
        "task": "TB-2",
        "kind": "project_wide",
        "command": "uv run pytest -q ap2/tests/",
        "exit_code": 1,
        "stderr_tail": "FAILED test_foo.py::test_bar",
    }
    out = events.summarize_verification_failed(e)
    assert out["fail_count"] == 1
    assert len(out["failed_bullets"]) == 1
    fb = out["failed_bullets"][0]
    assert "uv run pytest" in fb["bullet"]
    assert "FAILED test_foo.py" in fb["notes"]
    assert "exit 1" in out["summary_line"]


# ---------------------------------------------------------------------------
# TB-179 / TB-180: summarize_usage_event shared helper. Both
# `ap2/cli.py::cmd_logs` (CLI, TB-180) and `ap2/web.py::_compact_usage_row`
# (web events table, TB-179) consume this so the surfaces stay
# byte-symmetric on the rendered string content.


def _judge_call_event(**override) -> dict:
    """Synthesize a `judge_call` event with the same nested-usage shape
    we see in real today's-events payloads. Tests override the `task`,
    `bullet_idx`, etc. as needed."""
    e = {
        "ts": "2026-05-04T19:11:38Z",
        "type": "judge_call",
        "task": "TB-1900",
        "bullet_idx": 7,
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
                "costUSD": 0.006605,
                "inference_geo": "us",
            },
        },
    }
    e.update(override)
    return e


def _task_run_usage_event(**override) -> dict:
    e = {
        "ts": "2026-05-04T15:15:13Z",
        "type": "task_run_usage",
        "task": "TB-1901",
        "run_id": "20260504T150009Z-TB-1901",
        "status": "complete",
        "duration_s": 342.117,
        "total_cost_usd": 0.851234,
        "num_turns": 41,
        "model": "claude-opus-4-7",
        "usage": {
            "input_tokens": 42,
            "cache_creation_input_tokens": 68234,
            "cache_read_input_tokens": 512891,
            "output_tokens": 4123,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
        "model_usage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 6727,
                "costUSD": 0.006812,
            },
        },
    }
    e.update(override)
    return e


def _control_run_usage_event(**override) -> dict:
    e = {
        "ts": "2026-05-04T18:09:21Z",
        "type": "control_run_usage",
        "label": "ideation",
        "run_id": "20260504T180620Z-ideation",
        "status": "complete",
        "duration_s": 178.301,
        "total_cost_usd": 0.421875,
        "num_turns": 11,
        "usage": {
            "input_tokens": 18,
            "cache_creation_input_tokens": 49231,
            "cache_read_input_tokens": 104982,
            "output_tokens": 2034,
            "server_tool_use": {"web_search_requests": 0},
            "service_tier": "standard",
        },
        "model_usage": {
            "claude-haiku-4-5-20251001": {
                "inputTokens": 4726,
                "costUSD": 0.004806,
            },
        },
    }
    e.update(override)
    return e


def test_summarize_usage_event_returns_compact_string():
    """The helper returns a non-empty compact string for each of the
    three usage-carrying event types, with the correct identity prefix
    per type, all 6 numeric fields, and a length well under ~200 chars
    on a real-world payload."""
    # judge_call — `task=TB-N bullet=N/<kind> <verdict>` prefix.
    out_jc = events.summarize_usage_event(_judge_call_event())
    assert out_jc, "judge_call helper returned empty string"
    assert "task=TB-1900" in out_jc
    assert "bullet=7/prose" in out_jc
    assert "pass" in out_jc
    assert len(out_jc) < 200, out_jc

    # task_run_usage — `task=TB-N <status> run=<run_id>` prefix.
    out_tr = events.summarize_usage_event(_task_run_usage_event())
    assert out_tr
    assert "task=TB-1901" in out_tr
    assert "complete" in out_tr
    assert "run=20260504T150009Z-TB-1901" in out_tr
    assert len(out_tr) < 200, out_tr

    # control_run_usage — `label=<label> <status> run=<run_id>` prefix.
    out_cr = events.summarize_usage_event(_control_run_usage_event())
    assert out_cr
    assert "label=ideation" in out_cr
    assert "complete" in out_cr
    assert "run=20260504T180620Z-ideation" in out_cr
    assert len(out_cr) < 200, out_cr

    # All three carry the 6 numeric fields.
    for out, fields in (
        (out_jc, ("in=6", "out=287", "cc=17,016", "cr=42,310",
                  "$0.1462", "8.0s")),
        (out_tr, ("in=42", "out=4,123", "cc=68,234", "cr=512,891",
                  "$0.8512", "342.1s")),
        (out_cr, ("in=18", "out=2,034", "cc=49,231", "cr=104,982",
                  "$0.4219", "178.3s")),
    ):
        for f in fields:
            assert f in out, (out, f)


def test_summarize_usage_event_strips_verbose_nested_keys():
    """Verbose nested fields — `server_tool_use`, `iterations`,
    `service_tier`, `inference_geo`, `model_usage`, the nested
    `cache_creation` object — must NOT leak into the compact string.
    Inline compaction is the whole point; the verbose payload survives
    in events.jsonl and (on web) in the row's <details> footer."""
    for e in (
        _judge_call_event(),
        _task_run_usage_event(),
        _control_run_usage_event(),
    ):
        out = events.summarize_usage_event(e)
        for forbidden in (
            "server_tool_use",
            "iterations",
            "service_tier",
            "inference_geo",
            "model_usage",
            "ephemeral_5m_input_tokens",
        ):
            assert forbidden not in out, (e["type"], forbidden, out)


def test_summarize_usage_event_returns_empty_for_unrelated_type():
    """The helper is opt-in by event type. A non-target type (e.g.
    `task_complete`, `daemon_start`) returns "" so the caller falls
    back to the generic field-dump renderer."""
    for typ in ("task_complete", "daemon_start", "verification_failed",
                "task_start", "backlog_auto_promoted"):
        out = events.summarize_usage_event({"type": typ, "task": "TB-X"})
        assert out == "", (typ, out)


def test_summarize_usage_event_handles_missing_usage_dict():
    """A usage-typed event without any `usage` / `total_cost_usd` /
    `duration_s` falls back to "" rather than emitting a degenerate
    identity-only line. The caller can then defer to the generic
    field-dump path."""
    e = {"type": "judge_call"}  # No identity, no token, no duration.
    assert events.summarize_usage_event(e) == ""


def test_summarize_usage_event_truncates_when_max_chars_set():
    """`max_chars=N` caps the returned string at N characters and
    appends `…`. Callers on tight terminal widths can pin a width
    budget; the natural compact form is well under 200 chars on real
    payloads so the cap rarely kicks in."""
    e = _judge_call_event()
    natural = events.summarize_usage_event(e)
    assert len(natural) > 50

    truncated = events.summarize_usage_event(e, max_chars=50)
    assert len(truncated) <= 50
    assert truncated.endswith("…")
    # Identity prefix still surfaces in the truncated form.
    assert truncated.startswith("task=TB-1900")


def test_summarize_usage_event_omits_token_summary_when_no_usage_dict():
    """An event of a target type that carries `total_cost_usd` /
    `duration_s` but no `usage` dict (very thin payload) still produces
    a useful compact line — identity + cost + duration — without
    crashing on the missing dict."""
    e = {
        "type": "task_run_usage",
        "task": "TB-1902",
        "status": "incomplete",
        "run_id": "20260504T123456Z-TB-1902",
        "total_cost_usd": 0.05,
        "duration_s": 12.3,
    }
    out = events.summarize_usage_event(e)
    assert "task=TB-1902" in out
    assert "incomplete" in out
    assert "run=20260504T123456Z-TB-1902" in out
    assert "$0.0500" in out
    assert "12.3s" in out
    # No `in=` etc. because there was no usage dict to derive them from.
    assert "in=" not in out
    assert "cr=" not in out
