"""E2E tests for per-task verification (TB-69) — daemon hook + verifier.

These tests exercise the path: parse_result → TB-66 gate (skipped here, no
AP2_VERIFY_CMD) → TB-69 per-task verifier → move_to_complete (or
verification_failed). The verifier itself is unit-tested in test_verify.py
on the pure parsing/aggregation paths; this file pins the daemon integration.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ap2 import events
from ap2.board import Board
from ap2.daemon import _tick

from ap2.tests.e2e._fakes import FakeSDK, text_respond, tool_call_respond


def _seed_briefing(cfg, task_id: str, title: str, verification_section: str) -> str:
    """Write a briefing under .cc-autopilot/tasks/ and return its rel path."""
    brief_path = cfg.tasks_dir / f"{task_id.lower()}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"# {task_id} — {title}\n\n"
        "## Goal\nDo the thing.\n\n"
        "## Verification\n"
        f"{verification_section}\n"
    )
    brief_path.write_text(text)
    return str(brief_path.relative_to(cfg.project_root))


def _seed_ready_with_briefing(cfg, task_id: str, briefing_section: str) -> None:
    rel = _seed_briefing(cfg, task_id, "Run the thing", briefing_section)
    board = Board.load(cfg.tasks_file)
    board.add("Ready", task_id=task_id, title="Run the thing", briefing=rel)
    board.save()


def _complete_responder(sdk: FakeSDK, task_id: str) -> None:
    sdk.on(
        f"## Task\n{task_id}",
        tool_call_respond(
            "report_result",
            {
                "status": "complete", "commit": "abc12345",
                "summary": f"did {task_id}", "files_changed": "a.py",
            },
        ),
    )


def test_verifier_skipped_when_briefing_has_no_verification_section(e2e_project):
    """Backward-compat regression pin (stoch et al.): a task whose briefing
    lacks a `## Verification` section must proceed to Complete unchanged.
    """
    cfg = e2e_project()
    # Briefing with no verification section
    rel = _seed_briefing(cfg, "TB-50", "legacy",
                         verification_section="")  # empty section won't be added
    # Actually, write a briefing with NO ## Verification section at all
    brief_path = cfg.tasks_dir / "tb-50.md"
    brief_path.write_text("# TB-50 — legacy\n\n## Goal\nLegacy work.\n")
    board = Board.load(cfg.tasks_file)
    board.add("Ready", task_id="TB-50", title="legacy",
              briefing=str(brief_path.relative_to(cfg.project_root)))
    board.save()

    sdk = FakeSDK()
    _complete_responder(sdk, "TB-50")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-50")[0] == "Complete"
    evts = events.tail(cfg.events_file, 30)
    # Skip path → no verification_failed / verification_partial events.
    assert all(e["type"] not in ("verification_failed", "verification_partial")
               for e in evts)


def test_verifier_skipped_when_no_briefing_at_all(e2e_project):
    """A Ready task without a briefing link (older add_ready calls) skips
    verification entirely — the verifier returns None."""
    cfg = e2e_project(ready_task=("TB-51", "no briefing"))

    sdk = FakeSDK()
    _complete_responder(sdk, "TB-51")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-51")[0] == "Complete"


def test_verifier_passes_all_shell_bullets_moves_to_complete(e2e_project):
    """All shell bullets exit 0 → verdict pass → task lands in Complete."""
    cfg = e2e_project()
    _seed_ready_with_briefing(
        cfg, "TB-52",
        briefing_section=(
            "- `true` — first check\n"
            "- `echo ok > /dev/null` — second check\n"
        ),
    )

    sdk = FakeSDK()
    _complete_responder(sdk, "TB-52")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-52")[0] == "Complete"


def test_verifier_fails_one_shell_bullet_moves_to_backlog(e2e_project):
    """A shell bullet that exits non-zero flips the overall verdict to fail
    and routes the task back through `_handle_failure` (Backlog → retry)."""
    cfg = e2e_project()
    _seed_ready_with_briefing(
        cfg, "TB-53",
        briefing_section=(
            "- `true` — passes\n"
            "- `false` — fails\n"
        ),
    )

    sdk = FakeSDK()
    _complete_responder(sdk, "TB-53")
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-53")[0] == "Backlog"

    evts = events.tail(cfg.events_file, 30)
    failures = [e for e in evts if e["type"] == "verification_failed"
                and e.get("kind") == "per_task"]
    assert len(failures) == 1
    f = failures[0]
    assert f["task"] == "TB-53"
    assert f["overall"] == "fail"
    # Per-criterion detail must be in the event for diagnosis.
    statuses = [c["status"] for c in f["criteria"]]
    assert statuses == ["pass", "fail"]


def test_verifier_partial_with_unverified_prose_completes_with_event(e2e_project):
    """Prose bullet without an SDK to judge → unverified → overall partial.
    Daemon treats partial as a soft pass: task moves to Complete but a
    `verification_partial` event is logged."""
    cfg = e2e_project()
    _seed_ready_with_briefing(
        cfg, "TB-54",
        briefing_section=(
            "- `true` — shell bullet passes\n"
            "- prose bullet that requires SDK judging\n"
        ),
    )

    # FakeSDK does NOT have a responder for the verify-prompt — fake_sdk
    # falls through with an empty default response → judge returns
    # unverified, NOT pass/fail. With one shell pass + one unverified, the
    # verdict aggregates to "partial" → Complete + verification_partial event.
    sdk = FakeSDK()
    _complete_responder(sdk, "TB-54")
    # The verifier passes the same `sdk` to _judge_prose_bullet — but for
    # prompts it doesn't recognize, FakeSDK returns the empty default which
    # the parser converts to "unverified" with an "empty judge response" note.

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-54")[0] == "Complete"

    evts = events.tail(cfg.events_file, 30)
    partials = [e for e in evts if e["type"] == "verification_partial"]
    assert len(partials) == 1
    statuses = [c["status"] for c in partials[0]["criteria"]]
    assert "pass" in statuses and "unverified" in statuses


def test_verifier_uses_task_commit_diff_on_retry_not_head(e2e_project):
    """TB-127 regression pin: on retry of a task whose first attempt already
    committed an implementation, the prose-bullet judge must see the
    implementation diff (the `<task_id>: ...` commit), NOT HEAD's daemon
    bookkeeping diff.

    Setup mirrors the bug observed live (TB-122 / TB-123 on 2026-04-30):
      1. An "implementation commit" with subject `TB-60: implement foo`
         introduces `foo.py`.
      2. A daemon `state:` commit on top — this is what HEAD points at
         when the retry tick begins.
      3. A task is seeded Ready with a prose bullet referring to `foo.py`.
      4. The fake task agent emits `report_result(complete)` without
         making a new commit (the agent correctly recognized the work is
         already done).
      5. The fake judge inspects the diff it receives and passes only if
         it contains `foo.py` + `def foo`.

    Before the fix: judge would receive HEAD's diff (TASKS.md only), say
    fail, task lands in Backlog. After the fix: judge receives the
    implementation commit's diff, says pass, task lands in Complete.
    """
    import subprocess

    def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(cwd)] + args,
            capture_output=True, text=True, check=True,
        )

    cfg = e2e_project()
    root = cfg.project_root

    # Init git, plant baseline + implementation + state commit.
    _git(["init", "--initial-branch=main"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["commit", "--allow-empty", "-m", "init"], root)

    (root / "foo.py").write_text("def foo():\n    return 42\n")
    _git(["add", "foo.py"], root)
    _git(["commit", "-m", "TB-60: implement foo"], root)

    # Daemon state commit on top — simulates what HEAD looks like when
    # the retry tick begins.
    (root / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    _git(["add", "TASKS.md"], root)
    _git(["commit", "-m", "state: TB-60 → Backlog"], root)

    # Seed Ready with a prose-bullet briefing.
    _seed_ready_with_briefing(
        cfg, "TB-60",
        briefing_section=(
            "- `true` — shell bullet passes\n"
            "- The diff adds `foo.py` with a `def foo` definition\n"
        ),
    )

    sdk = FakeSDK()
    _complete_responder(sdk, "TB-60")

    # Custom judge responder: pass iff the diff carries the implementation
    # file. This is the discriminator — without TB-127's fix the judge
    # would see only TASKS.md and the assertion below would fail.
    captured: dict = {}

    async def _judge_gen(prompt, options):  # noqa: ARG001
        captured["prompt"] = prompt
        from ap2.tests.e2e._fakes import _FakeMsg
        if "foo.py" in prompt and "def foo" in prompt:
            yield _FakeMsg('{"status": "pass", "rationale": "foo.py present"}')
        else:
            yield _FakeMsg('{"status": "fail", "rationale": "no foo.py in diff"}')

    sdk.on("evaluating ONE acceptance bullet", _judge_gen)

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # The judge actually got called.
    assert "prompt" in captured, "judge responder was never invoked"
    # Pin the discriminator: the implementation file landed in the diff.
    assert "foo.py" in captured["prompt"]
    assert "def foo" in captured["prompt"]

    board = Board.load(cfg.tasks_file)
    assert board.find("TB-60")[0] == "Complete", (
        "task should land in Complete because the prose judge saw the real "
        "implementation diff and passed; if this asserts Backlog the verifier "
        "is back to feeding HEAD-only diffs to the judge (TB-127 regression)."
    )


def test_add_backlog_rejects_missing_briefing():
    """TB-135 inversion of the prior auto-fill test: without an explicit
    `briefing` payload the call now fails with `briefing is required`,
    nothing lands on the board, no briefing file is written. The
    auto-fill skeleton (TB-69) was retired because its placeholder
    `## Verification` line slipped through the per-task verifier and
    completed tasks like TB-131 with zero scope-specific scoring."""
    import tempfile
    from pathlib import Path
    from ap2.config import Config
    from ap2.init import init_project
    from ap2.tools import do_board_edit

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        init_project(root)
        cfg = Config.load(root)
        cfg.ensure_dirs()

        before_tasks = (root / "TASKS.md").read_text()
        tasks_dir = root / ".cc-autopilot" / "tasks"
        before_briefings = sorted(p.name for p in tasks_dir.iterdir())

        res = do_board_edit(cfg, {
            "action": "add_backlog",
            "title": "no briefing",
            "tags": [],
            "description": "what success looks like",
            # briefing intentionally omitted — must error.
        })
        assert res.get("isError"), res
        assert "briefing is required" in res["content"][0]["text"]
        # Board untouched, no briefing file written.
        assert (root / "TASKS.md").read_text() == before_tasks
        assert sorted(p.name for p in tasks_dir.iterdir()) == before_briefings


def test_add_backlog_preserves_explicit_briefing():
    """When the caller passes a briefing payload, the template is NOT injected
    — TB-69's auto-fill only kicks in when briefing is omitted/empty."""
    import tempfile
    from pathlib import Path
    from ap2.config import Config
    from ap2.init import init_project
    from ap2.tools import do_board_edit

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        init_project(root)
        cfg = Config.load(root)
        cfg.ensure_dirs()

        # TB-154: structurally canonical briefing — every add_* path
        # now rejects briefings missing one of the canonical
        # `##`-level sections. The original test claim (explicit
        # briefing payload is preserved on disk verbatim, NOT
        # auto-filled with the template) survives — we just need
        # canonical-shape input for the gate to let the call through.
        custom = (
            "# Custom\n\n"
            "## Goal\n\nstub\n\n"
            "## Scope\n\n- foo.py\n\n"
            "## Design\n\nstub\n\n"
            "## Verification\n- `uv run pytest -q`\n\n"
            "## Out of scope\n\n- nothing\n"
        )
        res = do_board_edit(cfg, {
            "action": "add_backlog",
            "title": "explicit briefing",
            "briefing": custom,
        })
        assert not res.get("isError"), res

        import json as _json
        body = _json.loads(res["content"][0]["text"])
        brief_path = root / body["briefing_path"]
        assert brief_path.read_text() == custom
