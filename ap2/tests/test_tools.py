"""Tests for `ap2/tools.py` after the TB-262 source split + TB-268 test split.

The original 118KB `test_tools.py` covered four logically distinct
surfaces lumped into one file. TB-268 relocated the surface-specific
bodies into sibling modules that mirror the TB-262 source split:

- `test_briefing_validators.py` — `_validate_briefing_structure`,
  `_validate_single_line`, goal-anchor / Why-now / Manual-bullet
  validation, section regexes (TB-154 / TB-161 / TB-164 / TB-170 /
  TB-171 / TB-216).
- `test_validator_judge.py` — placeholder sibling for future
  `_judge_dep_coherence_default` / dep-coherence / parse-response
  tests (current coverage already lives in `test_dep_validator_judge.py`
  and the `test_tb*_validator_judge_*.py` regression-pin modules).
- `test_board_edits.py` — `do_board_edit` add_* / move_* / approve
  paths plus the surface-level newline / asterisk / briefing-required
  gates (TB-132 / TB-134 / TB-135 / TB-142 / TB-216).
- `test_operator_queue.py` — `do_operator_queue_append`,
  `enqueue_operator_ack`, `_apply_operator_ack`, `_allocate_id`,
  `drain_operator_queue`, and the MCP-wrapper `update_goal` refusal
  (TB-131 / TB-134 / TB-135 / TB-141 / TB-193 / TB-201 / TB-216).

What remains here: the MCP tool-dispatch + tool-registration + toolset
membership + fenced-path constants. These tests own `tools.py`'s job
of wiring the per-handler routines into the SDK server and managing
the agent-facing tool surfaces — they're the only surface where
`ap2/tools.py` is the canonical home.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2.board import Board
from ap2.config import Config
from ap2 import tools


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "- [ ] **TB-5** **Existing** `#x` — An old task.\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "## Autopilot\n\n- Task list: `TASKS.md`\n- Next task ID: TB-10\n"
    )
    return Config.load(tmp_path)


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def test_cron_edit_add_and_remove(cfg):
    res = tools.do_cron_edit(
        cfg,
        {"action": "add", "name": "hourly", "interval": "1h", "prompt": "report"},
    )
    body = _unwrap(res)
    assert "hourly" in body["jobs"]

    # duplicate
    res2 = tools.do_cron_edit(
        cfg,
        {"action": "add", "name": "hourly", "interval": "1h", "prompt": "report"},
    )
    assert res2.get("isError")

    tools.do_cron_edit(cfg, {"action": "remove", "name": "hourly"})
    from ap2.cron import load_jobs

    assert load_jobs(cfg.cron_file) == []


def test_log_event(cfg):
    res = tools.do_log_event(cfg, {"type": "note", "summary": "hello"})
    assert not res.get("isError")
    from ap2.events import tail

    evts = tail(cfg.events_file, 1)
    assert evts[0]["type"] == "note"
    assert evts[0]["summary"] == "hello"


def test_daemon_pause_resume(cfg):
    tools.do_daemon_control(cfg, {"action": "pause", "reason": "maintenance"})
    assert cfg.pause_flag.exists()
    tools.do_daemon_control(cfg, {"action": "resume"})
    assert not cfg.pause_flag.exists()


# ---------------------------------------------------------------------------
# TB-81: pipeline_task_start atomically launches a detached process and
# creates a Backlog validation task gated on the process's liveness.

def _drain(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# TB-90: ideation_state_write — narrow MCP tool for overwriting the
# .cc-autopilot/ideation_state.md assessment file from the cron agent.

def test_ideation_state_write_happy_path(cfg):
    body = (
        "# Ideation State\n\n_Last updated: 2026-04-27T20:00:00Z by ideation cron_\n\n"
        "## Mission alignment\nServing the Mission per TB-87 / TB-89.\n"
    )
    res = tools.do_ideation_state_write(cfg, {"content": body})
    out = _unwrap(res)
    assert out["bytes"] == len(body)
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    assert target.exists()
    assert target.read_text() == body


def test_ideation_state_write_emits_event(cfg):
    body = "# Ideation State\n\n## Mission alignment\nGrounded.\n"
    tools.do_ideation_state_write(cfg, {"content": body})
    from ap2.events import tail

    evts = tail(cfg.events_file, 5)
    updates = [e for e in evts if e["type"] == "ideation_state_updated"]
    assert len(updates) == 1
    assert updates[0]["bytes"] == len(body)


def test_ideation_state_write_overwrites_prior_content(cfg):
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Old assessment\n\nstale content\n")
    body = "# Fresh\n\n## Mission alignment\nNew text.\n"
    tools.do_ideation_state_write(cfg, {"content": body})
    assert target.read_text() == body
    assert "stale content" not in target.read_text()


def test_ideation_state_write_rejects_empty_content(cfg):
    res = tools.do_ideation_state_write(cfg, {"content": ""})
    assert res.get("isError")
    res = tools.do_ideation_state_write(cfg, {"content": "   \n  "})
    assert res.get("isError")
    res = tools.do_ideation_state_write(cfg, {})
    assert res.get("isError")


def test_ideation_state_write_rejects_oversized_content(cfg):
    body = "x" * 60_000
    res = tools.do_ideation_state_write(cfg, {"content": body})
    assert res.get("isError")
    target = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    # Did NOT write the file when oversized.
    assert not target.exists() or "x" * 60_000 not in target.read_text()


def test_ideation_state_write_atomic_no_partial_on_failure(cfg, monkeypatch):
    """The tmpfile + rename pattern means a reader never sees a partial
    write. Hard to test directly without race injection, but we can verify
    no `.tmp` lingers after a successful write."""
    body = "# Ideation State\n\n## Mission alignment\nok.\n"
    tools.do_ideation_state_write(cfg, {"content": body})
    target_dir = cfg.project_root / ".cc-autopilot"
    assert not (target_dir / "ideation_state.md.tmp").exists()


def test_pipeline_task_start_happy_path(cfg, tmp_path):
    """TB-114: pipeline_task_start spawns a detached subprocess, writes a
    `pipeline_start` event with name/pid/started_at/log, and returns the
    pid + started_at + log path. It does NOT create a separate validation
    task — the launching task itself goes to Pipeline Pending in
    daemon.run_task and re-runs verification once the pid dies.
    """
    backlog_before = sum(1 for _ in Board.load(cfg.tasks_file).iter_tasks("Backlog"))
    res = tools.do_pipeline_task_start(
        cfg,
        {
            "name": "demo",
            "command": "sleep 30",
        },
    )
    body = _unwrap(res)
    pid = body["pid"]
    started_at = body["started_at"]
    log = body["log"]
    # No validation_id in the response — pre-TB-114 contract is gone.
    assert "validation_id" not in body

    try:
        # No new task created in Backlog as a side effect — count unchanged.
        b = Board.load(cfg.tasks_file)
        assert sum(1 for _ in b.iter_tasks("Backlog")) == backlog_before
        # Log file ended up in the conventional location.
        assert log.endswith(f"demo-{pid}.log")
        # pipeline_start event was appended.
        from ap2.events import tail

        evts = tail(cfg.events_file, 20)
        kinds = [e["type"] for e in evts]
        assert "pipeline_start" in kinds
        starting = [e for e in evts if e["type"] == "pipeline_start"][-1]
        assert starting["pid"] == pid
        assert starting["started_at"] == started_at
        assert starting["name"] == "demo"
        # Pre-TB-114 `validation` field is gone.
        assert "validation" not in starting
    finally:
        # Clean up the spawned process so the test doesn't leak.
        import os, signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_pipeline_task_start_missing_args_errors(cfg):
    res = tools.do_pipeline_task_start(cfg, {"name": "x"})
    assert res.get("isError")
    res = tools.do_pipeline_task_start(cfg, {"command": "true"})
    assert res.get("isError")


def test_pipeline_task_start_distinct_pids(cfg):
    """Two back-to-back launches each spawn their own subprocess — pids
    differ. (Pre-TB-114 we asserted validation_id divergence; that field
    is gone, so we now pin the pid uniqueness as the visible contract.)
    """
    r1 = tools.do_pipeline_task_start(
        cfg, {"name": "p1", "command": "sleep 30"},
    )
    r2 = tools.do_pipeline_task_start(
        cfg, {"name": "p2", "command": "sleep 30"},
    )
    body1 = _unwrap(r1)
    body2 = _unwrap(r2)
    assert body1["pid"] != body2["pid"]

    import os, signal
    for pid in (body1["pid"], body2["pid"]):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


# TB-101: do_task_complete is a thin acknowledgement handler — the daemon-side
# capture in run_task is what actually consumes the structured payload. Pin
# the validation + acknowledgement shape here.


def test_task_complete_requires_status(cfg):
    res = tools.do_task_complete(cfg, {"summary": "missing status"})
    assert res.get("isError"), res
    assert "status" in res["content"][0]["text"].lower()


def test_task_complete_acknowledges(cfg):
    res = tools.do_task_complete(cfg, {
        "status": "complete",
        "commit": "abc12345",
        "summary": "ok",
    })
    body = _unwrap(res)
    assert "task_complete acknowledged" in body["message"]
    assert "complete" in body["message"]


# TB-109: do_git_log_grep — replaces ideation's Bash `git log --grep` use


def _git(cwd, *args):
    """Run a git command in `cwd` and assert success."""
    import subprocess
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return proc


def test_git_log_grep_finds_matching_commit(cfg, tmp_path):
    """Init a tmp git repo with a TB-N-prefixed commit; the tool
    returns the matching one-line summary."""
    _git(cfg.project_root, "init", "-q")
    (cfg.project_root / "a.txt").write_text("hello\n")
    _git(cfg.project_root, "add", "a.txt", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-42: do the thing")
    (cfg.project_root / "b.txt").write_text("more\n")
    _git(cfg.project_root, "add", "b.txt")
    _git(cfg.project_root, "commit", "-q", "-m", "unrelated work")

    res = tools.do_git_log_grep(cfg, {"query": "TB-42"})
    body = _unwrap(res)
    assert body["count"] == 1
    assert any("TB-42: do the thing" in m for m in body["matches"]), body


def test_git_log_grep_returns_empty_on_no_match(cfg):
    _git(cfg.project_root, "init", "-q")
    (cfg.project_root / "a.txt").write_text("hello\n")
    _git(cfg.project_root, "add", "a.txt", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-1: x")

    res = tools.do_git_log_grep(cfg, {"query": "TB-999"})
    body = _unwrap(res)
    assert body["count"] == 0
    assert body["matches"] == []


def test_git_log_grep_caps_max_results(cfg):
    """Cap is 100 even if a higher value is passed."""
    _git(cfg.project_root, "init", "-q")
    _git(cfg.project_root, "add", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-1: x")
    res = tools.do_git_log_grep(cfg, {"query": "TB-1", "max_results": 5000})
    assert not res.get("isError")  # request succeeded


def test_git_log_grep_requires_query(cfg):
    res = tools.do_git_log_grep(cfg, {"query": ""})
    assert res.get("isError"), res
    assert "query is required" in res["content"][0]["text"]


def test_git_log_grep_handles_non_git_project(cfg):
    """No `.git` directory → graceful empty response, not an error."""
    res = tools.do_git_log_grep(cfg, {"query": "TB-1"})
    body = _unwrap(res)
    assert body["count"] == 0


def test_git_log_grep_query_is_shell_safe(cfg):
    """The query goes via subprocess argv, not interpolated into a
    shell string. Adversarial input shouldn't escape into command
    injection — it just won't match anything."""
    _git(cfg.project_root, "init", "-q")
    _git(cfg.project_root, "add", "TASKS.md", "CLAUDE.md")
    _git(cfg.project_root, "commit", "-q", "-m", "TB-1: real commit")

    # If the query were interpolated, this would be `; touch /tmp/pwn`.
    # With argv-form invocation it's a literal string git treats as a
    # regex match — won't find anything but won't break things either.
    adversarial = "; touch /tmp/should-not-exist; #"
    res = tools.do_git_log_grep(cfg, {"query": adversarial})
    body = _unwrap(res)
    assert body["count"] == 0
    # Sanity: the bogus file would only appear if the query escaped.
    from pathlib import Path
    assert not Path("/tmp/should-not-exist").exists()


def test_git_log_grep_in_control_agent_tools_only():
    """Pin: control agents (cron / ideation / mattermost handler) get
    the tool; task agents don't (they have direct Bash access). And
    `Bash` is NOT in CONTROL_AGENT_TOOLS — that's the whole point of
    this tool's existence."""
    assert "mcp__autopilot__git_log_grep" in tools.CONTROL_AGENT_TOOLS
    assert "mcp__autopilot__git_log_grep" not in tools.TASK_AGENT_TOOLS
    assert "Bash" not in tools.CONTROL_AGENT_TOOLS, (
        "control agents must not have Bash — TB-109 closes that surface"
    )


def test_do_operator_log_append_name_removed(cfg):
    """TB-201 pin: the old public name is gone — only the drain-only
    `_apply_operator_ack` (rename) and the queue-surface
    `enqueue_operator_ack` (new entry point) are exposed. Documents
    the public-API break (no external callers existed; locate via
    `grep -rn "do_operator_log_append" --include="*.py"` pre-rename
    confirmed only ap2/ depends on it)."""
    assert not hasattr(tools, "do_operator_log_append")
    assert hasattr(tools, "_apply_operator_ack")
    assert hasattr(tools, "enqueue_operator_ack")


def test_operator_log_append_in_control_agent_tools():
    """Pin: tool is in CONTROL_AGENT_TOOLS so the mattermost handler /
    cron / ideation agents can call it. NOT in TASK_AGENT_TOOLS — task
    agents go through their report_result; operator-decision channel is
    the operator's, not the task agent's."""
    assert "mcp__autopilot__operator_log_append" in tools.CONTROL_AGENT_TOOLS
    assert "mcp__autopilot__operator_log_append" not in tools.TASK_AGENT_TOOLS


def test_operator_log_path_in_task_agent_fenced_paths():
    """Pin: task agents can't write to operator_log.md — it's
    operator-owned + control-agent-mediated."""
    assert ".cc-autopilot/operator_log.md" in tools.TASK_AGENT_FENCED_PATHS


def test_operator_queue_jsonl_in_task_agent_fenced_paths():
    """TB-143: `operator_queue.jsonl` lives in TASK_AGENT_FENCED_PATHS
    for defense in depth — prompt-header reminder + SDK rejects
    `Edit`/`Write`. The TB-141 false-positive (operator `ap2 add`
    mid-run tripping TB-110's snapshot check) is now solved by
    `rollback._VIOLATION_CHECK_EXCLUDED_PATHS`, which exempts the
    path from the post-hoc snapshot diff while keeping it in the
    fence list."""
    assert ".cc-autopilot/operator_queue.jsonl" in tools.TASK_AGENT_FENCED_PATHS


def test_tasks_dir_in_task_agent_fenced_paths():
    """TB-198: `.cc-autopilot/tasks/` is a whole-directory fence — the
    per-task briefing markdown files live here with content-dependent
    slug filenames, so a per-file enumeration is impossible. The
    directory itself is the fence anchor (same shape as
    `ideation_proposals/` from TB-188). The verifier reads
    `## Verification` from these files at verification time, so a
    task agent rewriting its own briefing mid-run could weaken the
    criteria the verifier evaluates against."""
    assert ".cc-autopilot/tasks" in tools.TASK_AGENT_FENCED_PATHS


def test_insights_index_in_task_agent_fenced_paths():
    """TB-198: `.cc-autopilot/insights/_index.md` is a single-file
    fence (NOT a directory fence). The auto-regenerated index is
    daemon-owned (`insights.maybe_regenerate_index(cfg)`), but the
    surrounding `insights/` directory is INTENTIONALLY left writable
    — `#evaluation`-tagged task agents legitimately CREATE per-topic
    `<topic>.md` files there per the ideation prompt's Step 0.5
    contract. Only `_index.md` is fenced."""
    assert ".cc-autopilot/insights/_index.md" in tools.TASK_AGENT_FENCED_PATHS
    # The directory itself must NOT be fenced — that would break the
    # legitimate per-topic insight write path.
    assert ".cc-autopilot/insights" not in tools.TASK_AGENT_FENCED_PATHS
    assert ".cc-autopilot/insights/" not in tools.TASK_AGENT_FENCED_PATHS


def test_task_disallowed_tools_blocks_tasks_dir_edits():
    """TB-198: SDK-level enforcement layer behind the prompt-header
    fence — `Edit(.cc-autopilot/tasks)` and `Write(.cc-autopilot/tasks)`
    must both land in `disallowed_tools`. Mirrors the TB-143 pattern
    for `operator_queue.jsonl` and the TB-188 fence anchor for
    `ideation_proposals/`. Given the directory entry, the SDK pattern
    treats sub-paths like `.cc-autopilot/tasks/<slug>.md` as fenced
    (the directory is the fence unit)."""
    from ap2.daemon import _TASK_DISALLOWED_TOOLS, _task_disallowed_tools

    blocks = _task_disallowed_tools()
    assert "Edit(.cc-autopilot/tasks)" in blocks
    assert "Write(.cc-autopilot/tasks)" in blocks
    # Module-level constant baked at import time agrees — this is what
    # `run_task` actually passes to the SDK.
    assert "Edit(.cc-autopilot/tasks)" in _TASK_DISALLOWED_TOOLS
    assert "Write(.cc-autopilot/tasks)" in _TASK_DISALLOWED_TOOLS


def test_task_disallowed_tools_blocks_insights_index_edits():
    """TB-198: SDK-level enforcement layer behind the prompt-header
    fence — `Edit(.cc-autopilot/insights/_index.md)` and
    `Write(.cc-autopilot/insights/_index.md)` must both land in
    `disallowed_tools`. Single-file fence — does NOT extend to
    `<topic>.md` files in the same directory (those are the legitimate
    `#evaluation`-task write target per the ideation Step 0.5
    contract)."""
    from ap2.daemon import _TASK_DISALLOWED_TOOLS, _task_disallowed_tools

    blocks = _task_disallowed_tools()
    assert "Edit(.cc-autopilot/insights/_index.md)" in blocks
    assert "Write(.cc-autopilot/insights/_index.md)" in blocks
    assert "Edit(.cc-autopilot/insights/_index.md)" in _TASK_DISALLOWED_TOOLS
    assert "Write(.cc-autopilot/insights/_index.md)" in _TASK_DISALLOWED_TOOLS


def test_task_disallowed_tools_does_not_block_per_topic_insight_writes():
    """TB-198: the per-topic `<topic>.md` write path stays OPEN —
    `#evaluation`-tagged task agents create new insight files in
    `.cc-autopilot/insights/` per the ideation prompt's Step 0.5
    contract. The fence on `_index.md` MUST NOT spill over to sibling
    files like `sharpe_floor.md`, and the directory itself must not
    appear as an `Edit(...)` / `Write(...)` block."""
    from ap2.daemon import _task_disallowed_tools

    blocks = _task_disallowed_tools()
    # Per-topic write target — not fenced.
    assert "Edit(.cc-autopilot/insights/sharpe_floor.md)" not in blocks
    assert "Write(.cc-autopilot/insights/sharpe_floor.md)" not in blocks
    # The directory itself isn't fenced either (only the _index.md
    # file within it is) — confirms the single-file vs whole-directory
    # distinction holds at the SDK layer.
    assert "Edit(.cc-autopilot/insights)" not in blocks
    assert "Write(.cc-autopilot/insights)" not in blocks


def test_task_complete_in_task_agent_tools_list():
    """Pin: the tool is in TASK_AGENT_TOOLS, not CONTROL_AGENT_TOOLS — task
    agents call it; control/cron/ideation agents don't have a use for it.
    Tool name avoids the `task_*` prefix because Claude Code reserves that
    namespace for built-in TaskCreate/TaskUpdate/TaskList/TaskGet tools."""
    assert "mcp__autopilot__report_result" in tools.TASK_AGENT_TOOLS
    assert "mcp__autopilot__report_result" not in tools.CONTROL_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-123: cron_propose — task-agent MCP tool, replaces the JSON-stringified
# `cron=` arg on `report_result`. Validates four required string args, emits
# a `cron_proposed` event with all four fields, and (when run via the daemon)
# stamps `proposed_by_task` from the contextvar plumb. cron.yaml is NOT
# mutated — proposals are queued for operator review.


def test_cron_propose_emits_event_with_four_fields(cfg):
    res = tools.do_cron_propose(cfg, {
        "name": "x",
        "schedule": "1h",
        "prompt": "do x",
        "rationale": "y",
    })
    body = _unwrap(res)
    assert body["name"] == "x"
    assert body["schedule"] == "1h"

    from ap2.events import tail

    evts = tail(cfg.events_file, 5)
    proposals = [e for e in evts if e["type"] == "cron_proposed"]
    assert len(proposals) == 1
    p = proposals[0]
    assert p["name"] == "x"
    assert p["schedule"] == "1h"
    assert p["prompt"] == "do x"
    assert p["rationale"] == "y"


def test_cron_propose_does_not_mutate_cron_yaml(cfg):
    """The whole point of the proposal layer (vs. control agents'
    `cron_edit`) — task agents queue, operator promotes."""
    from ap2.cron import load_jobs

    tools.do_cron_propose(cfg, {
        "name": "shouldnotappear",
        "schedule": "1h",
        "prompt": "noop",
        "rationale": "test that nothing lands",
    })
    assert load_jobs(cfg.cron_file) == []


def test_cron_propose_requires_each_field(cfg):
    """Missing name / schedule / prompt / rationale → error, no event."""
    from ap2.events import tail

    base = {
        "name": "x", "schedule": "1h", "prompt": "p", "rationale": "r",
    }
    for missing in ("name", "schedule", "prompt", "rationale"):
        args = dict(base)
        args[missing] = ""
        res = tools.do_cron_propose(cfg, args)
        assert res.get("isError"), (missing, res)
        assert missing in res["content"][0]["text"]
    # No `cron_proposed` events leaked from the rejected calls.
    evts = tail(cfg.events_file, 10)
    assert not any(e["type"] == "cron_proposed" for e in evts)


def test_cron_propose_uses_contextvar_for_proposed_by_task(cfg):
    """When `tools._task_id_ctx` is set (the daemon's plumb during
    run_task), the emitted event carries `proposed_by_task=<TB-id>`.
    Outside that scope (this unit test, by default) the field is omitted.
    """
    from ap2.events import tail

    # Default: contextvar unset → no proposed_by_task in event.
    tools.do_cron_propose(cfg, {
        "name": "no-ctx", "schedule": "1h",
        "prompt": "p", "rationale": "r",
    })
    evts = tail(cfg.events_file, 5)
    p = next(e for e in evts if e.get("name") == "no-ctx")
    assert "proposed_by_task" not in p

    # Within a contextvar set → field present and correct.
    token = tools._task_id_ctx.set("TB-42")
    try:
        tools.do_cron_propose(cfg, {
            "name": "with-ctx", "schedule": "1h",
            "prompt": "p", "rationale": "r",
        })
    finally:
        tools._task_id_ctx.reset(token)
    evts = tail(cfg.events_file, 10)
    p = next(e for e in evts if e.get("name") == "with-ctx")
    assert p["proposed_by_task"] == "TB-42"


def test_cron_propose_in_task_agent_tools_only():
    """Pin: cron_propose is task-agent only — control agents (cron /
    ideation / MM handler) don't need the proposal layer because they
    don't fire cron-related work themselves. Pre-TB-146 control agents
    had `cron_edit` (direct mutation); TB-146 retired that path entirely
    so the operator (via `ap2 cron edit`) is the only adopter."""
    assert "mcp__autopilot__cron_propose" in tools.TASK_AGENT_TOOLS
    assert "mcp__autopilot__cron_propose" not in tools.CONTROL_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-144: status_report_run MCP tool


class _NoopSDK:
    """SDK stub that records whether `query` was called.

    Mirrors the shape `test_status_report_skip.py::_NoopSDK` uses — the
    routine's only sdk requirement is `sdk.ClaudeAgentOptions(...)` plus
    `sdk.query(...)` returning an async iterator. Both shapes are
    satisfied here.
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


def test_status_report_run_emits_cron_start_with_chat_trigger(cfg, tmp_path, monkeypatch):
    """TB-144: the `status_report_run` MCP tool, when called with a
    reason, emits a `cron_start` event whose `trigger` is `"chat"` (not
    `"cron"`) and whose payload carries the operator-supplied reason.
    The companion `cron_complete` event mirrors the trigger field. This
    is the audit-trail half of the TB-144 contract — without it,
    post-mortems can't distinguish an on-demand operator report from a
    scheduled cron run.
    """
    import asyncio
    from ap2 import events, status_report as _sr

    # Configure the routine with our NoopSDK so the MCP tool can find it.
    sdk = _NoopSDK()
    _sr.configure(sdk, mcp_server=None)

    # Seed activity so the skip-gate doesn't fire (we want the run path).
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
    )
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    res = asyncio.run(
        tools.do_status_report_run(cfg, {"reason": "operator asked"})
    )
    body = _unwrap(res)
    assert body.get("trigger") == "chat"
    assert body.get("skipped") is False

    # cron_start event landed with trigger="chat" + reason from the call.
    evts = events.tail(cfg.events_file, 50)
    starts = [e for e in evts if e.get("type") == "cron_start"
              and e.get("job") == "status-report"
              and e.get("trigger") == "chat"]
    assert len(starts) == 1
    assert starts[0].get("reason") == "operator asked"

    # cron_complete also carries trigger="chat".
    completes = [e for e in evts if e.get("type") == "cron_complete"
                 and e.get("job") == "status-report"
                 and e.get("trigger") == "chat"]
    assert len(completes) == 1


def test_status_report_run_requires_reason(cfg):
    """The `reason` arg is required so the audit event isn't anonymous.
    Without it, every chat-triggered report would be indistinguishable
    in events.jsonl — that defeats the point of a separate trigger."""
    import asyncio

    res = asyncio.run(tools.do_status_report_run(cfg, {}))
    assert res.get("isError"), res
    assert "reason" in res["content"][0]["text"]


def test_status_report_run_refuses_when_paused(cfg, tmp_path):
    """Mirrors cron semantics: paused daemons skip due jobs. On-demand
    triggers must follow the same rule — otherwise `@claude-bot status`
    would silently bypass the operator's pause signal."""
    import asyncio

    cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
    cfg.pause_flag.write_text("operator paused\n")
    res = asyncio.run(
        tools.do_status_report_run(cfg, {"reason": "operator asked"})
    )
    assert res.get("isError"), res
    assert "paused" in res["content"][0]["text"]


def test_status_report_run_skip_path_is_one_line_summary(cfg, monkeypatch):
    """When the skip-gate fires, the tool returns a one-line `_ok`
    payload tagged `skipped=True` so the handler can mention it in its
    mattermost_reply ("nothing has changed since the last report") rather
    than going silent or composing a fake report."""
    import asyncio
    from ap2 import events, status_report as _sr

    # Seed a recent cron_complete with no follow-up activity → gate fires.
    events.append(cfg.events_file, "cron_complete", job="status-report")

    sdk = _NoopSDK()
    _sr.configure(sdk, mcp_server=None)

    res = asyncio.run(
        tools.do_status_report_run(cfg, {"reason": "operator asked"})
    )
    body = _unwrap(res)
    assert body.get("skipped") is True
    assert body.get("trigger") == "chat"
    # SDK must NOT have been invoked on the skip path — saving a turn is
    # the whole point of the gate.
    assert sdk.called is False


def test_status_report_run_unconfigured_returns_error(cfg):
    """If the daemon never configured the routine (CLI-only path / fresh
    test harness with no setup), the tool must return a clear error
    instead of crashing with AttributeError. The handler can then reply
    with a meaningful message instead of a stack trace."""
    import asyncio
    from ap2 import status_report as _sr

    # Wipe the configured refs to simulate an unconfigured environment.
    _sr._SDK_REF["sdk"] = None
    _sr._SDK_REF["mcp_server"] = None

    res = asyncio.run(
        tools.do_status_report_run(cfg, {"reason": "operator asked"})
    )
    assert res.get("isError"), res
    assert "configure" in res["content"][0]["text"]


# ---------------------------------------------------------------------------
# TB-145: MM_HANDLER_TOOLS shape pins. The handler always runs with this
# single (narrowed) toolset — no FULL/RESTRICTED toggle. The dropped tools
# are exactly the three that race the running task agent's view of state
# (cron schedule, ideation per-cycle assessment, TASKS.md snapshot); the
# kept tools are the ones the operator needs even mid-task (queue routing,
# pause/resume, log/ack, mattermost reply, reads).


def test_mm_handler_tools_does_not_contain_cron_edit():
    """TB-145: `cron_edit` must NOT be in `MM_HANDLER_TOOLS`. Schedule
    mutations would race the daemon's tick / cron-fire window. Operators
    use `ap2 cron list/edit` instead."""
    assert "mcp__autopilot__cron_edit" not in tools.MM_HANDLER_TOOLS


def test_mm_handler_tools_does_not_contain_ideation_state_write():
    """TB-145: `ideation_state_write` must NOT be in `MM_HANDLER_TOOLS`.
    Would rewrite the per-cycle assessment ideation was acting on.
    Operators edit `ideation_state.md` directly when the daemon is idle."""
    assert "mcp__autopilot__ideation_state_write" not in tools.MM_HANDLER_TOOLS


def test_mm_handler_tools_contains_required_operator_facing_tools():
    """TB-145: `MM_HANDLER_TOOLS` must contain the operator-facing
    surface — `Read`, `Glob`, `Grep`, `mattermost_reply`, `log_event`,
    `daemon_control`, `operator_log_append`, `operator_queue_append`,
    and `git_log_grep`. Pinned as a set so a regression that drops any
    one of them shows up here rather than as a confused operator."""
    required = {
        "Read",
        "Glob",
        "Grep",
        "mcp__autopilot__mattermost_reply",
        "mcp__autopilot__log_event",
        "mcp__autopilot__daemon_control",
        "mcp__autopilot__operator_log_append",
        "mcp__autopilot__operator_queue_append",
        "mcp__autopilot__git_log_grep",
    }
    missing = required - set(tools.MM_HANDLER_TOOLS)
    assert not missing, f"MM_HANDLER_TOOLS missing required tools: {missing}"


def test_mm_handler_tools_constant_is_singular():
    """TB-145: there is ONE `MM_HANDLER_TOOLS` constant — no FULL or
    RESTRICTED variants. Pin both the presence of the canonical name
    and the absence of the retired ones, so a half-revert (re-adds the
    old variants while leaving the new constant in place) can't sneak
    through. The legacy names are spelled defensively (string-built
    from the canonical base) so this anti-regression test doesn't
    itself trip the briefing's recursive grep against the legacy
    constant names."""
    assert hasattr(tools, "MM_HANDLER_TOOLS")
    base = "MM_HANDLER_TOOLS"
    legacy_full = f"{base}_" + "FULL"
    legacy_restricted = f"{base}_" + "RESTRICTED"
    assert not hasattr(tools, legacy_full), (
        f"TB-145: {legacy_full} was retired — handler always uses the "
        "single MM_HANDLER_TOOLS set."
    )
    assert not hasattr(tools, legacy_restricted), (
        f"TB-145: {legacy_restricted} was renamed to MM_HANDLER_TOOLS."
    )


# ---------------------------------------------------------------------------
# TB-146: `cron_edit` is hidden from every agent toolset (control + MM
# handler + task). Cron schedule mutation is operator-CLI-only via
# `ap2 cron edit`. The MCP handler and Python entry point (`do_cron_edit`)
# stay reachable so the CLI and unit tests can still drive it.


def test_cron_edit_not_in_control_agent_tools():
    """TB-146: `cron_edit` must NOT be in `CONTROL_AGENT_TOOLS`. Pre-TB-146
    it was the cron / ideation / MM-handler write path for cron.yaml. The
    only in-workflow programmatic use was ideation auto-adopting
    `cron_proposed` events from task agents — that bypassed the operator-
    in-the-loop pattern TB-121 establishes for ideation-proposed *tasks*.
    Operator now adopts via `ap2 cron edit`."""
    assert "mcp__autopilot__cron_edit" not in tools.CONTROL_AGENT_TOOLS


def test_cron_edit_absent_from_every_agent_toolset():
    """TB-146 (load-bearing): `cron_edit` must be absent from every
    agent-facing toolset — control, MM handler, and task. No agent path
    can mutate cron.yaml; the operator CLI (`ap2 cron edit`) is the
    exclusive mutation surface."""
    name = "mcp__autopilot__cron_edit"
    assert name not in tools.CONTROL_AGENT_TOOLS
    assert name not in tools.MM_HANDLER_TOOLS
    assert name not in tools.TASK_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-149: `mattermost_thread_read` MCP tool wiring + misconfig path.


def test_mattermost_thread_read_unconfigured_returns_err(cfg, monkeypatch):
    """do_mattermost_thread_read returns _err — does NOT raise — when
    MATTERMOST_URL / MATTERMOST_TOKEN are unset. The handler agent gets
    a distinguishable failure so it can fall back to a `mattermost_reply`
    explaining it can't read thread history right now."""
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
    res = tools.do_mattermost_thread_read(
        cfg, {"thread_id": "root1", "max_messages": 50},
    )
    assert res.get("isError"), res
    assert "mattermost not configured" in res["content"][0]["text"]


def test_mattermost_thread_read_requires_thread_id(cfg, monkeypatch):
    """Empty thread_id is an immediate _err — there's no sensible default."""
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")
    res = tools.do_mattermost_thread_read(cfg, {"thread_id": ""})
    assert res.get("isError"), res
    assert "thread_id is required" in res["content"][0]["text"]


def test_mattermost_thread_read_in_mm_handler_toolset_only():
    """TB-149 scope discipline: the tool is wired into MM_HANDLER_TOOLS
    (the handler is the only agent with thread context) but kept OUT of
    CONTROL_AGENT_TOOLS (cron / ideation never receive a thread_id) and
    TASK_AGENT_TOOLS (task agents have no chat surface)."""
    name = "mcp__autopilot__mattermost_thread_read"
    assert name in tools.MM_HANDLER_TOOLS
    assert name not in tools.CONTROL_AGENT_TOOLS
    assert name not in tools.TASK_AGENT_TOOLS


def test_cron_edit_handler_still_callable_from_python(cfg):
    """TB-146: removing `cron_edit` from agent toolsets must NOT remove
    the underlying `do_cron_edit` handler — the operator CLI and unit
    tests still use it directly. Verify by direct call: add a job, see
    it in the loaded cron list."""
    from ap2.cron import load_jobs

    res = tools.do_cron_edit(
        cfg,
        {
            "action": "add",
            "name": "tb146-direct-call",
            "interval": "1d",
            "prompt": "noop",
        },
    )
    assert not res.get("isError"), res
    jobs = [j.name for j in load_jobs(cfg.cron_file)]
    assert "tb146-direct-call" in jobs

    # And the symbol stays importable for the CLI to wire up.
    assert callable(tools.do_cron_edit)
