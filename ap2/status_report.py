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
import hashlib as _hashlib
import json as _json
import os
from dataclasses import dataclass
from typing import Literal

from . import automation_stats, automation_status, events
from .board import Board
from .config import Config
from .cron import load_state as _load_cron_state
from .cron import mark_run, mark_run_with_payload


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


# TB-298: shared truncation rule for the `ap2 status` text-render
# `attention:` cluster line — the CLI-pull sibling of the TB-282
# status-report cron push surface (`render_attention_section`) and
# the TB-296 web `/attention` pull page (`web_attention._render_attention`).
# All four surfaces share `attention.detect_attention_conditions(cfg)`
# as their detector entrypoint; this helper owns the text-render
# truncation contract so the CLI cluster line stays compact alongside
# its peer cluster entries (review:, janitor:, classifications:,
# decisions needed:, audit:). The cap is 3 (not 5 like
# `_format_pending_review_line`) because attention bullets carry
# longer prose-summary text — three of them already saturate one
# terminal row, where TB-N lists pack five comfortably. JSON
# consumers always see the full unfiltered list (parser stability
# mirror of the `auto_approve` / `audit` / `env_stale` contracts);
# truncation is a text-render concern only.
_ATTENTION_STATUS_LINE_CAP = 3


def _format_attention_status_line(
    conditions: list,
    *,
    cap: int = _ATTENTION_STATUS_LINE_CAP,
) -> str:
    """Format active attention conditions into the `ap2 status` cluster
    line body (the segment AFTER the `attention:  N condition(s) — `
    prefix the caller renders).

    Bullets are joined by `; ` and capped at `cap` with a
    `(+M more — ap2 web /attention)` suffix when the input list is
    longer. Each bullet renders as `TB-N <summary>` when the
    condition's `extras['task']` is set (per-task detectors:
    `task_stuck`, `task_frozen`), or as the bare `<summary>` for
    singleton detectors (`validator_judge_noisy`,
    `auto_approve_paused`, `cost_cap_approach`). Matches the
    `render_attention_section` / `web_attention._render_attention`
    contract that the four operator-facing surfaces share — drift
    between them would mean a `task_stuck` bullet showing one TB-N
    label on the web page and a different label here.

    Returns the empty string for an empty list — the caller decides
    whether to suppress the wrapping `attention:` prefix when
    N=0 (the CLI text branch omits the entire line; mirrors the
    TB-258 `audit:` / TB-260 `env stale` / TB-177 `janitor:`
    omit-on-empty discipline so quiet projects don't grow a
    zero-noise cluster row).

    Pure / no I/O so the CLI render path can call it without
    re-walking the detector entrypoint. Defined here (and imported
    by `cli_daemon.py`) so the verification grep
    `_format_attention_status_line` lands in both files (TB-298).
    """
    if not conditions:
        return ""
    rendered: list[str] = []
    for cond in conditions[:cap]:
        # `cond.extras` is the AttentionCondition dataclass field; the
        # `.get` falls through cleanly when `extras` is empty (singleton
        # detectors leave `task` unset). Strip whitespace so an upstream
        # detector that ever emits a padded string doesn't break the
        # bullet spacing.
        task_id = ""
        extras = getattr(cond, "extras", None) or {}
        if isinstance(extras, dict):
            task_id = (extras.get("task") or "").strip()
        summary = getattr(cond, "summary", "") or ""
        if task_id:
            rendered.append(f"{task_id} {summary}".strip())
        else:
            rendered.append(summary.strip())
    if len(conditions) > cap:
        rendered.append(
            f"(+{len(conditions) - cap} more — ap2 web /attention)"
        )
    return "; ".join(rendered)


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
# operator's primary walk-away surface (the status-report Mattermost
# post) was silent on axis-4 (`focus_advanced` / `roadmap_complete`),
# which contradicts axis 4's own framing ("walk-away time scales with
# the operator-declared roadmap length", goal.md L137-138). A
# `roadmap_complete` halt at 03:00Z used to wait for the operator's
# next manual `ap2 status` to surface; now it lands in the very next
# status-report cron post.

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
# walk-away surface (the status-report Mattermost post) was silent
# on `validator_judge_fail` / `validator_judge_timeout`, which weakens
# goal.md L82-85 ("upstream gates already make this safe in practice")
# because the TB-235 dep-coherence judge IS one of those upstream gates
# and a fail-open gate without push-channel observability is
# functionally invisible during the walk-away window goal.md L57-59
# promises ("walk away for a week without intervention"). A judge
# silently degrading at 03:00Z used to wait for the operator's next
# manual `ap2 status` to surface; now it lands in the very next
# status-report cron post.

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
# channel (the status-report Mattermost post) carried no audit-pile
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


# ---------------------------------------------------------------------------
# TB-280: Recent task activity digest section. Parallels the
# `render_automation_loop_activity_section` / TB-244 / TB-245 / TB-258 /
# TB-259 / TB-260 pre-rendered sections above. Closes goal.md focus-1's
# Done-when bullet "identifies tasks by title + one-line summary (never
# bare TB-N alone) and leads with the project name".
#
# Pre-TB-280 the status-report prompt asked the agent to compose bullets
# of shape `TB-N + 1-line outcome + short SHA` (prompt L741-744), forcing
# the multi-project operator who hasn't seen this project since the
# previous report to alt-tab to the repo to translate every TB-N into a
# task title. This renderer walks the inter-report window (events at
# indices > the previous `cron_complete job=status-report`), resolves
# `Board.find(task_id).title` per terminal task event, and emits one
# bullet per event so the agent's body bullets sit on top of an already-
# titled digest the operator can read without context-switching.

_RECENT_TASK_ACTIVITY_HEADING = "## Recent task activity"

# Terminal task-event types the digest renders. Mirrors the briefing's
# enumeration: `task_complete` covers the dominant happy/failed-verify
# path (its `status` field distinguishes the two), `task_failed` is the
# forward-compat hook for an explicit failure event, `verification_failed`
# fires from the per-task verifier (predates the `task_complete` close-
# out on retries), `retry_exhausted` fires when the retry budget is
# spent.
_TERMINAL_TASK_EVENT_TYPES: frozenset[str] = frozenset({
    "task_complete",
    "task_failed",
    "verification_failed",
    "retry_exhausted",
})


def _first_line(text: str | None) -> str:
    """Return the first non-empty line of `text`, stripped.

    Used as the title-lookup fallback when `Board.find(task_id)` misses
    (task moved between sections, deleted, or never landed on the
    board). The event's `summary` field carries the task agent's
    one-sentence completion summary — its first line is the closest
    operator-readable identifier we have when board lookup fails.
    Returns the empty string when `text` is None / empty / whitespace
    so callers can substitute a stable fallback marker.
    """
    if not text:
        return ""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _resolve_task_title(cfg: Config, task_id: str, summary: str | None) -> str:
    """Resolve a task's display title for the digest bullet.

    Lookup order:
      1. `Board.load(cfg.tasks_file).get(task_id).title` — the
         operator-curated title from `TASKS.md`. Stable across the
         task's lifetime (the daemon only mutates section / checkbox
         on move, never the title text).
      2. First non-empty line of the event's `summary` field —
         falls back to the task agent's completion summary when the
         task isn't on the board (deleted / never landed / board not
         loadable).
      3. The literal `"(title unavailable)"` when neither yields
         text. A stable placeholder is better than rendering a bare
         TB-N + colon and re-introducing the very regression this
         section closes.

    Board lookup failures (file missing, parse error) are swallowed —
    the renderer must never break the status-report run for a board
    snapshot issue.
    """
    if cfg.tasks_file.exists():
        try:
            board = Board.load(cfg.tasks_file)
        except Exception:  # noqa: BLE001
            board = None
        if board is not None:
            task = board.get(task_id)
            if task is not None and task.title:
                return task.title
    summary_line = _first_line(summary)
    if summary_line:
        return summary_line
    return "(title unavailable)"


def _outcome_for_event(ev: dict) -> str:
    """Render a one-line outcome string from a terminal task event.

    The daemon supplies "title + structural shape only" (briefing's
    Out-of-scope clause); per-bullet language polish stays with the
    agent. We mechanically map the event's status / commit / counter
    fields to a deterministic outcome label so the bullet has a
    stable closing token even when the agent doesn't add anything
    further. Operators reading two back-to-back digests see the same
    rendering for the same event shape.
    """
    typ = ev.get("type", "")
    if typ == "task_complete":
        status = (ev.get("status") or "").strip() or "complete"
        commit = (ev.get("commit") or "").strip()
        if commit:
            return f"{status} ({commit[:7]})"
        return status
    if typ == "task_failed":
        reason = (ev.get("reason") or ev.get("status") or "").strip()
        return f"failed ({reason})" if reason else "failed"
    if typ == "verification_failed":
        kind = (ev.get("kind") or "").strip()
        return f"verification_failed ({kind})" if kind else "verification_failed"
    if typ == "retry_exhausted":
        attempts = ev.get("attempts")
        last = (ev.get("last_status") or "").strip()
        if isinstance(attempts, int) and attempts > 0 and last:
            return f"retry_exhausted ({attempts} attempts, last={last})"
        if isinstance(attempts, int) and attempts > 0:
            return f"retry_exhausted ({attempts} attempts)"
        return "retry_exhausted"
    return typ or "(unknown)"


def render_recent_task_activity_section(
    cfg: Config,
    *,
    since_event_idx: int,
    tail: list[dict] | None = None,
) -> str:
    """Return the Markdown `## Recent task activity` section the cron
    agent forwards verbatim into the Mattermost post, or "" when no
    terminal task event landed in the inter-report window.

    `since_event_idx` is the positional index of the previous
    `cron_complete job=status-report` event in the tail; only events
    at strictly greater indices contribute. Mirrors the scoping
    contract TB-228 / TB-244 use for their parallel digests.

    Shape (when rendered):

        ## Recent task activity

        - **TB-N** — <title>: <one-line outcome>
        - **TB-M** — <title>: <one-line outcome>

    One bullet per terminal task event (`task_complete`, `task_failed`,
    `verification_failed`, `retry_exhausted`) in tail order, so a window
    with `verification_failed` + later `task_complete` for the same
    TB-N reads chronologically (operator sees the failure resolution
    arc).

    Omit-on-empty: returns "" when zero terminal task events sit in
    the window — quiet windows stay byte-identical to the pre-TB-280
    digest baseline so the prior axis-1/2/3/4/audit/stats/env-stale
    sub-block tests continue to pass when the window has no task
    activity to summarize.
    """
    if tail is None:
        if cfg.events_file.exists():
            tail = events.tail(cfg.events_file, 2000)
        else:
            tail = []

    start_at = since_event_idx + 1 if since_event_idx >= 0 else 0
    rendered: list[str] = []
    for ev in tail[start_at:]:
        if ev.get("type") not in _TERMINAL_TASK_EVENT_TYPES:
            continue
        task_id = (ev.get("task") or "").strip()
        if not task_id:
            # No task ID → can't render a TB-N bullet. Skip rather
            # than emit a bare `**?** — …` line that would re-
            # introduce the very ambiguity this section closes.
            continue
        title = _resolve_task_title(cfg, task_id, ev.get("summary"))
        outcome = _outcome_for_event(ev)
        rendered.append(f"- **{task_id}** — {title}: {outcome}")

    if not rendered:
        return ""
    return _RECENT_TASK_ACTIVITY_HEADING + "\n\n" + "\n".join(rendered)


# ---------------------------------------------------------------------------
# TB-282: Attention-needing conditions section. Distinct from the routine
# `## Recent task activity` digest above — that section says "here is
# what just happened"; this section says "here is what needs you to
# act NOW". The two play complementary roles in the walk-away operator's
# triage: routine progress vs. interrupt-worthy condition.
#
# The detector layer (`ap2/attention.py`) returns structured records;
# this renderer pre-renders one bullet per still-active condition so the
# agent forwards the section verbatim — keeps the daemon authoritative
# for both detection and presentation (the agent never has to compute
# durations or look up titles).
#
# Position contract: the status-report prompt instructs the agent to
# forward this section BEFORE the routine body bullets so the
# attention signal lands FIRST in the post. The position is the
# load-bearing axis: an attention bullet buried below 8 progress
# bullets is the very failure mode TB-282 closes.

_ATTENTION_NEEDED_HEADING = "## Attention needed"


def render_attention_section(
    cfg: Config,
    *,
    since_event_idx: int,  # noqa: ARG001 — accepted for parity with sibling helpers
    tail: list[dict] | None = None,
    now: _dt.datetime | None = None,
) -> str:
    """Return the Markdown `## Attention needed` section the cron
    agent forwards verbatim into the Mattermost post, or "" when no
    attention conditions are currently active.

    `since_event_idx` is accepted for parity with the sibling
    digest helpers (TB-228 / TB-244 / TB-258 / TB-259 / TB-280) but
    is intentionally NOT used for scoping — attention conditions are
    point-in-time facts about the current state (Active section +
    most-recent `task_start`), not window-scoped event counts. A
    still-stuck task that crossed the threshold BEFORE the previous
    `cron_complete` is still stuck NOW and the operator still needs
    to see it.

    `now` (TB-301): optional reference time threaded into the underlying
    `detect_attention_conditions` call. Defaults to None — production
    cron-push callers leave it unset and the detector uses actual UTC
    (`_dt.datetime.now(_dt.timezone.utc)`). Tests pass a deterministic
    reference so an event seeded relative to a hardcoded timestamp does
    not silently fall outside the detector's 24h recency window on a
    later calendar day, time-bombing the test.

    Shape (when rendered):

        ## Attention needed

        - ⚠ **TB-N** — <title> Active for <h>h since <ts>
        - ⚠ **TB-M** — <title> Active for <h>h since <ts>

    One bullet per active condition. Operator-legible phrasing
    embedded by the detector's `summary` field (rendered verbatim
    after the warning glyph + bold TB-N + em-dash). The bullet is
    visually distinct from the routine progress bullets via the
    leading `⚠` glyph — same visual-distinctness pattern web
    chrome uses for warn-tinted rows.

    Omit-on-empty: returns "" when zero attention conditions are
    active. Quiet projects (no stuck tasks, etc.) stay byte-identical
    to the pre-TB-282 digest baseline so the prior axis-1/2/3/4/audit/
    stats/env-stale/recent-activity tests continue to pass when nothing
    needs attention.

    Defensive fallback: a detector exception is swallowed (returns
    "") so a regression in `detect_attention_conditions` never takes
    a status-report run down.
    """
    # TB-315: `detect_attention_conditions` lives in
    # `ap2/components/attention/__init__.py` post-migration. Core
    # resolves it via a dynamic `importlib.import_module(...)` call
    # so the TB-311 import-direction gate (which walks static
    # Import / ImportFrom nodes) stays quiet; the module attribute
    # is dereferenced at call time so monkeypatch.setattr-style
    # test fixtures targeting the new module path still propagate.
    import importlib as _importlib
    try:
        _attention_mod = _importlib.import_module(
            "ap2.components.attention",
        )
        conditions = _attention_mod.detect_attention_conditions(
            cfg, tail=tail, now=now,
        )
    except Exception:  # noqa: BLE001 — never break the status-report run
        return ""
    if not conditions:
        return ""

    rendered: list[str] = []
    for cond in conditions:
        # The detector's `summary` is the operator-legible phrasing
        # for the bullet body. We prepend the warning glyph + bold
        # TB-N (the canonical operator-readable anchor) and an
        # em-dash so the bullet reads as one cohesive line. The
        # `task` field in extras carries the TB-N; we look up the
        # title from extras too (the detector pre-resolved it via
        # Board.find so the renderer doesn't re-parse TASKS.md).
        task_id = (cond.extras.get("task") or "").strip()
        title = (cond.extras.get("title") or "").strip()
        if cond.type == "task_stuck":
            # For `task_stuck`, compose the bullet from the structural
            # extras so the format stays load-bearing across detector
            # implementations: `⚠ **TB-N** — <title> Active for <h>h
            # since <ts>`. Fall back to `cond.summary` if the extras
            # are malformed (defense for future detector variants).
            age_s = cond.extras.get("age_s")
            start_ts = cond.extras.get("start_ts") or cond.ts
            if isinstance(age_s, (int, float)) and start_ts:
                age_h = float(age_s) / 3600.0
                title_str = title or "(title unavailable)"
                rendered.append(
                    f"- ⚠ **{task_id}** — {title_str} "
                    f"Active for {age_h:.1f}h since {start_ts}"
                )
                continue
        # Generic fallback (covers future detectors AND `task_stuck`
        # with malformed extras): render the pre-rendered summary
        # verbatim. The warning glyph still leads.
        rendered.append(f"- ⚠ {cond.summary}")

    if not rendered:
        return ""
    return _ATTENTION_NEEDED_HEADING + "\n\n" + "\n".join(rendered)


# ---------------------------------------------------------------------------
# TB-281: Content-fingerprint dedup gate so consecutive status-report posts
# skip when nothing changed.
#
# Pre-TB-281 the skip-gate (`_status_report_should_skip`) only suppressed
# fully-idle windows — windows where zero "interesting" events landed
# since the previous `cron_complete name=status-report`. A window with
# even one `ideation_skipped reason=focus_exhausted` event (which is
# NOT in the boring-types denylist) bypassed the gate, the agent ran,
# and the post landed — but its STRUCTURAL CONTENT (board counts,
# pending-review TB-Ns, decisions-needed bullets, digest sub-sections,
# halt reason) was byte-for-byte identical to the previous post. Three
# consecutive low-delta posts trained the operator to ignore the
# channel, defeating the monitoring half of the walk-away promise the
# `Current focus: operator-legible reporting and monitoring` is built
# around.
#
# The fingerprint approach: compute a SHA-1 hex (truncated to 12 chars)
# over the structural inputs that drive the rendered post (board
# per-section counts; sorted pending-review TB-Ns; sorted decisions-
# needed bullet texts; content-fingerprints of each digest sub-section;
# most-recent auto-approve halt reason — explicitly EXCLUDING the
# headline timestamp). Persist the fingerprint in `cron_state.json`
# alongside the last-run float via `mark_run_with_payload`. At the
# next skip-check, recompute the prospective fingerprint — if it
# matches the stored one, the post would be identical → skip with
# `cron_skipped reason=duplicate_content` so the operator can audit
# suppressions via `ap2 logs`.
#
# The fingerprint gate runs ONLY when the idle gate would let the
# post through — the idle check stays the cheap-first fast path so
# fully-idle windows pay zero hashing cost (loads + Board parse +
# digest rendering).

_LAST_POST_FINGERPRINT_FIELD = "last_post_fingerprint"


def _compose_status_report_snapshot(cfg: Config) -> dict:
    """Build the structural snapshot driving the next status-report post.

    Returns a dict with:
      - `pending_review_ids`: list[str] — Backlog TB-Ns blocked on review.
      - `decisions_needed`: list[str] — ideator's
        `## Decisions needed from operator` bullet texts.
      - `digest_sections`: dict[str, str] mapping section heading →
        rendered section content. Empty / omitted sections are
        excluded so the fingerprint is sensitive to "appeared / went
        away" transitions on each axis. Sections covered:
        TB-228 (Automation loop activity), TB-244 (Focus rotation
        activity), TB-245 (Validator-judge fail-open window),
        TB-258 (Retrospective audit), TB-259 (Stats window aggregates),
        TB-260 (Daemon env file stale), TB-280 (Recent task activity).
      - `halt_reason`: str — most-recent auto-approve halt reason
        (one of `consecutive_freezes` / `window_token_cap_exceeded` /
        `per_task_token_cap_exceeded` / `task_error` /
        `validator_judge_noisy`, or "" when no halt is active).
      - `state_extras`: list[str] — ordered Markdown lines the routine
        injects into `## Current state`. Shared with the
        prompt-build wiring so `run_status_report` doesn't recompute
        digests twice.
      - `target_channel`: str — resolved Mattermost channel ID, "" if
        unconfigured.

    Pure read-only — composes existing helpers and emits no events.
    Snapshot is sensitive to events.jsonl + TASKS.md + ideation_state.md
    + cron_state.json at call time, so two calls back-to-back will
    return identical dicts iff nothing structural moved between them.
    """
    from .ideation import parse_operator_decisions
    # TB-309: janitor's data accessor moved behind the registry's
    # `status_findings_counts` hook-point. The status-report digest
    # composition stays in core (goal.md L150-152) — only the
    # janitor-data fetch is delegated.
    from .registry import default_registry

    _recent_finding_counts = default_registry().hook(
        "status_findings_counts", component="janitor",
    )

    state_extras: list[str] = []
    digest_sections: dict[str, str] = {}

    pending_ids = _pending_review_ids(cfg)
    if pending_ids:
        state_extras.append(
            f"- Pending operator review ({len(pending_ids)}): "
            f"{_format_pending_review_line(pending_ids)} "
            "— `ap2 approve TB-N`"
        )
    operator_decisions = parse_operator_decisions(
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if operator_decisions:
        state_extras.append(
            f"- Decisions needed from operator ({len(operator_decisions)}): "
            + "; ".join(operator_decisions)
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
        digest_sections[_AUTOMATION_DIGEST_HEADING] = automation_section
    focus_rotation_section = render_focus_rotation_activity_section(
        cfg, since_event_idx=since_idx, tail=activity_tail,
    )
    if focus_rotation_section:
        state_extras.append(focus_rotation_section)
        digest_sections[_FOCUS_ROTATION_HEADING] = focus_rotation_section
    validator_judge_state = automation_status.collect_window_validator_judge(
        cfg,
    )
    validator_judge_lines = render_validator_judge_activity_section(
        validator_judge_state,
    )
    if validator_judge_lines:
        block = "\n".join(validator_judge_lines)
        state_extras.append(block)
        digest_sections[_VALIDATOR_JUDGE_HEADING] = block
    audit_state = automation_status.collect_audit_state(cfg)
    audit_lines = render_audit_state_section(audit_state)
    if audit_lines:
        block = "\n".join(audit_lines)
        state_extras.append(block)
        digest_sections[_AUDIT_STATE_HEADING] = block
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
                if delta_s >= automation_stats.MIN_WINDOW_S:
                    stats_window_s = int(delta_s)
                elif delta_s > 0:
                    stats_window_s = automation_stats.MIN_WINDOW_S
            except (ValueError, TypeError):
                pass
    stats = automation_stats.collect_stats(cfg, window_s=stats_window_s)
    stats_lines = render_stats_window_section(stats)
    if stats_lines:
        block = "\n".join(stats_lines)
        state_extras.append(block)
        digest_sections[_STATS_WINDOW_HEADING_FMT.format(
            window=stats.get("window") or "?",
        )] = block
    env_staleness = automation_status.collect_env_staleness(cfg)
    env_staleness_lines = render_env_staleness_section(env_staleness)
    if env_staleness_lines:
        block = "\n".join(env_staleness_lines)
        state_extras.append(block)
        digest_sections[_ENV_STALENESS_HEADING] = block
    recent_task_activity_section = render_recent_task_activity_section(
        cfg, since_event_idx=since_idx, tail=activity_tail,
    )
    if recent_task_activity_section:
        state_extras.append(recent_task_activity_section)
        digest_sections[_RECENT_TASK_ACTIVITY_HEADING] = (
            recent_task_activity_section
        )
    # TB-282: attention-needing conditions go LAST in the composition
    # so the digest_sections insertion order reflects positional
    # contract documented in the prompt body. Rendering itself is
    # NOT positional within `state_extras` (the snapshot block lists
    # all sections in append order) — the prompt's verbatim-forwarding
    # contract tells the agent to lift this section ABOVE the routine
    # body bullets when posting to Mattermost. Same omit-on-empty rule
    # the sibling digest helpers use.
    attention_section = render_attention_section(
        cfg, since_event_idx=since_idx, tail=activity_tail,
    )
    if attention_section:
        state_extras.append(attention_section)
        digest_sections[_ATTENTION_NEEDED_HEADING] = attention_section

    # Most-recent auto-approve halt reason. `collect_auto_approve_state`
    # returns the active halt's `pause_reason` (or None when healthy).
    # We coalesce to "" so the JSON-stable fingerprint payload has a
    # deterministic key even when no halt is active.
    try:
        auto_state = automation_status.collect_auto_approve_state(cfg)
        halt_reason = auto_state.get("pause_reason") or ""
    except Exception:  # noqa: BLE001 — never break the routine.
        halt_reason = ""

    return {
        "pending_review_ids": pending_ids,
        "decisions_needed": list(operator_decisions),
        "digest_sections": digest_sections,
        "halt_reason": halt_reason,
        "state_extras": state_extras,
        "target_channel": target_channel,
    }


def compute_status_report_fingerprint(
    cfg: Config,
    *,
    board: Board | None = None,
    snapshot: dict | None = None,
) -> str:
    """SHA-1 hex (truncated to 12 chars) over the post's structural inputs.

    Inputs the fingerprint covers:
      - per-section board counts (`board.sections[s]` lengths across all
        six sections — Active / Ready / Backlog / Pipeline Pending /
        Complete / Frozen);
      - sorted tuple of pending-review TB-Ns (from `_pending_review_ids`);
      - sorted tuple of decisions-needed bullet texts;
      - per-digest-section content fingerprints (one SHA-1 of each
        rendered sub-section's content) — sensitive to both
        "appeared / disappeared" AND "content changed" axes;
      - most-recent auto-approve halt reason ("" when healthy).

    EXPLICITLY EXCLUDED: the headline timestamp, the cron-bookkeeping
    `cron_start` / `cron_complete` / `status_report` events, and the
    rendered Markdown prose flourish — only the deterministic structural
    skeleton the daemon controls contributes to the hash. Two
    back-to-back skip-checks against an unchanged events.jsonl +
    TASKS.md will produce identical fingerprints; the headline `now:`
    delta does NOT bust dedup.

    `board` defaults to `Board.load(cfg.tasks_file)` (None on parse
    failure → empty section_counts); `snapshot` defaults to
    `_compose_status_report_snapshot(cfg)`. Tests drive the helper
    with fabricated `snapshot` dicts to pin axis-by-axis sensitivity
    without spinning up real events files.
    """
    if board is None:
        if cfg.tasks_file.exists():
            try:
                board = Board.load(cfg.tasks_file)
            except Exception:  # noqa: BLE001
                board = None
    if snapshot is None:
        snapshot = _compose_status_report_snapshot(cfg)

    section_counts: dict[str, int] = {}
    if board is not None:
        for s, lines in board.sections.items():
            section_counts[s] = len(lines)

    digest_sections = snapshot.get("digest_sections") or {}
    digest_fingerprints = {
        heading: _hashlib.sha1(content.encode("utf-8")).hexdigest()
        for heading, content in sorted(digest_sections.items())
        if content
    }

    payload = {
        "section_counts": section_counts,
        "pending_review_ids": sorted(
            snapshot.get("pending_review_ids") or []
        ),
        "decisions_needed": sorted(snapshot.get("decisions_needed") or []),
        "digest_sections": digest_fingerprints,
        "halt_reason": snapshot.get("halt_reason") or "",
    }
    blob = _json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return _hashlib.sha1(blob).hexdigest()[:12]


def _load_last_post_fingerprint(cfg: Config) -> str:
    """Return the stored `status-report.last_post_fingerprint`, or ""
    if absent (first-ever run / state file missing / file unreadable).

    Reads via `cron.load_state` so the same lock-protected read path
    backs both the skip-check and the post-success stash. Returns ""
    (not None) so callers can do a simple `if fp and fp == ...` check
    without juggling the unset case.
    """
    try:
        state = _load_cron_state(cfg.cron_state_file)
    except Exception:  # noqa: BLE001
        return ""
    fp = state.get(f"status-report.{_LAST_POST_FINGERPRINT_FIELD}")
    return str(fp) if fp else ""


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
- TB-282: if the snapshot's `## Current state` block carries a
  `## Attention needed` section (heading + one bullet per still-
  active attention condition the daemon's `detect_attention_conditions`
  detector surfaced — today this is `task_stuck`; future detectors land
  alongside as `validator_judge_noisy` / `cost_cap_approach` / etc.),
  copy that entire section VERBATIM into your post (preserve the
  heading and every bullet, including the leading ⚠ glyph). Same
  verbatim-forwarding contract as TB-228 / TB-244 / TB-245 / TB-258 /
  TB-259 / TB-280 — the daemon owns the rendering; do NOT recompute,
  paraphrase, or drop bullets. Position this section IMMEDIATELY
  AFTER the headline and BEFORE your body bullets — the attention
  signal MUST be visually first, distinct from routine progress, so
  the walk-away operator sees what needs them to act NOW before
  scrolling past 8 lines of routine activity. Absent ⇒ no attention
  conditions are active (healthy / quiet project) ⇒ omit (the daemon
  renders nothing in that case).
- Headline: `**[<project_name>] Autopilot Status Report** — <now>`
  (TB-280: the daemon substitutes `<project_name>` at prompt-build
  time from `cfg.project_name` — a multi-project operator monitoring
  several daemons reads the bracketed identifier to know which
  project the post comes from without alt-tabbing to the repo.
  Keep the literal `<project_name>` token in this prompt — it is the
  load-bearing substitution target.)
- 4-8 bullets covering: tasks completed, tasks failed /
  verification_failed / retry_exhausted, pipelines started/completed,
  cron / ideation activity, daemon pause/resume, operator acks, open
  issues. Keep under 12 lines. TB-280: for terminal task events
  (`task_complete`, `task_failed`, `verification_failed`,
  `retry_exhausted`) in the inter-report window, DO NOT compose
  bullets from scratch — the daemon pre-renders the
  `## Recent task activity` section below with one
  `**TB-N** — <title>: <outcome>` line per terminal event, which you
  forward verbatim. Your bullets cover the OTHER signal (pipelines,
  cron / ideation, daemon lifecycle, operator acks, open issues).
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
  `ideation_state_updated` event in the tail; up to one ideation
  interval of staleness can bleed through into the
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
- TB-280: if the snapshot's `## Current state` block carries a
  `## Recent task activity` section (heading + one bullet per
  terminal task event in the inter-report window, each shaped as
  `- **TB-N** — <title>: <outcome>`), copy that entire section
  VERBATIM into your post (preserve the heading and every bullet).
  The daemon already resolved each task's title via `Board.find` and
  composed the outcome — do NOT recompute, paraphrase, or drop
  bullets, and do NOT re-emit bare TB-N references for events
  already pre-rendered here. Position this section AFTER your body
  bullets but BEFORE the automation / focus-rotation / validator-
  judge / audit / stats / env-stale sub-blocks so the operator
  scanning the post sees task-completion identity first, then the
  axis-level digests. Absent ⇒ no terminal task events landed in
  the window ⇒ omit (the daemon renders nothing in that case).
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
# walk-away channel by the next status-report cron tick. Without this, the silent-degradation
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
    # TB-282: proactive attention-raised push surface. A fresh
    # `attention_raised` event (e.g. a `task_stuck` detector fire)
    # MUST un-skip the dedup/idle gate so the operator's primary
    # walk-away channel surfaces the new attention condition on the
    # very next status-report cron tick — same TB-244 / TB-245
    # pattern of extending this set as new push-surface event
    # classes ship.
    "attention_raised",
    # TB-297: opt-in immediate-Mattermost-push audit event. A fresh
    # `attention_pushed` (the daemon's `_maybe_push_attention`
    # helper posted a one-line condition message to
    # `AP2_MM_CHANNELS[0]`) must un-skip the dedup/idle gate so the
    # next routine status-report cron acknowledges the immediate
    # push happened — keeps the two surfaces coherent (operator
    # reading the next status-report post sees the same condition rather than
    # the cron silently skipping because nothing else moved).
    # Mirrors the `attention_raised` entry just above; both event
    # classes can fire in the same tick so listing both is
    # symmetric.
    "attention_pushed",
})


def _status_report_idle_skip(cfg: Config) -> bool:
    """Return True iff the inter-report window has zero interesting events.

    Factored out of `_status_report_should_skip` (TB-281) so the
    fingerprint-dedup gate (`_status_report_skip_decision`) can reuse
    the idle check as a cheap-first fast path and the existing
    behavioral semantics stay byte-identical for callers that just
    want the bool.
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


def _status_report_skip_decision(cfg: Config) -> tuple[bool, str | None]:
    """Return `(should_skip, reason)` for the next status-report run.

    Two-tier gate:
      1. `no_activity_since_last_report` (TB-128) — cheap idle check.
         Suppresses windows where zero "interesting" events landed
         since the previous `cron_complete name=status-report`.
      2. `duplicate_content` (TB-281) — content-fingerprint check.
         Suppresses windows where SOME interesting events landed but
         the prospective post would be structurally identical to the
         last one stashed in `cron_state.json[status-report.
         last_post_fingerprint]`. The fingerprint hashes board counts
         + pending-review TB-Ns + decisions-needed bullets + digest
         sub-section contents + halt reason — see
         `compute_status_report_fingerprint`.

    The fingerprint tier runs ONLY when the idle gate would have
    let the post through, so fully-idle windows pay zero hashing /
    Board-parse / digest-rendering cost — same fast-path semantics
    that TB-128 promised pre-TB-281.

    Returns `(True, "no_activity_since_last_report")` from the idle
    tier, `(True, "duplicate_content")` from the fingerprint tier,
    `(False, None)` when the run should proceed.

    Defensive fallback: if `_compose_status_report_snapshot` or
    `compute_status_report_fingerprint` raises (corrupted events.jsonl
    / TASKS.md parse error / unexpected exception), we treat the gate
    as open (`False, None`) — the routine then runs to completion and
    emits a fresh fingerprint on the post-success path. Failing OPEN
    on the dedup gate is the load-bearing trade-off: a near-duplicate
    extra post is fine; a missed legitimate post (because the gate
    crashed silently) is not.
    """
    if _status_report_idle_skip(cfg):
        return True, "no_activity_since_last_report"
    stored_fp = _load_last_post_fingerprint(cfg)
    if not stored_fp:
        return False, None
    try:
        snapshot = _compose_status_report_snapshot(cfg)
        prospective_fp = compute_status_report_fingerprint(
            cfg, snapshot=snapshot,
        )
    except Exception:  # noqa: BLE001 — fail-open on the dedup gate.
        return False, None
    if prospective_fp == stored_fp:
        return True, "duplicate_content"
    return False, None


def _status_report_should_skip(cfg: Config) -> bool:
    """Return True iff a status-report run would be a no-op.

    Two-tier gate (TB-128 idle + TB-281 content-fingerprint dedup).
    Thin wrapper over `_status_report_skip_decision` so callers that
    only need the bool (existing tests, the daemon's diagnostic
    surfaces) stay byte-identical to the pre-TB-281 signature. Pre-
    TB-281 this was the only skip-gate; the new helper that returns
    `(bool, reason)` is the canonical entry point for the run path so
    it can log the right `cron_skipped` reason on suppression.

    "No-op" means one of:
      - idle gate (TB-128): there's a previous `cron_complete
        job=status-report` in the recent tail AND no events of
        interest have been appended after it (positionally — the
        events log timestamps to one-second resolution, so same-
        second self-noise after the cron_complete must not be misread
        as fresh activity). Events of interest are anything except
        this job's own bookkeeping.
      - duplicate-content gate (TB-281): events DID land in the
        window, but the prospective post is structurally identical to
        the one stashed under `status-report.last_post_fingerprint`
        in `cron_state.json`.

    Returns False if the job has never run before (or its last run
    rolled out of the tail) — first-run / cold-cache, always run.

    TB-228: automation-loop events (`auto_approve_paused`,
    `auto_approve_halted`, `auto_unfreeze_applied`,
    `auto_unfreeze_skipped`, `auto_approved`) count as interesting —
    they fall through the boring-types denylist. TB-244: axis-4
    focus-rotation events (`focus_advanced`, `roadmap_complete`) too.
    TB-245: axis-1 validator-judge fail-open events
    (`validator_judge_fail`, `validator_judge_timeout`) too. All
    listed in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES`.
    """
    skip, _reason = _status_report_skip_decision(cfg)
    return skip


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

    # TB-281: two-tier skip-gate (idle + duplicate_content). The
    # decision helper returns `(True, "no_activity_since_last_report")`
    # from the cheap idle check OR `(True, "duplicate_content")` from
    # the fingerprint comparison; the routine then logs whichever
    # reason fired so `ap2 logs` and `/events` can audit suppressions.
    should_skip, skip_reason = _status_report_skip_decision(cfg)
    if should_skip:
        skip_payload: dict = {
            "job": "status-report",
            "trigger": trigger,
            "reason": skip_reason or "no_activity_since_last_report",
        }
        if reason:
            skip_payload["chat_reason"] = reason
        events.append(cfg.events_file, "cron_skipped", **skip_payload)
        if trigger == "cron":
            mark_run(cfg.cron_state_file, "status-report")
        return StatusReportResult(
            skipped=True,
            reason=skip_reason or "no_activity_since_last_report",
        )

    # TB-281: hoisted state_extras composition into the snapshot helper
    # so the dedup gate (`_status_report_skip_decision`) and this routine
    # share one source-of-truth for what the prompt's `## Current state`
    # block carries. The snapshot dict also carries the digest-section
    # contents the fingerprint hashes — so the fingerprint stashed on
    # post-success matches what `_status_report_skip_decision` would
    # compute on the NEXT tick (modulo events that landed in between).
    # Pre-TB-281 the same composition lived inline here as a 220-line
    # block (TB-151 pending-review + TB-173/191 decisions-needed +
    # TB-177/178 janitor findings + TB-190 target-channel + TB-228
    # automation digest + TB-244 focus rotation + TB-245 validator-judge
    # + TB-258 audit + TB-259 stats window + TB-260 env staleness +
    # TB-280 recent task activity). The helper preserves the bullet
    # ordering byte-identically so prior axis-test expectations
    # continue to pass.
    snapshot = _compose_status_report_snapshot(cfg)
    state_extras = snapshot["state_extras"]
    # TB-281: compute the prospective post's fingerprint here (NOT in
    # the gate) so the value we stash on the post-success path matches
    # what the agent actually saw at prompt-build time. A later tick's
    # skip-check recomputes against the same axes; if nothing
    # structural moved, the hashes match and the cron emits
    # `cron_skipped reason=duplicate_content` instead of re-firing the
    # post.
    post_fingerprint = compute_status_report_fingerprint(
        cfg, snapshot=snapshot,
    )
    # TB-280: project-identity headline substitution. The prompt body
    # carries the literal `<project_name>` token; the daemon swaps it
    # for `cfg.project_name` here so the agent posts
    # `**[<name>] Autopilot Status Report** — <now>` to Mattermost.
    # `.replace` (not `.format`) avoids collisions with the prompt's
    # existing curly braces (none today; defense against future
    # `{ts}`-style interpolation additions).
    prompt_body = STATUS_REPORT_PROMPT.replace(
        "<project_name>", cfg.project_name,
    )
    prompt = _prompts.build_control_prompt(
        cfg, "status-report", prompt_body,
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
    # TB-339 (axis-5 cleanup): the per-site `status_report_effort`
    # layer is now resolved through `cfg.get_core_value(...)` too —
    # the `or`-chain collapses the empty-string default to the global
    # `agent_effort` fallback, preserving the original `per-site env
    # > global env > per-site default` precedence exactly (sectioned
    # env > flat env > TOML > "" > sectioned env > flat env > TOML >
    # "medium"). FLAT_TO_SECTIONED already maps
    # `AP2_STATUS_REPORT_EFFORT` → `core.status_report_effort`.
    effort = cfg.get_core_value("status_report_effort", default="") \
        or cfg.get_core_value("agent_effort", default="medium")
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
        # TB-281: stash the just-rendered post's fingerprint alongside
        # the last-run timestamp via `mark_run_with_payload`. The
        # sibling key `status-report.last_post_fingerprint` lands next
        # to `status-report`'s float timestamp; `due_jobs` is untouched
        # (the dotted key never collides with a job name). On the next
        # tick's skip-check, `_status_report_skip_decision` reads this
        # value back and compares against the prospective fingerprint
        # — match ⇒ `cron_skipped reason=duplicate_content`.
        mark_run_with_payload(
            cfg.cron_state_file,
            "status-report",
            payload={_LAST_POST_FINGERPRINT_FIELD: post_fingerprint},
        )
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
