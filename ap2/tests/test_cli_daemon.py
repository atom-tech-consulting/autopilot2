"""Tests for the cli-prefixed daemon-lifecycle verbs (TB-266 split from
`test_cli.py`).

Mirrors `ap2/cli_daemon.py` (TB-264 split): cmd_start / cmd_stop /
cmd_status / cmd_pause / cmd_resume / cmd_web. Verb groupings preserved
from the pre-split section headers — see the divider comments below for
the TB-N each block traces back to.
"""
from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

from ap2 import events, tools
from ap2.board import Board
from ap2.cli import _require_oauth_token, cmd_start
from ap2.config import Config
from ap2.tests.conftest import _drain, _project


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
# TB-358 (axis 5): backend-aware daemon-start auth gate. `_require_oauth_token`
# now walks the resolved per-kind backend map and requires exactly the
# credentials that set implies — OAuth for any claude-backed kind, the
# OpenAI/codex credential for any codex-backed kind. The pre-axis-5 no-arg
# calls (the TB-79 tests above) keep their all-claude behavior.


def _clear_agent_backend_env(monkeypatch) -> None:
    """Drop every `AP2_AGENT_BACKEND_<KIND>` override so a test's cfg reads
    the all-claude default unless it sets an override explicitly."""
    from ap2.adapters.select import AGENT_KINDS

    for kind in AGENT_KINDS:
        monkeypatch.delenv(f"AP2_AGENT_BACKEND_{kind.upper()}", raising=False)


def test_require_oauth_token_all_claude_requires_only_oauth(
    tmp_path: Path, monkeypatch,
):
    """An all-claude backend map (the default) requires only OAuth — the
    OpenAI credential being absent does NOT block start. Identical to the
    pre-axis-5 install."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _require_oauth_token(cfg) == 0


def test_require_oauth_token_all_claude_refuses_without_oauth(
    tmp_path: Path, monkeypatch, capsys,
):
    """An all-claude map with OAuth missing still refuses — the cfg-passed
    path preserves the TB-79 requirement, naming the claude-backed kinds."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _require_oauth_token(cfg) == 1
    err = capsys.readouterr().err
    assert "CLAUDE_CODE_OAUTH_TOKEN" in err
    # OpenAI cred is not demanded when no kind is codex-backed.
    assert "OPENAI_API_KEY" not in err


def test_require_oauth_token_codex_kind_requires_openai_cred(
    tmp_path: Path, monkeypatch, capsys,
):
    """A codex-mapped kind (`AP2_AGENT_BACKEND_TASK=codex`) requires the
    OpenAI/codex credential even when OAuth is present (the other kinds are
    still claude-backed). The error names both the credential and the kind."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")

    rc = _require_oauth_token(cfg)
    assert rc == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err
    # The message names the codex-backed kind needing the credential.
    assert "task" in err


def test_require_oauth_token_codex_kind_passes_with_both_creds(
    tmp_path: Path, monkeypatch,
):
    """A mixed map (one codex kind, the rest claude) passes when BOTH the
    OAuth token and the OpenAI credential are present."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fake")
    monkeypatch.setenv("AP2_AGENT_BACKEND_TASK", "codex")
    assert _require_oauth_token(cfg) == 0


def test_require_oauth_token_all_codex_does_not_require_oauth(
    tmp_path: Path, monkeypatch,
):
    """Switching EVERY kind to codex drops the OAuth requirement entirely —
    the OpenAI credential alone suffices. This is the briefing's core
    guarantee: a codex-backed kind no longer hard-fails the OAuth-only
    gate."""
    _clear_agent_backend_env(monkeypatch)
    cfg = _project(tmp_path)
    from ap2.adapters.select import AGENT_KINDS

    for kind in AGENT_KINDS:
        monkeypatch.setenv(f"AP2_AGENT_BACKEND_{kind.upper()}", "codex")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fake")
    assert _require_oauth_token(cfg) == 0


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
# TB-139: `ap2 status` version-line surfaces (text + JSON). The
# `_git_suffix` / `_version_string` / `get_version` helper-level pins
# stay in `test_cli.py` since they're not tied to a single CLI verb.


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
# TB-189 / TB-251: `ap2 status` surfaces the operator's classify-verdict
# tallies for the last 30 days. The `cmd_classify` verb (and the
# `IMPACT_VERDICTS` enum it validates against) live in
# `test_cli_board.py`; here we only pin how cmd_status renders the
# counts.


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
