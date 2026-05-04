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


def test_briefing_with_manual_verification_bullet_is_warning(cfg):
    """TB-138: a briefing whose `## Verification` contains a `- Manual: ...`
    bullet emits a warning-level Issue (not an error) so operators can fix
    it before dispatch without blocking the rest of `ap2 check`.
    """
    brief = cfg.tasks_dir / "tb-99-manual.md"
    brief.write_text(
        "# TB-99 — example\n\n"
        "## Goal\n\nstub\n\n"
        "## Verification\n\n"
        "- `uv run pytest -q` — full suite passes\n"
        "- Manual: kick a long-running task and observe handler reply\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    report = check.check_project(cfg)
    # Warnings don't fail.
    assert report.ok
    assert any(
        i.file == "tb-99-manual.md"
        and "Manual" in i.message
        and i.severity == "warning"
        for i in report.warnings
    ), [(i.severity, i.file, i.message) for i in report.issues]


def test_briefing_with_bracketed_manual_tag_is_warning(cfg):
    """`[manual]` prefix is also banned (case-insensitive) — same gating
    consequence as `Manual:`."""
    brief = cfg.tasks_dir / "tb-100-bracketed.md"
    brief.write_text(
        "# TB-100 — example\n\n"
        "## Verification\n\n"
        "- `uv run pytest -q`\n"
        "- [manual] operator runs deploy and confirms\n"
    )
    report = check.check_project(cfg)
    assert report.ok
    assert any(
        i.file == "tb-100-bracketed.md" and i.severity == "warning"
        for i in report.warnings
    )


def test_briefing_with_only_auto_verifiable_bullets_no_warning(cfg):
    """Backticked shell, test name, and judge-checkable prose all pass the
    lint cleanly. Pins the rule's negative case so the lint doesn't
    over-flag and become noise.
    """
    brief = cfg.tasks_dir / "tb-101-clean.md"
    brief.write_text(
        "# TB-101 — example\n\n"
        "## Verification\n\n"
        "- `uv run pytest -q` — regression gate\n"
        "- `grep -q foo bar.py` — symbol pinned\n"
        "- new test `test_foo_in_bar` covers the responsiveness claim\n"
        "- prose: `Daemon.main_loop` in `ap2/daemon.py` splits via "
        "`asyncio.gather`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    report = check.check_project(cfg)
    assert not any(
        i.file == "tb-101-clean.md" and "Manual" in i.message
        for i in report.issues
    ), [(i.severity, i.file, i.message) for i in report.issues]


def test_manual_bullet_outside_verification_section_not_flagged(cfg):
    """A `Manual:` bullet outside `## Verification` (e.g. in `## Goal` or
    `## Design` prose) is not the gating-criterion problem TB-138 targets;
    don't flag it. The lint scopes to the `## Verification` slice only.
    """
    brief = cfg.tasks_dir / "tb-102-design.md"
    # TB-154: structurally canonical briefing so the new
    # `_check_briefing_structure` lint stays quiet — that lint emits a
    # warning for any briefing missing one of the canonical sections,
    # which would otherwise drown out the test's actual claim (that the
    # `Manual:` lint scopes correctly).
    brief.write_text(
        "# TB-102 — example\n\n"
        "## Goal\n\nstub\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\n"
        "- Manual: this prose-bullet is fine in design notes\n\n"
        "## Verification\n\n"
        "- `uv run pytest -q`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    report = check.check_project(cfg)
    assert not any(
        i.file == "tb-102-design.md" for i in report.issues
    )


def test_briefing_template_carries_auto_verifiable_rule():
    """TB-138: the empty briefing template's `## Verification` preamble
    must carry the auto-verifiable rule explicitly, so editor-mode
    `ap2 add` users see it in the buffer they're filling in.
    """
    from ap2 import init as init_mod
    text = init_mod.BRIEFING_TEMPLATE
    lower = text.lower()
    assert "auto-verifiable" in lower or "auto verifiable" in lower
    # No-Manual-bullets rule is named.
    assert "Manual:" in text or "manual:" in lower
    # The escape hatch (move to `## Out of scope`) is named.
    assert "Out of scope" in text


# ---------------------------------------------------------------------------
# TB-154: on-disk briefing structure lint (warning-level companion to the
# queue-append-time hard gate in `ap2/tools.py`). The hard gate refuses
# new non-canonical briefings; this lint surfaces legacy entries the
# operator can opportunistically fix.

_TB154_CANONICAL_BRIEFING = (
    "# TB-X — example\n\n"
    "## Goal\n\nstub\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nstub\n\n"
    "## Verification\n\n- `uv run pytest -q`\n\n"
    "## Out of scope\n\n- nothing\n"
)


def test_tb154_check_briefing_structure_warns_on_missing_section(cfg):
    """An on-disk briefing missing a canonical section emits a
    warning-level Issue (not an error). Mirrors `_check_briefing_links`'s
    warning shape so the operator can fix opportunistically without
    `ap2 check` going red."""
    brief = cfg.tasks_dir / "tb-200-no-verification.md"
    brief.write_text(
        "# TB-200 — no verification\n\n"
        "## Goal\n\nstub\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nstub\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    report = check.check_project(cfg)
    # Warning, not error — non-fatal so legacy briefings don't block
    # other check usage.
    assert report.ok
    matching = [
        i for i in report.warnings
        if i.file == "tb-200-no-verification.md"
        and "non-canonical" in i.message.lower()
    ]
    assert matching, [(i.severity, i.file, i.message) for i in report.issues]
    assert "## Verification" in matching[0].message


def test_tb154_check_briefing_structure_silent_on_canonical(cfg):
    """A canonical on-disk briefing emits zero structure warnings — the
    lint stays quiet for the normal case."""
    brief = cfg.tasks_dir / "tb-201-canonical.md"
    brief.write_text(_TB154_CANONICAL_BRIEFING)
    report = check.check_project(cfg)
    assert not any(
        i.file == "tb-201-canonical.md" and "non-canonical" in i.message.lower()
        for i in report.issues
    ), [(i.severity, i.file, i.message) for i in report.issues]


def test_tb154_check_briefing_structure_silent_on_canonical_with_extras(cfg):
    """Extra `##`-level sections on disk are fine — extension is
    explicitly allowed (mirrors the queue-append validator's behavior)."""
    brief = cfg.tasks_dir / "tb-202-extras.md"
    brief.write_text(
        "# TB-202 — example\n\n"
        "## Goal\n\nstub\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nstub\n\n"
        "## Decision log\n\n- decided X\n\n"
        "## Verification\n\n- `uv run pytest -q`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    report = check.check_project(cfg)
    assert not any(
        i.file == "tb-202-extras.md" and "non-canonical" in i.message.lower()
        for i in report.issues
    )


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
