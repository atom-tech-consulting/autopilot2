"""Tests for `ap2.web` — the local read-only web UI (TB-99 → TB-93 thaw).

Exercises the renderers against a synthetic project. Skips spinning up a
real HTTP server; the handler is a thin urlsplit + dispatch wrapper around
the same renderers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import web, events as ev_mod
from ap2.board import Board
from ap2.config import Config


@pytest.fixture
def project(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n- [ ] **TB-1** **Active task**\n"
        "## Ready\n\n"
        "## Backlog\n\n- [ ] **TB-2** **Backlog task** `#tag`\n"
        "## Complete\n\n- [x] **TB-3** **Done thing** — summary text\n"
        "## Frozen\n\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    ev_mod.append(cfg.events_file, "daemon_start")
    ev_mod.append(cfg.events_file, "task_complete", task="TB-3", status="complete",
                  commit="abc12345", summary="finished it")
    ev_mod.append(cfg.events_file, "task_error", task="TB-2", error="ValueError: x")
    ev_mod.append(cfg.events_file, "ideation_empty_board", cooldown_s=7200)
    return cfg


def test_home_renders(project: Config):
    html = web._render_home(project)
    assert "<!DOCTYPE html>" in html
    assert "TB-3" in html or "Active" in html  # board section labels present
    assert "daemon" in html.lower()
    # All four events surface in the events table
    assert "task_complete" in html
    assert "task_error" in html
    assert "ideation_empty_board" in html
    assert "daemon_start" in html


def test_home_marks_failure_class(project: Config):
    html = web._render_home(project)
    # task_error is in FAILURE_EVENT_TYPES → row gets the `failure` class
    assert 'class="failure"' in html


def test_verification_partial_gets_warning_tint(project: Config):
    """`verification_partial` lands tasks in Complete, so it's not a daemon-
    health failure (and isn't in diagnose.FAILURE_EVENT_TYPES). But the
    operator should still notice it — partial verdicts often signal a prose
    bullet the SDK judge can't evaluate. Web UI gives it a `warning` class
    distinct from `failure`."""
    ev_mod.append(project.events_file, "verification_partial",
                  task="TB-3", criterion="some prose claim")
    html = web._render_events(project, typ=None, n=50)
    assert 'class="warning"' in html
    # Sanity: verification_partial did NOT also pick up the failure class.
    rows = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    partial_row = next(r for r in rows.split("<tr ") if "verification_partial" in r)
    assert 'class="warning"' in partial_row
    assert 'class="failure"' not in partial_row


def test_verification_partial_in_quick_filters(project: Config):
    """The events page exposes a `verification_partial` quick-filter button so
    the operator can see all of them at once without typing the URL."""
    html = web._render_events(project, typ=None, n=10)
    assert "?type=verification_partial" in html


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


def test_events_page_tints_task_complete_rows_by_status(project: Config):
    """End-to-end: the rendered events HTML must carry the matching CSS
    class on each task_complete row's `<tr>`, and not collapse them into a
    single uniform color. Probes for each row by its summary string so the
    assertion isn't fragile to row order."""
    # Fixture already has TB-3 task_complete with status=complete (green).
    # Stack on the failure modes the briefing calls out.
    ev_mod.append(project.events_file, "task_complete", task="TB-A",
                  status="verification_failed", commit="aaa11111",
                  summary="probe-verif-failed")
    ev_mod.append(project.events_file, "task_complete", task="TB-B",
                  status="state_violation", commit="",
                  summary="probe-state-violation")
    ev_mod.append(project.events_file, "task_complete", task="TB-C",
                  status="retry_exhausted", commit="",
                  summary="probe-retry-exhausted")
    ev_mod.append(project.events_file, "task_complete", task="TB-D",
                  status="totally-bogus", commit="",
                  summary="probe-bogus")
    html = web._render_events(project, typ="task_complete", n=50)
    rows_block = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    def _row_for(needle: str) -> str:
        return next(r for r in rows_block.split("<tr ") if needle in r)

    # Original fixture row (status=complete) → lifecycle (green).
    assert 'class="lifecycle"' in _row_for("finished it")
    # verification_failed → warning (orange).
    assert 'class="warning"' in _row_for("probe-verif-failed")
    # state_violation → failure (red).
    assert 'class="failure"' in _row_for("probe-state-violation")
    # retry_exhausted → frozen (dark red).
    assert 'class="frozen"' in _row_for("probe-retry-exhausted")
    # Unknown status → neutral (gray) — defensive bucket.
    assert 'class="neutral"' in _row_for("probe-bogus")


def test_events_page_includes_legend(project: Config):
    """A small legend block teaches the row colors on the /events page so
    a first-time viewer can map color → meaning without reading code.
    Behind a `<details>` to keep the chrome quiet for repeat visitors."""
    html = web._render_events(project, typ=None, n=10)
    # The legend is wrapped in a <details> with summary "row colors".
    assert "row colors" in html
    # Each tint label appears in the legend swatch list.
    for swatch in ("complete", "verification_failed", "state_violation",
                   "retry_exhausted"):
        assert swatch in html


def test_events_renders_full_text(project: Config):
    """Truncation-free rendering — long values land verbatim in the page.

    This is the whole point of the web UI vs `ap2 logs`: the latter caps each
    field at 120 chars (`_short`), the former shows the entire value.
    """
    long_summary = "x" * 1000
    ev_mod.append(project.events_file, "cron_complete", job="status-report",
                  summary=long_summary)
    html = web._render_events(project, typ=None, n=50)
    assert long_summary in html  # full text, no ellipsis truncation


def test_events_filters_by_type(project: Config):
    html = web._render_events(project, typ="task_error", n=50)
    assert "task_error" in html
    # task_complete shouldn't appear in the events table when filtered to task_error.
    # (It still appears in the filter button list — guard against false hits there.)
    rows_block = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0] if "<tbody>" in html else ""
    assert "task_complete" not in rows_block
    assert "ideation_empty_board" not in rows_block


def test_tasks_groups_by_section(project: Config):
    html = web._render_tasks(project)
    assert "Active" in html and "TB-1" in html
    assert "Backlog" in html and "TB-2" in html
    assert "Complete" in html and "TB-3" in html
    # Frozen is empty → "(empty)" placeholder
    assert "Frozen" in html


def test_task_detail_pulls_related_events(project: Config):
    html = web._render_task(project, "TB-3")
    assert "TB-3" in html and "Done thing" in html
    # The related-events scan should match task_complete (task=TB-3)
    assert "task_complete" in html
    # An unrelated event (TB-2's task_error) should NOT appear in this task's view
    related_block = html.split("related events", 1)[1] if "related events" in html else ""
    assert "TB-2" not in related_block


def test_task_detail_includes_briefing_when_present(project: Config, tmp_path: Path):
    briefing_path = tmp_path / ".cc-autopilot" / "tasks" / "tb4-thing.md"
    briefing_path.parent.mkdir(parents=True, exist_ok=True)
    briefing_path.write_text("# TB-4 brief\n\n## Goal\nDo the thing.\n## Verification\n- pytest\n")
    board = Board.load(project.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-4",
        title="With briefing",
        briefing=".cc-autopilot/tasks/tb4-thing.md",
    )
    board.save()
    html = web._render_task(project, "TB-4")
    assert "## Goal" in html
    assert "Do the thing." in html
    assert "## Verification" in html


def test_task_detail_404_for_missing(project: Config):
    html = web._render_task(project, "TB-999")
    assert "TB-999" in html
    assert "Not on the board" in html


# --------- TB-93 thaw: pipelines / insights / ideation_state / commits ---------


def test_pipelines_lists_pipeline_starts(project: Config):
    ev_mod.append(
        project.events_file, "pipeline_start",
        name="my-sweep", pid=99999, command="uv run python scripts/sweep.py",
        validation="TB-7", log="/tmp/my-sweep-99999.log",
    )
    html = web._render_pipelines(project)
    assert "my-sweep" in html
    assert "TB-7" in html
    assert "/task/TB-7" in html
    assert "uv run python scripts/sweep.py" in html
    # PID 99999 is almost certainly not alive in CI
    assert "dead/exited" in html


def test_pipelines_empty_state(project: Config):
    html = web._render_pipelines(project)
    assert "no pipeline_start events" in html


def test_insights_shows_files(project: Config, tmp_path):
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "alpha.md").write_text(
        "---\n"
        "tldr: Alpha decay observed in regime X\n"
        "updated: 2026-04-28T10:00:00Z\n"
        "updated_by: TB-50\n"
        "cites: [TB-49, TB-50]\n"
        "---\n\n"
        "Body content.\n"
    )
    html = web._render_insights(project)
    assert "alpha.md" in html
    assert "Alpha decay observed in regime X" in html
    assert "TB-50" in html


def test_insights_empty_state(project: Config):
    html = web._render_insights(project)
    assert "no insights dir" in html or "empty" in html


def test_insight_detail_shows_full_content(project: Config, tmp_path):
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    body = "---\ntldr: x\nupdated: 2026-04-28\nupdated_by: op\ncites: []\n---\n# Header\n\nBody.\n"
    (insights_dir / "alpha.md").write_text(body)
    html = web._render_insight(project, "alpha.md")
    assert "Header" in html
    assert "Body." in html


def test_insight_404(project: Config):
    html = web._render_insight(project, "nonexistent.md")
    assert "not found" in html


def test_insight_blocks_path_traversal(project: Config):
    html = web._render_insight(project, "../../../../etc/passwd")
    assert "invalid" in html.lower()


def test_ideation_state_shows_file_and_summary(project: Config, tmp_path):
    state_path = tmp_path / ".cc-autopilot" / "ideation_state.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("# Ideation State\n\n## Mission alignment\nAll good.\n")
    ev_mod.append(
        project.events_file, "ideation_complete",
        summary="Cycle 4: proposed TB-100, TB-101, TB-102.",
    )
    ev_mod.append(project.events_file, "ideation_state_updated", bytes=120)
    html = web._render_ideation_state(project)
    assert "All good." in html
    assert "Cycle 4" in html
    assert "120" in html


def test_ideation_state_no_file(project: Config):
    html = web._render_ideation_state(project)
    assert "not yet written" in html or "ideation_state.md" in html


def test_commits_in_non_git_dir(project: Config):
    html = web._render_commits(project)
    assert "not a git repo" in html


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


# --------- TB-129: live task-run detail page ---------


import json as _json


def _seed_run(
    project: Config,
    *,
    run_id: str,
    rows: list[dict],
    full_rows: list[dict] | None = None,
    prompt: str = "system prompt body…\n\nUser: do the thing.",
) -> tuple[Path, Path, Path]:
    """Synthesize a debug-dump triple on disk — mirror of `_prep_debug_dumps`."""
    d = project.project_root / ".cc-autopilot" / "debug"
    d.mkdir(parents=True, exist_ok=True)
    prompt_p = d / f"{run_id}.prompt.md"
    stream_p = d / f"{run_id}.stream.jsonl"
    messages_p = d / f"{run_id}.messages.jsonl"
    prompt_p.write_text(prompt)
    stream_p.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    messages_p.write_text(
        "\n".join(_json.dumps(r) for r in (full_rows or rows)) + "\n"
    )
    return prompt_p, stream_p, messages_p


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


def test_events_table_links_task_start_rows_to_run_view(project: Config):
    """`task_start` rows in the events table get a `→ live` link to
    `/task-run/<run-id>` when the debug files exist on disk; rows without
    files get no link (avoids 404s for pruned runs)."""
    _seed_run(project, run_id="20260430T170000Z-TB-60", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
    ])
    ev_mod.append(project.events_file, "task_start", task="TB-60",
                  title="With debug files",
                  ts="2026-04-30T17:00:00Z")
    # Manually inject a task_start whose ts matches the seeded run since
    # `events.append` stamps `ts` itself; we rebuild via the public renderer.
    # Hack: write directly to the events file with controlled ts.
    extra = {"ts": "2026-04-30T17:00:00Z", "type": "task_start",
             "task": "TB-60", "title": "Seeded run"}
    with project.events_file.open("a") as f:
        f.write(_json.dumps(extra) + "\n")
    # Also one whose files do NOT exist — should NOT produce a link.
    extra2 = {"ts": "2026-04-30T17:00:00Z", "type": "task_start",
              "task": "TB-NONE", "title": "No debug"}
    with project.events_file.open("a") as f:
        f.write(_json.dumps(extra2) + "\n")
    h = web._render_events(project, typ="task_start", n=10)
    assert "/task-run/20260430T170000Z-TB-60" in h
    assert "/task-run/20260430T000000Z-TB-NONE" not in h
    # Non-task_start rows never get the run-link badge.
    h_all = web._render_events(project, typ=None, n=50)
    # Confirm the link badge text is row-anchored to task_start
    rows_block = h_all.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    for row in rows_block.split("<tr "):
        if "→ live" in row:
            assert "task_start" in row, "run-link should only appear on task_start"


def test_task_page_lists_runs_with_status_badges(project: Config):
    """Per-task page includes a Runs section sourced from disk, newest first,
    each row linked to `/task-run/<rid>` with a terminal-status badge when
    known. Pruned runs show `none on disk`."""
    # Two runs: one terminal, one in-flight.
    _seed_run(project, run_id="20260430T180000Z-TB-70", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
    ])
    _seed_run(project, run_id="20260430T190000Z-TB-70", rows=[
        {"seq": 0, "type": "SystemMessage", "subtype": "init"},
    ])
    ev_mod.append(project.events_file, "task_complete", task="TB-70",
                  status="complete", commit="abc12345",
                  ts="2026-04-30T18:30:00Z")
    # Inject the task_complete with a controlled ts so it sits between the
    # two run starts (terminating only the first).
    with project.events_file.open("a") as f:
        f.write(_json.dumps({
            "ts": "2026-04-30T18:30:00Z", "type": "task_complete",
            "task": "TB-70", "status": "complete", "commit": "abc12345",
        }) + "\n")
    # Add the task to the board so /task/TB-70 renders.
    board = Board.load(project.tasks_file)
    board.add("Backlog", task_id="TB-70", title="Two-run task")
    board.save()
    h = web._render_task(project, "TB-70")
    assert "runs" in h.lower()
    assert "/task-run/20260430T180000Z-TB-70" in h
    assert "/task-run/20260430T190000Z-TB-70" in h
    # Newest first: 19:00 should appear before 18:00
    assert h.index("20260430T190000Z-TB-70") < h.index("20260430T180000Z-TB-70")
    # Status badges
    assert "in-flight" in h  # 19:00 has no terminal


def test_task_page_runs_section_handles_no_disk_files(project: Config):
    """Task with no debug dumps on disk shows a friendly placeholder, not
    a broken/empty table."""
    board = Board.load(project.tasks_file)
    board.add("Backlog", task_id="TB-71", title="No runs yet")
    board.save()
    h = web._render_task(project, "TB-71")
    assert "none on disk" in h


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


# --------- TB-130: daemon-bundled web lifecycle ---------


def test_is_web_disabled_truthy_values(monkeypatch):
    """The `AP2_WEB_DISABLED` env knob accepts the standard ap2 truthy
    strings (1/true/yes/on, case-insensitive). Anything else (including
    unset) keeps the UI on so daemon-spawned mode is the default."""
    for val in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv("AP2_WEB_DISABLED", val)
        assert web.is_web_disabled() is True, val
    for val in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("AP2_WEB_DISABLED", val)
        assert web.is_web_disabled() is False, val
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)
    assert web.is_web_disabled() is False


def test_daemon_web_port_default_and_override(monkeypatch):
    """Default 8729 (TB-130 spec); `AP2_WEB_PORT` overrides; malformed
    values fall back to the default rather than crashing the daemon at
    startup."""
    monkeypatch.delenv("AP2_WEB_PORT", raising=False)
    assert web.daemon_web_port() == web.DEFAULT_DAEMON_WEB_PORT == 8729

    monkeypatch.setenv("AP2_WEB_PORT", "9999")
    assert web.daemon_web_port() == 9999

    # A typo shouldn't kill the daemon — fall back rather than ValueError.
    monkeypatch.setenv("AP2_WEB_PORT", "not-a-number")
    assert web.daemon_web_port() == web.DEFAULT_DAEMON_WEB_PORT


def _free_port() -> int:
    """Bind 0 to grab a kernel-assigned port, then release it. Cheap; the
    test grabs the same port a moment later for the real bind."""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def test_serve_async_serves_and_cancels_cleanly(project: Config):
    """`serve_async` is the daemon-managed entry point: bind a real socket,
    confirm a request lands, then cancel the task and confirm the port
    frees up. End-to-end check that cancellation actually shuts down the
    HTTP server thread (otherwise restarting the daemon would EADDRINUSE)."""
    import asyncio
    import urllib.request

    port = _free_port()

    async def _exercise() -> tuple[int, str]:
        task = asyncio.create_task(
            web.serve_async(project, host="127.0.0.1", port=port)
        )
        # The bind happens synchronously inside `serve_async` before it
        # parks on Event.wait, but the thread's first accept() takes a
        # tick — yield control once so the listener is ready.
        for _ in range(50):
            await asyncio.sleep(0.02)
            try:
                resp = await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"http://127.0.0.1:{port}/", None, 2.0,
                )
                body = resp.read().decode()
                status = resp.status
                resp.close()
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError("server never accepted a request")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return status, body

    status, body = asyncio.run(_exercise())
    assert status == 200
    assert "<!DOCTYPE html>" in body
    # And the port is releasable — otherwise the next daemon restart trips
    # EADDRINUSE. `_free_port` will throw or return a different port if
    # this one is still bound; a fresh bind on the same port should work.
    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_serve_async_propagates_bind_error(project: Config):
    """A port collision should surface as `OSError` from `serve_async` so
    the daemon can log a `web_error` event instead of crashing the loop."""
    import asyncio
    import socket

    port = _free_port()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)

    async def _go():
        with pytest.raises(OSError):
            await web.serve_async(project, host="127.0.0.1", port=port)

    try:
        asyncio.run(_go())
    finally:
        blocker.close()
