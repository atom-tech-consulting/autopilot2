"""TB-352: `ap2 logs --follow` live event-monitor mode.

Folds the former loose `scripts/monitor_events.py` into the packaged
`ap2/event_monitor.py` (the `KEEP` allowlist + compact `_format_event`
formatter + the `tail -F` follow loop) and wires it onto `ap2 logs` as
a `--follow` / `-f` mode. These tests cover the *pure* surface — the
format/filter layer + events-path resolution + the parser wiring — and
the `cmd_logs` dispatch into `event_monitor.follow`. The live `tail -F`
subprocess loop is intentionally NOT unit-tested (the briefing factors
it out so the testable layer doesn't spawn `tail`); instead the
dispatch test monkeypatches `follow` to capture its kwargs.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from ap2 import cli, event_monitor
from ap2.tests.conftest import _project


# ---------------------------------------------------------------------------
# `_format_event` — the compact one-line formatter + KEEP allowlist filter.


def test_format_event_keeps_allowlisted_type():
    """A kept type (`task_start` ∈ KEEP) formats to the compact one-liner."""
    out = event_monitor._format_event(
        {"ts": "2026-05-30T08:11:01Z", "type": "task_start", "task": "TB-1"}
    )
    assert out is not None
    # `HH:MM:SS | <type> | k=v ...` shape.
    assert out.startswith("08:11:01 | task_start")
    assert "task=TB-1" in out
    assert " | " in out


def test_format_event_drops_non_allowlisted_type():
    """A non-kept type (`task_run_usage` — the canonical noise the monitor
    filters out) returns None so the follow stream stays signal-only."""
    assert "task_run_usage" not in event_monitor.KEEP  # guard the premise
    out = event_monitor._format_event(
        {"ts": "2026-05-30T08:11:01Z", "type": "task_run_usage", "task": "TB-1"}
    )
    assert out is None


def test_format_event_all_disables_allowlist():
    """`allow_all=True` (backing `ap2 logs --follow --all`) formats a type
    that is NOT in KEEP — the explicit debug escape hatch."""
    out = event_monitor._format_event(
        {"ts": "2026-05-30T08:11:01Z", "type": "task_run_usage", "task": "TB-1"},
        allow_all=True,
    )
    assert out is not None
    assert out.startswith("08:11:01 | task_run_usage")
    assert "task=TB-1" in out


def test_format_event_extracts_documented_kv_fields():
    """The documented key=val fields surface in order; absent/empty fields
    are skipped (no `key=` with an empty value)."""
    out = event_monitor._format_event(
        {
            "ts": "2026-05-30T08:11:01Z",
            "type": "operator_queue_drained",
            "task": "TB-9",
            "reason": "promote",
            "status": "ok",
            "applied": 3,
            "op": "add_backlog",
            # `trigger` / `from` / `to` / `removed_chars` intentionally absent.
        }
    )
    assert out is not None
    assert "task=TB-9" in out
    assert "reason=promote" in out
    assert "status=ok" in out
    assert "applied=3" in out
    assert "op=add_backlog" in out
    # Absent documented fields don't emit empty key= tokens.
    assert "trigger=" not in out
    assert "from=" not in out
    assert "to=" not in out
    assert "removed_chars=" not in out


def test_format_event_truncates_summary_to_cap():
    """`summary` is truncated to `SUMMARY_CAP` chars so a long summary can't
    blow out the one-line shape."""
    long_summary = "x" * (event_monitor.SUMMARY_CAP + 50)
    out = event_monitor._format_event(
        {
            "ts": "2026-05-30T08:11:01Z",
            "type": "task_complete",
            "summary": long_summary,
        }
    )
    assert out is not None
    # The rendered summary token carries exactly SUMMARY_CAP chars.
    marker = "summary="
    rendered_summary = out[out.index(marker) + len(marker):]
    assert len(rendered_summary) == event_monitor.SUMMARY_CAP
    assert rendered_summary == "x" * event_monitor.SUMMARY_CAP


def test_keep_allowlist_lives_in_package():
    """The operator-interest allowlist is now the package's source of truth
    — a representative spread of the curated types is present (TB-352
    relocates verbatim; it does not re-curate)."""
    for typ in (
        "ideation_skipped",
        "task_start",
        "task_complete",
        "verify_passed",
        "verification_failed",
        "backlog_auto_promoted",
        "operator_queue_drained",
        "daemon_start",
    ):
        assert typ in event_monitor.KEEP


# ---------------------------------------------------------------------------
# `_resolve_events_path` — the shim's `[project]` / `--events` argv contract.


def test_resolve_events_path_honors_project():
    """A project root resolves to `<project>/.cc-autopilot/events.jsonl` —
    the same path the global `--project` flag feeds `cfg.events_file`."""
    path = event_monitor._resolve_events_path("/some/proj", None)
    assert path == Path("/some/proj/.cc-autopilot/events.jsonl").resolve()


def test_resolve_events_path_explicit_events_overrides_project():
    """An explicit `--events` path wins over project resolution."""
    path = event_monitor._resolve_events_path(
        "/some/proj", "/elsewhere/events.jsonl"
    )
    assert path == Path("/elsewhere/events.jsonl").resolve()


def test_resolve_events_path_defaults_to_cwd(tmp_path, monkeypatch):
    """No project + no events → `<cwd>/.cc-autopilot/events.jsonl`."""
    monkeypatch.chdir(tmp_path)
    path = event_monitor._resolve_events_path(None, None)
    assert path == (tmp_path / ".cc-autopilot" / "events.jsonl").resolve()


# ---------------------------------------------------------------------------
# Parser wiring — `--follow` / `-f` / `--all` registered; one-shot unchanged.


def test_logs_parser_accepts_follow_and_all():
    """`build_parser()` registers `--follow` / `-f` and `--all` on `logs`."""
    parser = cli.build_parser()
    ns = parser.parse_args(["logs", "--follow", "--all"])
    assert ns.follow is True
    assert ns.all is True
    assert ns.func is cli.cmd_logs


def test_logs_parser_follow_short_flag():
    """The `-f` short flag is an alias for `--follow`."""
    parser = cli.build_parser()
    ns = parser.parse_args(["logs", "-f"])
    assert ns.follow is True
    assert ns.all is False


def test_logs_parser_one_shot_unchanged():
    """One-shot `logs` keeps its contract: `-n` default 40, `--json`/
    `--follow`/`--all` default off."""
    parser = cli.build_parser()
    ns = parser.parse_args(["logs"])
    assert ns.n == 40
    assert ns.json is False
    assert ns.follow is False
    assert ns.all is False
    # `-n N` still parses.
    ns2 = parser.parse_args(["logs", "-n", "5"])
    assert ns2.n == 5


# ---------------------------------------------------------------------------
# `cmd_logs` dispatch — `--follow` routes into event_monitor.follow.


def test_cmd_logs_follow_dispatches_to_event_monitor(tmp_path: Path, monkeypatch):
    """With `follow=True`, `cmd_logs` delegates to `event_monitor.follow`,
    passing `cfg.events_file` plus the `--all` / `--json` flags through as
    `allow_all` / `as_json`."""
    cfg = _project(tmp_path)
    captured = {}

    def _fake_follow(events_path, *, allow_all=False, as_json=False):
        captured["events_path"] = events_path
        captured["allow_all"] = allow_all
        captured["as_json"] = as_json
        return 0

    monkeypatch.setattr(event_monitor, "follow", _fake_follow)

    rc = cli.cmd_logs(cfg, Namespace(n=40, json=True, follow=True, all=True))
    assert rc == 0
    assert captured["events_path"] == cfg.events_file
    assert captured["allow_all"] is True
    assert captured["as_json"] is True


def test_cmd_logs_one_shot_does_not_touch_event_monitor(
    tmp_path: Path, monkeypatch, capsys,
):
    """Without `--follow`, `cmd_logs` never reaches `event_monitor.follow`
    — the one-shot dump path is unchanged (back-compat)."""
    cfg = _project(tmp_path)

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("one-shot logs must not call event_monitor.follow")

    monkeypatch.setattr(event_monitor, "follow", _boom)

    from ap2 import events

    events.append(cfg.events_file, "task_start", task="TB-1")
    rc = cli.cmd_logs(cfg, Namespace(n=10, json=False, follow=False, all=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "task_start" in out


def test_cmd_logs_back_compat_namespace_without_follow_attr(
    tmp_path: Path, capsys,
):
    """The TB-158 / TB-180 unit tests build a bare `Namespace(n=, json=)`
    with no `follow` / `all` attributes; `cmd_logs` must still render the
    one-shot dump via the `getattr` guard rather than raising."""
    cfg = _project(tmp_path)
    from ap2 import events

    events.append(cfg.events_file, "task_complete", task="TB-2")
    rc = cli.cmd_logs(cfg, Namespace(n=10, json=False))
    assert rc == 0
    assert "task_complete" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Shim — `scripts/monitor_events.py` no longer owns the allowlist/format.


def test_monitor_events_script_is_a_shim():
    """The loose script defines neither `KEEP` nor `_format_event` — both
    moved into the package; it imports + delegates to `ap2.event_monitor`."""
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "scripts" / "monitor_events.py").read_text()
    assert "KEEP = {" not in text
    assert "def _format_event" not in text
    # It delegates to the packaged entrypoint.
    assert "ap2.event_monitor" in text
    assert "follow" in text
