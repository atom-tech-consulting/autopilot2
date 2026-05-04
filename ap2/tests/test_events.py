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
