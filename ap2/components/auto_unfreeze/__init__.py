"""Auto-unfreeze briefing-shape fix sweep (TB-225 + TB-233 dry-run).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) decides WHEN to sweep Frozen tasks; this module
owns the parsing + guard chain + patch-application logic itself:

  - `_auto_unfreeze_allowlist` / `_auto_unfreeze_dry_run` /
    `_auto_unfreeze_max_per_task` / `_auto_unfreeze_max_per_day`: env-knob
    parsing for the trust contract + caps + monitor-only on-ramp.
  - `_most_recent_blocked_complete_for` / `_count_auto_unfreeze_*`: events
    tail-scan helpers for the per-task + per-day cap accounting.
  - `_apply_auto_unfreeze_patch`: read-modify-write the briefing line +
    queue the `update` + `unfreeze` operator-queue ops (TB-153 lineage).
  - `_maybe_auto_unfreeze`: the orchestrator entry point — walks Frozen,
    parses each task's `task_complete blocked` summary's `BriefingFix:`
    line via `_shared.parse_blocked_summary_fix_shape`, applies any that
    pass the allowlist + caps + line-match guards.
  - `_shared_parse`: thin wrapper kept here so the TB-225 verification
    gate's "helper is callable from the daemon module-text" assertion
    still resolves through the daemon re-export.

The `_append_decisions_needed_bullet` helper from `auto_approve.py` is
imported here because the daily-cap halt surfaces a `## Decisions needed
from operator` bullet — same surface auto-approve uses, so they share
the writer.

Recurring failure mode: a Frozen task whose root cause is a briefing-shape
regression the agent already diagnosed in its `task_complete blocked`
summary (e.g. TB-204's `grep -lE` → `grep -rlE`, TB-207's literal-backtick
in shell bullets). The agent emits a structured `BriefingFix: <shape> at
<path>:<line>: <from> -> <to>` line; the daemon parses it, verifies the
briefing-line literal match (closes the operator-edit-during-failure
data-race window), patches the briefing via the operator-queue `update` op
(TB-153 lineage — same audit-trail + atomic-with-redispatch contract as
operator-applied edits), and unfreezes the task.

Allowlist-driven: `AP2_AUTO_UNFREEZE_FIX_SHAPES` names the trust contract.
Unknown shapes still require manual `ap2 unfreeze`. Per-task + per-day caps
bound blast radius. Default-unset on the shapes knob → feature opt-in only;
operators upgrade trust by adding shape tokens, never by removing safeties.

Separate event audit trail: `auto_unfreeze_applied` (success) and
`auto_unfreeze_skipped` (any guarded skip — `knob_unset` is implicit and
does NOT emit per-tick to avoid noise; the other reasons all emit so the
operator can see why a Frozen task stayed Frozen).
"""
from __future__ import annotations

import os
import time

from ap2 import events, tools
from ap2.auto_approve import _append_decisions_needed_bullet, _parse_event_ts
from ap2.board import Board
from ap2.config import Config


_AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT = 1
_AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT = 3
_AUTO_UNFREEZE_WINDOW_S = 24 * 3600  # rolling 24h, mirrors TB-224's window


def _auto_unfreeze_allowlist() -> frozenset[str]:
    """Effective allowlist parsed from `AP2_AUTO_UNFREEZE_FIX_SHAPES`.

    Comma-separated shape tokens. Default unset → empty set, which the
    `_maybe_auto_unfreeze` caller treats as "feature disabled" (no
    auto-unfreeze attempts, no skip events). Operators opt in by listing
    shape tokens; the env-knob string IS the trust contract.

    Whitespace around tokens is trimmed; empty tokens (e.g. trailing
    comma) are dropped. The frozenset return makes the value safe to
    pass around or compare against without defensive copies.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_FIX_SHAPES", "").strip()
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _auto_unfreeze_dry_run() -> bool:
    """TB-233: True iff `AP2_AUTO_UNFREEZE_DRY_RUN` is set to a truthy
    value (`"1"` / `"true"` / `"yes"`, case-insensitive).

    Monitor-only on-ramp for the auto-unfreeze loop (TB-225), sibling
    of `automation_status._is_auto_approve_dry_run` (TB-232) on the
    axis-1 side. When both `AP2_AUTO_UNFREEZE_FIX_SHAPES` (non-empty)
    AND `AP2_AUTO_UNFREEZE_DRY_RUN=1` are set, `_maybe_auto_unfreeze`
    runs the entire guard chain (allowlist + per-task cap + per-day
    cap + briefing-line match) but, instead of calling
    `_apply_auto_unfreeze_patch`, emits a `would_auto_unfreeze` audit
    event with the same payload shape as `auto_unfreeze_applied`. The
    briefing file is NOT mutated and no operator-queue ops are
    appended; the per-day-count counter does NOT increment in dry-run
    (no real application). Operator observes the simulated decisions
    in `ap2 logs --type would_auto_unfreeze` (and the status-report's
    automation-loop digest from TB-228) for a window, gains confidence
    on the live Frozen set, then flips the dry-run knob off to engage
    real patching.

    Default unset → False (current TB-225 behavior; byte-identical to
    pre-TB-233 when the knob has never been set). Permissive parse
    mirrors the boolean shape used by `_is_truthy` in
    `automation_status.py` so operators tuning the autopilot env file
    see one consistent convention across knobs.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_DRY_RUN", "").strip().lower()
    return raw in ("1", "true", "yes")


def _auto_unfreeze_max_per_task() -> int:
    """Per-task cap from `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default 1).

    Bounds oscillation when an auto-applied patch still fails: a task
    that's been auto-unfrozen once and re-frozen falls back to manual
    `ap2 unfreeze`. Default 1 because the typical recurrence is "fix
    once, succeeds on retry"; >1 indicates the patched form ALSO
    failed and the operator should see it.

    Permissive parse: empty / non-int / negative falls back to the
    default. Zero is honored (caps disabled = unbounded retries) but
    is intentionally NOT the default — disabling the per-task cap
    should be an explicit operator decision.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_TASK", "").strip()
    if not raw:
        return _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT
    return v if v >= 0 else _AUTO_UNFREEZE_MAX_PER_TASK_DEFAULT


def _auto_unfreeze_max_per_day() -> int:
    """Per-day cap from `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default 3).

    Rolling 24h cap on total auto-unfreeze applications across all
    tasks. Bounds the "systemic regression cascades through 10 tasks
    before operator notices" failure mode. When exceeded, the daemon
    halts and surfaces a `## Decisions needed from operator` bullet so
    the operator sees a systemic-regression signal rather than a silent
    burn.

    Default 3 calibrated for the observed steady-state recurrence rate
    on this codebase (TB-204 + TB-207 = 2 instances in one week);
    higher values invite the silent-burn failure mode, lower values
    invite operator-toil from over-frequent caps.
    """
    raw = os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_DAY", "").strip()
    if not raw:
        return _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT
    return v if v >= 0 else _AUTO_UNFREEZE_MAX_PER_DAY_DEFAULT


def _most_recent_blocked_complete_for(
    tail: list[dict], task_id: str,
) -> dict | None:
    """Return the most recent `task_complete status=blocked` event for
    `task_id` in the events tail, or None when no such event exists.

    Tail is ordered oldest-first (per `events.tail`); we scan forward
    and keep the last match. `blocked` is the specific agent-emitted
    status the parser contract attaches to — the agent's own
    `report_result(status="blocked", summary=...)` carries the
    `BriefingFix:` line in `summary`. Other failure statuses
    (`verification_failed`, `failed`, `incomplete`) do not — they're
    daemon-synthesized or agent-emitted-without-fix-shape paths.
    """
    last: dict | None = None
    for e in tail:
        if e.get("type") != "task_complete":
            continue
        if str(e.get("task") or "") != task_id:
            continue
        if str(e.get("status") or "").strip() != "blocked":
            continue
        last = e
    return last


def _count_auto_unfreeze_applied_for_task(
    tail: list[dict], task_id: str,
) -> int:
    """Count `auto_unfreeze_applied` events for `task_id` over the full
    tail. The per-task cap fires when this hits
    `AP2_AUTO_UNFREEZE_MAX_PER_TASK` (default 1).

    No window — a task that's been auto-unfrozen even once long ago
    must NOT silently re-cycle through auto-unfreeze attempts after
    every fresh freeze. The operator's manual `ap2 unfreeze` is the
    expected escape after the per-task cap trips; that emits a
    `task_unfrozen` event but does NOT reset this counter (intentional
    — the per-task cap is about "this task is auto-unfreeze-eligible
    over its whole lifetime," not "since the last operator touch").
    """
    return sum(
        1
        for e in tail
        if e.get("type") == "auto_unfreeze_applied"
        and str(e.get("task") or "") == task_id
    )


def _count_auto_unfreeze_applied_in_window(
    tail: list[dict], *, now_s: float | None = None,
) -> int:
    """Count `auto_unfreeze_applied` events whose `ts` falls within the
    last `_AUTO_UNFREEZE_WINDOW_S` (24h). The per-day cap fires when
    this hits `AP2_AUTO_UNFREEZE_MAX_PER_DAY` (default 3).

    Rolling window (not calendar day) to match TB-224's
    cost-cap-window shape — same operator-rhythm rationale, no
    timezone ambiguity. Events with unparseable `ts` are skipped
    rather than counted (defensive; matches `_parse_event_ts`'s
    convention).
    """
    if now_s is None:
        now_s = time.time()
    count = 0
    for e in tail:
        if e.get("type") != "auto_unfreeze_applied":
            continue
        ts = _parse_event_ts(e.get("ts"))
        if ts is None:
            continue
        if now_s - ts > _AUTO_UNFREEZE_WINDOW_S:
            continue
        count += 1
    return count


def _apply_auto_unfreeze_patch(
    cfg: Config,
    *,
    task_id: str,
    fix: dict,
) -> str | None:
    """Apply the agent-diagnosed line replacement to the briefing file
    and queue the `update` + `unfreeze` ops on the operator queue.
    Returns None on success, or a `reason` token on guarded skip.

    Guards (in order):
      - `briefing_path_missing`: the `file` named in the fix doesn't
        exist on disk (briefing was renamed / deleted between failure
        and freeze handling).
      - `briefing_mismatch`: the named line doesn't literally contain
        the `from` pattern. The agent's diagnosis is stale (e.g. the
        operator hand-edited the briefing mid-failure to try fixing
        it themselves). The fail-safe is to leave the task Frozen.
      - `queue_error`: the operator-queue `update` or `unfreeze` op
        rejected our payload (structural validation, board-state
        mismatch). Surfaces the underlying `_err` text so post-hoc
        forensics can grep for the rejection reason.

    The patch is applied as the operator-queue `update` op with
    `briefing=<full new content>` and `skip_goal_alignment=True` (the
    briefing was already goal-validated at add time; a mechanical
    single-line fix doesn't change the goal anchor). The `unfreeze` op
    moves the task from Frozen → Backlog and resets the retry counter.
    Both ops drain on the NEXT tick — one-tick delay before the task
    is dispatchable, the trade-off for the audit-trail symmetry with
    operator-applied edits (TB-153 lineage).
    """
    briefing_path = cfg.project_root / fix["file"]
    if not briefing_path.exists():
        return "briefing_path_missing"
    try:
        content = briefing_path.read_text()
    except OSError:
        return "briefing_path_missing"
    lines = content.splitlines(keepends=True)
    line_no = fix["line"]
    if line_no < 1 or line_no > len(lines):
        return "briefing_mismatch"
    target_line = lines[line_no - 1]
    if fix["from"] not in target_line:
        return "briefing_mismatch"
    new_line = target_line.replace(fix["from"], fix["to"], 1)
    if new_line == target_line:
        # Replacement was a no-op (from == to, or from empty). Still
        # a mismatch in spirit — refuse to spend an auto-unfreeze slot
        # on a no-op patch.
        return "briefing_mismatch"
    lines[line_no - 1] = new_line
    new_content = "".join(lines)
    # Queue the update + unfreeze ops. Order matters: the drain applies
    # them in queue order, so the briefing patch lands before the
    # unfreeze (which makes the task dispatchable). A failed unfreeze
    # after a successful update leaves the briefing patched but the
    # task Frozen — the operator can still manually unfreeze with the
    # patched briefing already in place, which is the right fail-safe.
    update_res = tools.do_operator_queue_append(
        cfg,
        {
            "op": "update",
            "task_id": task_id,
            "briefing": new_content,
            "skip_goal_alignment": True,
        },
    )
    if update_res.get("isError"):
        return "queue_error"
    unfreeze_res = tools.do_operator_queue_append(
        cfg,
        {"op": "unfreeze", "task_id": task_id},
    )
    if unfreeze_res.get("isError"):
        return "queue_error"
    return None


def _maybe_auto_unfreeze(cfg: Config) -> None:
    """Sweep Frozen tasks for agent-diagnosed briefing-shape fixes and
    apply any that pass the allowlist + cap + briefing-match guards
    (TB-225).

    Pure / side-effect-bounded: writes events + queues operator ops,
    never touches TASKS.md / briefings directly. Safe to call from
    `_tick` without taking the board lock (operator-queue append takes
    its own narrow lock, board reads are crash-tolerant).

    The function is a no-op when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is
    unset (the feature's master switch — opt-in only; no skip events
    fire in this branch to avoid `events.jsonl` noise from operators
    who haven't engaged the feature). All OTHER guarded skips emit a
    structured `auto_unfreeze_skipped reason=<token>` event so the
    operator can see, via `ap2 logs`, why a Frozen task stayed Frozen.

    Per-task cap (`AP2_AUTO_UNFREEZE_MAX_PER_TASK`, default 1): once
    exceeded, the task falls back to manual `ap2 unfreeze`. Per-day
    cap (`AP2_AUTO_UNFREEZE_MAX_PER_DAY`, default 3): once exceeded,
    the daemon halts further auto-unfreeze applications on the tick
    AND surfaces a `## Decisions needed from operator` bullet so the
    operator sees a systemic-regression signal rather than a silent
    burn.
    """
    allowlist = _auto_unfreeze_allowlist()
    if not allowlist:
        return
    if not cfg.tasks_file.exists():
        return
    try:
        board = Board.load(cfg.tasks_file)
    except Exception:  # noqa: BLE001
        return
    frozen_tasks = list(board.iter_tasks("Frozen"))
    if not frozen_tasks:
        return
    tail = events.tail(cfg.events_file, 2000)
    per_task_cap = _auto_unfreeze_max_per_task()
    per_day_cap = _auto_unfreeze_max_per_day()
    day_count = _count_auto_unfreeze_applied_in_window(tail)

    for task in frozen_tasks:
        last_blocked = _most_recent_blocked_complete_for(tail, task.id)
        if last_blocked is None:
            # No diagnosed fix-shape — silently leave Frozen. The
            # operator-manual path is the expected route for non-
            # blocked failure-statuses (verification_failed, error,
            # timeout) and for tasks that simply haven't surfaced a
            # diagnosable summary yet. No skip event: this is the
            # baseline state for most Frozen tasks.
            continue
        summary = str(last_blocked.get("summary") or "")
        fix = _shared_parse(summary)
        if fix is None:
            # Malformed / missing `BriefingFix:` prefix. The agent's
            # summary lacked a structured diagnosis — fall back to
            # today's manual-unfreeze path. No skip event for the
            # same reason as the no-blocked-complete case: baseline.
            continue
        if fix["shape"] not in allowlist:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason="shape_not_in_allowlist",
                shape=fix["shape"],
            )
            continue
        prior_for_task = _count_auto_unfreeze_applied_for_task(
            tail, task.id,
        )
        if per_task_cap > 0 and prior_for_task >= per_task_cap:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason="per_task_cap",
                applied=prior_for_task,
                cap=per_task_cap,
            )
            continue
        if per_day_cap > 0 and day_count >= per_day_cap:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason="per_day_cap",
                applied=day_count,
                cap=per_day_cap,
            )
            # TB-233: in dry-run the per-day cap halt is still the
            # right signal that the allowlist would generate more
            # applications than the safety floor allows — surface it
            # pre-flight. The decisions-needed bullet AND the
            # `## Decisions needed from operator` mutation, however,
            # belong to the real-application path only; dry-run is
            # monitor-only and must NOT touch board / state. Skip the
            # bullet append in dry-run and short-circuit the same way
            # the real path does. Operator sees the skip event +
            # (over the dry-run window) the would_auto_unfreeze stream
            # and infers the systemic-regression signal directly from
            # the auto_unfreeze_skipped count.
            if _auto_unfreeze_dry_run():
                return
            try:
                _append_decisions_needed_bullet(
                    cfg,
                    (
                        f"Auto-unfreeze daily cap reached "
                        f"({day_count}/{per_day_cap}) — systemic-regression "
                        f"signal. Recent Frozen tasks are exhausting the "
                        f"briefing-shape auto-heal budget; inspect via "
                        f"`ap2 logs --type auto_unfreeze_applied` and "
                        f"either bump `AP2_AUTO_UNFREEZE_MAX_PER_DAY` or "
                        f"investigate why so many briefing-shape regressions "
                        f"are landing."
                    ),
                )
            except OSError:
                pass
            # Halt: no further auto-unfreeze attempts this tick. The
            # remaining Frozen tasks (if any) stay Frozen until the
            # window rolls forward or the operator intervenes.
            return
        # TB-233: dry-run check happens AFTER all skip-emission so
        # the operator's dry-run window observes the same
        # `auto_unfreeze_skipped` events it would see live — the only
        # change in dry-run is the WRITE step: instead of calling
        # `_apply_auto_unfreeze_patch` (which queues `update` +
        # `unfreeze` ops on the operator queue and mutates the
        # briefing file), emit a `would_auto_unfreeze` audit event
        # with the same payload shape as `auto_unfreeze_applied` and
        # continue. The per-day-count + per-task-prior-count
        # counters do NOT increment in dry-run (no real application),
        # so a dry-run window can observe MORE simulated decisions
        # than the per-day cap would normally allow — that's the
        # right shape (the operator wants to see the full Frozen-set
        # decision before flipping the switch). When dry-run is off,
        # behavior is byte-identical to pre-TB-233.
        if _auto_unfreeze_dry_run():
            events.append(
                cfg.events_file,
                "would_auto_unfreeze",
                task=task.id,
                shape=fix["shape"],
                file=fix["file"],
                line=fix["line"],
                **{"from": fix["from"], "to": fix["to"]},
            )
            continue
        skip_reason = _apply_auto_unfreeze_patch(
            cfg, task_id=task.id, fix=fix,
        )
        if skip_reason is not None:
            events.append(
                cfg.events_file,
                "auto_unfreeze_skipped",
                task=task.id,
                reason=skip_reason,
                shape=fix["shape"],
            )
            continue
        events.append(
            cfg.events_file,
            "auto_unfreeze_applied",
            task=task.id,
            shape=fix["shape"],
            **{"from": fix["from"], "to": fix["to"]},
        )
        day_count += 1


def _shared_parse(summary: str) -> dict | None:
    """Thin wrapper over `ap2._shared.parse_blocked_summary_fix_shape`
    so the daemon's call site keeps `parse_blocked_summary_fix_shape`
    in the daemon's module-text (TB-225 verification gate looks for
    the helper name in `ap2/daemon.py`). Same import-time module-text
    consumption pattern as TB-220's `_shared.now` / `_shared.read_pid`
    consumers.
    """
    from ap2._shared import parse_blocked_summary_fix_shape
    return parse_blocked_summary_fix_shape(summary)
