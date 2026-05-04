"""Smoke tests for ap2.prompts: the load-bearing parts of the task prompt
must not silently drift. Each phrase pinned here corresponds to a daemon-side
invariant — change them only when the corresponding daemon code also changes.
"""
from __future__ import annotations

from pathlib import Path

from ap2.board import Task
from ap2.config import Config
from ap2.prompts import (
    build_control_prompt,
    build_mattermost_prompt,
    build_task_prompt,
)


def _cfg(tmp_path: Path) -> Config:
    (tmp_path / "TASKS.md").write_text("# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n## Complete\n\n## Frozen\n")
    return Config.load(tmp_path)


def test_prompt_pins_commit_subject_convention(tmp_path):
    """`<TASK_ID>:` prefix is what the daemon's commit-fallback (TB-65) parses."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "STARTS WITH the task ID" in p
    assert "load-bearing" in p


def test_prompt_pins_pre_run_history_check(tmp_path):
    """Retry agents must check `git log --grep` before redoing prior work."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "Before you start: check for prior work" in p
    assert 'git log --grep="<TASK_ID>"' in p


def test_prompt_warns_against_naive_complete_from_commit_existence(tmp_path):
    """Agent must verify completeness, not just trust subject — the daemon's
    fallback trusts naively, the agent should not."""
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "DO NOT declare status=complete based on commit existence alone" in p


def test_prompt_pins_pipeline_task_start_guidance(tmp_path):
    """TB-114: task agent header must steer agents to `pipeline_task_start`
    for any work expected to take more than ~5 min wall-clock —
    independent of whether the briefing has a `## Pipeline launch` section
    (that two-shape pattern was retired). The agent self-classifies; on
    pipeline pivot the daemon parks the task in `Pipeline Pending` and
    re-runs verification once subprocesses die.
    """
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    # Tool named explicitly — agents see the symbol they need to call.
    assert "pipeline_task_start" in p
    # Strong MUST phrasing — weaker words let agents rationalize past it.
    assert "MUST" in p
    # Self-classification trigger (cost-aware, not briefing-shape-aware).
    assert "5 minutes" in p or "~5 minutes" in p
    # The Pipeline Pending parking is the load-bearing post-dispatch fact.
    assert "Pipeline Pending" in p
    # Make sure the agent doesn't double-do the work inline + via pipeline.
    assert "Do NOT ALSO" in p or "Do NOT also" in p


def test_prompt_pins_state_file_fence(tmp_path):
    """Daemon (not agent) owns TASKS.md / progress.md / events.jsonl /
    CLAUDE.md / ideation_state.md / cron.yaml; operator owns goal.md.

    Every fenced path must appear in the prompt header. Permission-level
    enforcement (run_task's disallowed_tools) is the second line of defense;
    the prompt fence is the first, and a missing entry would silently let
    the agent edit a fenced file that the SDK guard *does* still block.
    """
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "do NOT touch" in p
    for f in TASK_AGENT_FENCED_PATHS:
        assert f in p, f"fenced path {f!r} missing from task prompt header"


def test_task_disallowed_tools_covers_every_fenced_path():
    """Every TASK_AGENT_FENCED_PATHS entry must produce both an `Edit(<path>)`
    and a `Write(<path>)` block in the disallowed_tools list — that's the
    SDK-level enforcement layer behind the prompt fence."""
    from ap2.daemon import _task_disallowed_tools
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    blocks = _task_disallowed_tools()
    # Always-on Bash blocks survive
    assert "Bash(git push*)" in blocks
    assert "Bash(rm -rf *)" in blocks
    # Every fenced path appears as both Edit and Write
    for path in TASK_AGENT_FENCED_PATHS:
        assert f"Edit({path})" in blocks, f"Edit({path}) missing from disallowed_tools"
        assert f"Write({path})" in blocks, f"Write({path}) missing from disallowed_tools"


def test_task_fenced_paths_includes_goal_md():
    """goal.md is operator-curated; if a task can rewrite it, ideation
    rewrites its own constraints (the TB-144 feedback-loop case in stoch)."""
    from ap2.tools import TASK_AGENT_FENCED_PATHS

    assert "goal.md" in TASK_AGENT_FENCED_PATHS


def test_task_prompt_fenced_reminder_mentions_operator_queue_jsonl(tmp_path):
    """TB-143: the rendered task-agent prompt's fenced-files reminder must
    mention `operator_queue.jsonl` so the agent sees a literal "don't
    touch" cue alongside the other fenced paths. The previous TB-141
    prose said the file was intentionally NOT fenced — that note has
    been replaced now that the path is back in the defense list (the
    snapshot-check exemption lives in rollback.py instead).
    """
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    assert "operator_queue.jsonl" in p
    # Anti-regression on the obsolete TB-141 prose.
    assert "intentionally NOT fenced" not in p


def test_prompt_advertises_cron_propose_for_recurring_proposals(tmp_path):
    """TB-123: the `cron=` arg was lifted off `report_result` and into a
    dedicated `cron_propose` MCP tool. The prompt footer must surface the
    new tool by name (so the agent can discover it) and must NOT
    instruct the agent to pass cron via `report_result`'s args (which
    would silently fail since the field is gone from the schema).
    """
    cfg = _cfg(tmp_path)
    t = Task(id="TB-99", title="x", section="Active")
    p = build_task_prompt(cfg, t)
    # New tool surfaced by name with all four arg fields.
    assert "cron_propose" in p
    assert "rationale" in p
    assert "schedule" in p
    # Drop-pin: the obsolete `cron=` arg phrasing in `report_result` is
    # gone — the JSON-list-in-string contract no longer exists.
    assert "cron='[" not in p
    assert '"action": "add"' not in p


def test_mattermost_prompt_pins_explicit_thread_id(tmp_path):
    """The handler agent must reply in the user's thread, not in some thread_id
    it picks up from the recent-events block (which often contains an unrelated
    cron status-report thread). The fix wires the literal thread_id into the
    prompt as the value to pass to `mattermost_reply`.
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "stoch",
        "user": "li.zhang",
        "text": "@claude-bot status?",
        "thread_id": "",  # top-level message
    }
    p = build_mattermost_prompt(cfg, msg)
    assert 'channel: "ch-abc"' in p
    assert 'thread_id: ""' in p
    assert "do NOT pull" in p

    # Threaded reply: the handler should use the thread root.
    msg_threaded = dict(msg, thread_id="root-xyz")
    p2 = build_mattermost_prompt(cfg, msg_threaded)
    assert 'thread_id: "root-xyz"' in p2


def test_mattermost_prompt_restriction_note_is_unconditional(tmp_path):
    """TB-122 + TB-142 + TB-145: the prompt always carries the toolset
    restriction explanation — `cron_edit`, `ideation_state_write`, and
    `board_edit` are off-limits, board mutations route through
    `operator_queue_append`. TB-145 dropped the FULL/RESTRICTED gate
    (the underlying toolset is now unconditional too), so the same
    note appears regardless of board state. The same prompt must spell
    out the operator-still-available actions (queue add/approve/delete/
    backlog/unfreeze, daemon_control, operator_log_append) so the
    handler doesn't refuse work it CAN do."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot pause",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    # Pinned: agent knows the disabled tools by name.
    assert "cron_edit" in p
    assert "ideation_state_write" in p
    # Pinned (TB-142): board_edit is named as off-limits, and the
    # queue-routing equivalent is named so the handler can still mutate.
    assert "board_edit" in p
    assert "operator_queue_append" in p
    # Pinned: agent knows pause takes effect on the next tick.
    assert "next" in p.lower() and "tick" in p.lower()
    # Pinned: TB-121 cross-ref — `approve` must remain discoverable
    # (it's now a queue op, not a board_edit action).
    assert "approve" in p
    # Pinned: operator_log_append remains available so "ack:" still works.
    assert "operator_log_append" in p


def test_tb154_mattermost_prompt_carries_canonical_briefing_section_list(tmp_path):
    """TB-154: the MM handler authors briefing payloads when the
    operator types `@claude-bot add ...`. The prompt must spell out
    the canonical `##`-section names so the agent's first attempt
    passes the queue-append-time validator. Closes the TB-153 failure
    mode where the handler used `## Acceptance` instead of
    `## Verification` and the per-task verifier silently skipped.

    Pinned phrasing — every canonical section name appears verbatim,
    so future prompt edits can't drop one without this test failing.
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot add a task to do X",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    # Every canonical section name must appear in the prompt so the
    # handler doesn't author `## Acceptance` / `## Approach` / etc.
    for section in ("## Goal", "## Scope", "## Design",
                    "## Verification", "## Out of scope"):
        assert section in p, f"prompt missing canonical section {section!r}"
    # The cross-ref so a future reader can trace the rule's origin.
    assert "TB-154" in p


def test_mattermost_prompt_routes_board_ops_through_queue(tmp_path):
    """TB-142 + TB-145 (load-bearing): the "Your job" rubric must direct
    the agent at `operator_queue_append` for board mutations and
    explicitly steer it AWAY from `board_edit` (which is filtered out
    of `MM_HANDLER_TOOLS`). Pin both the routing instruction and the
    rationale (drain happens between tick stages, so any running task's
    snapshot window never sees the mutation).
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot approve TB-9",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    # The "Your job" rubric routes board mutations through the queue.
    assert "operator_queue_append" in p
    # Explicit "NOT board_edit" guidance (so the agent doesn't fall
    # back to `board_edit` if it remembers seeing it elsewhere).
    assert "NOT `board_edit`" in p or "not `board_edit`" in p.lower()
    # The TB-142 rationale ties the routing to the in-flight snapshot
    # window — agents who understand WHY are less likely to drift.
    assert "snapshot" in p.lower() or "TB-110" in p


def test_mattermost_prompt_does_not_mention_conditional_toolset_switching(tmp_path):
    """TB-145 invariant: the MM handler prompt MUST NOT mention
    "when a task is active" / "when the board is idle" / "your toolset
    varies" / similar conditional language. The handler always runs
    with the same fixed `MM_HANDLER_TOOLS` set, and the prompt should
    reflect that. A regression here means either the prompt is back to
    branching on `task_in_flight` or the prose still describes the
    retired FULL/RESTRICTED toggle."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot status",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    lower = p.lower()
    forbidden = [
        "when a task is active",
        "when the board is idle",
        "task currently in flight",
        "task agent is currently running",
        "your toolset varies",
        "depending on board state",
        "depends on board state",
        "they'll be available again once the daemon is idle",
    ]
    for phrase in forbidden:
        assert phrase.lower() not in lower, (
            f"prompt mentions conditional toolset switching: {phrase!r}"
        )


# ---------------------------------------------------------------------------
# TB-128: control prompts must inject a fresh "right now" snapshot, and the
# status-report job must get an explicit timestamp / freshness contract.

def test_control_prompt_injects_current_state_block(tmp_path):
    """Every control-agent prompt (cron + ideation) must carry a
    `## Current state` block with a freshly computed UTC `now:` timestamp,
    board counts, and recent commits. This is the deterministic anchor the
    status-report cron uses for its headline (TB-128) — no more re-rendering
    text from a prior context.
    """
    cfg = _cfg(tmp_path)
    p = build_control_prompt(cfg, "status-report", "post a status report")
    assert "## Current state" in p
    assert "rendered just before this prompt was sent" in p
    # Headline timestamp is a real ISO-Z string formatted right now.
    import re

    assert re.search(
        r"now: 20\d\d-[01]\d-[0-3]\dT[0-2]\d:[0-5]\d:[0-5]\dZ", p
    ), "expected a current ISO-Z `now:` timestamp in the snapshot block"
    # Board counts use the same A/R/B/P/C/F shape as `ap2 status`.
    assert "(Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)" in p


def test_control_prompt_status_report_pins_freshness_contract(tmp_path):
    """For the `status-report` job specifically, the prompt must spell out
    the load-bearing rules so the agent can't drift back to copying a stale
    timestamp from prior turns: (1) headline timestamp = the snapshot's
    `now:` value verbatim, (2) re-read events.jsonl + TASKS.md fresh,
    (3) skip the post if nothing has changed since the last status_report.
    """
    cfg = _cfg(tmp_path)
    p = build_control_prompt(cfg, "status-report", "post a status report")
    # (1) Use the snapshot timestamp verbatim — no copying from elsewhere.
    assert "Status-report contract" in p
    assert "literal `now:` value" in p
    assert "Do NOT reuse a timestamp from" in p
    # (2) Fresh reads of the canonical state files.
    assert ".cc-autopilot/events.jsonl" in p
    assert "TASKS.md" in p
    # (3) Skip-when-idle directive (defense in depth — daemon also gates).
    assert "Skip the Mattermost post entirely" in p
    assert "no activity since" in p


def test_control_prompt_non_status_jobs_skip_status_report_contract(tmp_path):
    """The status-report contract is keyed on job name. Other control jobs
    (e.g. ideation) must not get the status-report-specific addendum
    appended to their prompt — it's noise for them and would confuse
    ideation's own freshness model.
    """
    cfg = _cfg(tmp_path)
    p_status = build_control_prompt(cfg, "status-report", "x")
    p_other = build_control_prompt(cfg, "ideation", "x")
    assert "Status-report contract" in p_status
    assert "Status-report contract" not in p_other
    # The shared `## Current state` block IS in both — it's harmless context.
    assert "## Current state" in p_other


# ---------------------------------------------------------------------------
# TB-144: MM handler prompt routes status queries through `status_report_run`
# instead of composing freeform replies. Pin the recognition phrasing + the
# tool name so a refactor can't silently drop the routing.

def test_mattermost_prompt_routes_status_queries_through_status_report_run(tmp_path):
    """TB-144 + TB-145: when the operator asks for a status report
    (recognize: "status", "what's going on", etc.), the MM handler
    prompt must instruct the agent to call `status_report_run` rather
    than compose its own reply. Otherwise chat-triggered reports drift
    from the cron format AND the audit trail loses the
    `cron_start`/`cron_complete` pair (post-mortems can't tell on-demand
    from scheduled).

    Pinned phrases — the recognition pattern (so the agent matches the
    operator's wording), the tool name (so the agent calls the right
    surface), and the don't-call-twice steer (the routine has its own
    skip-gate; spamming it doesn't get a fresher report).

    TB-145: there's only one prompt shape now, so we exercise it once.
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "@claude-bot status?",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    assert "status_report_run" in p
    # Recognition pattern is explicit so the agent doesn't have to guess.
    assert '"status"' in p
    assert "what's going on" in p
    # Don't-call-twice steer.
    assert "more than once per turn" in p or "Don't call it more than once" in p


def test_mattermost_prompt_status_routing_steers_away_from_freeform_reply(tmp_path):
    """The routing instruction must explicitly say "instead of composing
    your own reply" so the agent doesn't BOTH call the tool AND fabricate
    a status reply in the same turn (the routine's report goes through
    its own mattermost_reply call inside the sub-agent — a parallel reply
    from the handler would produce two posts)."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "@claude-bot status",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    assert "instead of composing your own reply" in p


def test_mattermost_prompt_pins_reject_tb_n_recognition(tmp_path):
    """TB-152: the MM handler agent must recognize "reject TB-N" as an
    operator command for ideation proposals, route it through
    `operator_queue_append` with `op="reject"`, and know that the chat
    surface accepts a `reason: ...` arg. Pin the cross-reference + the
    routing contract so a future prompt rewrite can't silently drop the
    surface (which would force operators back to the CLI for a chat-
    natural action). Also pin that `reject` is described as ideation-
    proposals-only with `delete` as the fallback for everything else."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "@claude-bot reject TB-9 reason: duplicates TB-3",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    # Cross-ref + routing instruction.
    assert "TB-152" in p
    assert "reject TB-N" in p
    assert "operator_queue_append" in p
    # Op name surfaced (matches the same form as the approve test).
    assert '"reject"' in p or "op=\"reject\"" in p
    # The verb's ideation-only scope is documented so the handler doesn't
    # apply it to typos / superseded tasks (where `delete` is correct).
    p_lower = p.lower()
    assert "ideation proposal" in p_lower
    # operator_log.md is named so the handler knows where the reason
    # lands (and why capturing one matters).
    assert "operator_log.md" in p


def test_mattermost_prompt_pins_approve_tb_n_recognition(tmp_path):
    """TB-121: the MM handler agent must recognize "approve TB-N" as an
    operator command and route it through `operator_queue_append` with
    `op="approve"`. Pin the cross-reference, the recognized phrasing,
    and the queue-routing instruction so a future prompt rewrite can't
    silently drop the surface (which would force operators back to the
    CLI for a chat-natural action)."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "@claude-bot approve TB-9",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    # Cross-ref and the queue routing.
    assert "TB-121" in p
    assert "approve" in p
    assert "operator_queue_append" in p
    # The op name + the codespan it strips are both named so the agent
    # has the full mental model.
    assert '"approve"' in p or "op=\"approve\"" in p
    assert "@blocked:review" in p


# ---------------------------------------------------------------------------
# TB-149: thread-reply context. When the incoming MM message has a
# non-empty thread_id, the prompt instructs the handler to call
# `mattermost_thread_read` first; for top-level mentions the instruction
# is absent (the message is self-contained).


def test_mattermost_prompt_threaded_reply_instructs_thread_read(tmp_path):
    """A thread-reply message (non-empty thread_id) must surface a
    `mattermost_thread_read` instruction in the prompt so the handler
    fetches prior context before acting. The thread_id must appear
    inside the suggested call so the agent doesn't have to re-derive it
    from the events block (which often contains unrelated cron threads).
    """
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-2",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "yes",
        "thread_id": "root-xyz",
    }
    p = build_mattermost_prompt(cfg, msg)
    # Tool name surfaced — agent sees the symbol it needs to call.
    assert "mattermost_thread_read" in p
    # The actual thread_id is embedded in the suggested call so the
    # agent doesn't have to derive it from elsewhere in the prompt.
    assert 'thread_id="root-xyz"' in p
    # The "why" — pin the rationale so a future prompt rewrite that
    # silently drops the thread-context guidance trips this test.
    assert "thread reply" in p.lower()


def test_mattermost_prompt_top_level_message_skips_thread_read(tmp_path):
    """A top-level mention (empty thread_id) must NOT include the
    `mattermost_thread_read` instruction — the message is self-contained
    and the tool would error on an empty thread_id anyway. Pin the
    absence of the instruction sentence + the section header so a
    future prompt rewrite that always emits the section (regardless of
    thread_id) trips this test.

    Note: the tool name `mattermost_thread_read` itself may legitimately
    appear elsewhere in the prompt (e.g. a future toolset reminder) —
    the load-bearing check is that the *thread-context instruction
    section* is omitted for top-level messages, so we anchor on the
    section header phrasing rather than the tool name."""
    cfg = _cfg(tmp_path)
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "ap2",
        "user": "li.zhang",
        "text": "@claude-bot status?",
        "thread_id": "",
    }
    p = build_mattermost_prompt(cfg, msg)
    # Section header from the threaded branch must be absent.
    assert "thread context" not in p.lower()
    # The threaded-branch instruction sentence is also absent.
    assert "this message is a thread reply" not in p.lower()


# ---------------------------------------------------------------------------
# TB-163: pattern-level operator-veto signal at proposal-authoring time.
# The ideation prompt's `## Current state` header must surface the most
# recent `rejected ideation proposal` lines from operator_log.md as a
# "Recent operator rejections" block, so the ideator can spot patterns
# (recurring framing the operator keeps vetoing) without having to
# manually walk operator_log.md line-by-line.


def _seed_operator_log(cfg: Config, lines: list[str]) -> None:
    """Write a fixture operator_log.md at `cfg.project_root /
    .cc-autopilot/operator_log.md` with the given body lines (one per
    list item, no trailing newline). The file's header matches what
    `tools.py::do_operator_log_append` writes on first append — keeping
    the fixture realistic so future readers can't accidentally couple
    to a stripped-down shape."""
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Operator log\n\n"
        "_Operator decisions and action acknowledgements. Append-only.\n"
        "Ideation reads this in Step 0; logged items are authoritative —\n"
        "ideation won't re-propose decisions logged here._\n\n"
    )
    log_path.write_text(header + "\n".join(lines) + "\n")


def test_build_control_prompt_renders_rejection_block_when_present(tmp_path):
    """3 rejection lines + unrelated audit lines → block heading appears,
    all 3 TB-Ns appear in newest-last (chronological) order."""
    cfg = _cfg(tmp_path)
    _seed_operator_log(
        cfg,
        [
            # Unrelated audit lines from `_append_operator_audit_line` —
            # the reader must skip these.
            "- 2026-05-01T08:00:00Z — applied operator-queued add_backlog → TB-100",
            "- 2026-05-02T09:00:00Z — applied operator-queued approve → TB-101",
            # Rejection lines (oldest first in the file — newest last is
            # the rendered convention).
            "- 2026-05-03T10:00:00Z — rejected ideation proposal → TB-200 (oldest reject): scope drift",
            "- 2026-05-03T11:00:00Z — applied operator-queued reject → TB-200",
            "- 2026-05-04T12:00:00Z — rejected ideation proposal → TB-201 (middle reject): no signal",
            "- 2026-05-04T13:00:00Z — rejected ideation proposal → TB-202 (newest reject): superseded",
        ],
    )
    p = build_control_prompt(cfg, "ideation", "ideate")

    assert "## Recent operator rejections" in p
    # All 3 TB-Ns appear, in chronological order (oldest first, newest last).
    idx_200 = p.find("TB-200")
    idx_201 = p.find("TB-201")
    idx_202 = p.find("TB-202")
    assert idx_200 != -1 and idx_201 != -1 and idx_202 != -1
    assert idx_200 < idx_201 < idx_202, (
        "rejection block must be chronological (newest last) to match the "
        "events-block convention"
    )
    # The redundant `applied operator-queued reject → TB-200` audit line
    # is not what we want to render — only the richer
    # `rejected ideation proposal` lines reach the block. (Negative-pin:
    # if we ever start matching the audit verb too, this test fails.)
    block_start = p.find("## Recent operator rejections")
    block = p[block_start: block_start + 2000]
    assert "applied operator-queued" not in block


def test_build_control_prompt_skips_rejection_block_when_empty(tmp_path):
    """No operator_log.md / no matching lines → no heading rendered.
    Important: we don't want an empty `## Recent operator rejections (last 0)`
    heading polluting fresh-project prompts."""
    cfg = _cfg(tmp_path)
    # No operator_log.md at all — first branch.
    p_no_file = build_control_prompt(cfg, "ideation", "ideate")
    assert "Recent operator rejections" not in p_no_file

    # File exists but has no rejection lines — second branch.
    _seed_operator_log(
        cfg,
        [
            "- 2026-05-01T08:00:00Z — applied operator-queued add_backlog → TB-100",
            "- 2026-05-02T09:00:00Z [TB-101] — decided to keep TB-x as reference",
        ],
    )
    p_no_match = build_control_prompt(cfg, "ideation", "ideate")
    assert "Recent operator rejections" not in p_no_match


def test_build_control_prompt_truncates_rejection_block_to_default_limit(tmp_path):
    """7 rejection lines on disk → only the 5 most recent appear in the
    rendered block. Pins both the cap (5) and that "most recent" means
    last-in-the-file (since lines are appended chronologically by
    `_append_operator_audit_line`)."""
    cfg = _cfg(tmp_path)
    rejection_lines = [
        f"- 2026-05-04T{hour:02d}:00:00Z — rejected ideation proposal → TB-{300 + i} (proposal {i}): reason {i}"
        for i, hour in enumerate(range(7))
    ]
    _seed_operator_log(cfg, rejection_lines)

    p = build_control_prompt(cfg, "ideation", "ideate")
    assert "## Recent operator rejections (last 5)" in p
    # Most recent 5 = TB-302..TB-306; oldest two (TB-300, TB-301) drop.
    assert "TB-300" not in p
    assert "TB-301" not in p
    for tid in ("TB-302", "TB-303", "TB-304", "TB-305", "TB-306"):
        assert tid in p, f"expected most-recent rejection {tid} in prompt"
    # Chronological order preserved within the surviving 5.
    assert (
        p.find("TB-302")
        < p.find("TB-303")
        < p.find("TB-304")
        < p.find("TB-305")
        < p.find("TB-306")
    )


def test_status_report_run_in_mm_handler_toolset():
    """TB-144 + TB-145: the MCP tool must be available to the MM
    handler — operators ask for status whether a task is running or not.
    Adding to `CONTROL_AGENT_TOOLS` (the source for the handler set) is
    the load-bearing change; this test pins the result. TB-145
    collapsed FULL/RESTRICTED into a single `MM_HANDLER_TOOLS`, so
    there's only one set to check.
    """
    from ap2.tools import CONTROL_AGENT_TOOLS, MM_HANDLER_TOOLS

    name = "mcp__autopilot__status_report_run"
    assert name in CONTROL_AGENT_TOOLS
    assert name in MM_HANDLER_TOOLS


# ---------------------------------------------------------------------------
# TB-168: `_current_state_block` accepts `include_board` / `include_commits`
# kwargs so ideation can opt out of two sub-blocks that don't pay rent for
# it specifically (board counts re-derived from TASKS.md; recent commits
# ~60% daemon meta-noise, signal subsumed by progress.md). Defaults stay
# True so the status-report cron path keeps its byte-identical rendering.


def _init_git_with_two_commits(repo: Path) -> None:
    """Helper: initialize a git repo at `repo` with two commits so the
    `_current_state_block` git-log subprocess returns real short-shas.
    Used by the byte-identical and trim-shape tests below."""
    import subprocess as _sp

    _sp.run(["git", "init", "-q"], cwd=repo, check=True)
    _sp.run(
        ["git", "config", "user.email", "tb168@example.com"],
        cwd=repo, check=True,
    )
    _sp.run(["git", "config", "user.name", "tb168"], cwd=repo, check=True)
    _sp.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "first.txt").write_text("first\n")
    _sp.run(["git", "add", "first.txt"], cwd=repo, check=True)
    _sp.run(
        ["git", "commit", "-q", "-m", "first commit"],
        cwd=repo, check=True,
    )
    (repo / "second.txt").write_text("second\n")
    _sp.run(["git", "add", "second.txt"], cwd=repo, check=True)
    _sp.run(
        ["git", "commit", "-q", "-m", "second commit"],
        cwd=repo, check=True,
    )


def test_current_state_block_default_kwargs_render_unchanged_shape(tmp_path):
    """TB-168: with default kwargs (both include_board and include_commits
    True), `_current_state_block` produces the same shape it did pre-TB-168
    — header line, `now:`, `board:`, `recent commits (HEAD~10):`, indented
    commit lines. This pins backwards compatibility for the status-report
    cron path (which calls the function via `build_control_prompt`'s
    default kwargs and embeds the rendered block in the posted report).
    """
    from ap2.prompts import _current_state_block

    # Fixture: ≥1 task in TASKS.md (so board counts are real, not the
    # `(board not found)` fallback) and ≥2 commits (so the commits sub-
    # block has real short-shas to render).
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n- [TB-1] one ready task\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    _init_git_with_two_commits(tmp_path)
    cfg = Config.load(tmp_path)

    result = _current_state_block(cfg)
    lines = result.split("\n")

    # Line 0: snapshot header (load-bearing — `_STATUS_REPORT_CONTRACT`
    # references this block by name).
    assert lines[0] == (
        "## Current state (rendered just before this prompt was sent)"
    )
    # Line 1: `now: <ISO-Z timestamp>` — the status-report cron uses this
    # value verbatim as the headline timestamp.
    import re

    assert re.match(
        r"^- now: 20\d\d-[01]\d-[0-3]\dT[0-2]\d:[0-5]\d:[0-5]\dZ$",
        lines[1],
    ), f"line 1 not a `now:` line: {lines[1]!r}"
    # Line 2: `- board: <counts> (Active/Ready/Backlog/Pipeline-Pending/...)`.
    assert lines[2].startswith("- board: ")
    assert lines[2].endswith(
        " (Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)"
    )
    # Line 3: the `recent commits (HEAD~10):` heading.
    assert lines[3] == "- recent commits (HEAD~10):"
    # Subsequent non-empty lines: commit lines, each indented with two
    # spaces. We have exactly 2 commits, so lines[4] and lines[5] are
    # commits. (lines[6] is the trailing-newline empty string.)
    for idx in (4, 5):
        assert re.match(
            r"^  [0-9a-f]{7,} ", lines[idx]
        ), f"line {idx} not a commit short-sha entry: {lines[idx]!r}"
    # The block ends with a single trailing newline, no orphan blank lines.
    assert result.endswith("\n")
    assert "\n\n" not in result, (
        "default rendering should not contain blank lines — extras and "
        "rejections are absent in this fixture"
    )


def test_current_state_block_omits_board_and_commits_when_kwargs_false(
    tmp_path,
):
    """TB-168: with include_board=False and include_commits=False, the
    rendered snapshot contains only the header and `now:` — the two sub-
    blocks are dropped entirely. Pin: (a) `now:` survives, (b) `board:`
    is gone, (c) the recent-commits heading and commit short-sha lines
    are gone, (d) no whitespace-only orphan blocks where the suppressed
    sections would have been."""
    from ap2.prompts import _current_state_block

    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n- [TB-1] one ready task\n\n"
        "## Backlog\n\n## Complete\n\n## Frozen\n"
    )
    _init_git_with_two_commits(tmp_path)
    cfg = Config.load(tmp_path)

    result = _current_state_block(
        cfg, include_board=False, include_commits=False,
    )

    # (a) `now:` survives.
    import re

    assert re.search(
        r"now: 20\d\d-[01]\d-[0-3]\dT[0-2]\d:[0-5]\d:[0-5]\dZ", result
    ), "`now:` line missing from trimmed snapshot"
    # (b) `board:` substring is gone — both the line label AND the
    # `(Active/Ready/Backlog/...)` legend it carried.
    assert "board:" not in result
    assert (
        "(Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)"
        not in result
    )
    # (c) `recent commits` heading is gone, and no commit short-sha
    # patterns appear (would render as `^  [0-9a-f]{7,} <subject>`).
    assert "recent commits" not in result.lower()
    for line in result.splitlines():
        assert not re.match(
            r"^  [0-9a-f]{7,} ", line
        ), f"line looks like a commit short-sha: {line!r}"
    # (d) No orphan blank lines. With both sub-blocks suppressed and no
    # extras / rejections in the fixture, the rendered body is exactly
    # the header + `now:` + trailing newline.
    assert result == (
        "## Current state (rendered just before this prompt was sent)\n"
        + result.split("\n", 2)[1] + "\n"
    ), "trimmed body has unexpected content beyond header + `now:`"


def test_current_state_block_omits_only_board_when_commits_kept(tmp_path):
    """TB-168: kwargs are independent — include_board=False keeps the
    commits sub-block and vice versa. Pins both (a) the per-kwarg
    independence, and (b) that suppressing one sub-block doesn't leave
    an orphan blank line where it would have rendered."""
    from ap2.prompts import _current_state_block

    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Complete\n\n## Frozen\n"
    )
    _init_git_with_two_commits(tmp_path)
    cfg = Config.load(tmp_path)

    only_commits = _current_state_block(cfg, include_board=False)
    # `now:` and `recent commits` survive; `board:` is gone.
    assert "now:" in only_commits
    assert "- recent commits (HEAD~10):" in only_commits
    assert "board:" not in only_commits
    # No blank line between `now:` and the commits sub-block.
    assert "\n\n" not in only_commits

    only_board = _current_state_block(cfg, include_commits=False)
    # `now:` and `board:` survive; `recent commits` is gone.
    assert "now:" in only_board
    assert "- board: " in only_board
    assert "recent commits" not in only_board.lower()
    assert "\n\n" not in only_board


def test_build_control_prompt_forwards_include_kwargs_to_state_block(
    tmp_path,
):
    """TB-168: `build_control_prompt(cfg, "ideation", load_prompt(cfg),
    include_board=False, include_commits=False)` produces a prompt whose
    `## Current state` block contains `now:` but neither `board:` nor
    `recent commits`. The rest of the prompt (`_CONTROL_HEADER`, body,
    `## Guidance`, `_events_block`) is unchanged.
    """
    cfg = _cfg(tmp_path)
    p = build_control_prompt(
        cfg, "ideation", "(ideation body)",
        include_board=False, include_commits=False,
    )

    # Snapshot block: `now:` survives, `board:`/`recent commits` are gone.
    assert "## Current state (rendered just before this prompt was sent)" in p
    assert "now:" in p
    assert "board:" not in p
    assert "recent commits" not in p.lower()
    # The `(Active/Ready/Backlog/...)` legend lived only inside the
    # `board:` line — its absence is a strong negative pin.
    assert (
        "(Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)" not in p
    )

    # Rest of the prompt is unchanged: `_CONTROL_HEADER`, the job-name
    # framing, the body verbatim, the guidance block, the events tail.
    assert "autopilot v2 control agent" in p  # _CONTROL_HEADER lead-in
    assert "## Control job: ideation" in p
    assert "(ideation body)" in p
    assert "## Guidance" in p
    assert "## Recent events" in p


def test_build_control_prompt_default_kwargs_keep_status_report_shape(
    tmp_path,
):
    """TB-168: when called WITHOUT the new kwargs, `build_control_prompt`
    renders the snapshot block in its pre-TB-168 shape — `now:`,
    `board:`, and `recent commits` all present. This pins backwards
    compatibility for the status-report cron and any future caller that
    omits the kwargs."""
    cfg = _cfg(tmp_path)
    p = build_control_prompt(cfg, "status-report", "post a status report")

    # All three load-bearing snapshot lines render.
    assert "- now: " in p
    assert "- board: " in p
    assert "- recent commits (HEAD~10):" in p
    # The board legend is intact.
    assert "(Active/Ready/Backlog/Pipeline-Pending/Complete/Frozen)" in p
