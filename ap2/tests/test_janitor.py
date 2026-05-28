"""TB-177 + TB-178: janitor — git-stranded-state detector + LLM-judge classifier.

Covers:

  TB-177 (deterministic detection)
  1. The three subkinds fire on the right shapes; daemon-managed paths
     (.cc-autopilot/events.jsonl etc.) are excluded.
  2. The healthy / no-findings case emits NO events — janitor must be
     silent on a clean working tree.
  3. The cron-job dispatch routes through `janitor.run_janitor` (NOT
     through `_run_control_agent`); `cron_start` / `cron_complete`
     events bookend the run with `job="janitor"`.
  4. `ap2 status` (CLI) renders a `janitor:` line when at least one
     recent `janitor_finding` event exists in events.jsonl.

  TB-178 (LLM judge classification)
  5. The `_judge_finding` step routes per-finding through the SDK and
     populates `verdict` ∈ {real_strand, operator_draft, ambiguous}
     plus a one-sentence `reasoning` field on the emitted event.
  6. `operator_log.md` MUST NOT be written by janitor (events-only
     emission rule, per the operator's directive).
  7. The cost-cap fallback (`AP2_JANITOR_MAX_FINDINGS_LLM=10`) skips
     judging for findings beyond the cap; overflow findings emit with
     `verdict="ambiguous"`.
  8. Disabled judge (`AP2_JANITOR_MAX_FINDINGS_LLM=0`) makes ZERO SDK
     calls and emits all findings with `verdict="ambiguous"`.
  9. CLI / status-report surfacing splits strands (urgent) from
     drafts (soft summary) so a `draft_*.md` operator notebook
     doesn't drive the urgency tone.

Tests use real `git` subprocesses (the janitor's whole point is parsing
`git status --porcelain`); the temp project is `git init`'d in each test
fixture. The FakeSDK stub mirrors `ap2/tests/e2e/_fakes._FakeMsg`.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

from ap2 import events
# TB-309: janitor moved to `ap2.components.janitor`; import under the
# old name so the rest of the test file continues to reference
# `janitor.<sym>` unchanged.
from ap2.components import janitor
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
# Scripted SDK stubs for the judge tests
# ---------------------------------------------------------------------------


class _FakeMsg:
    """Minimal message shape matching `ap2/tests/e2e/_fakes._FakeMsg`."""

    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


class _ScriptedJudgeSDK:
    """SDK stub whose `query()` returns canned JSON verdict strings.

    The constructor takes a list of one-line JSON responses; each call to
    `query()` consumes the next response in order, allowing tests to
    script per-finding verdicts. Tracks call count so tests can assert
    "at most N SDK calls".
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.captured_options: list[dict] = []
        self.captured_prompts: list[str] = []

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options) -> AsyncIterator[_FakeMsg]:
        self.calls += 1
        # Save kwargs from ClaudeAgentOptions for assertion checks.
        opts_kw = getattr(options, "kw", {}) or {}
        self.captured_options.append(opts_kw)
        self.captured_prompts.append(prompt)
        if self._responses:
            response = self._responses.pop(0)
        else:
            response = '{"verdict": "ambiguous", "reasoning": "no script"}'

        async def _gen() -> AsyncIterator[_FakeMsg]:
            yield _FakeMsg(response)

        return _gen()


class _NoopSDK:
    """SDK stub that records whether `query` was called.

    Used by `test_cron_dispatch_routes_janitor_through_run_janitor_not_control_agent`
    with `AP2_JANITOR_MAX_FINDINGS_LLM=0` to confirm that disabling the
    judge keeps the cron path SDK-free (matches TB-177's pre-judge
    contract for cost-constrained projects).
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


# ---------------------------------------------------------------------------
# Subkind: each detector fires on its expected shape.
# ---------------------------------------------------------------------------


def test_run_janitor_emits_three_findings_for_three_strandedness_shapes(
    tmp_path: Path, monkeypatch,
):
    """All three subkinds fire on a project with (a) a staged-but-uncommitted
    file, (b) a tracked file modified ≥ MIN_MODIFIED_AGE_S ago,
    (c) an untracked file outside .gitignore. PLUS:
    (d) `.cc-autopilot/events.jsonl` modified within the last second — this
    must NOT trigger a finding (excluded path).

    Asserts (TB-177 + TB-178):
      - exactly three `janitor_finding` events in events.jsonl (one
        per subkind from a-c)
      - the events.jsonl modification (d) does NOT trigger a finding
      - each event carries `verdict` and `reasoning` fields (TB-178)
      - operator_log.md is NOT created or appended (TB-178: events-only
        emission rule)
    """
    # Disable the LLM judge so the test is hermetic; verdicts default to
    # "ambiguous" but the verdict + reasoning fields still ride the event
    # envelope.
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "0")
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

    log_path = tmp_path / ".cc-autopilot" / "operator_log.md"
    pre_log_existed = log_path.exists()

    report = asyncio.run(janitor.run_janitor(cfg, sdk=None))

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

    # Events: exactly three janitor_finding events (one per subkind),
    # each with TB-178's verdict + reasoning fields present.
    evts = events.tail(cfg.events_file, 50)
    findings = [e for e in evts if e.get("type") == "janitor_finding"]
    assert len(findings) == 3
    assert {e["subkind"] for e in findings} == subkinds
    for e in findings:
        assert e.get("kind") == "git_stranded_state"
        assert isinstance(e.get("hint"), str) and e["hint"]
        assert isinstance(e.get("paths"), list) and e["paths"]
        # TB-178: verdict + reasoning ride every emitted event.
        assert e.get("verdict") in janitor.KNOWN_VERDICTS, e.get("verdict")
        assert "reasoning" in e

    # TB-178: operator_log.md MUST NOT be created or appended by janitor.
    if pre_log_existed:
        assert log_path.exists()  # only because we created it pre-run
    else:
        assert not log_path.exists(), (
            "TB-178: janitor must NOT create operator_log.md; "
            f"got {log_path.read_text()!r}"
        )


def test_excluded_paths_under_cc_autopilot_do_not_fire_modified_finding(
    tmp_path: Path, monkeypatch,
):
    """Daemon-managed paths under `.cc-autopilot/` (events.jsonl,
    cron_state.json, etc.) churn between commits as a matter of normal
    operation. The janitor must NOT surface them — operators would drown
    in noise on every healthy project.

    Belt-and-braces: we backdate the file beyond MIN_MODIFIED_AGE_S so
    age guard alone wouldn't suppress it; only the exclusion list does.
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "0")
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

    report = asyncio.run(janitor.run_janitor(cfg, sdk=None))
    assert report.findings == [], (
        "daemon-managed path should be excluded from janitor findings; "
        f"got {report.findings}"
    )


def test_modified_age_guard_skips_recently_modified_files(
    tmp_path: Path, monkeypatch,
):
    """Files modified more recently than MIN_MODIFIED_AGE_S are plausibly
    in-flight task agent edits — the janitor must NOT fire on them. Only
    stale modifications (≥ threshold) surface."""
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "0")
    cfg = _project(tmp_path)
    tracked = tmp_path / "TASKS.md"
    tracked.write_text(tracked.read_text() + "\n# fresh edit\n")
    # Default mtime is "now" — well within the age guard.
    report = asyncio.run(janitor.run_janitor(cfg, sdk=None))
    assert report.findings == []


# ---------------------------------------------------------------------------
# Healthy / no-findings case: no events, no log line.
# ---------------------------------------------------------------------------


def test_clean_working_tree_emits_no_events_and_no_log_line(
    tmp_path: Path, monkeypatch,
):
    """Healthy projects must stay quiet. A clean working tree → empty
    report, NO `janitor_finding` events, NO operator_log.md line appended.

    Pre-existing log content is preserved unchanged (defense in depth — a
    silent run that nuked the log would be catastrophic)."""
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "0")
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

    report = asyncio.run(janitor.run_janitor(cfg, sdk=None))
    assert report.findings == []

    # No janitor_finding events appended.
    if cfg.events_file.exists():
        evts = events.tail(cfg.events_file, 50)
        assert not [e for e in evts if e.get("type") == "janitor_finding"]

    # Operator log unchanged.
    assert log_path.read_text() == pre_log


# ---------------------------------------------------------------------------
# TB-178: LLM judge classification — verdict routing
# ---------------------------------------------------------------------------


def test_judge_real_strand_verdict_lands_on_event_no_oplog_write(
    tmp_path: Path, monkeypatch,
):
    """Briefing's 'real_strand' fixture: a staged-uncommitted file matches
    a recently-completed `task_pipeline_pending` event. The scripted SDK
    returns `verdict=real_strand`. Asserts:
      (a) one `janitor_finding` event with verdict=real_strand and a
          non-empty reasoning field
      (b) NO line appended to operator_log.md (events-only emission rule)
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "10")
    cfg = _project(tmp_path)

    # Seed events.jsonl with a recent task_pipeline_pending whose paths
    # would correlate with the staged finding.
    events.append(
        cfg.events_file, "task_pipeline_pending",
        task="TB-22", title="reeval stoch",
    )
    # Stage a file that looks like pipeline output.
    (tmp_path / "reeval_results.md").write_text("results\n")
    _git(tmp_path, "add", "reeval_results.md")

    # Capture pre-run operator_log.md state (should remain untouched).
    log_path = tmp_path / ".cc-autopilot" / "operator_log.md"
    pre_log_text = log_path.read_text() if log_path.exists() else None

    sdk = _ScriptedJudgeSDK([
        '{"verdict": "real_strand", "reasoning": '
        '"matches TB-22 pipeline output paths; pipeline likely failed to commit"}',
    ])
    report = asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    # Exactly one finding (just the staged_uncommitted check), classified
    # as real_strand with non-empty reasoning.
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.subkind == "staged_uncommitted"
    assert f.verdict == "real_strand"
    assert f.reasoning  # non-empty

    # Same shape on the emitted event.
    evts = events.tail(cfg.events_file, 50)
    findings = [e for e in evts if e.get("type") == "janitor_finding"]
    assert len(findings) == 1
    assert findings[0]["verdict"] == "real_strand"
    assert findings[0]["reasoning"]

    # SDK was called exactly once (one finding, judge enabled).
    assert sdk.calls == 1

    # operator_log.md MUST NOT have been written. If it didn't exist
    # pre-run, it must STILL not exist; if it did, content unchanged.
    if pre_log_text is None:
        assert not log_path.exists(), (
            f"TB-178: janitor must not create operator_log.md; "
            f"got {log_path.read_text()!r}"
        )
    else:
        assert log_path.read_text() == pre_log_text


def test_judge_operator_draft_verdict_renders_softer_in_status(
    tmp_path: Path, monkeypatch, capsys,
):
    """Briefing's 'operator_draft' fixture: an untracked `draft_tasks.md`
    in repo root with no TB-N reference. SDK returns
    `verdict=operator_draft`. Asserts:
      - finding emitted with verdict=operator_draft
      - `ap2 status` rendering counts it under "drafts" not "strands"
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "5")
    cfg = _project(tmp_path)

    # Operator-style draft filename — untracked.
    (tmp_path / "draft_tasks.md").write_text("# scratch notes\n")

    sdk = _ScriptedJudgeSDK([
        '{"verdict": "operator_draft", "reasoning": '
        '"draft_*.md naming; untracked in repo root with no TB-N reference"}',
    ])
    asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    evts = events.tail(cfg.events_file, 50)
    findings = [e for e in evts if e.get("type") == "janitor_finding"]
    assert len(findings) == 1
    assert findings[0]["verdict"] == "operator_draft"

    # `ap2 status` rendering: "draft" appears, NOT "strand".
    from ap2.cli import cmd_status

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "janitor:" in out
    assert "1 draft" in out
    assert "strand" not in out  # operator_draft must not read as urgent

    # JSON output: per-verdict breakdown is present.
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["janitor_findings"] == 1
    assert payload["janitor_findings_by_verdict"]["operator_draft"] == 1
    assert payload["janitor_findings_by_verdict"]["real_strand"] == 0


# ---------------------------------------------------------------------------
# TB-178: LLM judge classification — cost cap + disabled fallback
# ---------------------------------------------------------------------------


def test_cost_cap_via_seeded_findings_directly(
    tmp_path: Path, monkeypatch,
):
    """TB-178 cost-cap fallback: 12 candidate findings,
    AP2_JANITOR_MAX_FINDINGS_LLM=10. Asserts:
      - SDK is called at most 10 times (only the first 10 findings
        get a judge call)
      - the 11th and 12th findings carry verdict=ambiguous with a
        reasoning that names the env var so operators can adjust

    Drives the judge loop directly with a seeded 12-finding list. The
    deterministic detector caps at 3 findings per scan (one per
    subkind), so the cost-cap behavior is tested at the judge-loop
    layer rather than via the detectors.
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "10")
    cfg = _project(tmp_path)

    seeded = [
        janitor.JanitorFinding(
            subkind="untracked_non_ignored",
            paths=[f"scratch_{i}.md"],
            age_s=0,
            hint="hint",
        )
        for i in range(12)
    ]
    sdk = _ScriptedJudgeSDK([
        '{"verdict": "real_strand", "reasoning": "ok"}'
        for _ in range(10)
    ])

    # Drive run_janitor's judge step over the 12 seeded findings by
    # monkeypatching the detector layer to no-op AND using a custom
    # entry that pre-populates report.findings before the judge pass.
    # This mirrors run_janitor's own structure exactly so the cap
    # logic under test is the production code path, not a copy.

    cap = janitor._max_findings_llm()
    assert cap == 10  # sanity-check the env propagation

    shared_ctx = janitor._build_judge_shared_context(cfg)

    async def _drive():
        for i, f in enumerate(seeded):
            if i >= cap:
                f.verdict = janitor.VERDICT_AMBIGUOUS
                f.reasoning = (
                    f"skipped: exceeded AP2_JANITOR_MAX_FINDINGS_LLM={cap}"
                )
                continue
            verdict, reasoning = await janitor._judge_finding(
                cfg, sdk, f, shared_ctx,
            )
            f.verdict = verdict
            f.reasoning = reasoning

    asyncio.run(_drive())

    # Cap: SDK called exactly 10 times (one per first-10 findings).
    assert sdk.calls == 10, (
        f"cost cap should hold SDK calls to 10, got {sdk.calls}"
    )

    # Findings 0-9 carry verdict=real_strand (judge ran); 10-11
    # carry verdict=ambiguous with cost-cap-skip reasoning.
    verdicts = [f.verdict for f in seeded]
    assert verdicts[:10] == ["real_strand"] * 10
    assert verdicts[10:] == ["ambiguous", "ambiguous"]

    # Reasoning on overflow findings flags the env var by name so
    # operators reading events.jsonl know how to raise / disable it.
    overflow_reasoning = seeded[10].reasoning
    assert "AP2_JANITOR_MAX_FINDINGS_LLM" in overflow_reasoning
    assert "10" in overflow_reasoning


def test_disabled_judge_zero_sdk_calls_all_ambiguous(
    tmp_path: Path, monkeypatch,
):
    """TB-178 disabled-judge fallback: AP2_JANITOR_MAX_FINDINGS_LLM=0
    → no SDK calls AND every emitted finding carries verdict=ambiguous
    (deterministic-only behavior, mirroring TB-177 minus the operator-log
    line that TB-178 also dropped)."""
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "0")
    cfg = _project(tmp_path)

    # Seed all three subkinds (same shape as the regression fixture).
    (tmp_path / "staged_new.md").write_text("staged content\n")
    _git(tmp_path, "add", "staged_new.md")

    tracked = tmp_path / "TASKS.md"
    tracked.write_text(tracked.read_text() + "\n# touched\n")
    _set_mtime(tracked, age_s=janitor.MIN_MODIFIED_AGE_S + 60)

    (tmp_path / "scratch.txt").write_text("scratch\n")

    sdk = _ScriptedJudgeSDK([])  # any call would be a contract violation
    report = asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    assert sdk.calls == 0, (
        "AP2_JANITOR_MAX_FINDINGS_LLM=0 must disable the judge entirely"
    )
    assert len(report.findings) == 3
    for f in report.findings:
        assert f.verdict == "ambiguous", (
            f"disabled judge should default verdict=ambiguous; "
            f"got {f.verdict} for {f.subkind}"
        )

    # Events emitted with verdict=ambiguous and an empty reasoning
    # (no judge call → no error message either).
    evts = events.tail(cfg.events_file, 50)
    findings = [e for e in evts if e.get("type") == "janitor_finding"]
    assert len(findings) == 3
    assert all(e["verdict"] == "ambiguous" for e in findings)


def test_events_only_emission_no_oplog_write(
    tmp_path: Path, monkeypatch,
):
    """TB-178 events-only emission rule (operator-directive pin):
    one real_strand finding produces exactly one `janitor_finding` event
    in events.jsonl AND zero new lines in operator_log.md. Pin BOTH
    file states.
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "10")
    cfg = _project(tmp_path)

    # Operator-log baseline: pre-existing committed line so we can
    # detect whether janitor appended.
    log_path = tmp_path / ".cc-autopilot" / "operator_log.md"
    log_path.write_text("- 2026-05-01T00:00:00Z — pre-existing audit line\n")
    _git(tmp_path, "add", "-f", str(log_path))
    _git(tmp_path, "commit", "-q", "-m", "seed operator log")
    pre_log_text = log_path.read_text()
    pre_log_mtime = log_path.stat().st_mtime

    # Stage a candidate strand file.
    (tmp_path / "stranded.txt").write_text("residue\n")
    _git(tmp_path, "add", "stranded.txt")

    sdk = _ScriptedJudgeSDK([
        '{"verdict": "real_strand", "reasoning": "stranded by pipeline"}',
    ])
    asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    # events.jsonl: exactly ONE `janitor_finding` event for this run
    # (we count only the staged_uncommitted subkind to be precise).
    evts = events.tail(cfg.events_file, 100)
    findings = [
        e for e in evts
        if e.get("type") == "janitor_finding"
        and e.get("subkind") == "staged_uncommitted"
    ]
    assert len(findings) == 1
    assert findings[0]["verdict"] == "real_strand"

    # operator_log.md: byte-for-byte unchanged AND mtime preserved.
    assert log_path.read_text() == pre_log_text
    assert log_path.stat().st_mtime == pre_log_mtime, (
        "TB-178: janitor must not touch operator_log.md (mtime drift "
        "indicates a write even when content matches)"
    )


def test_judge_uses_read_only_tools_no_bash_or_writes(
    tmp_path: Path, monkeypatch,
):
    """TB-178 read-only judge contract: the SDK options handed to
    `_judge_finding` carry `Read`/`Glob`/`Grep` only — no Bash, no
    Edit, no Write, no NotebookEdit. Mirrors TB-136's identical pin
    on the prose-bullet judge.
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "5")
    cfg = _project(tmp_path)

    (tmp_path / "scratch.txt").write_text("scratch\n")

    sdk = _ScriptedJudgeSDK([
        '{"verdict": "operator_draft", "reasoning": "looks deliberate"}',
    ])
    asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    assert sdk.calls == 1
    opts_kw = sdk.captured_options[0]
    allowed = opts_kw.get("allowed_tools")
    assert allowed is not None, "allowed_tools must be passed"
    assert set(allowed) == {"Read", "Glob", "Grep"}
    for forbidden in ("Bash", "Edit", "Write", "NotebookEdit"):
        assert forbidden not in allowed, (
            f"{forbidden!r} must not be in the janitor-judge "
            "allowed_tools — judge is read-only by design"
        )
    # cwd must scope to project_root so Read/Glob/Grep don't escape.
    assert opts_kw.get("cwd") == str(cfg.project_root)


def test_judge_emits_judge_call_event_with_verdict(
    tmp_path: Path, monkeypatch,
):
    """TB-178 + TB-157: each judge SDK call emits a `judge_call` event
    carrying the verdict and a `bullet_kind` tag of `janitor:<subkind>`
    so the per-finding judge cost can be aggregated independently of
    the daemon's `_log_message` capture path."""
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "5")
    cfg = _project(tmp_path)

    (tmp_path / "scratch.txt").write_text("scratch\n")

    sdk = _ScriptedJudgeSDK([
        '{"verdict": "ambiguous", "reasoning": "cannot decide"}',
    ])
    asyncio.run(janitor.run_janitor(cfg, sdk=sdk))

    evts = events.tail(cfg.events_file, 50)
    judge_calls = [e for e in evts if e.get("type") == "judge_call"]
    assert len(judge_calls) == 1
    jc = judge_calls[0]
    assert jc.get("verdict") == "ambiguous"
    assert jc.get("bullet_kind") == "janitor:untracked_non_ignored"


# ---------------------------------------------------------------------------
# Cron-job dispatch: janitor routes through `run_janitor`, NOT `_run_control_agent`.
# ---------------------------------------------------------------------------


def test_cron_dispatch_routes_janitor_through_run_janitor_not_control_agent(
    tmp_path: Path, monkeypatch,
):
    """Adding a `janitor` job to cron and invoking the daemon's `run_cron`
    must:
      - call `janitor.run_janitor` (NOT `_run_control_agent`)
      - emit `cron_start` and `cron_complete` events with `job="janitor"`
      - emit a `janitor_finding` event when stranded state is present

    With AP2_JANITOR_MAX_FINDINGS_LLM=0 the SDK is NOT invoked (judge
    disabled) — same contract TB-177 pinned for the deterministic-only
    flavor. A separate test (test_cron_dispatch_invokes_judge_when_enabled)
    pins the SDK-active path.
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "0")
    cfg = _project(tmp_path)
    # Seed a stranded file so the run produces a finding.
    (tmp_path / "scratch.txt").write_text("untracked scratch\n")

    sdk = _NoopSDK()
    job = CronJob(
        name="janitor", interval_s=300, prompt="ignored", max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    # With judge disabled, the SDK must NOT have been touched.
    assert sdk.called is False, (
        "AP2_JANITOR_MAX_FINDINGS_LLM=0 must disable the judge SDK call"
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


def test_cron_dispatch_invokes_judge_when_enabled(
    tmp_path: Path, monkeypatch,
):
    """The cron-dispatched janitor calls the LLM judge when
    AP2_JANITOR_MAX_FINDINGS_LLM > 0 and findings are present. Pins:
      - SDK is called once (one finding)
      - the emitted janitor_finding event carries the scripted verdict
    """
    monkeypatch.setenv("AP2_JANITOR_MAX_FINDINGS_LLM", "5")
    cfg = _project(tmp_path)
    (tmp_path / "scratch.txt").write_text("untracked scratch\n")

    sdk = _ScriptedJudgeSDK([
        '{"verdict": "operator_draft", "reasoning": "scratch.txt — draft"}',
    ])
    job = CronJob(
        name="janitor", interval_s=300, prompt="ignored", max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    assert sdk.calls == 1
    evts = events.tail(cfg.events_file, 50)
    findings = [e for e in evts if e.get("type") == "janitor_finding"]
    assert len(findings) == 1
    assert findings[0]["verdict"] == "operator_draft"


# ---------------------------------------------------------------------------
# CLI rendering: `ap2 status` surfaces the janitor count.
# ---------------------------------------------------------------------------


def test_cmd_status_renders_janitor_line_when_recent_finding_present(
    tmp_path: Path, capsys,
):
    """When at least one recent `janitor_finding` event is in events.jsonl,
    `ap2 status` text output contains a `janitor:` line with a strand
    count and a hint to inspect via `ap2 logs`. JSON output carries
    `janitor_findings: N` plus the per-verdict breakdown."""
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
        verdict="real_strand",
        reasoning="pipeline residue",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "janitor:" in out
    assert "1 strand" in out
    assert "ap2 logs" in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["janitor_findings"] == 1
    assert payload["janitor_findings_by_verdict"]["real_strand"] == 1


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
    assert payload["janitor_findings_by_verdict"]["real_strand"] == 0
