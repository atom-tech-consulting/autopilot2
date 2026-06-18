"""TB-421: the `grep_recursive_needs_binary_skip` auto-unfreeze fix-shape.

Background: a recurring briefing-shape defect is a recursive `grep -rn ...`
verification bullet that false-fails because it descends into binary
`__pycache__/*.pyc` files. The agent already EMITS the structured hint

    BriefingFix: grep_recursive_needs_binary_skip at <f>:<n>: grep -rn -> grep -rnI

in its `task_complete status=blocked` summary, but until this work the shape
wasn't a recognized fix-shape, so the hint was inert and the task froze for a
manual operator edit. This module pins the now-registered shape end-to-end:

  1. `parse_blocked_summary_fix_shape` structurally parses the canonical
     `BriefingFix:` line into the five-field dict and reports the shape.
  2. The shared transform (`rewrite_briefing_line_for_fix`) rewrites a
     briefing line's `grep -rn ` -> `grep -rnI ` and is idempotent — a line
     that already carries `grep -rnI` is a no-op (no `grep -rnII`
     double-insert). It also no-ops when the pattern is absent, and the
     default (non-binary-skip) shapes keep their literal `from`->`to`
     behavior.
  3. Application is gated by the `AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist:
     allowlisted -> `auto_unfreeze_applied` + briefing patched + task
     re-dispatched; NOT allowlisted -> `auto_unfreeze_skipped
     reason=shape_not_in_allowlist`, task stays Frozen.

The end-to-end fixture mirrors `test_tb225_auto_unfreeze.py` (same init +
operator-queue add/freeze shape) so the on-disk briefing the sweep encounters
matches production.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap2 import daemon, events, tools
from ap2._shared import (
    RECOGNIZED_FIX_SHAPES,
    parse_blocked_summary_fix_shape,
    rewrite_briefing_line_for_fix,
)
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# Minimal goal.md so the briefing structural validator + goal-anchor gate
# don't false-positive when we exercise the add/update path. Mirrors
# `_GOAL_MD` in test_tb225_auto_unfreeze.py.
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


# Briefing whose `## Verification` carries a recursive `grep -rn` bullet —
# the exact shape the binary-skip fix targets. Structurally identical to
# test_tb225's fixture (H1 / Goal-cites-current-focus / Why-now / Scope /
# Design / Verification / Out-of-scope) so the structural validator passes.
_BRIEFING = (
    "# TB-421 fixture briefing\n\n"
    "## Goal\n\n"
    "Self-heals the recursive-grep binary-skip regression class so the "
    "end-to-end automation focus (`## Current focus: end-to-end automation`) "
    "can land without operator-manual unfreeze on every recurrence.\n\n"
    "Why now: closes the failure-recovery operator dependency — a recursive "
    "`grep -rn` bullet false-fails on binary `__pycache__/*.pyc` and freezes "
    "the task for a manual edit.\n\n"
    "## Scope\n\n"
    "- ap2/_shared.py\n\n"
    "## Design\n\n"
    "Direct edit.\n\n"
    "## Verification\n"
    "- `grep -rn 'pattern' ap2/` — matches at least one file.\n\n"
    "## Out of scope\n\n"
    "- nothing\n"
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Project root with the standard ap2 init layout + a real goal.md.

    Strips every `AP2_*` env knob BEFORE `Config.load` so the cfg snapshot
    doesn't carry a stale `AP2_AUTO_UNFREEZE_*` value from the parent process
    (mirrors the TB-327 `cfg` fixture in test_tb225_auto_unfreeze.py), and
    disables the LLM dep-coherence judge so the `add_backlog` path doesn't
    make a real Haiku call.
    """
    import os
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _add_and_freeze(cfg: Config, *, title: str = "tb421 fixture") -> tuple[str, Path]:
    """Add a Backlog task with `_BRIEFING`, materialize it on disk, then move
    it to Frozen. Returns `(task_id, briefing_path)`. Same shape as the
    test_tb225 helper so the sweep sees a production-shaped on-disk briefing."""
    res = tools.do_operator_queue_append(
        cfg, {"op": "add_backlog", "title": title, "briefing": _BRIEFING},
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
    tools.do_board_edit(cfg, {"action": "move_to_frozen", "task_id": task_id})
    return task_id, briefing_path


def _emit_blocked_complete(cfg: Config, *, task_id: str, summary: str) -> None:
    events.append(
        cfg.events_file, "task_complete",
        task=task_id, status="blocked", commit="", summary=summary,
    )


def _briefing_fix_line(*, shape: str, path: str, line: int, frm: str, to: str) -> str:
    return f"BriefingFix: {shape} at {path}:{line}: {frm} -> {to}"


def _grep_rn_line_index(briefing_path: Path) -> int:
    """1-indexed line number of the `grep -rn ` bullet in the briefing."""
    for i, line in enumerate(briefing_path.read_text().splitlines()):
        if "grep -rn " in line:
            return i + 1
    raise AssertionError("fixture briefing has no `grep -rn ` bullet")


# ===========================================================================
# (1) Parser recognizes the shape's canonical BriefingFix line.
# ===========================================================================


def test_parser_recognizes_binary_skip_shape():
    """`parse_blocked_summary_fix_shape` parses the canonical
    `grep_recursive_needs_binary_skip` line into the five-field dict."""
    summary = (
        "Agent self-diagnosis: the recursive `grep -rn` bullet descends into "
        "binary __pycache__/*.pyc and false-fails; add -I to skip binaries.\n"
        "BriefingFix: grep_recursive_needs_binary_skip at "
        ".cc-autopilot/tasks/f.md:64: grep -rn -> grep -rnI\n"
        "Recommend re-dispatch after the patch lands."
    )
    fix = parse_blocked_summary_fix_shape(summary)
    assert fix is not None
    assert fix["shape"] == "grep_recursive_needs_binary_skip"
    assert fix["file"] == ".cc-autopilot/tasks/f.md"
    assert fix["line"] == 64
    assert fix["from"] == "grep -rn"
    assert fix["to"] == "grep -rnI"


def test_shape_is_in_recognized_registry():
    """The shape is enumerated in the human-facing registry alongside the
    four bootstrap shapes."""
    assert "grep_recursive_needs_binary_skip" in RECOGNIZED_FIX_SHAPES
    for bootstrap in (
        "grep_missing_r_on_dir",
        "literal_backtick_in_shell_bullet",
        "bare_python_to_uv_run",
        "bare_path_to_test_f",
    ):
        assert bootstrap in RECOGNIZED_FIX_SHAPES


# ===========================================================================
# (2) Transform: grep -rn -> grep -rnI, idempotent, and default-shape
#     behavior preserved.
# ===========================================================================


_BINARY_SKIP_FIX = {
    "shape": "grep_recursive_needs_binary_skip",
    "from": "grep -rn",
    "to": "grep -rnI",
}


def test_transform_rewrites_grep_rn_to_grep_rnI():
    """The transform adds the `-I` binary-skip flag, leaving the rest of the
    line (codespan backticks, pattern, path, prose) untouched."""
    line = "- `grep -rn 'pattern' ap2/` — matches at least one file.\n"
    out = rewrite_briefing_line_for_fix(_BINARY_SKIP_FIX, line)
    assert out == "- `grep -rnI 'pattern' ap2/` — matches at least one file.\n"


def test_transform_is_idempotent_on_already_fixed_line():
    """A line already carrying `grep -rnI` is a no-op — the transform must
    NOT double-insert the flag (`grep -rnII`). Returns None."""
    fixed = "- `grep -rnI 'pattern' ap2/` — matches at least one file.\n"
    assert rewrite_briefing_line_for_fix(_BINARY_SKIP_FIX, fixed) is None


def test_transform_no_op_when_pattern_absent():
    """No `grep -rn ` on the line -> no-op (None)."""
    line = "- `uv run pytest -q ap2/tests/` — all tests pass.\n"
    assert rewrite_briefing_line_for_fix(_BINARY_SKIP_FIX, line) is None


def test_transform_default_shape_literal_replace_unchanged():
    """A non-binary-skip shape still uses the literal `from`->`to` path —
    confirms the registry delegation didn't regress the bootstrap shapes."""
    fix = {"shape": "grep_missing_r_on_dir", "from": "grep -lE", "to": "grep -rlE"}
    line = "- `grep -lE 'pat' ap2/tests/` — matches.\n"
    out = rewrite_briefing_line_for_fix(fix, line)
    assert out == "- `grep -rlE 'pat' ap2/tests/` — matches.\n"


def test_transform_default_shape_no_op_when_from_absent():
    """Default-shape `from` not on the line -> None (the applier reads this
    as `briefing_mismatch`)."""
    fix = {"shape": "grep_missing_r_on_dir", "from": "grep -XYZ", "to": "grep -rlE"}
    line = "- `grep -lE 'pat' ap2/tests/` — matches.\n"
    assert rewrite_briefing_line_for_fix(fix, line) is None


# ===========================================================================
# (3) Application is gated by the AP2_AUTO_UNFREEZE_FIX_SHAPES allowlist.
# ===========================================================================


def test_binary_skip_applied_when_allowlisted(cfg: Config, monkeypatch):
    """With `grep_recursive_needs_binary_skip` on the allowlist, a Frozen
    task whose blocked summary carries the canonical hint is auto-unfrozen:
    `auto_unfreeze_applied` fires, the briefing line is patched `grep -rn `
    -> `grep -rnI ` on drain, and the task moves Frozen -> Backlog."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_recursive_needs_binary_skip",
    )
    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=(
            "Agent diagnosis: recursive grep descends into binary "
            "__pycache__/*.pyc and false-fails; add -I.\n"
            + _briefing_fix_line(
                shape="grep_recursive_needs_binary_skip",
                path=rel,
                line=_grep_rn_line_index(briefing_path),
                frm="grep -rn",
                to="grep -rnI",
            )
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    assert len(applied) == 1, applied
    assert applied[0]["task"] == task_id
    assert applied[0]["shape"] == "grep_recursive_needs_binary_skip"

    # Queued, not yet on disk — drain to apply.
    tools.drain_operator_queue(cfg)
    text_after = briefing_path.read_text()
    assert "grep -rnI " in text_after, text_after
    # No bare `grep -rn ` survives once the fixed form is stripped (i.e. the
    # flag was added in-place, not double-inserted as `grep -rnII`).
    assert "grep -rn " not in text_after.replace("grep -rnI ", ""), text_after
    assert "grep -rnII" not in text_after, text_after

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Backlog", loc


def test_binary_skip_skipped_when_not_allowlisted(cfg: Config, monkeypatch):
    """The shape is opt-in: with a DIFFERENT shape on the allowlist, the
    binary-skip hint skips with `shape_not_in_allowlist` and the task stays
    Frozen. Pins the trust-contract gating from Scope (2)."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=_briefing_fix_line(
            shape="grep_recursive_needs_binary_skip",
            path=rel,
            line=_grep_rn_line_index(briefing_path),
            frm="grep -rn",
            to="grep -rnI",
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    skipped = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_skipped"
        and e.get("reason") == "shape_not_in_allowlist"
    ]
    assert applied == [], applied
    assert len(skipped) == 1, skipped
    assert skipped[0]["task"] == task_id
    assert skipped[0]["shape"] == "grep_recursive_needs_binary_skip"

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", loc


def test_binary_skip_unset_allowlist_is_noop(cfg: Config, monkeypatch):
    """Unset allowlist -> opt-in feature disabled: no applied/skip events,
    task stays Frozen (no `events.jsonl` noise for operators who haven't
    engaged the feature)."""
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", raising=False)
    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=_briefing_fix_line(
            shape="grep_recursive_needs_binary_skip",
            path=rel,
            line=_grep_rn_line_index(briefing_path),
            frm="grep -rn",
            to="grep -rnI",
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    assert [e for e in evts if e.get("type") == "auto_unfreeze_applied"] == []
    assert [e for e in evts if e.get("type") == "auto_unfreeze_skipped"] == []
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", loc
