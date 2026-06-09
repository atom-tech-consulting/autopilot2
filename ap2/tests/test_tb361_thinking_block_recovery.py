"""TB-361: the thinking-block recovery path is no longer inert.

TB-356 shipped a thinking-block-immutability-400 effort-downshift, but the
recovery had three latent holes this module pins shut:

  1. **Classifier matched only the dict shape.** `_is_thinking_block_corruption`
     flattened its input with `json.dumps(..., default=str)`, which captures
     the signature when the stream tail arrives as summary *dicts* (what the
     `task_error` event records as `last_messages`) but emits a useless
     `<AssistantMessage ...>` repr — dropping the inner text — when handed raw
     SDK message *objects*. TB-361 walks each message's text fragments
     (`text_preview` / `result` / content-block `text`) so BOTH shapes match.
     The load-bearing test below is built from a REAL recorded `last_messages`
     payload (TB-358's `task_error`, 2026-05-31) so it pins the production
     shape and the raw-object shape — the one that returned False before this
     change.
  2. **The class wedged the auto-approve window.** A thinking-block
     `task_error` tripped the TB-224 cost/blast-radius breaker, pausing the
     whole window. TB-361 stamps `thinking_block_corruption=true` on the
     failure event and exempts flagged events from the breaker (and from the
     TB-223 consecutive-freeze scan); a GENUINE `task_error` still pauses.
  3. **A gated Backlog head froze everything behind it.** The auto-promote
     step inspected only `next_dispatchable`'s single head; when the gate
     halted it the tick ended, freezing operator-added / operator-approved
     work queued behind a gated auto-approved task. TB-361 iterates
     dispatchable candidates and promotes the first the gate does NOT halt.

Test list mirrors the briefing's `## Verification` (a)-(d) + the skip-past
pair.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import daemon, events, retry, tools
from ap2.board import Board
from ap2.components.auto_approve import run_auto_approve_pass
from ap2.config import Config
from ap2.daemon import _is_thinking_block_corruption, run_task
from ap2.init import init_project


# ---------------------------------------------------------------------------
# REAL recorded `last_messages` payload — the load-bearing fixture.
#
# Transcribed verbatim from the `task_error` event TB-358 emitted on
# 2026-05-31T18:04:14Z (and identical in shape to TB-353 / TB-354): a list of
# per-message summary dicts where the 400 signature lives in an
# AssistantMessage's `text_preview` (and is echoed on the trailing
# ResultMessage). The `model="<synthetic>"` marker + backtick-wrapped
# `thinking` / `redacted_thinking` tokens are reproduced exactly so the
# fixture pins the production shape — NOT a hand-simplified string. This is
# the exact shape the pre-TB-361 classifier matched on dicts but MISSED on
# raw objects; both must now classify True.
# ---------------------------------------------------------------------------
_REAL_400_TEXT = (
    "API Error: 400 messages.1.content.15: `thinking` or `redacted_thinking` "
    "blocks in the latest assistant message cannot be modified. These blocks "
    "must remain as they were in the original response."
)

_REAL_LAST_MESSAGES: list[dict] = [
    {"seq": 80, "type": "AssistantMessage",
     "text_preview": "Let me re-read the briefing and finish the edit.",
     "model": "claude-opus-4-8"},
    {"seq": 81, "type": "AssistantMessage",
     "tool_calls": [{"name": "Edit", "args_preview": "{'file_path': '...'}"}],
     "model": "claude-opus-4-8"},
    {"seq": 82, "type": "UserMessage",
     "tool_results": [{"tool_use_id": "toolu_01abc", "is_error": False,
                       "preview": "The file ... has been updated successfully."}]},
    {"seq": 83, "type": "UserMessage",
     "tool_results": [{"tool_use_id": "toolu_019tY8", "is_error": False,
                       "preview": "{'type': 'tool_reference', 'tool_name': "
                                  "'mcp__autopilot__report_result'}"}]},
    {"seq": 84, "type": "AssistantMessage",
     "text_preview": _REAL_400_TEXT,
     "model": "<synthetic>", "stop_reason": "stop_sequence",
     "usage": {"input_tokens": 0, "output_tokens": 0}},
    {"seq": 85, "type": "ResultMessage",
     "text_preview": _REAL_400_TEXT,
     "stop_reason": "stop_sequence", "num_turns": 34,
     "total_cost_usd": 0.162, "subtype": "success",
     "usage": {"input_tokens": 1, "output_tokens": 7002}},
]


def _real_payload_as_objects() -> list:
    """The SAME recorded payload, rebuilt as raw SDK-message-like objects
    (attributes, not dict keys) — the shape whose `json.dumps(default=str)`
    repr drops the inner text and so returned False before TB-361. The 400
    text lands on a content block's `.text` (AssistantMessage) and on the
    trailing message's `.result` (ResultMessage), mirroring how
    `_extract_text` / `_walk_blocks` read a live SDK stream.
    """
    def assistant(text: str):
        return SimpleNamespace(content=[SimpleNamespace(text=text)], result=None)

    def result(text: str):
        return SimpleNamespace(content=None, result=text)

    return [
        assistant("Let me re-read the briefing and finish the edit."),
        assistant(_REAL_400_TEXT),
        result(_REAL_400_TEXT),
    ]


# ===========================================================================
# (a) + (the raw-object cleavage) classifier on the REAL recorded shape.
# ===========================================================================


def test_classifier_true_on_real_recorded_last_messages_dicts():
    """The load-bearing pin: `_is_thinking_block_corruption` is True on a
    real recorded `last_messages` payload (summary dicts whose `text_preview`
    carries the 400). This is the production shape; if a future refactor
    regresses the classifier to a lossy object-repr match this test fails."""
    assert _is_thinking_block_corruption(_REAL_LAST_MESSAGES) is True


def test_signature_lives_in_text_preview_not_metadata():
    """Sanity: the discriminating signature is in the message TEXT
    (`text_preview`), confirming the classifier must read text content —
    not the bare `json.dumps` of structural metadata (type/seq/usage)."""
    sig_msg = next(m for m in _REAL_LAST_MESSAGES if m["seq"] == 84)
    assert "cannot be modified" in sig_msg["text_preview"]
    # The structural metadata alone (no text_preview / result) must NOT match.
    metadata_only = [
        {k: v for k, v in m.items() if k not in ("text_preview", "result")}
        for m in _REAL_LAST_MESSAGES
    ]
    assert _is_thinking_block_corruption(metadata_only) is False


def test_classifier_true_on_same_payload_as_raw_objects():
    """The exact shape that returned False BEFORE TB-361: the same recorded
    payload rebuilt as raw SDK message objects. The pre-TB-361 classifier
    `json.dumps`'d these to `<namespace ...>` reprs (signature dropped) and
    returned False; the hardened classifier walks `.content[*].text` /
    `.result` and returns True. The recovery path no longer depends on which
    shape the call site happens to pass."""
    assert _is_thinking_block_corruption(_real_payload_as_objects()) is True


def test_classifier_false_on_generic_and_verification_failures():
    """(d) A generic `task_error` surface string and a verification-style
    failure tail must NOT classify — no downshift, no breaker exemption."""
    assert _is_thinking_block_corruption(
        "Exception: Claude Code returned an error result: success"
    ) is False
    assert _is_thinking_block_corruption(
        [{"seq": 0, "type": "ResultMessage",
          "text_preview": "FAILED tests/test_x.py::test_y - assert 1 == 2"}]
    ) is False
    # "cannot be modified" without a thinking token is deliberately rejected.
    assert _is_thinking_block_corruption(
        [{"text_preview": "the file cannot be modified by this user"}]
    ) is False


# ===========================================================================
# (b) failure → bump → effort_downshift wiring, driven by the real shape.
# ===========================================================================


@pytest.fixture
def run_task_cfg(tmp_path: Path, monkeypatch) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-5** **Victim** `#x` — Will be run. [→ brief](brief.md)\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    monkeypatch.delenv("AP2_VERIFY_CMD", raising=False)
    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)
    monkeypatch.delenv("AP2_CORE_AGENT_EFFORT", raising=False)
    monkeypatch.delenv("AP2_THINKING_BLOCK_EFFORT_DROP_DISABLED", raising=False)
    monkeypatch.delenv("AP2_CORE_THINKING_BLOCK_EFFORT_DROP_DISABLED", raising=False)
    monkeypatch.setenv("AP2_MAX_RETRIES", "5")
    monkeypatch.setenv("AP2_TASK_TIMEOUT_S", "60")
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    return cfg_


class _FakeMsg:
    """A stream message carrying assistant text — lands in stream_log's
    `text_preview` via `_summarize_message`, exactly like a real run."""

    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


def _sdk_yield_then_raise(text: str, exc: BaseException):
    """Yield one message carrying `text` (the 400 lands in the stream tail),
    then raise the opaque surface exception — mirrors production."""
    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    async def gen():
        yield _FakeMsg(text)
        raise exc

    return SimpleNamespace(query=lambda prompt, options: gen(),
                           ClaudeAgentOptions=_Options)


def test_thinking_block_failure_bumps_downshift_and_emits_event(run_task_cfg):
    """A run whose stream tail carries the REAL 400 text + an opaque surface
    exception is classified as thinking-block-corruption: the task lands in
    Backlog, its per-task downshift level bumps to 1, and an `effort_downshift`
    event fires (xhigh → high)."""
    cfg = run_task_cfg
    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_yield_then_raise(
        _REAL_400_TEXT,
        RuntimeError("Claude Code returned an error result: success"),
    )
    asyncio.run(run_task(cfg, sdk, None, task))

    assert Board.load(cfg.tasks_file).find("TB-5")[0] == "Backlog"
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 1

    ds = [e for e in events.tail(cfg.events_file, 40)
          if e["type"] == "effort_downshift" and e.get("task") == "TB-5"]
    assert len(ds) == 1, ds
    assert ds[0]["from"] == "xhigh"
    assert ds[0]["to"] == "high"
    assert ds[0]["reason"] == "thinking_block_corruption"


def test_task_error_event_carries_thinking_block_flag(run_task_cfg):
    """The `task_error` event the daemon records carries
    `thinking_block_corruption=true` — the single flag both the retry
    downshift and the breaker exemption read. A generic crash records the
    flag as False."""
    cfg = run_task_cfg
    task = Board.load(cfg.tasks_file).get("TB-5")
    sdk = _sdk_yield_then_raise(
        _REAL_400_TEXT,
        RuntimeError("Claude Code returned an error result: success"),
    )
    asyncio.run(run_task(cfg, sdk, None, task))
    errs = [e for e in events.tail(cfg.events_file, 40)
            if e["type"] == "task_error" and e.get("task") == "TB-5"]
    assert len(errs) == 1, errs
    assert errs[0].get("thinking_block_corruption") is True

    # Generic crash → flag present and False (no downshift, no exemption).
    cfg.retry_state_file.unlink(missing_ok=True)
    tools.do_board_edit(cfg, {"action": "move_to_ready", "task_id": "TB-5"})
    task2 = Board.load(cfg.tasks_file).get("TB-5")

    class _Options:
        def __init__(self, **kw):
            self.kw = kw

    async def gen():
        if False:  # pragma: no cover - make this an async generator
            yield None
        raise RuntimeError("boom — unrelated crash")

    sdk2 = SimpleNamespace(query=lambda prompt, options: gen(),
                           ClaudeAgentOptions=_Options)
    asyncio.run(run_task(cfg, sdk2, None, task2))
    errs2 = [e for e in events.tail(cfg.events_file, 40)
             if e["type"] == "task_error" and e.get("task") == "TB-5"]
    assert errs2 and errs2[-1].get("thinking_block_corruption") is False
    assert retry.downshift_level(cfg.retry_state_file, "TB-5") == 0


# ===========================================================================
# (c) auto-approve breaker exemption for the thinking-block class.
# ===========================================================================

_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck plus the cost ceilings.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)

_BRIEFING = (
    "# A thinking-block recovery test briefing\n\n"
    "## Goal\n\n"
    "Make the thinking-block recovery path live so the end-to-end "
    "automation focus (`## Current focus: end-to-end automation`) can "
    "flip safely.\n\n"
    "Why now: an inert recovery path wedges the board on every "
    "thinking-block 400 and needs a manual operator approve, breaking "
    "the walk-away promise.\n\n"
    "## Scope\n\n- daemon.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _seed_auto_approved_task(cfg: Config, *, title: str) -> str:
    # TB-383: `board_edit` is policy-free; run the loop pass after the add
    # (caller sets `AP2_AUTO_APPROVE=1`) so the review token is stripped +
    # `auto_approved` emitted, reproducing the daemon's PRE_DISPATCH step.
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": title,
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot"],
        },
    )
    tb_id = _unwrap(res)["task_id"]
    run_auto_approve_pass(cfg)
    return tb_id


def _seed_operator_approved_task(cfg: Config, *, title: str) -> str:
    """An operator-added + operator-approved task: a gate-tag keeps it OUT of
    the auto bucket at add time, then `approve` strips the review token."""
    res = tools.do_board_edit(
        cfg,
        {
            "action": "add_backlog",
            "title": title,
            "blocked_on": "review",
            "briefing": _BRIEFING,
            "tags": ["#autopilot", "#breaking-change"],
        },
    )
    tid = _unwrap(res)["task_id"]
    tools.do_board_edit(cfg, {"action": "approve", "task_id": tid})
    return tid


def _stub_tick_quiet(monkeypatch) -> None:
    monkeypatch.setattr(
        tools, "drain_operator_queue",
        lambda cfg: {"applied": 0, "touched_paths": [], "force_ideate": False},
    )

    async def _noop_sweep(cfg, sdk):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_sweep)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    from ap2 import ideation as _ideation
    monkeypatch.setattr(_ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(_ideation, "force_ideate", _noop_async)
    # TB-381: the cron stage is now the `Phase.CRON_DISPATCH` walk into the
    # cron scheduler component; neutralize it by stubbing the component's
    # `load_jobs` (string target avoids importing the impl module here).
    monkeypatch.setattr("ap2.components.cron.impl.load_jobs", lambda path: [])

    async def _noop_run_task(cfg, sdk, mcp_server, task):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "run_task", _noop_run_task)


class _NoopSDK:
    def __init__(self) -> None:
        self.called = False

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


def test_thinking_block_task_error_does_not_trip_breaker(cfg, monkeypatch):
    """A `task_error` carrying `thinking_block_corruption=true` is EXEMPT
    from the TB-224 single-event breaker — `_auto_approve_check_violations`
    returns None — while an UNFLAGGED `task_error` for an auto-approved task
    still trips it. One classifier flag, one exemption."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    monkeypatch.delenv("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", raising=False)

    events.append(cfg.events_file, "auto_approved", task="TB-900", knob="1")
    # Flagged thinking-block task_error → exempt → no violation.
    events.append(
        cfg.events_file, "task_error", task="TB-900",
        error="RuntimeError: Claude Code returned an error result: success",
        thinking_block_corruption=True,
    )
    assert daemon._auto_approve_check_violations(cfg) is None

    # A genuine (unflagged) task_error on an auto-approved task DOES trip.
    events.append(cfg.events_file, "auto_approved", task="TB-901", knob="1")
    events.append(
        cfg.events_file, "task_error", task="TB-901",
        error="TimeoutError: SDK subprocess hung past deadline",
        thinking_block_corruption=False,
    )
    violation = daemon._auto_approve_check_violations(cfg)
    assert violation is not None
    reason, _u, _c, trigger, detail = violation
    assert reason == "task_error"
    assert trigger == "TB-901"
    assert "TimeoutError" in detail


def test_thinking_block_failure_not_counted_in_freeze_window(cfg, monkeypatch):
    """A `task_complete` carrying the thinking-block flag is excluded from the
    TB-223 consecutive-freeze window: three flagged completions (which would
    cross the default threshold of 3) do NOT pause, while three unflagged
    failures do."""
    monkeypatch.delenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", raising=False)

    def _fail(task, *, flag):
        events.append(
            cfg.events_file, "task_complete", task=task, status="error",
            commit="", summary="thinking-block",
            thinking_block_corruption=flag,
        )

    for t in ("TB-1", "TB-2", "TB-3"):
        _fail(t, flag=True)
    events.append(cfg.events_file, "retry_exhausted", task="TB-3",
                  attempts=5, last_status="error")
    assert daemon._auto_approve_paused(cfg) is False

    for t in ("TB-4", "TB-5", "TB-6"):
        _fail(t, flag=False)
    events.append(cfg.events_file, "retry_exhausted", task="TB-6",
                  attempts=5, last_status="error")
    assert daemon._auto_approve_paused(cfg) is True


# ===========================================================================
# Skip-past a gated Backlog head (defect 3).
# ===========================================================================


def test_gated_head_does_not_freeze_operator_work_behind_it(cfg, monkeypatch):
    """A Backlog ordered `[auto-approved (gated), operator-approved]` with the
    auto-approve window halted promotes the OPERATOR-approved task while the
    gated auto-approved head stays in Backlog with its skip event. Proves a
    gated head no longer freezes non-gated work behind it (the live
    2026-05-31 wedge: human-authored TB-361 stuck behind gated TB-359/360)."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "10000")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")

    # Prior auto-approved task burned the rolling-window budget → window_cap
    # halt is active for the auto layer.
    events.append(cfg.events_file, "auto_approved", task="TB-900", knob="1")
    events.append(
        cfg.events_file, "task_run_usage", task="TB-900", run_id="r-900-1",
        status="verification_failed", duration_s=10.0,
        usage={"input_tokens": 8000, "output_tokens": 4000,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        model_usage={}, total_cost_usd=0.5, num_turns=10, model="opus",
    )

    # Backlog HEAD = auto-approved (gated); BEHIND it = operator-approved.
    tb_gated = _seed_auto_approved_task(cfg, title="auto-approved gated head")
    tb_op = _seed_operator_approved_task(cfg, title="operator work behind gate")

    # Board order is [gated, op] and the window-cap violation is live.
    backlog_order = [t.id for t in Board.load(cfg.tasks_file).iter_tasks("Backlog")]
    assert backlog_order.index(tb_gated) < backlog_order.index(tb_op), backlog_order
    assert daemon._was_auto_approved(cfg, tb_gated) is True
    assert daemon._was_auto_approved(cfg, tb_op) is False
    assert daemon._auto_approve_check_violations(cfg) is not None

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # Operator work promoted despite the gated head ahead of it.
    loc_op = board.find(tb_op)
    assert loc_op is not None and loc_op[0] in ("Ready", "Active"), (
        f"operator-approved task must dispatch from behind a gated head; "
        f"got section={loc_op[0] if loc_op else 'missing'}"
    )
    # Gated auto-approved head stays held in Backlog.
    loc_gated = board.find(tb_gated)
    assert loc_gated is not None and loc_gated[0] == "Backlog", (
        f"gated auto-approved head must stay in Backlog; "
        f"got section={loc_gated[0] if loc_gated else 'missing'}"
    )
    # And the gated head emitted its skip event.
    skipped = [e for e in events.tail(cfg.events_file, 200)
               if e.get("type") == "auto_approve_skipped"
               and e.get("task") == tb_gated]
    assert skipped, "gated head must still emit auto_approve_skipped"
    assert skipped[-1]["reason"] == "window_cap"

    # At most ONE promotion this tick.
    promoted = [e for e in events.tail(cfg.events_file, 200)
                if e.get("type") == "backlog_auto_promoted"]
    assert len(promoted) == 1 and promoted[0]["task"] == tb_op, promoted


def test_all_gated_candidates_stay_held_no_promotion(cfg, monkeypatch):
    """When EVERY dispatchable candidate is auto-approved and the window is
    halted, none promote (the pause still holds the auto layer) — each gated
    candidate emits its skip event and the tick ends with no promotion."""
    monkeypatch.setenv("AP2_AUTO_APPROVE", "1")
    monkeypatch.setenv("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "10000")
    monkeypatch.setenv("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "0")
    events.append(cfg.events_file, "auto_approved", task="TB-900", knob="1")
    events.append(
        cfg.events_file, "task_run_usage", task="TB-900", run_id="r-900-1",
        status="verification_failed", duration_s=10.0,
        usage={"input_tokens": 8000, "output_tokens": 4000,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        model_usage={}, total_cost_usd=0.5, num_turns=10, model="opus",
    )
    tb_a = _seed_auto_approved_task(cfg, title="gated A")
    tb_b = _seed_auto_approved_task(cfg, title="gated B")

    _stub_tick_quiet(monkeypatch)
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find(tb_a)[0] == "Backlog"
    assert board.find(tb_b)[0] == "Backlog"
    promoted = [e for e in events.tail(cfg.events_file, 200)
                if e.get("type") == "backlog_auto_promoted"]
    assert promoted == [], promoted
    skipped_tasks = {e.get("task") for e in events.tail(cfg.events_file, 200)
                     if e.get("type") == "auto_approve_skipped"}
    assert {tb_a, tb_b} <= skipped_tasks, skipped_tasks
