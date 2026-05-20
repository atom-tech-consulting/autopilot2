"""TB-267: events / tasks / pipelines route group tests — mirror of `ap2/web_events.py`.

Relocated from `ap2/tests/test_web.py` by the TB-267 split. Each test body
is byte-identical to its pre-TB-267 original; only the module home and the
shared `project` fixture's location (now `ap2/tests/conftest.py`) changed.

Covers pages owned by `ap2/web_events.py`:
  - `/events` — `_render_events` (TB-148 / TB-157 / TB-158).
  - `/tasks` — `_render_tasks` (TB-121 pending-review filter).
  - `/task/<TB-N>` — `_render_task` + per-task `runs` section.
  - `/pipelines` — `_render_pipelines`.
  - `/commits` — `_render_commits`.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from ap2 import web, events as ev_mod
from ap2.board import Board
from ap2.config import Config
from ap2.tests.conftest import _seed_run, _seed_vf_event


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


# --------- TB-187: mixed-blocker pending-review surfacing ---------
#
# A task with `@blocked:review,TB-X` is pending operator review (the
# operator's approval is meaningful) AND structurally gated on TB-X.
# The surfacing predicate must classify it as pending review; the
# dispatch predicate (`_is_dispatchable`) is unchanged and still
# requires TB-X to land in Complete before auto-promotion.


def test_is_pending_review_handles_mixed_blockers(tmp_path: Path):
    """TB-187 regression: `_is_pending_review` returns True for any task
    with `review` AMONG its blockers — pure-review (unchanged), mixed
    (the bug fix), pure-non-review (unchanged), no blockers (unchanged).
    """
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-180** **review only** `@blocked:review` — pure review\n"
        "- [ ] **TB-181** **mixed** `@blocked:review,TB-99` — review + TB-99\n"
        "- [ ] **TB-182** **dep only** `@blocked:TB-99` — only TB-99\n"
        "- [ ] **TB-183** **no blockers** — clean\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    board = Board.load(cfg.tasks_file)

    by_id = {t.id: t for t in board.iter_tasks("Backlog")}
    assert web._is_pending_review(by_id["TB-180"]) is True
    # The fix: mixed-blocker tasks now surface (was False pre-fix).
    assert web._is_pending_review(by_id["TB-181"]) is True
    assert web._is_pending_review(by_id["TB-182"]) is False
    assert web._is_pending_review(by_id["TB-183"]) is False


def test_tasks_page_filter_includes_mixed_blocker_pending_review(
    tmp_path: Path,
):
    """`?filter=pending-review` must list a `@blocked:review,TB-X` task —
    the operator's approval is the load-bearing missing signal, and pre-
    TB-187 this task was invisible on the filter view."""
    # IDs are well above any TB-N referenced in the rendered HTML
    # itself (CSS comments cite real TB-Ns up to mid-100s), so a naive
    # `in html` substring check stays unambiguous.
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-9001** **proposal** `@blocked:review` — needs approval\n"
        "- [ ] **TB-9002** **mixed** `@blocked:review,TB-9099` — needs approval & TB-9099\n"
        "- [ ] **TB-9003** **dep only** `@blocked:TB-9099` — gated on TB-9099 only\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    html = web._render_tasks(cfg, filter_kind="pending-review")
    assert "TB-9001" in html
    assert "TB-9002" in html  # the fix — was missing pre-TB-187
    assert "TB-9003" not in html


def test_mixed_blocker_pending_review_does_not_auto_promote(tmp_path: Path):
    """TB-187 dispatch independence: surfacing a mixed-blocker task as
    pending review does NOT change auto-promotion semantics. A task with
    `@blocked:review,TB-X` (TB-X still in Backlog) MUST NOT be picked up
    by `next_dispatchable`. The fix is surfacing-only.

    TB-9099 is the prereq task — it has no blockers and could be
    dispatched on its own merits, so the assertion is targeted at
    TB-9070's `_is_dispatchable` flag, not the global sweep."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-9070** **mixed** `@blocked:review,TB-9099` — needs both gates cleared\n"
        "- [ ] **TB-9099** **prereq** — not yet complete\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    board = Board.load(cfg.tasks_file)

    # Surfacing classifies TB-9070 as pending review.
    by_id = {t.id: t for t in board.iter_tasks("Backlog")}
    assert web._is_pending_review(by_id["TB-9070"]) is True

    # Dispatch refuses to promote TB-9070 — `review` is unsatisfiable
    # while the codespan carries it (operator strips it via
    # `ap2 approve`), AND TB-9099 has not landed in Complete. Either
    # alone would block. (TB-9099 is itself dispatchable on its own
    # merits — no blockers — so we check TB-9070 specifically rather
    # than the sweep's first hit.)
    completed = board.completed_ids()
    assert board._is_dispatchable(by_id["TB-9070"], completed) is False


# --------- /events page rendering ---------


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
    field at 120 chars (`short`), the former shows the entire value.
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


# --------- TB-93 thaw: pipelines / commits ---------


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


def test_commits_in_non_git_dir(project: Config):
    html = web._render_commits(project)
    assert "not a git repo" in html


# --------- TB-129: events-table task_start linking + per-task runs section ---------


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


# ---------------------------------------------------------------------------
# TB-158: surface bullet failures clearly in events logs (web /events row).


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
