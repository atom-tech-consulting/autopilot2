"""Per-envelope SDK message serialization helpers (TB-85 / TB-157).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`run_task`, `_run_control_agent`) calls these helpers
per envelope to build the `.stream.jsonl` (compact summary) and
`.messages.jsonl` (full content) debug dumps. Pure utility code —
no I/O of its own; the caller writes the returned dicts.

Public surface (re-exported from `ap2/daemon.py` so `daemon._summarize_message`
etc. continue to resolve for existing test paths):

  - `_extract_text(msg) -> str`: best-effort final text-block extractor.
  - `_truncate(s, n) -> str`: shared truncator used by the previews.
  - `_extract_tool_result_payload(content) -> dict | None`: parses a
    ToolResultBlock's payload into the MCP-tool reply shape.
  - `_stringify_block_content(c) -> str`: one-line stringify of a
    ToolResultBlock's content (str or list of sub-blocks).
  - `_walk_blocks(msg, *, full)`: walks `msg.content`, returns fields
    suitable for merging into a summary or full record.
  - `_summarize_message(msg) -> dict`: compact per-envelope summary
    for `.stream.jsonl`.
  - `_serialize_message_full(msg) -> dict`: full-content per-envelope
    record for `.messages.jsonl`.

TB-85 background: the previous instrumentation only captured
`_extract_text(msg)[:500]` which returned None for most envelopes since
the SDK emits many AssistantMessage envelopes that are pure tool_use
with no text — leaving the stream dump useless for diagnosing the
"exit code 1 / empty stderr" crash class.
"""
from __future__ import annotations

import json as _json


def _extract_text(msg) -> str:
    """Best-effort extraction of an assistant message's final text block."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in reversed(content):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text
    result = getattr(msg, "result", None)
    if isinstance(result, str):
        return result
    return ""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _extract_tool_result_payload(content) -> dict | None:
    """Parse a ToolResultBlock's content into the dict the daemon's MCP
    tools return via `_ok(...)` (the body of the inner `text` field is a
    JSON object with `message` + structured fields).

    Returns the dict on success, or None when the shape doesn't match (the
    block was an error, the content is a non-JSON string, etc.).
    """
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for blk in content:
            t = getattr(blk, "text", None)
            if isinstance(t, str):
                text = t
                break
            if isinstance(blk, dict):
                t = blk.get("text")
                if isinstance(t, str):
                    text = t
                    break
    if not text:
        return None
    try:
        payload = _json.loads(text)
    except _json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _stringify_block_content(c) -> str:
    """Best-effort one-line stringify of a ToolResultBlock.content payload.

    Real shapes seen: a bare string (e.g., a Bash tool's output), or a list of
    sub-blocks (e.g., text + image). We only need a preview, so flatten to a
    string and let callers truncate.
    """
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            t = getattr(b, "text", None)
            parts.append(t if isinstance(t, str) else str(b))
        return " ".join(parts)
    return str(c)


def _walk_blocks(msg, *, full: bool) -> dict:
    """Walk a Message's `.content` blocks. Returns extracted fields ready to
    merge into a dict by `_summarize_message` / `_serialize_message_full`.

    `full=False` truncates text to 200 chars and tool args to 200 chars (for
    .stream.jsonl); `full=True` returns untruncated content (.messages.jsonl).
    """
    out: dict = {}
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return out

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    blocks_full: list[dict] = []

    for part in content:
        block_full: dict = {"block_type": type(part).__name__}

        text = getattr(part, "text", None)
        if isinstance(text, str):
            if text.strip():
                text_parts.append(text)
            block_full["text"] = text

        # ToolUseBlock: has `name` + `input` + `id`.
        name = getattr(part, "name", None)
        inp = getattr(part, "input", None)
        if name is not None and inp is not None:
            args_json = _json.dumps(inp, default=str)
            tool_calls.append({
                "name": name,
                "args_preview": _truncate(args_json, 200),
            })
            block_full["name"] = name
            block_full["input"] = inp
            tool_id = getattr(part, "id", None)
            if tool_id is not None:
                block_full["id"] = tool_id

        # ToolResultBlock: has `tool_use_id` + `content` (str or list of blocks).
        tu_id = getattr(part, "tool_use_id", None)
        if tu_id is not None:
            tr_content = getattr(part, "content", None)
            preview_str = _stringify_block_content(tr_content)
            is_err = bool(getattr(part, "is_error", False))
            tool_results.append({
                "tool_use_id": tu_id,
                "is_error": is_err,
                "preview": _truncate(preview_str, 200),
            })
            block_full["tool_use_id"] = tu_id
            block_full["is_error"] = is_err
            if tr_content is not None:
                block_full["content"] = preview_str if isinstance(tr_content, str) else _stringify_block_content(tr_content)

        blocks_full.append(block_full)

    if not full:
        if text_parts:
            out["text_preview"] = _truncate(text_parts[-1], 200)
        if tool_calls:
            out["tool_calls"] = tool_calls
        if tool_results:
            out["tool_results"] = tool_results
    else:
        out["content"] = blocks_full

    return out


def _summarize_message(msg) -> dict:
    """Compact per-envelope summary for `.stream.jsonl` (TB-85).

    Returns: `{type, text_preview?, tool_calls?, tool_results?, stop_reason?,
    num_turns?, total_cost_usd?, subtype?, usage?, model_usage?}`. Optional
    fields are omitted when absent so the dump stays scannable. `seq` is
    added by the caller.
    """
    out: dict = {"type": type(msg).__name__}
    out.update(_walk_blocks(msg, full=False))

    # AssistantMessage carries the model string; ResultMessage carries usage /
    # cost / stop_reason at the message level. Capture both so the stream is
    # debuggable end-to-end (which model produced this turn? what stop_reason
    # ended it?).
    for k in ("model", "stop_reason", "num_turns", "total_cost_usd"):
        v = getattr(msg, k, None)
        if v is not None:
            out[k] = v
    sub = getattr(msg, "subtype", None)
    if sub is not None:
        out["subtype"] = sub
    # TB-157: capture token / cache counters from ResultMessage. The `usage`
    # dict shape is well-known (Anthropic API response): input_tokens,
    # output_tokens, cache_creation_input_tokens, cache_read_input_tokens.
    # `model_usage` carries the same fields broken down by model when the
    # session spans multiple variants. Pass through verbatim — downstream
    # aggregators (adhoc/token_breakdown.py, the web detail page) parse the
    # nested dict directly.
    for k in ("usage", "model_usage"):
        v = getattr(msg, k, None)
        if isinstance(v, dict) and v:
            out[k] = v
    # Some ResultMessage variants carry text in `.result` rather than via
    # content blocks.
    if "text_preview" not in out:
        result = getattr(msg, "result", None)
        if isinstance(result, str) and result.strip():
            out["text_preview"] = _truncate(result, 200)
    return out


def _serialize_message_full(msg) -> dict:
    """Full-content per-envelope record for `.messages.jsonl` (TB-85).

    Same shape as `_summarize_message` but without truncation. Cross-reference
    with the stream summary by `seq`.
    """
    out: dict = {"type": type(msg).__name__}
    out.update(_walk_blocks(msg, full=True))
    for k in ("model", "stop_reason", "num_turns", "total_cost_usd", "subtype", "result"):
        v = getattr(msg, k, None)
        if v is not None:
            out[k] = v
    # TB-157: same usage / model_usage capture as the compact summary; the
    # full-record file is the durable archive for cost-tradeoff analysis.
    for k in ("usage", "model_usage"):
        v = getattr(msg, k, None)
        if isinstance(v, dict) and v:
            out[k] = v
    return out
