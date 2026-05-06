"""TB-128 + TB-144: skip-if-idle gate for the status-report routine.

Status-report has historically posted reports with stale headline
timestamps when nothing changed between runs (the agent re-rendered text
from a prior context's cache). The fix has two layers:

1. The prompt builder injects a deterministic `## Current state` block
   with a fresh UTC `now:` timestamp and binds the status-report job to
   "use that value verbatim" (covered in `test_prompts.py`).
2. The daemon's `run_cron` short-circuits the agent invocation entirely
   when no events of interest have happened since the last
   `cron_complete job=status-report` — covered here.

Both layers are belt-and-braces: the agent could still ignore the prompt
contract, but the daemon-level gate prevents an SDK turn from being
burned in the first place when there's nothing new to report.

TB-144 hoisted the gate (and the surrounding `run_status_report`
routine) into `ap2.status_report` so the chat-trigger MCP tool shares
the same skip semantics as the cron tick. The `_status_report_should_skip`
import is preserved on `ap2.daemon` as a re-export — the symbol moved,
the import contract didn't. Tests below exercise both call sites
(cron-trigger and chat-trigger) against the same gate.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ap2 import events, status_report as status_report_mod
from ap2.config import Config
from ap2.cron import CronJob
from ap2.daemon import _status_report_should_skip, run_cron
from ap2.status_report import run_status_report


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def test_skip_returns_false_when_never_run(tmp_path):
    """First run ever (or last run rolled out of the tail) — never skip."""
    cfg = _cfg(tmp_path)
    # Empty events log: no prior cron_complete to anchor against.
    assert _status_report_should_skip(cfg) is False


def test_skip_returns_true_when_only_self_events_after_last_complete(tmp_path):
    """Back-to-back run: previous cron_complete is in the tail and the only
    events since are self-bookkeeping (cron_start/cron_complete for
    status-report itself, the agent's own status_report log_event, and the
    outbound mattermost_reply that quoted the report headline). Nothing of
    substance happened — skip.
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_start", job="status-report")
    events.append(
        cfg.events_file, "mattermost_reply",
        channel="ap2",
        summary="**Autopilot Status Report** — 2026-04-30T10:00Z\n• ...",
    )
    events.append(
        cfg.events_file, "status_report",
        summary="Posted to #ap2: idle since last report.",
    )
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # Nothing else happens before the next run fires.
    assert _status_report_should_skip(cfg) is True


def test_skip_returns_false_when_task_completed_since_last_run(tmp_path):
    """A `task_complete` event between runs is exactly the kind of activity
    the status report is supposed to surface — must not skip."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_start", job="status-report")
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-99", status="complete", commit="deadbee",
        summary="did the thing",
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_returns_false_when_pipeline_event_since_last_run(tmp_path):
    """Pipeline activity is interesting — must not skip."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "pipeline_complete",
        task="TB-50", name="data-fetch", pid=12345,
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_returns_false_when_verification_failed_since_last_run(tmp_path):
    """Verification failures are interesting — must not skip."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "verification_failed",
        task="TB-77", overall="fail",
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_filters_self_mattermost_reply_by_summary(tmp_path):
    """The cron's own outbound `mattermost_reply` (summary starts with
    "**Autopilot Status Report**") is self-noise — must not count as
    activity. A non-self mattermost_reply (e.g. handler answering the
    operator) IS activity.
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # Self-noise: the cron's outbound headline post.
    events.append(
        cfg.events_file, "mattermost_reply",
        channel="ap2",
        summary="**Autopilot Status Report** — 2026-04-30T10:00Z",
    )
    assert _status_report_should_skip(cfg) is True

    # Now a non-self mattermost_reply (handler responding to operator) —
    # this IS interesting activity.
    events.append(
        cfg.events_file, "mattermost_reply",
        channel="ap2",
        summary="Pausing the daemon as requested.",
    )
    assert _status_report_should_skip(cfg) is False


def test_skip_filters_other_status_report_log_events(tmp_path):
    """Self-emitted `status_report` log_events between cron_completes (e.g.
    a prior skipped run) must not register as activity."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "status_report",
        summary="skipped: no activity since last report",
    )
    events.append(cfg.events_file, "cron_skipped",
                  job="status-report", reason="no_activity_since_last_report")
    assert _status_report_should_skip(cfg) is True


# ---------------------------------------------------------------------------
# Integration: run_cron honors the skip gate end-to-end.


class _NoopSDK:
    """SDK stub that records whether `query` was called."""

    def __init__(self) -> None:
        self.called = False

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


def test_run_cron_skips_status_report_when_idle(tmp_path):
    """run_cron must short-circuit (no SDK call, but `cron_skipped` event
    + cron_state mark) when the gate says skip."""
    cfg = _cfg(tmp_path)
    # Seed a prior cron_complete with no follow-up activity so the gate
    # returns True.
    events.append(cfg.events_file, "cron_complete", job="status-report")

    sdk = _NoopSDK()
    job = CronJob(
        name="status-report", interval_s=60, prompt="post a report",
        max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    assert sdk.called is False, "skipped run must not invoke the SDK"

    # Skip event landed; no cron_start / cron_complete from this aborted run.
    evts = events.tail(cfg.events_file, 50)
    skipped = [e for e in evts if e.get("type") == "cron_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["job"] == "status-report"
    assert skipped[0]["reason"] == "no_activity_since_last_report"

    # cron_state was advanced so the daemon doesn't re-fire every tick.
    import json

    state = json.loads(cfg.cron_state_file.read_text())
    assert "status-report" in state and state["status-report"] > 0


def test_run_cron_does_not_skip_when_activity_present(tmp_path, monkeypatch):
    """run_cron must NOT skip when meaningful activity has happened since
    the last status report — and must reach the SDK invocation path.

    We stub the SDK with a no-op generator (returns immediately) so the
    test doesn't depend on real Claude wiring; the assertion is that
    `query` was reached at all. We also patch the prompt builder out so
    the test doesn't need a real `Bash` for `git log` (the helper handles
    a non-git tmp_path, but the safe.directory subprocess invocation in
    a CI sandbox is not worth the surface area for this assertion).
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    monkeypatch.setattr(
        "ap2.daemon.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    sdk = _NoopSDK()
    job = CronJob(
        name="status-report", interval_s=60, prompt="post a report",
        max_turns=5,
    )
    asyncio.run(run_cron(cfg, sdk, mcp_server=None, job=job))

    assert sdk.called is True, "active state should reach the SDK"
    evts = events.tail(cfg.events_file, 50)
    kinds = [e["type"] for e in evts]
    assert "cron_start" in kinds
    assert "cron_complete" in kinds
    assert "cron_skipped" not in kinds


# ---------------------------------------------------------------------------
# cron.default.yaml prompt content must encode the freshness contract.


def test_status_report_prompt_constant_pins_freshness_contract():
    """TB-144: the canonical status-report prompt body now lives at
    `ap2.status_report.STATUS_REPORT_PROMPT` (hoisted out of
    `cron.default.yaml` so the chat-trigger MCP tool can share it). The
    headline-timestamp / fresh-read / skip-if-idle rules must be pinned
    on that constant — operators editing the constant change the report
    shape for both cron and chat triggers in lockstep.

    Pre-TB-144 this test asserted against the cron.default.yaml prompt;
    that prompt is now a stub explaining the migration (the daemon
    ignores it for the status-report job). The cron stub is checked
    separately in `test_cron_default_status_report_prompt_is_stub`.
    """
    from ap2.status_report import STATUS_REPORT_PROMPT

    body = STATUS_REPORT_PROMPT
    # Headline timestamp pin.
    assert "Freshness contract" in body
    assert "`now:` value" in body
    # Re-read pin.
    assert "events.jsonl" in body
    assert "TASKS.md" in body
    # Skip-if-idle pin.
    assert "SKIP" in body
    assert "status_report" in body


def test_cron_default_status_report_prompt_is_stub():
    """TB-144: `cron.default.yaml`'s status-report prompt body is now an
    intentional stub that points at the canonical constant in
    `ap2.status_report`. The daemon's `run_cron` ignores `job.prompt`
    for the `status-report` job (see `daemon.run_cron` →
    `status_report.run_status_report`), so an operator who edited their
    local cron.yaml's prompt body pre-TB-144 won't see drift between
    chat and cron reports — the routine is authoritative either way.

    The stub mentions the canonical location so a curious operator
    reading `ap2 cron list` follows the breadcrumb. The cron job's
    `interval` and `max_turns` fields are still tunable from
    cron.yaml — only the prompt body is hoisted.
    """
    from ap2.cron import load_jobs

    default = (
        Path(__file__).resolve().parent.parent / "cron.default.yaml"
    )
    jobs = {j.name: j for j in load_jobs(default)}
    sr = jobs["status-report"]
    body = sr.prompt
    # The stub says where the canonical content lives.
    assert "ap2.status_report" in body
    assert "STATUS_REPORT_PROMPT" in body
    # Anti-regression: the freshness contract must NOT live in two
    # places (drift hazard). It lives in STATUS_REPORT_PROMPT only.
    assert "Freshness contract" not in body
    # Cron's interval / max_turns are still meaningful — operator-tunable.
    assert sr.interval_s == 7200
    assert sr.max_turns == 10


# ---------------------------------------------------------------------------
# TB-144: chat-trigger semantics for `run_status_report`.
#
# The shared routine routes both the cron tick and the on-demand
# `mcp__autopilot__status_report_run` MCP tool. Tests below pin the
# trigger-aware semantics: the skip-gate fires for both triggers, but
# `cron_state` advance only happens for cron triggers (otherwise an
# operator-triggered report at 11:00 would silence the scheduled noon
# cron).


def test_run_status_report_chat_trigger_honors_skip_gate(tmp_path):
    """`run_status_report(trigger="chat")` honors the same skip-if-idle
    gate as the cron path. On skip, no SDK turn is burned; a
    `cron_skipped` event lands with `trigger="chat"` so post-mortems can
    distinguish chat-trigger skips from cron-trigger skips."""
    cfg = _cfg(tmp_path)
    # Seed a recent cron_complete with no follow-up activity → gate fires.
    events.append(cfg.events_file, "cron_complete", job="status-report")

    sdk = _NoopSDK()
    result = asyncio.run(
        run_status_report(
            cfg, sdk, mcp_server=None,
            trigger="chat", reason="operator asked",
        )
    )

    assert result.skipped is True
    assert result.reason == "no_activity_since_last_report"
    assert sdk.called is False, "skipped chat trigger must not invoke the SDK"

    evts = events.tail(cfg.events_file, 50)
    skipped = [e for e in evts if e.get("type") == "cron_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["trigger"] == "chat"
    assert skipped[0]["reason"] == "no_activity_since_last_report"
    # The operator's reason rides on the skipped event so the audit trail
    # explains what was asked for, even when nothing was posted.
    assert skipped[0].get("chat_reason") == "operator asked"


def test_run_status_report_cron_trigger_advances_state(tmp_path, monkeypatch):
    """`trigger="cron"` advances `cron_state[status-report].last_run` so
    the daemon's `due_jobs` won't immediately re-fire the cron. Pin both
    the state file write and the `trigger="cron"` field on the
    `cron_start` / `cron_complete` events."""
    cfg = _cfg(tmp_path)
    # Seed activity so the skip-gate doesn't fire — we want to exercise
    # the run path, not the skip path.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
    )
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is True

    state = json.loads(cfg.cron_state_file.read_text())
    assert "status-report" in state
    assert state["status-report"] > 0, (
        "cron-trigger must advance cron_state.last_run so due_jobs sees "
        "the run as recent"
    )

    evts = events.tail(cfg.events_file, 50)
    starts = [e for e in evts if e.get("type") == "cron_start"
              and e.get("job") == "status-report"]
    completes = [e for e in evts if e.get("type") == "cron_complete"
                 and e.get("job") == "status-report"]
    # Filter out the seeded cron_complete (no `trigger` field — pre-call).
    completes_with_trigger = [e for e in completes if "trigger" in e]
    assert any(e.get("trigger") == "cron" for e in starts)
    assert any(e.get("trigger") == "cron" for e in completes_with_trigger)


def test_run_status_report_chat_trigger_does_not_advance_state(tmp_path, monkeypatch):
    """The chat trigger MUST NOT advance `cron_state[status-report].last_run`.
    Otherwise an operator-triggered report at 11:00 would silence the
    scheduled noon cron — the opposite of what the operator asked for.

    The contract: cron and chat triggers share the prompt + skip-gate +
    audit shape, but cron-trigger owns the schedule. Chat triggers are
    additive, not replacement.
    """
    cfg = _cfg(tmp_path)
    # Seed activity so the run path is exercised (skip-gate doesn't fire).
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
    )
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(
            cfg, sdk, mcp_server=None,
            trigger="chat", reason="operator asked",
        )
    )

    assert sdk.called is True, "chat-trigger run still hits the SDK"

    # The cron_state file must remain untouched by the chat trigger.
    # (It's allowed to not exist at all if no prior cron run created it.)
    if cfg.cron_state_file.exists():
        state = json.loads(cfg.cron_state_file.read_text())
        assert "status-report" not in state, (
            f"chat-trigger should NOT have written cron_state; got {state!r}"
        )

    # The cron_start event carries trigger="chat" and the operator's reason.
    evts = events.tail(cfg.events_file, 50)
    starts = [e for e in evts if e.get("type") == "cron_start"
              and e.get("job") == "status-report"
              and e.get("trigger") == "chat"]
    assert len(starts) == 1
    assert starts[0].get("reason") == "operator asked"

    # cron_complete also carries trigger="chat".
    completes = [e for e in evts if e.get("type") == "cron_complete"
                 and e.get("job") == "status-report"
                 and e.get("trigger") == "chat"]
    assert len(completes) == 1


def test_run_status_report_cron_trigger_skip_advances_state(tmp_path):
    """Symmetric to the run path: cron-trigger SKIP also advances state
    (so the daemon doesn't re-fire every tick when nothing is happening).
    Chat-trigger skip does not — chat skips never silence cron.
    """
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is False
    state = json.loads(cfg.cron_state_file.read_text())
    assert state["status-report"] > 0


def test_run_status_report_chat_trigger_skip_does_not_advance_state(tmp_path):
    """Mirror of the run-path test: chat-trigger skip must NOT advance
    cron_state. If it did, the next scheduled cron would think a recent
    run had already happened and slip past its interval."""
    cfg = _cfg(tmp_path)
    events.append(cfg.events_file, "cron_complete", job="status-report")

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(
            cfg, sdk, mcp_server=None,
            trigger="chat", reason="operator asked",
        )
    )

    assert sdk.called is False
    if cfg.cron_state_file.exists():
        state = json.loads(cfg.cron_state_file.read_text())
        assert "status-report" not in state


# ---------------------------------------------------------------------------
# TB-151: pending-review TB-Ns in the snapshot block + agent-prompt forwarder.
#
# The status-report routine collects Backlog tasks whose only blocker is
# the `review` scheme and injects "Pending operator review (N): TB-..."
# into the `## Current state` snapshot block via `state_extras` on
# `build_control_prompt`. The prompt body separately instructs the agent
# to copy the line verbatim into the posted Mattermost report. Both
# halves are pinned below.


def _seed_active_for_run(cfg: Config) -> None:
    """Seed an event so the skip-gate doesn't fire — we need the run path."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
    )


def _board_with_review_tasks(cfg: Config, ids: list[str]) -> None:
    """Seed `ids` into the Backlog with the `@blocked:review` codespan."""
    from ap2.board import Board

    board = Board.load(cfg.tasks_file)
    for i, tid in enumerate(ids):
        board.add(
            "Backlog", task_id=tid, title=f"prop {i}",
            meta={"blocked": "review"},
        )
    board.save()


def test_run_status_report_injects_pending_review_line_when_n_positive(
    tmp_path, monkeypatch,
):
    """When the board carries N>0 review-gated Backlog tasks, the prompt
    handed to the SDK contains a "Pending operator review (N): TB-..." line
    inside the `## Current state` snapshot block, with the IDs listed."""
    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)
    _board_with_review_tasks(cfg, ["TB-300", "TB-301", "TB-302"])

    captured: dict[str, str] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        # Reproduce build_control_prompt's behavior just enough to thread
        # state_extras into the captured prompt for the assertion.
        block = "## Current state\n"
        if state_extras:
            block += "\n".join(state_extras) + "\n"
        captured["prompt"] = block + f"\n## Control job: {name}\n{body}"
        return captured["prompt"]

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture_prompt)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is True
    prompt = captured["prompt"]
    # The injected line appears in the prompt fed to the SDK, with the
    # "(N): " preamble + each TB-N + the action hint.
    assert "Pending operator review (3):" in prompt
    assert "TB-300" in prompt
    assert "TB-301" in prompt
    assert "TB-302" in prompt
    assert "ap2 approve TB-N" in prompt


def test_run_status_report_truncates_pending_review_in_snapshot(
    tmp_path, monkeypatch,
):
    """6 review-gated tasks → snapshot line truncates to first 5 + "(+1 more)",
    matching the CLI's truncation cap (helpers shared)."""
    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)
    ids = [f"TB-{n}" for n in range(400, 406)]  # TB-400 .. TB-405
    _board_with_review_tasks(cfg, ids)

    captured: dict[str, str] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        captured["extras"] = "\n".join(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture_prompt)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    extras = captured["extras"]
    # First 5 named.
    for tid in ids[:5]:
        assert tid in extras
    # 6th truncated out of the text but counted in the suffix; total N=6.
    assert "TB-405" not in extras
    assert "(+1 more)" in extras
    assert "Pending operator review (6):" in extras


def test_run_status_report_omits_pending_review_line_when_zero(
    tmp_path, monkeypatch,
):
    """Clean board (zero review-gated tasks) → no snapshot line, so a
    routine post doesn't grow a noisy "0 pending" bullet."""
    # TB-190: the daemon now also injects a `post target channel:` line
    # when either env var is set. This test asserts the strictly-empty
    # extras path, so isolate from the operator's actual env (the user
    # who runs the suite likely has `AP2_MM_CHANNELS` set in their shell).
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)
    # No review-gated Backlog tasks added.

    captured: dict[str, list[str]] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture_prompt)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    # Nothing injected — list is empty.
    assert captured["extras"] == []


# ---------------------------------------------------------------------------
# TB-187: `_pending_review_ids` (the helper that drives the snapshot
# block's "Pending operator review (N): TB-..." line) must include
# mixed-blocker tasks. Pre-fix it stripped any task carrying a
# non-review blocker alongside `review`; the operator never saw it on
# the cron-driven status post.

def test_pending_review_ids_includes_mixed_blocker(tmp_path):
    """Synthesize a Backlog with one pure-review task, one mixed
    review+TB-X task, and one pure TB-X task. `_pending_review_ids`
    returns the first two; the third stays out (`review` not among its
    blockers). Pre-TB-187 only the first appeared."""
    from ap2.board import Board
    from ap2.status_report import _pending_review_ids

    cfg = _cfg(tmp_path)
    board = Board.load(cfg.tasks_file)
    board.add(
        "Backlog", task_id="TB-770", title="pure review",
        meta={"blocked": "review"},
    )
    board.add(
        "Backlog", task_id="TB-771", title="mixed review and TB-99",
        meta={"blocked": "review,TB-99"},
    )
    board.add(
        "Backlog", task_id="TB-772", title="pure dep",
        meta={"blocked": "TB-99"},
    )
    board.save()

    ids = _pending_review_ids(cfg)
    assert set(ids) == {"TB-770", "TB-771"}


def test_status_report_prompt_instructs_forwarding_pending_review_line():
    """The canonical STATUS_REPORT_PROMPT body must tell the agent to
    forward the "Pending operator review" snapshot line into the posted
    Mattermost report verbatim. Without this instruction the snapshot
    line would land in the agent's context but not in the operator-
    visible report — defeating the purpose of the injection."""
    from ap2.status_report import STATUS_REPORT_PROMPT

    body = STATUS_REPORT_PROMPT
    assert "Pending operator review" in body
    # The forwarding rule is explicit (verbatim copy, not paraphrase).
    assert "verbatim" in body.lower() or "VERBATIM" in body


# ---------------------------------------------------------------------------
# TB-173 / TB-191: ideator decisions-needed snapshot block + agent-prompt
# forwarder.
#
# Mirrors the TB-151 plumbing pattern: `parse_operator_decisions` reads
# the `## Decisions needed from operator` section from
# `.cc-autopilot/ideation_state.md` (renamed from the pre-TB-191 `## Open
# questions for operator`) and the routine injects a
# "Decisions needed from operator (N): ..." line into the snapshot's
# state_extras. The prompt body separately tells the agent to forward
# the line VERBATIM into the posted Mattermost report.
#
# TB-191 also added the agent-internal `## Cycle observations` section
# that MUST NOT leak to operator-facing surfaces. The cron status-report
# is one of those surfaces — the leak-guard test at the bottom of this
# block pins that observations content never enters the snapshot
# state_extras even when both sections coexist on disk.


def _seed_ideation_state(cfg: Config, body: str) -> None:
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_run_status_report_injects_operator_decisions_line_when_present(
    tmp_path, monkeypatch,
):
    """When ideation_state.md has a non-empty decisions-needed section,
    the routine injects a "Decisions needed from operator (N): ..."
    line into `state_extras` so the agent's snapshot block carries it.
    Pins the state_extras → build_control_prompt plumbing for
    TB-173 / TB-191."""
    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)
    _seed_ideation_state(
        cfg,
        "## Decisions needed from operator\n\n"
        "- Decision needed: should goal.md declare a new focus?\n"
        "- Approve or reject TB-171 / TB-172 / TB-173.\n"
        "- Operator input required: rotate focus item?\n",
    )

    captured: dict[str, str] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        captured["extras"] = "\n".join(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture_prompt)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    extras = captured["extras"]
    # Snapshot line carries the count + each bullet joined by `; ` so the
    # agent can copy it verbatim into the report's bullet list.
    assert "Decisions needed from operator (3):" in extras
    assert "Decision needed: should goal.md declare a new focus?" in extras
    assert "Approve or reject TB-171 / TB-172 / TB-173." in extras
    assert "Operator input required: rotate focus item?" in extras


def test_run_status_report_omits_operator_decisions_line_when_empty(
    tmp_path, monkeypatch,
):
    """No ideation_state.md / no section → snapshot has no
    decisions-needed line, so a routine post doesn't grow a noisy
    "0 decisions needed" bullet. Mirrors the omit-on-zero shape of the
    pending-review line."""
    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)
    # No ideation_state.md created.

    captured: dict[str, list[str]] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture_prompt)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    # No decisions-needed line in the extras — list may still carry other
    # entries (e.g. the pending-review line) but nothing about
    # "Decisions needed from operator".
    joined = "\n".join(captured["extras"])
    assert "Decisions needed from operator" not in joined


def test_status_report_prompt_instructs_forwarding_operator_decisions_line():
    """The canonical STATUS_REPORT_PROMPT body must tell the agent to
    forward the "Decisions needed from operator" snapshot line into the
    posted Mattermost report verbatim. Without this instruction the
    snapshot line lands in the agent's context but not in the operator-
    visible report — same failure shape as the TB-151 pin above."""
    from ap2.status_report import STATUS_REPORT_PROMPT

    body = STATUS_REPORT_PROMPT
    assert "Decisions needed from operator" in body
    # Forwarding rule must be explicit somewhere in the prompt body.
    assert "verbatim" in body.lower() or "VERBATIM" in body


def test_run_status_report_does_not_leak_cycle_observations(
    tmp_path, monkeypatch,
):
    """TB-191: when ideation_state.md carries BOTH `## Decisions needed
    from operator` (with two valid bullets) AND `## Cycle observations`
    (with three observation-shaped bullets), the cron status-report
    routine must inject ONLY the decisions content into state_extras
    and NEVER any line referencing the cycle-observations bullets.
    The agent-internal observations section is structurally excluded
    by `parse_operator_decisions` — this test pins the structural
    exclusion at the cron-post forwarding flow, not just the parser."""
    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)
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

    captured: dict[str, str] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        captured["extras"] = "\n".join(state_extras or [])
        return "stub"

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _capture_prompt)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    extras = captured["extras"]
    # Decisions content is present in the snapshot line.
    assert "Decisions needed from operator (2):" in extras
    assert "Decision needed: approve TB-200?" in extras
    assert (
        "Operator input required: rotate focus to verifier robustness?"
        in extras
    )
    # None of the cycle-observations content leaks into state_extras.
    for forbidden in (
        "n=3 retries on bullet kind Y",
        "No unadopted cron_proposed events",
        "Cadence is steady at 12 ticks/min",
    ):
        assert forbidden not in extras, (
            f"TB-191: cycle-observations bullet leaked into the cron "
            f"status-report state_extras: {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# TB-182 / TB-191: validate-against-events instruction for forwarded
# TB-N references.
#
# The decisions-needed snapshot line forwards bullets the ideator wrote
# at the last `ideation_state_updated` event. Up to ~2h of staleness can
# bleed through (the gap between ideation cycles). The status-report
# agent's prompt body must instruct it to cross-check forwarded TB-N
# references against events.jsonl for any superseding `task_complete`,
# `task_deleted`, `task_updated`, or `verification_failed` event AFTER
# the `ideation_state_updated` ts, and skip / annotate stale bullets.


def test_status_report_prompt_pins_validation_against_events():
    """TB-182: the canonical STATUS_REPORT_PROMPT body must instruct the
    agent to validate forwarded TB-N references against events.jsonl
    before posting. Pin the load-bearing markers so a paraphrase that
    drops the cross-check trips this test:

      - `ideation_state_updated` (the timestamp anchor)
      - `task_complete` (one of the four superseding event types)
      - `events.jsonl` (the source of truth the agent walks)
      - `TB-182` (cross-ref so future trims preserve the lineage)
    """
    from ap2.status_report import STATUS_REPORT_PROMPT

    body = STATUS_REPORT_PROMPT
    # Anchor: `ideation_state_updated` is the timestamp the agent uses
    # to decide which events count as "after the decisions-needed
    # content was last refreshed".
    assert "ideation_state_updated" in body, (
        "TB-182: the validation instruction needs the "
        "`ideation_state_updated` event-name anchor so the agent knows "
        "which timestamp to compare against."
    )
    # At least one of the four superseding event types must be named
    # so the agent knows what shape of event invalidates a bullet.
    assert "task_complete" in body
    # The four-event list should be enumerated together — pin the
    # other three names too so a regression that names only one of
    # them surfaces here.
    for ev in ("task_deleted", "task_updated", "verification_failed"):
        assert ev in body, (
            f"TB-182: superseding event type {ev!r} missing from the "
            f"status-report validation instruction"
        )
    # The agent walks `events.jsonl` directly (already in context).
    assert "events.jsonl" in body
    # TB-182 cross-ref so future trims preserve the lineage.
    assert "TB-182" in body


def test_status_report_prompt_validation_instruction_describes_skip_or_annotate():
    """TB-182: the validation instruction must explain BOTH branches —
    when a stale bullet is found, the agent either SKIPS it or
    REWRITES it with a parenthetical noting the staleness. A prompt
    that says "validate" but doesn't tell the agent what to DO with
    a stale bullet leaves the resolution undefined.
    """
    from ap2.status_report import STATUS_REPORT_PROMPT

    body = STATUS_REPORT_PROMPT
    lower = body.lower()
    # Skip branch.
    assert "skip" in lower, (
        "TB-182: validation instruction missing the skip-stale-bullet branch"
    )
    # Rewrite-with-parenthetical branch.
    assert (
        "stale ideation_state.md" in lower
        or "rewrite" in lower
        or "parenthetical" in lower
    ), (
        "TB-182: validation instruction missing the rewrite-with-"
        "parenthetical branch"
    )
    # The "if not found, forward as-is" branch — the no-staleness path
    # must be explicit so the agent doesn't drop bullets that are still
    # current.
    assert "as-is" in lower or "as is" in lower or "still current" in lower, (
        "TB-182: validation instruction missing the no-staleness "
        "forward-as-is branch"
    )


def test_run_status_report_smoke_stale_operator_decisions_bullet(tmp_path, monkeypatch):
    """TB-182 / TB-191 smoke test: when the project has an
    `ideation_state.md` with a `TB-X retry watch` decisions-needed
    bullet AND `events.jsonl` contains a `task_complete TB-X` event
    AFTER the `ideation_state_updated` ts, the prompt the agent
    receives must carry BOTH (so the agent has the context to
    validate) AND the validation instruction (so it knows to
    validate). We can't pin the agent's actual reasoning without an
    integration test; the prompt-content + event-presence pins are
    the load-bearing assertions.

    Threading: use `_OptionsCapturingSDK` so the prompt actually
    handed to the SDK is captured (snapshot-block + body), then assert
    the captured prompt contains the decisions-needed snapshot line,
    the `task_complete` event marker, and the validation instruction.
    """
    cfg = _cfg(tmp_path)
    # Seed: an old ideation_state_updated event, then a task_complete
    # event for TB-X landing AFTER it. The skip-gate also needs at least
    # one non-self event after the most recent cron_complete; the
    # task_complete satisfies both that gate AND the staleness fixture.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(cfg.events_file, "ideation_state_updated", bytes=200)
    events.append(
        cfg.events_file, "task_complete",
        task="TB-999", status="complete", commit="abc1234",
        summary="TB-999 landed Complete after the ideation snapshot",
    )
    # Decisions-needed section referencing TB-999 — this is the bullet
    # the agent must validate as stale (since TB-999 has since
    # task_complete'd) and either skip or annotate.
    _seed_ideation_state(
        cfg,
        "## Decisions needed from operator\n\n"
        "- **TB-999 retry watch (n=1 prose-bullet over-specification)**: "
        "decision needed: should we re-rank the retry watch now that the "
        "task landed Complete?\n",
    )

    from ap2 import prompts as _prompts_mod

    real_build = _prompts_mod.build_control_prompt
    captured: dict[str, str] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        # Reproduce build_control_prompt's structure faithfully enough
        # that the assertion can find both halves: the snapshot line
        # injected via `state_extras` (TB-173) and the validation
        # instruction in `body` (TB-182).
        block = "## Current state\n"
        if state_extras:
            block += "\n".join(state_extras) + "\n"
        out = block + f"\n## Control job: {name}\n{body}"
        captured["prompt"] = out
        return out

    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt", _capture_prompt
    )

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is True, "skip-gate fired unexpectedly"
    prompt = captured["prompt"]
    # The decisions-needed snapshot line carries the TB-X reference the
    # agent must validate.
    assert "Decisions needed from operator (1):" in prompt
    assert "TB-999 retry watch" in prompt
    # The validation instruction is in the body.
    assert "ideation_state_updated" in prompt
    assert "task_complete" in prompt
    assert "TB-182" in prompt
    # The events-file fixture exists on disk so the agent's Read tool
    # could observe both events at runtime (the prompt instructs the
    # agent to re-read the events tail with Read).
    assert cfg.events_file.is_file()
    evts = events.tail(cfg.events_file, 50)
    types = [e.get("type") for e in evts]
    assert "ideation_state_updated" in types
    assert any(
        e.get("type") == "task_complete" and e.get("task") == "TB-999"
        for e in evts
    )

    # Sanity: the same fixture wired through the REAL build_control_prompt
    # would also expose both halves to the agent. We don't run the real
    # builder here (it pulls in `git log` + a board snapshot, which is
    # out of scope for this prompt-content assertion) but we verify the
    # body-level pin lands on the source constant directly so a future
    # `build_control_prompt` rewrite can't drop the validation
    # instruction without flipping the body-level test above.
    from ap2.status_report import STATUS_REPORT_PROMPT
    assert "ideation_state_updated" in STATUS_REPORT_PROMPT
    assert "task_complete" in STATUS_REPORT_PROMPT


def test_run_status_report_smoke_no_staleness_operator_decisions_bullet(
    tmp_path, monkeypatch,
):
    """TB-182 / TB-191 smoke test, no-staleness branch: when
    `events.jsonl` has NO `task_complete` / `task_deleted` /
    `task_updated` / `verification_failed` event for the bullet's
    TB-N AFTER the `ideation_state_updated` ts, the bullet is current
    and should forward as-is. The prompt structure must give the agent
    the context to make that call: the snapshot line is present, the
    validation instruction is present, and `events.jsonl` does NOT
    contain a superseding event for the referenced TB-N.

    Mirrors the staleness fixture above but inverts the events tail
    so the no-staleness branch of the validation instruction is
    pinned.
    """
    cfg = _cfg(tmp_path)
    # Seed activity that satisfies the skip-gate but does NOT match the
    # bullet's TB-N — i.e. the staleness check should find no
    # superseding event.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(cfg.events_file, "ideation_state_updated", bytes=200)
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
        summary="unrelated task",
    )
    # Decisions-needed bullet references TB-998 — but no event for
    # TB-998 appears AFTER the ideation_state_updated ts.
    _seed_ideation_state(
        cfg,
        "## Decisions needed from operator\n\n"
        "- **TB-998 retry watch (n=1 prose-bullet over-specification)**: "
        "decision needed: should we keep the retry watch open?\n",
    )

    captured: dict[str, str] = {}

    def _capture_prompt(cfg, name, body, *, state_extras=None):
        block = "## Current state\n"
        if state_extras:
            block += "\n".join(state_extras) + "\n"
        out = block + f"\n## Control job: {name}\n{body}"
        captured["prompt"] = out
        return out

    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt", _capture_prompt
    )

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is True
    prompt = captured["prompt"]
    # Snapshot line + bullet present — the agent has something to
    # validate (and forward unchanged once it confirms freshness).
    assert "Decisions needed from operator (1):" in prompt
    assert "TB-998 retry watch" in prompt
    # Validation instruction present — the agent knows to validate.
    assert "ideation_state_updated" in prompt
    assert "task_complete" in prompt
    # The "if not found, forward as-is" branch is named in the prompt
    # so the agent has explicit guidance for the no-staleness case.
    lower = prompt.lower()
    assert (
        "as-is" in lower or "as is" in lower or "still current" in lower
    ), "no-staleness branch ('forward as-is') missing from prompt"
    # Events fixture: NO superseding event for TB-998 lands after the
    # ideation_state_updated ts — verifies the no-staleness fixture is
    # set up correctly.
    evts = events.tail(cfg.events_file, 50)
    # Find the ideation_state_updated index, then scan after it for
    # any superseding event referencing TB-998. There must be none.
    isu_idx = -1
    for i, e in enumerate(evts):
        if e.get("type") == "ideation_state_updated":
            isu_idx = i
    assert isu_idx >= 0
    superseding_for_998 = [
        e for e in evts[isu_idx + 1:]
        if e.get("type") in {
            "task_complete", "task_deleted",
            "task_updated", "verification_failed",
        } and e.get("task") == "TB-998"
    ]
    assert superseding_for_998 == [], (
        "no-staleness fixture is broken: a superseding event for "
        "TB-998 was found after ideation_state_updated; the test "
        "should pin the case where NONE exists"
    )


# ---------------------------------------------------------------------------
# TB-156: per-call-site effort knob for the status-report routine.
#
# Status-report is a pure summarization job (read events tail, render
# markdown, post to Mattermost). It doesn't need the multi-step reasoning
# budget that `xhigh` is sized for. The new env knob
# `AP2_STATUS_REPORT_EFFORT` lets operators tune this site separately
# from the rest of the agent fleet; the per-site default of `medium`
# kicks in when neither it nor the global `AP2_AGENT_EFFORT` is set.
#
# Both cron and chat triggers route through the same code path so the
# tests exercise both — same effort, regardless of who pulled the trigger.


class _OptionsCapturingSDK:
    """SDK stub that captures the kwargs handed to `ClaudeAgentOptions`
    so the TB-156 effort tests can pin `extra_args["effort"]`. Mirrors
    `_NoopSDK` above but exposes the captured kwargs to the test for
    post-call assertions on `extra_args`."""

    def __init__(self) -> None:
        self.options_kw: dict | None = None
        self.called = False
        outer = self

        class _OptionsBound:
            def __init__(self, **kw):
                outer.options_kw = kw

        # Bind a per-instance options class so each SDK stub keeps its
        # own captured kwargs (the daemon reads `sdk.ClaudeAgentOptions`
        # off the instance, not the class).
        self.ClaudeAgentOptions = _OptionsBound  # noqa: N803

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


def _run_status_report_capturing_effort(
    tmp_path: Path,
    monkeypatch,
    *,
    trigger: str = "cron",
) -> str:
    """Drive `run_status_report` once with the SDK captured and return the
    `extra_args["effort"]` value handed to `ClaudeAgentOptions`. Seeds
    enough event activity that the skip-gate doesn't fire."""
    cfg = _cfg(tmp_path)
    # Seed activity so the run path is exercised (skip-gate doesn't fire).
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete", task="TB-1",
        status="complete", commit="abc1234",
    )
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    sdk = _OptionsCapturingSDK()
    asyncio.run(
        run_status_report(
            cfg, sdk, mcp_server=None,
            trigger=trigger,
            reason=("operator asked" if trigger == "chat" else None),
        )
    )

    assert sdk.called, "skip-gate fired unexpectedly; effort not captured"
    assert sdk.options_kw is not None
    extra = sdk.options_kw.get("extra_args") or {}
    assert "effort" in extra, (
        f"extra_args missing 'effort' key: {sdk.options_kw!r}"
    )
    return extra["effort"]


def test_status_report_default_effort_is_medium_when_no_env_set(
    tmp_path, monkeypatch,
):
    """TB-156: with neither `AP2_STATUS_REPORT_EFFORT` nor `AP2_AGENT_EFFORT`
    set, the per-site default kicks in and the SDK options carry
    `extra_args["effort"] == "medium"` — NOT `xhigh` (the pre-TB-156
    global default that this knob displaces for status-report
    specifically). Pure summarization doesn't need the bigger reasoning
    budget."""
    monkeypatch.delenv("AP2_STATUS_REPORT_EFFORT", raising=False)
    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)

    effort = _run_status_report_capturing_effort(tmp_path, monkeypatch)
    assert effort == "medium"


def test_status_report_per_site_env_takes_precedence(tmp_path, monkeypatch):
    """TB-156: `AP2_STATUS_REPORT_EFFORT` overrides the global
    `AP2_AGENT_EFFORT`. With per-site=`high` and global=`xhigh`, the SDK
    options carry `high` — operators can dial status-report separately
    from the rest of the agent fleet."""
    monkeypatch.setenv("AP2_STATUS_REPORT_EFFORT", "high")
    monkeypatch.setenv("AP2_AGENT_EFFORT", "xhigh")

    effort = _run_status_report_capturing_effort(tmp_path, monkeypatch)
    assert effort == "high"


def test_status_report_falls_through_to_global_when_per_site_unset(
    tmp_path, monkeypatch,
):
    """TB-156: precedence chain — when `AP2_STATUS_REPORT_EFFORT` is unset
    but `AP2_AGENT_EFFORT` is set, the global wins (and so status-report
    inherits whatever global override the operator pinned). Only when
    BOTH are unset does the per-site default of `medium` kick in."""
    monkeypatch.delenv("AP2_STATUS_REPORT_EFFORT", raising=False)
    monkeypatch.setenv("AP2_AGENT_EFFORT", "xhigh")

    effort = _run_status_report_capturing_effort(tmp_path, monkeypatch)
    assert effort == "xhigh"


def test_status_report_chat_trigger_uses_same_effort_as_cron(
    tmp_path, monkeypatch,
):
    """TB-156: cron and chat triggers share the same code path for the
    SDK call, so the effort knob applies to both equally. Pin chat-trigger
    behavior independently so a future divergence (e.g. someone wires
    chat-trigger through a different SDK call site) can't quietly skip
    the per-site lowering."""
    monkeypatch.delenv("AP2_STATUS_REPORT_EFFORT", raising=False)
    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)

    effort = _run_status_report_capturing_effort(
        tmp_path, monkeypatch, trigger="chat",
    )
    assert effort == "medium"


def test_status_report_effort_env_knob_present_in_source():
    """Source-level pin so a maintainer can't silently drop the
    `AP2_STATUS_REPORT_EFFORT` env knob and revert status-report to the
    global `AP2_AGENT_EFFORT`. The verification grep
    (`AP2_STATUS_REPORT_EFFORT in ap2/`) backs this up at the daemon
    level; this test catches the same regression in CI."""
    import inspect

    from ap2 import status_report

    src = inspect.getsource(status_report.run_status_report)
    assert "AP2_STATUS_REPORT_EFFORT" in src, (
        "regression: TB-156's per-site effort knob is missing from "
        "run_status_report"
    )


def test_run_control_agent_default_effort_unchanged_for_other_callers(
    tmp_path, monkeypatch,
):
    """Anti-regression: `_run_control_agent` callers that DON'T pass an
    explicit `effort=` (cron jobs other than status-report, ideation, the
    MM handler — though MM handler uses its own SDK call site) keep
    reading from `AP2_AGENT_EFFORT` and default to `xhigh`. TB-156
    introduced the optional override; the existing call sites stay on
    the pre-TB-156 default unless they opt in."""
    import asyncio as _asyncio

    from ap2 import daemon

    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)

    cfg = _cfg(tmp_path)
    sdk = _OptionsCapturingSDK()

    _asyncio.run(daemon._run_control_agent(
        cfg, sdk, mcp_server=None,
        label="unit-test",
        prompt="hi",
        allowed_tools=[],
        max_turns=1,
    ))

    assert sdk.called, "default-path SDK call did not fire"
    extra = (sdk.options_kw or {}).get("extra_args") or {}
    assert extra.get("effort") == "xhigh", (
        f"default effort drifted from xhigh; got {extra!r}"
    )


def test_run_control_agent_explicit_effort_overrides_env(
    tmp_path, monkeypatch,
):
    """When a caller passes `effort="medium"` explicitly,
    `_run_control_agent` honors it regardless of `AP2_AGENT_EFFORT`. This
    is the contract `run_status_report` relies on — the per-site
    computation lives in the caller; `_run_control_agent` is just the
    plumbing."""
    import asyncio as _asyncio

    from ap2 import daemon

    monkeypatch.setenv("AP2_AGENT_EFFORT", "xhigh")

    cfg = _cfg(tmp_path)
    sdk = _OptionsCapturingSDK()

    _asyncio.run(daemon._run_control_agent(
        cfg, sdk, mcp_server=None,
        label="unit-test",
        prompt="hi",
        allowed_tools=[],
        max_turns=1,
        effort="medium",
    ))

    extra = (sdk.options_kw or {}).get("extra_args") or {}
    assert extra.get("effort") == "medium", (
        f"explicit effort=medium not honored over env xhigh; got {extra!r}"
    )


# ---------------------------------------------------------------------------
# TB-190: server-side resolution of the status-report target channel.
#
# Pre-fix the prompt told the agent to "post...to the channel identified
# by AP2_MM_REPORT_CHANNEL (or #autopilot if unset)" — but control agents
# have no env-var access, the literal string was opaque, and the
# `#autopilot` fallback didn't exist on the server. The agent ended up
# posting to whatever channel the server defaulted to (town-square),
# NOT the operator's configured channel. The fix: the daemon resolves
# the env vars and injects a `- post target channel: <id>` line into
# the snapshot block via `state_extras`; the prompt body now points at
# that line. Tests below pin (a) explicit env wins, (b) fallback to
# `AP2_MM_CHANNELS[0]`, (c) skip-on-unset, (d) prompt body grep
# regressions for the dead-letter `#autopilot` literal and the new
# instruction's load-bearing markers.


def _capture_extras_factory():
    """Return (capture_dict, capture_fn) so the daemon-side state_extras
    threading can be inspected without rendering the full prompt."""
    captured: dict[str, list[str]] = {"extras": []}

    def _capture(cfg, name, body, *, state_extras=None):
        captured["extras"] = list(state_extras or [])
        return "stub"

    return captured, _capture


def test_run_status_report_resolves_explicit_report_channel(
    tmp_path, monkeypatch,
):
    """TB-190: explicit `AP2_MM_REPORT_CHANNEL=<id>` wins over the
    `AP2_MM_CHANNELS` fallback. The resolved ID lands in the
    `state_extras` list as `- post target channel: <id>` — the agent
    reads it from the rendered `## Current state` snapshot block."""
    monkeypatch.setenv("AP2_MM_REPORT_CHANNEL", "channel-foo")
    monkeypatch.setenv("AP2_MM_CHANNELS", "channel-bar,channel-baz")

    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)

    captured, capture_fn = _capture_extras_factory()
    monkeypatch.setattr("ap2.prompts.build_control_prompt", capture_fn)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is True
    assert "- post target channel: channel-foo" in captured["extras"]
    # The fallback channels are NOT used when the explicit override is set.
    assert not any("channel-bar" in x for x in captured["extras"])
    assert not any("channel-baz" in x for x in captured["extras"])


def test_run_status_report_falls_back_to_first_mm_channel(
    tmp_path, monkeypatch,
):
    """TB-190: when `AP2_MM_REPORT_CHANNEL` is unset, the daemon falls
    back to the first entry of `AP2_MM_CHANNELS` — the natural default
    for single-channel projects (the inbound-watch channel is where
    outbound status posts belong)."""
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.setenv("AP2_MM_CHANNELS", "channel-bar,channel-baz")

    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)

    captured, capture_fn = _capture_extras_factory()
    monkeypatch.setattr("ap2.prompts.build_control_prompt", capture_fn)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert "- post target channel: channel-bar" in captured["extras"]
    # The second entry is NOT used — only the first.
    assert not any("channel-baz" in x for x in captured["extras"])


def test_run_status_report_omits_target_channel_when_unset(
    tmp_path, monkeypatch,
):
    """TB-190: when neither env var is set, NO `post target channel:`
    line is appended. The agent then takes the prompt's explicit-skip
    branch instead of guessing a channel ID from server defaults."""
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)

    captured, capture_fn = _capture_extras_factory()
    monkeypatch.setattr("ap2.prompts.build_control_prompt", capture_fn)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    joined = "\n".join(captured["extras"])
    assert "post target channel:" not in joined


def test_run_status_report_treats_blank_env_as_unset(tmp_path, monkeypatch):
    """TB-190: an empty / whitespace-only `AP2_MM_REPORT_CHANNEL` (e.g.
    `AP2_MM_REPORT_CHANNEL=` left in an env file) is treated as unset
    so the fallback to `AP2_MM_CHANNELS[0]` still kicks in. Mirrors
    `mattermost._channels_to_watch` parsing — an empty value is not a
    valid channel ID."""
    monkeypatch.setenv("AP2_MM_REPORT_CHANNEL", "   ")
    monkeypatch.setenv("AP2_MM_CHANNELS", "channel-bar")

    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)

    captured, capture_fn = _capture_extras_factory()
    monkeypatch.setattr("ap2.prompts.build_control_prompt", capture_fn)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert "- post target channel: channel-bar" in captured["extras"]


def test_status_report_prompt_drops_dead_letter_autopilot_fallback():
    """TB-190 regression: the prompt body must NOT carry the literal
    string `#autopilot` (the pre-fix fallback that nobody could reach
    — there's no `#autopilot` channel on the server). The fix removed
    it entirely; the prompt now routes the agent through the explicit-
    skip branch when no channel is configured."""
    from ap2.status_report import STATUS_REPORT_PROMPT

    assert "#autopilot" not in STATUS_REPORT_PROMPT, (
        "TB-190 regression: dead-letter `#autopilot` fallback string "
        "is back in STATUS_REPORT_PROMPT"
    )


def test_status_report_prompt_instructs_reading_post_target_channel():
    """TB-190: the prompt body must point the agent at the
    `- post target channel:` snapshot line and tell it to skip with a
    `log_event` audit when the line is absent — pinned by phrasal
    markers so a paraphrase that drops the contract trips this test."""
    from ap2.status_report import STATUS_REPORT_PROMPT

    body = STATUS_REPORT_PROMPT
    # The forwarder names the snapshot line so the agent knows where to
    # look for the resolved ID.
    assert "post target channel:" in body
    # The skip branch is explicit — the agent log_events with the load-
    # bearing reason string when the line is absent.
    assert "no AP2_MM_REPORT_CHANNEL or AP2_MM_CHANNELS configured" in body
    # The env-var name is referenced in the prompt-body context (the
    # skip-reason string for grep regressions).
    assert "AP2_MM_REPORT_CHANNEL" in body
    # Anti-regression: the agent is explicitly told NOT to fall back to
    # server defaults / inbound-mention channels.
    lower = body.lower()
    assert "do not guess" in lower or "do not guess a channel" in lower


def test_run_status_report_target_channel_threads_into_full_prompt(
    tmp_path, monkeypatch,
):
    """TB-190 agent-input integrity: when a channel is configured, the
    rendered prompt's `## Current state` snapshot block actually
    carries the `- post target channel: <id>` line in a position the
    agent can read it — same pattern the TB-151 snapshot pin uses.

    Synthesizes a fixture environment, runs the status-report routine
    against the REAL `build_control_prompt` (not a stub), and asserts
    the captured prompt has the snapshot line wired through end-to-end."""
    monkeypatch.setenv("AP2_MM_REPORT_CHANNEL", "channel-foo")
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)

    cfg = _cfg(tmp_path)
    _seed_active_for_run(cfg)

    # Wrap the real builder so we can inspect the rendered prompt
    # without short-circuiting it. Same shape as the TB-182 smoke test.
    from ap2 import prompts as _prompts_mod

    real_build = _prompts_mod.build_control_prompt
    captured: dict[str, str] = {}

    def _wrapped(cfg, name, body, **kw):
        out = real_build(cfg, name, body, **kw)
        captured["prompt"] = out
        return out

    monkeypatch.setattr("ap2.prompts.build_control_prompt", _wrapped)

    sdk = _NoopSDK()
    asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron")
    )

    assert sdk.called is True
    prompt = captured["prompt"]
    # Snapshot block carries the resolved channel ID.
    assert "- post target channel: channel-foo" in prompt
    # The line lands inside the `## Current state` block (above the
    # `## Control job` framing) so the agent reads it as part of the
    # snapshot, not as part of the job body.
    cs_idx = prompt.find("## Current state")
    cj_idx = prompt.find("## Control job")
    target_idx = prompt.find("- post target channel: channel-foo")
    assert cs_idx >= 0 and cj_idx > cs_idx and cs_idx < target_idx < cj_idx, (
        "post target channel line is not inside the `## Current state` "
        "block — agent will not pick it up as snapshot context"
    )
