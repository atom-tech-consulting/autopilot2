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


def test_add_backlog_writes_template_when_briefing_omitted():
    """Pin the auto-fill: do_board_edit add_backlog without `briefing` writes
    the BRIEFING_TEMPLATE, including the `## Verification` section."""
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

        res = do_board_edit(cfg, {
            "action": "add_backlog",
            "title": "auto-template task",
            "tags": [],
            "description": "what success looks like",
            # briefing omitted on purpose
        })
        assert not res.get("isError"), res

        # The briefing file should exist and have the Verification section.
        import json as _json
        body = _json.loads(res["content"][0]["text"])
        brief_path = root / body["briefing_path"]
        assert brief_path.exists()
        text = brief_path.read_text()
        assert "## Verification" in text
        assert "uv run pytest" in text


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

        custom = "# Custom\n\nNo verification section.\n"
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
