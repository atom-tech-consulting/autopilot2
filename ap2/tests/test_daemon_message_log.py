"""Tests for TB-85 — `_summarize_message` / `_serialize_message_full` /
`_prep_debug_dumps` (the SDK message-stream debug instrumentation rewrite).

Stoch's TB-84 task_error captured a 396-line `.stream.jsonl` where every
envelope had `text_preview: null` because the previous implementation only
walked text blocks via `_extract_text` and most AssistantMessage envelopes
are pure tool_use with no text. These tests pin the new behavior: real text
preview, tool_calls / tool_results surfaced, and a parallel full-content
file for deep diagnosis.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import daemon
from ap2.config import Config


def _msg(*blocks, **fields) -> SimpleNamespace:
    """Fake an SDK Message with `.content` of block-shaped namespaces."""
    ns = SimpleNamespace(content=list(blocks), **fields)
    return ns


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def _tool_use(name: str, input: dict, id: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=input, id=id)


def _tool_result(tool_use_id: str, content, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(tool_use_id=tool_use_id, content=content, is_error=is_error)


# ---------------- _summarize_message ----------------


def test_summarize_text_block():
    m = _msg(_text("Hello, this is the agent speaking."))
    out = daemon._summarize_message(m)
    # The `type` field comes from the class name — for real SDK messages it
    # would be `AssistantMessage` etc. What matters is the text_preview field
    # is now populated, not None (the old bug from TB-84).
    assert out["text_preview"] == "Hello, this is the agent speaking."
    assert "tool_calls" not in out
    assert "tool_results" not in out


def test_summarize_truncates_long_text():
    m = _msg(_text("x" * 1000))
    out = daemon._summarize_message(m)
    # 200-char cap + ellipsis sentinel
    assert len(out["text_preview"]) <= 201
    assert out["text_preview"].endswith("…")


def test_summarize_tool_use_block():
    m = _msg(_tool_use("Edit", {"file_path": "foo.py", "old_string": "a", "new_string": "b"}))
    out = daemon._summarize_message(m)
    assert "text_preview" not in out  # no text in this envelope
    assert out["tool_calls"] == [{
        "name": "Edit",
        "args_preview": '{"file_path": "foo.py", "old_string": "a", "new_string": "b"}',
    }]


def test_summarize_truncates_long_tool_args():
    m = _msg(_tool_use("Bash", {"command": "x" * 1000}))
    out = daemon._summarize_message(m)
    assert len(out["tool_calls"][0]["args_preview"]) <= 201
    assert out["tool_calls"][0]["args_preview"].endswith("…")


def test_summarize_tool_result_block():
    m = _msg(_tool_result("tu_1", "stdout from bash"))
    out = daemon._summarize_message(m)
    assert out["tool_results"] == [{
        "tool_use_id": "tu_1",
        "is_error": False,
        "preview": "stdout from bash",
    }]


def test_summarize_tool_result_with_error_flag():
    m = _msg(_tool_result("tu_2", "command failed: ...", is_error=True))
    out = daemon._summarize_message(m)
    assert out["tool_results"][0]["is_error"] is True


def test_summarize_mixed_blocks():
    """A real AssistantMessage often has text + one or more tool_use blocks."""
    m = _msg(
        _text("Going to edit foo.py."),
        _tool_use("Edit", {"file_path": "foo.py"}),
    )
    out = daemon._summarize_message(m)
    assert out["text_preview"] == "Going to edit foo.py."
    assert len(out["tool_calls"]) == 1
    assert out["tool_calls"][0]["name"] == "Edit"


def test_summarize_result_message_extras():
    m = SimpleNamespace(
        stop_reason="end_turn",
        num_turns=42,
        total_cost_usd=0.1234,
        result="final answer text",
    )
    out = daemon._summarize_message(m)
    assert out["stop_reason"] == "end_turn"
    assert out["num_turns"] == 42
    assert out["total_cost_usd"] == 0.1234
    # Falls through to `.result` since there are no content blocks.
    assert out["text_preview"] == "final answer text"


def test_summarize_system_message_subtype():
    m = SimpleNamespace(subtype="init")
    out = daemon._summarize_message(m)
    assert out["subtype"] == "init"


def test_summarize_assistant_message_model():
    """AssistantMessage.model — captured for the stream so a debugger can
    tell which Claude variant produced any given turn (TB-97)."""
    m = _msg(_text("hello"), model="claude-opus-4-7-1m")
    out = daemon._summarize_message(m)
    assert out["model"] == "claude-opus-4-7-1m"


# ---------------- _serialize_message_full ----------------


def test_full_preserves_long_text():
    long_text = "y" * 800
    m = _msg(_text(long_text))
    out = daemon._serialize_message_full(m)
    assert out["content"][0]["text"] == long_text


def test_full_preserves_full_tool_input():
    inp = {"big_arg": "z" * 500}
    m = _msg(_tool_use("Bash", inp))
    out = daemon._serialize_message_full(m)
    assert out["content"][0]["input"] == inp
    assert out["content"][0]["name"] == "Bash"
    assert out["content"][0]["id"] == "tu_1"


def test_full_preserves_tool_result():
    m = _msg(_tool_result("tu_1", "z" * 800, is_error=True))
    out = daemon._serialize_message_full(m)
    assert out["content"][0]["content"] == "z" * 800
    assert out["content"][0]["is_error"] is True


# ---------------- _prep_debug_dumps ----------------


def test_prep_debug_dumps_returns_three_paths(tmp_path):
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    paths = daemon._prep_debug_dumps(cfg, "TB-1")
    assert len(paths) == 3
    prompt, stream, messages = paths
    assert prompt.name.endswith("TB-1.prompt.md")
    assert stream.name.endswith("TB-1.stream.jsonl")
    assert messages.name.endswith("TB-1.messages.jsonl")
    # Same timestamp prefix → atomic per-task triplet.
    assert prompt.name[:16] == stream.name[:16] == messages.name[:16]
    # Live in the gitignored debug dir.
    assert prompt.parent.name == "debug"


# ---------------- TB-157: usage / model_usage capture ----------------


def test_summarize_captures_usage_dict():
    """ResultMessage's `usage` dict (input/output/cache tokens) must
    round-trip through `_summarize_message` so the per-call stream dump
    carries the data needed for cost-tradeoff aggregation.
    """
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 0,
    }
    m = SimpleNamespace(usage=usage, total_cost_usd=0.001)
    out = daemon._summarize_message(m)
    assert out["usage"] == usage


def test_summarize_captures_model_usage_dict():
    """Per-model breakdown rides alongside the message-level usage dict
    when the session spanned multiple Claude variants. Pass through
    verbatim — downstream aggregators parse the nested shape directly.
    """
    model_usage = {
        "claude-opus-4-7": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    }
    m = SimpleNamespace(model_usage=model_usage)
    out = daemon._summarize_message(m)
    assert out["model_usage"] == model_usage


def test_summarize_omits_usage_when_absent():
    """Legacy ResultMessages (pre-TB-157, or transports that never carry
    `usage`) must not gain a `usage: null` key — the dump stays scannable.
    """
    m = SimpleNamespace(stop_reason="end_turn", num_turns=1,
                        total_cost_usd=0.01)
    out = daemon._summarize_message(m)
    assert "usage" not in out
    assert "model_usage" not in out


def test_summarize_omits_usage_when_empty_dict():
    """An empty dict counts as "no data" — same outcome as missing entirely."""
    m = SimpleNamespace(usage={}, model_usage={})
    out = daemon._summarize_message(m)
    assert "usage" not in out
    assert "model_usage" not in out


def test_full_captures_usage_dict():
    """Same capture in the full-content serializer; the .messages.jsonl
    file is the durable archive used for after-the-fact cost analysis.
    """
    usage = {"input_tokens": 7, "output_tokens": 3,
             "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    m = SimpleNamespace(usage=usage)
    out = daemon._serialize_message_full(m)
    assert out["usage"] == usage


# ---------------- regression for the actual TB-84-stoch bug ----------------


def test_text_preview_no_longer_null_for_pure_tool_use_message():
    """Regression: the old `_extract_text(msg)[:500] or None` returned None for
    AssistantMessage envelopes that were pure tool_use (no text block). Stoch's
    TB-84 task_error stream had 396 such envelopes all logged as text_preview:
    null — the dump was useless. The new summary surfaces the tool_calls list
    instead, so even a text-less envelope carries diagnostic content.
    """
    m = _msg(_tool_use("Bash", {"command": "uv run pytest -q"}))
    out = daemon._summarize_message(m)
    # No text → no text_preview key at all (rather than text_preview: null).
    assert "text_preview" not in out
    # But the envelope is NOT empty diagnostics; tool_calls captures intent.
    assert "tool_calls" in out
    assert out["tool_calls"][0]["name"] == "Bash"
    assert "uv run pytest -q" in out["tool_calls"][0]["args_preview"]
