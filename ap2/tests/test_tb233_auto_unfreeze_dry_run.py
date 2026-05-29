"""TB-233: behavioral pinning for the monitor-only auto-unfreeze dry-run
on-ramp (`AP2_AUTO_UNFREEZE_DRY_RUN=1`).

Sibling on-ramp to TB-232's `AP2_AUTO_APPROVE_DRY_RUN=1` for the axis-2
auto-unfreeze loop. The end-to-end automation **Current focus: end-to-end
automation** (goal.md L38-151, axis 2 "Failure-recovery operator
dependency", L90-100) currently has TB-225's auto-unfreeze loop fully
active OR fully off — no monitor-only path. When both
`AP2_AUTO_UNFREEZE_FIX_SHAPES` is set AND `AP2_AUTO_UNFREEZE_DRY_RUN=1`,
`_maybe_auto_unfreeze` runs the entire guard chain (allowlist match +
per-task cap + per-day cap + briefing-line match) and, instead of calling
`_apply_auto_unfreeze_patch`, emits a `would_auto_unfreeze` event with the
same payload shape as `auto_unfreeze_applied` plus the `file` + `line`
fields. The board stays untouched; no operator-queue ops are appended.
Operator observes the decisions in `ap2 logs --type would_auto_unfreeze`
for a window, gains confidence, then flips dry-run off.

Three behavioral cases (briefing's `## Verification` prose Scope 6):

  (a) DRY_RUN=1 + populated allowlist + Frozen task with a matching
      `BriefingFix:` summary → asserts `would_auto_unfreeze` event
      emitted, no `auto_unfreeze_applied` event, briefing file content
      unchanged, no operator-queue ops appended.
  (b) dry-run + per-task-cap-reached → asserts the existing
      `auto_unfreeze_skipped reason=per_task_cap` event still fires AND
      no `would_auto_unfreeze` for that task (skip wins over dry-run,
      same precedence as the non-dry-run path).
  (c) dry-run + per-day-cap-reached → asserts the systemic-regression
      `## Decisions needed from operator` bullet is NOT appended in
      dry-run (board/state untouched). The `auto_unfreeze_skipped
      reason=per_day_cap` event still fires (skip emission is preserved
      in dry-run); the short-circuit semantics are preserved too.

Plus a direct helper unit pin on `_auto_unfreeze_dry_run()` parsing and a
default-off byte-identical pin (dry-run unset → behavior identical to
pre-TB-233, validated by re-using the TB-225 happy path expectations).

Test shape mirrors `test_tb225_auto_unfreeze.py` (the auto-unfreeze loop)
and `test_tb232_auto_approve_dry_run.py` (the axis-1 dry-run sibling).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import daemon, events, tools
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# Minimal goal.md so the briefing structural validator + goal-anchor gate
# don't false-positive when we exercise the add-task path via the
# operator queue. Mirrors `_GOAL_MD` in test_tb225_auto_unfreeze.py.
_GOAL_MD = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away "
    "without intervention.\n\n"
    "## Current focus: end-to-end automation\n\n"
    "Close the manual-approval bottleneck plus failure-recovery gaps.\n\n"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


# Briefing whose `## Verification` has a `grep -lE` shell bullet we can
# patch in the dry-run cases. Mirrors `_BRIEFING` in
# test_tb225_auto_unfreeze.py exactly so the structural validator agrees.
_BRIEFING = (
    "# TB-233 fixture briefing\n\n"
    "## Goal\n\n"
    "Monitor-only on-ramp pin for the axis-2 auto-unfreeze loop "
    "(`## Current focus: end-to-end automation`) so the dry-run path "
    "can be observed without committing to the binary cliff.\n\n"
    "Why now: closes the failure-recovery operator dependency on a "
    "binary on/off switch — without dry-run the operator's first "
    "allowlist deployment mutates real briefings.\n\n"
    "## Scope\n\n"
    "- ap2/daemon.py\n\n"
    "## Design\n\n"
    "Direct edit.\n\n"
    "## Verification\n"
    "- `grep -lE 'pattern' ap2/tests/` — matches at least one file.\n\n"
    "## Out of scope\n\n"
    "- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Project root with the standard ap2 init layout + a real goal.md.

    TB-327: strip every `AP2_*` env knob BEFORE `Config.load` so the
    cfg snapshot doesn't carry a stale `AP2_AUTO_UNFREEZE_*` value
    from the parent process (mirror of the TB-326 `clean_env` shape).
    """
    import os
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _add_and_freeze(cfg: Config, *, title: str = "tb233 fixture") -> tuple[str, Path]:
    """Add a Backlog task with `_BRIEFING`, then move it directly to Frozen
    via the operator queue + drain. Returns `(task_id, briefing_path)`.

    Mirrors `_add_and_freeze` in `test_tb225_auto_unfreeze.py` — same
    on-disk shape the daemon's auto-unfreeze sweep encounters at run time.
    """
    res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "add_backlog",
            "title": title,
            "briefing": _BRIEFING,
        },
    )
    info = _unwrap(res)
    task_id = info["task_id"]
    tools.drain_operator_queue(cfg)
    board = Board.load(cfg.tasks_file)
    task = board.get(task_id)
    assert task is not None and task.briefing, (
        f"fixture: {task_id} has no briefing path after add_backlog drain"
    )
    briefing_path = cfg.project_root / task.briefing
    assert briefing_path.exists(), f"briefing not on disk: {briefing_path}"
    tools.do_board_edit(
        cfg,
        {"action": "move_to_frozen", "task_id": task_id},
    )
    return task_id, briefing_path


def _emit_blocked_complete(
    cfg: Config, *, task_id: str, summary: str,
) -> None:
    """Append a `task_complete status=blocked` event with the given
    summary so the auto-unfreeze sweep has something to parse."""
    events.append(
        cfg.events_file,
        "task_complete",
        task=task_id,
        status="blocked",
        commit="",
        summary=summary,
    )


def _briefing_fix_line(
    *, shape: str, path: str, line: int, frm: str, to: str,
) -> str:
    """Render the canonical `BriefingFix:` prefix. Agent-prompt contract
    is `BriefingFix: <shape> at <path>:<line>: <from> -> <to>`."""
    return f"BriefingFix: {shape} at {path}:{line}: {frm} -> {to}"


def _read_queue(cfg: Config) -> list[dict]:
    """Read the operator queue file and return its entries (or empty
    when the file doesn't yet exist). Lets the (a) test pin "no
    operator-queue ops appended" without depending on internal state.
    """
    qpath = tools.operator_queue_path(cfg)
    if not qpath.exists():
        return []
    out: list[dict] = []
    for line in qpath.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ===========================================================================
# Direct helper unit pin: `_auto_unfreeze_dry_run()` parsing contract.
# ===========================================================================


def test_dry_run_helper_default_off_and_truthy_parse(
    cfg: Config, monkeypatch,
):
    """`AP2_AUTO_UNFREEZE_DRY_RUN` defaults to False (feature off);
    `1` / `true` / `yes` (any case) parse truthy. Anything else parses
    False so a typo doesn't silently enable a feature that's supposed
    to flip the WRITE step. Pins the briefing's Scope (1) parse shape.

    TB-327: helper now takes a `cfg` argument and resolves the env
    name via `Config.get_component_value`'s reverse-`FLAT_TO_SECTIONED`
    back-compat path, so the flat env name still wins end-to-end and
    the parse-shape pin holds.
    """
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    assert daemon._auto_unfreeze_dry_run(cfg) is False

    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "")
    assert daemon._auto_unfreeze_dry_run(cfg) is False

    for truthy in ("1", "true", "yes", "TRUE", "Yes", "TrUe"):
        monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", truthy)
        assert daemon._auto_unfreeze_dry_run(cfg) is True, (
            f"{truthy!r} must parse truthy"
        )

    for falsy in ("0", "false", "no", "off", "garbage", "  "):
        monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", falsy)
        assert daemon._auto_unfreeze_dry_run(cfg) is False, (
            f"{falsy!r} must parse falsy"
        )


# ===========================================================================
# (a) DRY_RUN=1 + populated allowlist + Frozen task with matching prefix
#     → would_auto_unfreeze emitted, no auto_unfreeze_applied, briefing
#     unchanged, no operator-queue ops appended.
# ===========================================================================


def test_a_dry_run_emits_would_event_and_leaves_state_untouched(
    cfg: Config, monkeypatch,
):
    """Full dry-run happy path: parser hits, allowlist matches, briefing-
    line match passes, but instead of patching + queueing ops, the
    daemon emits `would_auto_unfreeze` and leaves the briefing file +
    the operator queue untouched. Pins briefing Scope (2) end-to-end.
    """
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir,literal_backtick_in_shell_bullet",
    )
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    text_before = briefing_path.read_text()
    lines_before = text_before.splitlines()
    grep_line_idx = next(
        i for i, line in enumerate(lines_before) if "grep -lE" in line
    )

    # Capture the operator queue length BEFORE the sweep so we can
    # assert exactly-zero new entries land. (The fixture already
    # queued + drained the add_backlog op, so the queue may exist on
    # disk but its current length is the "no new ops" baseline.)
    queue_before = _read_queue(cfg)

    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=(
            "Agent diagnosis: grep -lE on a directory needs -r to recurse.\n"
            + _briefing_fix_line(
                shape="grep_missing_r_on_dir",
                path=rel,
                line=grep_line_idx + 1,
                frm="grep -lE",
                to="grep -rlE",
            )
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    would = [
        e for e in evts
        if e.get("type") == "would_auto_unfreeze"
        and e.get("task") == task_id
    ]
    assert len(would) == 1, (
        f"expected exactly one would_auto_unfreeze event; got: {would}"
    )
    # Payload mirrors auto_unfreeze_applied plus file + line fields.
    assert would[0]["shape"] == "grep_missing_r_on_dir"
    assert would[0]["file"] == rel
    assert would[0]["line"] == grep_line_idx + 1
    assert would[0]["from"] == "grep -lE"
    assert would[0]["to"] == "grep -rlE"

    # Real-application event must NOT fire in dry-run.
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    assert applied == [], (
        f"dry-run must NOT emit auto_unfreeze_applied; got: {applied}"
    )

    # Briefing file is byte-for-byte unchanged.
    text_after = briefing_path.read_text()
    assert text_after == text_before, (
        "dry-run must leave the briefing file untouched; got differing "
        f"content:\nBEFORE:\n{text_before}\nAFTER:\n{text_after}"
    )

    # No new operator-queue ops landed (no update / unfreeze for the
    # auto-heal path).
    queue_after = _read_queue(cfg)
    assert queue_after == queue_before, (
        f"dry-run must NOT append operator-queue ops; got new entries: "
        f"{queue_after[len(queue_before):]}"
    )

    # Task stays Frozen (the unfreeze op never queued, never drained).
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"dry-run must leave the task Frozen; got section={loc}"
    )


# ===========================================================================
# (b) dry-run + per-task-cap-reached → per_task_cap skip event fires AND
#     no would_auto_unfreeze for that task (skip wins over dry-run, same
#     precedence as the non-dry-run path).
# ===========================================================================


def test_b_dry_run_per_task_cap_skip_wins_over_dry_run(
    cfg: Config, monkeypatch,
):
    """Same precedence as the real-application path: when the per-task
    cap is exceeded, the `auto_unfreeze_skipped reason=per_task_cap`
    event fires and the dry-run write-step is NEVER reached, so no
    `would_auto_unfreeze` event lands for the task either. Pins
    briefing Scope (6b)."""
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "1")

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    grep_line_idx = next(
        i for i, line in enumerate(briefing_path.read_text().splitlines())
        if "grep -lE" in line
    )

    # Pre-seed one real auto_unfreeze_applied event for this task so
    # the per-task cap is already reached. (Only real applications
    # count toward the per-task cap; `would_auto_unfreeze` does not
    # increment it. This shape is realistic — the operator may have
    # already had one real auto-unfreeze land before enabling dry-run.)
    events.append(
        cfg.events_file,
        "auto_unfreeze_applied",
        task=task_id,
        shape="grep_missing_r_on_dir",
        **{"from": "grep -lE", "to": "grep -rlE"},
    )

    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=_briefing_fix_line(
            shape="grep_missing_r_on_dir",
            path=rel,
            line=grep_line_idx + 1,
            frm="grep -lE",
            to="grep -rlE",
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    skipped = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_skipped"
        and e.get("reason") == "per_task_cap"
        and e.get("task") == task_id
    ]
    assert len(skipped) == 1, (
        f"per_task_cap skip must fire in dry-run (skip precedes the "
        f"dry-run check); got: {skipped}"
    )

    would = [
        e for e in evts
        if e.get("type") == "would_auto_unfreeze"
        and e.get("task") == task_id
    ]
    assert would == [], (
        f"per_task_cap skip must short-circuit before the dry-run write "
        f"step; got would_auto_unfreeze events: {would}"
    )

    # And nothing was applied for real either.
    new_applied = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_applied"
        and e.get("task") == task_id
    ]
    assert len(new_applied) == 1, (
        f"only the pre-seeded auto_unfreeze_applied should exist; got: "
        f"{new_applied}"
    )

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen"


# ===========================================================================
# (c) dry-run + per-day-cap-reached → systemic-regression `## Decisions
#     needed from operator` bullet NOT appended in dry-run (board/state
#     untouched). The skip event still fires; short-circuit preserved.
# ===========================================================================


def test_c_dry_run_per_day_cap_halts_without_decisions_needed_bullet(
    cfg: Config, monkeypatch,
):
    """The per-day cap halt fires identically in dry-run (the skip event
    + the short-circuit return preserve the operator-visible pre-flight
    signal), but the `## Decisions needed from operator` bullet append
    — which mutates `.cc-autopilot/ideation_state.md` — is skipped
    because dry-run is monitor-only and must NOT touch board/state.
    Pins briefing Scope (6c) + Design point on "dry-run users get the
    same halt signal pre-flight"."""
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_DRY_RUN", "1")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "5")
    monkeypatch.setenv("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "2")

    # Seed 2 prior real auto_unfreeze_applied events within the last 24h
    # (the per-day cap counts real applications, not dry-run sims).
    events.append(
        cfg.events_file,
        "auto_unfreeze_applied",
        task="TB-900",
        shape="grep_missing_r_on_dir",
        **{"from": "grep -lE", "to": "grep -rlE"},
    )
    events.append(
        cfg.events_file,
        "auto_unfreeze_applied",
        task="TB-901",
        shape="grep_missing_r_on_dir",
        **{"from": "grep -lE", "to": "grep -rlE"},
    )

    # Snapshot ideation_state.md content BEFORE the sweep so we can
    # assert byte-equal AFTER. (The fixture's `cfg.ensure_dirs()`
    # creates the file lazily; we mirror that here by reading whatever
    # exists, defaulting to None if missing.)
    state_path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    text_before = state_path.read_text() if state_path.exists() else None

    task_id, briefing_path = _add_and_freeze(
        cfg, title="tb233 should be capped",
    )
    rel = str(briefing_path.relative_to(cfg.project_root))
    grep_line_idx = next(
        i for i, line in enumerate(briefing_path.read_text().splitlines())
        if "grep -lE" in line
    )
    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=_briefing_fix_line(
            shape="grep_missing_r_on_dir",
            path=rel,
            line=grep_line_idx + 1,
            frm="grep -lE",
            to="grep -rlE",
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 400)
    # Per-day-cap skip event fires (skip emission is preserved in dry-run).
    skipped = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_skipped"
        and e.get("reason") == "per_day_cap"
        and e.get("task") == task_id
    ]
    assert len(skipped) == 1, (
        f"per_day_cap skip event must fire in dry-run (operator's "
        f"pre-flight halt signal); got: {skipped}"
    )

    # No would_auto_unfreeze for this task (the per-day-cap halt
    # short-circuits before the dry-run write step, same as the real
    # path short-circuits before _apply_auto_unfreeze_patch).
    would = [
        e for e in evts
        if e.get("type") == "would_auto_unfreeze"
        and e.get("task") == task_id
    ]
    assert would == [], (
        f"per_day_cap halt must short-circuit before the dry-run write "
        f"step; got would_auto_unfreeze events: {would}"
    )

    # The `## Decisions needed from operator` bullet was NOT appended in
    # dry-run. ideation_state.md is byte-for-byte unchanged (or still
    # missing, if it didn't exist before).
    text_after = state_path.read_text() if state_path.exists() else None
    assert text_after == text_before, (
        f"dry-run must NOT mutate ideation_state.md; got:\n"
        f"BEFORE:\n{text_before!r}\nAFTER:\n{text_after!r}"
    )
    # Explicit anti-substring check on the bullet's distinctive phrase
    # so a future regression that emits the bullet via a different write
    # path still trips the test.
    if text_after is not None:
        assert "Auto-unfreeze daily cap" not in text_after, (
            f"dry-run must NOT append the auto-unfreeze daily-cap bullet "
            f"to ideation_state.md; got:\n{text_after}"
        )

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"per_day_cap halt must leave the task Frozen; got section={loc}"
    )


# ===========================================================================
# Default-off pin: dry-run unset → byte-identical to pre-TB-233 behavior.
# Re-uses TB-225's happy-path expectations (auto_unfreeze_applied fires,
# briefing patched on drain).
# ===========================================================================


def test_default_off_is_byte_identical_to_tb225_path(
    cfg: Config, monkeypatch,
):
    """When `AP2_AUTO_UNFREEZE_DRY_RUN` is unset, the auto-unfreeze sweep
    behaves identically to the TB-225 path: the real
    `_apply_auto_unfreeze_patch` runs, `auto_unfreeze_applied` fires,
    and the briefing gets patched on the next drain. Pins the briefing's
    Design point on "Default-off: when AP2_AUTO_UNFREEZE_DRY_RUN is
    unset, behavior is byte-identical to today"."""
    monkeypatch.delenv("AP2_AUTO_UNFREEZE_DRY_RUN", raising=False)
    monkeypatch.setenv(
        "AP2_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    grep_line_idx = next(
        i for i, line in enumerate(briefing_path.read_text().splitlines())
        if "grep -lE" in line
    )
    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=_briefing_fix_line(
            shape="grep_missing_r_on_dir",
            path=rel,
            line=grep_line_idx + 1,
            frm="grep -lE",
            to="grep -rlE",
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    applied = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_applied"
        and e.get("task") == task_id
    ]
    assert len(applied) == 1, (
        f"default-off (no DRY_RUN) must run the real apply path; got: "
        f"{applied}"
    )
    would = [
        e for e in evts
        if e.get("type") == "would_auto_unfreeze"
    ]
    assert would == [], (
        f"default-off must NOT emit would_auto_unfreeze; got: {would}"
    )

    # Drain to materialize the queued update + unfreeze ops.
    tools.drain_operator_queue(cfg)
    assert "grep -rlE" in briefing_path.read_text(), (
        "default-off path must patch the briefing on drain"
    )
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] in ("Backlog", "Ready"), (
        f"default-off path must move the task off Frozen after drain; "
        f"got section={loc}"
    )
