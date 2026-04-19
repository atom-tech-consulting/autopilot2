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
