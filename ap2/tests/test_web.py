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


# --------- TB-121: pending-review pill + filter ---------


def _project_with_review_gate(tmp_path: Path) -> Config:
    """Synthetic project where Backlog has a `@blocked:review` task and
    a plain task — exercises both the pill and the filter."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-50** **proposal** `@blocked:review` — needs approval\n"
        "- [ ] **TB-51** **regular** — no blockers\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_tasks_page_shows_pending_review_pill(tmp_path: Path):
    """TB-121: a Backlog task with `@blocked:review` renders a `pending
    review` pill so operators can spot the gate without reading the
    raw codespan column."""
    cfg = _project_with_review_gate(tmp_path)
    html = web._render_tasks(cfg)
    assert "pending review" in html
    # The pill is on TB-50 (gated), NOT TB-51 (ungated). We crudely
    # check that the pill text is in the same vicinity as TB-50 by
    # asserting that TB-51's `<li>` doesn't carry the pill class.
    assert "TB-50" in html
    assert "TB-51" in html
    # Two-step: split out the TB-51 list-item, ensure no pill.
    li_tb51 = next(
        chunk for chunk in html.split("<li>") if "TB-51" in chunk
    )
    assert "pending-review" not in li_tb51
    li_tb50 = next(
        chunk for chunk in html.split("<li>") if "TB-50" in chunk
    )
    assert "pending-review" in li_tb50


def test_tasks_page_filter_pending_review_narrows(tmp_path: Path):
    """`?filter=pending-review` restricts the page to review-gated
    tasks. TB-50 must appear; TB-51 must not."""
    cfg = _project_with_review_gate(tmp_path)
    html = web._render_tasks(cfg, filter_kind="pending-review")
    assert "TB-50" in html
    assert "TB-51" not in html
    # The filter chip is highlighted on the active filter.
    assert 'href="/tasks?filter=pending-review"' in html


def test_tasks_page_filter_bar_shown_unfiltered(tmp_path: Path):
    """The filter bar is always rendered on `/tasks` so an operator can
    jump straight to the review queue without typing the URL."""
    cfg = _project_with_review_gate(tmp_path)
    html = web._render_tasks(cfg)
    assert 'href="/tasks?filter=pending-review"' in html
    assert "pending review" in html


def test_tasks_page_filter_empty_state(tmp_path: Path):
    """When no tasks are pending review, the filter view says so
    (instead of rendering an empty page that looks like a bug)."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-1** **regular** — no blockers\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    html = web._render_tasks(cfg, filter_kind="pending-review")
    assert "no tasks pending review" in html.lower()


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


# --------- TB-157: usage / token totals footer + show=tokens ---------


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
    the daemon can log a `web_error` event instead of crashing the loop.

    TB-155: auto-enumeration is opt-out via `max_attempts=1` — pre-TB-155
    callers that want the original "first failure raises" behavior keep
    that contract by passing `max_attempts=1`. The daemon path now sets
    `max_attempts=10` and only raises after exhausting the range; that's
    covered by `test_serve_async_range_exhausted_raises` below.
    """
    import asyncio
    import socket

    port = _free_port()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)

    async def _go():
        with pytest.raises(OSError):
            await web.serve_async(
                project, host="127.0.0.1", start_port=port, max_attempts=1,
            )

    try:
        asyncio.run(_go())
    finally:
        blocker.close()


# --------- TB-155: web port auto-enumerate on conflict ---------


def test_bind_with_enumeration_no_conflict_returns_start_port():
    """Happy path: when `start_port` is free, the helper binds it and
    returns it untouched — no enumeration churn, no offset added.
    Establishes the baseline contract before testing the conflict cases."""
    port = _free_port()
    sock, bound = web._bind_with_enumeration(
        "127.0.0.1", port, web.DEFAULT_WEB_PORT_MAX_ATTEMPTS,
    )
    try:
        assert bound == port
        # The returned socket is actually bound to that port (so the caller
        # can hand it to `socketserver` and start serving without re-binding).
        assert sock.getsockname()[1] == port
    finally:
        sock.close()


def test_bind_with_enumeration_skips_busy_port():
    """When `start_port` is already bound, the helper walks forward and
    binds the next free port. This is the core TB-155 behavior — silently
    paper over a single-port collision (typically a stale daemon or an
    `ap2 web` standalone) instead of failing the whole web UI."""
    import socket as _sock

    port = _free_port()
    # Block `port`; leave `port+1` open. The helper should pick `port+1`.
    blocker = _sock.socket()
    blocker.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        sock, bound = web._bind_with_enumeration("127.0.0.1", port, 10)
        try:
            assert bound == port + 1, (
                f"expected port+1={port + 1}, got {bound}"
            )
            assert sock.getsockname()[1] == port + 1
        finally:
            sock.close()
    finally:
        blocker.close()


def test_bind_with_enumeration_exhausts_range_and_raises():
    """When ALL ports in the enumerated range are bound, the helper
    raises a single `OSError` whose message names the range — no infinite
    loop, no climb into the ephemeral range. The error message is the
    operator's only handle on the conflict, so it must include the
    boundaries they need to investigate."""
    import socket as _sock

    # Grab a contiguous range of free ports first (kernel-assigned ports
    # aren't guaranteed contiguous, so probe upward from a free start).
    start = _free_port()
    blockers = []
    n = 4  # tight range so the test is cheap.
    try:
        for offset in range(n):
            s = _sock.socket()
            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", start + offset))
            except OSError:
                # Skip this run — port races with another process.
                pytest.skip("contiguous port range unavailable")
            s.listen(1)
            blockers.append(s)
        with pytest.raises(OSError) as exc:
            web._bind_with_enumeration("127.0.0.1", start, n)
        msg = str(exc.value)
        # The range must be in the message — that's the audit trail the
        # operator gets in the `web_error` event payload.
        assert f"{start}..{start + n - 1}" in msg, msg
    finally:
        for s in blockers:
            s.close()


def test_bind_with_enumeration_non_eaddrinuse_propagates_immediately():
    """Errors other than EADDRINUSE (e.g. permission denied on a privileged
    port) shouldn't trigger enumeration — walking forward wouldn't help and
    would just produce N noisy retries. The first non-EADDRINUSE OSError
    propagates as-is."""
    # Privileged port on a non-root user trips EACCES, not EADDRINUSE.
    # Skip when we happen to have privilege (e.g. running as root in CI).
    import os as _os
    if _os.geteuid() == 0:
        pytest.skip("test requires non-root euid to trip EACCES")
    with pytest.raises(OSError) as exc:
        web._bind_with_enumeration("127.0.0.1", 1, 10)
    # Either EACCES (Linux) or EPERM (macOS) — both are non-EADDRINUSE,
    # which is the contract we're checking. Concretely: the message must
    # NOT contain the "no free port in range" wording, because that would
    # mean the helper enumerated through privileged ports instead of
    # raising on the first failure.
    assert "no free port in range" not in str(exc.value), (
        "non-EADDRINUSE errors must propagate without enumeration"
    )


def test_serve_async_no_conflict_binds_start_port(project: Config):
    """TB-155 baseline: with no port collision, `serve_async(start_port=X)`
    binds X exactly — no enumeration churn, the `on_bind` callback fires
    with the requested port. The daemon wrapper relies on this equality
    to decide whether to omit the `requested_port` field from the
    `web_start` event (the audit signal that a silent enumeration
    happened); if `serve_async` ever drifted off the requested port on
    the happy path, every daemon startup would emit a spurious
    `requested_port` and the audit signal would lose its meaning."""
    import asyncio

    start_port = _free_port()
    bound_holder: dict = {}

    def _on_bind(host: str, port: int) -> None:
        bound_holder["host"] = host
        bound_holder["port"] = port

    async def _exercise() -> None:
        task = asyncio.create_task(
            web.serve_async(
                project,
                host="127.0.0.1",
                start_port=start_port,
                max_attempts=10,
                on_bind=_on_bind,
            )
        )
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "port" in bound_holder:
                    break
            assert "port" in bound_holder, "on_bind never fired"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_exercise())

    # Resolved port equals requested — no enumeration, no offset.
    assert bound_holder["host"] == "127.0.0.1"
    assert bound_holder["port"] == start_port, (
        f"expected bound=={start_port} on the no-conflict path, "
        f"got {bound_holder['port']}"
    )


def test_serve_async_auto_enumerates_on_conflict(project: Config):
    """End-to-end: with `start_port` already bound, `serve_async` quietly
    binds the next free port and the `on_bind` callback fires with the
    resolved port. Mirrors what `_web_loop_for_daemon` relies on for its
    `web_start` event payload."""
    import asyncio
    import socket as _sock
    import urllib.request

    start_port = _free_port()
    blocker = _sock.socket()
    blocker.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", start_port))
    blocker.listen(1)

    bound_holder: dict = {}

    def _on_bind(host: str, port: int) -> None:
        bound_holder["host"] = host
        bound_holder["port"] = port

    async def _exercise() -> None:
        task = asyncio.create_task(
            web.serve_async(
                project,
                host="127.0.0.1",
                start_port=start_port,
                max_attempts=10,
                on_bind=_on_bind,
            )
        )
        # Wait for `on_bind` to fire and a request to land on the
        # auto-enumerated port — confirms the server is actually listening
        # on the resolved port, not the requested one.
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "port" in bound_holder:
                    break
            assert "port" in bound_holder, "on_bind never fired"
            assert bound_holder["port"] != start_port, (
                "should have enumerated past the busy port"
            )
            resp = await asyncio.to_thread(
                urllib.request.urlopen,
                f"http://127.0.0.1:{bound_holder['port']}/", None, 2.0,
            )
            assert resp.status == 200
            resp.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(_exercise())
    finally:
        blocker.close()

    assert bound_holder["host"] == "127.0.0.1"
    assert bound_holder["port"] == start_port + 1


def test_serve_async_range_exhausted_raises(project: Config):
    """When the entire enumeration range is bound, `serve_async` re-raises
    the helper's `OSError` so the daemon can log a single `web_error`
    naming the range — the operator's hunt for the offending pid starts
    there. No silent fall-through, no climb past `max_attempts`."""
    import asyncio
    import socket as _sock

    start = _free_port()
    n = 3
    blockers = []
    try:
        for offset in range(n):
            s = _sock.socket()
            s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", start + offset))
            except OSError:
                pytest.skip("contiguous port range unavailable")
            s.listen(1)
            blockers.append(s)

        async def _go():
            with pytest.raises(OSError) as exc:
                await web.serve_async(
                    project,
                    host="127.0.0.1",
                    start_port=start,
                    max_attempts=n,
                )
            assert f"{start}..{start + n - 1}" in str(exc.value)

        asyncio.run(_go())
    finally:
        for s in blockers:
            s.close()


def test_serve_calls_through_to_enumeration(project: Config, monkeypatch):
    """The standalone `ap2 web` path (`web.serve`) routes the bind through
    `_bind_with_enumeration` too, so an operator with a stale standalone
    on 7820 still gets a working UI on 7821 instead of an OSError. We
    don't run `serve()` to completion (it blocks on serve_forever), so
    this is a focused white-box: assert `_build_server` is called with
    the standalone start port and the helper's enumeration kicks in."""
    import socket as _sock

    # Block port 7820 with a real socket so enumeration has something to
    # walk past. Use a kernel-assigned proxy port (`_free_port` then bind
    # at the helper level) is not possible here because `serve()`'s
    # default is the literal 7820. Skip when 7820 is unbindable for
    # unrelated reasons (an actual standalone running, e.g.).
    blocker = _sock.socket()
    blocker.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    try:
        try:
            blocker.bind(("127.0.0.1", web.DEFAULT_STANDALONE_WEB_PORT))
        except OSError:
            pytest.skip(
                f"standalone default port "
                f"{web.DEFAULT_STANDALONE_WEB_PORT} not bindable in this env"
            )
        blocker.listen(1)

        # Stub `serve_forever` so `serve()` returns instead of blocking.
        # We just want to confirm the bind path picked port+1.
        captured: dict = {}
        real_build = web._build_server

        def _spy_build(cfg, host, start_port, max_attempts=10):
            srv, bound = real_build(
                cfg, host, start_port, max_attempts=max_attempts,
            )
            captured["bound"] = bound

            # Make `serve_forever` a no-op so the test doesn't hang.
            def _noop_serve_forever():
                return None

            srv.serve_forever = _noop_serve_forever  # type: ignore[method-assign]
            return srv, bound

        monkeypatch.setattr(web, "_build_server", _spy_build)
        web.serve(project)  # uses defaults: host=127.0.0.1, port=7820

        assert captured["bound"] == web.DEFAULT_STANDALONE_WEB_PORT + 1, (
            f"expected enumeration to {web.DEFAULT_STANDALONE_WEB_PORT + 1}, "
            f"got {captured.get('bound')}"
        )
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# TB-158: surface bullet failures clearly in events logs (web).
#
# `/events` rows render verification_failed with a per-row pass/fail
# counter and an inline list of failing-bullet headlines so the operator
# can see WHICH bullet failed without expanding the raw json `<details>`.
# `/task-run/<run-id>` adds a top-of-page block when the run's terminal
# verdict is `verification_failed` (or `task_complete` with status set
# to `verification_failed`).


def _seed_vf_event(
    project: Config,
    *,
    task: str = "TB-VF",
    pass_n: int = 5,
    fail_bullets: list[tuple[str, str, str]] | None = None,
    unverified_n: int = 1,
) -> None:
    """Append a synthetic `verification_failed` event whose criteria list
    matches the briefing's expected shape (kind, status, bullet, notes)."""
    fails = fail_bullets or []
    criteria = (
        [
            {"kind": "shell", "status": "pass", "bullet": f"shell pass #{i}",
             "notes": ""}
            for i in range(pass_n)
        ]
        + [
            {"kind": k, "status": "fail", "bullet": b, "notes": n}
            for (k, b, n) in fails
        ]
        + [
            {"kind": "prose", "status": "unverified",
             "bullet": f"prose unv #{i}", "notes": "skip"}
            for i in range(unverified_n)
        ]
    )
    ev_mod.append(
        project.events_file, "verification_failed",
        task=task, kind="per_task", overall="fail", criteria=criteria,
    )


def test_events_page_renders_verification_failed_inline(project: Config):
    """`/events` rendering of a `verification_failed` event surfaces the
    pass/fail counter and the failing-bullet headlines inline. Passing
    bullets are NOT in the rendered HTML — only the counter carries
    them, mirroring the CLI's noise-control rule."""
    _seed_vf_event(
        project,
        task="TB-300",
        pass_n=5,
        fail_bullets=[
            ("prose", "Manual: kick a long-running task on stoch and "
                      "watch the mattermost reply",
             "no live evidence captured"),
            ("shell", "grep -qE 'NOT_THERE' ap2/foo.py",
             "ripgrep exited 1"),
        ],
        unverified_n=1,
    )
    h = web._render_events(project, typ="verification_failed", n=10)
    rows_block = h.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    # Strip the raw-json `<details>` body from each row before pinning
    # the inline summary — the raw payload still carries every bullet
    # (that's the point of the details fallback) but the briefing's
    # contract is that passing bullets do NOT appear in the *rendered*
    # row summary that shows by default.
    summary_cells = []
    for row in rows_block.split("<tr "):
        if "verification_failed" not in row:
            continue
        before_details = row.split("<details>", 1)[0]
        summary_cells.append(before_details)
    summary_html = "\n".join(summary_cells)

    # Counter naming all three buckets is on the row.
    assert "5/8 passed" in summary_html
    assert "2 failed" in summary_html
    assert "1 unverified" in summary_html
    # Failing-bullet headlines surface as an inline sub-list.
    assert "failed-bullets-inline" in summary_html
    assert "Manual: kick a long-running task on stoch" in summary_html
    assert "NOT_THERE" in summary_html
    # Passing-bullet text is NOT rendered (counter only) outside of the
    # raw-json fallback, which lives behind a `<details>` toggle.
    assert "shell pass #0" not in summary_html
    assert "shell pass #4" not in summary_html
    # Unverified-bullet text is NOT rendered either (also counter-only).
    assert "prose unv #0" not in summary_html
    # Row tinted as a failure-class event (red) — `verification_failed`
    # lives in `diagnose.FAILURE_EVENT_TYPES`. The status-aware tinting
    # is on `task_complete` rows; the structured `verification_failed`
    # event itself stays uniformly red so the operator can spot it in
    # the row stream.
    assert 'class="failure"' in rows_block


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


def test_summarize_verification_failed_shared_helper_is_grep_visible():
    """TB-158 verification gate: `summarize_verification_failed` is
    referenced by name in events.py, cli.py, AND web.py — the briefing's
    `grep -qE` bullet pins this. A refactor that drops the call from
    either surface would silently break the consistent rendering."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    for fname in ("events.py", "cli.py", "web.py"):
        text = (root / fname).read_text()
        assert "summarize_verification_failed" in text, fname


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
    in `ap2/web.py` — specifically near the `verification_failed`
    special-case branch in `_events_table`. The briefing's `grep -nE`
    bullet pins this."""
    from pathlib import Path as _P

    text = (_P(web.__file__).resolve().parent / "web.py").read_text()
    for typ in ("judge_call", "task_run_usage", "control_run_usage"):
        assert typ in text, typ


# --------- TB-162: pending operator-queue card on `/` ---------


import json as _json


def _seed_queue_entry(
    cfg: Config,
    *,
    uuid: str,
    op: str,
    args: dict,
    ts: str = "2026-05-04T17:15:30Z",
) -> None:
    """Append one operator-queue record to `.cc-autopilot/operator_queue.jsonl`.

    Mirrors the shape `tools.do_operator_queue_append` writes — uuid + op
    + args + ts is the contract `_render_pending_queue` reads.
    """
    queue_path = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"uuid": uuid, "op": op, "args": args, "ts": ts}
    with queue_path.open("a") as f:
        f.write(_json.dumps(rec) + "\n")


def _seed_queue_state_applied(cfg: Config, uuids: list[str]) -> None:
    """Mirror `tools._save_operator_queue_applied` — the state file the
    drain handler keeps in sync with the queue."""
    state_path = cfg.project_root / ".cc-autopilot" / "operator_queue_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_json.dumps({"applied": list(uuids)}, indent=2))


def test_pending_queue_card_renders_three_op_kinds(project: Config):
    """Three undrained ops (add_backlog with title, update with fields,
    approve) all appear in the rendered HTML on `/`, AND each carries
    its per-op-kind summary shape: `title="..."` for add_backlog,
    `fields=...` for update, no extra arg for approve."""
    _seed_queue_entry(
        project,
        uuid="aaaaaaaa-1111-2222-3333-444444444444",
        op="add_backlog",
        args={"task_id": "TB-200", "title": "Surface pending operator queue"},
        ts="2026-05-04T17:18:02Z",
    )
    _seed_queue_entry(
        project,
        uuid="bbbbbbbb-1111-2222-3333-444444444444",
        op="update",
        args={
            "task_id": "TB-152",
            "title": "rev",
            "fields": ["title", "description", "briefing"],
        },
        ts="2026-05-04T17:15:30Z",
    )
    _seed_queue_entry(
        project,
        uuid="cccccccc-1111-2222-3333-444444444444",
        op="approve",
        args={"task_id": "TB-152"},
        ts="2026-05-04T17:18:09Z",
    )
    page = web._render_home(project)
    # Card present.
    assert "pending-queue" in page
    # All three op kinds rendered.
    assert "[add_backlog]" in page
    assert "[update]" in page
    assert "[approve]" in page
    # All three task_ids rendered.
    assert "TB-200" in page
    assert "TB-152" in page
    # Per-op-kind summaries.
    assert 'title="Surface pending operator queue"' in page
    assert "fields=title,description,briefing" in page
    # `approve` carries no per-op extra (no fields=, no title=, no
    # force=) — the task_id pill alone is the load-bearing signal.
    li_approve = next(
        chunk for chunk in page.split("<li>") if "[approve]" in chunk
    )
    li_approve = li_approve.split("</li>", 1)[0]
    assert "title=" not in li_approve
    assert "fields=" not in li_approve
    assert "force=" not in li_approve


def test_pending_queue_card_omitted_when_queue_empty(project: Config):
    """Empty (or missing) queue file → card is omitted entirely from `/`,
    not just CSS-hidden. The `pending-queue` selector lives in the page
    `<style>` so we scope the assertion to the post-`</style>` body —
    that's where a rendered card would land if one were emitted."""
    # Case 1: file does not exist.
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="pending-queue"' not in body
    assert "operator op" not in body  # header text only fires when card renders
    # Case 2: file exists but is empty.
    queue_path = project.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("")
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="pending-queue"' not in body
    assert "operator op" not in body


def test_pending_queue_uuid_is_truncated(project: Config):
    """UUIDs render as a short prefix (≤16 chars), not the full 36-char
    canonical form — avoids horizontal overflow on narrow viewports."""
    long_uuid = "deadbeef-1234-5678-9abc-def012345678"
    _seed_queue_entry(
        project,
        uuid=long_uuid,
        op="approve",
        args={"task_id": "TB-99"},
    )
    page = web._render_home(project)
    # The header label "uuid=" is followed by an 8-char prefix; the full
    # 36-char form must NOT appear inside the entry's `<li>` body
    # (the raw json `<details>` carries it, which is fine).
    li = next(chunk for chunk in page.split("<li>") if "TB-99" in chunk)
    # Cut the body at the `<details>raw json</details>` boundary so the
    # raw JSON dump (which legitimately carries the full uuid) doesn't
    # falsely satisfy the assertion.
    body, _, _ = li.partition("<details>")
    assert "uuid=deadbeef" in body
    assert long_uuid not in body, (
        f"full uuid leaked into rendered body (should be ≤8 char prefix): "
        f"{body!r}"
    )


def test_pending_queue_filters_out_drained_entries(project: Config):
    """An entry whose uuid is in `operator_queue_state.json`'s applied-set
    is treated as drained-but-not-yet-compacted and omitted from the
    rendered card. Pins the brief window between drain (state file
    updated) and `_compact_operator_queue` (queue file rewritten)."""
    drained_uuid = "11111111-1111-1111-1111-111111111111"
    pending_uuid = "22222222-2222-2222-2222-222222222222"
    _seed_queue_entry(
        project,
        uuid=drained_uuid,
        op="approve",
        args={"task_id": "TB-DRAINED"},
    )
    _seed_queue_entry(
        project,
        uuid=pending_uuid,
        op="approve",
        args={"task_id": "TB-PENDING"},
    )
    _seed_queue_state_applied(project, [drained_uuid])
    page = web._render_home(project)
    assert "pending-queue" in page  # card still rendered (one pending)
    assert "TB-PENDING" in page
    # Drained entry must not appear in the rendered card. Limit the
    # check to the card's own slice so unrelated TB-DRAINED references
    # elsewhere on the page (none today, but defensive) couldn't
    # falsely satisfy the assertion.
    card = page.split('<div class="pending-queue">', 1)[1].split("</div>", 1)[0]
    assert "TB-DRAINED" not in card
    assert "11111111" not in card


def test_pending_queue_helper_is_grep_visible():
    """The briefing's `grep -nE "def _render_pending_queue"` and
    `grep -qE "pending-queue"` verification bullets pin both the helper
    name and the CSS class name to web.py source. A refactor that
    drops either would silently break the operator-facing card."""
    from pathlib import Path as _P

    text = (_P(web.__file__)).read_text()
    assert "def _render_pending_queue" in text
    assert "pending-queue" in text


# --------- TB-173: ideator open-questions card on `/` ---------
#
# `_render_open_questions(cfg)` reads the `## Open questions for operator`
# section from `.cc-autopilot/ideation_state.md` via
# `parse_open_questions`, renders one `<li>` per bullet, and is mounted
# above `_render_pending_queue` on `/`. Empty list → card omitted
# entirely (server-side, not CSS-hidden).


def _seed_ideation_state(cfg: Config, body: str) -> None:
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_open_questions_card_renders_when_present(project: Config):
    """Three bullets in the file → home page carries an `.open-questions`
    card with one `<li>` per bullet and a header that names the count."""
    _seed_ideation_state(
        project,
        "## Open questions for operator\n\n"
        "- Should goal.md declare a new focus?\n"
        "- Approve or reject TB-171 / TB-172 / TB-173.\n"
        "- Insights index still empty.\n",
    )
    page = web._render_home(project)
    # Card class present.
    assert 'class="open-questions"' in page
    # Header carries the count.
    assert "3 open questions" in page
    # Each bullet rendered as one `<li>`.
    assert "<li>Should goal.md declare a new focus?</li>" in page
    assert (
        "<li>Approve or reject TB-171 / TB-172 / TB-173.</li>"
    ) in page
    assert "<li>Insights index still empty.</li>" in page


def test_open_questions_card_omitted_when_empty(project: Config):
    """No file / no section / empty section → card omitted entirely from
    `/`, not just CSS-hidden. The `.open-questions` selector lives in
    the page `<style>` so we scope the assertion to the post-`</style>`
    body — that's where a rendered card would land."""
    # Case 1: file does not exist (`project` fixture doesn't seed one).
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="open-questions"' not in body
    assert "open questions" not in body.lower()

    # Case 2: file exists but no `## Open questions for operator` section.
    _seed_ideation_state(
        project,
        "# Ideation State\n\n## Mission alignment\n\n- nothing\n",
    )
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="open-questions"' not in body
    assert "open questions" not in body.lower()

    # Case 3: section header present but empty body.
    _seed_ideation_state(
        project,
        "## Open questions for operator\n\n## Proposals this cycle\n\n- TB-1\n",
    )
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="open-questions"' not in body


def test_open_questions_card_renders_above_pending_queue(project: Config):
    """When BOTH cards have content, the open-questions card renders
    ABOVE the pending-queue card on `/` so ideator-surfaced operator-
    judgement work gets visual priority over mechanical pending ops."""
    _seed_ideation_state(
        project,
        "## Open questions for operator\n\n"
        "- Should we declare verifier robustness as the next focus?\n",
    )
    _seed_queue_entry(
        project,
        uuid="aaaaaaaa-1111-2222-3333-444444444444",
        op="approve",
        args={"task_id": "TB-99"},
    )
    page = web._render_home(project)
    oq_idx = page.find('class="open-questions"')
    pq_idx = page.find('class="pending-queue"')
    assert oq_idx >= 0
    assert pq_idx >= 0
    assert oq_idx < pq_idx, (
        f"open-questions card should render above pending-queue card; "
        f"got open-questions at {oq_idx}, pending-queue at {pq_idx}"
    )


def test_open_questions_card_escapes_html(project: Config):
    """Bullet bodies are HTML-escaped before rendering — defends against
    an ideator (or some future adversarial input) writing a `<script>`
    tag into the section body."""
    _seed_ideation_state(
        project,
        "## Open questions for operator\n\n"
        "- Should we use `<script>` tags? & other HTML\n",
    )
    page = web._render_home(project)
    # Locate the card's `<li>` row — that's where bullet content lands.
    li_start = page.find("<li>", page.find('class="open-questions"'))
    li_end = page.find("</li>", li_start)
    li = page[li_start:li_end]
    # Raw `<script>` must not survive escaping; entities must be present.
    assert "<script>" not in li
    assert "&lt;script&gt;" in li
    assert "&amp;" in li


def test_open_questions_helper_is_grep_visible():
    """Mirrors `test_pending_queue_helper_is_grep_visible` — the briefing's
    `grep -rnE "parse_open_questions|open_questions" ap2/web.py` bullet
    pins the helper name + CSS class to web.py source so a refactor that
    drops either silently breaks the operator-facing card."""
    from pathlib import Path as _P

    text = (_P(web.__file__)).read_text()
    assert "def _render_open_questions" in text
    assert "open-questions" in text
    assert "parse_open_questions" in text
