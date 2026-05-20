"""TB-267: task-run live page tests — mirror of `ap2/web_tasks.py`.

Relocated from `ap2/tests/test_web.py` by the TB-267 split. Each test body
is byte-identical to its pre-TB-267 original; only the module home and the
shared `project` fixture + `_seed_run` / `_seed_vf_event` helpers' location
(now `ap2/tests/conftest.py`) changed.

Covers pages owned by `ap2/web_tasks.py`:
  - `/task-run/<run-id>` — `_render_task_run` (TB-129 live page).
  - `/task-run/<run-id>/stream.json` — `_render_task_run_stream_json`.
  - TB-157 usage totals footer + opt-in token columns.
  - TB-158 verification-failure summary block above the stream table.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from ap2 import web, events as ev_mod
from ap2.config import Config
from ap2.tests.conftest import _seed_run, _seed_vf_event


# --------- TB-129: live task-run detail page ---------


def test_render_task_run_renders_prompt_and_rows(project: Config):
    """Detail page surfaces the prompt and a row per stream entry, with
    color-coded classes by row role (assistant/tool/tool-result/result)."""
    _seed_run(project, run_id="20260430T140000Z-TB-20", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
        {"seq": 1, "type": "AssistantMessage",
         "text_preview": "thinking out loud", "model": "claude-opus-4-7"},
        {"seq": 2, "type": "AssistantMessage",
         "tool_calls": [{"name": "Bash",
                         "args_preview": '{"command": "ls"}'}]},
        {"seq": 3, "type": "UserMessage",
         "tool_results": [{"tool_use_id": "toolu_xyz",
                           "is_error": False, "preview": "file1\nfile2"}]},
        {"seq": 4, "type": "ResultMessage", "subtype": "success",
         "stop_reason": "end_turn", "num_turns": 5,
         "total_cost_usd": 0.1234},
    ])
    h = web._render_task_run(project, "20260430T140000Z-TB-20")
    # Page header
    assert "20260430T140000Z-TB-20" in h
    # Prompt block
    assert "system prompt body" in h
    # All five rows render
    for seq in range(5):
        assert f'data-seq="{seq}"' in h
    # Tool call name + args
    assert "Bash" in h and "ls" in h
    # Tool result preview survives
    assert "file1" in h
    # ResultMessage cost rendered as $-formatted dollars
    assert "$0.1234" in h
    # Color-code classes appear
    assert "row-assistant" in h
    assert "row-tool" in h
    assert "row-tool-result" in h
    assert 'row-result is-success' in h
    assert "row-system" in h


def test_render_task_run_in_flight_emits_poll_script(project: Config):
    """When no terminal event has landed, the page includes the auto-refresh
    `<script>` and the in-flight banner. Without the script, operators have
    to F5 manually — the whole point of this view is live-watch."""
    ev_mod.append(project.events_file, "task_start", task="TB-21", title="t")
    _seed_run(project, run_id="20260430T140000Z-TB-21", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
        {"seq": 1, "type": "AssistantMessage", "text_preview": "go"},
    ])
    h = web._render_task_run(project, "20260430T140000Z-TB-21")
    assert "in-flight" in h
    assert "setInterval" in h
    # The script builds the URL via `'/task-run/' + encodeURIComponent(runId)
    # + '/stream.json?since=' + since` — the literal pieces show up in the
    # source even though the full path is concatenated at call time.
    assert "'/task-run/'" in h
    assert "/stream.json?since='" in h
    assert '"20260430T140000Z-TB-21"' in h  # runId injected via json.dumps
    # since-cursor seeded at max(seq)+1 (rows seq 0,1 → since starts at 2)
    assert "since = 2" in h


def test_render_task_run_terminal_omits_script_and_shows_verdict(project: Config):
    """Once terminal, no polling — and a verdict banner displays the final
    status + commit prefix from the matching event."""
    _seed_run(project, run_id="20260430T140100Z-TB-22", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
        {"seq": 1, "type": "ResultMessage", "subtype": "success",
         "stop_reason": "end_turn", "num_turns": 3, "total_cost_usd": 0.05},
    ])
    ev_mod.append(project.events_file, "task_complete", task="TB-22",
                  status="complete", commit="cafe1234abcd",
                  summary="all good")
    h = web._render_task_run(project, "20260430T140100Z-TB-22")
    assert "setInterval" not in h
    assert "verdict success" in h
    assert "cafe1234" in h  # commit prefix in verdict
    assert "all good" in h  # summary surfaced


def test_render_task_run_invalid_run_id_blocks_path_traversal(project: Config):
    """`run_id` must match `<compact_ts>-<task_id>` — anything else (slashes,
    `..` segments, unmatched shape) is rejected without touching disk."""
    h = web._render_task_run(project, "../../../etc/passwd")
    assert "invalid run-id" in h
    h = web._render_task_run(project, "not-a-valid-shape")
    assert "invalid run-id" in h


def test_render_task_run_missing_files_returns_friendly_error(project: Config):
    """Run-id is well-formed but no `.stream.jsonl` on disk → friendly
    message + back-link to the task page (debug files were pruned)."""
    h = web._render_task_run(project, "20260101T000000Z-TB-NOPE")
    assert "No stream.jsonl" in h
    assert "/task/TB-NOPE" in h


def test_stream_json_endpoint_returns_new_rows_with_cursor(project: Config):
    """`/task-run/<rid>/stream.json?since=N` returns rows with seq >= N,
    advances next_since past the max seen, and reports in_flight."""
    _seed_run(project, run_id="20260430T150000Z-TB-30", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
        {"seq": 1, "type": "AssistantMessage", "text_preview": "hi"},
        {"seq": 2, "type": "AssistantMessage",
         "tool_calls": [{"name": "Bash", "args_preview": "{}"}]},
    ])
    status, data = web._render_task_run_stream_json(
        project, "20260430T150000Z-TB-30", since=1,
    )
    assert status == 200
    j = _json.loads(data)
    assert j["run_id"] == "20260430T150000Z-TB-30"
    assert j["in_flight"] is True
    assert j["terminal"] is None
    seqs = [r["seq"] for r in j["rows"]]
    assert seqs == [1, 2]
    assert j["next_since"] == 3
    # body_html includes the type-specific content (tool name for the call row)
    tool_row = next(r for r in j["rows"] if r["seq"] == 2)
    assert "Bash" in tool_row["body_html"]
    assert tool_row["css_class"] == "row-tool"


def test_stream_json_endpoint_rejects_bad_run_id(project: Config):
    """400 on an invalid shape — no disk touch, no JSON injection."""
    status, data = web._render_task_run_stream_json(project, "../etc", since=0)
    assert status == 400
    assert b"invalid" in data


def test_stream_json_endpoint_404_when_missing(project: Config):
    """404 when the stream file is gone — JS poller treats this as a soft
    error (catch in the fetch chain) and stops gracefully."""
    status, data = web._render_task_run_stream_json(
        project, "20260101T000000Z-TB-MIA", since=0,
    )
    assert status == 404


def test_stream_json_endpoint_reports_terminal(project: Config):
    """When a terminal event exists for the run's task, the JSON response
    flips `in_flight=False` and includes the terminal event under
    `terminal` — the JS poller's signal to stop the timer."""
    _seed_run(project, run_id="20260430T160000Z-TB-40", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
    ])
    ev_mod.append(project.events_file, "task_complete", task="TB-40",
                  status="complete", commit="abc123de")
    status, data = web._render_task_run_stream_json(
        project, "20260430T160000Z-TB-40", since=0,
    )
    j = _json.loads(data)
    assert status == 200
    assert j["in_flight"] is False
    assert j["terminal"]["type"] == "task_complete"
    assert j["terminal"]["status"] == "complete"


# --------- TB-157: usage / token totals footer + classify-row helper ---------


def test_render_task_run_renders_usage_totals_footer(project: Config):
    """When stream rows carry `usage`, the per-task-run detail page
    renders a totals block (input/output/cache/cost) summed across all
    ResultMessages in the run.
    """
    _seed_run(
        project,
        run_id="20260430T200000Z-TB-157",
        rows=[
            {"seq": 0, "type": "SystemMessage", "subtype": "init"},
            {"seq": 1, "type": "AssistantMessage",
             "text_preview": "thinking", "model": "claude-opus-4-7"},
            {"seq": 2, "type": "ResultMessage", "subtype": "success",
             "stop_reason": "end_turn", "num_turns": 3,
             "total_cost_usd": 0.1234,
             "usage": {
                 "input_tokens": 100,
                 "output_tokens": 50,
                 "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 80,
             }},
            {"seq": 3, "type": "ResultMessage", "subtype": "success",
             "stop_reason": "end_turn", "num_turns": 1,
             "total_cost_usd": 0.05,
             "usage": {
                 "input_tokens": 200,
                 "output_tokens": 30,
                 "cache_creation_input_tokens": 50,
                 "cache_read_input_tokens": 0,
             }},
        ],
    )
    h = web._render_task_run(project, "20260430T200000Z-TB-157")
    # Section header is present.
    assert "usage" in h.lower()
    # Sums across both ResultMessages: 100 + 200 = 300 input, 50 + 30 = 80
    # output, 0 + 50 = 50 cache_creation, 80 + 0 = 80 cache_read.
    assert "300" in h
    assert "80" in h
    # Total cost summed.
    assert "0.1734" in h or "0.1734" in h.replace(",", "")
    # Hit rate denominator = 80 + 50 + 300 = 430. cache_read = 80.
    # 80 / 430 ≈ 18.6%
    assert "18.6%" in h


def test_render_task_run_omits_usage_footer_when_no_usage(project: Config):
    """Legacy runs (pre-TB-157, no `usage` on any row) gracefully render
    without the footer block — no empty section, no zero-padded totals.
    """
    _seed_run(
        project,
        run_id="20260430T210000Z-TB-LEGACY",
        rows=[
            {"seq": 0, "type": "SystemMessage", "subtype": "init"},
            {"seq": 1, "type": "ResultMessage", "subtype": "success",
             "stop_reason": "end_turn", "num_turns": 1,
             "total_cost_usd": 0.01},
        ],
    )
    h = web._render_task_run(project, "20260430T210000Z-TB-LEGACY")
    # No usage-totals section header (the only `<h2>usage` rendering point).
    assert "<h2>usage" not in h
    assert "usage-totals" not in h


def test_compute_run_usage_totals_hit_rate():
    """White-box pin on the hit-rate formula: cache_read divided by the
    full input footprint (cache_read + cache_creation + input_tokens).
    """
    rows = [
        {"type": "ResultMessage", "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
        }, "total_cost_usd": 0.0},
    ]
    t = web._compute_run_usage_totals(rows)
    # Pure cache hit: 100 / 100 = 1.0.
    assert t["hit_rate"] == 1.0
    assert t["cache_read"] == 100


def test_classify_row_assigns_expected_classes():
    """White-box check on the row-class mapping. The CSS depends on these
    exact class strings; renaming one without updating the stylesheet would
    silently strip color-coding."""
    assert web._classify_row({"type": "AssistantMessage",
                              "text_preview": "x"})[0] == "row-assistant"
    assert web._classify_row({"type": "AssistantMessage",
                              "tool_calls": [{}]})[0] == "row-tool"
    assert web._classify_row({"type": "UserMessage",
                              "tool_results": [{"is_error": False}]})[0] \
        == "row-tool-result"
    assert web._classify_row({"type": "UserMessage",
                              "tool_results": [{"is_error": True}]})[0] \
        == "row-tool-result is-error"
    assert web._classify_row({"type": "ResultMessage",
                              "subtype": "success"})[0] \
        == "row-result is-success"
    assert web._classify_row({"type": "ResultMessage",
                              "subtype": "error_max_turns"})[0] \
        == "row-result"
    assert web._classify_row({"type": "SystemMessage"})[0] == "row-system"


# ---------------------------------------------------------------------------
# TB-158: verification-failure summary block on the task-run page.


def test_task_run_page_shows_verification_summary_block(
    project: Config, tmp_path: Path,
):
    """`/task-run/<run-id>` for a run whose terminal event is
    `verification_failed` (per-task kind) renders a top-of-page block
    naming the failing bullets and judge notes — operators arriving from
    a `task_complete` link see WHY immediately."""
    _seed_run(
        project,
        run_id="20260430T220000Z-TB-310",
        rows=[{"seq": 0, "type": "SystemMessage", "subtype": "init"}],
    )
    # Append the structured verification_failed event AFTER the run start.
    _seed_vf_event(
        project,
        task="TB-310",
        pass_n=3,
        fail_bullets=[
            ("prose", "Manual: confirm the alpha decay claim against "
                      "live regime X data",
             "no live regime evidence captured in commit"),
        ],
        unverified_n=0,
    )
    # AND the lifecycle task_complete row the daemon also emits — both
    # shapes must trigger the summary block.
    ev_mod.append(
        project.events_file, "task_complete",
        task="TB-310", status="verification_failed", commit="abc12345",
        summary="rolled to Backlog",
    )
    h = web._render_task_run(project, "20260430T220000Z-TB-310")
    # The verification-summary block sits ABOVE the stream table.
    # The raw substring `verif-summary` also appears in the page's CSS
    # (stylesheet rule for the block); pin on the rendered <div> instead
    # so a stylesheet-only match doesn't satisfy this gate.
    body_html = h.split("</style>", 1)[1] if "</style>" in h else h
    assert 'class="verif-summary"' in body_html
    assert "Verification: 3/4 passed" in body_html
    assert "1 failed" in body_html
    assert "0 unverified" in body_html
    # Failing-bullet headline + judge note both surface.
    assert "Manual: confirm the alpha decay claim" in body_html
    assert "no live regime evidence captured" in body_html
    # Block ordering: the summary lives above the stream <h2>.
    assert body_html.index('class="verif-summary"') < body_html.index("<h2>stream")


def test_task_run_page_omits_verification_summary_on_success(
    project: Config, tmp_path: Path,
):
    """A run that landed successfully — terminal event is `task_complete`
    with `status=complete` — does NOT show the verification-summary
    block. The block is failure-only signal; firing it on green runs
    would be noise."""
    _seed_run(
        project,
        run_id="20260430T230000Z-TB-311",
        rows=[{"seq": 0, "type": "SystemMessage", "subtype": "init"}],
    )
    ev_mod.append(
        project.events_file, "task_complete",
        task="TB-311", status="complete", commit="cafe1234",
        summary="all good",
    )
    h = web._render_task_run(project, "20260430T230000Z-TB-311")
    # `verif-summary` appears in the page's stylesheet — strip the CSS
    # block before asserting absence of the rendered <div>.
    body_html = h.split("</style>", 1)[1] if "</style>" in h else h
    assert 'class="verif-summary"' not in body_html
    # Sanity: the regular verdict banner still renders.
    assert "verdict success" in body_html


def test_task_run_page_summary_uses_latest_verification_failed_event(
    project: Config, tmp_path: Path,
):
    """When two `verification_failed` events exist for the same task
    (older attempt followed by a fresh one in this run window), the
    block must surface the LATEST — operators looking at the page after
    a retry shouldn't see the previous run's failure."""
    _seed_run(
        project,
        run_id="20260430T230500Z-TB-312",
        rows=[{"seq": 0, "type": "SystemMessage", "subtype": "init"}],
    )
    # Stale failure from a prior attempt (before the run start) — must
    # be excluded by the at-or-after window.
    with project.events_file.open("a") as f:
        f.write(_json.dumps({
            "ts": "2026-04-30T22:00:00Z",
            "type": "verification_failed",
            "task": "TB-312",
            "kind": "per_task",
            "overall": "fail",
            "criteria": [
                {"kind": "shell", "status": "fail",
                 "bullet": "OLD-FAILURE-bullet", "notes": "old-note"},
            ],
        }) + "\n")
    # Newer failure that should win (post-run-start).
    with project.events_file.open("a") as f:
        f.write(_json.dumps({
            "ts": "2026-04-30T23:06:00Z",
            "type": "verification_failed",
            "task": "TB-312",
            "kind": "per_task",
            "overall": "fail",
            "criteria": [
                {"kind": "prose", "status": "fail",
                 "bullet": "NEW-FAILURE-bullet", "notes": "new-note"},
            ],
        }) + "\n")
    ev_mod.append(
        project.events_file, "task_complete",
        task="TB-312", status="verification_failed", commit="",
        summary="rolled",
    )
    h = web._render_task_run(project, "20260430T230500Z-TB-312")
    body_html = h.split("</style>", 1)[1] if "</style>" in h else h
    assert 'class="verif-summary"' in body_html
    # Newer failure shown.
    assert "NEW-FAILURE-bullet" in body_html
    # Older failure excluded — prevents stale information confusing
    # operators reviewing a retried task.
    assert "OLD-FAILURE-bullet" not in body_html
