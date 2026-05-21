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

import datetime as _dt
import os
from dataclasses import dataclass
from typing import Literal

from . import automation_stats, automation_status, events
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


# ---------------------------------------------------------------------------
# TB-228: Automation loop activity digest section for the cron post.
#
# The walk-away operator's first-touch surface (the scheduled status-
# report Mattermost post) was silent on the TB-223 / TB-224 / TB-225
# automation loop: an operator returning to find 12 auto-approved tasks
# had landed unattended had to alt-tab to `ap2 logs` to see it. The
# digest below renders ONE Markdown section the agent forwards verbatim
# into the post; the heading literal `## Automation loop activity` is
# the load-bearing string both the prompt contract and the verification
# grep pin on.
#
# Omit-on-empty rule (no zero-noise on pre-opt-in projects): the
# section renders ONLY when at least one of these is true:
#   - `AP2_AUTO_APPROVE=1` in the daemon env, OR
#   - any `auto_approved` / `auto_unfreeze_applied` /
#     `auto_unfreeze_skipped` / `auto_approve_paused` event fired
#     since the previous `cron_complete name=status-report` event.
# When both conditions are false the renderer returns "" so a fresh
# project / pre-opt-in cron run doesn't grow a perpetual "0 since last
# report" bullet — same omit-on-empty pattern TB-151 / TB-227 use.

_AUTOMATION_DIGEST_HEADING = "## Automation loop activity"


def _format_skipped_reason_breakdown(by_reason: dict[str, int]) -> str:
    """Render the `auto_unfreeze_skipped` reason breakdown as a compact
    `reason1=N, reason2=M` string for the digest bullet. Order is
    alphabetical so the rendered string is deterministic across runs
    (operator scanning two reports back-to-back sees stable order)."""
    if not by_reason:
        return ""
    parts = [f"{k}={v}" for k, v in sorted(by_reason.items())]
    return ", ".join(parts)


def render_automation_loop_activity_section(
    cfg: Config,
    *,
    since_event_idx: int,
    tail: list[dict] | None = None,
) -> str:
    """Return the Markdown `## Automation loop activity` section the
    cron agent forwards verbatim into the Mattermost post, or "" when
    the section should be omitted entirely.

    `since_event_idx` is the positional index of the previous
    `cron_complete job=status-report` event in the tail; counts in
    the rendered section are scoped to events at indices > that.

    Shape (when rendered):

        ## Automation loop activity

        auto-approve: <healthy|PAUSED reason=X>; auto-unfreeze: <healthy|cooldown>

        - N tasks auto-approved (M completed, K froze)
        - L tasks auto-unfrozen / R briefing-fix shapes auto-applied (P succeeded, Q re-froze)
        - S auto-unfreeze attempts skipped (reason breakdown)
        - Most recent halt: <ts> <event_type> reason=<x> — run `ap2 ack <verb>` to clear

        *Dry-run window:*
        - auto-approve: `<N>` `would_auto_approve` in 24h
        - auto-unfreeze: `<M>` `would_auto_unfreeze` in 24h

    TB-238: the trailing "Dry-run window" sub-block renders only when
    at least one of `dry_run_enabled` / `auto_unfreeze_dry_run_enabled`
    is True in the collector output, with only the on-axis line(s)
    surfaced (the other axis's line is suppressed). When both
    dry-runs are off, the sub-block is omitted entirely so the
    default-off digest output stays byte-identical to TB-228 — the
    load-bearing regression pin operators rely on.

    Omit-on-empty: returns "" when knob unset AND all four event-type
    counters in the window are zero. Pin the omission rule with the
    test `test_section_absent_when_knob_off_and_all_counters_zero`.
    """
    if tail is None:
        if cfg.events_file.exists():
            tail = events.tail(cfg.events_file, 2000)
        else:
            tail = []

    state = automation_status.collect_auto_approve_state(cfg)
    activity = automation_status.collect_window_loop_activity(
        cfg, since_event_idx=since_event_idx, tail=tail,
    )

    # Omit-on-empty: the section is suppressed only when the operator
    # hasn't opted in (knob unset) AND no automation-loop events fired
    # in the window. The four event-type counters mirror the briefing's
    # "interesting events" list — `auto_approve_paused` /
    # `auto_unfreeze_applied` are included so a halt or fix in the
    # window still surfaces the section even after the operator
    # toggles the knob off.
    enabled = state["auto_approve_enabled"]
    nothing_happened = (
        activity["auto_approved"] == 0
        and activity["auto_unfreeze_applied"] == 0
        and activity["auto_unfreeze_skipped"] == 0
        and activity["auto_approve_paused"] == 0
        and activity["auto_approve_halted"] == 0
    )
    if not enabled and nothing_happened:
        return ""

    # Headline line: paused | healthy on each axis. `auto_approve_paused`
    # event in this window means we render PAUSED with the reason; same
    # `auto_approve_halted` (renamed in the helper). Auto-unfreeze axis
    # never "pauses" with a discrete event; we render "cooldown" when
    # any `auto_unfreeze_skipped reason=per_day_cap` event landed in
    # the window (operator hit the daily cap), else "healthy".
    pause_reason = state["pause_reason"]
    if pause_reason:
        auto_approve_status = f"PAUSED reason={pause_reason}"
    else:
        auto_approve_status = "healthy"
    by_reason = activity["auto_unfreeze_skipped_by_reason"]
    auto_unfreeze_status = (
        "cooldown" if by_reason.get("per_day_cap", 0) else "healthy"
    )
    headline = (
        f"auto-approve: {auto_approve_status}; "
        f"auto-unfreeze: {auto_unfreeze_status}"
    )

    # Bullet list. The auto-approved / auto-unfrozen lines render even
    # when the counts are zero so a knob-on project sees a stable
    # zero-baseline rather than the bullets vanishing on a quiet
    # window (operator wants the "nothing happened" signal to be
    # legible too). The auto-approved line is always emitted; the
    # other three are conditional on non-zero counts.
    bullets: list[str] = []
    bullets.append(
        f"- {activity['auto_approved']} tasks auto-approved "
        f"({activity['auto_approved_completed']} completed, "
        f"{activity['auto_approved_froze']} froze)"
    )
    if activity["auto_unfreeze_applied"] or activity["auto_unfreeze_tasks"]:
        bullets.append(
            f"- {activity['auto_unfreeze_tasks']} tasks auto-unfrozen "
            f"/ {activity['auto_unfreeze_applied']} briefing-fix "
            f"shapes auto-applied "
            f"({activity['auto_unfreeze_succeeded']} succeeded, "
            f"{activity['auto_unfreeze_refroze']} re-froze)"
        )
    if activity["auto_unfreeze_skipped"]:
        breakdown = _format_skipped_reason_breakdown(by_reason)
        bullets.append(
            f"- {activity['auto_unfreeze_skipped']} auto-unfreeze "
            f"attempts skipped ({breakdown})"
        )
    latest_halt = activity["latest_halt"]
    if latest_halt:
        bullets.append(
            f"- Most recent halt: {latest_halt['ts']} "
            f"{latest_halt['event_type']} "
            f"reason={latest_halt['reason']} — run "
            f"`ap2 ack {latest_halt['ack_verb']}` to clear"
        )

    # TB-238: optional "Dry-run window" sub-block. Rendered only when
    # at least one dry-run knob is on, so the default-off output stays
    # byte-identical to TB-228 (regression-pinned). The sub-block is
    # appended at the END of the section so existing readers' muscle
    # memory for the bullet positions is preserved. Per-axis lines are
    # suppressed individually so an operator who only flipped one of
    # the two knobs sees just the on-axis line — no zero-noise from
    # the off axis.
    aa_dry_run = state["dry_run_enabled"]
    au_dry_run = state["auto_unfreeze_dry_run_enabled"]
    dry_run_lines: list[str] = []
    if aa_dry_run or au_dry_run:
        dry_run_lines.append("*Dry-run window:*")
        if aa_dry_run:
            dry_run_lines.append(
                f"- auto-approve: `{state['would_auto_approve_count_24h']}` "
                f"`would_auto_approve` in 24h"
            )
        if au_dry_run:
            dry_run_lines.append(
                f"- auto-unfreeze: "
                f"`{state['would_auto_unfreeze_count_24h']}` "
                f"`would_auto_unfreeze` in 24h"
            )

    section = (
        f"{_AUTOMATION_DIGEST_HEADING}\n\n"
        f"{headline}\n\n"
        + "\n".join(bullets)
    )
    if dry_run_lines:
        section += "\n\n" + "\n".join(dry_run_lines)
    return section


# ---------------------------------------------------------------------------
# TB-244: Focus rotation activity sub-section for the cron status-report
# digest. Parallels `render_automation_loop_activity_section` above —
# same omit-on-empty rule, same `state_extras` wiring shape, but a
# distinct heading + helper so the axis-1/2/3 digest's existing test
# expectations stay byte-identical (the briefing's option B).
#
# Closes TB-228 / TB-238's surface-parity gap on the push channel: the
# operator's primary walk-away surface (the 2h status-report Mattermost
# post) was silent on axis-4 (`focus_advanced` / `roadmap_complete`),
# which contradicts axis 4's own framing ("walk-away time scales with
# the operator-declared roadmap length", goal.md L137-138). A
# `roadmap_complete` halt at 03:00Z used to wait for the operator's
# next manual `ap2 status` to surface; now it lands in the very next
# 2h cron post.

_FOCUS_ROTATION_HEADING = "## Focus rotation activity"


def render_focus_rotation_activity_section(
    cfg: Config,
    *,
    since_event_idx: int,
    tail: list[dict] | None = None,
) -> str:
    """Return the Markdown `## Focus rotation activity` sub-section
    the cron agent forwards verbatim into the Mattermost post, or ""
    when the section should be omitted entirely (no axis-4 events
    landed in the inter-report window).

    `since_event_idx` is the positional index of the previous
    `cron_complete job=status-report` event in the tail; counts in
    the rendered section are scoped to events at indices > that.

    Shape (when rendered):

        ## Focus rotation activity

        - focus_advanced: <from-title> → <to-title> (N of M)
        - roadmap_complete: all foci exhausted — ideation parked; `ap2 update-goal` to resume or `ap2 ack roadmap_complete` to dismiss

    Each line is rendered once per event in the window (so a window
    with 2 advances + 1 halt yields 3 lines). The lines preserve
    tail order (TB-226 emits `focus_advanced` first when the advance
    crosses the last focus, then `roadmap_complete` on the same
    tick) so a multi-event window reads chronologically.

    Omit-on-empty: returns "" when the helper's `total` is 0 — no
    axis-4 events in the window means no sub-block. Symmetric to
    TB-228's `render_automation_loop_activity_section` rule (knob
    off + all counters zero); axis 4 has no opt-in knob (the focus
    list is operator-curated and the daemon always tracks the
    pointer when the focus list is non-empty), so the only gate is
    the "did something happen" counter.
    """
    if tail is None:
        if cfg.events_file.exists():
            tail = events.tail(cfg.events_file, 2000)
        else:
            tail = []

    activity = automation_status.collect_window_focus_rotation(
        cfg, since_event_idx=since_event_idx, tail=tail,
    )
    if activity["total"] == 0:
        return ""

    lines: list[str] = [_FOCUS_ROTATION_HEADING, ""]
    # `focus_advanced` rendering: include the (N of M) position when
    # the daemon's payload carried it (TB-226 always does, but be
    # defensive about a future schema change). `from` / `to` are
    # always present in the payload; an empty `to` means the advance
    # crossed past the last focus (and `roadmap_complete` fired on
    # the same tick — see the second loop below).
    for ev in activity["focus_advanced"]:
        from_title = ev.get("from") or "(none)"
        to_title = ev.get("to") or "(none)"
        new_index = ev.get("new_index")
        total_foci = ev.get("total_foci")
        if isinstance(new_index, int) and isinstance(total_foci, int):
            # `new_index` is 0-based in the event payload; the
            # operator-facing "N of M" uses 1-based to match
            # TB-242's `ap2 status` rendering (`alpha (1 of 3)`).
            position = f" ({new_index + 1} of {total_foci})"
        else:
            position = ""
        lines.append(
            f"- focus_advanced: {from_title} → {to_title}{position}"
        )
    # `roadmap_complete` rendering: the resume + dismiss hints are
    # verbatim so the operator can copy-paste them from the
    # Mattermost post. TB-275: this is now an ideation-trigger
    # park, not a dispatch halt — extend the roadmap to resume
    # IDEATION, or ack to dismiss the notice. Task dispatch is NOT
    # affected.
    for _ev in activity["roadmap_complete"]:
        lines.append(
            "- roadmap_complete: all foci exhausted — "
            "ideation parked; `ap2 update-goal` to resume or "
            "`ap2 ack roadmap_complete` to dismiss"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TB-245: Validator-judge fail-open activity sub-section for the cron
# status-report digest. Parallels `render_focus_rotation_activity_section`
# above — same omit-on-empty rule, same `state_extras` wiring shape — but
# scopes counts to a rolling 24h window (matching TB-243's pull-surface
# window so the operator never has to reconcile two different validator-
# judge counts between pull and push surfaces).
#
# Closes TB-243's push-surface gap on axis 1: the operator's primary
# walk-away surface (the 2h status-report Mattermost post) was silent
# on `validator_judge_fail` / `validator_judge_timeout`, which weakens
# goal.md L82-85 ("upstream gates already make this safe in practice")
# because the TB-235 dep-coherence judge IS one of those upstream gates
# and a fail-open gate without push-channel observability is
# functionally invisible during the walk-away window goal.md L57-59
# promises ("walk away for a week without intervention"). A judge
# silently degrading at 03:00Z used to wait for the operator's next
# manual `ap2 status` to surface; now it lands in the very next 2h
# cron post.

_VALIDATOR_JUDGE_HEADING = "*Validator-judge fail-open window (24h):*"


def render_validator_judge_activity_section(
    state: dict,
) -> list[str]:
    """Return the Markdown lines for the `*Validator-judge fail-open
    window (24h):*` sub-section the cron agent forwards verbatim into
    the Mattermost post, or `[]` when the sub-section should be
    omitted entirely (no validator-judge events in the rolling 24h
    window).

    `state` is the dict returned by
    `automation_status.collect_window_validator_judge` — pre-computed
    by the caller so the renderer stays pure / no I/O and tests can
    drive it with fabricated state dicts without spinning up a real
    events file (parallel to TB-238's dry-run sub-block pattern).

    Shape (when rendered, `[noisy]` suffix conditional on
    `state["is_noisy"]`):

        *Validator-judge fail-open window (24h):* [noisy]
        - validator_judge_fail: N
        - validator_judge_timeout: M

    Both per-event-type lines are always emitted when `total > 0`
    (even when one of the two counts is zero) so the operator scanning
    the digest sees the same two-row shape every post — symmetric to
    TB-243's pull-surface text line which always names both counts
    when either is non-zero.

    Omit-on-empty: returns `[]` when `state["total"] == 0`. This is
    the load-bearing default-off byte-identical regression pin —
    operators today get zero validator-judge output and most days that
    should continue (a healthy judge has 0/0 counts and the digest
    stays untouched). Returns `list[str]` (not `str`) so the caller
    can join with `"\\n"` or extend its own `state_extras` list
    directly; the wiring in `run_status_report` joins and appends as
    one block so the section reads as a unit.
    """
    if state.get("total", 0) == 0:
        return []

    header = _VALIDATOR_JUDGE_HEADING
    if state.get("is_noisy"):
        # `[noisy]` badge mirrors TB-243's pull-side CLI text suffix
        # (` [noisy]` appended to the `validator-judge: ...` sub-line
        # when the threshold trips). Both surfaces light up in
        # lockstep when the operator tunes
        # `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD`.
        header = f"{header} [noisy]"

    return [
        header,
        f"- validator_judge_fail: {state['validator_judge_fail_count']}",
        f"- validator_judge_timeout: {state['validator_judge_timeout_count']}",
    ]


# ---------------------------------------------------------------------------
# TB-258: Retrospective-audit unreviewed-count sub-section for the cron
# status-report digest. Parallels `render_validator_judge_activity_section`
# above — same omit-on-empty rule, same `state_extras` wiring shape — but
# scopes the count to the operator_log.md-derived "unreviewed since last
# `ran audit (...)` cursor" pile (no rolling-window math; the cursor IS
# the boundary).
#
# Closes TB-248's push-surface gap: the operator's primary walk-away
# channel (the 2h status-report Mattermost post) carried no audit-pile
# digest. Under `AP2_AUTO_APPROVE=1` the auto-approved tasks ship
# without operator-in-the-loop review at dispatch time; retrospective
# review is the operator's only judgment surface. Without this digest,
# the walk-away operator's first sighting of an unreviewed-task pile
# was one manual `ap2 audit` invocation later than the system already
# knew it was true — weakening the goal.md L28-30 done-when bullet
# "walk away for a week without intervention".

_AUDIT_STATE_HEADING = "*Retrospective audit (unreviewed shipped):*"


def render_audit_state_section(
    state: dict,
) -> list[str]:
    """Return the Markdown lines for the `*Retrospective audit
    (unreviewed shipped):*` sub-section the cron agent forwards
    verbatim into the Mattermost post, or `[]` when the sub-section
    should be omitted entirely (zero unreviewed tasks).

    `state` is the dict returned by
    `automation_status.collect_audit_state` — pre-computed by the
    caller so the renderer stays pure / no I/O and tests can drive it
    with fabricated state dicts without spinning up a real operator
    log (parallel to TB-245's validator-judge sub-block pattern).

    Shape (when rendered):

        *Retrospective audit (unreviewed shipped):*
        - <N> unreviewed since <cursor-ts> — run `ap2 audit` to walk

    Omit-on-empty: returns `[]` when `state["unreviewed_count"] == 0`.
    This is the load-bearing default-off byte-identical regression pin
    — operators today get zero audit output on quiet/fully-reviewed
    windows and that should continue (a fresh project with no shipped
    tasks OR a project where every shipped task has been classified /
    audit-skipped / rejected has zero pending audit). Returns
    `list[str]` (not `str`) so the caller can extend its own
    `state_extras` list directly; the wiring in `run_status_report`
    joins and appends as one block so the section reads as a unit.

    Cursor-ts rendering: when `cursor_ts` is None (no prior
    `ran audit (...)` line ever written — first-ever audit) the label
    renders as `(epoch)` so the operator sees a stable two-token shape
    regardless of audit history; mirrors the CLI text branch.
    """
    if state.get("unreviewed_count", 0) <= 0:
        return []

    cursor_display = state.get("cursor_ts") or "(epoch)"
    return [
        _AUDIT_STATE_HEADING,
        f"- {state['unreviewed_count']} unreviewed since "
        f"{cursor_display} — run `ap2 audit` to walk",
    ]


# ---------------------------------------------------------------------------
# TB-259: Stats window aggregates sub-section for the cron status-report
# digest. Parallels `render_audit_state_section` above — same omit-on-empty
# rule, same `state_extras` wiring shape, same verbatim-forwarding contract.
#
# Closes the push-vs-pull surface-parity gap that TB-255 left open. TB-255
# shipped the `/stats` HTML + `/stats.json` PULL surface for task / bullet /
# ideation timing + turn + attempt aggregates over events.jsonl (helper:
# `automation_stats.collect_stats(cfg, window_s=...)`). But the cron
# status-report digest — the operator's primary walk-away PUSH channel —
# carried no top-line aggregates summary. The dashboard pays rent only
# during active operator sessions, not during the walk-away promise
# (goal.md L28-30: "walk away for a week without intervention") the
# Current focus is built around. This wraps the existing collector and
# renders one Markdown sub-block the agent forwards verbatim into the
# Mattermost post; same axis-parity shape TB-241 / TB-242 / TB-244 /
# TB-245 closed on their axes.
#
# Omit-on-empty rule (no zero-noise on quiet windows): the section renders
# ONLY when the window's task-completion count is non-zero. A 24h-quiet
# project that completed nothing has nothing to summarize — and the
# `/stats` pull surface still renders the full zero-state dashboard for
# operators who load it directly.

_STATS_WINDOW_HEADING_FMT = "*Stats window aggregates ({window}):*"

# TB-259: window fallback when no parseable previous-report ts exists
# (first-ever status-report run, or the prior one rolled out of the
# tail). 24h matches the operator's natural-cadence return rhythm and
# stays inside `automation_stats.MIN_WINDOW_S` / `MAX_WINDOW_S` bounds.
_DEFAULT_STATS_WINDOW_S = 86400


def render_stats_window_section(stats: dict) -> list[str]:
    """Return the Markdown lines for the `*Stats window aggregates
    (<window>):*` sub-section the cron agent forwards verbatim into
    the Mattermost post, or `[]` when the sub-section should be
    omitted entirely (zero task completions in window).

    `stats` is the dict returned by
    `automation_stats.collect_stats(cfg, window_s=...)` — pre-computed
    by the caller so the renderer stays pure / no I/O and tests can
    drive it with fabricated state dicts without spinning up a real
    events file (parallel to TB-245's validator-judge / TB-258's
    audit-state sub-block patterns).

    Shape (when rendered, 3-5 lines):

        *Stats window aggregates (7d):*
        - tasks: 12 completed (p50 240s, p95 1800s)
        - ideation: 4 cycles, 8 proposals
        - bullet judges: 35 evaluations, 1 fail-open

    Omit-on-empty: returns `[]` when
    `stats["tasks"]["complete_count"] <= 0`. Load-bearing default-off
    byte-identical regression pin — quiet windows stay byte-identical
    to the pre-TB-259 digest baseline so the prior axis-parity tests
    (TB-228 / TB-244 / TB-245 / TB-258) continue to pass when the
    `collect_stats` window has nothing to summarize. Returns
    `list[str]` (not `str`) so the caller can extend its own
    `state_extras` list directly; the wiring in `run_status_report`
    joins and appends as one block so the section reads as a unit.

    The `bullet judges: <N> evaluations, <M> fail-open` line counts
    `judge_call_count` (per-bullet prose-judge invocations) and the
    sum of `validator_judge_fail_count + validator_judge_timeout_count`
    (TB-235's fail-open audit events). Both fields are in the
    existing `collect_stats` shape — no new aggregates were added
    (Out-of-scope per briefing).
    """
    tasks = stats.get("tasks") or {}
    if int(tasks.get("complete_count") or 0) <= 0:
        return []

    window_label = stats.get("window") or "?"
    header = _STATS_WINDOW_HEADING_FMT.format(window=window_label)

    duration = tasks.get("duration_s") or {}
    p50 = float(duration.get("p50") or 0.0)
    p95 = float(duration.get("p95") or 0.0)
    tasks_line = (
        f"- tasks: {int(tasks.get('complete_count') or 0)} completed "
        f"(p50 {p50:.0f}s, p95 {p95:.0f}s)"
    )

    ideation = stats.get("ideation") or {}
    ideation_line = (
        f"- ideation: {int(ideation.get('cycle_count') or 0)} cycles, "
        f"{int(ideation.get('proposals_recorded') or 0)} proposals"
    )

    verifier = stats.get("verifier") or {}
    judge_count = int(verifier.get("judge_call_count") or 0)
    fail_count = (
        int(verifier.get("validator_judge_fail_count") or 0)
        + int(verifier.get("validator_judge_timeout_count") or 0)
    )
    bullet_line = (
        f"- bullet judges: {judge_count} evaluations, "
        f"{fail_count} fail-open"
    )

    return [header, tasks_line, ideation_line, bullet_line]


# ---------------------------------------------------------------------------
# TB-260: stale-env sub-section for the cron status-report digest. Parallels
# `render_audit_state_section` above — same omit-on-empty rule, same
# `state_extras` wiring shape, same verbatim-forwarding contract.
#
# Closes the operator-surface gap that bit TB-255: an `AP2_VERIFY_TIMEOUT_S`
# bump in `.cc-autopilot/env` is silently ignored until daemon restart;
# without this digest line, the operator who walked away after editing
# the file has no push-channel reminder that a restart is needed before
# the bump takes effect. The cron digest's once-per-cycle surface
# matches the briefing's debounce-by-design intent (per-detection
# direct-mention notifications are explicitly out of scope).

_ENV_STALENESS_HEADING = "*Daemon env file stale (restart required):*"


def render_env_staleness_section(state: dict) -> list[str]:
    """Return the Markdown lines for the `*Daemon env file stale
    (restart required):*` sub-section the cron agent forwards verbatim
    into the Mattermost post, or `[]` when the env file isn't stale.

    `state` is the dict returned by
    `automation_status.collect_env_staleness` — pre-computed by the
    caller so the renderer stays pure / no I/O and tests can drive it
    with fabricated state dicts without spinning up a real daemon
    state file (parallel to TB-245's validator-judge / TB-258's
    audit-state sub-block patterns).

    Shape (when rendered):

        *Daemon env file stale (restart required):*
        - .cc-autopilot/env modified at <iso-ts> (after daemon start at <iso-ts>) — run `ap2 stop && ap2 start` to apply changes

    Omit-on-empty: returns `[]` when `state["env_stale"]` is False
    (default-off byte-identical regression pin — pre-TB-260 digests
    carry zero env-staleness output and that stays the steady-state
    happy path). Returns `list[str]` (not `str`) so the caller can
    extend its own `state_extras` list directly; the wiring in
    `run_status_report` joins and appends as one block so the section
    reads as a unit.
    """
    if not state.get("env_stale"):
        return []
    return [
        _ENV_STALENESS_HEADING,
        f"- .cc-autopilot/env modified at {state['env_file_mtime']} "
        f"(after daemon start at {state['env_file_mtime_at_start']}) — "
        f"run `ap2 stop && ap2 start` to apply changes",
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
- TB-173 / TB-191: if the snapshot's `## Current state` block
  carries an `- Decisions needed from operator (N): ...` line, copy
  that line VERBATIM as one of your bullets too. The ideator
  surfaces this section when there is an actionable decision the
  operator must engage with — focus-rotation calls, residual-risk
  acceptances awaiting sign-off, escalations — operator-judgement
  work that needs visibility on the report. If the line is absent,
  omit the bullet — there's nothing to surface.
- TB-182: BEFORE you forward the decisions-needed line (or any
  TB-N reference its bullets carry) into the post, validate
  against events.jsonl that the references are still current. The
  bullets were written by the ideator at the most recent
  `ideation_state_updated` event in the tail; up to the ideation
  interval (~2h) of staleness can bleed through into the
  decisions-needed snapshot. Procedure:
    1. Note the `ts` of the most recent `ideation_state_updated`
       event in `events.jsonl`. That's when the decisions-needed
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
- TB-228: if the snapshot's `## Current state` block carries a
  `## Automation loop activity` section (heading + headline +
  bullets summarizing auto-approve / auto-unfreeze counts since
  the last report), copy that entire section VERBATIM into your
  post (preserve the heading, the `auto-approve:` / `auto-unfreeze:`
  headline line, and every bullet). The daemon already aggregated
  the counts and rendered the markdown — do NOT recompute,
  paraphrase, or drop bullets. Position the section AFTER your
  bullet list (it's its own section with its own heading) so the
  walk-away operator scanning the post sees the digest as a
  distinct block. Absent ⇒ pre-opt-in project / quiet window ⇒
  omit (the daemon renders nothing in that case).
- TB-244: if the snapshot's `## Current state` block carries a
  `## Focus rotation activity` section (heading + one bullet per
  `focus_advanced` / `roadmap_complete` event landed in the
  inter-report window), copy that entire section VERBATIM into
  your post (preserve the heading and every bullet). Same
  verbatim-forwarding contract as TB-228's automation digest — the
  daemon owns the rendering; do NOT recompute, paraphrase, or drop
  bullets. Position the section AFTER the automation digest (or
  AFTER your bullet list when the automation digest is absent) so
  the walk-away operator sees both axis-1/2/3 and axis-4 activity
  as distinct blocks. Absent ⇒ no rotation activity in the window
  ⇒ omit.
- TB-245: if the snapshot's `## Current state` block carries a
  `*Validator-judge fail-open window (24h):*` sub-block (italicized
  header — possibly carrying a ` [noisy]` suffix — plus two
  bullets for the rolling 24h counts of `validator_judge_fail` and
  `validator_judge_timeout`), copy that entire sub-block VERBATIM
  into your post (preserve the header AND both bullets, including
  the `[noisy]` suffix on the header when present). Same
  verbatim-forwarding contract as TB-228 / TB-244 — the daemon
  owns the rendering; do NOT recompute, paraphrase, or drop
  bullets. Position the sub-block AFTER the focus-rotation
  section (or AFTER the automation digest when focus-rotation is
  absent, or AFTER your bullet list when both are absent) so the
  axis-1 safety-net signal lands at the bottom of the digest —
  mirrors TB-243's web home placement of the validator-judge row
  at the bottom of the automation card. Absent ⇒ 0/0 fail-open
  counts in the last 24h ⇒ omit.
- TB-258: if the snapshot's `## Current state` block carries a
  `*Retrospective audit (unreviewed shipped):*` sub-block
  (italicized header + one bullet naming the unreviewed-shipped
  count + cursor timestamp + `ap2 audit` nudge), copy that entire
  sub-block VERBATIM into your post (preserve the header and the
  bullet). Same verbatim-forwarding contract as TB-228 / TB-244 /
  TB-245 — the daemon owns the rendering; do NOT recompute,
  paraphrase, or drop bullets. Position the sub-block AFTER the
  validator-judge sub-block (or AFTER whichever digest section
  ends the body when validator-judge is absent) so the explicit
  "you have N to review" operator-decision nudge lands at the
  bottom of the digest — the natural call-to-action position.
  Absent ⇒ 0 unreviewed shipped tasks (fully-reviewed / fresh
  project) ⇒ omit.
- TB-259: if the snapshot's `## Current state` block carries a
  `*Stats window aggregates (<window>):*` sub-block (italicized
  header naming the inter-report window + 3 bullets summarizing
  task completions with p50/p95 duration, ideation cycles +
  proposals, and bullet-judge evaluations + fail-open count over
  the same window), copy that entire sub-block VERBATIM into
  your post (preserve the header and every bullet). Same
  verbatim-forwarding contract as TB-228 / TB-244 / TB-245 /
  TB-258 — the daemon owns the rendering; do NOT recompute,
  paraphrase, or drop bullets. Position the sub-block AFTER the
  audit sub-block (or AFTER whichever digest section ends the
  body when audit is absent) so the "what happened since last
  report" top-line glance lands at the bottom of the digest —
  parallels the `/stats` pull-surface aggregates the operator
  opens on-demand. Absent ⇒ 0 task completions in window (quiet
  window / fresh project) ⇒ omit.

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

# TB-228: positive-allowlist anchor for the digest's "MUST NOT skip"
# clause. The boring-types frozenset above is the structural gate
# (anything outside it is interesting), but the briefing's contract
# explicitly names these as triggering events — surface them here so a
# refactor that flips the gate to allowlist-only still treats automation-
# loop activity as interesting. The set is referenced from
# `_status_report_should_skip`'s docstring and from the TB-228 tests
# (`test_should_skip_false_when_auto_approve_paused_in_window`).
#
# TB-244: extended with axis-4 focus-rotation events (`focus_advanced`,
# `roadmap_complete`) so the push surface (status-report cron post)
# carries the same operator-attention signal that TB-242 added to the
# pull surfaces (`ap2 status` text/JSON + web home). The
# `roadmap_complete` halt is especially load-bearing — the operator's
# walk-away time on a roadmap-exhaustion bounded by the manual `ap2
# status` cadence contradicts axis 4's own framing
# ("walk-away time scales with the operator-declared roadmap length",
# goal.md L137-138).
#
# TB-245: extended with axis-1 validator-judge fail-open events
# (`validator_judge_fail`, `validator_judge_timeout`) so a fresh
# fail-open event on the TB-235 dep-coherence judge un-skips the
# status-report digest and surfaces on the operator's primary
# walk-away channel within 2h. Without this, the silent-degradation
# hazard TB-235's fail-open design carries (judge skips a briefing
# on a transient API hiccup; briefing is admitted regardless) had
# zero push-channel observability — directly weakened the goal.md
# L82-85 auto-approve safety claim ("upstream gates already make
# this safe in practice"), because the dep-coherence judge IS one
# of those upstream gates and an invisible gate is a missing gate.
_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES = frozenset({
    "auto_approve_paused",
    "auto_approve_halted",
    "auto_unfreeze_applied",
    "auto_unfreeze_skipped",
    "auto_approved",
    # TB-244: axis-4 focus-rotation events.
    "focus_advanced",
    "roadmap_complete",
    # TB-245: axis-1 validator-judge fail-open events.
    "validator_judge_fail",
    "validator_judge_timeout",
})


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

    TB-228: automation-loop events (`auto_approve_paused`,
    `auto_approve_halted`, `auto_unfreeze_applied`,
    `auto_unfreeze_skipped`, `auto_approved`) count as interesting —
    they're listed in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`
    and fall through the boring-types denylist below. An operator
    walking away should see the digest the moment a halt or fix
    landed, even if no other board state changed.

    TB-244: axis-4 focus-rotation events (`focus_advanced`,
    `roadmap_complete`) are also surfaced in
    `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` — a focus advance
    or roadmap-complete halt landing in the window must keep the
    report from skipping, so the operator's primary push channel
    carries the rotation-state change without waiting on the next
    `task_complete` or other automation-loop event.

    TB-245: axis-1 validator-judge fail-open events
    (`validator_judge_fail`, `validator_judge_timeout`) are also
    treated as interesting — a fresh fail-open landing in the
    window must keep the report from skipping so the operator's
    primary push channel surfaces silent-degradation of the TB-235
    dep-coherence judge without waiting on a manual `ap2 status`.
    Parallel push-surface closure to TB-244 on the validator-judge
    axis (TB-243 shipped the pull surfaces last cycle).
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
    # TB-173 / TB-191: surface the ideator's `## Decisions needed from
    # operator` section so the cron status-report carries the same
    # escalation signal as the CLI / web home (single source of truth
    # via `parse_operator_decisions`). Bullets joined with `; ` so the
    # line mirrors the CLI text rendering — the agent then forwards
    # the line verbatim into the Mattermost post per the prompt's
    # contract below. When the file or section is absent / empty the
    # helper returns [] and we skip the line entirely so a clean
    # board doesn't grow a noisy "0 decisions needed" bullet. The
    # agent-internal `## Cycle observations` section (TB-191) is
    # structurally excluded by the parser — it never reaches this
    # surface and therefore never reaches the Mattermost post.
    from .ideation import parse_operator_decisions

    operator_decisions = parse_operator_decisions(
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if operator_decisions:
        state_extras.append(
            f"- Decisions needed from operator ({len(operator_decisions)}): "
            + "; ".join(operator_decisions)
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
    # operator's configured channel. The pre-fix prompt also carried a
    # `#autopilot` fallback string for the unset case, which was a
    # dead letter — no `#autopilot` channel exists on the server, so
    # the agent's "fallback" still resolved to town-square. The fix
    # moves resolution to the daemon: explicit `AP2_MM_REPORT_CHANNEL`
    # wins; otherwise fall back to the first entry of `AP2_MM_CHANNELS`
    # (the inbound-watch channel is the natural place to send outbound
    # status posts in single-channel projects). When neither is set we
    # omit the line entirely — the prompt body then routes the agent
    # into the explicit-skip branch with a `log_event` audit so the
    # operator can grep events.jsonl for the configuration miss. The
    # `#autopilot` literal is retained in this comment as a regression
    # anchor: the prompt body must NEVER carry it (see the
    # `test_status_report_prompt_drops_dead_letter_autopilot_fallback`
    # regression pin), but referencing it here documents the historical
    # bug shape for future readers and keeps the verification grep
    # honest.
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
    # TB-228: render the Automation loop activity digest section into
    # `state_extras` so the agent forwards it verbatim. The section's
    # window scopes to "since the previous `cron_complete name=status-
    # report` event"; on first-ever run (or when the previous report
    # rolled out of the tail) we count from the start of the tail.
    # The renderer returns "" when the operator hasn't opted in AND
    # nothing of interest fired, so a pre-opt-in project stays clean.
    activity_tail = (
        events.tail(cfg.events_file, 2000)
        if cfg.events_file.exists() else []
    )
    since_idx = automation_status.find_previous_status_report_idx(
        activity_tail,
    )
    automation_section = render_automation_loop_activity_section(
        cfg, since_event_idx=since_idx, tail=activity_tail,
    )
    if automation_section:
        state_extras.append(automation_section)
    # TB-244: render the axis-4 focus-rotation sub-block (parallel to
    # the TB-228 automation digest above). Same `since_idx` scoping so
    # both surfaces share one inter-report window; the renderer returns
    # "" when no `focus_advanced` / `roadmap_complete` events landed in
    # the window. Wiring lives here so axis-1/2/3 digest tests stay
    # byte-identical when axis-4 is quiet — the briefing's option B
    # (parallel renderer, not in-place extension).
    focus_rotation_section = render_focus_rotation_activity_section(
        cfg, since_event_idx=since_idx, tail=activity_tail,
    )
    if focus_rotation_section:
        state_extras.append(focus_rotation_section)
    # TB-245: render the axis-1 validator-judge fail-open sub-block
    # (parallel to the TB-244 focus-rotation digest above). Window is
    # rolling 24h to match TB-243's pull-surface so operator never
    # reconciles two counts between `ap2 status` and the cron post.
    # Placed AFTER the focus-rotation block so the digest reads top-
    # down as "automation activity → focus rotation → validator-judge
    # fail-open" (axis-1 safety net rendered last so the eye lands on
    # it; mirrors TB-243's web home placement of the validator-judge
    # row at the bottom of the automation card). Renderer returns []
    # when both 24h counts are zero — quiet windows stay byte-identical
    # to the pre-TB-245 baseline.
    validator_judge_state = automation_status.collect_window_validator_judge(
        cfg,
    )
    validator_judge_lines = render_validator_judge_activity_section(
        validator_judge_state,
    )
    if validator_judge_lines:
        state_extras.append("\n".join(validator_judge_lines))
    # TB-258: render the retrospective-audit unreviewed-count sub-block
    # (parallel to the TB-245 validator-judge digest above). Pure
    # read-layer composition over `audit.list_unreviewed` +
    # `audit.parse_audit_cursor` via `collect_audit_state`. The
    # underlying boundary is the operator_log.md `ran audit (...)`
    # cursor — count is window-INDEPENDENT (always the full unreviewed
    # pile, not just since-last-report) so an audit pile that
    # accumulated over a multi-day silence still surfaces on every
    # report until the operator clears it. Placed AFTER the
    # validator-judge sub-block so the digest reads top-down as
    # "automation activity → focus rotation → validator-judge fail-open
    # → retrospective audit" (operator-decision queue rendered last so
    # the eye lands on it — the explicit "you have N to review" nudge
    # is the digest's natural call-to-action). Renderer returns []
    # when the unreviewed-count is zero — fully-reviewed / fresh
    # projects stay byte-identical to the pre-TB-258 baseline.
    audit_state = automation_status.collect_audit_state(cfg)
    audit_lines = render_audit_state_section(audit_state)
    if audit_lines:
        state_extras.append("\n".join(audit_lines))
    # TB-259: render the `*Stats window aggregates (<window>):*`
    # sub-block (parallel to the TB-258 audit sub-block above). Pure
    # read-layer composition over the existing-in-HEAD
    # `automation_stats.collect_stats` helper TB-255 built for the
    # `/stats` HTML + `/stats.json` PULL surface; this push wiring
    # reuses the same aggregates (no new collect_stats fields). Window
    # is scoped to "now - last status-report cron_complete ts" so the
    # digest matches the inter-report window the TB-228 / TB-244 /
    # TB-245 / TB-258 sub-blocks above scope against; falls back to
    # 24h when no prior report ts is parseable (first-ever run, or
    # the previous one rolled out of the tail). Renderer returns []
    # when the window's task-completion count is zero — quiet
    # windows stay byte-identical to the pre-TB-259 baseline.
    stats_window_s = _DEFAULT_STATS_WINDOW_S
    if since_idx >= 0 and since_idx < len(activity_tail):
        last_ts_raw = activity_tail[since_idx].get("ts")
        if isinstance(last_ts_raw, str) and last_ts_raw:
            try:
                last_dt = _dt.datetime.strptime(
                    last_ts_raw, "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=_dt.timezone.utc)
                delta_s = (
                    _dt.datetime.now(_dt.timezone.utc) - last_dt
                ).total_seconds()
                # Floor at `automation_stats.MIN_WINDOW_S` (1h) so a
                # back-to-back-second report doesn't compute a
                # zero-width window that excludes every event by
                # `>= start_dt` arithmetic — mirrors `parse_window`'s
                # same-named clamp on the pull surface so push and pull
                # windows align at the edge case.
                if delta_s >= automation_stats.MIN_WINDOW_S:
                    stats_window_s = int(delta_s)
                elif delta_s > 0:
                    stats_window_s = automation_stats.MIN_WINDOW_S
            except (ValueError, TypeError):
                pass
    stats = automation_stats.collect_stats(cfg, window_s=stats_window_s)
    stats_lines = render_stats_window_section(stats)
    if stats_lines:
        state_extras.append("\n".join(stats_lines))
    # TB-260: render the stale-env sub-block (parallel to the TB-259
    # stats-window sub-block above). Reads `daemon_state.json`'s
    # `env_file_mtime_at_start` stash via `collect_env_staleness` and
    # compares against the live `.cc-autopilot/env` mtime. Placed
    # AFTER the stats sub-block so the digest reads top-down as
    # "automation activity → focus rotation → validator-judge fail-open
    # → retrospective audit → stats window → daemon env staleness"
    # (operator-restart nudge rendered last so the eye lands on it —
    # the digest's natural call-to-action when active; mirrors
    # TB-258's audit sub-block placement rationale). Renderer returns
    # [] when not stale — healthy daemons stay byte-identical to the
    # pre-TB-260 baseline.
    env_staleness = automation_status.collect_env_staleness(cfg)
    env_staleness_lines = render_env_staleness_section(env_staleness)
    if env_staleness_lines:
        state_extras.append("\n".join(env_staleness_lines))
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
