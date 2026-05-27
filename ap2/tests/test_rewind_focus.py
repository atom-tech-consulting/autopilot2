"""TB-295: regression-pin for `ap2 rewind-focus`.

The operator-CLI verb closes the 2026-05-26 false-advance recovery
hole. Today's recovery path (direct `focus_pointer.json` edit) emits
no event; the empty-cycles counter's cutoff scan
(`focus_advance._ideation_empty_against_focus`'s most-recent
`focus_advanced to=<focus_title>` lookup) leaves `cutoff_idx = -1`,
so the counter walks the entire 200-event tail and picks up
pre-rewind `ideation_empty_board` + `ideation_complete` pairs as if
they belonged to the rewound focus — re-tripping the false advance
after a single truly-empty post-rewind cycle.

The new verb queues a `rewind_focus` op; the drain-side handler
mutates `focus_pointer.json` AND emits a synthetic
`focus_advanced trigger=operator_rewind to=<title>` event. The
counter's cutoff scan keys off `to=<focus_title>` regardless of
`trigger`, so the synthetic event closes both the audit-trail gap
AND the counter-cutoff hole in one write.

Pins exercised here:

  CLI verb registration:
    - `ap2 rewind-focus --help` works (parser registered)
    - cmd_rewind_focus accepts a title (and optional --reason)
    - CLI rejects an unknown title with non-zero exit, no queue write

  Drain-side handler (`_apply_operator_op` `rewind_focus` branch):
    - Pointer fields updated correctly (`active_index`,
      `active_title`, `exhausted_titles`, `empty_cycles`,
      `roadmap_complete_emitted`)
    - Emits `focus_advanced trigger=operator_rewind` with the
      documented payload fields (`from`, `to`, `new_index`,
      `total_foci`, `reason`)
    - `operator_log.md` receives the rich audit line
    - Standard `applied operator-queued rewind_focus` audit line
      also lands (verb-vs-other-ops distinction)

  Counter-cutoff semantics (the load-bearing fix):
    - Empty cycles before the synthetic `focus_advanced` event are
      NOT counted; only post-rewind cycles count toward the rewound
      focus's counter.

  Title-resolution race:
    - Operator edits goal.md between CLI invocation and drain,
      dropping the target title → drain rejects with a meaningful
      error; pointer unmodified; `operator_queue_error` event
      lands.

Direct unit pins (no daemon/SDK harness), mirrors the shape of
`test_approve.py` for the CLI + queue + drain triad.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2 import events, goal, tools
from ap2.cli import build_parser, cmd_rewind_focus
from ap2.config import Config
from ap2.focus_advance import _ideation_empty_against_focus
from ap2.init import init_project


# Direct references so a future refactor renaming any of these
# surfaces will fail this module on import — the briefing pins both
# the CLI handler and the drain branch by name.
_NAMES_FOR_DRIFT_GATE = (
    cmd_rewind_focus,
    _ideation_empty_against_focus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_GOAL_MD_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n\n"
    "Drive the project toward end-to-end automation.\n\n"
    "## Done when\n\n"
    "- An operator can point ap2 at a fresh project and walk away.\n\n"
    "{focus_section}"
    "## Non-goals\n\n"
    "- something out of scope.\n"
)


def _make_goal_md(focus_section: str) -> str:
    return _GOAL_MD_TEMPLATE.format(focus_section=focus_section)


def _write_goal_with_foci(cfg: Config, *titles: str) -> None:
    sections = "".join(
        f"## Current focus: {t}\n\nBody for {t}.\n\n" for t in titles
    )
    (cfg.project_root / "goal.md").write_text(_make_goal_md(sections))


def _project(tmp_path: Path) -> Config:
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drain(cfg: Config) -> dict:
    return tools.drain_operator_queue(cfg)


# ---------------------------------------------------------------------------
# CLI parser + handler
# ---------------------------------------------------------------------------


def test_rewind_focus_verb_registered():
    """`ap2 rewind-focus <title>` parses cleanly via build_parser —
    the verb exists in the dispatcher and accepts a positional title."""
    parser = build_parser()
    ns = parser.parse_args(["rewind-focus", "alpha"])
    assert ns.cmd == "rewind-focus"
    assert ns.title == "alpha"
    assert ns.reason is None
    # With --reason
    ns2 = parser.parse_args(
        ["rewind-focus", "alpha", "--reason", "recover from false advance"]
    )
    assert ns2.reason == "recover from false advance"


def test_rewind_focus_help_includes_verb(capsys):
    """`ap2 rewind-focus --help` mentions the verb (regression pin
    against an accidental rename / drop). Asserts the help text
    contains `rewind` so the briefing's
    `ap2 rewind-focus --help 2>&1 | grep -qi 'rewind'` verifier
    bullet has a code-level counterpart."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rewind-focus", "--help"])
    out = capsys.readouterr().out
    assert "rewind" in out.lower()


def test_cli_rejects_unknown_title(tmp_path: Path, capsys):
    """Snapshot validation runs at CLI time — a title that doesn't
    match any `## Current focus:` heading in goal.md is rejected
    immediately with exit 1, no queue record written."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")

    rc = cmd_rewind_focus(
        cfg, Namespace(title="nonexistent", reason=None)
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not match" in err
    assert "'alpha'" in err and "'beta'" in err

    queue_path = tools.operator_queue_path(cfg)
    if queue_path.exists():
        for ln in queue_path.read_text().splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            assert rec.get("op") != "rewind_focus"


def test_cli_rejects_empty_title(tmp_path: Path, capsys):
    """An empty/whitespace title is rejected at CLI time."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha")
    rc = cmd_rewind_focus(cfg, Namespace(title="   ", reason=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "required" in err.lower()


def test_cli_queues_op_on_valid_title(tmp_path: Path):
    """Happy path at the CLI layer: cmd_rewind_focus queues a
    `rewind_focus` op carrying the title (and optional reason).
    Pre-drain the queue file has the record but `focus_pointer.json`
    is unchanged — the mutation is deferred to the drain."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")
    # Seed a pointer that's past the last focus (exhausted) so the
    # rewind has something to actually rewind.
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 2
    pointer["active_title"] = ""
    pointer["exhausted_titles"] = ["alpha", "beta"]
    pointer["roadmap_complete_emitted"] = True
    pointer["empty_cycles"] = 4
    goal.save_pointer(cfg, pointer)

    pre_pointer = json.loads(goal.pointer_path(cfg).read_text())

    rc = cmd_rewind_focus(
        cfg, Namespace(title="alpha", reason="false advance recovery")
    )
    assert rc == 0

    # Pre-drain: pointer untouched on disk.
    post_pointer = json.loads(goal.pointer_path(cfg).read_text())
    assert post_pointer == pre_pointer

    # Queue file carries the op.
    queue_path = tools.operator_queue_path(cfg)
    assert queue_path.exists()
    recs = [
        json.loads(ln) for ln in queue_path.read_text().splitlines()
        if ln.strip()
    ]
    rewinds = [r for r in recs if r.get("op") == "rewind_focus"]
    assert len(rewinds) == 1
    assert rewinds[0]["args"]["title"] == "alpha"
    assert rewinds[0]["args"]["reason"] == "false advance recovery"


# ---------------------------------------------------------------------------
# Drain-side handler
# ---------------------------------------------------------------------------


def test_drain_updates_pointer_correctly(tmp_path: Path):
    """Drain-side `_apply_operator_op` `rewind_focus` branch updates
    `focus_pointer.json` to re-engage the target focus:

      - `active_index` matches the target
      - `active_title` matches the target
      - `exhausted_titles` no longer includes the target
      - `roadmap_complete_emitted` resets to False
      - `empty_cycles` resets to 0
    """
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta", "gamma")
    # Seed: roadmap fully exhausted state.
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 3
    pointer["active_title"] = ""
    pointer["exhausted_titles"] = ["alpha", "beta", "gamma"]
    pointer["roadmap_complete_emitted"] = True
    pointer["empty_cycles"] = 5
    goal.save_pointer(cfg, pointer)

    rc = cmd_rewind_focus(cfg, Namespace(title="beta", reason=""))
    assert rc == 0
    summary = _drain(cfg)
    assert summary["applied"] == 1

    after = goal.load_pointer(cfg)
    assert after["active_index"] == 1
    assert after["active_title"] == "beta"
    assert "beta" not in after["exhausted_titles"]
    # Other exhausted titles preserved (we only drop the target).
    assert "alpha" in after["exhausted_titles"]
    assert "gamma" in after["exhausted_titles"]
    assert after["roadmap_complete_emitted"] is False
    assert after["empty_cycles"] == 0


def test_drain_emits_synthetic_focus_advanced(tmp_path: Path):
    """Drain emits `focus_advanced trigger=operator_rewind` with the
    documented payload fields. Closes the audit-trail half of the
    fix; the counter-cutoff half is exercised by the dedicated
    cutoff test below."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1  # currently on beta
    pointer["active_title"] = "beta"
    pointer["exhausted_titles"] = ["alpha"]
    goal.save_pointer(cfg, pointer)

    cmd_rewind_focus(
        cfg, Namespace(title="alpha", reason="recovery from false advance")
    )
    _drain(cfg)

    tail = events.tail(cfg.events_file, 50)
    advanced = [e for e in tail if e.get("type") == "focus_advanced"]
    assert advanced, "no focus_advanced event emitted"
    e = advanced[-1]
    assert e["from"] == "beta"
    assert e["to"] == "alpha"
    assert e["trigger"] == "operator_rewind"
    assert e["new_index"] == 0
    assert e["total_foci"] == 2
    assert e["reason"] == "recovery from false advance"


def test_drain_writes_operator_log_audit_line(tmp_path: Path):
    """`operator_log.md` receives the rich
    `<ts> — operator rewound focus pointer (<old> → <target>):
    <reason>` line in addition to the standard
    `applied operator-queued rewind_focus` line."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    pointer["active_title"] = "beta"
    pointer["exhausted_titles"] = ["alpha"]
    goal.save_pointer(cfg, pointer)

    cmd_rewind_focus(
        cfg, Namespace(title="alpha", reason="false advance")
    )
    _drain(cfg)

    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    assert log_path.exists()
    text = log_path.read_text()
    # Standard verb audit line.
    assert "applied operator-queued rewind_focus" in text
    # Rich rewind-pointer audit line.
    assert "operator rewound focus pointer" in text
    assert "(beta → alpha)" in text
    assert ": false advance" in text


def test_drain_audit_line_without_reason(tmp_path: Path):
    """Empty reason renders without a trailing colon-empty (no
    `: ` suffix). The line still uses the `(old → target)` shape."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    pointer["active_title"] = "beta"
    pointer["exhausted_titles"] = ["alpha"]
    goal.save_pointer(cfg, pointer)

    cmd_rewind_focus(cfg, Namespace(title="alpha", reason=None))
    _drain(cfg)

    log_text = (
        cfg.project_root / ".cc-autopilot" / "operator_log.md"
    ).read_text()
    # Find the rich line.
    rich_lines = [
        ln for ln in log_text.splitlines()
        if "operator rewound focus pointer" in ln
    ]
    assert len(rich_lines) == 1
    line = rich_lines[0]
    assert "(beta → alpha)" in line
    # No trailing `: <reason>` since reason was empty.
    assert not line.rstrip().endswith(":")
    assert ": " not in line.split("(beta → alpha)")[1]


# ---------------------------------------------------------------------------
# Counter-cutoff semantics — the load-bearing fix
# ---------------------------------------------------------------------------


def test_counter_respects_synthetic_cutoff_in_seeded_tail():
    """Direct unit pin on `_ideation_empty_against_focus`: a seeded
    tail with 2 pre-rewind empty cycles + 1
    `focus_advanced trigger=operator_rewind to=<title>` event + 1
    post-rewind empty cycle returns 1 (only the post-rewind cycle
    counts), NOT 3 (which would be the pre-TB-295 behavior where the
    cutoff_idx scan finds no event and walks the whole tail).

    The counter looks for `focus_advanced to=<focus_title>`
    regardless of `trigger`, so the synthetic operator_rewind event
    closes the cutoff hole without any focus_advance.py change.
    """
    # 2 pre-rewind empty cycles.
    pre = [
        {"type": "ideation_empty_board"},
        {"type": "ideation_complete"},
        {"type": "ideation_empty_board"},
        {"type": "ideation_complete"},
    ]
    # The synthetic rewind event — the cutoff anchor.
    rewind_event = [
        {
            "type": "focus_advanced",
            "from": "beta",
            "to": "alpha",
            "trigger": "operator_rewind",
            "new_index": 0,
            "total_foci": 2,
            "reason": "false advance recovery",
        }
    ]
    # 1 post-rewind empty cycle — the only one that should count.
    post = [
        {"type": "ideation_empty_board"},
        {"type": "ideation_complete"},
    ]
    tail = pre + rewind_event + post

    # Counter against the rewound focus title — pre-rewind cycles
    # are below the cutoff and excluded.
    assert _ideation_empty_against_focus(tail, "alpha") == 1
    # Sanity: without the rewind event, the same pre+post tail
    # would walk the whole list and return 3.
    assert _ideation_empty_against_focus(pre + post, "alpha") == 3


def test_drain_synthetic_event_anchors_counter_end_to_end(tmp_path: Path):
    """End-to-end: seed 2 pre-rewind empty cycles, run
    cmd_rewind_focus + drain (which emits the synthetic
    `focus_advanced trigger=operator_rewind`), then seed 1
    post-rewind empty cycle. The counter computed against the
    on-disk events tail returns 1, not 3.

    This is the regression pin against the 2026-05-26 incident:
    today's recovery path (direct pointer edit) leaves the counter
    at 3, re-tripping the false advance after a single truly-empty
    post-rewind cycle; the new verb anchors the cutoff at 1."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")
    # Seed: pointer was falsely advanced to beta after alpha.
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    pointer["active_title"] = "beta"
    pointer["exhausted_titles"] = ["alpha"]
    goal.save_pointer(cfg, pointer)
    # 2 pre-rewind empty cycles (these would erroneously count
    # against the rewound focus's counter under the pre-TB-295
    # recovery path).
    for _ in range(2):
        events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
        events.append(cfg.events_file, "ideation_complete", summary="pre")

    # Operator rewinds.
    cmd_rewind_focus(cfg, Namespace(title="alpha", reason="recovery"))
    _drain(cfg)

    # 1 post-rewind empty cycle.
    events.append(cfg.events_file, "ideation_empty_board", cooldown_s=0)
    events.append(cfg.events_file, "ideation_complete", summary="post")

    tail = events.tail(cfg.events_file, 200)
    count = _ideation_empty_against_focus(tail, "alpha")
    assert count == 1, (
        f"counter walked past the synthetic cutoff "
        f"(got {count}, expected 1)"
    )


# ---------------------------------------------------------------------------
# Title-resolution race — goal.md changes between CLI and drain
# ---------------------------------------------------------------------------


def test_drain_rejects_when_goal_md_no_longer_has_title(tmp_path: Path):
    """Operator-edited goal.md drops the target title between CLI
    invocation and drain → the drain rejects with a meaningful
    error and the pointer is left unmodified. The op is still
    marked applied (so the drain doesn't loop forever) and an
    `operator_queue_error` event lands."""
    cfg = _project(tmp_path)
    _write_goal_with_foci(cfg, "alpha", "beta")
    pointer = goal.load_pointer(cfg)
    pointer["active_index"] = 1
    pointer["active_title"] = "beta"
    pointer["exhausted_titles"] = ["alpha"]
    goal.save_pointer(cfg, pointer)

    pre_pointer = json.loads(goal.pointer_path(cfg).read_text())

    rc = cmd_rewind_focus(cfg, Namespace(title="alpha", reason=None))
    assert rc == 0
    # Operator (or a concurrent `ap2 update-goal` drain) edits
    # goal.md and drops the alpha focus.
    _write_goal_with_foci(cfg, "beta")
    summary = _drain(cfg)
    # The op was processed (applied counter doesn't matter; what
    # matters is the failure was recorded and the pointer wasn't
    # mutated).
    _ = summary

    # Pointer unchanged.
    after = json.loads(goal.pointer_path(cfg).read_text())
    # Compare the operator-controlled fields (updated_ts gets
    # stamped on every save; if it didn't change, no save happened).
    for k in (
        "active_index",
        "active_title",
        "exhausted_titles",
        "roadmap_complete_emitted",
        "empty_cycles",
    ):
        assert after[k] == pre_pointer[k], (
            f"pointer.{k} mutated on rejected drain: "
            f"{pre_pointer[k]!r} → {after[k]!r}"
        )

    # No synthetic focus_advanced event for the rejected op.
    tail = events.tail(cfg.events_file, 50)
    advanced = [
        e for e in tail
        if e.get("type") == "focus_advanced"
        and e.get("trigger") == "operator_rewind"
    ]
    assert not advanced, "synthetic event leaked despite rejection"

    # `operator_queue_error` event lands so the failure is auditable.
    errors = [
        e for e in tail
        if e.get("type") == "operator_queue_error"
        and e.get("op") == "rewind_focus"
    ]
    assert errors, "no operator_queue_error for the rejected rewind"


# ---------------------------------------------------------------------------
# Op vocabulary
# ---------------------------------------------------------------------------


def test_rewind_focus_in_operator_queue_ops():
    """The op-name registry exposes `rewind_focus` — pins the queue-
    append handler's `OPERATOR_QUEUE_OPS` membership so a future
    refactor that drops the verb fails grep + this test."""
    from ap2.operator_queue import OPERATOR_QUEUE_OPS

    assert "rewind_focus" in OPERATOR_QUEUE_OPS
