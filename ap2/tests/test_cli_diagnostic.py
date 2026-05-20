"""Tests for the cli-prefixed diagnostic verbs (TB-266 split from
`test_cli.py`).

Mirrors `ap2/cli_diagnostic.py` (TB-264 split): cmd_doctor / cmd_check /
cmd_logs / cmd_cron_list / cmd_cron_edit / cmd_init. Verb groupings
preserved from the pre-split section headers — see the divider
comments below for the TB-N each block traces back to.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from ap2 import events
from ap2.board import Board
from ap2.config import Config
from ap2.cron import load_jobs
from ap2.tests.conftest import _project


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
# TB-202 (cron-edit half): `ap2 cron edit` writes fenced files
# synchronously (bypassing the operator-queue routing pattern). If the
# operator runs it while a task agent is in flight, the TB-110 post-hoc
# snapshot diff detects the fenced-file mutation and rolls the task
# back — same false-positive cascade as the pre-TB-201 `ap2 ack` path.
# TB-202's cheaper-than-queue-routing mitigation is a pre-flight
# refuse-if-active check on the verb; these tests pin the refusal
# text, the exit code, and the "fenced state untouched on refuse"
# invariant. The companion `cmd_backfill_proposals` half of TB-202
# lives in `test_cli_review.py` since `cmd_backfill_proposals` is a
# review-surface verb (TB-264 source split).


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
