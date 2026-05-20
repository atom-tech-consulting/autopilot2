"""Tests for the cli-prefixed board-mutation verbs (TB-266 split from
`test_cli.py`).

Mirrors `ap2/cli_board.py` (TB-264 split): cmd_add / cmd_update /
cmd_backlog / cmd_unfreeze / cmd_delete / cmd_reject / cmd_approve /
cmd_classify. Verb groupings preserved from the pre-split section
headers — see the divider comments below for the TB-N each block
traces back to.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, retry, tools
from ap2.board import Board
from ap2.cli import (
    cmd_add,
    cmd_backlog,
    cmd_delete,
    cmd_reject,
    cmd_unfreeze,
)
from ap2.config import Config
from ap2.tests._briefing_fixtures import canonical_briefing
from ap2.tests.conftest import _drain, _project


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
