"""Append-only event log. Each line is a JSON object with at least `ts` and `type`.

Events are the shared awareness mechanism in v2: every `query()` call receives
the last N events as context, so stateless agents can reconstruct recent history
without accumulating it in any long-lived session.

Event-type catalog: emitters across `ap2/*.py` call `events.append(events_file,
"<type>", ...)` with a fixed string literal. Notable recent additions:
  - `auto_approved` (TB-223) — ideation-proposed row landed without
    `@blocked:review` because `AP2_AUTO_APPROVE` is on and the task
    doesn't carry any `AP2_AUTO_APPROVE_GATE_TAGS` tag. Audit-trail
    event so `ap2 logs` and the cron status-report surface what
    auto-approval shipped without operator review. Payload: `task`
    (TB-N) + `knob` (env value at emit time, for forensic trail).
  - `auto_approve_paused` (TB-223) — cumulative-regression
    circuit-breaker tripped; the daemon halted auto-promotion of
    auto-approved Backlog tasks until the operator emits
    `ap2 ack auto_approve_unfreeze`. Payload: `task`, `threshold`,
    `reason` (descriptive sentence). Counterpart `operator_ack` event
    with a note containing `auto_approve_unfreeze` resets the
    failure window.
  - `auto_approve_halted` (TB-224) — one-shot halt notification when a
    cost / blast-radius guard tripped:
    `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` exceeded (single runaway
    task), `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` exceeded (24h-rolling
    drift), or a `task_error` event landed for an auto-approved task
    (infrastructure failure — distinct from `verification_failed`).
    Payload: `task` (trigger TB-N), `reason` (one of `per_task_cap` /
    `window_cap` / `task_error`), plus `used` / `cap` / `window_used`
    / `error_excerpt` per reason. Counterpart `operator_ack` event
    with a note containing `auto_approve_window_resume` clears the
    halt for both window-cap and task-error reasons (one ack covers
    both since they share the same auto-promote-paused state).
  - `auto_approve_skipped` (TB-224) — per-tick "would have promoted
    but a cap intervened" event, fired once per preempted promotion
    attempt while a halt is active. Payload: `task` (the would-have-
    promoted TB-N), `reason` (matches the active `auto_approve_halted`
    event's reason). TB-272 added a new payload variant fired by the
    axis-1+3 cross-cut safety-floor pause when the rolling-24h
    `validator_judge_fail` + `validator_judge_timeout` sum crosses
    `AP2_VALIDATOR_JUDGE_NOISY_THRESHOLD` (default 5; TB-243
    calibration): `reason="validator_judge_noisy"` plus
    `fail_count_24h` (int), `timeout_count_24h` (int), `threshold`
    (int — the resolved knob value at emit time). No counterpart
    `auto_approve_halted` event for this variant — the noisy state is
    count-derived (not event-driven), self-clears as old events age
    out of the 24h window, and reuses the existing
    `auto_approve_unfreeze` ack verb. Opt-out:
    `AP2_AUTO_APPROVE_NOISY_PAUSE_DISABLED=1` restores the pre-TB-272
    cosmetic-only TB-243 behavior (status surface still surfaces the
    `[noisy]` badge but dispatch is not gated).
  - `would_auto_approve` (TB-232) — monitor-only dry-run sibling of
    `auto_approved`. Fires at proposal-emission time when both
    `AP2_AUTO_APPROVE=1` AND `AP2_AUTO_APPROVE_DRY_RUN=1` are set and
    the tags gate would have stripped `@blocked:review`. The codespan
    is preserved (operator-manual `ap2 approve` still required).
    Payload: `task` (TB-N), `knob` (env value at emit time, mirrors
    `auto_approved`), `dry_run=True` (discriminator field so the 24h
    counter aggregator + offline tooling can parse both event streams
    together without ambiguity). The operator runs in dry-run for
    ≥24h, reads the `would_auto_approve` event stream + the
    `would_auto_approve_count_24h` counter on `ap2 status` to confirm
    the gate's decisions match their judgment, then unsets the
    dry-run knob to engage real dispatch.
  - `auto_unfreeze_applied` (TB-225) — agent-diagnosed briefing-shape
    fix was auto-applied to a Frozen task. The daemon parsed a
    `BriefingFix: <shape> at <path>:<line>: <from> -> <to>` line from
    the agent's most recent `task_complete status=blocked` summary,
    verified the named line literally matches `from`, queued an
    `update` op (briefing patch) + an `unfreeze` op (Frozen →
    Backlog) on the operator queue, and emitted this event for the
    audit trail. Payload: `task` (TB-N), `shape` (allowlist token),
    `from`, `to`. Counterpart `task_updated` (TB-153) + `task_unfrozen`
    events land on next-tick drain.
  - `auto_unfreeze_skipped` (TB-225) — auto-unfreeze attempt was
    refused at one of the layered guards. Payload: `task` (TB-N
    when scoped to a task; absent for the `sweep_error` reason
    which is daemon-wide), `reason` (one of
    `shape_not_in_allowlist`, `briefing_mismatch`,
    `briefing_path_missing`, `per_task_cap`, `per_day_cap`,
    `queue_error`, `sweep_error`). The `knob_unset` case does NOT
    emit per-tick — the feature is opt-in and operators who haven't
    set `AP2_AUTO_UNFREEZE_FIX_SHAPES` shouldn't see noise.
  - `would_auto_unfreeze` (TB-233) — monitor-only dry-run sibling
    of `auto_unfreeze_applied`. Fires when both
    `AP2_AUTO_UNFREEZE_FIX_SHAPES` (non-empty) AND
    `AP2_AUTO_UNFREEZE_DRY_RUN=1` are set and the full guard chain
    (allowlist + per-task cap + per-day cap + briefing-line match)
    would have passed. The briefing file is NOT mutated and no
    operator-queue ops are appended; per-day-count + per-task-prior
    counters do NOT increment in dry-run (no real application). The
    payload mirrors `auto_unfreeze_applied` plus the
    `file` + `line` fields from the parsed `BriefingFix:` prefix:
    `task` (TB-N), `shape` (allowlist token), `file` (briefing
    path), `line` (1-indexed line number), `from`, `to`. Operator
    runs the dry-run window to confirm the loop's decisions match
    their judgment on the live Frozen set, then unsets the dry-run
    knob to engage real patching. Sibling on-ramp to TB-232's
    `would_auto_approve` on the axis-1 side.
  - `validator_judge_timeout` / `validator_judge_fail` (TB-235) —
    fail-open audit events from the LLM-driven dependency-coherence
    check (validator check #7 in `tools._validate_briefing_structure`).
    Fires when the Haiku-4.5 judge's SDK call exceeds
    `AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default 15s) or fails for any
    other reason (network, parse error, model unavailable). The
    validator's policy on judge failure is fail-open — refusing to
    gate `ap2 add` / `ap2 update` on a transient Anthropic API
    hiccup is the load-bearing trade-off — but the operator needs to
    notice if the skip rate climbs, so each skipped call lands as an
    event. Payload: `validator_judge_timeout` carries `timeout_s` +
    `error`; `validator_judge_fail` carries `error` (the exception
    repr or "non-dict judge response"). Counterpart cron status-
    report (TB-228) surfaces skip counts so a rising rate prompts
    operator triage. Counter-event-of-record for the
    `AP2_VALIDATOR_JUDGE_DISABLED` operator escape hatch (when set,
    the check is bypassed entirely and neither event fires).
  - `focus_advanced` (TB-226) — daemon advanced its in-memory focus
    pointer past an exhausted `## Current focus:` heading in
    goal.md. TB-283: triggered solely by the empty-cycles
    heuristic (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES` consecutive
    0-proposal ideation cycles against the active focus). The
    prior LLM-judge path against `Done when:` bullets was deleted
    because it collapsed multi-week foci into ~3-task cycles by
    diff-reading commit shape. Payload: `from` (old focus
    title), `to` (new focus title, empty string when the advance
    crossed the last focus into roadmap-exhausted state),
    `trigger` (`empty_cycles_heuristic`), `new_index` (the
    pointer's new `active_index`), `total_foci` (current foci-
    list length).
  - `roadmap_complete` (TB-226) — focus pointer has advanced past
    the last `## Current focus:` heading in goal.md. TB-275:
    ideation parks on subsequent ticks (`_maybe_ideate` skips with
    `reason=roadmap_complete`) until the operator extends the
    roadmap (adding new `## Current focus:` headings via
    `ap2 update-goal`) OR emits `ap2 ack roadmap_complete` to
    dismiss the notice. Task dispatch is NOT affected; already-
    queued Backlog tasks continue to drain. `ap2 pause` remains
    the explicit full-stop verb. Payload: `exhausted_count`
    (the foci-list length at exhaustion), `trigger`
    (`pointer_past_last`). Fired once per exhaustion episode; the
    `_maybe_advance_focus` pass suppresses re-emission via the
    pointer's `roadmap_complete_emitted` flag, which resets on
    the next advance after the operator extends the roadmap.
  - `env_reloaded` (TB-271) — daemon `_tick` re-sourced
    `.cc-autopilot/env` at tick-top and detected at least one knob
    whose value changed since the last reload. Mutates the tunable
    `Config` dataclass fields in-place (timeouts, max-turns,
    `verify_cmd`, tick intervals) and overwrites `os.environ` for
    file-sourced keys, preserving the "shell export wins" contract
    for keys never set by the file. Payload: `changed` (sorted list
    of all knob names whose value differs from the prior value),
    `hot` (subset in `env_reload.HOT_RELOADABLE_KNOBS` — take effect
    on this tick), `fixed` (subset in `env_reload.FIXED_KNOBS` —
    require `ap2 stop && ap2 start` to apply, e.g. `AP2_WEB_PORT`,
    `AP2_MM_CHANNELS`), `other` (anything not in either set;
    treated conservatively — TB-260 stale-warning stays live).
    Removes the restart-to-apply-a-knob friction TB-260 only warned
    about (TB-255 ran ~26h against the old 600s verify ceiling because
    `AP2_VERIFY_TIMEOUT_S` had been bumped but the daemon hadn't
    restarted). Mtime-gated: a touch that doesn't change any value
    is silent — event is only emitted when at least one key's value
    actually differs.
  - `env_reload_error` (TB-271) — `env_reload.maybe_reload_env` raised
    an exception at tick-top (parse failure / state-file write error
    / OS error on the env file). The daemon swallows the exception
    so the tick continues on whatever cfg state survived; the event
    surfaces the failure shape for operator triage. Payload: `error`
    (`<ExceptionType>: <message>`).
  - `verify_passed` (TB-252) — project-wide `AP2_VERIFY_CMD` ran to
    completion AND exited zero (the successful sibling of
    `verification_failed`). Emitted from daemon.py's
    post-`_run_verify` success branch on both the synchronous task
    path and the pipeline-pending re-verify path. Payload: `task`
    (TB-N owning the run), `command` (the resolved verify command),
    `exit_code` (0 by contract — kept in the payload so the shape
    mirrors `verification_failed` for symmetric tooling),
    `duration_s` (wall-clock seconds), optional `source`
    (`pipeline_pending` on the async path). Consumed by
    `verify_timeout_audit` in `ap2/doctor.py` to size
    `AP2_VERIFY_TIMEOUT_S` (default 600s) against the
    observed-typical successful run duration — anchors the doctor
    WARN that goal.md axis-2 calls for on env-knob-vs-workload
    drift (TB-245/246/247/249/250 cascade).
  - `validator_judge_passed` (TB-269) — TB-235 dep-coherence judge
    SDK call completed without timeout / SDK exception (the
    successful sibling of `validator_judge_timeout` /
    `validator_judge_fail`). Emitted from
    `ap2.validator_judge._judge_dep_coherence_default` just after
    the worker thread returns successfully, BEFORE the JSON parse —
    a parse-failure call still spent the same wall-clock against the
    SDK and that cost matters for sizing
    `AP2_VALIDATOR_JUDGE_TIMEOUT_S`. Payload: `duration_s`
    (wall-clock seconds), `briefing_bytes` (UTF-8 byte length of the
    briefing payload — feeds future prompt-shape investigations),
    `max_turns` (the resolved SDK turn budget at call time),
    `timeout_s` (the resolved timeout knob at call time). Consumed
    by `validator_judge_timeout_audit` in `ap2/doctor.py` (axis-1
    mirror of `verify_timeout_audit`) to size
    `AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default 60s per TB-269; bumped
    from 15s after the TB-257 artifact measured the real SDK call at
    17.6-46.8s wall-clock) against the observed-typical successful
    call duration — completes the happy-path/fail-open/timeout
    triangle on a single namespace so an operator can see the gate's
    true firing rate, not just the failure subset TB-243 surfaces.
  - `attention_raised` (TB-282) — `ap2/attention.py`'s
    `detect_attention_conditions(cfg)` surfaced a condition that
    warrants immediate operator attention; the daemon's per-tick
    wire-up (`_maybe_emit_attention_events` in `daemon.py`) debounced
    against any prior matching fire within `AP2_ATTENTION_DEBOUNCE_S`
    (default 21600 / 6h) and emitted this event for each fresh
    condition. Per-(attention_type, key) debounce so a second stuck
    task doesn't get suppressed because a first one fired recently.
    Payload: `attention_type` (detector identifier — `task_stuck` is
    the only one seeded today; future detectors land alongside as
    `validator_judge_noisy` / `cost_cap_approach` / etc.), `key`
    (per-condition dedup key — e.g. `task_stuck:TB-N`), `summary`
    (one-line operator-legible string the status-report renderer
    surfaces), plus a detector-specific extras blob inlined into the
    payload (`task_stuck` carries `task`, `title`, `age_s`,
    `start_ts`, `threshold_s`). The status-report renderer
    (`render_attention_section` in `ap2/status_report.py`) reads the
    still-active conditions on each cron tick and emits one bullet
    per condition under a distinct `## Attention needed` section the
    agent forwards VERBATIM into the Mattermost post — positioned
    BEFORE the routine progress bullets so the walk-away operator
    sees the attention signal first. `attention_raised` is listed in
    `IDEATION_RELEVANT_EVENT_TYPES` (so ideation sees fresh attention
    events in its prompt tail and can reason against them next
    cycle) AND in `_STATUS_REPORT_AUTOMATION_INTERESTING_TYPES` (so a
    fresh fire un-skips the dedup/idle gate, parallel to the
    TB-244 / TB-245 pattern). Closes goal.md focus-1's Done-when
    bullet on shallow monitoring.
  - `ideation_state_scrubbed` (TB-284) — `_run_ideation`'s post-write
    scrub stripped exhaustion-asserting sentences from
    `ideation_state.md` after the ideation control-agent finished
    writing it. Trigger: each ideation cycle (natural-cron or forced)
    runs `ideation_scrub.scrub_exhaustion_language` on the just-
    written file; the event fires ONLY when the scrubbed text differs
    from the agent's original (silent no-op on already-clean files,
    which is the steady-state path once the scrub has trained the
    file's content shape). Payload: `removed_chars` (int, byte
    length delta — always positive in steady state since the scrub
    only deletes sentences). Closes the goal.md `## Done when`
    failure mode "ideation reliably proposes goal-aligned next steps
    that substantively advance the goal (not just goal-shaped
    pro-forma compliance)" by removing the verdict-language anchor
    that primed the next cycle toward repeating "we're nearly done"
    framing. See `ap2/ideation_scrub.py` for the prompt contract and
    the fail-safe-by-returning-input-unchanged design.
  - `cron_skipped` (TB-128 + TB-281) — status-report cron run was
    suppressed pre-flight. Carries `job="status-report"`, `trigger`
    (`cron` or `chat`), and a `reason` field naming which gate
    fired:
      - `no_activity_since_last_report` (TB-128): the inter-report
        window carries zero "interesting" events past the previous
        `cron_complete name=status-report`.
      - `duplicate_content` (TB-281): events DID land in the window
        but the prospective post is structurally identical to the
        last one (board counts + pending-review TB-Ns + decisions-
        needed bullets + digest sub-sections + halt reason all
        unchanged from the fingerprint stashed under
        `status-report.last_post_fingerprint` in `cron_state.json`).
        Closes goal.md focus-1's Done-when bullet "no two consecutive
        reports repeat unchanged content".
    Chat-trigger paths additionally carry `chat_reason` (the operator-
    supplied trigger justification) so audit trails preserve the
    invocation cause distinct from the suppression cause.

The full canonical list lives in `ap2/howto.md`'s `## Event schema`
section — `test_every_event_type_documented` (`ap2/tests/test_docs_drift.py`)
and `test_every_event_type_has_test_reference`
(`ap2/tests/test_coverage_drift.py`) gate that emitted types stay
documented and tested.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterable

from ap2._shared import now, short


def append(events_file: Path, type: str, **fields: Any) -> dict:
    """Append an event; returns the event dict actually written."""
    events_file.parent.mkdir(parents=True, exist_ok=True)
    evt = {"ts": now(), "type": type, **fields}
    line = json.dumps(evt, default=str)
    fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (line + "\n").encode())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return evt


def tail(events_file: Path, n: int = 50) -> list[dict]:
    """Return the last `n` events as dicts (oldest first)."""
    if not events_file.exists():
        return []
    lines = _tail_lines(events_file, n)
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _tail_lines(path: Path, n: int) -> list[str]:
    """Efficient tail: read backwards in blocks until we have n newlines."""
    block = 8192
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            read = min(block, size)
            size -= read
            f.seek(size)
            data = f.read(read) + data
    lines = data.decode(errors="replace").splitlines()
    return lines[-n:]


def format_for_prompt(events: Iterable[dict], *, max_chars: int = 6000) -> str:
    """Render events as a compact string suitable for a prompt block."""
    rendered = []
    total = 0
    for e in events:
        ts = e.get("ts", "")
        typ = e.get("type", "?")
        extras = {k: v for k, v in e.items() if k not in ("ts", "type")}
        extra_str = " ".join(f"{k}={short(v, 200)}" for k, v in extras.items())
        line = f"{ts} {typ} {extra_str}".rstrip()
        total += len(line) + 1
        if total > max_chars:
            break
        rendered.append(line)
    return "\n".join(rendered)


# TB-158: shared formatter for `verification_failed` events. Both
# `ap2 logs` (CLI) and `ap2/web.py` (events table + task-run detail page)
# call this so the per-bullet summary, sort order, and truncation rules
# stay in lockstep — the surface-specific layer only handles ANSI vs HTML
# and chooses truncation lengths via the kwargs.
#
# Sort order: failed > unverified > pass within `failed_bullets` (only
# `fail` is included today; the buckets are listed for callers that want
# them). Within failed, source order is preserved so the rendering order
# matches the briefing's `## Verification` bullet order.
def summarize_verification_failed(
    event: dict,
    *,
    max_bullet: int = 240,
    max_note: int = 400,
) -> dict:
    """Compact, surface-agnostic summary of a `verification_failed` event.

    Returns a dict with:
        summary_line     "5/8 passed, 2 failed, 1 unverified" (or fallback)
        failed_bullets   list of {kind, bullet, notes} — fail-status only,
                         truncated per the max_* kwargs.
        pass_count       int
        fail_count       int
        unverified_count int
        total            int (sum of the three; 0 for legacy events)

    Two flavours of the event exist on disk today:
      - per-task (briefing-driven) — carries `criteria=[{kind, status,
        bullet, notes}, ...]`. We score and render from that list.
      - project-wide gate — carries `command`, `exit_code`, `stderr_tail`
        and NO `criteria`. We synthesize a single failed bullet from
        `command` + `stderr_tail` so the renderer still has something
        meaningful to display.

    Events with no recognizable structure (e.g. very old or hand-written
    test fixtures) return the empty fallback `pass=0, fail=0, total=0,
    failed_bullets=[]` rather than raising — operators reading old
    events.jsonl shouldn't see the page break on a missing field.
    """
    criteria = event.get("criteria")
    if not isinstance(criteria, list):
        cmd = str(event.get("command") or "").strip()
        if cmd:
            stderr = str(event.get("stderr_tail") or "").strip()
            return {
                "summary_line": (
                    f"project-wide verification failed "
                    f"(exit {event.get('exit_code', '?')})"
                ),
                "failed_bullets": [{
                    "kind": "project_gate",
                    "bullet": _truncate(cmd, max_bullet),
                    "notes": _truncate(stderr, max_note),
                }],
                "pass_count": 0,
                "fail_count": 1,
                "unverified_count": 0,
                "total": 1,
            }
        return {
            "summary_line": "verification failed (no criteria captured)",
            "failed_bullets": [],
            "pass_count": 0,
            "fail_count": 0,
            "unverified_count": 0,
            "total": 0,
        }

    def _status(c: Any) -> str:
        if not isinstance(c, dict):
            return ""
        return str(c.get("status") or "").strip().lower()

    pass_count = sum(1 for c in criteria if _status(c) == "pass")
    fail_count = sum(1 for c in criteria if _status(c) == "fail")
    unverified_count = sum(1 for c in criteria if _status(c) == "unverified")
    total = pass_count + fail_count + unverified_count

    failed_bullets = [
        {
            "kind": str((c or {}).get("kind") or ""),
            "bullet": _truncate(str((c or {}).get("bullet") or ""), max_bullet),
            "notes": _truncate(str((c or {}).get("notes") or ""), max_note),
        }
        for c in criteria
        if _status(c) == "fail"
    ]

    return {
        "summary_line": (
            f"{pass_count}/{total} passed, "
            f"{fail_count} failed, {unverified_count} unverified"
        ),
        "failed_bullets": failed_bullets,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "unverified_count": unverified_count,
        "total": total,
    }


def _truncate(s: str, limit: int) -> str:
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


# TB-179 / TB-180: shared compact formatter for the three usage-carrying
# event types — `judge_call`, `task_run_usage`, `control_run_usage`.
# Their verbose `usage` (and `model_usage`, `server_tool_use`,
# `cache_creation`, `service_tier`, etc.) blob, when dumped inline via
# the generic `_event_extra` / `short` field-dump path, wraps the row
# across several lines and drowns the at-a-glance signal both on the
# events page and in `ap2 logs`.
#
# Both `ap2/web.py::_compact_usage_row` and `ap2/cli.py::cmd_logs`
# consume this helper so the surfaces stay symmetric — an operator who
# reads the same event in `ap2 logs` and on `/events` sees the same
# 6-field tuple + identity prefix and muscle-memory scanning works
# across both. Same shared-helper pattern TB-158 used to keep
# `summarize_verification_failed` in lockstep across CLI and web.
#
# Shape: `<identity> · in=N out=N cc=N cr=N hit=N% $C · Ts` —
# six numeric fields (input_tokens, output_tokens,
# cache_creation_input_tokens, cache_read_input_tokens, total_cost_usd,
# duration_s; cache hit % is derived from the four token fields and
# rendered alongside) plus an event-type-specific identity prefix:
#   judge_call         task=TB-N bullet=N/<kind> <verdict>
#   task_run_usage     task=TB-N <status> run=<run_id>
#   control_run_usage  label=<label> <status> run=<run_id>
#
# Verbose nested fields (model_usage, server_tool_use, iterations,
# service_tier, inference_geo, the nested `cache_creation` object,
# etc.) drop from the inline string entirely; on the web they still
# live in the row's `<details>raw json</details>` toggle, and on the
# CLI operators wanting raw bytes use `ap2 logs --json`. No data loss.
_COMPACT_USAGE_EVENT_TYPES: frozenset[str] = frozenset({
    "judge_call",
    "task_run_usage",
    "control_run_usage",
})


def summarize_usage_event(
    event: dict,
    *,
    max_chars: int | None = None,
) -> str:
    """Compact, surface-agnostic one-line summary of a usage-carrying
    event (`judge_call`, `task_run_usage`, `control_run_usage`).

    Returns "" for events of any other type, OR for events of those
    types that carry no `usage` / `total_cost_usd` / `duration_s` to
    summarize. Callers typically check the return value and fall back
    to a generic field-dump renderer when it's empty.

    `max_chars` (optional) caps the returned string length, replacing
    the tail with `…`. Surfaces with tight width budgets (CLI on a
    narrow terminal) can pin a cap; the natural compact form is
    well under 200 chars on a real-world payload.
    """
    typ = str(event.get("type") or "")
    if typ not in _COMPACT_USAGE_EVENT_TYPES:
        return ""

    # Identity prefix — distinct fields per event type.
    parts: list[str] = []
    if typ == "judge_call":
        task = str(event.get("task") or "").strip()
        bidx = event.get("bullet_idx")
        bkind = str(event.get("bullet_kind") or "").strip()
        verdict = str(event.get("verdict") or "").strip()
        if task:
            parts.append(f"task={task}")
        if bidx is not None:
            bullet = f"{bidx}/{bkind}" if bkind else str(bidx)
            parts.append(f"bullet={bullet}")
        if verdict:
            parts.append(verdict)
    elif typ == "task_run_usage":
        task = str(event.get("task") or "").strip()
        status = str(event.get("status") or "").strip()
        run_id = str(event.get("run_id") or "").strip()
        if task:
            parts.append(f"task={task}")
        if status:
            parts.append(status)
        if run_id:
            parts.append(f"run={run_id}")
    elif typ == "control_run_usage":
        label = str(event.get("label") or "").strip()
        status = str(event.get("status") or "").strip()
        run_id = str(event.get("run_id") or "").strip()
        if label:
            parts.append(f"label={label}")
        if status:
            parts.append(status)
        if run_id:
            parts.append(f"run={run_id}")
    identity = " ".join(parts)

    # Token + cost summary (in/out/cc/cr/hit%/$cost). Mirrors the shape
    # of TB-157's `_event_token_summary` so the `?show=tokens` column
    # and the compact row carry identical numeric formatting.
    u = event.get("usage")
    cost = event.get("total_cost_usd")
    token_bits: list[str] = []
    if isinstance(u, dict):
        inp = int(u.get("input_tokens", 0) or 0)
        outp = int(u.get("output_tokens", 0) or 0)
        cc = int(u.get("cache_creation_input_tokens", 0) or 0)
        cr = int(u.get("cache_read_input_tokens", 0) or 0)
        denom = cr + cc + inp
        hit = (cr / denom * 100.0) if denom else 0.0
        token_bits.append(f"in={inp:,}")
        token_bits.append(f"out={outp:,}")
        token_bits.append(f"cc={cc:,}")
        token_bits.append(f"cr={cr:,}")
        token_bits.append(f"hit={hit:.1f}%")
    if isinstance(cost, (int, float)):
        token_bits.append(f"${float(cost):.4f}")
    token_summary = " · ".join(token_bits)

    # Duration.
    dur = event.get("duration_s")
    dur_str = f"{float(dur):.1f}s" if isinstance(dur, (int, float)) else ""

    bits = [b for b in (identity, token_summary, dur_str) if b]
    if not bits:
        return ""
    out = " · ".join(bits)
    if max_chars is not None and len(out) > max_chars:
        cap = max(0, max_chars - 1)
        out = out[:cap].rstrip() + "…"
    return out
