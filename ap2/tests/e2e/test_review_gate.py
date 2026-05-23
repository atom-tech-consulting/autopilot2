"""E2E for TB-121: ideation-proposed tasks are gated behind operator
review before dispatch.

Three paths exercised end-to-end against the real `_tick`:
  1. A review-gated Backlog task is NOT auto-promoted (the daemon
     ticks past it; the board section stays unchanged).
  2. After `ap2 approve TB-N` runs through the operator queue and the
     daemon drains it, the next tick auto-promotes the task to Ready
     and dispatches it like any other Backlog item.
  3. The ideation cron fires against an empty board and EVERY task it
     proposes lands with `(blocked on: review)` (rendered as the
     `@blocked:review` codespan). Proves the prompt-change took effect
     end-to-end: the default ideation prompt instructs the agent to
     pass `blocked_on: "review"` on every `add_backlog` call, and the
     fake-agent stand-in (which mimics that instruction by routing
     through `do_board_edit`) leaves the resulting Backlog tasks all
     review-gated — none escape the gate.

These pin the load-bearing assertion: ideation's autonomous proposal
pipeline can't reach the dispatch loop without operator action.
"""
from __future__ import annotations

import asyncio
from argparse import Namespace

from ap2 import events, tools
from ap2.board import Board
from ap2.cli import cmd_approve
from ap2.daemon import _tick

from ap2.tests._briefing_fixtures import canonical_briefing
from ap2.tests.e2e._fakes import FakeSDK, _FakeMsg, tool_call_respond


def _seed_review_gated(cfg, *, task_id="TB-50", title="ideation proposal"):
    """Seed Backlog with one review-gated task, mimicking ideation."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id=task_id,
        title=title,
        meta={"blocked": "review"},
    )
    board.save()


def test_tick_does_not_promote_review_gated_backlog(e2e_project):
    """A `@blocked:review` task in Backlog stays in Backlog across a
    tick — auto-promotion's `_is_blocker_satisfied("review")` is False.
    """
    cfg = e2e_project()
    _seed_review_gated(cfg, task_id="TB-50")

    sdk = FakeSDK()
    # No "## Task\nTB-50" handler — if the daemon DID try to dispatch
    # it, FakeSDK would error out unhandled.
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    board = Board.load(cfg.tasks_file)
    # Section unchanged — task neither moved nor mutated.
    assert board.find("TB-50")[0] == "Backlog"
    t = board.get("TB-50")
    assert t is not None
    assert t.blocked_on == ["review"]

    # No task lifecycle events (since dispatch never started).
    evts = events.tail(cfg.events_file, n=20)
    kinds = [e["type"] for e in evts]
    assert "task_start" not in kinds
    assert "backlog_auto_promoted" not in kinds


def test_approve_then_tick_promotes_and_dispatches(e2e_project):
    """`ap2 approve TB-N` queues the strip; one tick drains the queue
    AND (in the same tick — drain runs before dispatch) auto-promotes
    the now-ungated task. The dispatch path matches a normal Backlog
    item's lifecycle."""
    cfg = e2e_project()
    _seed_review_gated(cfg, task_id="TB-60", title="now approve me")

    rc = cmd_approve(cfg, Namespace(task_id="TB-60"))
    assert rc == 0
    # CLI didn't drain the queue — TASKS.md is still unchanged at this point.
    raw_pre_tick = cfg.tasks_file.read_text()
    assert "`@blocked:review`" in raw_pre_tick

    sdk = FakeSDK()
    sdk.on(
        "## Task\nTB-60",
        tool_call_respond(
            "report_result",
            {
                "status": "complete",
                "commit": "facade12",
                "summary": "ran the approved task",
                "files_changed": "",
                "tests_passed": "true",
            },
        ),
    )
    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # Drain stripped the codespan, auto-promotion picked it up, dispatch
    # ran, completion event landed.
    raw_post_tick = cfg.tasks_file.read_text()
    assert "`@blocked:review`" not in raw_post_tick
    board = Board.load(cfg.tasks_file)
    assert board.find("TB-60")[0] == "Complete"

    evts = events.tail(cfg.events_file, n=20)
    kinds = [e["type"] for e in evts]
    assert "ideation_approved" in kinds
    assert "task_start" in kinds
    assert "task_complete" in kinds
    # Approved before dispatch (drain runs before backlog promotion).
    assert kinds.index("ideation_approved") < kinds.index("task_start")


# ---------------------------------------------------------------------------
# TB-121 verification bullet: "ideation cron fires against an empty board →
# produces N proposed tasks, ALL with `(blocked on: review)` clauses
# (gating — proves the prompt change took effect end-to-end)."
#
# A FakeSDK can't actually run the LLM, so this stand-in simulates an
# instruction-following agent: when ideation fires we route the agent's
# "tool calls" through the real `do_board_edit` with `blocked_on="review"`
# (which is what the prompt's TB-121 gate section instructs). The end-to-
# end signal is that ideation's whole pipeline — empty-board trigger →
# agent → board mutation → on-disk TASKS.md — leaves every new Backlog
# task review-gated. A separate `test_ideation_defaults` test pins that
# the default prompt actually contains the `blocked on: review` directive,
# so the two tests together close the loop: prompt says it, infrastructure
# carries it through.


def _ideation_proposes_n_gated(cfg, n: int):
    """Async-gen factory mimicking an ideation agent that follows the
    TB-121 gate directive on N proposals.

    Each simulated `board_edit({"action":"add_backlog",...})` call passes
    `blocked_on="review"` exactly as the default ideation prompt
    instructs (see `ap2/ideation.default.md` "## Human-review gate").
    The real `do_board_edit` is invoked under the board lock, so the
    resulting TASKS.md write goes through the same code path the live
    SDK→MCP bridge would.
    """

    async def gen(prompt, options):  # noqa: ARG001
        for i in range(n):
            tools.do_board_edit(
                cfg,
                {
                    "action": "add_backlog",
                    "title": f"ideation proposal #{i + 1}",
                    "tags": ["proposed"],
                    "briefing": canonical_briefing(
                        f"TB-PROP{i + 1}",
                        title=f"Ideation proposal {i + 1}",
                        verification=(
                            "- `true` — placeholder shell bullet so the "
                            "briefing satisfies TB-135 / TB-138 "
                            "(auto-verifiable).\n"
                        ),
                    ),
                    # Load-bearing: the prompt's TB-121 gate instructs
                    # every add_backlog call to set blocked_on="review".
                    "blocked_on": "review",
                },
            )
        yield _FakeMsg("(ideation done — 3 gated proposals)")

    return gen


def test_ideation_cron_proposals_are_all_review_gated(e2e_project, monkeypatch):
    """End-to-end: empty board → ideation fires → every proposal lands
    in Backlog with `@blocked:review`. Verifies the gate is uniform —
    no proposal escapes review.
    """
    import time
    from ap2.cron import save_state

    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "3600")
    # TB-280 hermeticity fix: the operator's shell sets
    # `AP2_AUTO_APPROVE=1`, which leaks into pytest and triggers
    # `tools.do_board_edit`'s add_backlog branch to strip the
    # `@blocked:review` codespan from proposed rows — the exact
    # gate this test is asserting on. Without scrubbing the env,
    # the assertion would fail purely on operator-shell pollution
    # rather than a real regression in the gate. Mirrors the
    # delenv pattern already used in `test_cli_status_json_*`.
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)

    # No `ideation_prompt=` override → the daemon loads the real
    # `ap2/ideation.default.md`, so this test exercises the same prompt
    # the production daemon ships. The TB-121 gate language is the
    # signal we're proving end-to-end.
    cfg = e2e_project()
    save_state(cfg.cron_state_file, {"ideation": time.time() - 7200})

    sdk = FakeSDK()
    # Match a substring unique to the default ideation prompt so this
    # responder fires only for the ideation control-agent invocation
    # (not for any cron job that might be added later).
    sdk.on("Human-review gate", _ideation_proposes_n_gated(cfg, n=3))

    asyncio.run(_tick(cfg, sdk, mcp_server=None))

    # 1. Ideation actually fired — without this the rest is moot.
    kinds = [e["type"] for e in events.tail(cfg.events_file, 50)]
    assert "ideation_empty_board" in kinds, (
        f"ideation didn't fire; events were {kinds}"
    )

    # 2. Three new Backlog tasks landed (matches the simulated agent's
    # 3 add_backlog calls).
    board = Board.load(cfg.tasks_file)
    backlog = list(board.iter_tasks("Backlog"))
    assert len(backlog) == 3, (
        f"expected 3 ideation proposals in Backlog, got {len(backlog)}: "
        f"{[(t.id, t.title) for t in backlog]}"
    )

    # 3. EVERY proposal carries the review gate — no escapes. This is
    # the load-bearing assertion: a hallucinated proposal can't slip
    # past auto-promotion because the gate is uniform.
    for t in backlog:
        assert t.blocked_on == ["review"], (
            f"{t.id} {t.title!r} missing review gate; "
            f"blocked_on={t.blocked_on!r}"
        )

    # 4. The on-disk TASKS.md renders the codespan literally — that's
    # the surface auto-promotion / `_is_blocker_satisfied` reads. Pin
    # the rendered shape so a refactor that changes Task.render()
    # without updating the gate semantics breaks here.
    raw = cfg.tasks_file.read_text()
    assert raw.count("`@blocked:review`") == 3, (
        f"expected 3 `@blocked:review` codespans in TASKS.md, got "
        f"{raw.count('`@blocked:review`')} — TASKS.md was:\n{raw}"
    )

    # 5. Auto-promotion's `_is_blocker_satisfied('review')` returns
    # False for every proposal, so `next_dispatchable('Backlog')`
    # returns None — none of these would dispatch on the next tick.
    assert board.next_dispatchable("Backlog") is None, (
        "a review-gated proposal slipped past next_dispatchable — "
        "the gate isn't being honored end-to-end"
    )

    # 6. Sanity: the prompt the daemon dumped to disk genuinely
    # contained the TB-121 gate directive. Without this the test
    # could pass purely on the fake-agent's good behavior; the dump
    # check proves the prompt-change shipped to the agent.
    debug_dir = cfg.project_root / ".cc-autopilot" / "debug"
    prompt_dumps = sorted(debug_dir.glob("*-ideation.prompt.md"))
    assert prompt_dumps, (
        f"no ideation prompt dump under {debug_dir} — the daemon should "
        f"have written one before invoking the SDK"
    )
    dumped = prompt_dumps[-1].read_text().lower()
    assert "blocked on: review" in dumped, (
        "ideation prompt dump is missing the TB-121 gate directive — "
        "the prompt change didn't reach the agent"
    )
