"""TB-392: minimal-kernel e2e — dispatch → verify (shell) → report.

Pins the final unproven Progress signal of the **Current focus: get the
component boundary right — loop-level participants only**: "a task
dispatches → verifies (shell) → reports in that minimal-kernel config"
(goal.md L234-236), backing the Done-when criterion that "the full test
suite passes in the default configuration AND in an 'every component
disabled' configuration" (goal.md L68-70).

`ap2/tests/test_components_disabled.py` already smoke-tests the core
SURFACES (board parse, briefing validators, operator-queue drain,
status-report compose, channel routing) under the all-components-disabled
env — but nothing exercises a full daemon TICK in that minimal kernel.
This test closes that gap: with EVERY env-flag-bearing component disabled
(registry-driven via the shared `enumerate_disabled_env_flags()` helper,
plus an explicit `AP2_IDEATION_DISABLED` post-TB-391), it runs one
`daemon._tick` against a stubbed agent and asserts a Ready task
dispatches, passes shell-bullet verification, and lands in Complete with
a `task_verify` event carrying `verdict=pass`.

Hermetic: the agent run is faked via the shared `FakeSDK` harness exactly
like `test_single_tick.py` / `test_verify_per_task.py` — no live SDK. The
stubbed agent does real work (writes the artifact the shell `## Verification`
bullet checks for) before reporting complete, so the verifier exercises the
genuine dispatch→verify→report loop rather than a trivially-true bullet.

The disable list is registry-driven (the helper walks
`Registry.discover().components`) so this test auto-tracks any new
env-flag-bearing component instead of hardcoding names — when a future
refactor adds a component, this minimal-kernel pin disables it too.
"""
from __future__ import annotations

import asyncio

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick
from ap2.registry import (
    Registry,
    _reset_default_registry,
    default_registry,
)

from ap2.tests.e2e._fakes import FakeSDK, _FakeMixedMsg, _FakeToolUseBlock
from ap2.tests.test_components_disabled import enumerate_disabled_env_flags


def _seed_ready_with_shell_briefing(cfg, task_id: str, verification: str) -> None:
    """Write a briefing with a `## Verification` shell section under
    `.cc-autopilot/tasks/` and seed a matching Ready task linked to it."""
    brief_path = cfg.tasks_dir / f"{task_id.lower()}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(
        f"# {task_id} — minimal-kernel smoke\n\n"
        "## Goal\nProve the minimal-kernel dispatch→verify→report loop.\n\n"
        "## Verification\n"
        f"{verification}\n"
    )
    rel = str(brief_path.relative_to(cfg.project_root))
    board = Board.load(cfg.tasks_file)
    board.add("Ready", task_id=task_id, title="minimal-kernel smoke", briefing=rel)
    board.save()


def _artifact_writing_complete_responder(
    sdk: FakeSDK, cfg, task_id: str, artifact_rel: str
) -> None:
    """Wire the FakeSDK so the stubbed agent, when dispatched for `task_id`,
    does its work — writes `artifact_rel` (the file the shell `## Verification`
    bullet checks for) under the project root — then reports complete.

    Mirrors `tool_call_respond` but with an artifact-writing side effect so
    the shell verifier (which runs with `cwd=project_root`) sees a genuine
    work product rather than a trivially-true bullet.
    """

    def factory(prompt, options):  # noqa: ARG001
        async def gen():
            (cfg.project_root / artifact_rel).write_text("done\n")
            yield _FakeMixedMsg([_FakeToolUseBlock(
                name="report_result",
                input={
                    "status": "complete",
                    "commit": "abc12345",
                    "summary": f"did {task_id} in the minimal kernel",
                    "files_changed": artifact_rel,
                    "tests_passed": "true",
                },
            )])
        return gen()

    sdk.on(f"## Task\n{task_id}", factory)


def test_minimal_kernel_tick_dispatch_verify_report(e2e_project, monkeypatch):
    """Every component disabled → a Ready task still dispatches, shell-verifies,
    and lands in Complete with a `task_verify verdict=pass` event."""
    # 1. Disable every env-flag-bearing component (registry-driven, NOT
    #    hardcoded — the helper walks `Registry.discover().components`).
    flags = enumerate_disabled_env_flags(Registry.discover())
    for key, val in flags.items():
        if val:
            monkeypatch.setenv(key, val)
        else:
            monkeypatch.delenv(key, raising=False)
    # Post-TB-391 the helper already maps `AP2_IDEATION_DISABLED -> "1"`, but
    # set it explicitly so the minimal-kernel config is robust to ordering /
    # any future helper change.
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    # Force re-discovery so the cached registry reflects the disabled env.
    _reset_default_registry()

    try:
        # Sanity: the kernel really is minimal — every env-flag-bearing
        # component (ideation included) dropped out; only always-on
        # (env_flag=None) components remain.
        registry = default_registry()
        enabled = {m.name for m in registry.enabled_components()}
        for manifest in registry.components:
            if manifest.env_flag is not None:
                assert manifest.name not in enabled, (
                    f"{manifest.name!r} should be disabled in the minimal "
                    f"kernel; enabled={sorted(enabled)}"
                )
        assert "ideation" not in enabled, enabled

        # 2. Build a fresh project + seed one Ready task whose briefing has a
        #    passing shell bullet (`test -f <artifact>` the stubbed agent
        #    creates).
        cfg = e2e_project()
        artifact_rel = "kernel_artifact.txt"
        _seed_ready_with_shell_briefing(
            cfg, "TB-700",
            verification=f"- `test -f {artifact_rel}` — the agent created the artifact\n",
        )

        # 3. Stub the agent: write the artifact, then report complete.
        sdk = FakeSDK()
        _artifact_writing_complete_responder(sdk, cfg, "TB-700", artifact_rel)

        # 4. One daemon tick — dispatch → verify (shell) → report.
        asyncio.run(_tick(cfg, sdk, mcp_server=None))

        # 5a. The task moved Ready → Complete.
        board = Board.load(cfg.tasks_file)
        assert board.find("TB-700")[0] == "Complete", board.find("TB-700")

        # 5b. A terminal `task_verify` event with verdict=pass was emitted,
        #     and the shell bullet passed (1/1).
        evts = events.tail(cfg.events_file, 40)
        kinds = [e["type"] for e in evts]
        verifies = [e for e in evts if e["type"] == "task_verify"]
        assert len(verifies) == 1, evts
        tv = verifies[0]
        assert tv["task"] == "TB-700"
        assert tv["verdict"] == "pass", tv
        assert tv["shell"] == "1/1", tv
        bullet_kinds = [b["kind"] for b in tv["bullets"]]
        assert bullet_kinds == ["shell"], tv["bullets"]
        assert all(b["verdict"] == "pass" for b in tv["bullets"]), tv["bullets"]

        # 5c. Lifecycle ordering: task_solve → task_verify → task_complete.
        assert "task_solve" in kinds
        assert "task_complete" in kinds
        assert (
            kinds.index("task_solve")
            < kinds.index("task_verify")
            < kinds.index("task_complete")
        )
    finally:
        # Drop the cached registry so a sibling test gets a clean discovery
        # pass against the (monkeypatch-reverted) env state.
        _reset_default_registry()
