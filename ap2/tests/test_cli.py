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
    Ideation surfaces these in the open-questions list."""
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
) -> Namespace:
    """Build a Namespace shaped like cmd_add's argparse output.

    TB-135: the positional `title`, `-d/--description` are gone — title /
    description live in the briefing. `_add_args` no longer accepts them.
    TB-132: `--blocked CSV` writes a `@blocked:<csv>` codespan onto the
    rendered task line.
    """
    return Namespace(
        section=section,
        tags=tags,
        briefing_file=briefing_file,
        no_verify=no_verify,
        blocked=blocked,
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
    brief.write_text(
        "# TB-99 — Real title here\n\n"
        "## Goal\n\nstub\n\nWhy now: closes the failure mode named in the briefing scope.\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nedit\n\n"
        "## Verification\n- `uv run pytest -q` — passes\n\n"
        "## Out of scope\n\n- nothing\n"
    )

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
) -> Namespace:
    """Build a Namespace shaped like cmd_update's argparse output."""
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
    new_brief.write_text(
        "# Updated\n\n"
        "## Goal\n\nbetter\n\n"
        "Why now: closes the failure mode named in the briefing scope.\n\n"
        "## Scope\n\n- foo.py\n\n"
        "## Design\n\nedit\n\n"
        "## Verification\n- `pytest`\n\n"
        "## Out of scope\n\n- nothing\n"
    )
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

    new_briefing = (
        "# Stdin briefing\n\n"
        "## Goal\n\ng\n\n"
        "Why now: closes the failure mode named in the briefing scope.\n\n"
        "## Scope\n\n- f\n\n"
        "## Design\n\nd\n\n"
        "## Verification\n- `t`\n\n"
        "## Out of scope\n\n- n\n"
    )
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


def test_cmd_ideate_refuses_when_active_task_present(tmp_path: Path, capsys):
    """Default refusal: a non-empty Active section (task in flight) blocks
    forced ideate. The error message names `--force`. No queue record is
    appended."""
    cfg = _project(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add("Active", task_id="TB-2000", title="in flight")
    board.save()

    rc = cmd_ideate(cfg, Namespace(force=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "Active" in err
    assert "--force" in err

    queue_path = tools.operator_queue_path(cfg)
    if queue_path.exists():
        lines = [ln for ln in queue_path.read_text().splitlines() if ln.strip()]
        assert lines == [], "no queue record should have been appended"


def test_cmd_ideate_force_overrides_active_refusal(tmp_path: Path):
    """`ap2 ideate --force` bypasses the Active-task refusal and queues
    the record. `force=true` rides on the queue payload."""
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
