"""Tests for `ap2 check` (TB-108): on-disk state-file integrity check.

Covers: TASKS.md shape (sections present, in order, no malformed lines),
briefing-link resolution, cron.yaml schema, JSON state-file parseability,
insights front-matter completeness, optional-file warnings.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ap2 import check
from ap2.config import Config
from ap2.init import init_project


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg_ = Config.load(tmp_path)
    cfg_.ensure_dirs()
    return cfg_


def test_clean_init_project_has_no_errors(cfg):
    """A freshly `ap2 init`-d project should pass with zero errors. Pin
    so any new check we add doesn't accidentally fail the bootstrapped
    case."""
    report = check.check_project(cfg)
    assert report.ok, [(i.severity, i.file, i.message) for i in report.issues]
    # goal.md template IS created by init_project — no warning expected.
    file_warnings = {i.file for i in report.warnings}
    assert "goal.md" not in file_warnings


def test_missing_tasks_md_is_error(cfg):
    cfg.tasks_file.unlink()
    report = check.check_project(cfg)
    assert not report.ok
    msgs = [(i.file, i.message) for i in report.errors]
    assert ("TASKS.md", "missing") in msgs


def test_section_out_of_order_is_error(cfg):
    """Section header order matters — Board._parse + dispatch precedence
    walk Active→Ready→Backlog. Reordering silently changes routing."""
    cfg.tasks_file.write_text(
        "# Tasks\n\n"
        "## Ready\n\n"
        "## Active\n\n"
        "## Backlog\n\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    report = check.check_project(cfg)
    assert any(
        "section order" in i.message for i in report.errors
    ), report.issues


def test_missing_section_is_error(cfg):
    cfg.tasks_file.write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "## Complete\n"
        # Frozen missing
    )
    report = check.check_project(cfg)
    assert any(
        "section order" in i.message and "Frozen" not in i.message.split("got ")[1]
        for i in report.errors
    ), report.issues


def test_malformed_task_line_is_error(cfg):
    """Whitespace-prefixed prose in Backlog (the TB-92 stoch case) —
    Board._parse flags it; check surfaces it."""
    cfg.tasks_file.write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "  this is orphan prose, not a task\n"
        "\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    report = check.check_project(cfg)
    assert any(
        "malformed line in Backlog" in i.message for i in report.errors
    ), report.issues


def test_stale_briefing_link_is_warning(cfg):
    cfg.tasks_file.write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "- [ ] **TB-1** **t** — desc [→ brief](.cc-autopilot/tasks/missing.md)\n\n"
        "## Backlog\n\n## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    report = check.check_project(cfg)
    assert report.ok  # warnings don't fail
    assert any(
        "briefing link points to missing file" in i.message
        for i in report.warnings
    ), report.issues


def test_resolved_briefing_link_no_issue(cfg):
    brief = cfg.tasks_dir / "tb1.md"
    brief.write_text("# brief\n")
    cfg.tasks_file.write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        f"- [ ] **TB-1** **t** — desc [→ brief](.cc-autopilot/tasks/tb1.md)\n\n"
        "## Backlog\n\n## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    report = check.check_project(cfg)
    assert not any(
        "briefing link" in i.message for i in report.issues
    )


def test_corrupt_json_state_is_error(cfg):
    cfg.cron_state_file.write_text("{this is not, json}")
    report = check.check_project(cfg)
    msgs = [(i.file, i.message) for i in report.errors]
    assert any(f == cfg.cron_state_file.name and "corrupt JSON" in m for f, m in msgs), msgs


def test_cron_yaml_missing_prompt_is_error(cfg):
    cfg.cron_file.write_text(
        "jobs:\n"
        "  - name: status-report\n"
        "    interval: 1h\n"
        "    prompt: \"\"\n"
    )
    report = check.check_project(cfg)
    assert any(
        "empty prompt" in i.message for i in report.errors
    ), report.issues


def test_cron_yaml_unparseable_is_error(cfg):
    cfg.cron_file.write_text(": : not valid yaml :\n")
    report = check.check_project(cfg)
    assert any(
        "parse failed" in i.message and i.file == "cron.yaml"
        for i in report.errors
    ), report.issues


def test_insight_missing_front_matter_is_warning(cfg):
    insights_dir = cfg.project_root / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "naked.md").write_text("# Just markdown body, no YAML\n\nbody.\n")
    report = check.check_project(cfg)
    assert any(
        i.file == "naked.md" and "front matter" in i.message
        for i in report.warnings
    ), report.issues
    # Still passes (warnings don't fail).
    assert report.ok


def test_insight_missing_front_matter_key_is_warning(cfg):
    insights_dir = cfg.project_root / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "partial.md").write_text(
        "---\n"
        "tldr: short summary\n"
        "updated: 2026-04-29T00:00:00Z\n"
        # missing updated_by + cites
        "---\n\nbody.\n"
    )
    report = check.check_project(cfg)
    keys_flagged = {
        i.message.split("missing key: ", 1)[1]
        for i in report.warnings
        if i.file == "partial.md" and "missing key" in i.message
    }
    assert "'updated_by'" in keys_flagged
    assert "'cites'" in keys_flagged


def test_index_md_not_treated_as_insight(cfg):
    """`_index.md` is auto-generated; it doesn't have YAML front matter
    and shouldn't be flagged."""
    insights_dir = cfg.project_root / ".cc-autopilot" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "_index.md").write_text("# Insights index\n\n(none yet)\n")
    report = check.check_project(cfg)
    assert not any(i.file == "_index.md" for i in report.issues)


def test_render_text_clean_path(cfg):
    text = check.render_text(check.check_project(cfg))
    assert "ap2 check: clean" in text


def test_render_text_with_errors(cfg):
    cfg.tasks_file.unlink()
    text = check.render_text(check.check_project(cfg))
    assert "1 error" in text
    assert "TASKS.md" in text


def test_render_json_shape(cfg):
    cfg.tasks_file.unlink()
    import json
    out = json.loads(check.render_json(check.check_project(cfg)))
    assert out["ok"] is False
    assert any("missing" == i["message"] for i in out["errors"])


def test_cmd_check_exit_code(cfg, capsys):
    from argparse import Namespace
    from ap2.cli import cmd_check

    rc_clean = cmd_check(cfg, Namespace(json=False))
    capsys.readouterr()
    assert rc_clean == 0

    cfg.tasks_file.unlink()
    rc_dirty = cmd_check(cfg, Namespace(json=False))
    assert rc_dirty == 1
    out = capsys.readouterr().out
    assert "TASKS.md" in out
