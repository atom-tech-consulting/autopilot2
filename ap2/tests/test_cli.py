"""Tests for the ap2 CLI subcommands (TB-77, TB-79).

Lightweight unit tests that call cmd_* directly with an argparse.Namespace
rather than spawning a subprocess.

TB-131: cmd_backlog / cmd_unfreeze / cmd_delete / cmd_add now stage their
work through `.cc-autopilot/operator_queue.jsonl` instead of mutating
TASKS.md synchronously. Tests use `_drain` to apply the queue exactly as
the daemon's `_tick` first stage would, so the post-state assertions
that follow are unchanged.
"""
from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, retry, tools
from ap2.board import Board
from ap2.cron import load_jobs
from ap2.cli import (
    _require_oauth_token,
    cmd_add,
    cmd_backlog,
    cmd_delete,
    cmd_ideate,
    cmd_reject,
    cmd_start,
    cmd_unfreeze,
)
from ap2.config import Config
from ap2.init import init_project
from ap2.tests._briefing_fixtures import (
    briefing_missing,
    canonical_briefing,
)


def _project(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drain(cfg: Config) -> dict:
    """Apply pending operator-queue ops as the daemon's `_tick` would.

    Tests that exercise cmd_backlog / cmd_unfreeze / cmd_delete / cmd_add
    use this to advance from "queued" to "applied" state — the CLI
    commands themselves are deferred (TB-131).
    """
    return tools.drain_operator_queue(cfg)


def test_backlog_moves_from_frozen(tmp_path: Path):
    """Replaces what `cmd_skip` used to do: move-to-Backlog from any
    section, including Frozen."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-50", title="legacy frozen")
    board.save()

    rc = cmd_backlog(cfg, Namespace(task_id="TB-50"))
    assert rc == 0
    _drain(cfg)

    board2 = Board.load(cfg.tasks_file)
    assert board2.find("TB-50")[0] == "Backlog"


def test_backlog_moves_from_active(tmp_path: Path):
    """Same path also covers Active → Backlog (the original `skip` use case)."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-51", title="stuck active")
    board.save()

    rc = cmd_backlog(cfg, Namespace(task_id="TB-51"))
    assert rc == 0
    _drain(cfg)
    assert Board.load(cfg.tasks_file).find("TB-51")[0] == "Backlog"


def test_backlog_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_backlog(cfg, Namespace(task_id="TB-999"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not on board" in err


def test_unfreeze_moves_from_frozen_to_backlog(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-60", title="bug-frozen task")
    board.save()

    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-60"))
    assert rc == 0
    _drain(cfg)

    board2 = Board.load(cfg.tasks_file)
    assert board2.find("TB-60")[0] == "Backlog"


def test_unfreeze_clears_retry_state(tmp_path: Path):
    """The whole point of `unfreeze` over `backlog` is fresh retry budget.
    Without this, the next failure pushes the task straight back to Frozen."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-61", title="had retries")
    board.save()
    # Simulate the retry-exhausted state that Frozen tasks come from.
    retry.bump_attempt(cfg.retry_state_file, "TB-61")
    retry.bump_attempt(cfg.retry_state_file, "TB-61")
    retry.bump_attempt(cfg.retry_state_file, "TB-61")
    assert retry.attempt_count(cfg.retry_state_file, "TB-61") == 3

    cmd_unfreeze(cfg, Namespace(task_id="TB-61"))
    _drain(cfg)

    assert retry.attempt_count(cfg.retry_state_file, "TB-61") == 0


def test_unfreeze_emits_audit_event(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-62", title="audited unfreeze")
    board.save()

    cmd_unfreeze(cfg, Namespace(task_id="TB-62"))
    _drain(cfg)

    evts = events.tail(cfg.events_file, 5)
    unfrozen = [e for e in evts if e["type"] == "task_unfrozen"]
    assert len(unfrozen) == 1
    assert unfrozen[0]["task"] == "TB-62"


def test_unfreeze_refuses_non_frozen(tmp_path: Path, capsys):
    """The validation + move happens inside `locked_board()`; refusing on
    non-Frozen is also where the `backlog` command should be used instead.
    """
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-70", title="already backlog")
    board.save()

    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-70"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "not Frozen" in err
    assert "ap2 backlog" in err  # nudge to the right command
    # Task didn't move.
    assert Board.load(cfg.tasks_file).find("TB-70")[0] == "Backlog"


def test_unfreeze_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_unfreeze(cfg, Namespace(task_id="TB-999"))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_start oauth-token precondition (TB-79)


def test_require_oauth_token_passes_when_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-fake")
    assert _require_oauth_token() == 0


def test_require_oauth_token_refuses_when_unset(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    rc = _require_oauth_token()
    assert rc == 1
    err = capsys.readouterr().err
    assert "CLAUDE_CODE_OAUTH_TOKEN" in err
    # Operator-side remediation hints surfaced in the message.
    assert "sudo -u" in err
    assert "install-token" in err


def test_require_oauth_token_refuses_when_blank(monkeypatch):
    """Whitespace-only token = absent (the SDK would still fail). Refuse."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "   ")
    assert _require_oauth_token() == 1


def test_cmd_start_refuses_without_token(tmp_path: Path, monkeypatch, capsys):
    """End-to-end: cmd_start exits 1 + does NOT spawn a subprocess when
    the token is missing. Pinned via subprocess.Popen monkeypatch raising
    if called — the precondition must short-circuit before fork."""
    cfg = _project(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    # Sentinel: if Popen ever runs, fail loudly.
    import subprocess as _sp
    def boom(*a, **kw):
        raise AssertionError("Popen called despite missing token — precondition is broken")
    monkeypatch.setattr(_sp, "Popen", boom)

    rc = cmd_start(cfg, Namespace(foreground=False))
    assert rc == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_delete (TB-107)


def test_delete_removes_from_frozen(tmp_path: Path):
    """Primary use case: abandon a Frozen task that's been superseded.
    Ideation surfaces these in the decisions-needed list (TB-191)."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-91", title="superseded")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-91", force=False))
    assert rc == 0
    _drain(cfg)
    # Task is gone from the board entirely.
    assert Board.load(cfg.tasks_file).find("TB-91") is None


def test_delete_removes_from_backlog(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-80", title="never mind")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-80", force=False))
    assert rc == 0
    _drain(cfg)
    assert Board.load(cfg.tasks_file).find("TB-80") is None


def test_delete_refuses_active_without_force(tmp_path: Path, capsys):
    """Active means in-flight; deleting could orphan the SDK subprocess
    or break orphan recovery. Default refusal."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-50", title="running now")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-50", force=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Active" in err
    assert "--force" in err
    # Task untouched.
    assert Board.load(cfg.tasks_file).find("TB-50")[0] == "Active"


def test_delete_refuses_ready_without_force(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Ready", task_id="TB-51", title="next-up")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-51", force=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Ready" in err
    assert "ap2 backlog" in err  # nudge to the right alternative
    assert Board.load(cfg.tasks_file).find("TB-51")[0] == "Ready"


def test_delete_force_allows_active(tmp_path: Path):
    """--force overrides the Active/Ready safety. Use case: stale Active
    line left by a daemon crash, where the operator knows the task isn't
    really running."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-52", title="actually dead")
    board.save()

    rc = cmd_delete(cfg, Namespace(task_id="TB-52", force=True))
    assert rc == 0
    _drain(cfg)
    assert Board.load(cfg.tasks_file).find("TB-52") is None


def test_delete_emits_audit_event(tmp_path: Path):
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Frozen", task_id="TB-92", title="auditable delete")
    board.save()

    cmd_delete(cfg, Namespace(task_id="TB-92", force=False))
    _drain(cfg)

    evts = events.tail(cfg.events_file, 5)
    deleted = [e for e in evts if e["type"] == "task_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["task"] == "TB-92"
    assert deleted[0]["section"] == "Frozen"
    assert deleted[0]["title"] == "auditable delete"


def test_delete_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_delete(cfg, Namespace(task_id="TB-999", force=False))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_reject (TB-152) — explicit rejection of an ideation-proposed task,
# with a reason captured to operator_log.md so ideation Step 0 has a
# signal to avoid re-proposing the same idea next cycle. Pre-validation
# limits the verb to Backlog tasks still gated by `@blocked:review`;
# anything else gets routed at `ap2 delete`.


def _seed_proposal(cfg: Config, task_id: str, title: str = "an ideation proposal") -> None:
    """Synthesize a Backlog task with the `@blocked:review` codespan —
    the canonical "ideation proposal awaiting operator decision" shape."""
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id=task_id,
        title=title,
        meta={"blocked": "review"},
    )
    board.save()


def test_reject_end_to_end_writes_reason_to_operator_log(tmp_path: Path):
    """Briefing-spec verification: synthesize a Backlog task with
    `@blocked:review`, run cmd_reject with a reason, drain the queue, and
    assert (a) TASKS.md no longer contains the row, (b) the briefing file
    is gone, AND (c) operator_log.md contains the rejected-proposal line
    with the supplied reason text — not just the action verb."""
    cfg = _project(tmp_path)
    # Stage a real briefing file so we can pin the unlink behavior.
    briefing_path = cfg.tasks_dir / "the-proposal.md"
    briefing_path.parent.mkdir(parents=True, exist_ok=True)
    briefing_path.write_text("# stub briefing\n")
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog",
        task_id="TB-810",
        title="redundant idea",
        meta={"blocked": "review"},
        briefing=str(briefing_path.relative_to(cfg.project_root)),
    )
    board.save()

    rc = cmd_reject(
        cfg,
        Namespace(task_id="TB-810", reason="duplicates TB-700, no incremental signal"),
    )
    assert rc == 0
    _drain(cfg)

    # (a) Row is gone from TASKS.md.
    assert Board.load(cfg.tasks_file).find("TB-810") is None
    # (b) Briefing file is gone.
    assert not briefing_path.exists()
    # (c) operator_log.md carries the rejected-proposal line WITH the reason
    # — not just the bare action verb. Both "rejected ideation proposal"
    # and the supplied reason text must be in the log.
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "rejected ideation proposal" in log
    assert "TB-810" in log
    assert "redundant idea" in log  # title preserved in the audit line
    assert "duplicates TB-700, no incremental signal" in log


def test_reject_without_reason_records_placeholder(tmp_path: Path):
    """A reject with `--reason` omitted records `(no reason given)` —
    itself a signal ideation can spot in operator_log.md."""
    cfg = _project(tmp_path)
    _seed_proposal(cfg, "TB-811", title="quiet rejection")

    rc = cmd_reject(cfg, Namespace(task_id="TB-811", reason=None))
    assert rc == 0
    _drain(cfg)

    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "rejected ideation proposal" in log
    assert "TB-811" in log
    assert "(no reason given)" in log


def test_reject_refuses_non_backlog_task(tmp_path: Path, capsys):
    """Pre-validation: cmd_reject refuses to act on Active tasks (not an
    ideation proposal anymore — a running task with `@blocked:review`
    structurally couldn't dispatch, but the verb still belongs to
    `delete`'s lane). The error message points the operator at
    `ap2 delete`."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Active",
        task_id="TB-820",
        title="running",
        meta={"blocked": "review"},
    )
    board.save()

    rc = cmd_reject(cfg, Namespace(task_id="TB-820", reason="nope"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a pending-review proposal" in err or "pending-review" in err
    assert "ap2 delete" in err
    # Task untouched.
    assert Board.load(cfg.tasks_file).find("TB-820")[0] == "Active"


def test_reject_refuses_already_approved_proposal(tmp_path: Path, capsys):
    """Pre-validation: a Backlog task without `@blocked:review` (i.e.
    operator already approved it, or it never had the review gate) is
    not a pending-review proposal — refuse and route the operator at
    `ap2 delete`."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id="TB-821", title="already approved")
    board.save()

    rc = cmd_reject(cfg, Namespace(task_id="TB-821", reason="changed mind"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "ap2 delete" in err
    # Task untouched.
    assert Board.load(cfg.tasks_file).find("TB-821")[0] == "Backlog"


def test_reject_unknown_task_returns_error(tmp_path: Path, capsys):
    cfg = _project(tmp_path)
    rc = cmd_reject(cfg, Namespace(task_id="TB-9999", reason="x"))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err


def test_reject_emits_task_deleted_event(tmp_path: Path):
    """Drain emits `task_deleted` so the audit-event surface stays
    grep-able by the same event type as `delete` (the verb-vs-`delete`
    distinction is carried by the operator_log.md line shape, not the
    event type)."""
    cfg = _project(tmp_path)
    _seed_proposal(cfg, "TB-830", title="eventful reject")

    cmd_reject(cfg, Namespace(task_id="TB-830", reason="overlaps TB-799"))
    _drain(cfg)

    evts = events.tail(cfg.events_file, 10)
    deleted = [e for e in evts if e["type"] == "task_deleted"]
    assert any(d["task"] == "TB-830" for d in deleted)


# ---------------------------------------------------------------------------
# TB-121: `ap2 status` shows the pending-review queue depth so an
# operator can spot ideation proposals waiting on `ap2 approve` without
# having to load /tasks?filter=pending-review.

def test_status_shows_pending_review_count(tmp_path: Path, capsys):
    """When N>0 pending-review tasks exist, status emits a `review:` line
    naming the count and the action (`ap2 approve TB-N`).

    TB-151: the line also names the actual TB-Ns (`test_status_lists_
    pending_review_ids` below pins the ID-listing + truncation contract);
    here we only assert the count + action survive."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-700", title="prop a",
        meta={"blocked": "review"},
    )
    board.add(
        "Backlog", task_id="TB-701", title="prop b",
        meta={"blocked": "review"},
    )
    board.save()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "review:" in out
    assert "2 pending" in out
    assert "ap2 approve" in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["pending_review"] == 2


def test_status_omits_pending_review_when_zero(tmp_path: Path, capsys):
    """A clean board doesn't grow a `review: 0 pending` noise line. The
    json output still carries `pending_review`: 0 for machine-parseability."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "review:" not in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["pending_review"] == 0
    # TB-151: machine-readable list parallels the count.
    assert payload["pending_review_ids"] == []


# ---------------------------------------------------------------------------
# TB-151: surface the pending-review TB-Ns themselves (not just the count)
# in `ap2 status` text + JSON, with a 5-ID truncation rule. Operators were
# having to grep TASKS.md to figure out which TB-Ns to pass to
# `ap2 approve`.

def test_status_lists_pending_review_ids(tmp_path: Path, capsys):
    """3 review-gated tasks → the `review:` line names all 3 TB-Ns
    (under the 5-ID truncation cap) and the JSON branch carries the
    same list under `pending_review_ids`."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    for i, tid in enumerate(("TB-800", "TB-801", "TB-802")):
        board.add(
            "Backlog", task_id=tid, title=f"prop {i}",
            meta={"blocked": "review"},
        )
    board.save()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # The actual IDs land on the `review:` line — operator can copy any
    # of them straight into `ap2 approve TB-N`.
    assert "TB-800" in out
    assert "TB-801" in out
    assert "TB-802" in out
    # No truncation suffix when N <= 5.
    assert "more)" not in out
    # Action hint survives.
    assert "ap2 approve" in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["pending_review"] == 3
    assert payload["pending_review_ids"] == ["TB-800", "TB-801", "TB-802"]


def test_status_truncates_pending_review_ids_after_five(tmp_path: Path, capsys):
    """6 review-gated tasks → the text line names the first 5 TB-Ns
    with a "(+1 more)" suffix; the JSON branch carries all 6 unmolested
    so machine consumers don't lose data to a presentation cap."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    ids = [f"TB-{n}" for n in range(900, 906)]  # TB-900 .. TB-905
    for i, tid in enumerate(ids):
        board.add(
            "Backlog", task_id=tid, title=f"prop {i}",
            meta={"blocked": "review"},
        )
    board.save()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # First 5 IDs visible.
    for tid in ids[:5]:
        assert tid in out
    # 6th ID dropped from the text rendering — replaced by the suffix.
    assert "TB-905" not in out
    assert "(+1 more)" in out
    # Count still reflects the full N=6.
    assert "6 pending" in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    # JSON branch keeps the full list — truncation is presentation-only.
    assert payload["pending_review_ids"] == ids
    assert payload["pending_review"] == 6


# ---------------------------------------------------------------------------
# TB-187: mixed-blocker pending-review surfacing.
#
# A task with `@blocked:review,TB-X` was hidden from the `review:` line
# pre-fix because the strict `all(b == "review" for b in blocked_on)`
# filter excluded any task carrying a non-review blocker too. The fix
# loosens the filter to `any(...)` — the operator still needs to
# approve, the auto-dispatch gate (`_is_dispatchable`) is unchanged.

def test_status_includes_mixed_blocker_in_pending_review(
    tmp_path: Path, capsys,
):
    """Three Backlog tasks: pure review, mixed review+TB-X, pure TB-X.
    The `review:` line names the first two; the third is excluded.
    Pre-TB-187 only the first appeared. The JSON branch carries the
    same list under `pending_review_ids` (machine consumers also need
    the mixed-blocker IDs)."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-880", title="pure review",
        meta={"blocked": "review"},
    )
    board.add(
        "Backlog", task_id="TB-881", title="mixed review and TB-99",
        meta={"blocked": "review,TB-99"},
    )
    board.add(
        "Backlog", task_id="TB-882", title="pure TB-99 dep",
        meta={"blocked": "TB-99"},
    )
    board.save()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # Both review-bearing IDs land on the `review:` line.
    assert "review:" in out
    assert "TB-880" in out
    assert "TB-881" in out
    # The pure-TB-99 case stays out of the surfacing — `review` is not
    # among its blockers.
    review_line = next(
        (ln for ln in out.splitlines() if ln.startswith("review:")),
        "",
    )
    assert "TB-882" not in review_line
    # Count reflects the loose-predicate semantics (2, not 1).
    assert "2 pending" in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["pending_review"] == 2
    assert set(payload["pending_review_ids"]) == {"TB-880", "TB-881"}


# --------- TB-173 / TB-191: `ap2 status` surfaces ideator decisions ---------
#
# `parse_operator_decisions` reads the `## Decisions needed from operator`
# section from `.cc-autopilot/ideation_state.md` (renamed from the
# pre-TB-191 `## Open questions for operator`). The CLI text branch
# renders a "decisions needed (N): ..." line truncated to the first 5
# with a "(+M more)" suffix; the JSON branch carries the full helper
# output under `operator_decisions`. When the file or section is
# absent, both branches stay quiet — the line is omitted from text
# entirely, and JSON carries the empty list.
#
# TB-191 also added the agent-internal `## Cycle observations` section
# that MUST NOT leak to operator-facing surfaces; the test at the end
# of this block pins that the CLI never surfaces observations content
# even when both sections coexist in the file.


def _seed_ideation_state(cfg: Config, body: str) -> None:
    """Write `body` to `.cc-autopilot/ideation_state.md` so `cmd_status`
    can pick it up via `parse_operator_decisions`."""
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_cmd_status_renders_operator_decisions_when_present(
    tmp_path: Path, capsys,
):
    """3 decisions in the file → text-mode `ap2 status` includes a
    line beginning with "decisions needed" naming the count and joining
    the bullets with "; ". Verifies the line is wired into the CLI
    rendering path at all."""
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    _seed_ideation_state(
        cfg,
        "## Decisions needed from operator\n\n"
        "- Decision needed: should goal.md declare a new focus?\n"
        "- Approve or reject TB-171 / TB-172 / TB-173.\n"
        "- Operator input required: rotate focus item?\n",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # Line shape: "decisions needed (3): bullet; bullet; bullet"
    line = next(
        (ln for ln in out.splitlines() if ln.startswith("decisions needed")),
        None,
    )
    assert line is not None, f"no decisions-needed line in status output:\n{out}"
    assert "(3):" in line
    assert "Decision needed: should goal.md declare a new focus?" in line
    assert "Approve or reject TB-171 / TB-172 / TB-173." in line
    assert "Operator input required: rotate focus item?" in line


def test_cmd_status_json_carries_full_operator_decisions_list(
    tmp_path: Path, capsys,
):
    """JSON-mode `ap2 status --json` carries an `operator_decisions` key
    with the full bullet list (untruncated by the CLI's 5-cap
    presentation rule). Machine consumers see exactly what
    `parse_operator_decisions` returned."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    _seed_ideation_state(
        cfg,
        "## Decisions needed from operator\n\n"
        "- First?\n- Second?\n- Third?\n",
    )

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["operator_decisions"] == ["First?", "Second?", "Third?"]


def test_cmd_status_omits_operator_decisions_line_when_absent(
    tmp_path: Path, capsys,
):
    """No `ideation_state.md` file (fresh project) or empty section →
    text branch must not grow a noisy "0 decisions needed" line, and
    JSON carries the empty list. Mirrors TB-121's omit-on-zero shape
    for pending-review."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    # No ideation_state.md created — the helper returns [].

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "decisions needed" not in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["operator_decisions"] == []


def test_cmd_status_truncates_operator_decisions_in_text_to_five(
    tmp_path: Path, capsys,
):
    """When the helper returns more than 5 entries, the text branch shows
    the first 5 (per-bullet truncated) with a "(+M more)" tail; JSON
    keeps the full list (capped at 7+1 by `parse_operator_decisions`
    itself). Pins the CLI's presentation cap independently of the
    helper's cap."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    bullets = "\n".join(
        f"- decision {i} text body here?"
        for i in range(1, 7)  # 6 bullets — under helper cap, over CLI cap
    )
    _seed_ideation_state(
        cfg,
        f"## Decisions needed from operator\n\n{bullets}\n",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    line = next(
        ln for ln in out.splitlines() if ln.startswith("decisions needed")
    )
    assert "(6):" in line
    # First 5 bullets named.
    for i in range(1, 6):
        assert f"decision {i} text body here?" in line
    # 6th truncated out of the text rendering — replaced by suffix.
    assert "decision 6 text body here?" not in line
    assert "(+1 more)" in line

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    # JSON branch carries the full helper output untouched (6 entries,
    # under the helper's 7-cap so no synthetic trailer is appended).
    assert len(payload["operator_decisions"]) == 6


def test_cmd_status_does_not_leak_cycle_observations(
    tmp_path: Path, capsys,
):
    """TB-191: when `ideation_state.md` carries BOTH `## Decisions
    needed from operator` (with two valid bullets) AND
    `## Cycle observations` (with three observation-shaped bullets),
    `ap2 status` text output must surface ONLY the decisions content
    and NEVER any line referencing the cycle-observations bullets.
    The agent-internal observations section is structurally excluded
    by `parse_operator_decisions` — this test proves the structural
    exclusion lands at the CLI surface, not just at the parser."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    _seed_ideation_state(
        cfg,
        "# Ideation State\n\n"
        "## Cycle observations\n\n"
        "- n=3 retries on bullet kind Y this week.\n"
        "- No unadopted cron_proposed events.\n"
        "- Cadence is steady at 12 ticks/min.\n\n"
        "## Decisions needed from operator\n\n"
        "- Decision needed: approve TB-200?\n"
        "- Operator input required: rotate focus to verifier robustness?\n",
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    # The decisions surface line is present with the right count.
    line = next(
        (ln for ln in out.splitlines() if ln.startswith("decisions needed")),
        None,
    )
    assert line is not None
    assert "(2):" in line
    assert "Decision needed: approve TB-200?" in line
    assert "Operator input required: rotate focus to verifier robustness?" in line
    # None of the observations content leaks into the CLI output.
    for forbidden in (
        "n=3 retries on bullet kind Y",
        "No unadopted cron_proposed events",
        "Cadence is steady",
    ):
        assert forbidden not in out

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    # JSON also carries only the decisions bullets.
    assert payload["operator_decisions"] == [
        "Decision needed: approve TB-200?",
        "Operator input required: rotate focus to verifier robustness?",
    ]


# --------- TB-130: `ap2 status` reports the bundled web URL ---------


def test_status_prints_web_url_when_running(tmp_path: Path, monkeypatch, capsys):
    """When the daemon is running and the web UI wasn't disabled, status
    prints the URL operators should point a browser at. Uses the same env
    resolution as the daemon — `AP2_WEB_PORT` overrides — so what's
    printed matches what the daemon is actually serving."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    # Fake "daemon is running" by writing the current pid into the pid file
    # (`_is_running` just os.kill(pid, 0)s; our own pid is alive).
    cfg.pid_file.write_text(str(os.getpid()))
    monkeypatch.setenv("AP2_WEB_PORT", "9123")
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "web:" in out
    assert "http://127.0.0.1:9123/" in out

    # JSON variant carries the URL under `web_url`.
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["web_url"] == "http://127.0.0.1:9123/"


def test_status_omits_web_url_when_disabled(tmp_path: Path, monkeypatch, capsys):
    """`AP2_WEB_DISABLED=1` — operator opted out of the bundled UI for
    this daemon — so status must not print a URL the operator can't
    actually reach. Covers the headless / CI path."""
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    cfg.pid_file.write_text(str(os.getpid()))
    monkeypatch.setenv("AP2_WEB_DISABLED", "1")

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "web:" not in out


def test_status_omits_web_url_when_daemon_stopped(tmp_path: Path, monkeypatch, capsys):
    """No daemon running → no daemon-spawned web UI → no URL. Avoids the
    misleading case where status prints a URL but nothing is listening
    because the operator stopped the daemon (or it crashed)."""
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    # No pid file — daemon not running.
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "web:" not in out


def test_status_prints_web_url_from_web_start_event(
    tmp_path: Path, monkeypatch, capsys,
):
    """TB-155: `cmd_status` reads the most recent `web_start` event from
    `events.jsonl` so the printed URL reflects the auto-enumerated port
    (e.g. 8731 when 8729 was busy at daemon start). Pre-TB-155 the URL
    came from `AP2_WEB_PORT` env, which doesn't reflect the actual bind
    after enumeration — the operator could click a URL pointing at
    nothing.

    Setup: pre-seed events.jsonl with `web_start` carrying port 8731 and
    `requested_port` 8729; set `AP2_WEB_PORT=9999` (a different port to
    prove env is NOT consulted). Status must print `:8731`, not `:9999`.
    """
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    cfg.pid_file.write_text(str(os.getpid()))
    # Env knob points at a port we did NOT bind — proves we read the
    # event log, not env, post-TB-155.
    monkeypatch.setenv("AP2_WEB_PORT", "9999")
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)
    events.append(
        cfg.events_file, "web_start",
        host="127.0.0.1", port=8731, url="http://127.0.0.1:8731/",
        requested_port=8729,
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "web:" in out
    assert "http://127.0.0.1:8731/" in out, out
    # Belt-and-suspenders: env-derived URL must NOT bleed through.
    assert ":9999" not in out, out


def test_status_falls_back_to_env_when_no_web_start_event(
    tmp_path: Path, monkeypatch, capsys,
):
    """Compatibility safety net: if the daemon's `web_start` hasn't been
    written yet (brief window between `ap2 start` and the first bind, or
    older events.jsonl predating TB-130 wiring), fall back to env-derived
    resolution. Otherwise `cmd_status` would silently swallow the URL
    line during normal operation right after a daemon restart."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    cfg.pid_file.write_text(str(os.getpid()))
    monkeypatch.setenv("AP2_WEB_PORT", "9123")
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)
    # Seed an unrelated event so events.jsonl exists but has no `web_start`.
    events.append(cfg.events_file, "daemon_start")

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["web_url"] == "http://127.0.0.1:9123/"


def test_status_prefers_recent_web_start_over_older_one(
    tmp_path: Path, monkeypatch, capsys,
):
    """When the daemon has restarted (e.g. operator killed and
    re-started), events.jsonl contains multiple `web_start` events. Status
    must reflect the MOST RECENT one — otherwise a URL from a previous
    daemon lifecycle (different port, possibly different enumeration)
    bleeds through and confuses the operator."""
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    cfg.pid_file.write_text(str(os.getpid()))
    monkeypatch.delenv("AP2_WEB_DISABLED", raising=False)

    # Older lifecycle: bound 8729, then stopped.
    events.append(
        cfg.events_file, "web_start",
        host="127.0.0.1", port=8729, url="http://127.0.0.1:8729/",
    )
    events.append(cfg.events_file, "web_stop", host="127.0.0.1", port=8729)
    # Current lifecycle: enumerated to 8730 because someone else grabbed 8729.
    events.append(
        cfg.events_file, "web_start",
        host="127.0.0.1", port=8730, url="http://127.0.0.1:8730/",
        requested_port=8729,
    )

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8730/" in out, out


# ---------------------------------------------------------------------------
# TB-135: ap2 add requires --briefing-file. Title and tags are parsed from
# the briefing's H1 and an optional `Tags:` line; -t/-d are repurposed (-t
# extends the briefing's tags; -d is dropped — descriptions live in the
# briefing). Pre-TB-135 a missing --briefing-file silently auto-filled a
# skeleton whose `## Verification` had only a placeholder bullet, so the
# per-task verifier "passed" tasks on regression-gate alone (TB-131 hit
# this on 2026-04-30). Test that authoring is now mandatory and that the
# happy-path round-trips the briefing bytes onto disk.


def _add_args(
    section: str = "Backlog",
    tags: list[str] | None = None,
    briefing_file: str | None = None,
    no_verify: bool = False,
    blocked: str | None = None,
    skip_goal_alignment: bool = False,
) -> Namespace:
    """Build a Namespace shaped like cmd_add's argparse output.

    TB-135: the positional `title`, `-d/--description` are gone — title /
    description live in the briefing. `_add_args` no longer accepts them.
    TB-132: `--blocked CSV` writes a `@blocked:<csv>` codespan onto the
    rendered task line.
    TB-170: `--skip-goal-alignment` bypasses the TB-161 + TB-164 goal-
    alignment checks for operator-driven exceptions.
    """
    return Namespace(
        section=section,
        tags=tags,
        briefing_file=briefing_file,
        no_verify=no_verify,
        blocked=blocked,
        skip_goal_alignment=skip_goal_alignment,
    )


_GOOD_BRIEFING = (
    "# Add foo helper\n\n"
    "Tags: #cli #helpers\n\n"
    "## Goal\n\nReal goal text.\n\n"
    "Why now: closes the missing-helper failure mode TB-X named.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nStraightforward add.\n\n"
    "## Verification\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


def test_add_requires_briefing_file(tmp_path: Path, monkeypatch, capsys):
    """TB-135 verification: `ap2 add` without `--briefing-file` AND with no
    `$EDITOR` set exits non-zero with a clear usage hint pointing at where
    to find the canonical template. Nothing is queued; nothing lands in
    TASKS.md.

    EDITOR is explicitly unset so this test exercises the
    no-briefing-no-editor path; the editor-driven flow has its own tests
    below.
    """
    cfg = _project(tmp_path)
    monkeypatch.delenv("EDITOR", raising=False)
    before = cfg.tasks_file.read_text()

    rc = cmd_add(cfg, _add_args(briefing_file=None))

    assert rc == 1
    err = capsys.readouterr().err
    # Hint mentions both the flag and where to find the template.
    assert "--briefing-file" in err
    assert "BRIEFING_TEMPLATE" in err or "init.py" in err
    # Nothing landed.
    assert cfg.tasks_file.read_text() == before
    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    assert not queue.exists() or queue.read_text() == ""


def test_add_with_briefing_file_succeeds(tmp_path: Path):
    """Happy path: `ap2 add --briefing-file <path>` allocates a TB-N,
    queues the add, and (after the daemon's drain) lands a task line
    whose `[→ brief](...)` points at the briefing on disk. Briefing
    bytes round-trip into .cc-autopilot/tasks/<slug>.md."""
    cfg = _project(tmp_path)
    brief = tmp_path / "input-briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))

    assert rc == 0
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    # H1 sets the title.
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None, list(board.iter_tasks())
    # Briefing link present, pointing under .cc-autopilot/tasks/.
    assert found.briefing is not None
    assert ".cc-autopilot/tasks/" in found.briefing
    # Briefing bytes landed on disk verbatim.
    target = cfg.project_root / found.briefing
    assert target.exists()
    assert target.read_text() == _GOOD_BRIEFING
    # Tags: line in briefing → tags on the task line (lower-cased,
    # `#`-prefixed). The `_GOOD_BRIEFING` carries `#cli #helpers`.
    assert "#cli" in found.tags
    assert "#helpers" in found.tags


def test_add_with_briefing_file_stdin(tmp_path: Path, monkeypatch):
    """`ap2 add --briefing-file -` reads the briefing from stdin and
    behaves identically to the file path. Operator-flow case: piping a
    here-doc into the CLI without leaving a file behind."""
    import io

    cfg = _project(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(_GOOD_BRIEFING))

    rc = cmd_add(cfg, _add_args(briefing_file="-"))

    assert rc == 0
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    assert found.briefing is not None
    target = cfg.project_root / found.briefing
    assert target.read_text() == _GOOD_BRIEFING


def test_add_rejects_briefing_file_without_h1(tmp_path: Path, capsys):
    """No H1 → no title can be derived → refuse. The error points at
    H1 specifically so the operator can fix the briefing."""
    cfg = _project(tmp_path)
    brief = tmp_path / "no-h1.md"
    brief.write_text("Just some prose, no heading.\n\n## Verification\n- `pytest`\n")

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))

    assert rc == 1
    err = capsys.readouterr().err
    assert "H1" in err or "title" in err.lower()


def test_add_rejects_empty_briefing_file(tmp_path: Path, capsys):
    """Empty briefing means no `## Verification` either — verifier would
    have nothing to score. Refuse, don't fall back to a skeleton."""
    cfg = _project(tmp_path)
    brief = tmp_path / "empty.md"
    brief.write_text("")

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))

    assert rc == 1
    err = capsys.readouterr().err
    assert "empty" in err.lower()


def test_add_strips_tbn_prefix_from_h1(tmp_path: Path):
    """Briefings on disk often carry `# TB-N — Title` once the daemon's
    prep step has stamped them. A re-add (e.g. operator copies a frozen
    briefing into a new add) must not bake the prior id into the new
    task's title — strip the `TB-N — ` prefix on parse."""
    cfg = _project(tmp_path)
    brief = tmp_path / "prefixed.md"
    brief.write_text(canonical_briefing("TB-99", title="Real title here"))

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))

    assert rc == 0
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    titles = [t.title for t in board.iter_tasks()]
    assert "Real title here" in titles
    # No `TB-99` substring leaked through.
    assert not any("TB-99" in t for t in titles)


def test_add_extends_briefing_tags_with_flag(tmp_path: Path):
    """`-t` is repurposed (TB-135) as an APPEND of extra tags on top of
    those parsed from the briefing's `Tags:` line. Both sources land on
    the rendered task line; duplicates are deduped."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)  # Tags: #cli #helpers

    rc = cmd_add(
        cfg,
        _add_args(briefing_file=str(brief), tags=["#extra", "#cli"]),
    )

    assert rc == 0
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    # Tags from the briefing AND the flag both present.
    assert "#cli" in found.tags
    assert "#helpers" in found.tags
    assert "#extra" in found.tags
    # `#cli` not duplicated.
    assert found.tags.count("#cli") == 1


def test_add_with_blocked_writes_codespan_not_description(tmp_path: Path):
    """TB-132: `ap2 add --blocked TB-5,review` writes a `@blocked:` codespan
    on the rendered task line and leaves the description prose untouched.
    The legacy `(blocked on: ...)` description-injection path is gone.
    """
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(
        cfg,
        _add_args(briefing_file=str(brief), blocked="TB-5,review"),
    )

    assert rc == 0
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    # Codespan landed in meta and survives Task.blocked_on parsing.
    assert found.meta.get("blocked") == "TB-5,review"
    assert found.blocked_on == ["TB-5", "review"]
    # The rendered task line has the codespan after tags, before the
    # em-dash — round-trip-readable for the next parse.
    raw_line = next(
        (line for line in cfg.tasks_file.read_text().splitlines()
         if found.id in line),
        "",
    )
    assert "`@blocked:TB-5,review`" in raw_line
    # Description prose is NOT carrying the legacy clause.
    assert "blocked on" not in (found.description or "").lower()
    assert "(blocked on:" not in raw_line


def test_add_rejects_newline_in_blocked_flag(tmp_path: Path, capsys):
    """TB-134 carry-forward, TB-132: a `--blocked` value with embedded
    newlines breaks TASK_LINE_RE same as a multi-line tag would. Reject
    with the same single-line error so the `@blocked:` codespan stays a
    single line on the rendered task."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(
        cfg,
        _add_args(briefing_file=str(brief), blocked="TB-5\nreview"),
    )

    assert rc == 1
    assert "single line" in capsys.readouterr().err


def test_add_rejects_newline_in_tag_flag(tmp_path: Path, capsys):
    """TB-134 carry-forward: a `--tags` value with embedded newlines
    breaks TASK_LINE_RE. Reject up-front with the same single-line
    error."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(
        cfg,
        _add_args(briefing_file=str(brief), tags=["#cli", "#bro\nken"]),
    )

    assert rc == 1
    assert "single line" in capsys.readouterr().err


def test_add_rejects_asterisk_in_title(tmp_path: Path, capsys):
    """TB-216: a briefing H1 containing `*` would collapse TASK_LINE_RE's
    bold-fence title group on drain (parsed via TASK_LINE_RE), so the
    rendered task lands in `Board.malformed_lines` and operator-queue
    verbs (`approve` / `update` / `delete`) can no longer address it.
    Reproduced live on TB-214 (`Pin 4 sandbox install-* CLI verbs`).
    The CLI path (`ap2 add`) forwards the H1 verbatim into
    `do_operator_queue_append({title: ...})`, which calls
    `_validate_single_line("title", ...)` and now refuses `*`. The
    CLI surfaces the error to stderr and exits non-zero; nothing
    lands on TASKS.md or in operator_queue.jsonl."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    # H1 carries `*` — `_parse_briefing_metadata` forwards it verbatim.
    brief.write_text(
        _GOOD_BRIEFING.replace("# Add foo helper", "# install-* helpers"),
    )
    before_tasks = cfg.tasks_file.read_text()
    queue_path = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    before_queue = queue_path.read_text() if queue_path.exists() else ""

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))

    assert rc == 1
    err = capsys.readouterr().err
    assert "*" in err
    assert "TASK_LINE_RE" in err or "bold-fence" in err
    # Nothing landed on the board, nothing queued.
    assert cfg.tasks_file.read_text() == before_tasks
    after_queue = queue_path.read_text() if queue_path.exists() else ""
    assert after_queue == before_queue


# ---------------------------------------------------------------------------
# TB-135: editor-driven authoring fallback. When `--briefing-file` isn't
# supplied AND `$EDITOR` is set, `ap2 add` opens the editor against the
# template and uses the saved buffer as the briefing — git-commit-style.
# Aborting (empty save, unchanged template, or non-zero exit) makes
# `ap2 add` exit non-zero without mutating TASKS.md or queuing anything.


def _fake_editor(tmp_path: Path, name: str, body: str) -> str:
    """Write a one-shot fake-editor shell script that replaces the
    target buffer with `body` and exits 0. Returns its absolute path
    suitable for `EDITOR=<path>`."""
    script = tmp_path / name
    # `$1` is the temp-file path the CLI hands the editor.
    script.write_text(
        "#!/bin/sh\n"
        "cat > \"$1\" <<'EOF'\n"
        f"{body}"
        + ("" if body.endswith("\n") else "\n")
        + "EOF\n"
    )
    script.chmod(0o755)
    return str(script)


def test_add_with_no_args_opens_editor_and_uses_saved_buffer(
    tmp_path: Path, monkeypatch,
):
    """`ap2 add` (no args) with `$EDITOR` set opens the template,
    operator saves a real briefing, and the add proceeds exactly as
    if `--briefing-file` had been used. Pins the happy path of the
    editor-driven flow."""
    cfg = _project(tmp_path)
    monkeypatch.setenv(
        "EDITOR", _fake_editor(tmp_path, "ed-good.sh", _GOOD_BRIEFING),
    )

    rc = cmd_add(cfg, _add_args(briefing_file=None))

    assert rc == 0
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    # Briefing bytes round-tripped from $EDITOR's saved buffer onto disk.
    assert found.briefing is not None
    target = cfg.project_root / found.briefing
    assert target.exists()
    assert "## Verification" in target.read_text()


def test_add_with_no_args_aborts_when_editor_saves_empty(
    tmp_path: Path, monkeypatch, capsys,
):
    """Empty save (truncated buffer) is the editor-flow equivalent of
    `git commit` aborting on an empty message: exit non-zero, mutate
    nothing — no TB-N allocated, no TASKS.md touched, no operator-queue
    record."""
    cfg = _project(tmp_path)
    # Editor truncates the buffer to empty.
    script = tmp_path / "ed-empty.sh"
    script.write_text("#!/bin/sh\n: > \"$1\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    before_tasks = cfg.tasks_file.read_text()
    before_claude = (cfg.project_root / "CLAUDE.md").read_text()

    rc = cmd_add(cfg, _add_args(briefing_file=None))

    assert rc == 1
    err = capsys.readouterr().err
    assert "--briefing-file" in err
    # No TB-N leaked — CLAUDE.md unchanged.
    assert (cfg.project_root / "CLAUDE.md").read_text() == before_claude
    # TASKS.md unchanged.
    assert cfg.tasks_file.read_text() == before_tasks
    # Nothing queued.
    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    assert not queue.exists() or queue.read_text() == ""


def test_add_with_no_args_aborts_when_editor_exits_nonzero(
    tmp_path: Path, monkeypatch, capsys,
):
    """Non-zero editor exit (operator hit `:cq` in vim or killed the
    process) is also an abort — same no-mutation contract as the empty
    case."""
    cfg = _project(tmp_path)
    script = tmp_path / "ed-nonzero.sh"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    before_tasks = cfg.tasks_file.read_text()

    rc = cmd_add(cfg, _add_args(briefing_file=None))

    assert rc == 1
    assert cfg.tasks_file.read_text() == before_tasks
    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    assert not queue.exists() or queue.read_text() == ""


def test_add_with_no_args_aborts_when_editor_unchanged(
    tmp_path: Path, monkeypatch, capsys,
):
    """If the operator saves the template verbatim (no edits), treat
    it as an abort — the placeholders aren't a real briefing. Mirrors
    `git commit` refusing an unmodified commit-message template."""
    from ap2.cli import _EDITOR_TEMPLATE

    cfg = _project(tmp_path)
    # Editor leaves the template untouched (no write).
    script = tmp_path / "ed-noop.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    # Sanity-check fixture — confirm template has the placeholder so
    # `text == _EDITOR_TEMPLATE` is the path being exercised.
    assert "your title here" in _EDITOR_TEMPLATE
    before_tasks = cfg.tasks_file.read_text()

    rc = cmd_add(cfg, _add_args(briefing_file=None))

    assert rc == 1
    assert cfg.tasks_file.read_text() == before_tasks


def test_compose_briefing_via_editor_returns_none_without_editor(monkeypatch):
    """Direct unit on the helper: no `$EDITOR` set → return `None`
    immediately (no temp file created, no editor spawned). Lets
    `cmd_add` distinguish the no-editor path cleanly."""
    from ap2.cli import _compose_briefing_via_editor

    monkeypatch.delenv("EDITOR", raising=False)
    assert _compose_briefing_via_editor() is None


# ---------------------------------------------------------------------------
# TB-167: `ap2 add` defaults the target section to Backlog (was Ready).
# Backlog matches ideation-proposed tasks (uniform "to be triaged" semantics),
# the daemon's auto-promotion fast-tracks an empty-board add to Ready on the
# next tick, and `--blocked review` only surfaces in `ap2 status` when the
# task lands in Backlog — keeping operator-filed review-pending tasks from
# vanishing into a Ready half-state. Explicit `-s Ready`/`-s Frozen` keep
# their existing semantics for callers that want them.


def test_add_argparse_default_section_is_backlog(tmp_path: Path):
    """TB-167: the `add` subparser's `-s/--section` argument defaults to
    Backlog — i.e. `ap2 add --briefing-file <path>` (no `-s`) parses
    with `args.section == "Backlog"`. Prior default was "Ready"; this
    test pins the new contract at the argparse layer so a refactor of
    `build_parser` can't silently regress to the old behavior."""
    from ap2.cli import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--project", str(tmp_path),
            "add",
            "--briefing-file", "/dev/null",
        ]
    )
    assert args.cmd == "add"
    assert args.section == "Backlog"


def test_add_with_default_section_routes_through_add_backlog(tmp_path: Path):
    """TB-167: `cmd_add` with no explicit `-s` (default = "Backlog")
    enqueues `op="add_backlog"` and, after the operator-queue drain,
    the new task lands in the Backlog section.

    Exercises the helper-default path that scripts and the
    ap2-task-skill quickstart hit — what the operator gets when they
    just type `ap2 add --briefing-file …`."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    # `_add_args` defaults `section` to "Backlog" (mirrors the new
    # argparse default — TB-167). Pass it explicitly here so the
    # assertion below documents the contract under test.
    rc = cmd_add(cfg, _add_args(briefing_file=str(brief), section="Backlog"))
    assert rc == 0

    # Pending op is `add_backlog` (not `add_ready`) — verifiable on the
    # operator-queue file before the drain runs.
    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    import json as _json
    rec = _json.loads(queue.read_text().strip().splitlines()[-1])
    assert rec["op"] == "add_backlog", rec

    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    section, _ = board.find(found.id)
    assert section == "Backlog"


def test_add_with_explicit_ready_routes_through_add_ready(tmp_path: Path):
    """TB-167 regression: callers that *do* want the prior fast-track
    behavior pass `-s Ready` and get exactly that — `op="add_ready"`
    and the task lands in the Ready section. Pins the explicit-flag
    path so the default change doesn't bleed into the `-s Ready`
    branch."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief), section="Ready"))
    assert rc == 0

    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    import json as _json
    rec = _json.loads(queue.read_text().strip().splitlines()[-1])
    assert rec["op"] == "add_ready", rec

    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    section, _ = board.find(found.id)
    assert section == "Ready"


def test_add_with_explicit_frozen_routes_through_add_frozen(tmp_path: Path):
    """TB-167 regression: `-s Frozen` continues to route through
    `op="add_frozen"` and land the task in Frozen. The third branch
    of the section_map — same default-only contract as `-s Ready`."""
    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief), section="Frozen"))
    assert rc == 0

    queue = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    import json as _json
    rec = _json.loads(queue.read_text().strip().splitlines()[-1])
    assert rec["op"] == "add_frozen", rec

    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    section, _ = board.find(found.id)
    assert section == "Frozen"


def test_add_default_with_blocked_review_surfaces_in_status(
    tmp_path: Path, capsys,
):
    """TB-167's motivating UX gap: `ap2 add --briefing-file <path>
    --blocked review` (no `-s`) used to land in Ready and stay
    invisible to `ap2 status`'s `review:` line, because the
    review-pending counter only walks Backlog tasks. The default-to-
    Backlog change closes that gap — the new task lands in Backlog
    AND `ap2 status` (text + JSON) names its TB-N in the
    pending-review list."""
    import json as _json
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    brief = tmp_path / "briefing.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(
        cfg,
        _add_args(
            briefing_file=str(brief),
            section="Backlog",  # default — TB-167
            blocked="review",
        ),
    )
    assert rc == 0
    _drain(cfg)

    board = Board.load(cfg.tasks_file)
    found = next(
        (t for t in board.iter_tasks() if t.title == "Add foo helper"),
        None,
    )
    assert found is not None
    # Lands in Backlog (the only section where review gating is
    # surfaced + auto-promotion respects @blocked:review).
    section, _ = board.find(found.id)
    assert section == "Backlog"
    # `@blocked:review` codespan made it onto the task line.
    assert found.meta.get("blocked") == "review"
    assert found.blocked_on == ["review"]

    # Text branch of `ap2 status` names the TB-N on the `review:` line.
    capsys.readouterr()  # drain anything cmd_add printed
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "review:" in out
    assert found.id in out
    assert "ap2 approve" in out

    # JSON branch carries the same TB-N in `pending_review_ids`.
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert found.id in payload["pending_review_ids"]
    assert payload["pending_review"] >= 1


# ---------------------------------------------------------------------------
# TB-139: ap2 --version embeds source-commit timestamp on editable installs
# so an operator can confirm freshness without falling back to `git log`.
# Format: `ap2 <base>(+<sha>.<ts>)?` per the briefing's pinned regex.

import re as _re
import subprocess as _sp

_VERSION_RE = _re.compile(r"^ap2 0\.\d+\.\d+(\+[a-f0-9]{7,}\.\d{8}T\d{6}Z)?$")


def _git_init_with_one_commit(path: Path) -> None:
    """Bootstrap a minimal git repo at `path` with one commit so
    `git log -1` has something to return. Used by the tests below to
    exercise the editable-install code path without touching the real
    autopilot2 checkout."""
    _sp.run(["git", "init", "-q", str(path)], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README").write_text("hi\n")
    _sp.run(["git", "-C", str(path), "add", "README"], check=True)
    _sp.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
    )


def test_git_suffix_in_a_real_git_repo_returns_sha_and_ts(tmp_path: Path):
    """Editable-install path: `_git_suffix(repo_root)` against a checkout
    with at least one commit returns `<sha>.<timestamp>` matching the
    pinned format. The leading `+` is added by `get_version()`, not by
    `_git_suffix()`; here we just pin the inner shape."""
    from ap2 import _git_suffix

    _git_init_with_one_commit(tmp_path)
    suffix = _git_suffix(tmp_path)

    # Non-empty + matches `<7+ hex>.<YYYYMMDDTHHMMSSZ>`.
    assert suffix
    assert _re.match(r"^[a-f0-9]{7,}\.\d{8}T\d{6}Z$", suffix), suffix


def test_git_suffix_outside_a_git_repo_is_empty(tmp_path: Path):
    """Released-wheel path: `_git_suffix(repo_root)` on a directory with
    no `.git/` returns `""`. `get_version()` then prints just the base
    version, no `+suffix` — which is what we want for installs that
    don't have source-commit info to expose."""
    from ap2 import _git_suffix

    # tmp_path is freshly created — no `.git/` subdir.
    assert _git_suffix(tmp_path) == ""


def test_get_version_format_matches_pinned_regex():
    """End-to-end on the package's own checkout: `ap2 <get_version()>`
    matches the regex pinned in TB-139's briefing. Verifies the actual
    string operators see when they run `ap2 --version` is shaped the way
    downstream tooling expects (e.g. a sed/awk script that wants to
    extract the SHA)."""
    from ap2 import get_version

    rendered = f"ap2 {get_version()}"
    assert _VERSION_RE.match(rendered), rendered


def test_cli_version_string_matches_get_version():
    """Parity: the string the CLI prints is exactly the canonical
    accessor's output. Pins the daemon_start event field and the
    `ap2 status` line to the same source-of-truth as `ap2 --version`,
    so an operator post-mortem isn't comparing three slightly-different
    formats."""
    from ap2 import get_version
    from ap2.cli import _version_string

    assert _version_string() == get_version()


def test_status_prints_version_line(tmp_path: Path, capsys):
    """`ap2 status` prints a `version: ap2 <version>` line so the
    operator can confirm freshness alongside daemon liveness without a
    second `ap2 --version` invocation. The exact bytes match what
    `--version` would print (parity-tested above)."""
    from ap2 import get_version
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "version:" in out
    assert f"ap2 {get_version()}" in out


def test_status_json_includes_version(tmp_path: Path, capsys):
    """The `--json` payload carries the same version string under a
    `version` key — pins the contract for any operator tooling that
    polls `ap2 status --json` for build identity."""
    import json as _json
    from ap2 import get_version
    from ap2.cli import cmd_status

    cfg = _project(tmp_path)
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["version"] == get_version()


def test_emit_daemon_start_includes_version(tmp_path: Path):
    """The daemon's startup event carries the source revision (TB-139)
    so a post-mortem reading `events.jsonl` can correlate state-file
    mutations with the exact commit the daemon was loading. Same string
    as `ap2 --version` and `ap2 status` (parity-tested above)."""
    from ap2 import events as _events, get_version
    from ap2.daemon import _emit_daemon_start

    cfg = _project(tmp_path)
    evt = _emit_daemon_start(cfg)

    assert evt["type"] == "daemon_start"
    assert evt["version"] == get_version()

    # Also pinned on disk — the events.jsonl line carries the field too.
    tail = _events.tail(cfg.events_file, 5)
    starts = [e for e in tail if e["type"] == "daemon_start"]
    assert starts and starts[-1]["version"] == get_version()



# ---------------------------------------------------------------------------
# TB-153: cmd_update — in-place edit via --title / --tags / --blocked /
# --description / --briefing-file / --clear-tags / --clear-blocked.
#
# Each test uses _drain to advance from "queued" → "applied" so the
# post-state assertions match the pre-TB-131 synchronous semantics
# operators are used to.


def _update_args(
    task_id: str,
    *,
    title: str | None = None,
    tags: str | None = None,
    blocked: str | None = None,
    description: str | None = None,
    clear_tags: bool = False,
    clear_blocked: bool = False,
    briefing_file: str | None = None,
    force: bool = False,
    skip_goal_alignment: bool = False,
) -> Namespace:
    """Build a Namespace shaped like cmd_update's argparse output.

    TB-170: `--skip-goal-alignment` bypasses TB-161 + TB-164 on
    briefing-content edits for operator-driven exceptions.
    """
    return Namespace(
        task_id=task_id,
        title=title,
        tags=tags,
        blocked=blocked,
        description=description,
        clear_tags=clear_tags,
        clear_blocked=clear_blocked,
        briefing_file=briefing_file,
        force=force,
        skip_goal_alignment=skip_goal_alignment,
    )


def _seed(cfg: Config, task_id: str = "TB-500", **kwargs) -> None:
    board = Board.load(cfg.tasks_file)
    board.add("Backlog", task_id=task_id, title=kwargs.pop("title", "seed"), **kwargs)
    board.save()


def test_cmd_update_invokes_queue_append_with_field_dict(
    tmp_path: Path, monkeypatch
):
    """`ap2 update TB-X --tags foo,bar` calls `do_operator_queue_append`
    with `op="update"` + the right field dict; omitted flags are NOT
    present-as-None in the payload so the queue-append handler can
    distinguish "unchanged" from "None"."""
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-500")

    captured: dict = {}

    def fake_append(cfg_arg, payload):
        captured["payload"] = payload
        return {
            "content": [
                {"type": "text", "text": '{"op":"update","task_id":"TB-500"}'},
            ]
        }

    monkeypatch.setattr(tools, "do_operator_queue_append", fake_append)

    rc = cmd_update(cfg, _update_args("TB-500", tags="foo,bar"))
    assert rc == 0
    payload = captured["payload"]
    assert payload["op"] == "update"
    assert payload["task_id"] == "TB-500"
    assert payload["tags"] == ["#foo", "#bar"]
    # Omitted flags absent (not present-as-None).
    assert "title" not in payload
    assert "description" not in payload
    assert "blocked" not in payload
    assert "briefing" not in payload


def test_cmd_update_title_round_trips(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-501", title="old")
    rc = cmd_update(cfg, _update_args("TB-501", title="brand new"))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-501")
    assert t is not None
    assert t.title == "brand new"


def test_cmd_update_tags_replaces_existing(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-502", tags=["#old"])
    rc = cmd_update(cfg, _update_args("TB-502", tags="alpha,#beta"))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-502")
    assert t is not None
    assert "#alpha" in t.tags
    assert "#beta" in t.tags
    assert "#old" not in t.tags


def test_cmd_update_clear_tags_removes_all(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-503", tags=["#a", "#b"])
    rc = cmd_update(cfg, _update_args("TB-503", clear_tags=True))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-503")
    assert t is not None
    assert t.tags == []


def test_cmd_update_blocked_round_trips(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-504")
    rc = cmd_update(cfg, _update_args("TB-504", blocked="TB-7,review"))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-504")
    assert t is not None
    assert t.meta.get("blocked") == "TB-7,review"


def test_cmd_update_clear_blocked_removes_codespan(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-505", meta={"blocked": "TB-7"})
    rc = cmd_update(cfg, _update_args("TB-505", clear_blocked=True))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-505")
    assert t is not None
    assert "blocked" not in t.meta


def test_cmd_update_description_round_trips(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-506", description="old prose")
    rc = cmd_update(cfg, _update_args("TB-506", description="new prose"))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-506")
    assert t is not None
    assert t.description == "new prose"


def test_cmd_update_briefing_file_round_trips(tmp_path: Path):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    # Seed a task with an existing briefing.
    cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
    bp = cfg.tasks_dir / "stable.md"
    bp.write_text("# old\n\n## Goal\nx\n## Scope\n- f\n## Design\nx\n## Verification\n- `t`\n## Out of scope\n- n\n")
    rel = str(bp.relative_to(cfg.project_root))
    _seed(cfg, task_id="TB-507", briefing=rel)

    new_brief = tmp_path / "new.md"
    new_brief.write_text(canonical_briefing("TB-507", title="Updated"))
    rc = cmd_update(cfg, _update_args("TB-507", briefing_file=str(new_brief)))
    assert rc == 0
    _drain(cfg)
    # Briefing file overwritten in place — slug-stable.
    assert bp.read_text() == new_brief.read_text()
    t = Board.load(cfg.tasks_file).get("TB-507")
    assert t.briefing == rel


def test_cmd_update_briefing_file_stdin(tmp_path: Path, monkeypatch):
    """`--briefing-file -` reads from stdin."""
    import io

    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
    bp = cfg.tasks_dir / "via-stdin.md"
    bp.write_text("# old\n\n## Goal\nx\n## Scope\n- f\n## Design\nx\n## Verification\n- `t`\n## Out of scope\n- n\n")
    rel = str(bp.relative_to(cfg.project_root))
    _seed(cfg, task_id="TB-508", briefing=rel)

    new_briefing = canonical_briefing("TB-508", title="Stdin briefing")
    monkeypatch.setattr("sys.stdin", io.StringIO(new_briefing))

    rc = cmd_update(cfg, _update_args("TB-508", briefing_file="-"))
    assert rc == 0
    _drain(cfg)
    assert bp.read_text() == new_briefing


def test_cmd_update_unknown_task_returns_error(tmp_path: Path, capsys):
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    rc = cmd_update(cfg, _update_args("TB-9999", title="x"))
    assert rc == 1
    assert "not on board" in capsys.readouterr().err


def test_cmd_update_no_fields_returns_error(tmp_path: Path, capsys):
    """No flags → no-op → refuse, since the queue would otherwise carry
    a record with empty `fields=[]`."""
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-510")
    rc = cmd_update(cfg, _update_args("TB-510"))
    assert rc == 1
    assert "field" in capsys.readouterr().err.lower()


def test_cmd_update_empty_tags_string_is_rejected(tmp_path: Path, capsys):
    """`--tags ''` is ambiguous (typo vs intentional clear) → refuse,
    nudging at `--clear-tags`."""
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    _seed(cfg, task_id="TB-511", tags=["#a"])
    rc = cmd_update(cfg, _update_args("TB-511", tags=""))
    assert rc == 1
    err = capsys.readouterr().err
    assert "--clear-tags" in err


def test_cmd_update_active_without_force_returns_error(
    tmp_path: Path, capsys
):
    """The fence message comes from `do_operator_queue_append` and
    surfaces verbatim through `cmd_update`."""
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-512", title="running")
    board.save()
    rc = cmd_update(cfg, _update_args("TB-512", title="x"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Active" in err
    assert "force" in err


def test_cmd_update_active_with_force_succeeds(tmp_path: Path):
    """`--force` allows the title update to land on Active."""
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-513", title="old running")
    board.save()
    rc = cmd_update(cfg, _update_args("TB-513", title="renamed", force=True))
    assert rc == 0
    _drain(cfg)
    t = Board.load(cfg.tasks_file).get("TB-513")
    assert t is not None
    assert t.title == "renamed"


def test_cmd_update_active_force_briefing_still_refused(
    tmp_path: Path, capsys
):
    """Even with `--force`, briefing-content edits to Active are
    hard-refused (TB-110 snapshot hash + agent mid-run re-read)."""
    from ap2.cli import cmd_update

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
    bp = cfg.tasks_dir / "running-task.md"
    bp.write_text("# old\n\n## Goal\nx\n## Scope\n- f\n## Design\nx\n## Verification\n- `t`\n## Out of scope\n- n\n")
    rel = str(bp.relative_to(cfg.project_root))
    board.add("Active", task_id="TB-514", title="running", briefing=rel)
    board.save()

    new_brief = tmp_path / "new.md"
    new_brief.write_text("# new\n\n## Goal\nz\n## Scope\n- f\n## Design\ne\n## Verification\n- `t`\n## Out of scope\n- n\n")
    rc = cmd_update(
        cfg,
        _update_args(
            "TB-514", briefing_file=str(new_brief), force=True
        ),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Active" in err
    assert "briefing" in err.lower()


# ---- argparse layer wiring


def test_cmd_update_argparse_wires_through_build_parser(tmp_path: Path):
    """`ap2 update TB-X --title 'x' ...` → cmd_update with the right
    Namespace. Belt-and-suspenders: the unit tests above call
    cmd_update directly; this one verifies argparse-side wiring so a
    refactor of `build_parser` can't silently drop the subcommand."""
    from ap2.cli import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--project", str(tmp_path),
            "update", "TB-700",
            "--title", "renamed",
            "--tags", "foo,bar",
            "--blocked", "TB-9",
            "--description", "blurb",
            "--force",
        ]
    )
    assert args.cmd == "update"
    assert args.task_id == "TB-700"
    assert args.title == "renamed"
    assert args.tags == "foo,bar"
    assert args.blocked == "TB-9"
    assert args.description == "blurb"
    assert args.force is True
    assert args.clear_tags is False
    assert args.clear_blocked is False
    assert args.briefing_file is None


def test_cmd_update_argparse_supports_clear_flags(tmp_path: Path):
    from ap2.cli import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--project", str(tmp_path),
            "update", "TB-701",
            "--clear-tags",
            "--clear-blocked",
        ]
    )
    assert args.clear_tags is True
    assert args.clear_blocked is True


# ---------------------------------------------------------------------------
# TB-170: `--skip-goal-alignment` operator-CLI escape hatch from the
# TB-161 goal-cite + TB-164 Why-now checks. The flag is wired in BOTH
# `ap2 add` and `ap2 update` subparsers; cmd_add/cmd_update forward it
# onto the operator-queue payload as `skip_goal_alignment: true`.


_TB170_NO_ALIGNMENT_BRIEFING = (
    # Canonical-shape briefing that intentionally fails BOTH TB-161
    # (Goal body cites no goal.md anchor) and TB-164 (no Why-now
    # marker). Used to exercise the bypass end-to-end.
    "# operator-meta typo fix\n\n"
    "## Goal\n\nFix a one-line typo in a comment.\n\n"
    "## Scope\n\n- foo.py\n\n"
    "## Design\n\nDirect edit.\n\n"
    "## Verification\n\n- `uv run pytest -q` — gates pass\n\n"
    "## Out of scope\n\n- nothing\n"
)


def _seed_real_goal_md(cfg: Config) -> None:
    """The validator's TB-161 anchor check short-circuits to "skip"
    when goal.md is the all-placeholder template. Tests of the bypass
    need a real goal.md so the no-anchor briefing actually trips the
    gate when the bypass is OFF."""
    (cfg.project_root / "goal.md").write_text(
        "# Project Goals\n\n"
        "## Mission\nOne-sentence statement of project purpose.\n\n"
        "## Done when\n"
        "- Operators can run the full pipeline without intervention.\n\n"
        "## Current focus: ideation quality\n\nstuff\n"
    )


def test_cmd_add_skip_goal_alignment_succeeds(tmp_path: Path):
    """`ap2 add --skip-goal-alignment --briefing-file <no-anchor-no-why-now>`
    succeeds: the queue-append payload carries `skip_goal_alignment:
    true`, the queue drains, and TASKS.md contains the new task. This
    is the end-to-end happy-path proof that the operator can file a
    legitimately-meta task without manufacturing goal-alignment prose.
    """
    cfg = _project(tmp_path)
    _seed_real_goal_md(cfg)
    brief = tmp_path / "no-alignment.md"
    brief.write_text(_TB170_NO_ALIGNMENT_BRIEFING)

    rc = cmd_add(
        cfg,
        _add_args(
            briefing_file=str(brief),
            skip_goal_alignment=True,
        ),
    )
    assert rc == 0

    # Payload-on-disk pin: the queue record carries the flag so the
    # drain-side audit can decorate operator_log.md.
    qpath = tools.operator_queue_path(cfg)
    import json as _json
    lines = [
        _json.loads(ln) for ln in qpath.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    assert lines[0]["args"].get("skip_goal_alignment") is True

    # Drain → TASKS.md contains the new task.
    _drain(cfg)
    board = Board.load(cfg.tasks_file)
    titles = [t.title for t in board.iter_tasks()]
    assert "operator-meta typo fix" in titles


def test_cmd_add_without_flag_rejects_no_alignment_briefing(
    tmp_path: Path, capsys
):
    """Pin the default contract: WITHOUT `--skip-goal-alignment`, the
    same briefing is rejected by TB-161/164 — `cmd_add` exits non-zero
    with a structural error, no queue line, no TASKS.md mutation."""
    cfg = _project(tmp_path)
    _seed_real_goal_md(cfg)
    brief = tmp_path / "no-alignment.md"
    brief.write_text(_TB170_NO_ALIGNMENT_BRIEFING)

    pre_tasks = cfg.tasks_file.read_text()
    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))
    assert rc == 1, "default cmd_add must reject a no-anchor + no-why-now briefing"
    err = capsys.readouterr().err
    # Either TB-161 (anchor) or TB-164 (why-now) surfaces.
    assert (
        "TB-161" in err or "TB-164" in err or "Why now" in err
        or "anchor" in err.lower()
    ), err
    # Nothing landed.
    assert cfg.tasks_file.read_text() == pre_tasks
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_cmd_add_skip_goal_alignment_audit_line_decorated(tmp_path: Path):
    """When the flag is set on `ap2 add`, the drain-side audit line in
    operator_log.md is decorated with `(goal-alignment check skipped)`
    so future ideation cycles can grep for the substring. Pins the
    audit-line shape end-to-end (queue → drain → operator_log.md).
    """
    cfg = _project(tmp_path)
    _seed_real_goal_md(cfg)
    brief = tmp_path / "no-alignment.md"
    brief.write_text(_TB170_NO_ALIGNMENT_BRIEFING)

    rc = cmd_add(
        cfg,
        _add_args(briefing_file=str(brief), skip_goal_alignment=True),
    )
    assert rc == 0
    _drain(cfg)
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log = log_path.read_text()
    assert "applied operator-queued add_backlog" in log
    assert "(goal-alignment check skipped)" in log


def test_cmd_add_without_flag_audit_line_unchanged(tmp_path: Path):
    """Pin the no-suffix shape: when `--skip-goal-alignment` is NOT
    passed, the drain-side audit line keeps the historical shape with
    no suffix. Concretely: a goal-aligned briefing applied without the
    flag does NOT land a `(goal-alignment check skipped)` substring in
    operator_log.md."""
    cfg = _project(tmp_path)
    brief = tmp_path / "good.md"
    brief.write_text(_GOOD_BRIEFING)

    rc = cmd_add(cfg, _add_args(briefing_file=str(brief)))
    assert rc == 0
    _drain(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "applied operator-queued add_backlog" in log
    # Audit suffix only present when the flag was set.
    assert "(goal-alignment check skipped)" not in log


def test_cmd_add_argparse_wires_skip_goal_alignment(tmp_path: Path):
    """`ap2 add --skip-goal-alignment` parses to `args.skip_goal_alignment
    is True`; absent it defaults to False. Argparse-side wiring belt-
    and-suspenders so a refactor of `build_parser` can't silently drop
    the flag."""
    from ap2.cli import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--project", str(tmp_path),
            "add",
            "--briefing-file", "-",
            "--skip-goal-alignment",
        ]
    )
    assert args.cmd == "add"
    assert args.skip_goal_alignment is True

    args2 = p.parse_args(
        ["--project", str(tmp_path), "add", "--briefing-file", "-"]
    )
    assert args2.skip_goal_alignment is False


def test_cmd_update_argparse_wires_skip_goal_alignment(tmp_path: Path):
    """Symmetrical pin for `ap2 update --skip-goal-alignment`. The flag
    must be wired in BOTH subparsers per the briefing's verification
    (`grep -c '"--skip-goal-alignment"' ap2/cli.py` ≥ 2)."""
    from ap2.cli import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--project", str(tmp_path),
            "update", "TB-700",
            "--title", "x",
            "--skip-goal-alignment",
        ]
    )
    assert args.cmd == "update"
    assert args.skip_goal_alignment is True

    args2 = p.parse_args(
        [
            "--project", str(tmp_path),
            "update", "TB-700",
            "--title", "x",
        ]
    )
    assert args2.skip_goal_alignment is False


# ---------------------------------------------------------------------------
# TB-158: cmd_logs renders `verification_failed` rows with a counter +
# failing-bullet headlines + judge notes. Passing / unverified bullets are
# collapsed into the counter only — full payload still available via
# `--json`. Pins both the pretty path AND the json regression so an
# operator script depending on raw output keeps working.


def _seed_verification_failed(
    cfg: Config,
    task: str = "TB-158",
    *,
    pass_n: int = 5,
    fail_bullets: list[tuple[str, str, str]] | None = None,
    unverified_n: int = 1,
) -> None:
    """Append one verification_failed event with the requested mix of
    pass/fail/unverified criteria. Each fail entry is `(kind, bullet, notes)`."""
    fails = fail_bullets or []
    criteria = (
        [
            {"kind": "shell", "status": "pass", "bullet": f"pass#{i}", "notes": ""}
            for i in range(pass_n)
        ]
        + [
            {"kind": k, "status": "fail", "bullet": b, "notes": n}
            for (k, b, n) in fails
        ]
        + [
            {"kind": "prose", "status": "unverified",
             "bullet": f"unv#{i}", "notes": "skipped"}
            for i in range(unverified_n)
        ]
    )
    events.append(
        cfg.events_file, "verification_failed",
        task=task, kind="per_task", overall="fail", criteria=criteria,
    )


def test_cmd_logs_pretty_renders_verification_failed(tmp_path: Path, capsys):
    """5 pass + 2 fail + 1 unverified renders with a counter naming the
    three buckets, both failing bullet headlines (truncated to ~120 in CLI),
    and the judge's notes (truncated to ~200). Passing bullets are NOT
    individually printed."""
    from ap2.cli import cmd_logs

    cfg = _project(tmp_path)
    _seed_verification_failed(
        cfg,
        task="TB-1500",
        pass_n=5,
        fail_bullets=[
            ("prose", "Manual: kick a long-running task on stoch and "
                      "mention `@claude-bot status`",
             "Manual verification bullet requires a live stoch deployment "
             "test — no evidence such a manual run was performed"),
            ("shell", "`grep -qE \"summarize_verification_failed\" "
                      "ap2/events.py ap2/cli.py ap2/web.py`",
             "ripgrep returned 1; symbol absent in cli.py"),
        ],
        unverified_n=1,
    )

    rc = cmd_logs(cfg, Namespace(n=10, json=False))
    assert rc == 0
    out = capsys.readouterr().out

    # Counter names all three buckets — the briefing's "5/2 failed,
    # 1 unverified (or equivalent counter)" pin.
    assert "5/8 passed" in out
    assert "2 failed" in out
    assert "1 unverified" in out
    # Both failing bullet headlines surface (truncated headlines, not
    # the full text — the prefix is enough for the operator to locate
    # the bullet).
    assert "Manual: kick a long-running task on stoch" in out
    assert "summarize_verification_failed" in out
    # The judge's note for at least one fail surfaces (truncated).
    assert "Manual verification bullet requires" in out
    assert "ripgrep returned 1" in out
    # Passing bullets are NOT individually rendered — only the counter
    # carries them. None of the synthetic `pass#i` markers leak.
    assert "pass#0" not in out
    assert "pass#4" not in out
    # Same for unverified — counter only.
    assert "unv#0" not in out
    # The fail-mark (✗) anchors each failed bullet headline.
    assert "✗" in out


def test_cmd_logs_json_flag_bypasses_pretty_formatter(tmp_path: Path, capsys):
    """Regression pin: `--json` prints the raw event JSON unchanged so
    operator scripts piping through `jq` or grep keep working. The pretty
    formatter must NOT engage when --json is set."""
    from ap2.cli import cmd_logs

    cfg = _project(tmp_path)
    _seed_verification_failed(
        cfg,
        task="TB-1501",
        pass_n=2,
        fail_bullets=[("prose", "manual headline", "judge note here")],
        unverified_n=0,
    )

    rc = cmd_logs(cfg, Namespace(n=10, json=True))
    assert rc == 0
    out = capsys.readouterr().out

    # No pretty rendering markers — the multi-line bullet/note formatter
    # uses ✗ and ↳ glyphs; in --json mode neither leaks.
    assert "✗" not in out
    assert "↳" not in out
    assert "passed," not in out  # the counter line uses this template
    # JSON shape preserved verbatim — the line is parseable and carries
    # the full criteria list (no truncation, no field reflowing).
    import json as _json
    lines = [ln for ln in out.splitlines() if ln.strip()]
    parsed = [_json.loads(ln) for ln in lines if "verification_failed" in ln]
    assert parsed, out
    e = parsed[-1]
    assert e["type"] == "verification_failed"
    assert e["task"] == "TB-1501"
    # criteria array survives unmolested — same shape on disk and in --json.
    assert isinstance(e["criteria"], list)
    assert any(c.get("status") == "fail" for c in e["criteria"])
    assert any(
        c.get("bullet") == "manual headline" for c in e["criteria"]
    )


# ---------------------------------------------------------------------------
# TB-180: cmd_logs renders the three usage-carrying event types
# (`judge_call`, `task_run_usage`, `control_run_usage`) with the same
# compact 6-field tuple + identity prefix that TB-179 introduced for
# `/events`. The verbose `usage` / `model_usage` / `server_tool_use` /
# `cache_creation` blobs do NOT leak into the inline rendering; operators
# wanting raw bytes use `--json` (regression-pinned).


_TB180_FULL_JUDGE_CALL = {
    "ts": "2026-05-04T19:11:38Z",
    "type": "judge_call",
    "task": "TB-1800",
    "bullet_idx": 7,
    "bullet_kind": "prose",
    "verdict": "pass",
    "duration_s": 8.002,
    "model": "claude-opus-4-7",
    "num_turns": 2,
    "total_cost_usd": 0.146176,
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 6,
        "cache_creation_input_tokens": 17016,
        "cache_read_input_tokens": 42310,
        "output_tokens": 287,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0,
        },
        "service_tier": "standard",
        "cache_creation": {"ephemeral_5m_input_tokens": 17016},
        "iterations": 1,
    },
    "model_usage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 7636,
            "outputTokens": 22,
            "costUSD": 0.006605,
            "inference_geo": "us",
        },
    },
}


_TB180_FULL_TASK_RUN_USAGE = {
    "ts": "2026-05-04T15:15:13Z",
    "type": "task_run_usage",
    "task": "TB-1801",
    "run_id": "20260504T150009Z-TB-1801",
    "status": "complete",
    "duration_s": 342.117,
    "total_cost_usd": 0.851234,
    "num_turns": 41,
    "model": "claude-opus-4-7",
    "usage": {
        "input_tokens": 42,
        "cache_creation_input_tokens": 68234,
        "cache_read_input_tokens": 512891,
        "output_tokens": 4123,
        "server_tool_use": {"web_search_requests": 0},
        "service_tier": "standard",
        "cache_creation": {"ephemeral_5m_input_tokens": 68234},
        "iterations": 1,
    },
    "model_usage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 6727,
            "costUSD": 0.006812,
            "inference_geo": "us",
        },
    },
}


_TB180_FULL_CONTROL_RUN_USAGE = {
    "ts": "2026-05-04T18:09:21Z",
    "type": "control_run_usage",
    "label": "ideation",
    "run_id": "20260504T180620Z-ideation",
    "status": "complete",
    "duration_s": 178.301,
    "total_cost_usd": 0.421875,
    "num_turns": 11,
    "usage": {
        "input_tokens": 18,
        "cache_creation_input_tokens": 49231,
        "cache_read_input_tokens": 104982,
        "output_tokens": 2034,
        "server_tool_use": {"web_search_requests": 0},
        "service_tier": "standard",
        "cache_creation": {"ephemeral_5m_input_tokens": 49231},
        "iterations": 1,
    },
    "model_usage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 4726,
            "costUSD": 0.004806,
            "inference_geo": "us",
        },
    },
}


def _seed_raw_event(cfg: Config, payload: dict) -> None:
    """Append a pre-shaped event line with explicit `ts` to events.jsonl.
    Bypasses `events.append` because that helper auto-stamps `ts`; we
    want to pin a stable timestamp for the test's stdout assertions."""
    import json as _json
    with cfg.events_file.open("a") as f:
        f.write(_json.dumps(payload) + "\n")


def _assert_no_verbose_keys(out: str) -> None:
    """Pin: the inline rendering omits the verbose nested keys that the
    full payload carries. Operators wanting them use `--json | jq`."""
    for forbidden in (
        "server_tool_use",
        "iterations",
        "service_tier",
        "inference_geo",
        "ephemeral_5m_input_tokens",
        "model_usage",
    ):
        assert forbidden not in out, f"verbose key leaked: {forbidden!r}"


def test_cmd_logs_pretty_renders_judge_call(tmp_path: Path, capsys):
    """TB-180: `judge_call` rows render as `<ts> judge_call <identity> ·
    <6-field tuple> · <duration>` — identity prefix is `task=TB-N
    bullet=N/<kind> <verdict>`. The verbose `usage` /
    `model_usage` / `server_tool_use` / nested `cache_creation` keys do
    NOT leak into the inline output."""
    from ap2.cli import cmd_logs

    cfg = _project(tmp_path)
    _seed_raw_event(cfg, _TB180_FULL_JUDGE_CALL)

    rc = cmd_logs(cfg, Namespace(n=10, json=False))
    assert rc == 0
    out = capsys.readouterr().out

    # Identity prefix tokens for the judge_call shape.
    assert "task=TB-1800" in out
    assert "bullet=7/prose" in out
    assert "pass" in out

    # All 6 compact fields surface.
    assert "in=6" in out                # input_tokens
    assert "out=287" in out             # output_tokens
    assert "cc=17,016" in out           # cache_creation_input_tokens
    assert "cr=42,310" in out           # cache_read_input_tokens
    assert "$0.1462" in out             # total_cost_usd, 4dp
    assert "8.0s" in out                # duration_s, 1dp

    # Verbose nested keys absent — that's the whole point of compaction.
    _assert_no_verbose_keys(out)
    # The nested `cache_creation` object's structure (matched braces around
    # ephemeral_5m_input_tokens) does not appear inline — the scalar
    # `cache_creation_input_tokens` (cc=) is what surfaces. Pin by absence
    # of the inner object marker.
    assert "{'ephemeral" not in out
    assert '"ephemeral' not in out


def test_cmd_logs_pretty_renders_task_run_usage(tmp_path: Path, capsys):
    """TB-180: `task_run_usage` rows render with the `task=TB-N <status>
    run=<run_id>` identity prefix instead of the `judge_call` bullet
    shape. The 6 numeric fields surface; verbose nested keys do not."""
    from ap2.cli import cmd_logs

    cfg = _project(tmp_path)
    _seed_raw_event(cfg, _TB180_FULL_TASK_RUN_USAGE)

    rc = cmd_logs(cfg, Namespace(n=10, json=False))
    assert rc == 0
    out = capsys.readouterr().out

    # Identity prefix specific to task_run_usage.
    assert "task=TB-1801" in out
    assert "complete" in out
    assert "run=20260504T150009Z-TB-1801" in out

    # 6 compact fields.
    assert "in=42" in out
    assert "out=4,123" in out
    assert "cc=68,234" in out
    assert "cr=512,891" in out
    assert "$0.8512" in out
    assert "342.1s" in out

    _assert_no_verbose_keys(out)


def test_cmd_logs_pretty_renders_control_run_usage(tmp_path: Path, capsys):
    """TB-180: `control_run_usage` rows render with the `label=<label>
    <status> run=<run_id>` identity prefix (cron / ideation / mattermost
    runs don't have a TB-id). The 6 compact fields surface."""
    from ap2.cli import cmd_logs

    cfg = _project(tmp_path)
    _seed_raw_event(cfg, _TB180_FULL_CONTROL_RUN_USAGE)

    rc = cmd_logs(cfg, Namespace(n=10, json=False))
    assert rc == 0
    out = capsys.readouterr().out

    # Identity prefix specific to control_run_usage.
    assert "label=ideation" in out
    assert "complete" in out
    assert "run=20260504T180620Z-ideation" in out

    # 6 compact fields.
    assert "in=18" in out
    assert "out=2,034" in out
    assert "cc=49,231" in out
    assert "cr=104,982" in out
    assert "$0.4219" in out
    assert "178.3s" in out

    _assert_no_verbose_keys(out)


def test_cmd_logs_json_flag_preserves_verbose_usage_payload(
    tmp_path: Path, capsys,
):
    """TB-180 regression pin (parallel to TB-158's verification_failed
    pin): when `--json` is set, `cmd_logs` skips ALL pretty-formatters
    — including the new compact-usage path — and prints the full event
    JSON verbatim. The verbose nested fields the compact path strips
    inline (`server_tool_use`, `iterations`, `service_tier`,
    `model_usage`, the nested `cache_creation` object) MUST be present
    in `--json` output so operator scripts piping through `jq` keep
    working unchanged."""
    from ap2.cli import cmd_logs

    cfg = _project(tmp_path)
    _seed_raw_event(cfg, _TB180_FULL_JUDGE_CALL)
    _seed_raw_event(cfg, _TB180_FULL_TASK_RUN_USAGE)
    _seed_raw_event(cfg, _TB180_FULL_CONTROL_RUN_USAGE)

    rc = cmd_logs(cfg, Namespace(n=10, json=True))
    assert rc == 0
    out = capsys.readouterr().out

    # Each non-empty stdout line is parseable JSON — pretty-formatting
    # bypassed. (No `·` separator from the compact form, no `<ts> type:16s`
    # padding.)
    import json as _json
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) >= 3
    parsed = [_json.loads(ln) for ln in lines]
    by_type = {e["type"]: e for e in parsed if "type" in e}

    # All three event types round-trip through `--json` unchanged.
    assert "judge_call" in by_type
    assert "task_run_usage" in by_type
    assert "control_run_usage" in by_type

    jc = by_type["judge_call"]
    assert jc["task"] == "TB-1800"
    # Verbose nested keys are STILL in the --json payload (pretty-bypass).
    assert jc["usage"]["server_tool_use"]["web_search_requests"] == 0
    assert jc["usage"]["service_tier"] == "standard"
    assert jc["usage"]["cache_creation"]["ephemeral_5m_input_tokens"] == 17016
    assert jc["usage"]["iterations"] == 1
    assert "model_usage" in jc
    assert (
        jc["model_usage"]["claude-haiku-4-5-20251001"]["inference_geo"]
        == "us"
    )

    tr = by_type["task_run_usage"]
    assert tr["task"] == "TB-1801"
    assert "model_usage" in tr
    assert tr["usage"]["server_tool_use"]["web_search_requests"] == 0

    cr = by_type["control_run_usage"]
    assert cr["label"] == "ideation"
    assert "model_usage" in cr
    assert cr["usage"]["service_tier"] == "standard"


def test_cmd_logs_pretty_path_does_not_mutate_events_jsonl(
    tmp_path: Path, capsys,
):
    """TB-180 pin: rendering compact usage rows is a display-layer
    operation. `cmd_logs` reads `events.jsonl` and writes nothing back.
    A pre/post hash + byte-count comparison catches any accidental
    write-on-read regression (e.g. a refactor that buffers lines back
    into the file)."""
    from ap2.cli import cmd_logs
    import hashlib

    cfg = _project(tmp_path)
    _seed_raw_event(cfg, _TB180_FULL_JUDGE_CALL)
    _seed_raw_event(cfg, _TB180_FULL_TASK_RUN_USAGE)
    _seed_raw_event(cfg, _TB180_FULL_CONTROL_RUN_USAGE)

    pre_bytes = cfg.events_file.read_bytes()
    pre_hash = hashlib.sha256(pre_bytes).hexdigest()

    rc = cmd_logs(cfg, Namespace(n=10, json=False))
    assert rc == 0
    capsys.readouterr()  # drain stdout so the test runner doesn't echo it.

    post_bytes = cfg.events_file.read_bytes()
    post_hash = hashlib.sha256(post_bytes).hexdigest()

    assert pre_bytes == post_bytes
    assert pre_hash == post_hash


# ---------------------------------------------------------------------------
# cmd_ideate (TB-159) — manual ideation trigger that bypasses the natural
# empty-board / cooldown / `AP2_IDEATION_DISABLED` gates by routing through
# the operator queue. The actual SDK invocation lives on the daemon side
# (`force_ideate` runs after `drain_operator_queue` returns
# `force_ideate=True`); the CLI just appends the queue record and exits.


def test_cmd_ideate_appends_queue_record_with_force_false_default(tmp_path: Path):
    """Default invocation (`ap2 ideate`, no `--force`) writes one
    `{"op":"ideate","force":false,...}` queue record and returns 0
    immediately."""
    cfg = _project(tmp_path)
    rc = cmd_ideate(cfg, Namespace(force=False))
    assert rc == 0

    queue_path = tools.operator_queue_path(cfg)
    lines = [
        ln for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    import json as _json

    rec = _json.loads(lines[0])
    assert rec["op"] == "ideate"
    assert rec["args"] == {"force": False}


def test_cmd_ideate_queues_when_active_task_present_no_force(
    tmp_path: Path, capsys
):
    """TB-194: with a task in Active, `ap2 ideate` (no `--force`) now
    queues successfully — the at-append-time Active gate has been
    removed (the loop topology already prevents the race the gate was
    guarding). Pre-TB-194 this was a hard rc=1 reject."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-2000", title="in flight")
    board.save()

    rc = cmd_ideate(cfg, Namespace(force=False))
    assert rc == 0
    err = capsys.readouterr().err
    assert err == ""

    queue_path = tools.operator_queue_path(cfg)
    import json as _json

    lines = [ln for ln in queue_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["op"] == "ideate"
    assert rec["args"] == {"force": False}


def test_cmd_ideate_force_still_queues_with_active(tmp_path: Path):
    """TB-194: `--force` is now a no-op for the routing decision but
    still rides the queue payload as audit metadata. `ap2 ideate
    --force` queues a record with `args.force=true` regardless of
    board state — same outcome as no-force, with the operator-intent
    flag preserved on the queue record."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-2010", title="forced through")
    board.save()

    rc = cmd_ideate(cfg, Namespace(force=True))
    assert rc == 0

    queue_path = tools.operator_queue_path(cfg)
    import json as _json

    lines = [ln for ln in queue_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["op"] == "ideate"
    assert rec["args"]["force"] is True


def test_cmd_ideate_is_non_blocking_no_sdk_invocation(
    tmp_path: Path, monkeypatch
):
    """The CLI MUST NOT spin up the SDK / control-agent itself — the
    daemon owns that. Pin the contract by stubbing `_run_control_agent`,
    `force_ideate`, and `_maybe_ideate` to raise if invoked, then
    asserting the call returns within a tight wallclock budget."""
    import time as _time

    cfg = _project(tmp_path)

    from ap2 import daemon as _daemon
    from ap2 import ideation as _ideation

    def boom(*a, **kw):
        raise AssertionError(
            "cmd_ideate must NOT invoke the SDK / ideation directly — "
            "the daemon picks up the queue signal on the next tick."
        )

    monkeypatch.setattr(_daemon, "_run_control_agent", boom)
    monkeypatch.setattr(_ideation, "force_ideate", boom)
    monkeypatch.setattr(_ideation, "_maybe_ideate", boom)
    monkeypatch.setattr(_ideation, "_run_ideation", boom)

    t0 = _time.monotonic()
    rc = cmd_ideate(cfg, Namespace(force=False))
    elapsed = _time.monotonic() - t0

    assert rc == 0
    # 2s is generous; in practice the queue append is sub-millisecond.
    assert elapsed < 2.0, (
        f"cmd_ideate should return immediately; took {elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# TB-193: cmd_update_goal — refresh goal.md via the operator queue so
# focus rotations don't require `ap2 daemon-control --pause`. The CLI
# reads from --file <path> or --file - (stdin), then dispatches via
# `do_operator_queue_append({"op":"update_goal", ...})`.


_UPDATE_GOAL_PAYLOAD = (
    "# Project Goals\n\n"
    "## Mission\nShip ap2 hands-off-by-default.\n\n"
    "## Done when\n- Operators can refresh goal.md without pausing.\n\n"
    "## Current focus: ideation quality\n\nSignal collection.\n"
)


def test_cmd_update_goal_file_path_dispatches(tmp_path: Path):
    """`--file <path>` reads from disk and queues an `update_goal` op.
    Drain applies the write; goal.md on disk matches the payload."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    payload_path = tmp_path / "new_goal.md"
    payload_path.write_text(_UPDATE_GOAL_PAYLOAD)

    rc = cmd_update_goal(
        cfg,
        Namespace(file=str(payload_path), reason=None),
    )
    assert rc == 0
    # Pre-drain: queue line written but goal.md unchanged.
    qpath = tools.operator_queue_path(cfg)
    assert qpath.exists() and qpath.read_text().strip()

    res = tools.drain_operator_queue(cfg)
    assert res["applied"] == 1
    assert (cfg.project_root / "goal.md").read_text() == _UPDATE_GOAL_PAYLOAD


def test_cmd_update_goal_stdin_dispatches(tmp_path: Path, monkeypatch):
    """`--file -` reads the goal payload from stdin (mirrors `ap2 add
    --briefing-file -`)."""
    from ap2.cli import cmd_update_goal
    import io
    import sys

    cfg = _project(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_UPDATE_GOAL_PAYLOAD))

    rc = cmd_update_goal(
        cfg,
        Namespace(file="-", reason="rotated focus from migration"),
    )
    assert rc == 0
    tools.drain_operator_queue(cfg)
    assert (cfg.project_root / "goal.md").read_text() == _UPDATE_GOAL_PAYLOAD


def test_cmd_update_goal_reason_plumbed_through(tmp_path: Path):
    """`--reason "..."` rides on the queue payload and lands in the
    operator-log audit line `<ts> — operator updated goal.md (<reason>)`."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    payload_path = tmp_path / "goal_payload.md"
    payload_path.write_text(_UPDATE_GOAL_PAYLOAD)

    rc = cmd_update_goal(
        cfg,
        Namespace(
            file=str(payload_path),
            reason="rotate focus to ideation quality",
        ),
    )
    assert rc == 0
    tools.drain_operator_queue(cfg)

    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert (
        "operator updated goal.md (rotate focus to ideation quality)" in log
    )


def test_cmd_update_goal_empty_payload_rejected(tmp_path: Path, capsys):
    """Whitespace-only file is rejected with non-zero exit + a hint
    message — the CLI defends against a path-vs-content typo before
    reaching the queue-append validator."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n\n")

    rc = cmd_update_goal(
        cfg, Namespace(file=str(empty), reason=None)
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "empty" in err.lower()
    # No queue line written.
    qpath = tools.operator_queue_path(cfg)
    assert not qpath.exists() or qpath.read_text() == ""


def test_cmd_update_goal_too_large_rejected(tmp_path: Path, capsys):
    """Soft 100KB cap surfaces a path-vs-content mistake (e.g. operator
    pointed at a log file). Refused before the queue-append call."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    big = tmp_path / "big.md"
    big.write_text("# huge\n" + ("x" * 200_000))

    rc = cmd_update_goal(
        cfg, Namespace(file=str(big), reason=None)
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "100000" in err or "cap" in err.lower()


def test_cmd_update_goal_missing_file_errors(tmp_path: Path, capsys):
    """Bad path → non-zero exit + readable error. We don't want a stray
    OSError traceback bubbling up to the operator."""
    from ap2.cli import cmd_update_goal

    cfg = _project(tmp_path)
    rc = cmd_update_goal(
        cfg,
        Namespace(file=str(tmp_path / "does_not_exist.md"), reason=None),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "ap2 update-goal" in err


# ---------------------------------------------------------------------------
# TB-189: cmd_classify — operator-authored retrospective verdict on a
# shipped proposal. Routes through the operator queue; the drain-side
# writes both an operator_log.md audit line AND an `impact` block to
# the per-proposal record from TB-188.


def test_classify_writes_operator_log_line(tmp_path: Path):
    """Briefing-spec verification: `ap2 classify TB-N --impact
    advanced-goal --reason "..."` exits 0, queues a `classify` record,
    and drains to the expected operator_log.md line shape (`classified
    TB-N impact=advanced-goal: ...`)."""
    from ap2.cli import cmd_classify

    cfg = _project(tmp_path)
    # Seed the task on the board so the queue-append snapshot check
    # accepts it (cmd_classify validates TB-N is on the board, same
    # symmetry as reject / delete).
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-840", title="shipped proposal")
    board.save()

    rc = cmd_classify(
        cfg,
        Namespace(
            task_id="TB-840",
            impact="advanced-goal",
            reason="closed the diagnostic gap that ideation flagged in cycle 12",
        ),
    )
    assert rc == 0
    _drain(cfg)

    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "classified TB-840 impact=advanced-goal" in log
    assert "closed the diagnostic gap that ideation flagged in cycle 12" in log


def test_classify_invalid_verdict_exits_nonzero(tmp_path: Path, capsys):
    """Briefing-spec verification: `ap2 classify TB-N --impact bogus`
    exits non-zero and does not queue any record. The CLI validates
    against `IMPACT_VERDICTS` before reaching the queue-append handler."""
    from ap2.cli import cmd_classify

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-841", title="any task")
    board.save()

    rc = cmd_classify(
        cfg,
        Namespace(task_id="TB-841", impact="bogus", reason=None),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "advanced-goal" in err
    assert "pro-forma" in err
    # No queue file written (or the queue file is empty of classify ops).
    queue_path = tools.operator_queue_path(cfg)
    if queue_path.exists():
        for line in queue_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            import json as _json
            rec = _json.loads(line)
            assert rec.get("op") != "classify", (
                f"unexpectedly queued a classify rec on bogus verdict: {rec!r}"
            )


def test_classify_without_reason_omits_reason_part(tmp_path: Path):
    """A classify with `--reason` omitted writes the operator_log line
    without a trailing colon-space-empty: `classified TB-N
    impact=<verdict>` (no `: <reason>`). Itself signal — operator who
    classified without a reason."""
    from ap2.cli import cmd_classify

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-842", title="quiet classification")
    board.save()

    rc = cmd_classify(
        cfg,
        Namespace(task_id="TB-842", impact="pro-forma", reason=None),
    )
    assert rc == 0
    _drain(cfg)

    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    # The line must NOT carry `: ` after the verdict (no reason → no colon).
    assert "classified TB-842 impact=pro-forma\n" in log


def test_classify_unknown_task_returns_error(tmp_path: Path, capsys):
    """Symmetry with reject / delete — unknown TB-N is operator error
    surfaced at append time (the snapshot validation under the board
    lock)."""
    from ap2.cli import cmd_classify

    cfg = _project(tmp_path)
    rc = cmd_classify(
        cfg,
        Namespace(task_id="TB-9999", impact="advanced-goal", reason="x"),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not on board" in err


def test_status_renders_classifications_30d(tmp_path: Path, capsys):
    """Briefing-spec verification: `ap2 status --json` includes
    `classifications_last_30d_by_verdict` with the four integer keys
    (TB-251 added `negative`). Always-present (zeros for fresh
    projects); populated after a classify lands."""
    from ap2.cli import cmd_status, cmd_classify
    import json as _json

    cfg = _project(tmp_path)
    # Empty state: JSON carries the dict with zeros.
    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert "classifications_last_30d_by_verdict" in out
    assert out["classifications_last_30d_by_verdict"] == {
        "advanced-goal": 0,
        "pro-forma": 0,
        "negative": 0,
        "unclear": 0,
    }

    # Now land one classify and re-check.
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-850", title="for the count")
    board.save()
    cmd_classify(
        cfg,
        Namespace(task_id="TB-850", impact="pro-forma", reason="no diff"),
    )
    _drain(cfg)
    # Drain the cmd_classify "queued classify..." print so capsys.out
    # below contains ONLY the cmd_status JSON.
    capsys.readouterr()

    rc = cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    counts = out["classifications_last_30d_by_verdict"]
    assert counts["pro-forma"] == 1
    assert counts["advanced-goal"] == 0
    assert counts["negative"] == 0
    assert counts["unclear"] == 0


def test_status_text_renders_classifications_line_when_present(
    tmp_path: Path, capsys,
):
    """Text-mode status renders the `classifications last 30d:
    advanced-goal=<n>, pro-forma=<m>, negative=<k>, unclear=<j>` line
    when at least one classification lives in the window. Empty windows
    skip the line entirely (no zero-noise on fresh projects). TB-251
    added `negative` as the fourth bucket; the renderer iterates
    `IMPACT_VERDICTS` so all four bucket counts appear with `=0` for
    any verdict with no observations."""
    from ap2.cli import cmd_status, cmd_classify

    cfg = _project(tmp_path)
    # Empty window: the line is absent.
    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "classifications last 30d" not in out

    # Populated window: the line shows the counts.
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-851", title="advanced one")
    board.add("Complete", task_id="TB-852", title="pro-forma one")
    board.save()
    cmd_classify(
        cfg, Namespace(task_id="TB-851", impact="advanced-goal", reason="ok"),
    )
    cmd_classify(
        cfg, Namespace(task_id="TB-852", impact="pro-forma", reason=None),
    )
    _drain(cfg)
    # Drop the cmd_classify queued-classify prints from capsys so the
    # status text comparison below is clean.
    capsys.readouterr()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "classifications last 30d:" in out
    assert "advanced-goal=1" in out
    assert "pro-forma=1" in out
    assert "negative=0" in out
    assert "unclear=0" in out


def test_impact_verdicts_enum_stable():
    """Briefing-spec pin: the `IMPACT_VERDICTS` tuple is exposed and
    the four values are exactly what goal.md L61-76 plus TB-251 name.
    Adding values is welcome (one-line tuple edit) but must not
    silently rename or drop any of the current values — downstream
    consumers (per-proposal record `impact.verdict`, operator_log
    line shape, status counter keys) rely on the literal strings."""
    assert tools.IMPACT_VERDICTS == (
        "advanced-goal",
        "pro-forma",
        "negative",
        "unclear",
    )


def test_impact_verdicts_tuple_length():
    """TB-251 regression-pin: explicit `len(IMPACT_VERDICTS) == 4`
    check so an accidental removal in a future refactor (e.g. someone
    rolling back to the 3-bucket vocabulary) trips a clearly-named
    test rather than only the broader enum-stable comparison above.
    Tuple shape is the contract."""
    assert len(tools.IMPACT_VERDICTS) == 4
    assert "negative" in tools.IMPACT_VERDICTS


@pytest.mark.parametrize(
    "verdict",
    ["advanced-goal", "pro-forma", "negative", "unclear"],
)
def test_classify_accepts_each_impact_verdict(
    tmp_path: Path, capsys, verdict: str,
):
    """TB-251: each of the 4 verdicts is accepted by `ap2 classify`
    without validation error — the queue op is generated and the
    drain handler lands it in operator_log.md. Parameterized so
    every bucket flows the same path (no special-case for
    `negative`)."""
    from ap2.cli import cmd_classify

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Complete", task_id="TB-860", title="for verdict gradient")
    board.save()
    rc = cmd_classify(
        cfg,
        Namespace(task_id="TB-860", impact=verdict, reason=f"checking {verdict}"),
    )
    assert rc == 0
    _drain(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert f"classified TB-860 impact={verdict}" in log


def test_classify_rejects_invalid_verdict(tmp_path: Path, capsys):
    """TB-251: an invalid `--impact` value is rejected by argparse
    (via `choices=`), exits non-zero, and the error names the 4
    valid choices so the operator sees the full menu in the failure
    message."""
    from ap2.cli import build_parser

    cfg = _project(tmp_path)
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(
            [
                "--project",
                str(cfg.project_root),
                "classify",
                "TB-861",
                "--impact",
                "bogus",
            ]
        )
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    # All four valid choices appear in the argparse error message.
    for v in ("advanced-goal", "pro-forma", "negative", "unclear"):
        assert v in err


def test_classifications_last_30d_renders_all_4_verdicts(
    tmp_path: Path, capsys,
):
    """TB-251: seed events for each of the 4 verdicts; assert the
    text-mode status line lists all 4 with correct counts. Pins the
    renderer to iterate `IMPACT_VERDICTS` (so adding a verdict to the
    tuple flows through without a render edit)."""
    from ap2.cli import cmd_status, cmd_classify

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    for i, _ in enumerate(tools.IMPACT_VERDICTS):
        board.add("Complete", task_id=f"TB-87{i}", title=f"bucket {i}")
    board.save()
    for i, v in enumerate(tools.IMPACT_VERDICTS):
        cmd_classify(
            cfg, Namespace(task_id=f"TB-87{i}", impact=v, reason=None),
        )
    _drain(cfg)
    capsys.readouterr()

    rc = cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "classifications last 30d:" in out
    for v in tools.IMPACT_VERDICTS:
        assert f"{v}=1" in out


# ---------------------------------------------------------------------------
# TB-201: cmd_ack — operator-decision channel for ideation. Routes
# through the operator queue rather than writing operator_log.md
# synchronously (the pre-TB-201 in-place write tripped TB-110's
# post-hoc fenced-file snapshot check on any task agent running
# concurrently with the operator's `ap2 ack`).


def test_ack_queues_and_does_not_write_operator_log(tmp_path: Path, capsys):
    """Briefing-spec verification: `cmd_ack` exits 0, queues exactly one
    record with `op="ack"` carrying the supplied note + task_id, leaves
    `operator_log.md` UNMODIFIED (write happens at drain time), and prints
    the documented "queued ack" shape (≤200 chars, contains 'queued')."""
    from ap2.cli import cmd_ack
    import json as _json

    cfg = _project(tmp_path)
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    pre_existed = log_path.exists()
    pre_text = log_path.read_text() if pre_existed else ""

    rc = cmd_ack(
        cfg,
        Namespace(
            note="closed TB-200 follow-up — retried on dev box, no repro",
            task=None,
        ),
    )
    assert rc == 0

    # operator_log.md is unchanged (regression bar — the whole point of
    # the TB-201 retrofit).
    assert log_path.exists() == pre_existed
    if pre_existed:
        assert log_path.read_text() == pre_text

    # The queue carries exactly one `ack` record with the note verbatim.
    queue_path = tools.operator_queue_path(cfg)
    recs = [
        _json.loads(ln)
        for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    ack_recs = [r for r in recs if r.get("op") == "ack"]
    assert len(ack_recs) == 1
    assert ack_recs[0]["args"]["note"].startswith("closed TB-200 follow-up")
    assert ack_recs[0]["args"]["task_id"] == ""

    # CLI output matches the "queued ack" UX shape.
    out = capsys.readouterr().out.strip()
    assert "queued" in out
    assert len(out) <= 200


def test_ack_with_task_id_records_task_id_on_queue_record(tmp_path: Path):
    """Briefing-spec verification: `-t TB-N` rides on the queued
    record's args as `task_id`; the drain-side picks it up to render
    the `[TB-N]` tag in operator_log.md."""
    from ap2.cli import cmd_ack
    import json as _json

    cfg = _project(tmp_path)
    rc = cmd_ack(
        cfg,
        Namespace(note="LaunchAgent installed", task="TB-139"),
    )
    assert rc == 0
    queue_path = tools.operator_queue_path(cfg)
    recs = [
        _json.loads(ln)
        for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    ack_recs = [r for r in recs if r.get("op") == "ack"]
    assert ack_recs[0]["args"]["task_id"] == "TB-139"

    # Drain → operator_log.md gets the bullet with `[TB-139]`.
    _drain(cfg)
    log = (cfg.project_root / ".cc-autopilot" / "operator_log.md").read_text()
    assert "[TB-139] — LaunchAgent installed" in log


def test_ack_drain_writes_operator_log_and_event(tmp_path: Path):
    """Briefing-spec verification: synthesize a queued ack, run
    `drain_operator_queue`, assert (a) `operator_log.md` carries the
    bullet line in the documented `- <ts> [TB-N] — <note>` shape, (b)
    events.jsonl carries an `operator_ack` event with the note + task
    fields, (c) the audit line `applied operator-queued ack → TB-N`
    is also written to operator_log.md (the verb-vs-other-ops audit
    pointer that other queue-routed ops also emit)."""
    from ap2 import events as _events
    cfg = _project(tmp_path)
    res = tools.enqueue_operator_ack(
        cfg, {"note": "operator ate the frog", "task_id": "TB-9"}
    )
    assert not res.get("isError"), res

    _drain(cfg)

    log = (
        cfg.project_root / ".cc-autopilot" / "operator_log.md"
    ).read_text()
    # (a) ack bullet line in the historical shape.
    import re
    bullet_re = re.compile(
        r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[TB-9\] — operator ate the frog$",
        re.MULTILINE,
    )
    assert bullet_re.search(log), (
        f"ack bullet not found in operator_log.md:\n{log}"
    )
    # (c) the standard `applied operator-queued ack → TB-9` audit line
    # is ALSO present — same shape as every other queue-routed op.
    audit_re = re.compile(
        r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z — "
        r"applied operator-queued ack → TB-9$",
        re.MULTILINE,
    )
    assert audit_re.search(log), (
        f"applied-audit line not found in operator_log.md:\n{log}"
    )

    # (b) events.jsonl carries the `operator_ack` event with the
    # note + task fields.
    evts = _events.tail(cfg.events_file, 20)
    ack_evts = [e for e in evts if e["type"] == "operator_ack"]
    assert len(ack_evts) == 1
    assert ack_evts[0]["note"] == "operator ate the frog"
    assert ack_evts[0]["task"] == "TB-9"


def test_ack_mcp_tool_path_queues_same_record(tmp_path: Path):
    """Briefing-spec verification: invoke the `operator_log_append` MCP
    tool body (`enqueue_operator_ack`) — the chat-handler entry point —
    and assert it produces the same queue-append record as the CLI
    path. Verifies the post-TB-201 MCP tool body queues rather than
    writing operator_log.md synchronously."""
    import json as _json

    cfg = _project(tmp_path)
    res = tools.enqueue_operator_ack(
        cfg,
        {"note": "chat-handler ack from claude-bot", "task_id": "TB-77"},
    )
    assert not res.get("isError"), res

    queue_path = tools.operator_queue_path(cfg)
    recs = [
        _json.loads(ln)
        for ln in queue_path.read_text().splitlines() if ln.strip()
    ]
    ack_recs = [r for r in recs if r.get("op") == "ack"]
    assert len(ack_recs) == 1
    assert ack_recs[0]["args"]["note"] == "chat-handler ack from claude-bot"
    assert ack_recs[0]["args"]["task_id"] == "TB-77"

    # Pre-drain: operator_log.md is NOT written. The TB-201 regression
    # bar — chat-driven acks (the MCP path) must defer the write the
    # same way the CLI path does, since the same false-positive
    # state_violation cascade applies regardless of which surface
    # initiated the ack.
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    if log_path.exists():
        assert "chat-handler ack from claude-bot" not in log_path.read_text()


def test_ack_no_state_violation_during_in_flight_task(tmp_path: Path):
    """TB-201 regression pin (the load-bearing test): synthesize a
    state where a task agent's run window encloses an `ap2 ack` call.
    Pre-TB-201 this rolled the task back via TB-110's post-hoc
    fenced-file snapshot check. Post-TB-201 the ack is in the queue,
    NOT in operator_log.md — the file's hash is unchanged until the
    drain runs (which the daemon runs at tick boundary, BEFORE
    dispatching the next task).

    Concretely: take `rollback.snapshot_fenced_files` (the daemon's
    pre-task snapshot), run `cmd_ack`, take the post snapshot via
    `rollback.detect_fenced_violations` — assert NO change to
    `.cc-autopilot/operator_log.md` is detected."""
    from ap2 import rollback
    from ap2.cli import cmd_ack
    from ap2.board import Board

    cfg = _project(tmp_path)
    # Seed a fake Active task to make the scenario realistic — the
    # operator's "I just acked while TB-77 is running" case.
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    # Pre-task snapshot — what `daemon._run_task` takes before
    # dispatching the agent.
    pre = rollback.snapshot_fenced_files(cfg)

    # Operator runs `ap2 ack` while TB-77 is running. Pre-TB-201 this
    # would write operator_log.md synchronously; post-TB-201 it
    # queues an op instead.
    rc = cmd_ack(
        cfg,
        Namespace(
            note="thinking out loud while TB-77 runs — looks fine so far",
            task=None,
        ),
    )
    assert rc == 0

    # Post-task snapshot — the violation detector compares the two.
    violations = rollback.detect_fenced_violations(cfg, pre)

    # operator_log.md is the file the pre-TB-201 path would dirty.
    # Assert it's NOT in the violation list — the regression bar.
    assert ".cc-autopilot/operator_log.md" not in violations, (
        f"operator_log.md tripped TB-110's post-hoc snapshot check "
        f"even though the ack went through the queue (violations={violations})"
    )


def test_operator_log_append_mcp_name_unchanged(tmp_path: Path):
    """TB-201 backwards-compat pin: the `operator_log_append` MCP
    tool's external name (the string chat handlers send when invoking
    `@claude-bot done: ...`) stays unchanged from pre-TB-201. Only
    the tool's body changes — to call `enqueue_operator_ack` instead
    of `do_operator_log_append`. Pinning the name keeps chat-side
    callers working without recompiling their tool-call dispatch."""
    # The tool name lands in CONTROL_AGENT_TOOLS prefixed with
    # `mcp__autopilot__`. The bare tool name (what `@tool(...)`
    # registers) is what chat handlers see.
    assert "mcp__autopilot__operator_log_append" in tools.CONTROL_AGENT_TOOLS
    # And it's NOT in TASK_AGENT_TOOLS — operator-mediated only.
    assert "mcp__autopilot__operator_log_append" not in tools.TASK_AGENT_TOOLS


# ---------------------------------------------------------------------------
# TB-202: `ap2 backfill-proposals` and `ap2 cron edit` both write fenced
# files synchronously (bypassing the operator-queue routing pattern). If
# the operator runs either while a task agent is in flight, the
# TB-110 post-hoc snapshot diff detects the fenced-file mutation and
# rolls the task back — same false-positive cascade as the pre-TB-201
# `ap2 ack` path. TB-202's cheaper-than-queue-routing mitigation is a
# pre-flight refuse-if-active check on both verbs; these tests pin the
# refusal text, the exit code, and the "fenced state untouched on
# refuse" invariant.


def test_backfill_proposals_refuses_when_active_task_present(tmp_path: Path, capsys):
    """TB-202: with a synthetic Active task on the board,
    `cmd_backfill_proposals` exits non-zero and stderr names the
    refuse-if-active rationale. Mirrors the pattern of TB-201's
    `test_ack_no_state_violation_during_in_flight_task` — except here
    the gate is at the CLI entry, not via queue deferral."""
    from ap2.cli import cmd_backfill_proposals

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    rc = cmd_backfill_proposals(cfg, Namespace(dry_run=False))
    assert rc == 1

    err = capsys.readouterr().err
    # Briefing's verification pin: message names the verb, the
    # current-task state, and the refusal verb.
    assert "backfill-proposals" in err
    assert "active" in err.lower()
    assert "refusing" in err.lower()
    # The Active task's TB-N is surfaced so the operator can map the
    # refusal back to a concrete in-flight task.
    assert "TB-77" in err


def test_backfill_proposals_refuse_does_not_mutate_fenced_dir(tmp_path: Path):
    """TB-202 invariant: the refuse path leaves
    `.cc-autopilot/ideation_proposals/` untouched — no new records, no
    file mtimes bumped. Pin captures the directory's file list before
    and after the refuse fires and asserts it's identical.

    Why this matters: the whole point of the gate is to prevent the
    fenced-path write from racing the in-flight task's snapshot
    window. A regression that bypasses the gate (e.g. running
    `backfill_proposals` AFTER the refuse-fires check) would
    re-introduce the rollback cascade."""
    from ap2.cli import cmd_backfill_proposals

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    proposals_dir = cfg.project_root / ".cc-autopilot" / "ideation_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    before = sorted(p.name for p in proposals_dir.iterdir())

    rc = cmd_backfill_proposals(cfg, Namespace(dry_run=False))
    assert rc == 1

    after = sorted(p.name for p in proposals_dir.iterdir())
    assert before == after, (
        f"backfill-proposals refuse path mutated "
        f".cc-autopilot/ideation_proposals/: before={before} after={after}"
    )


def test_backfill_proposals_succeeds_with_empty_active(tmp_path: Path):
    """TB-202 happy path: when board's Active section is empty, the
    refuse-if-active gate falls through and `cmd_backfill_proposals`
    runs normally. Uses the TB-195 zero-records baseline (no
    operator_log entries / no briefings) so the underlying
    `backfill_proposals` call is a no-op; what we pin is the gate not
    blocking."""
    from ap2.cli import cmd_backfill_proposals

    cfg = _project(tmp_path)
    # init_project leaves Active empty; explicit assertion documents
    # the precondition.
    board = Board.load(cfg.tasks_file)
    assert list(board.iter_tasks("Active")) == []

    rc = cmd_backfill_proposals(cfg, Namespace(dry_run=False))
    assert rc == 0


def test_cron_edit_refuses_when_active_task_present(tmp_path: Path, capsys):
    """TB-202: `cmd_cron_edit` (the `ap2 cron edit ...` handler)
    refuses with stderr naming the cron.yaml fenced path when a task
    is Active. Symmetric to the backfill-proposals refuse pin."""
    from ap2.cli import cmd_cron_edit

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    rc = cmd_cron_edit(
        cfg,
        Namespace(
            action="add",
            name="weekly-perf",
            interval="1d",
            prompt="run perf",
            active_when=None,
            max_turns=None,
        ),
    )
    assert rc == 1

    err = capsys.readouterr().err
    # Message names the verb ("cron edit"), the active state, and the
    # refusal verb (verification bullet's literal expectations).
    assert "cron" in err.lower()
    assert "active" in err.lower()
    assert "refusing" in err.lower()
    assert "TB-77" in err


def test_cron_edit_refuse_does_not_mutate_cron_yaml(tmp_path: Path):
    """TB-202 invariant: the cron-edit refuse path leaves
    `.cc-autopilot/cron.yaml` untouched. Mirrors the
    backfill-proposals invariant — captures the file's content before
    and after, assertions on equality. cron.yaml is fenced and is the
    rollback-trigger surface for this CLI verb."""
    from ap2.cli import cmd_cron_edit

    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-77", title="running task")
    board.save()

    cron_yaml = cfg.cron_file
    # Init writes a default cron.yaml; capture its bytes verbatim.
    before_bytes = cron_yaml.read_bytes() if cron_yaml.exists() else None

    rc = cmd_cron_edit(
        cfg,
        Namespace(
            action="add",
            name="weekly-perf",
            interval="1d",
            prompt="run perf",
            active_when=None,
            max_turns=None,
        ),
    )
    assert rc == 1

    after_bytes = cron_yaml.read_bytes() if cron_yaml.exists() else None
    assert before_bytes == after_bytes, (
        "cron edit refuse path mutated cron.yaml — the fenced-write "
        "gate is leaking past the refuse-if-active check"
    )


def test_cron_edit_succeeds_with_empty_active(tmp_path: Path):
    """TB-202 happy path: with empty Active, `cmd_cron_edit` falls
    through the gate and mutates cron.yaml normally (the underlying
    `do_cron_edit` handler — same one exercised in
    `test_tools.test_cron_edit_add_and_remove`). Adds + removes a job
    and asserts both ops return 0."""
    from ap2.cli import cmd_cron_edit

    cfg = _project(tmp_path)
    # Default Active is empty.

    rc = cmd_cron_edit(
        cfg,
        Namespace(
            action="add",
            name="tb-202-test",
            interval="1h",
            prompt="run a thing",
            active_when=None,
            max_turns=None,
        ),
    )
    assert rc == 0

    jobs = {j.name for j in load_jobs(cfg.cron_file)}
    assert "tb-202-test" in jobs

    rc = cmd_cron_edit(
        cfg,
        Namespace(
            action="remove",
            name="tb-202-test",
            interval=None,
            prompt=None,
            active_when=None,
            max_turns=None,
        ),
    )
    assert rc == 0

    jobs = {j.name for j in load_jobs(cfg.cron_file)}
    assert "tb-202-test" not in jobs
