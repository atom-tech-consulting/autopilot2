"""Real-SDK *real-work* task smoke, parametrized over BOTH adapter backends
(TB-377 / goal.md axis 7).

Every other task-agent smoke uses a trivial "don't do anything, just call the
tool" prompt with `commit=""` / `files_changed=""`
(`test_report_result_real_sdk.py`), so NO smoke validates an agent actually
*doing the job of a task agent* — editing a file, running a command, and
producing a REAL commit SHA — for either backend. For codex the write path
(`Sandbox.workspace_write`) was entirely unexercised: the dispatch smoke
(`test_codex_real_sdk.py`) even pins a read-only sandbox. "Codex can do a real
task" was unproven; we had only proven "codex can call a tool".

This smoke closes that gap. In a fresh temporary git repo it asks the agent to
make a small concrete change (create a file), commit it, read back the commit
SHA, and call `report_result` with the ACTUAL commit SHA and a non-empty
`files_changed`. It then asserts, for BOTH the claude and codex backends, that:

  1. the file change exists in the temp repo,
  2. a new commit was created (HEAD advanced past the seed commit), and
  3. the round-tripped `report_result` args carry the ACTUAL (non-empty) commit
     SHA — a prefix of the repo's real HEAD — plus a non-empty `files_changed`.

(`AgentResult.commit` itself stays empty: commit / report_result extraction
lives in the daemon's `run_task` and is out of scope here — the briefing's
"`AgentResult.commit` / report_result args" alternative resolves to the
report_result-args path, which is the surface a task agent actually populates.)

Dispatch flows through the SAME seam production uses — `select_adapter("task",
cfg)` + the streaming `AgentAdapter.run(...)` with the backend-neutral
`AgentTools` / `AgentOptions`, under `force_backend(..., "task", backend)` — so
the smoke matches how `daemon.run_task` dispatches. For the codex variant we
force a WRITABLE sandbox (`workspace-write`, i.e. `Sandbox.workspace_write`) —
NOT the read-only sandbox the dispatch smoke uses — so the codex write/commit
path is actually exercised: the parity-critical difference between "codex echoes
text" and "codex does work".

OPT-IN — same gate as the other real-SDK smokes: this test makes real API
calls. It only runs when `AP2_REAL_SDK` is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The default `pytest` invocation (and CI) skips it via the module-level skip
marker. It is run on the 6h `real-sdk-smoke` cron routine
(`ap2.smoke_runner.run_smoke_check`, which executes the whole
`ap2/tests/smoke/` directory when `AP2_REAL_SDK` is set), so dropping this file
into `ap2/tests/smoke/` wires it onto that cron alongside the other smokes.

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors; a missing credential / transport hiccup flows through the
shared `call_with_transient_retry`-then-skip helper, identical to the Claude
path. The change is kept small (one file, one commit) to bound cost.

No `git push`: the smoke commits locally in a throwaway temp repo with no remote
configured, and pins `Bash(git push*)` in `disallowed`, mirroring the task-agent
posture (`daemon._TASK_DISALLOWED_TOOLS`).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ._adapter import BACKENDS, extract_tool_calls, force_backend, gate_backend
from ._transient import call_with_transient_retry

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)

# The concrete change the agent is asked to make: create this file with this
# single-line marker, then commit it. Small + deterministic to bound cost.
_SMOKE_FILENAME = "AP2_SMOKE_REAL_WORK.txt"
_MARKER = "AP2_REAL_WORK_SMOKE_OK"


def _git(root: Path, *args: str) -> str:
    """Run a git command in `root`, returning stripped stdout (raises on error).

    Used only by the harness to seed / inspect the temp repo — NOT by the agent
    (the agent runs its own git via the backend's command tool).
    """
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _bootstrap_repo(root: Path):
    """Seed a temp git repo + minimal project skeleton, return `(cfg, head)`.

    Writes the `TASKS.md` / `CLAUDE.md` skeleton `Config.load` +
    `build_task_prompt` need (mirroring the sibling smokes' `_bootstrap_project`),
    initializes a git repo with a local identity (so the agent's `git commit`
    succeeds without global config) and NO remote (so `git push` is impossible),
    and lands a seed commit so HEAD exists before the agent's run. Returns the
    loaded `Config` and the seed commit SHA (the pre-run HEAD).
    """
    from ap2.config import Config

    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    (root / "CLAUDE.md").write_text(
        "# Smoke project\n\n## Autopilot\n\n- Next task ID: TB-2\n"
    )
    (root / "README.md").write_text("# real-work smoke seed\n")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "smoke@example.com")
    _git(root, "config", "user.name", "AP2 Smoke")
    # No `git remote add` — a push has nowhere to go, belt-and-suspenders with
    # the disallowed `Bash(git push*)`.
    _git(root, "add", "TASKS.md", "CLAUDE.md", "README.md")
    _git(root, "commit", "-q", "-m", "seed: initial commit")

    cfg = Config.load(root)
    cfg.ensure_dirs()
    return cfg, _git(root, "rev-parse", "HEAD")


def _real_work_task():
    """A synthetic `task` whose description asks for REAL work: create a file,
    commit it, read back the commit SHA, and report it. Mirrors the
    `_fake_task()` shape the no-work report_result smoke uses, but the body is a
    concrete edit + command + commit rather than "do nothing"."""
    from ap2.board import Task

    return Task(
        id="TB-1",
        title="real-work smoke",
        section="Active",
        description=(
            "TEST SCENARIO — real-work smoke. You are in a git repository "
            "(the current working directory). Do REAL work, in order:\n"
            f"  1. Create a file named `{_SMOKE_FILENAME}` whose entire "
            f"contents are exactly the single line: {_MARKER}\n"
            "  2. Stage and commit ONLY that file by running:\n"
            f"        git add {_SMOKE_FILENAME}\n"
            '        git commit -m "TB-1: real-work smoke commit"\n'
            "  3. Read back the SHA of the commit you just created by "
            "running:\n"
            "        git rev-parse HEAD\n"
            "  4. Call the `mcp__autopilot__report_result` tool ONCE with:\n"
            "       status: complete\n"
            "       commit: <the full SHA that `git rev-parse HEAD` printed "
            "in step 3>\n"
            '       summary: "real-work smoke ok"\n'
            f'       files_changed: "{_SMOKE_FILENAME}"\n'
            "       tests_passed: \"true\"\n"
            "Do NOT run `git push`. Then end your turn."
        ),
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_task_real_work_round_trip_via_adapter(backend, monkeypatch, tmp_path):
    """Real agent doing REAL work, dispatched through the production
    `AgentAdapter` seam. For BOTH backends: the agent edits a file, commits it,
    and reports the ACTUAL commit SHA back through `report_result`. Asserts the
    file change exists, a commit was created, and the reported SHA + non-empty
    `files_changed` round-trip — codex via a WRITABLE sandbox."""
    import asyncio

    gate_backend(backend)
    # Pin the `task` kind to this backend so `select_adapter("task", cfg)`
    # resolves to the matching adapter — the operator's "set the kind's backend
    # to codex and run the existing smoke" capability.
    force_backend(monkeypatch, "task", backend)

    from ap2.adapters import AgentOptions, AgentTools, select_adapter
    from ap2.daemon import _task_result_from_tool_args
    from ap2.prompts import build_task_prompt
    from ap2.tools import TASK_AGENT_TOOLS, build_mcp_server

    root = tmp_path / "repo"
    root.mkdir()
    cfg, seed_head = _bootstrap_repo(root)

    async def go() -> tuple[list[dict], str, object]:
        # Dispatch flows through the SAME seam production uses: the per-kind
        # backend resolver + the streaming `AgentAdapter.run(...)`, with the
        # full ap2 toolset registered through the selected adapter.
        adapter = select_adapter("task", cfg)
        assert adapter.backend == backend, adapter.backend
        mcp_server = build_mcp_server(cfg, adapter=adapter)
        prompt = build_task_prompt(cfg, _real_work_task())

        tools = AgentTools(
            allowed=TASK_AGENT_TOOLS,
            # Mirror the task-agent no-push posture (daemon._TASK_DISALLOWED_TOOLS).
            disallowed=["Bash(git push*)", "Bash(rm -rf *)"],
            mcp_servers={"autopilot": mcp_server},
        )
        options = AgentOptions(
            cwd=str(root),
            permission_mode="bypassPermissions",
            max_turns=14,
            setting_sources=["project"],
        )
        if backend == "codex":
            # The point of this smoke for codex: a WRITABLE sandbox
            # (`Sandbox.workspace_write`) so the file-write + commit path is
            # actually exercised — NOT the read-only sandbox the dispatch smoke
            # pins. `effort="low"` bounds cost.
            options.effort = "low"
            options.extra = {"sandbox": "workspace-write"}

        tool_calls: list[dict] = []
        final_text = ""
        terminal_result: object = None
        async for ev in adapter.run(prompt, tools, options):
            if ev.result is not None:
                terminal_result = ev.result
                continue
            tool_calls.extend(extract_tool_calls(ev.raw))
            if ev.text:
                final_text = ev.text
        return tool_calls, final_text, terminal_result

    # TB-351: a transient SDK transport/service error (or a missing credential)
    # is *raised* out of the adapter drain — retry once, then skip (not error).
    # A genuine wiring/work regression flows to the asserts below and still fails.
    tool_calls, final_text, terminal_result = call_with_transient_retry(
        lambda: asyncio.run(go()),
        describe=f"real-work task round-trip smoke [{backend}]",
    )

    print(f"\n[smoke:{backend}] {len(tool_calls)} tool calls observed:")
    for tc in tool_calls:
        print(f"  - {tc['name']!r}: {str(tc['input'])[:200]}")

    # ---- Real-work outcome 1: the file change exists in the temp repo. -------
    smoke_file = root / _SMOKE_FILENAME
    assert smoke_file.exists(), (
        f"[{backend}] agent did not create {_SMOKE_FILENAME} in the repo. "
        f"Final text: {final_text[:500]!r}. "
        f"Tools used: {[tc['name'] for tc in tool_calls]}"
    )
    assert _MARKER in smoke_file.read_text(), (
        f"[{backend}] {_SMOKE_FILENAME} exists but does not contain the marker "
        f"{_MARKER!r}: {smoke_file.read_text()[:200]!r}"
    )

    # ---- Real-work outcome 2: a new commit was created. ----------------------
    new_head = _git(root, "rev-parse", "HEAD")
    assert new_head != seed_head, (
        f"[{backend}] HEAD did not advance past the seed commit ({seed_head}); "
        "the agent did not create a commit."
    )
    # The committed tree actually carries the file (a real commit, not a stray
    # working-tree write).
    committed = _git(root, "show", "--name-only", "--format=", "HEAD")
    assert _SMOKE_FILENAME in committed, (
        f"[{backend}] the new commit {new_head[:8]} does not include "
        f"{_SMOKE_FILENAME}; committed paths: {committed!r}"
    )
    # No push could have happened: there is no remote configured.
    assert _git(root, "remote") == "", (
        f"[{backend}] unexpected git remote configured: "
        f"{_git(root, 'remote')!r}"
    )

    # ---- Real-work outcome 3: the ACTUAL commit SHA round-tripped through
    #      report_result, with a non-empty files_changed. ----------------------
    completes = [
        tc for tc in tool_calls
        if tc["name"] in ("report_result", "mcp__autopilot__report_result")
    ]
    assert completes, (
        f"[{backend}] agent did not call report_result. Final text: "
        f"{final_text[:500]!r}. Tools used: {[tc['name'] for tc in tool_calls]}"
    )
    args = completes[-1]["input"]

    # The captured args still convert to a valid `complete` TaskResult via the
    # production daemon path, exactly as the no-work report_result smoke checks.
    result = _task_result_from_tool_args(args)
    assert result.status == "complete", result

    reported_commit = str(args.get("commit", "")).strip()
    assert reported_commit, (
        f"[{backend}] report_result carried an EMPTY commit — the whole point "
        f"of the real-work smoke is a non-empty, real SHA. args={args!r}"
    )
    # The reported SHA is the ACTUAL commit, not a fabricated string: it is a
    # (case-insensitive) prefix of the repo's real HEAD (the agent may report
    # the short or full form).
    assert new_head.lower().startswith(reported_commit.lower()), (
        f"[{backend}] reported commit {reported_commit!r} is not a prefix of "
        f"the repo's real HEAD {new_head!r}"
    )

    reported_files = str(args.get("files_changed", "")).strip()
    assert reported_files, (
        f"[{backend}] report_result carried an EMPTY files_changed; "
        f"expected the changed file. args={args!r}"
    )

    usage = getattr(terminal_result, "usage", None)
    combined = getattr(usage, "combined_tokens", "?") if usage else "?"
    print(
        f"[smoke:{backend}] PASS — real work committed {new_head[:8]} "
        f"(reported {reported_commit[:8]}), files_changed={reported_files!r}, "
        f"combined_tokens={combined}"
    )
