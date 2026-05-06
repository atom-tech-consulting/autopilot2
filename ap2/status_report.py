"""Shared status-report routine (TB-144).

Pre-TB-144 the status-report agent invocation was entangled with the cron
tick: the prompt body lived in `cron.default.yaml`, the freshness contract
was appended only when `job.name == "status-report"` in
`prompts.build_control_prompt`, and the skip-if-idle gate
(`_status_report_should_skip`) was a daemon-private helper called only
from `daemon.run_cron`. The Mattermost handler had no way to compose a
status report with the same shape and audit trail — it built freeform
replies that drifted from the canonical format.

This module hoists everything status-report-specific into one callable so
the cron tick AND on-demand operator triggers (via the
`mcp__autopilot__status_report_run` MCP tool) share:

  - the same prompt body (`STATUS_REPORT_PROMPT`),
  - the same skip-if-idle gate (TB-128),
  - the same `cron_start` / `cron_complete` / `cron_skipped` event
    vocabulary (with a `trigger="cron"|"chat"` field so post-mortems can
    distinguish the two),
  - the same allowed-tools surface and SDK plumbing
    (`daemon._run_control_agent`).

Cron-trigger reports advance `cron_state[status-report].last_run`; chat-
trigger reports DO NOT — otherwise an operator-triggered report at 11:00
would silence the scheduled noon cron, which is the opposite of what the
operator asked for.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from . import events
from .board import Board
from .config import Config
from .cron import mark_run


# TB-151: shared truncation rule for pending-review TB-N lists. `ap2
# status` (CLI) and the cron status-report both call
# `_format_pending_review_line` so the cap stays in sync — bumping it
# here moves both surfaces in lockstep.
_PENDING_REVIEW_TRUNCATE_AT = 5


def _format_pending_review_line(ids: list[str]) -> str:
    """Format pending-review TB-Ns into a comma-joined display string.

    Truncates to the first `_PENDING_REVIEW_TRUNCATE_AT` IDs with a
    "(+N more)" suffix when the list is longer, matching the
    `diagnose._auto_diagnose_summary` rendering precedent so all three
    surfaces (CLI, cron status-report, watchdog summary) cap noise the
    same way. Returns the empty string for an empty list — callers
    decide whether to suppress their wrapping prefix when N=0.

    Pure / no I/O so both `ap2.cli.cmd_status` and
    `ap2.status_report.run_status_report` can call it without dragging
    in a Board load. Defined in this module (and imported by `cli.py`)
    so the verification grep `_format_pending_review_line` lands in
    both files (TB-151).
    """
    if not ids:
        return ""
    if len(ids) <= _PENDING_REVIEW_TRUNCATE_AT:
        return ", ".join(ids)
    head = ", ".join(ids[:_PENDING_REVIEW_TRUNCATE_AT])
    return f"{head} (+{len(ids) - _PENDING_REVIEW_TRUNCATE_AT} more)"


def _pending_review_ids(cfg: Config) -> list[str]:
    """Return TB-Ns of Backlog tasks with the `review` blocker scheme.

    Mirrors the comprehension at `cli.cmd_status` (kept inline there to
    avoid a `diagnose` import for one number) and `web._is_pending_review`.
    Predicate: at least one blocker, AND `review` appears among them
    (TB-187). The status-report routine needs the full list (not just
    the count) to inject the "Pending operator review (N): TB-..." line
    into the snapshot block; failing to load the board is treated as
    zero pending so a transient parse error never blocks a status post.

    Note: `diagnose._board_health` uses a stricter `all(...)`-flavored
    predicate intentionally — its watchdog needs to distinguish
    review-only Backlog (operator AFK) from mixed-blocker tasks
    (which it inspects for unsatisfiable non-review blockers
    separately). The surfacing predicate here is the loose one.
    """
    if not cfg.tasks_file.exists():
        return []
    try:
        board = Board.load(cfg.tasks_file)
    except Exception:  # noqa: BLE001
        return []
    return [
        t.id for t in board.iter_tasks("Backlog")
        if t.blocked_on and any(b.lower() == "review" for b in t.blocked_on)
    ]


# Body that pre-TB-144 lived in `cron.default.yaml`. The cron job's prompt
# field is now a stub ("see ap2.status_report.STATUS_REPORT_PROMPT") because
# the daemon's `run_cron` short-circuits status-report jobs to
# `run_status_report(...)` instead of `build_control_prompt(cfg, name,
# job.prompt)`. Operators with pre-existing cron.yaml files keep their copy
# until they re-bootstrap; the runtime ignores `job.prompt` for this job
# regardless, so the routine's content is always authoritative.
STATUS_REPORT_PROMPT = """\
Post a concise autopilot status report to the channel ID from the
`- post target channel:` line in the `## Current state` snapshot above
(TB-190; the daemon resolves `AP2_MM_REPORT_CHANNEL` — falling back to
`AP2_MM_CHANNELS[0]` — and injects the resolved ID there). If that line
is absent, the operator hasn't configured a status-report target — call
`log_event(type="status_report", summary="skipped: no AP2_MM_REPORT_CHANNEL or AP2_MM_CHANNELS configured")`
and finish. Do NOT guess a channel ID from server defaults or recent
inbound `mattermost` events.

Freshness contract (TB-128 — non-negotiable):
- The headline timestamp in your post is the literal `now:` value
  from the `## Current state` block at the top of this prompt. Do
  NOT compute, guess, or copy a timestamp from any other source.
- Re-read `.cc-autopilot/events.jsonl` (last ~50 lines) and
  `TASKS.md` with the `Read` tool right now, before composing the
  post. The board counts in the snapshot block above are
  authoritative; the embedded events tail is a courtesy.
- If nothing of substance has happened since the last
  `status_report` event in the tail (no new task_start /
  task_complete / verification_failed / pipeline_* /
  retry_exhausted / daemon_pause / daemon_resume / operator_ack /
  cron_proposed / ideation_complete events), SKIP the Mattermost
  post entirely. Just call
  `log_event(type="status_report", summary="skipped: no activity
  since <ts>")` and finish. The daemon also has a deterministic
  skip-gate, but you should mirror the decision so the report
  reflects current reality if you do post.

Body shape (when posting):
- Headline: `**Autopilot Status Report** — <now>`
- 4-8 bullets covering: tasks completed (TB-N + 1-line outcome +
  short SHA), tasks failed / verification_failed / retry_exhausted,
  pipelines started/completed, cron / ideation activity, daemon
  pause/resume, operator acks, open issues. Keep under 12 lines.
- TB-151: if the snapshot's `## Current state` block carries a
  `- Pending operator review (N): TB-...` line, copy that line
  VERBATIM as one of your bullets so the operator sees which TB-Ns
  are waiting on `ap2 approve` without having to grep TASKS.md. If
  the line is absent, omit the bullet — there's nothing to surface.
- TB-173: if the snapshot's `## Current state` block carries an
  `- Open questions for operator (N): ...` line, copy that line
  VERBATIM as one of your bullets too. The ideator surfaces this
  section when a focus item is `exhausted-needs-operator`, when
  goal.md appears to need updating, or when a gap was noticed
  outside any current focus item — operator-judgement work that
  needs visibility on the report. If the line is absent, omit the
  bullet — there's nothing to surface.
- TB-182: BEFORE you forward the open-questions line (or any TB-N
  reference its bullets carry) into the post, validate against
  events.jsonl that the references are still current. The bullets
  were written by the ideator at the most recent
  `ideation_state_updated` event in the tail; up to the ideation
  interval (~2h) of staleness can bleed through into the open-
  questions snapshot. Procedure:
    1. Note the `ts` of the most recent `ideation_state_updated`
       event in `events.jsonl`. That's when the open-questions
       content was last refreshed.
    2. For every TB-N referenced in a forwarded bullet, scan
       events.jsonl for any `task_complete`, `task_deleted`,
       `task_updated`, or `verification_failed` event for that TB-N
       with `ts` AFTER the `ideation_state_updated` ts.
    3. If found, the bullet is stale. Either skip it entirely
       (preferred when the bullet's premise no longer holds — e.g.
       a "TB-N retry watch" bullet for a TB-N that has now landed
       Complete) OR rewrite it with a parenthetical noting the
       staleness (e.g. "(per stale ideation_state.md; TB-N landed
       Complete at <ts>)"). Skipping is preferred — the snapshot
       line is best-effort, not load-bearing.
    4. If no superseding event is found, the bullet's TB-N
       references are still current — forward as-is.
  This validation is reasoning-only; the agent already has both
  events.jsonl and the snapshot in context. Don't wait on a tool;
  walk the events tail you already read above and decide.
- TB-177: if the snapshot's `## Current state` block carries a
  `- Janitor findings (N): stranded git state — ...` line, copy
  that line VERBATIM as one of your bullets too. The janitor cron
  surfaces stranded git state (staged-but-uncommitted, modified
  not staged, untracked-non-ignored) — operator-attention work
  that the report should carry. Absent ⇒ healthy ⇒ omit.

After posting (or skipping), call
`log_event(type="status_report", summary="<one sentence>")` so the
next run can find this report's marker in the tail.
"""


# Default max_turns for the status-report sub-agent. Mirrors the value
# `cron.default.yaml` carried pre-TB-144. The cron path passes the cron
# job's `max_turns` through so an operator who tunes `cron.yaml` keeps
# control; the chat path uses this default.
DEFAULT_MAX_TURNS = 10


# Events the skip-gate treats as self-noise — i.e. the routine's own
# bookkeeping that should NOT count as "fresh activity" for the purpose
# of suppressing back-to-back reports. See `_status_report_should_skip`.
_STATUS_REPORT_BORING_TYPES = frozenset(
    {"cron_start", "cron_complete", "status_report", "cron_skipped",
     "state_committed"}
)


def _status_report_should_skip(cfg: Config) -> bool:
    """Return True iff a status-report run would be a no-op (TB-128).

    "No-op" means: there's a previous `cron_complete job=status-report`
    in the recent tail AND no events of interest have been appended
    after it (positionally — the events log timestamps to one-second
    resolution, so same-second self-noise after the cron_complete must
    not be misread as fresh activity). Events of interest are anything
    except this job's own bookkeeping (cron_start / cron_complete for
    status-report, the agent's `status_report` log_event, the cron's
    outbound `mattermost_reply` that quotes the status report header,
    and previous `cron_skipped` markers).

    Returns False if the job has never run before (or its last run
    rolled out of the tail) — first-run / cold-cache, always run.

    Pre-TB-144 this lived in `daemon.py` and was cron-only; now both
    the cron tick AND the chat-trigger MCP tool route through the same
    gate so on-demand operator reports honor the same idle-skip
    semantics as scheduled ones.
    """
    evts = events.tail(cfg.events_file, n=200)
    last_done_idx = -1
    for i in range(len(evts) - 1, -1, -1):
        e = evts[i]
        if (
            e.get("type") == "cron_complete"
            and e.get("job") == "status-report"
        ):
            last_done_idx = i
            break
    if last_done_idx < 0:
        return False  # never ran (or rolled out of tail) — run it.
    for e in evts[last_done_idx + 1:]:
        typ = e.get("type", "")
        if typ in _STATUS_REPORT_BORING_TYPES:
            continue
        # The status-report cron's outbound post is a `mattermost_reply`
        # whose summary starts with the report headline. Filter those
        # out so back-to-back status posts don't keep "feeding" each
        # other as activity.
        if typ == "mattermost_reply":
            summary = e.get("summary", "") or ""
            if "Autopilot Status Report" in summary[:80]:
                continue
        # Found something interesting → don't skip.
        return False
    # Reached end of tail without finding interesting activity → skip.
    return True


@dataclass
class StatusReportResult:
    """Outcome shape for `run_status_report`.

    `skipped=True` means the skip-if-idle gate fired and no SDK turn was
    burned; `reason` carries the gate's reason string so the caller can
    surface it in chat replies. `skipped=False` means the SDK turn ran;
    `error` is set if the SDK timed out or crashed (mirrors the cron
    path's error-event semantics — the caller can still report success
    to the operator since the event audit trail is intact).
    """

    skipped: bool
    reason: str | None = None
    error: str | None = None
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Daemon-side wiring for the MCP tool.
#
# `mcp__autopilot__status_report_run` is invoked from inside an MCP tool
# handler; the handler doesn't have access to the daemon's `sdk` /
# `mcp_server` references (those are positional args to `run_status_report`).
# The daemon calls `configure(sdk, mcp_server)` once at startup
# (`main_loop`, after `build_mcp_server`) so the MCP tool can resolve them
# at call time. Tests configure their FakeSDK the same way before driving
# `do_status_report_run` directly.
#
# Module-level dict (instead of a contextvar) because the references are
# process-wide and immutable for the daemon's lifetime — the contextvar
# pattern is for per-task plumbing (see `tools._task_id_ctx`), not for
# long-lived singletons.

_SDK_REF: dict = {"sdk": None, "mcp_server": None}


def configure(sdk, mcp_server) -> None:
    """Stash the daemon's SDK + MCP server references for the MCP tool.

    Called once from `daemon.main_loop` after both are built. Tests that
    drive `do_status_report_run` directly should call this with their
    FakeSDK + a (possibly None) mcp_server before exercising the tool.
    Idempotent — re-calling overwrites the previous references, which is
    the right shape for tests that want to swap fakes between runs.
    """
    _SDK_REF["sdk"] = sdk
    _SDK_REF["mcp_server"] = mcp_server


def _resolved_sdk_refs() -> tuple[object, object]:
    """Return the configured (sdk, mcp_server) pair.

    Raises RuntimeError if `configure(...)` hasn't been called yet — the
    MCP tool surfaces this as an error response so the operator sees
    "status_report_run unavailable" instead of an opaque AttributeError.
    """
    sdk = _SDK_REF.get("sdk")
    mcp_server = _SDK_REF.get("mcp_server")
    if sdk is None:
        raise RuntimeError(
            "status_report.configure(sdk, mcp_server) has not been called; "
            "the MCP tool cannot dispatch a sub-agent without the daemon's "
            "SDK reference"
        )
    return sdk, mcp_server


# ---------------------------------------------------------------------------
# The shared routine.


async def run_status_report(
    cfg: Config,
    sdk,
    mcp_server,
    *,
    trigger: Literal["cron", "chat"],
    reason: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> StatusReportResult:
    """Run a status-report agent (TB-144).

    Both the cron tick (`daemon.run_cron` when `job.name ==
    "status-report"`) and the chat-trigger MCP tool
    (`mcp__autopilot__status_report_run`) call this so every status
    report shares one prompt, one skip-gate, and one event vocabulary.

    Steps:
      1. Skip-if-idle gate (`_status_report_should_skip`). On skip,
         emit `cron_skipped` with `trigger=...` and (cron only) advance
         `cron_state` so the daemon doesn't re-fire every tick.
      2. Build the control prompt — same `## Current state` snapshot the
         cron path used pre-TB-144, with the freshness contract still
         appended via `prompts.build_control_prompt(cfg, "status-report",
         STATUS_REPORT_PROMPT)`.
      3. Emit `cron_start` (with `trigger=...` field), invoke the SDK
         via `daemon._run_control_agent`, emit `cron_complete` (with
         `trigger=...`).
      4. Cron-trigger advances `cron_state[status-report].last_run`;
         chat-trigger does NOT (an operator-triggered report at 11:00
         must not silence the scheduled noon cron).

    Returns a `StatusReportResult` so the caller can surface skip/error
    state to the operator.
    """
    # Lazy import to avoid the daemon ↔ status_report cycle. Same pattern
    # `ideation._maybe_ideate` uses to reach `_run_control_agent` /
    # `_commit_state_files`.
    from . import daemon as _daemon
    from . import prompts as _prompts
    from .tools import CONTROL_AGENT_TOOLS

    if _status_report_should_skip(cfg):
        skip_payload: dict = {
            "job": "status-report",
            "trigger": trigger,
            "reason": "no_activity_since_last_report",
        }
        if reason:
            skip_payload["chat_reason"] = reason
        events.append(cfg.events_file, "cron_skipped", **skip_payload)
        if trigger == "cron":
            mark_run(cfg.cron_state_file, "status-report")
        return StatusReportResult(
            skipped=True, reason="no_activity_since_last_report",
        )

    # TB-151: surface pending-review TB-Ns inside the `## Current state`
    # snapshot block so the agent can copy the line verbatim into the
    # posted Mattermost report. The list is collected fresh per run
    # (board state moves between ticks); when N=0 we skip the line
    # entirely so a clean board doesn't grow a noisy "0 pending"
    # bullet. The wrapping prefix mirrors `diagnose._auto_diagnose_summary`'s
    # phrasing — "Pending operator review (N): TB-..." — so an operator
    # who reads watchdog summaries and status reports doesn't have to
    # context-switch between two phrasings.
    pending_ids = _pending_review_ids(cfg)
    state_extras: list[str] = []
    if pending_ids:
        state_extras.append(
            f"- Pending operator review ({len(pending_ids)}): "
            f"{_format_pending_review_line(pending_ids)} "
            "— `ap2 approve TB-N`"
        )
    # TB-173: surface the ideator's `## Open questions for operator`
    # section so the cron status-report carries the same escalation
    # signal as the CLI / web home (single source of truth via
    # `parse_open_questions`). Bullets joined with `; ` so the line
    # mirrors the CLI text rendering — the agent then forwards the line
    # verbatim into the Mattermost post per the prompt's contract below.
    # When the file or section is absent / empty the helper returns []
    # and we skip the line entirely so a clean board doesn't grow a
    # noisy "0 open questions" bullet.
    from .ideation import parse_open_questions

    open_questions = parse_open_questions(
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if open_questions:
        state_extras.append(
            f"- Open questions for operator ({len(open_questions)}): "
            + "; ".join(open_questions)
        )
    # TB-177 + TB-178: surface recent janitor findings inside the
    # `## Current state` snapshot so the cron status-report routine can
    # carry the signal into the Mattermost post. Verdict-aware split
    # (strands vs drafts vs ambiguous) so `draft_*.md` operator
    # notebooks don't read as urgent in the post; only `real_strand`
    # carries the operator-attention urgency. Bundled next to
    # pending-review + open-questions keeps the operator-attention
    # signals on one screen.
    from .janitor import (
        recent_finding_counts_by_verdict as _recent_finding_counts,
    )

    jcounts = _recent_finding_counts(cfg)
    n_strand = jcounts["real_strand"]
    n_draft = jcounts["operator_draft"]
    n_ambig = jcounts["ambiguous"]
    if n_strand or n_draft or n_ambig:
        parts: list[str] = []
        if n_strand:
            parts.append(f"{n_strand} strand{'s' if n_strand != 1 else ''}")
        if n_draft:
            parts.append(f"{n_draft} draft{'s' if n_draft != 1 else ''}")
        if n_ambig:
            parts.append(f"{n_ambig} ambiguous")
        state_extras.append(
            f"- Janitor findings: {', '.join(parts)} — "
            "`ap2 logs` (filter type=janitor_finding) to inspect"
        )
    # TB-190: resolve the status-report target channel server-side.
    # Pre-fix the prompt asked the agent to read `AP2_MM_REPORT_CHANNEL`
    # itself, but control agents have no env-var access — the agent saw
    # the literal env-var name and ended up posting to whatever channel
    # the server defaulted to (town-square in practice), NOT the
    # operator's configured channel. The fix moves resolution to the
    # daemon: explicit `AP2_MM_REPORT_CHANNEL` wins; otherwise fall back
    # to the first entry of `AP2_MM_CHANNELS` (the inbound-watch channel
    # is the natural place to send outbound status posts in single-
    # channel projects). When neither is set we omit the line entirely
    # — the prompt body then routes the agent into the explicit-skip
    # branch with a `log_event` audit so the operator can grep
    # events.jsonl for the configuration miss.
    target_channel = os.environ.get("AP2_MM_REPORT_CHANNEL", "").strip()
    if not target_channel:
        raw_channels = os.environ.get("AP2_MM_CHANNELS", "").strip()
        for c in raw_channels.split(","):
            c = c.strip()
            if c:
                target_channel = c
                break
    if target_channel:
        state_extras.append(f"- post target channel: {target_channel}")
    prompt = _prompts.build_control_prompt(
        cfg, "status-report", STATUS_REPORT_PROMPT,
        state_extras=state_extras,
    )
    start_payload: dict = {"job": "status-report", "trigger": trigger}
    if reason:
        start_payload["reason"] = reason
    events.append(cfg.events_file, "cron_start", **start_payload)

    # TB-156: status-report is a pure summarization job (read events tail,
    # render markdown, post to Mattermost). It doesn't need the multi-step
    # reasoning budget that `xhigh` is sized for. Default to `medium` so
    # cron + chat-trigger reports run cheaper than task agents (which
    # stay on the global default, `xhigh`); operators can still pin a
    # specific value via `AP2_STATUS_REPORT_EFFORT`, or globally via
    # `AP2_AGENT_EFFORT`. Precedence: per-site env > global env > per-site
    # default.
    effort = os.environ.get(
        "AP2_STATUS_REPORT_EFFORT",
        os.environ.get("AP2_AGENT_EFFORT", "medium"),
    )
    timed_out, error, stderr_tail, prompt_dump = await _daemon._run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label="cron-status-report",
        prompt=prompt,
        allowed_tools=CONTROL_AGENT_TOOLS,
        max_turns=max_turns,
        effort=effort,
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "cron_timeout",
            job="status-report",
            trigger=trigger,
            timeout_s=cfg.control_timeout_s,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "cron_error",
            job="status-report",
            trigger=trigger,
            error=error,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )

    if trigger == "cron":
        mark_run(cfg.cron_state_file, "status-report")
    events.append(
        cfg.events_file,
        "cron_complete",
        job="status-report",
        trigger=trigger,
    )
    return StatusReportResult(
        skipped=False,
        timed_out=timed_out,
        error=error,
    )
