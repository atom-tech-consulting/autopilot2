"""TB-177: janitor — deterministic detector for stranded git state.

Covers the four contract surfaces the briefing's `## Verification` block pins:

  1. The three subkinds fire on the right shapes; daemon-managed paths
     (.cc-autopilot/events.jsonl etc.) are excluded.
  2. The healthy / no-findings case emits NO events and NO operator_log line
     — janitor must be silent on a clean working tree.
  3. The cron-job dispatch routes through `janitor.run_janitor` (NOT through
     `_run_control_agent`), and `cron_start` / `cron_complete` events bookend
     the run with `job="janitor"`.
  4. `ap2 status` (CLI) renders a `janitor:` line when at least one recent
     `janitor_finding` event exists in events.jsonl.

Tests use real `git` subprocesses (the janitor's whole point is parsing
`git status --porcelain`); the temp project is `git init`'d in each test
fixture. The `clock` fixture and FakeSDK shape are reused from the e2e
harness pattern (see `ap2/tests/e2e/conftest.py`).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, janitor
from ap2.config import Config
from ap2.cron import CronJob
from ap2.daemon import run_cron


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run a git command, fail loudly if it errors. Returns stdout."""
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {r.returncode}\n{r.stderr}"
        )
    return r.stdout


def _project(tmp_path: Path) -> Config:
    """Build a minimal git-initialized project with the ap2 directory layout.

    Mirrors the `e2e_project` fixture's shape but inline so the unit tests
    don't need the e2e fixture's fake-SDK plumbing. The repo is real (so
    `git status --porcelain` works); a single seed commit lets us stage /
    modify against a real HEAD.
    """
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-1\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "test")
    # Seed the nested `.cc-autopilot/.gitignore` mirroring what `ap2 init`
    # writes so daemon-managed paths are ignored at the source. Without
    # this, the `.cc-autopilot/` directory itself would surface as `??`
    # (untracked) on a fresh test project, polluting the no-finding case.
    nested_gitignore = tmp_path / ".cc-autopilot" / ".gitignore"
    nested_gitignore.write_text(
        "events.jsonl\n"
        "cron_state.json\n"
        "mm_state.json\n"
        "daemon.pid\n"
        "paused\n"
        "auto_diagnose_state.json\n"
        "operator_queue.jsonl\n"
        "operator_queue_state.json\n"
        "pipelines/\n"
        "debug/\n"
        "*.lock\n"
    )
    _git(tmp_path, "add", "TASKS.md", "CLAUDE.md", str(nested_gitignore))
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return cfg


def _set_mtime(path: Path, age_s: float) -> None:
    """Backdate `path`'s mtime by `age_s` seconds from now."""
    t = time.time() - age_s
    os.utime(path, (t, t))


# ---------------------------------------------------------------------------
# Subkind: each detector fires on its expected shape.
# ---------------------------------------------------------------------------


def test_run_janitor_emits_three_findings_for_three_strandedness_shapes(
    tmp_path: Path,
):
    """All three subkinds fire on a project with (a) a staged-but-uncommitted
    file, (b) a tracked file modified ≥ MIN_MODIFIED_AGE_S ago,
    (c) an untracked file outside .gitignore. PLUS:
    (d) `.cc-autopilot/events.jsonl` modified within the last second — this
    must NOT trigger a finding (excluded path).

    Asserts:
      - exactly three `janitor_finding` events in events.jsonl after the call
        (one per subkind from a-c)
      - the events.jsonl modification (d) does NOT trigger a finding
      - operator_log.md gains exactly one summary line containing "janitor:"
        and one of "stranded" / "3"
    """
    cfg = _project(tmp_path)

    # (a) staged-but-uncommitted: create + add a new file but don't commit.
    (tmp_path / "staged_new.md").write_text("staged content\n")
    _git(tmp_path, "add", "staged_new.md")

    # (b) tracked file modified > MIN_MODIFIED_AGE_S ago, not staged.
    tracked = tmp_path / "TASKS.md"
    tracked.write_text(tracked.read_text() + "\n# touched\n")
    _set_mtime(tracked, age_s=janitor.MIN_MODIFIED_AGE_S + 60)

    # (c) untracked, not in .gitignore.
    (tmp_path / "scratch.txt").write_text("operator scratch\n")

    # (d) excluded path: events.jsonl. The append() the janitor itself does
    # later in the run will also keep this file fresh — but it's excluded
    # from the modified-not-staged check anyway, so no finding.
    events_file = cfg.events_file
    events_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.write_text('{"ts": "2026-05-05T00:00:00Z", "type": "noop"}\n')
    _set_mtime(events_file, age_s=0)  # within the last second

    report = janitor.run_janitor(cfg)

    # Report shape: 3 findings, one per subkind.
    assert len(report.findings) == 3
    subkinds = {f.subkind for f in report.findings}
    assert subkinds == {
        "staged_uncommitted",
        "modified_not_staged",
        "untracked_non_ignored",
    }

    # Per-subkind path correctness.
    by_kind = {f.subkind: f for f in report.findings}
    assert by_kind["staged_uncommitted"].paths == ["staged_new.md"]
    assert by_kind["modified_not_staged"].paths == ["TASKS.md"]
    assert by_kind["untracked_non_ignored"].paths == ["scratch.txt"]

    # The events.jsonl modification (d) is excluded from working-tree checks.
    all_paths = {p for f in report.findings for p in f.paths}
    assert ".cc-autopilot/events.jsonl" not in all_paths

    # Events: exactly three janitor_finding events (one per subkind).
    evts = events.tail(cfg.events_file, 50)
    findings = [e for e in evts if e.get("type") == "janitor_finding"]
    assert len(findings) == 3
    assert {e["subkind"] for e in findings} == subkinds
    # Each event carries the kind tag and a hint string.
    for e in findings:
        assert e.get("kind") == "git_stranded_state"
        assert isinstance(e.get("hint"), str) and e["hint"]
        assert isinstance(e.get("paths"), list) and e["paths"]

    # operator_log.md gains exactly one summary line for the run.
    log_path = tmp_path / ".cc-autopilot" / "operator_log.md"
    assert log_path.exists()
    janitor_lines = [
        ln for ln in log_path.read_text().splitlines()
        if "janitor:" in ln
    ]
    assert len(janitor_lines) == 1
    summary = janitor_lines[0]
    assert "stranded" in summary
    assert "3" in summary  # 3 findings


def test_excluded_paths_under_cc_autopilot_do_not_fire_modified_finding(
    tmp_path: Path,
):
    """Daemon-managed paths under `.cc-autopilot/` (events.jsonl,
    cron_state.json, etc.) churn between commits as a matter of normal
    operation. The janitor must NOT surface them — operators would drown
    in noise on every healthy project.

    Belt-and-braces: we backdate the file beyond MIN_MODIFIED_AGE_S so
    age guard alone wouldn't suppress it; only the exclusion list does.
    """
    cfg = _project(tmp_path)

    # Track a churn file in the index (so it shows up as ` M` rather than
    # `??` after we modify it). Use cron_state.json — explicit on the
    # exclusion list and not in `.gitignore` (so once tracked, modifications
    # would be visible to git status).
    state_file = tmp_path / ".cc-autopilot" / "cron_state.json"
    state_file.write_text("{}")
    _git(tmp_path, "add", "-f", str(state_file))
    _git(tmp_path, "commit", "-q", "-m", "track state file")
    # Modify it well past the age threshold so an unsuppressed check
    # would fire.
    state_file.write_text('{"status-report": 12345}')
    _set_mtime(state_file, age_s=janitor.MIN_MODIFIED_AGE_S + 60)

    report = janitor.run_janitor(cfg)
    assert report.findings == [], (
        "daemon-managed path should be excluded from janitor findings; "
        f"got {report.findings}"
    )


def test_modified_age_guard_skips_recently_modified_files(tmp_path: Path):
    """Files modified more recently than MIN_MODIFIED_AGE_S are plausibly
    in-flight task agent edits — the janitor must NOT fire on them. Only
    stale modifications (≥ threshold) surface."""
    cfg = _project(tmp_path)
    tracked = tmp_path / "TASKS.md"
    tracked.write_text(tracked.read_text() + "\n# fresh edit\n")
    # Default mtime is "now" — well within the age guard.
    report = janitor.run_janitor(cfg)
    assert report.findings == []


# ---------------------------------------------------------------------------
# Healthy / no-findings case: no events, no log line.
# ---------------------------------------------------------------------------


def test_clean_working_tree_emits_no_events_and_no_log_line(tmp_path: Path):
    """Healthy projects must stay quiet. A clean working tree → empty
    report, NO `janitor_finding` events, NO operator_log.md line appended.

    Pre-existing log content is preserved unchanged (defense in depth — a
    silent run that nuked the log would be catastrophic)."""
    cfg = _project(tmp_path)

    log_path = tmp_path / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("- 2026-05-01T00:00:00Z — pre-existing audit line\n")
    # Commit the pre-existing log so it's not surfaced as untracked
    # (in real projects the daemon's commit cycle keeps operator_log.md
    # in git after every state-changing tick).
    _git(tmp_path, "add", "-f", str(log_path))
    _git(tmp_path, "commit", "-q", "-m", "seed operator log")
    pre_log = log_path.read_text()

    report = janitor.run_janitor(cfg)
    assert report.findings == []

    # No janitor_finding events appended.
    if cfg.events_file.exists():
        evts = events.tail(cfg.events_file, 50)
        assert not [e for e in evts if e.get("type") == "janitor_finding"]

    # Operator log unchanged.
    assert log_path.read_text() == pre_log


# ---------------------------------------------------------------------------
# Cron-job dispatch: janitor routes through `run_janitor`, NOT the LLM agent.
# ---------------------------------------------------------------------------


class _NoopSDK:
    """SDK stub that records whether `query` was called.

    The janitor cron path must NOT reach the SDK at all (deterministic
    Python, no LLM). If `called` flips True, the dispatch wired through
    `_run_control_agent` instead of `janitor.run_janitor`.
    """

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


def test_cron_dispatch_routes_janitor_through_run_janitor_not_sdk(
    tmp_path: Path,
):
    """Adding a `janitor` job to cron and invoking the daemon's `run_cron`
    must:
      - call `janitor.run_janitor` (NOT `_run_control_agent`)
      - NOT invoke the SDK
      - emit `cron_start` and `cron_complete` events with `job="janitor"`
      - emit a `janitor_finding` event when stranded state is present
    """
    cfg = _project(tmp_path)
    # Seed a stranded file so the run produces a finding.
    (tmp_path / "scratch.txt").write_text("untracked scratch\n")

    sdk = _NoopSDK()
    job = CronJob(
        name="janitor", interval_s=300, prompt="ignored", max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    # The SDK must NOT have been touched — the janitor path is pure Python.
    assert sdk.called is False, (
        "janitor cron must not invoke the SDK; got SDK.query call"
    )

    evts = events.tail(cfg.events_file, 50)
    kinds = [(e.get("type"), e.get("job") or e.get("subkind") or "")
             for e in evts]
    # cron_start AND cron_complete bookend the run, both tagged job=janitor.
    assert ("cron_start", "janitor") in kinds
    assert ("cron_complete", "janitor") in kinds
    # The actual finding event also lands in the tail.
    assert any(
        e.get("type") == "janitor_finding"
        and e.get("subkind") == "untracked_non_ignored"
        for e in evts
    )

    # cron_state was advanced so the daemon doesn't re-fire every tick.
    state = json.loads(cfg.cron_state_file.read_text())
    assert "janitor" in state and state["janitor"] > 0


# ---------------------------------------------------------------------------
# CLI rendering: `ap2 status` surfaces the janitor count.
# ---------------------------------------------------------------------------


def test_cmd_status_renders_janitor_line_when_recent_finding_present(
    tmp_path: Path, capsys,
):
    """When at least one recent `janitor_finding` event is in events.jsonl,
    `ap2 status` text output contains a `janitor:` line with the finding
    count and a hint to inspect via `ap2 logs`. JSON output carries
    `janitor_findings: N`."""
    from ap2.cli import cmd_status
    from ap2.init import init_project

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    events.append(
        cfg.events_file, "janitor_finding",
        kind="git_stranded_state",
        subkind="staged_uncommitted",
        paths=["foo.md"],
        age_s=0,
        hint="commit or unstage",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "janitor:" in out
    assert "1 stranded-state finding" in out
    assert "ap2 logs" in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["janitor_findings"] == 1


def test_cmd_status_omits_janitor_line_when_no_recent_findings(
    tmp_path: Path, capsys,
):
    """A healthy project (no recent janitor_finding events) → no
    `janitor:` line in text output. JSON still carries
    `janitor_findings: 0` for machine-parseability."""
    from ap2.cli import cmd_status
    from ap2.init import init_project

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "janitor:" not in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["janitor_findings"] == 0
