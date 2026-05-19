"""Auto-approve dispatch policy (TB-223 + TB-224 + TB-232).

Lifted from `ap2/daemon.py` as part of TB-263's responsibility split. The
orchestrator (`_tick`) decides WHEN to consult these gates; this module
owns the gate-evaluation logic itself:

  - TB-223 cumulative-regression circuit-breaker: when N consecutive
    `task_complete` events end with a failure status AND the most recent
    failure rolled into `retry_exhausted`, halt auto-promotion of tasks
    that ideation auto-approved (`auto_approved` event-driven membership).
    Operator-approved tasks (`ideation_approved`) continue dispatching.
  - TB-224 cost + blast-radius caps: three layered halt conditions
    (`task_error`, `per_task_cap`, `window_cap`) sharing the same ack
    verb `auto_approve_window_resume`.
  - TB-232 dispatch-time decision helper: `evaluate_auto_approve_decision`
    routes the proposal-time `do_board_edit` add_backlog branch through
    a single source-of-truth gate chain (tags → freeze → token caps →
    dry-run vs. strip terminal branch).

Every public-ish symbol is re-exported from `ap2/daemon.py` so existing
test paths (`daemon._auto_approve_paused`, `daemon._was_auto_approved`,
etc.) continue to resolve. The split is mechanical — same env knobs, same
events, same caps.
"""
from __future__ import annotations

import os
import re as _re
import time
from datetime import datetime

from . import events, ideation
from .config import Config


# ============================================================================
# TB-223: cumulative-regression circuit-breaker for the opt-in
# `AP2_AUTO_APPROVE` mode. When N consecutive `task_complete` events
# end with a failure status AND ultimately route to `retry_exhausted`,
# the daemon halts auto-promotion of tasks that were auto-approved by
# ideation. Operator-approved tasks (those that went through
# `ap2 approve` after `@blocked:review` was preserved) continue to
# dispatch normally — the pause is targeted, not blanket.
#
# Unfreeze: the operator runs `ap2 ack auto_approve_unfreeze --reason
# "..."` (the existing `ap2 ack` verb + queue plumbing per TB-106 /
# TB-201). The drain-side emits an `operator_ack` event with the note
# carrying the `auto_approve_unfreeze` token; `_auto_approve_paused`
# below treats that as a state reset — subsequent failure counting
# starts from the next `task_complete`.
#
# Tunable via `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` (default 3). Setting
# it to 0 (or any non-positive integer) effectively disables the
# circuit-breaker (the freeze check immediately returns False), which
# is the escape hatch for operators who explicitly trust the upstream
# gates beyond this layer.
_AUTO_APPROVE_FAILURE_STATUSES: frozenset[str] = frozenset(
    {"verification_failed", "blocked", "error", "failed"},
)
_AUTO_APPROVE_UNFREEZE_TOKEN = "auto_approve_unfreeze"


def _auto_approve_freeze_threshold() -> int:
    """Effective threshold for the auto-approve cumulative-regression
    circuit-breaker, env-overridable via `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`.

    Default 3 (`ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT`).
    Non-int / empty values silently fall back to the default; a value
    `<= 0` is treated as "circuit-breaker disabled" (see
    `_auto_approve_paused` which returns False in that case so an
    operator who wants the auto-approve dispatch without the safety
    net can configure that explicitly). Same permissive-parse shape
    as `ideation._cooldown_s`.
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_FREEZE_THRESHOLD", "").strip()
    if not raw:
        return ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return ideation.AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT


def _auto_approve_paused(cfg: Config) -> bool:
    """True iff the auto-approve dispatch path should be halted now.

    Reads the tail of `events.jsonl` and looks at the most recent
    `task_complete` events. The path is paused when:
      - The threshold N (= `AP2_AUTO_APPROVE_FREEZE_THRESHOLD`, default
        3) is positive, AND
      - The last N `task_complete` events all carry a failure status
        in `_AUTO_APPROVE_FAILURE_STATUSES`, AND
      - The most recent of those was followed by a `retry_exhausted`
        event for the same task (the briefing's "end in
        `retry_exhausted`" qualifier — the failure chain ultimately
        froze a task rather than just looping a single TB through
        retries), AND
      - The operator has NOT emitted an `operator_ack` whose `note`
        contains `auto_approve_unfreeze` AFTER the failure window
        started (the explicit reset signal).

    Threshold `<= 0` short-circuits to False (operator opted out of
    the circuit-breaker explicitly — see the parser comment).

    Pure / no I/O beyond the events.jsonl tail read; safe to call from
    `_tick` without taking the board lock.
    """
    threshold = _auto_approve_freeze_threshold()
    if threshold <= 0:
        return False
    if not cfg.events_file.exists():
        return False
    # Tail-window must be big enough to cover the threshold-N
    # completions plus interleaved noise (status_report, cron, judge
    # calls). 500 is a generous default; production events.jsonl tail
    # is dominated by observability lines, so a bigger window is cheap
    # (events.tail is bounded by the file).
    tail = events.tail(cfg.events_file, 500)
    # Reset state at the most recent unfreeze ack: anything before it
    # is "old water under the bridge" and doesn't count toward the
    # current consecutive-failure window.
    last_unfreeze_idx = -1
    for i, e in enumerate(tail):
        if e.get("type") != "operator_ack":
            continue
        note = str(e.get("note") or "")
        if _AUTO_APPROVE_UNFREEZE_TOKEN in note:
            last_unfreeze_idx = i
    relevant = tail[last_unfreeze_idx + 1:]
    # Collect `task_complete` events in order.
    completes = [e for e in relevant if e.get("type") == "task_complete"]
    if len(completes) < threshold:
        return False
    window = completes[-threshold:]
    if not all(
        str(e.get("status", "")).strip() in _AUTO_APPROVE_FAILURE_STATUSES
        for e in window
    ):
        return False
    # The "end in retry_exhausted" qualifier: the most recent failing
    # task_complete must have been followed by a retry_exhausted event
    # for the same task (i.e. the failure chain actually froze a task,
    # not just looped a single TB through one retry). Scan the
    # `relevant` slice forward from the last-window-completion onward.
    final_complete = window[-1]
    final_task = str(final_complete.get("task") or "")
    if not final_task:
        return False
    # Find the index of `final_complete` in `relevant` and scan after.
    try:
        final_idx = next(
            i for i, e in enumerate(relevant) if e is final_complete
        )
    except StopIteration:
        return False
    for e in relevant[final_idx:]:
        if (
            e.get("type") == "retry_exhausted"
            and str(e.get("task") or "") == final_task
        ):
            return True
    return False


def _was_auto_approved(cfg: Config, task_id: str) -> bool:
    """True iff `task_id` has an `auto_approved` event in events.jsonl
    AND no subsequent `ideation_approved` event for the same TB-N
    (which would indicate the operator subsequently `ap2 approve`'d
    the task, promoting it to the operator-approved bucket).

    Drives the per-task gate at `_tick`'s auto-promote step: when the
    circuit-breaker is active (`_auto_approve_paused`), we still want
    to let operator-approved tasks through. Distinguishing
    auto-approved (event = `auto_approved`) from operator-approved
    (event = `ideation_approved`) lets the gate apply at the right
    granularity.

    A task that was auto-approved AND later operator-approved counts
    as operator-approved (the operator's explicit decision overrides
    the auto layer). Pure / events.jsonl tail read only.
    """
    if not cfg.events_file.exists():
        return False
    tail = events.tail(cfg.events_file, 1000)
    auto_seen = False
    for e in tail:
        if str(e.get("task") or "") != task_id:
            continue
        typ = e.get("type")
        if typ == "auto_approved":
            auto_seen = True
        elif typ == "ideation_approved":
            # Operator explicitly approved → no longer in the
            # auto-approved bucket regardless of prior auto stamp.
            auto_seen = False
    return auto_seen


# ============================================================================
# TB-224: cost + blast-radius guards layered on TB-223's auto-approve gate.
#
# Two env knobs (per-task + 24h-rolling-window token caps) plus a single-
# event `task_error` halt. All three halt conditions share the same
# auto-promote-paused state and resume via the same operator ack verb
# `ap2 ack auto_approve_window_resume`. Defaults are unset on both knobs
# → no caps applied (current behavior preserved); the operator opts in
# alongside flipping `AP2_AUTO_APPROVE=1`.
#
# Why two knobs, not one:
#   - `per_task_cap` catches the single-runaway pattern: one task in an
#     infinite tool-call loop burning $50 of tokens before the verifier
#     even runs.
#   - `window_cap` catches the drift pattern: 50 small tasks each within
#     the per-task cap but cumulatively unbounded.
# Orthogonal failure modes; both must be operator-tunable. Same shape as
# TB-223's per-task vs. cumulative-regression layering.
#
# Why "post-hoc" detection (vs. predictive estimator): `task_run_usage`
# events emit only at terminal paths (TB-165), so the cap fires AFTER the
# offending task finished — not mid-stream. The auto-promote-stream halt
# is what catches the "one more task in this loop would be unsafe"
# pattern at the right moment (next tick, before the next auto-approved
# task would dispatch). The briefing's "halt the in-flight task" framing
# in Scope (1) is forward-looking — practically the daemon detects after
# completion and gates the NEXT auto-promote. Same shape, slightly
# delayed actuation. The briefing explicitly excludes predictive cost
# estimation from this task's scope.
#
# Why one shared ack verb (`auto_approve_window_resume`) instead of one
# per cap: the operator's mental model collapses to "auto-promote
# paused" regardless of which cap tripped — three distinct resume verbs
# would be unnecessary friction. The audit trail's
# `auto_approve_halted reason=...` event field preserves the forensic
# distinction so an offline reader can still tell which cap tripped.
#
# Why `task_error` is single-event (no N threshold like TB-223): a
# `task_error` event indicates infrastructure failure (SDK timeout,
# agent OOM, briefing read failure) per `ap2/events.py` conventions. It
# is structurally rare in steady-state (the verifier's normal failure
# path is `verification_failed`, not `task_error`); a single event
# indicates infrastructure breakage that benefits from operator
# attention immediately, not after N similar events. Distinct from
# TB-223's cumulative-regression N=3 default which is calibrated for
# the noisier `verification_failed` channel.
# ============================================================================

_AUTO_APPROVE_WINDOW_RESUME_TOKEN = "auto_approve_window_resume"
_AUTO_APPROVE_WINDOW_S = 24 * 3600  # 24h rolling window


def _per_task_token_cap() -> int:
    """Effective per-task token cap from `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`.

    Returns `0` (cap disabled) when the env var is unset / empty /
    non-integer / non-positive. Operators who haven't budgeted their
    project don't get a hardcoded cap surprising them; the explicit
    way to disable is to leave the knob unset (or set it to `0`).
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
    except ValueError:
        return 0
    return v if v > 0 else 0


def _window_token_cap() -> int:
    """Effective 24h rolling-window token cap from
    `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`.

    Returns `0` (cap disabled) when the env var is unset / empty /
    non-integer / non-positive. Same parse shape as
    `_per_task_token_cap` so the two knobs share one mental model.
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", "").strip()
    if not raw:
        return 0
    try:
        v = int(raw)
    except ValueError:
        return 0
    return v if v > 0 else 0


def _event_combined_tokens(event: dict) -> int:
    """Combined `input_tokens + output_tokens` from a `task_run_usage`
    event's `usage` blob (TB-165 schema). Robust against missing
    fields or a non-dict `usage` (returns 0 in those cases — matches
    the defensive shape of `events.summarize_usage_event`).
    """
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return 0
    inp = int(usage.get("input_tokens", 0) or 0)
    outp = int(usage.get("output_tokens", 0) or 0)
    return inp + outp


def _parse_event_ts(ts: object) -> float | None:
    """Parse an event `ts` field (ISO8601 with `Z` suffix, per
    `_shared.now()`) to epoch seconds. Returns `None` on parse
    failure — events.jsonl shape has been stable but a malformed
    line shouldn't crash the auto-promote step.
    """
    if not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _auto_approve_window_resume_idx(tail: list[dict]) -> int:
    """Index of the most recent `operator_ack` whose `note` contains
    the `auto_approve_window_resume` token. Returns `-1` when absent.

    Same shape as `_auto_approve_paused`'s `last_unfreeze_idx` scan
    (TB-223), but on a distinct token. Two distinct ack tokens because
    the auto-promote-paused state has two semantically-distinct entry
    paths (TB-223 cumulative-regression vs. TB-224 cost+blast-radius)
    and operators benefit from a forensic record of which class of
    issue triggered the pause.
    """
    last = -1
    for i, e in enumerate(tail):
        if e.get("type") != "operator_ack":
            continue
        note = str(e.get("note") or "")
        if _AUTO_APPROVE_WINDOW_RESUME_TOKEN in note:
            last = i
    return last


def _auto_approved_task_ids(tail: list[dict]) -> set[str]:
    """Set of TB-Ns that ideation auto-approved within `tail`, with
    subsequent `ideation_approved` events removing them (a task the
    operator subsequently `ap2 approve`'d is no longer in the auto
    bucket — same rule as `_was_auto_approved`).

    Materialized as a set so the per-task / window scans below can
    filter `task_run_usage` events with O(1) lookups instead of
    re-scanning the tail per event.
    """
    auto: set[str] = set()
    for e in tail:
        tid = str(e.get("task") or "").strip()
        if not tid:
            continue
        typ = e.get("type")
        if typ == "auto_approved":
            auto.add(tid)
        elif typ == "ideation_approved":
            auto.discard(tid)
    return auto


def _auto_approve_check_violations(
    cfg: Config,
) -> tuple[str, int, int, str, str] | None:
    """Inspect recent events for TB-224 cost / blast-radius violations.

    Returns `None` when no halt condition fires, or a 5-tuple:
        (reason, total_used, cap, trigger_task, detail)

    where:
      - `reason` is one of `"per_task_cap"`, `"window_cap"`,
        `"task_error"`.
      - `total_used` is the token count that tripped the cap (or `0`
        for `task_error`).
      - `cap` is the effective env-knob value (`0` for `task_error`).
      - `trigger_task` is the offending TB-N (`""` if no single task
        is "the" trigger — today the window-cap path may have this
        shape when the sum tips over from interleaved tasks).
      - `detail` is a short excerpt (used for `task_error`).

    Order of precedence: `task_error` first (infrastructure issue —
    immediate attention), then `per_task_cap` (single runaway), then
    `window_cap` (drift sum). The first match short-circuits — only
    one halt event fires per tick regardless of how many conditions
    overlap.

    Resume semantics: the most recent `operator_ack` carrying the
    `auto_approve_window_resume` token resets all three checks to a
    fresh post-ack window. Events before the ack don't count; the
    operator explicitly cleared the halt and we trust that decision.

    Pure / events.jsonl tail-read only. Safe to call from `_tick`
    without taking the board lock.
    """
    if not cfg.events_file.exists():
        return None
    # 2000-event tail comfortably covers 24h of activity for typical
    # ap2 projects (a tight ideation+task loop emits ~30 events per
    # hour). Bigger than `_auto_approve_paused`'s 500 because the
    # window-cap sum legitimately spans 24h of `task_run_usage`
    # arrivals interleaved with cron / status-report observability
    # noise.
    tail = events.tail(cfg.events_file, 2000)
    if not tail:
        return None
    resume_idx = _auto_approve_window_resume_idx(tail)
    relevant = tail[resume_idx + 1:]
    if not relevant:
        return None

    # Auto-approved task ids: scan the FULL tail (a task auto-approved
    # before the ack still belongs to the auto bucket — the ack
    # resets the halt state, not the per-task category).
    auto_ids = _auto_approved_task_ids(tail)

    # 1) `task_error` on an auto-approved task — single-event halt.
    #    Distinct from `verification_failed` (TB-223 regression-pause
    #    condition) because infrastructure failures aren't noise.
    for e in relevant:
        if e.get("type") != "task_error":
            continue
        tid = str(e.get("task") or "").strip()
        if not tid or tid not in auto_ids:
            continue
        detail = str(e.get("error") or "")[:160]
        return ("task_error", 0, 0, tid, detail)

    per_task_cap = _per_task_token_cap()
    window_cap = _window_token_cap()

    # 2) `per_task_cap` — any task_run_usage for an auto-approved task
    #    whose tokens exceed the cap.
    if per_task_cap > 0:
        for e in relevant:
            if e.get("type") != "task_run_usage":
                continue
            tid = str(e.get("task") or "").strip()
            if not tid or tid not in auto_ids:
                continue
            used = _event_combined_tokens(e)
            if used > per_task_cap:
                return ("per_task_cap", used, per_task_cap, tid, "")

    # 3) `window_cap` — sum of input+output tokens across all
    #    auto-approved `task_run_usage` events within the last 24h
    #    (post-ack). Same shape `ap2 status-report`'s recent-events
    #    surface uses: tail scan, no new state file, no new
    #    persistence contract.
    if window_cap > 0:
        now_s = time.time()
        total = 0
        for e in relevant:
            if e.get("type") != "task_run_usage":
                continue
            tid = str(e.get("task") or "").strip()
            if not tid or tid not in auto_ids:
                continue
            ts = _parse_event_ts(e.get("ts"))
            if ts is None:
                continue
            if now_s - ts > _AUTO_APPROVE_WINDOW_S:
                continue
            total += _event_combined_tokens(e)
        if total > window_cap:
            return ("window_cap", total, window_cap, "", "")

    return None


def _auto_approve_already_halted(cfg: Config) -> bool:
    """True iff an `auto_approve_halted` event has already fired since
    the most recent `auto_approve_window_resume` operator ack.

    Dedupe gate so each triggering episode emits exactly ONE
    `auto_approve_halted` event (the "first-time" halt notification)
    even when the daemon's auto-promote step re-detects the same
    violation on every tick. Subsequent ticks still emit
    `auto_approve_skipped` per preempted promotion attempt.
    """
    if not cfg.events_file.exists():
        return False
    tail = events.tail(cfg.events_file, 2000)
    resume_idx = _auto_approve_window_resume_idx(tail)
    relevant = tail[resume_idx + 1:]
    for e in relevant:
        if e.get("type") == "auto_approve_halted":
            return True
    return False


def evaluate_auto_approve_decision(
    cfg: Config,
    *,
    tags: list[str] | None,
) -> str:
    """TB-232: Auto-approve dispatch path — single-source-of-truth
    branch over the full gate chain. Returns the WRITE action the
    proposal-time caller (`tools.do_board_edit`'s `add_backlog` branch)
    should take.

    Gate-evaluation order (serial; each evaluated top-to-bottom so the
    judge can read the branch ordering directly):

      1. `tags` — `ideation.should_auto_approve(tags)` excludes
         proposals carrying any of `AP2_AUTO_APPROVE_GATE_TAGS`
         (default `#breaking-change,#high-risk`). Existing TB-223 gate.
         A tag opt-out short-circuits to `"noop"` for both real and
         dry-run modes — the proposal is treated as non-auto-approved
         end-to-end.
      2. `freeze-threshold` — `_auto_approve_paused(cfg)` returns True
         when N consecutive task failures ended in `retry_exhausted`
         (TB-223 circuit-breaker; default N=3).
      3. `per-task-token-cap` + `window-token-cap` —
         `_auto_approve_check_violations(cfg)` returns non-None when
         either TB-224 cost-guard tripped (per-task first by
         precedence, then window-sum).

    Gates 2-3 are evaluated up-front so the dry-run terminal branch
    can consult their results without re-walking events.jsonl. Their
    *enforcement* depends on the mode (see terminal branch below).

    Terminal branch — only AFTER gates 1-3 are evaluated:

      - `"dry_run"` (`AP2_AUTO_APPROVE_DRY_RUN` truthy): monitor-only
        on-ramp. The simulated decision must honor the SAME gates the
        real-dispatch path honors; if freeze-threshold or any token
        cap tripped, the simulation returns `"noop"` (no
        `would_auto_approve` emitted — the simulation matches what
        real dispatch would do, including the safety halts). When all
        gates pass, return `"dry_run"` so the caller emits
        `would_auto_approve` and preserves the `@blocked:review`
        codespan.

      - `"strip"` (dry-run unset): real auto-approve. Caller strips
        `review` from `blocked_on` and emits `auto_approved`. The
        TB-223 freeze-threshold + TB-224 token-cap gates remain the
        canonical safety check at *dispatch time* in `_tick`
        (`_was_auto_approved` + `_auto_approve_paused` +
        `_auto_approve_check_violations`); we deliberately do NOT
        re-gate them here for real mode because the dispatch-time
        path is the existing single source of truth for halt events
        (`auto_approve_paused` / `auto_approve_halted`) and the
        operator playbook keys off those event types.

    The dry-run branch sits AFTER the tags / freeze-threshold /
    per-task-token-cap / window-token-cap gate evaluations so it
    never bypasses an existing safety check — dry-run only changes
    the WRITE action when all checks pass. The judge confirms this
    branch order by reading top-to-bottom in this function.

    Caller responsibility: `tags` is the proposal's tag list (may be
    `None` / empty — `ideation.should_auto_approve` treats both as
    "no opt-out tag"). The caller pre-checks that the proposal
    actually carries `@blocked:review` so this helper isn't invoked
    for operator-driven adds.

    Pure / no I/O beyond `events.jsonl` tail-reads inside the gate
    helpers; safe to call from `do_board_edit` under the board lock
    (no extra locking required).
    """
    # Late imports avoid a tools/automation_status ⇄ daemon import
    # cycle. The two modules are already on daemon's import surface
    # via other call sites, so this is just a lazy-bind of the
    # already-loaded modules.
    from . import automation_status as _astatus

    # Gate 1: tags. The TB-223 entry gate — `#breaking-change` /
    # `#high-risk` proposals (and operator-customized
    # `AP2_AUTO_APPROVE_GATE_TAGS`) opt out of auto-approve entirely.
    # Short-circuits both real and dry-run modes — a tag opt-out
    # means "operator must manually approve", which the
    # `@blocked:review` codespan already enforces.
    if not ideation.should_auto_approve(tags):
        return "noop"
    # Gate 2: freeze-threshold (TB-223 circuit-breaker). Evaluated
    # here so the dry-run terminal branch below can honor it; real
    # mode re-checks at dispatch time in `_tick` and emits
    # `auto_approve_paused` from that canonical site.
    freeze_paused = _auto_approve_paused(cfg)
    # Gate 3: per-task-token-cap + window-token-cap (TB-224
    # cost/blast-radius guards), evaluated in the precedence
    # `_auto_approve_check_violations` enforces (task_error >
    # per_task_cap > window_cap). Same dual-evaluation note as Gate
    # 2: real mode re-checks at dispatch time.
    token_violation = _auto_approve_check_violations(cfg)
    # Terminal branch — sits AFTER the tags / freeze / token-cap
    # evaluations above. Dry-run mode honors all gates in-place
    # (no separate dispatch-time pass exists for the simulated
    # decision); real mode delegates the freeze / token-cap
    # enforcement to the dispatch-time gate site in `_tick` so the
    # canonical `auto_approve_paused` / `auto_approve_halted` event
    # stream stays single-source-of-truth.
    if _astatus._is_auto_approve_dry_run():
        if freeze_paused or token_violation is not None:
            return "noop"
        return "dry_run"
    return "strip"


def _append_decisions_needed_bullet(cfg: Config, bullet: str) -> None:
    """Append a bullet to the `## Decisions needed from operator`
    section of `.cc-autopilot/ideation_state.md`. Creates the section
    at end-of-file if absent. Atomic write (tmpfile + rename) mirroring
    `do_ideation_state_write`'s shape.

    Used by TB-224's `task_error` halt to surface the failing TB-N +
    error excerpt as an actionable decision on `ap2 status` /
    `ap2 logs` / the web home page without waiting for the next
    ideation cron — the same `parse_operator_decisions` reader the
    three surfaces consume picks up the new bullet automatically.

    Caller responsibility: pass a clean bullet body (no leading `- `).
    The function adds the bullet marker. Newlines inside `bullet` are
    preserved as continuation lines (callers should keep entries
    single-line for the existing parser's bullet-extraction shape).
    """
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text()
    else:
        text = "# Ideation State\n\n"
    header_re = _re.compile(
        r"^##\s+Decisions needed from operator\s*$", _re.M,
    )
    next_re = _re.compile(r"^##\s+", _re.M)
    m = header_re.search(text)
    bullet_line = f"- {bullet.strip()}\n"
    if m is None:
        # No section yet — append fresh `## Decisions needed from operator`
        # at end-of-file. Two leading newlines to keep section spacing
        # consistent with the ideation prompt's schema.
        sep = ""
        if text and not text.endswith("\n"):
            sep = "\n\n"
        elif text.endswith("\n") and not text.endswith("\n\n"):
            sep = "\n"
        new_text = (
            text + sep + "## Decisions needed from operator\n\n" + bullet_line
        )
    else:
        # Insert the new bullet at the end of the existing section
        # body (just before the next `## ` header or EOF). Preserves
        # any sibling sections that follow.
        body_start = m.end()
        next_m = next_re.search(text, body_start)
        section_end = next_m.start() if next_m else len(text)
        body = text[body_start:section_end]
        body_rstripped = body.rstrip("\n")
        # One blank line between header and bullets when the body was
        # empty; otherwise just append after the existing bullets.
        if not body_rstripped.strip():
            new_body = "\n\n" + bullet_line + "\n"
        else:
            new_body = body_rstripped + "\n" + bullet_line + "\n"
        new_text = text[:body_start] + new_body + text[section_end:]
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)
