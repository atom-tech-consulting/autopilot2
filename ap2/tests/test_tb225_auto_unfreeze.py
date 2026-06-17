"""TB-225: behavioral pinning for auto-applied agent-diagnosed briefing-shape
fixes from `task_complete status=blocked` summaries.

Background (briefing's Why-now): TB-204's `grep -lE` → `grep -rlE` and TB-207's
literal-backtick-in-shell-bullet hit the same recurring failure shape — the
task agent self-diagnosed a briefing-shape regression in its blocked summary,
but the daemon had no mechanism to apply the diagnosed fix; the operator had
to run `ap2 unfreeze` manually after every recurrence. With this work the
daemon parses the agent's structured `BriefingFix:` prefix, verifies the
briefing-line literal match, patches via the operator-queue `update` op, and
unfreezes the task automatically — all gated on an operator-curated
`AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist + per-task / per-day caps.

Seven behavioral cases (briefing's `## Verification` prose):

  (a) unset allowlist = no auto-unfreeze attempts, no skip events; the
      feature is opt-in and operators who haven't engaged see no noise.
  (b) allowlisted shape + structured `BriefingFix:` prefix + briefing-line-
      literal-match = patch applied + `auto_unfreeze_applied` event +
      task re-dispatched on next-tick drain.
  (c) allowlisted shape + briefing-line-mismatch = skip with
      `briefing_mismatch` reason + task stays Frozen.
  (d) non-allowlisted shape = skip with `shape_not_in_allowlist` reason.
  (e) per-task cap exceeded = skip with `per_task_cap` reason + fallback
      to manual unfreeze (task stays Frozen).
  (f) per-day cap exceeded = halt with `per_day_cap` reason + decisions-
      needed bullet appended to ideation_state.md.
  (g) malformed / missing `BriefingFix:` prefix = parser returns None +
      behaves identically to today's manual-unfreeze path (no skip event,
      task stays Frozen until operator intervenes).

Test shape mirrors `test_tb223_auto_approve.py` (the auto-approve gate) and
`test_tb224_token_caps.py` (the cost-cap layer): direct unit pins on the
parser + env knobs, plus end-to-end `_tick` walks with stubbed internals
that exercise the full path through the operator queue.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ap2 import daemon, events, tools
from ap2._shared import parse_blocked_summary_fix_shape
from ap2.board import Board
from ap2.config import Config
from ap2.init import init_project


# Minimal goal.md so the briefing structural validator + goal-anchor gate
# don't false-positive when we exercise the update path via the operator
# queue. Mirrors `_GOAL_MD` in test_tb223_auto_approve.py / test_tb224_token_caps.py.
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


# Briefing whose `## Goal` body cites `## Current focus` verbatim + carries
# a `Why now:` rationale so the structural validator passes. Critically,
# the briefing has a `## Verification` shell bullet that we'll patch
# in the (b) / (c) / (e) / (f) cases to exercise the line-replacement path.
_BRIEFING = (
    "# TB-225 fixture briefing\n\n"
    "## Goal\n\n"
    "Self-heals the briefing-shape regression class so the end-to-end "
    "automation focus (`## Current focus: end-to-end automation`) can "
    "land without operator-manual unfreeze on every recurrence.\n\n"
    "Why now: closes the failure-recovery operator dependency — without "
    "this, every briefing-shape regression cascades into operator-manual "
    "unfreeze and the walk-away envelope contracts.\n\n"
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
    from a parent process whose `.cc-autopilot/env` happens to export
    a knob. The migrated helpers route through
    `Config.get_component_value`, whose env-first precedence reads
    `os.environ` at call time — but the snapshot layer (TOML overlay
    + load-time env-coerced value) populated at `apply_env_overrides`
    time would otherwise leak parent-process knob values into the
    "unset / empty / non-int → default" parser pins. Mirrors the
    TB-326 pilot's `clean_env` fixture shape.
    """
    import os
    for name in list(os.environ):
        if name.startswith("AP2_"):
            monkeypatch.delenv(name, raising=False)
    # Disable the LLM dep-coherence judge so the `add_backlog` path
    # below doesn't make a real, slow, non-deterministic Haiku judge
    # call (orthogonal to the auto-unfreeze behavior under test).
    # `AP2_VALIDATOR_JUDGE_DISABLED` is an `ENV_PERMITTED_KEYS` env-only
    # knob, so its flat env still applies under TB-413.
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_DISABLED", "1")
    init_project(tmp_path)
    (tmp_path / "goal.md").write_text(_GOAL_MD)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _unwrap(res: dict) -> dict:
    assert not res.get("isError"), res
    return json.loads(res["content"][0]["text"])


def _add_and_freeze(cfg: Config, *, title: str = "tb225 fixture") -> tuple[str, Path]:
    """Add a Backlog task with `_BRIEFING`, then move it directly to Frozen
    via the operator queue + drain. Returns `(task_id, briefing_path)`.

    Using the operator-queue path keeps the test honest about the on-disk
    shape the daemon's auto-unfreeze sweep will encounter at run time.
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
    # Drain to materialize the row on TASKS.md + the briefing file on disk.
    tools.drain_operator_queue(cfg)
    # Locate the briefing path via the board so the test exercises the
    # same lookup the daemon would.
    board = Board.load(cfg.tasks_file)
    task = board.get(task_id)
    assert task is not None and task.briefing, (
        f"fixture: {task_id} has no briefing path after add_backlog drain"
    )
    briefing_path = cfg.project_root / task.briefing
    assert briefing_path.exists(), f"briefing not on disk: {briefing_path}"
    # Move to Frozen via direct board edit (the operator queue doesn't
    # expose Backlog→Frozen; only retry_exhausted does, which we simulate
    # here without a real failed task agent run).
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
    """Render the canonical `BriefingFix:` prefix. The agent-prompt
    contract is `BriefingFix: <shape> at <path>:<line>: <from> -> <to>`."""
    return f"BriefingFix: {shape} at {path}:{line}: {frm} -> {to}"


# ===========================================================================
# Direct parser unit pins. Surface the structured-prefix contract so a
# refactor that softens the parse rules surfaces cleanly.
# ===========================================================================


def test_parser_happy_path():
    """`parse_blocked_summary_fix_shape` returns the five-field dict on a
    canonical `BriefingFix:` prefix. Pins the agent-prompt contract."""
    summary = (
        "Agent self-diagnosis: the verifier's grep bullet returns nothing "
        "without `-r` on a directory target.\n"
        "BriefingFix: grep_missing_r_on_dir at .cc-autopilot/tasks/foo.md:23: "
        "grep -lE 'pat' ap2/tests/ -> grep -rlE 'pat' ap2/tests/\n"
        "Recommend re-dispatch after the patch lands."
    )
    fix = parse_blocked_summary_fix_shape(summary)
    assert fix is not None
    assert fix["shape"] == "grep_missing_r_on_dir"
    assert fix["file"] == ".cc-autopilot/tasks/foo.md"
    assert fix["line"] == 23
    assert fix["from"] == "grep -lE 'pat' ap2/tests/"
    assert fix["to"] == "grep -rlE 'pat' ap2/tests/"


def test_parser_returns_none_on_missing_prefix():
    """Free-text summaries without the structured prefix return None — the
    parser only consumes what the agent structurally emits."""
    assert parse_blocked_summary_fix_shape("") is None
    assert parse_blocked_summary_fix_shape(
        "I think the briefing's grep -lE should be grep -rlE but I'm not sure."
    ) is None


def test_parser_returns_none_on_malformed_prefix():
    """Partial / structurally-broken prefixes return None rather than
    guessing fields. Pins the "structured or nothing" contract."""
    # Missing ` at ` separator.
    assert parse_blocked_summary_fix_shape(
        "BriefingFix: grep_missing_r_on_dir foo.md:23: a -> b"
    ) is None
    # Non-integer line number.
    assert parse_blocked_summary_fix_shape(
        "BriefingFix: grep_missing_r_on_dir at foo.md:abc: a -> b"
    ) is None
    # Missing arrow separator.
    assert parse_blocked_summary_fix_shape(
        "BriefingFix: grep_missing_r_on_dir at foo.md:23: a b"
    ) is None
    # Missing colon-space between path:line and from/to.
    assert parse_blocked_summary_fix_shape(
        "BriefingFix: grep_missing_r_on_dir at foo.md:23 a -> b"
    ) is None
    # Empty shape.
    assert parse_blocked_summary_fix_shape(
        "BriefingFix:  at foo.md:23: a -> b"
    ) is None


def test_parser_handles_non_string_input():
    """Non-string input returns None (defensive)."""
    assert parse_blocked_summary_fix_shape(None) is None  # type: ignore[arg-type]
    assert parse_blocked_summary_fix_shape(42) is None  # type: ignore[arg-type]


# ===========================================================================
# Direct env-knob unit pins. Same parse-shape contract as TB-223 / TB-224.
# ===========================================================================


def test_allowlist_unset_returns_empty(cfg: Config, monkeypatch):
    """`AP2_AUTO_UNFREEZE_FIX_SHAPES` unset / empty → empty set. The
    daemon treats this as "feature disabled," not as a typo / parse
    failure. Pins the opt-in default.

    TB-327: helper takes a `cfg` argument and resolves via
    `Config.get_component_value`. TB-413: the flat
    `AP2_AUTO_UNFREEZE_FIX_SHAPES` tunable override is removed
    (config.toml is the sole source), so the parse-shape pin injects via
    the SECTIONED env name `AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES`,
    which still overrides — holding the pin end-to-end.
    """
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", raising=False)
    assert daemon._auto_unfreeze_allowlist(cfg) == frozenset()

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "")
    assert daemon._auto_unfreeze_allowlist(cfg) == frozenset()

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "  ")
    assert daemon._auto_unfreeze_allowlist(cfg) == frozenset()


def test_allowlist_parses_csv(cfg: Config, monkeypatch):
    """Comma-separated tokens with whitespace are trimmed; empty tokens
    are dropped. Frozenset return so callers can pass it around without
    defensive copies.

    TB-327: same `cfg`-argument migration as the unset-pin sibling.
    """
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir, literal_backtick_in_shell_bullet ,, bare_path_to_test_f",
    )
    got = daemon._auto_unfreeze_allowlist(cfg)
    assert isinstance(got, frozenset)
    assert got == frozenset({
        "grep_missing_r_on_dir",
        "literal_backtick_in_shell_bullet",
        "bare_path_to_test_f",
    })


def test_per_task_cap_default_is_one(cfg: Config, monkeypatch):
    """`AP2_AUTO_UNFREEZE_MAX_PER_TASK` defaults to 1 (single attempt
    before fallback to manual unfreeze). Pins the briefing's stated
    default.

    TB-327: helper takes a `cfg` argument. TB-413: the flat
    `AP2_AUTO_UNFREEZE_MAX_PER_TASK` tunable override is removed, so the
    parse-shape pin injects via the SECTIONED env name
    `AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK`, which still overrides —
    exercising the env parser shape end-to-end
    (default-on-empty/garbage/negative, zero-honored, positive-int
    passthrough).
    """
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", raising=False)
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "garbage")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "-5")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 1

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "0")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 0  # explicit disable honored

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "5")
    assert daemon._auto_unfreeze_max_per_task(cfg) == 5


# ===========================================================================
# TB-320: kill-switch `AP2_AUTO_UNFREEZE_DISABLED` — top-of-tick-hook
# short-circuit + sticky-first-skip `auto_unfreeze_disabled` audit event.
# ===========================================================================


def test_tb320_kill_switch_short_circuits_and_emits_event(
    cfg: Config, monkeypatch,
):
    """Setting `AP2_AUTO_UNFREEZE_DISABLED=1` makes `_maybe_auto_unfreeze`
    return without running the allowlist / board-load / sweep, AND emits
    exactly one `auto_unfreeze_disabled` audit event on the first skip
    per process (sticky dedup via the module-level
    `_DISABLED_EVENT_EMITTED` flag).

    Mirrors TB-225's (a) "unset allowlist is noop" shape: the existing
    no-op early-return inside `_maybe_auto_unfreeze` lives BELOW the new
    kill-switch check, so the test sets BOTH a non-empty allowlist AND
    a frozen task with a parseable `BriefingFix:` summary (i.e.
    everything that would normally trigger an `auto_unfreeze_applied`),
    then flips the kill switch — confirming the short-circuit fires
    before any of the downstream guards.
    """
    from ap2.components import auto_unfreeze as au

    # Reset the sticky flag so the test is hermetic regardless of any
    # earlier test that exercised the disabled-path.
    au._reset_disabled_event_emitted_for_tests()

    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "1")

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    text = briefing_path.read_text()
    grep_line_idx = next(
        i for i, line in enumerate(text.splitlines())
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

    # First call → emits exactly one `auto_unfreeze_disabled` event.
    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    disabled_evts = [
        e for e in evts if e.get("type") == "auto_unfreeze_disabled"
    ]
    assert len(disabled_evts) == 1, disabled_evts
    assert disabled_evts[0].get("reason") == "env_flag_set", disabled_evts[0]
    assert (
        disabled_evts[0].get("env_flag") == "AP2_AUTO_UNFREEZE_DISABLED"
    ), disabled_evts[0]

    # No downstream applied / skipped event fired — the short-circuit
    # ran BEFORE the allowlist / cap / line-match guards.
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    skipped = [e for e in evts if e.get("type") == "auto_unfreeze_skipped"]
    assert applied == [], applied
    assert skipped == [], skipped

    # Task stays Frozen — short-circuit means no unfreeze queue op.
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", loc

    # Second call → STILL no second `auto_unfreeze_disabled` event
    # (sticky dedup via `_DISABLED_EVENT_EMITTED`).
    daemon._maybe_auto_unfreeze(cfg)
    evts = events.tail(cfg.events_file, 200)
    disabled_evts = [
        e for e in evts if e.get("type") == "auto_unfreeze_disabled"
    ]
    assert len(disabled_evts) == 1, (
        f"sticky dedup: second tick must NOT re-emit the disabled "
        f"event; got {disabled_evts}"
    )


def test_tb320_kill_switch_unset_runs_sweep_normally(
    cfg: Config, monkeypatch,
):
    """With `AP2_AUTO_UNFREEZE_DISABLED` unset (and the allowlist set
    to a matching shape + a Frozen task carrying a parseable
    `BriefingFix:` summary), the sweep runs end-to-end and emits the
    normal `auto_unfreeze_applied` event — confirming the kill switch
    is the only thing being toggled and the rest of the guard chain
    still fires when the knob is off.

    Sibling to the "disabled" test above — same fixture shape, knob
    off, asserts the positive case.
    """
    from ap2.components import auto_unfreeze as au

    au._reset_disabled_event_emitted_for_tests()
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", raising=False)
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir",
    )

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    text = briefing_path.read_text()
    grep_line_idx = next(
        i for i, line in enumerate(text.splitlines())
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
    disabled_evts = [
        e for e in evts if e.get("type") == "auto_unfreeze_disabled"
    ]
    assert disabled_evts == [], (
        f"kill switch unset must NOT emit `auto_unfreeze_disabled`; "
        f"got {disabled_evts}"
    )
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    assert len(applied) == 1, applied


def test_tb320_is_auto_unfreeze_disabled_truthy_parse(
    cfg: Config, monkeypatch,
):
    """`_is_auto_unfreeze_disabled` resolves the disabled knob via
    `Config.get_component_value` (TB-327 axis-5) and accepts the same
    truthy set as the sibling kill-switch parsers
    (`AP2_FOCUS_AUTO_ADVANCE_DISABLED`, `AP2_VALIDATOR_JUDGE_DISABLED`):
    `1` / `true` / `yes` / `on` (case-insensitive). Default-unset →
    False. TB-413: the flat `AP2_AUTO_UNFREEZE_DISABLED` tunable override
    is removed, so the truthy-parse pin injects via the SECTIONED env
    name `AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED`, which still overrides —
    exercising the env-side parser shape end-to-end.
    """
    from ap2.components.auto_unfreeze import _is_auto_unfreeze_disabled

    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", raising=False)
    assert _is_auto_unfreeze_disabled(cfg) is False
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "")
    assert _is_auto_unfreeze_disabled(cfg) is False
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "0")
    assert _is_auto_unfreeze_disabled(cfg) is False
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "false")
    assert _is_auto_unfreeze_disabled(cfg) is False
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "1")
    assert _is_auto_unfreeze_disabled(cfg) is True
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "TRUE")
    assert _is_auto_unfreeze_disabled(cfg) is True
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "yes")
    assert _is_auto_unfreeze_disabled(cfg) is True
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_DISABLED", "on")
    assert _is_auto_unfreeze_disabled(cfg) is True


def test_per_day_cap_default_is_three(cfg: Config, monkeypatch):
    """`AP2_AUTO_UNFREEZE_MAX_PER_DAY` defaults to 3 (rolling 24h).

    TB-327: helper takes a `cfg` argument. TB-413: the flat tunable
    override is removed, so this injects via the SECTIONED env name
    `AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY`.
    """
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", raising=False)
    assert daemon._auto_unfreeze_max_per_day(cfg) == 3

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 3

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "not-a-number")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 3

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "0")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 0

    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "10")
    assert daemon._auto_unfreeze_max_per_day(cfg) == 10


# ===========================================================================
# (a) Unset allowlist = no auto-unfreeze, no skip events.
# ===========================================================================


def test_a_unset_allowlist_is_noop(cfg: Config, monkeypatch):
    """When `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset, the sweep is a no-op
    even when a Frozen task has a parseable `BriefingFix:` prefix. No
    `auto_unfreeze_applied` / `auto_unfreeze_skipped` events fire; the
    task stays Frozen until operator-manual `ap2 unfreeze`."""
    monkeypatch.delenv("AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", raising=False)
    task_id, briefing_path = _add_and_freeze(cfg)
    # Provide a perfectly-shaped fix in a blocked summary.
    rel = str(briefing_path.relative_to(cfg.project_root))
    # Find the actual line number of the grep bullet in the briefing.
    text = briefing_path.read_text()
    grep_line_idx = next(
        i for i, line in enumerate(text.splitlines())
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
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    skipped = [e for e in evts if e.get("type") == "auto_unfreeze_skipped"]
    assert applied == [], (
        f"unset allowlist must not auto-apply; got: {applied}"
    )
    assert skipped == [], (
        f"unset allowlist must not emit skip events (opt-in feature); "
        f"got: {skipped}"
    )
    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"task must stay Frozen when feature is disabled; got section={loc}"
    )


# ===========================================================================
# (b) Happy path: allowlisted + structured prefix + line-match → patch
#     applied + auto_unfreeze_applied event + task re-dispatched.
# ===========================================================================


def test_b_patch_applied_and_redispatched(cfg: Config, monkeypatch):
    """Full happy path: parser hits, allowlist matches, briefing-line
    match passes, patch lands on the briefing file via the operator-
    queue update op, and the unfreeze op moves the task to Backlog.
    Pins the briefing's Scope (3) end-to-end."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES",
        "grep_missing_r_on_dir,literal_backtick_in_shell_bullet",
    )

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    text_before = briefing_path.read_text()
    lines_before = text_before.splitlines()
    grep_line_idx = next(
        i for i, line in enumerate(lines_before) if "grep -lE" in line
    )
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
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    assert len(applied) == 1, (
        f"expected exactly one auto_unfreeze_applied event; got: {applied}"
    )
    assert applied[0]["task"] == task_id
    assert applied[0]["shape"] == "grep_missing_r_on_dir"
    assert applied[0]["from"] == "grep -lE"
    assert applied[0]["to"] == "grep -rlE"

    # The patch hasn't yet been applied to disk — it's queued. Drain.
    tools.drain_operator_queue(cfg)
    text_after = briefing_path.read_text()
    assert "grep -rlE" in text_after, (
        f"briefing should now contain the patched form; got:\n{text_after}"
    )
    assert "grep -lE" not in text_after.replace("grep -rlE", ""), (
        "the original form must be fully replaced on the patched line; "
        f"got:\n{text_after}"
    )

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Backlog", (
        f"task must be re-dispatched to Backlog after auto-unfreeze; "
        f"got section={loc}"
    )

    # And task_unfrozen + task_updated events landed on the drain.
    drained = events.tail(cfg.events_file, 400)
    assert any(
        e.get("type") == "task_unfrozen" and e.get("task") == task_id
        for e in drained
    ), "drain must emit task_unfrozen"
    assert any(
        e.get("type") == "task_updated" and e.get("task") == task_id
        for e in drained
    ), "drain must emit task_updated for the briefing patch"


# ===========================================================================
# (c) Briefing-line mismatch = skip with briefing_mismatch + stays Frozen.
# ===========================================================================


def test_c_briefing_mismatch_skips_and_stays_frozen(cfg: Config, monkeypatch):
    """When the named line doesn't contain the agent-claimed `from`
    pattern (e.g. the briefing was operator-edited between failure and
    freeze handling), the daemon emits `auto_unfreeze_skipped
    reason=briefing_mismatch` and leaves the task Frozen. Pins the
    data-race-window safety check from Scope (3)."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    # Diagnose a fix against a `from` that does NOT appear on the named
    # line (the briefing only contains `grep -lE`, not `grep -XYZ`).
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
            frm="grep -XYZ",  # NOT in the briefing
            to="grep -rlE",
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    skipped = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_skipped"
        and e.get("reason") == "briefing_mismatch"
    ]
    assert applied == [], (
        f"briefing_mismatch must NOT apply any patch; got: {applied}"
    )
    assert len(skipped) == 1, (
        f"expected one briefing_mismatch skip event; got: {skipped}"
    )
    assert skipped[0]["task"] == task_id

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"task must stay Frozen on briefing_mismatch; got section={loc}"
    )


# ===========================================================================
# (d) Non-allowlisted shape = skip with shape_not_in_allowlist.
# ===========================================================================


def test_d_unlisted_shape_skips(cfg: Config, monkeypatch):
    """A perfectly-parseable `BriefingFix:` prefix whose shape token is
    NOT in `AP2_AUTO_UNFREEZE_FIX_SHAPES` skips with
    `shape_not_in_allowlist`. The operator opens new shapes by editing
    the env-knob string; the daemon never invents shapes."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
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
            shape="bare_python_to_uv_run",  # not in allowlist
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
        and e.get("reason") == "shape_not_in_allowlist"
    ]
    assert len(skipped) == 1, skipped
    assert skipped[0]["task"] == task_id
    assert skipped[0]["shape"] == "bare_python_to_uv_run"

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen"


# ===========================================================================
# (e) Per-task cap exceeded = skip with per_task_cap + fallback.
# ===========================================================================


def test_e_per_task_cap_falls_back_to_manual(cfg: Config, monkeypatch):
    """Once a task has had `AP2_AUTO_UNFREEZE_MAX_PER_TASK` auto-unfreeze
    applications, further attempts skip with `per_task_cap` and the task
    stays Frozen until operator-manual unfreeze. Bounds oscillation."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "1")

    task_id, briefing_path = _add_and_freeze(cfg)
    rel = str(briefing_path.relative_to(cfg.project_root))
    grep_line_idx = next(
        i for i, line in enumerate(briefing_path.read_text().splitlines())
        if "grep -lE" in line
    )

    # Simulate a prior auto-unfreeze on this task (the per-task cap=1
    # means the next attempt should be refused).
    events.append(
        cfg.events_file,
        "auto_unfreeze_applied",
        task=task_id,
        shape="grep_missing_r_on_dir",
        **{"from": "grep -lE", "to": "grep -rlE"},
    )

    # Now the agent diagnosed another fix — the cap should fire.
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
    new_applied = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_applied"
        and e.get("task") == task_id
    ]
    # The pre-seeded event still counts as 1; no NEW application this tick.
    assert len(new_applied) == 1, (
        f"per_task_cap must refuse the second attempt; got applied: {new_applied}"
    )
    skipped = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_skipped"
        and e.get("reason") == "per_task_cap"
    ]
    assert len(skipped) == 1, skipped
    assert skipped[0]["task"] == task_id

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"per_task_cap fallback must leave the task Frozen; got section={loc}"
    )


# ===========================================================================
# (f) Per-day cap exceeded = halt + decisions-needed bullet.
# ===========================================================================


def test_f_per_day_cap_halts_and_emits_decisions_needed(
    cfg: Config, monkeypatch,
):
    """When the rolling-24h count of `auto_unfreeze_applied` events hits
    `AP2_AUTO_UNFREEZE_MAX_PER_DAY`, the daemon halts further auto-unfreeze
    attempts on the tick AND appends a `## Decisions needed from operator`
    bullet to ideation_state.md so the operator sees a systemic-regression
    signal."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_TASK", "5")  # keep per-task open
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "2")

    # Seed 2 prior auto-unfreeze applications within the last 24h.
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

    # Now a Frozen task whose summary requests a third auto-unfreeze.
    task_id, briefing_path = _add_and_freeze(cfg, title="tb225 should be capped")
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
    new_applied = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_applied"
        and e.get("task") == task_id
    ]
    assert new_applied == [], (
        f"per_day_cap must refuse new applications; got: {new_applied}"
    )
    skipped = [
        e for e in evts
        if e.get("type") == "auto_unfreeze_skipped"
        and e.get("reason") == "per_day_cap"
    ]
    assert len(skipped) == 1, skipped
    assert skipped[0]["task"] == task_id

    # Decisions-needed bullet landed in ideation_state.md.
    state_path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    text = state_path.read_text()
    assert "## Decisions needed from operator" in text
    assert "Auto-unfreeze daily cap" in text, (
        f"decisions-needed bullet must mention the cap; got:\n{text}"
    )

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen", (
        f"per_day_cap halt must leave the task Frozen; got section={loc}"
    )


# ===========================================================================
# (g) Malformed / missing BriefingFix prefix = parser returns None +
#     same as manual-unfreeze path (no skip event, task stays Frozen).
# ===========================================================================


def test_g_missing_prefix_falls_through_to_manual(cfg: Config, monkeypatch):
    """An agent summary without the structured `BriefingFix:` prefix
    parses to None and behaves identically to today's manual-unfreeze
    path: no `auto_unfreeze_applied` event, no `auto_unfreeze_skipped`
    event, task stays Frozen. Pins the "no regex-on-prose guessing"
    contract."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )

    task_id, _ = _add_and_freeze(cfg)
    # Free-text diagnosis without the structured prefix.
    _emit_blocked_complete(
        cfg, task_id=task_id,
        summary=(
            "Looks like the grep bullet needs -r to recurse over the "
            "directory target. Suggest editing the briefing to use "
            "`grep -rlE` instead of `grep -lE`."
        ),
    )

    daemon._maybe_auto_unfreeze(cfg)

    evts = events.tail(cfg.events_file, 200)
    applied = [e for e in evts if e.get("type") == "auto_unfreeze_applied"]
    skipped = [e for e in evts if e.get("type") == "auto_unfreeze_skipped"]
    assert applied == [], applied
    assert skipped == [], (
        f"malformed/missing prefix must fall through silently (no skip "
        f"event); got: {skipped}"
    )

    board = Board.load(cfg.tasks_file)
    loc = board.find(task_id)
    assert loc is not None and loc[0] == "Frozen"


# ===========================================================================
# Additional pin: applied-event counter window respects the 24h boundary.
# ===========================================================================


def test_per_day_cap_window_rolls_off_after_24h(cfg: Config, monkeypatch):
    """`auto_unfreeze_applied` events older than 24h don't count toward
    the per-day cap — the window is rolling, not cumulative. Pins the
    briefing's "rolling 24h cap" rule from Scope (4)."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )
    monkeypatch.setenv("AP2_COMPONENTS_AUTO_UNFREEZE_MAX_PER_DAY", "2")

    # Seed an old applied event (more than 24h ago) and a recent one.
    # We can't easily backdate events.append's ts, so we'll patch
    # _parse_event_ts indirectly via injecting old ts on a hand-crafted
    # tail. Easier: emit two recent applied events and then assert the
    # count exceeds the cap; then bump now_s to roll the window.
    events.append(
        cfg.events_file, "auto_unfreeze_applied",
        task="TB-800", shape="grep_missing_r_on_dir",
        **{"from": "a", "to": "b"},
    )
    events.append(
        cfg.events_file, "auto_unfreeze_applied",
        task="TB-801", shape="grep_missing_r_on_dir",
        **{"from": "a", "to": "b"},
    )

    tail = events.tail(cfg.events_file, 200)
    # Within window: 2 applied events, equal to the cap.
    count_now = daemon._count_auto_unfreeze_applied_in_window(tail)
    assert count_now == 2

    # Outside window (simulate 25h elapsed): both events are stale.
    later = time.time() + 25 * 3600
    count_later = daemon._count_auto_unfreeze_applied_in_window(
        tail, now_s=later,
    )
    assert count_later == 0, (
        f"applied events >24h old must roll off; got count={count_later}"
    )


# ===========================================================================
# End-to-end pin: full _tick walks the auto-unfreeze + drain path.
# ===========================================================================


def _stub_tick_quiet(monkeypatch) -> None:
    """Stub every `_tick` internal except the operator-queue drain +
    auto-unfreeze sweep. Mirrors `_stub_tick_quiet` in TB-223 / TB-224
    tests (same shape, same external deps)."""

    async def _noop_sweep(cfg, sdk):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "_sweep_pipeline_pending", _noop_sweep)
    monkeypatch.setattr(daemon, "_maybe_auto_diagnose", lambda cfg: None)

    async def _noop_async(*a, **kw):  # noqa: ARG001
        return None

    from ap2 import ideation as _ideation
    monkeypatch.setattr(_ideation, "_maybe_ideate", _noop_async)
    monkeypatch.setattr(_ideation, "force_ideate", _noop_async)
    # TB-381: the cron stage is now the `Phase.CRON_DISPATCH` walk into the
    # cron scheduler component; neutralize it by stubbing the component's
    # `load_jobs` (string target avoids importing the impl module here).
    monkeypatch.setattr("ap2.components.cron.impl.load_jobs", lambda path: [])

    async def _noop_run_task(cfg, sdk, mcp_server, task):  # noqa: ARG001
        return None

    monkeypatch.setattr(daemon, "run_task", _noop_run_task)


class _NoopSDK:
    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        async def _gen():
            if False:
                yield None
        return _gen()


def test_end_to_end_tick_walks_auto_unfreeze_path(cfg: Config, monkeypatch):
    """A full `_tick` walk with `AP2_AUTO_UNFREEZE_FIX_SHAPES` set and a
    Frozen task carrying a fresh `BriefingFix:` summary: the auto-unfreeze
    sweep queues `update` + `unfreeze`; the NEXT tick's drain applies
    them. Pins the two-tick sequence end-to-end."""
    monkeypatch.setenv(
        "AP2_COMPONENTS_AUTO_UNFREEZE_FIX_SHAPES", "grep_missing_r_on_dir",
    )

    task_id, briefing_path = _add_and_freeze(cfg, title="tb225 e2e")
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

    _stub_tick_quiet(monkeypatch)
    # Tick 1: sweep queues update + unfreeze. Task is still Frozen.
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))
    board1 = Board.load(cfg.tasks_file)
    loc1 = board1.find(task_id)
    assert loc1 is not None and loc1[0] == "Frozen", (
        "tick 1: sweep queues ops; task is still Frozen until drain on "
        f"tick 2; got section={loc1}"
    )
    evts = events.tail(cfg.events_file, 400)
    assert any(
        e.get("type") == "auto_unfreeze_applied" and e.get("task") == task_id
        for e in evts
    ), "tick 1 should have emitted auto_unfreeze_applied"

    # Tick 2: drain applies the queued ops; task moves to Backlog.
    asyncio.run(daemon._tick(cfg, _NoopSDK(), mcp_server=None))
    board2 = Board.load(cfg.tasks_file)
    loc2 = board2.find(task_id)
    assert loc2 is not None and loc2[0] in ("Backlog", "Ready"), (
        f"tick 2: drain must move the task off Frozen; got section={loc2}"
    )
    assert "grep -rlE" in briefing_path.read_text(), (
        "tick 2 drain must have patched the briefing"
    )
