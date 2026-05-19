"""Review-surface CLI handlers (TB-264 split from `ap2/cli.py`).

Owns the operator-review / retrospective / signal-collection verbs:

  - `cmd_audit`              — retrospective review of unreviewed
                                Complete + Frozen tasks since the last
                                `ap2 audit` cursor (TB-248). Supports
                                table / JSON / `--interactive` walk
                                shapes.
  - `cmd_ack`                — queue an operator-decision line for the
                                daemon to append to
                                `.cc-autopilot/operator_log.md` at the
                                next tick (TB-106 / TB-201).
  - `cmd_rollback`           — linear walk-back along first-parent
                                history to a boundary commit + atomic
                                `git reset --hard` under the board
                                lock (TB-111).
  - `cmd_ideate`             — manually trigger an ideation pass on
                                the daemon's next tick (TB-159), bypassing
                                the natural empty-board / cooldown
                                gates.
  - `cmd_update_goal`        — refresh `goal.md` via the operator queue
                                (TB-193); operator-CLI-only by design.
  - `cmd_backfill_proposals` — one-off backfill of historical ideation
                                proposal records (TB-195).

Shared helpers `_cursor_label`, `_prompt_audit_action`,
`_prompt_impact_verdict`, `_queue_audit_run_cursor`, `_active_task_id`
live here too. `_active_task_id` is re-used by `cli_diagnostic.cmd_cron_edit`
via direct import — both call sites refuse mid-task to avoid racing the
agent's TB-110 snapshot window.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import audit, events, rollback, tools
from .board import Board, board_file_lock
from .cli_board import _read_briefing_file
from .config import Config


def _active_task_id(cfg: Config) -> str | None:
    """TB-202 helper: return the first Active task's ID, or None if
    Active is empty. Centralizes the board read; the literal "a task
    is currently active" refusal phrasing lives at each call site
    (`cmd_backfill_proposals` here and `cli_diagnostic.cmd_cron_edit`)
    so a future regression where one of the two refuse-if-active
    gates is dropped or weakened is grep-detectable.

    Why refuse-if-active rather than queue-routing both verbs: both
    are rare operations (`backfill-proposals` is a one-off historical
    seed; `cron edit` is operational tuning during project setup or
    cadence-adjust, not routine). Queue-routing has architectural
    overhead — register the op, add a drain-side handler, design the
    queue payload — worth it for frequent surfaces like `ap2 ack`
    (sibling TB-201) but not for these. The cheap mitigation: read
    the board once, refuse if Active is non-empty, point the
    operator at `ap2 status` and `ap2 pause` (with the caveat that
    pause doesn't abort in-flight tasks; it only stops dispatch of
    new ones).
    """
    board = Board.load(cfg.tasks_file)
    active = list(board.iter_tasks("Active"))
    return active[0].id if active else None


def _cursor_label(cursor: str | None) -> str:
    """Human-readable cursor for the `N unreviewed since <cursor>` line."""
    return cursor if cursor else "the beginning of time"


def _prompt_audit_action() -> str:
    """Return the single-letter action (`c` / `s` / `n` / `q`).

    Anything else is treated as `n` (next) by the caller — defensive
    against accidental enters / typos so an interactive session
    doesn't crash the walk.
    """
    try:
        raw = input("[c]lassify | [s]kip | [n]ext | [q]uit > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"
    return raw[:1] if raw else "n"


def _prompt_impact_verdict() -> str | None:
    """Prompt for one of `IMPACT_VERDICTS`. Returns None on invalid input
    so the caller can treat it as "skip to next without recording."
    """
    verdicts = list(tools.IMPACT_VERDICTS)
    print(f"  impact verdicts: {', '.join(verdicts)}")
    try:
        raw = input("  verdict > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw in verdicts:
        return raw
    print(f"  ! unknown verdict {raw!r}; skipping (no record written)",
          file=sys.stderr)
    return None


def _queue_audit_run_cursor(
    cfg: Config,
    unreviewed_count: int,
    *,
    reviewed: int | None = None,
    skipped: int | None = None,
    deferred: int | None = None,
) -> None:
    """Queue the `ran audit (...)` cursor line via the existing `ack`
    op-shape so the next `ap2 audit` invocation's cursor advances past
    this walk's completion timestamp.

    Per the briefing's Out-of-scope §7, we deliberately do NOT add a
    distinct `audit_run` op-shape — the `ack` op's free-form note
    field carries the structured `ran audit (...)` body. The audit
    cursor regex (`_RAN_AUDIT_RE` in `ap2/audit.py`) matches the
    operator_log.md line the `ack` drain produces.
    """
    if reviewed is not None:
        note = (
            f"ran audit (reviewed {reviewed}, "
            f"skipped {skipped or 0}, "
            f"deferred {deferred or 0})"
        )
    else:
        note = f"ran audit ({unreviewed_count} unreviewed)"
    res = tools.do_operator_queue_append(
        cfg, {"op": "ack", "note": note, "task_id": ""}
    )
    if res.get("isError"):
        print(
            f"warning: audit cursor write failed: "
            f"{res['content'][0]['text']}",
            file=sys.stderr,
        )


def cmd_audit(cfg: Config, args: argparse.Namespace) -> int:
    """Retrospective audit of unreviewed shipped tasks (TB-248).

    Default invocation lists every unreviewed Complete + Frozen task
    since the most recent `ran audit (...)` line in operator_log.md.
    `--interactive` walks the list one task at a time and prompts for
    `[c]lassify | [s]kip | [n]ext | [q]uit`. `--json` emits the same
    data as a machine-readable list. `--since <iso-date>` overrides the
    cursor; `--frozen-only` / `--auto-approved-only` filter the shape.

    State derivation is grep-only over `.cc-autopilot/operator_log.md`
    — no new state file is introduced. The audit cursor is the most
    recent `<ts> — ran audit (...)` line; the reviewed set is the
    union of `classified TB-N` / `audit-skipped TB-N` / `rejected
    TB-N` lines.

    The command WRITES nothing to disk directly. The cursor update
    (`ran audit (...)`) is queued via the existing `ack` op-shape; the
    `[s]kip` action queues via the new `audit_skip` op-shape (TB-248);
    the `[c]lassify` action queues via the existing `classify` op-shape
    by reusing `cmd_classify`'s handler. All three writes serialize
    through `do_operator_queue_append` so the daemon's drain stays the
    single owner of operator_log.md writes.

    Rollback as an interactive-prompt action is intentionally out of
    scope per the briefing (a follow-up TB will decide between
    walk-back-N / rollback-this-TB / revert-and-classify shapes). The
    operator can still `ap2 rollback` outside the audit walk.
    """
    cursor = args.since if args.since else audit.parse_audit_cursor(cfg)
    rows = audit.list_unreviewed(
        cfg,
        since=args.since,
        frozen_only=bool(args.frozen_only),
        auto_approved_only=bool(args.auto_approved_only),
    )

    if args.json:
        # Machine-readable shape. Mirrors `UnreviewedTask`'s dataclass
        # fields verbatim so a downstream dashboard tool can `.task_id`
        # / `.auto_approved` / etc. without translation. The cursor +
        # filter context goes alongside so the consumer doesn't have to
        # re-derive them.
        payload = {
            "cursor": cursor or "",
            "filter": {
                "frozen_only": bool(args.frozen_only),
                "auto_approved_only": bool(args.auto_approved_only),
                "since_override": bool(args.since),
            },
            "unreviewed": [
                {
                    "task_id": r.task_id,
                    "status": r.status,
                    "commit": r.commit,
                    "auto_approved": r.auto_approved,
                    "summary": r.summary,
                    "completed_at": r.completed_at,
                    "briefing_path": r.briefing_path,
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not rows:
        print(
            f"0 unreviewed since {_cursor_label(cursor)}; nothing to review"
        )
        _queue_audit_run_cursor(cfg, len(rows))
        return 0

    if not args.interactive:
        print(audit.format_table(rows))
        print()
        print(
            f"{len(rows)} unreviewed since {_cursor_label(cursor)}; "
            f"run `ap2 audit --interactive` to walk through"
        )
        _queue_audit_run_cursor(cfg, len(rows))
        return 0

    # --interactive: per-task walkthrough.
    reviewed_n = 0
    skipped_n = 0
    deferred_n = 0
    for i, r in enumerate(rows, 1):
        print()
        print(f"=== [{i}/{len(rows)}] {r.task_id} ({r.status}) ===")
        if r.completed_at:
            print(f"completed_at:  {r.completed_at}")
        if r.commit:
            print(f"commit:        {r.commit}")
        print(f"auto_approved: {'yes' if r.auto_approved else 'no'}")
        if r.briefing_path:
            print(f"briefing:      {r.briefing_path}")
        if r.summary:
            print(f"summary:       {r.summary}")
        print()
        choice = _prompt_audit_action()
        if choice == "q":
            break
        if choice == "n":
            deferred_n += 1
            continue
        if choice == "s":
            reason = input(f"  skip reason for {r.task_id} (optional): ").strip()
            res = tools.do_operator_queue_append(
                cfg,
                {
                    "op": "audit_skip",
                    "task_id": r.task_id,
                    "reason": reason,
                },
            )
            if res.get("isError"):
                print(
                    f"  ! queue-append failed: "
                    f"{res['content'][0]['text']}",
                    file=sys.stderr,
                )
                # Don't bump the counter on failure — operator can
                # re-decide next walk.
                continue
            skipped_n += 1
            print(f"  queued audit_skip {r.task_id}")
            continue
        if choice == "c":
            verdict = _prompt_impact_verdict()
            if verdict is None:
                # Invalid input: treat as "[n]ext", not a hard exit.
                deferred_n += 1
                continue
            reason = input(
                f"  classify reason for {r.task_id} (optional): "
            ).strip()
            res = tools.do_operator_queue_append(
                cfg,
                {
                    "op": "classify",
                    "task_id": r.task_id,
                    "verdict": verdict,
                    "reason": reason,
                },
            )
            if res.get("isError"):
                print(
                    f"  ! queue-append failed: "
                    f"{res['content'][0]['text']}",
                    file=sys.stderr,
                )
                continue
            reviewed_n += 1
            print(
                f"  queued classify {r.task_id} impact={verdict}"
            )
            continue
        # Any other key: treat as next.
        deferred_n += 1

    # End-of-walk cursor line. Captures the per-walk verb tallies so
    # operator_log.md tells the future audit what happened in this
    # session without re-grepping the per-op lines.
    print()
    print(
        f"audit walk complete: reviewed {reviewed_n}, "
        f"skipped {skipped_n}, deferred {deferred_n}"
    )
    _queue_audit_run_cursor(
        cfg,
        len(rows),
        reviewed=reviewed_n,
        skipped=skipped_n,
        deferred=deferred_n,
    )
    return 0


def cmd_ideate(cfg: Config, args: argparse.Namespace) -> int:
    """Manually trigger an ideation pass on the daemon's next tick (TB-159).

    Bypasses the natural empty-board / cooldown / `AP2_IDEATION_DISABLED`
    gates that govern `ideation._maybe_ideate`. Routed through the
    operator queue rather than spinning up the SDK from the CLI process
    so the daemon stays the single owner of the control-agent SDK slot
    (same pattern as `ap2 add` / `approve` / `reject` / `unfreeze` /
    `delete` / `update`).

    TB-194: the queue-append handler no longer rejects this op when a
    task happens to be Active. By the loop-topology invariant the
    drain runs as `_tick`'s first stage with Active already cleared
    by the previous tick's synchronous `run_task`, so the previously-
    feared concurrent-SDK interleaving is unreachable. `--force` is
    accepted as a no-op for the routing decision (audit-only metadata
    on the queue payload); the flag is preserved for one release so
    callers passing it don't break.

    The CLI is non-blocking: it returns immediately after the queue
    append; the daemon picks up the signal in the next tick (≤30s by
    default).
    """
    res = tools.do_operator_queue_append(
        cfg, {"op": "ideate", "force": bool(args.force)}
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print("queued ideate (will run at next tick — ≤30s)")
    return 0


def cmd_update_goal(cfg: Config, args: argparse.Namespace) -> int:
    """Refresh `goal.md` via the operator queue (TB-193).

    Routes through the operator queue rather than mutating goal.md in
    place because ideation reads the file mid-cycle (anchors injected
    into the prompt; `_goal_md_anchors` consulted at queue-append time
    for TB-161) and the per-task verifier (TB-69) reads it as part of
    the rollback-cohesion state surface — an in-place edit racing a
    snapshot-window write would tear against any of those readers. The
    queue-routed write lands at a tick boundary, under
    `board_file_lock`, in the same `state: drained N operator op(s)`
    commit as any co-staged ops.

    Symmetric to `ap2 add --briefing-file`: pass `--file <path>` to
    read the new goal content from a path, or `--file -` to read from
    stdin. `--reason` is optional and feeds the operator-log audit
    line `<ts> — operator updated goal.md (<reason>)` future ideation
    cycles read as a goal-drift signal.
    """
    try:
        content = _read_briefing_file(args.file)
    except OSError as e:
        print(f"ap2 update-goal: {e}", file=sys.stderr)
        return 1
    if not content.strip():
        print(
            "ap2 update-goal: --file is empty — refusing.\n"
            "  Pass a non-empty goal.md payload (whitespace-only is "
            "rejected).",
            file=sys.stderr,
        )
        return 1
    # Soft client-side cap so a runaway file doesn't get queued. The
    # daemon will accept whatever lands, but a goal.md > 100KB is
    # almost certainly a path-vs-content mistake; bail early.
    if len(content) > 100_000:
        print(
            f"ap2 update-goal: --file is {len(content)} bytes — "
            f"refusing (cap 100000). goal.md is meant to be a short "
            f"focus document; double-check you passed the goal "
            f"content, not a log/dump.",
            file=sys.stderr,
        )
        return 1
    payload: dict = {"op": "update_goal", "goal_content": content}
    if args.reason is not None:
        payload["reason"] = args.reason
    res = tools.do_operator_queue_append(cfg, payload)
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print("queued update_goal (lands at next tick)")
    return 0


def cmd_rollback(cfg: Config, args: argparse.Namespace) -> int:
    """Linear rollback (TB-111).

    Walk back along first-parent history to a boundary commit and
    `git reset --hard` to it. Atomic via `locked_board()`. Mid-history
    rollback (revert TB-X while keeping TB-Y after) is explicitly out of
    scope — operators do that by hand with `git revert`.
    """
    if not (cfg.project_root / ".git").exists():
        print("ap2 rollback: project is not a git repo — nothing to roll back",
              file=sys.stderr)
        return 1

    # Pre-flight: refuse a dirty working tree. Rollback isn't a stash.
    porcelain = subprocess.run(
        ["git", "-C", str(cfg.project_root), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if porcelain.returncode != 0:
        print(f"ap2 rollback: `git status --porcelain` failed: "
              f"{porcelain.stderr.strip()}", file=sys.stderr)
        return 1
    if porcelain.stdout.strip() and not args.force:
        print(
            "ap2 rollback: working tree is dirty — refusing.\n"
            "  Commit, stash, or `git checkout -- .` your changes first,\n"
            "  or pass --force to bypass (the dirt will be discarded).",
            file=sys.stderr,
        )
        return 1

    # Resolve boundary from -n / --task / --to (mutually exclusive; default -n 1).
    boundary: str | None = None
    if args.to:
        # Explicit ancestor sha. Refuse if not an ancestor (no rebases mid-rollback).
        if not rollback.is_ancestor(cfg, args.to):
            print(f"ap2 rollback: {args.to} is not an ancestor of HEAD — refusing",
                  file=sys.stderr)
            return 1
        # Resolve to a full SHA so the print is unambiguous.
        rp = subprocess.run(
            ["git", "-C", str(cfg.project_root), "rev-parse", args.to],
            capture_output=True, text=True,
        )
        boundary = rp.stdout.strip() if rp.returncode == 0 else args.to
    elif args.task:
        boundary = rollback.resolve_boundary_by_task(cfg, args.task)
        if boundary is None:
            print(
                f"ap2 rollback: {args.task} not found in HEAD's first-parent "
                f"history.\n  Try `git log --grep={args.task} --oneline` — "
                f"the task may be too far back, or it shipped on a side branch.",
                file=sys.stderr,
            )
            return 1
    else:
        n = args.n if args.n is not None else 1
        if n <= 0:
            print("ap2 rollback: -n must be ≥ 1", file=sys.stderr)
            return 2
        boundary = rollback.resolve_boundary_by_n(cfg, n)
        if boundary is None:
            print(f"ap2 rollback: history doesn't have {n} task-completions "
                  f"to roll back", file=sys.stderr)
            return 1

    affected = rollback.list_affected_commits(cfg, boundary)
    if not affected:
        print(f"ap2 rollback: nothing to roll back "
              f"(boundary {boundary[:8]} == HEAD)")
        return 0
    affected_tasks = rollback.affected_task_ids(affected)
    pipeline_warnings = rollback.list_alive_pipelines_in_range(cfg, boundary)

    # Print plan.
    print("Rollback plan:")
    print(f"  Boundary: {boundary[:8]}")
    print(f"  Affected commits ({len(affected)}):")
    for sha, subject in affected:
        print(f"    - {sha[:8]} {subject}")
    if affected_tasks:
        print(f"  Affected tasks: {', '.join(affected_tasks)}")
    if pipeline_warnings:
        print("  Pipelines still running (NOT auto-killed):")
        for w in pipeline_warnings:
            print(f"    pid {w['pid']} ({w['name'] or '?'}) "
                  f"— log: {w['log']}")
    if not args.yes:
        try:
            reply = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("ap2 rollback: aborted (no changes made)")
            return 0

    # Execute under board lock for atomicity vs. the daemon. We use the
    # save-less variant because `git reset --hard` already wrote the
    # post-reset TASKS.md; locked_board's save-on-exit would clobber it.
    with board_file_lock(cfg.tasks_file):
        try:
            rollback.linear_rollback_to(cfg, boundary)
        except Exception as exc:  # noqa: BLE001
            events.append(
                cfg.events_file, "rollback_error",
                boundary=boundary, error=f"{type(exc).__name__}: {exc}",
            )
            print(f"ap2 rollback: failed: {exc}", file=sys.stderr)
            return 1

    events.append(
        cfg.events_file,
        "task_rollback",
        boundary_sha=boundary,
        reverted_commits=[
            {"sha": sha, "subject": subject} for sha, subject in affected
        ],
        affected_tasks=affected_tasks,
        pipeline_warnings=pipeline_warnings,
    )
    print(f"ap2 rollback: reset to {boundary[:8]} "
          f"({len(affected)} commit(s) reverted, "
          f"{len(affected_tasks)} task(s) affected)")
    if pipeline_warnings:
        print(f"  warning: {len(pipeline_warnings)} pipeline subprocess(es) "
              f"still running — terminate manually if rerunning")
    return 0


def cmd_backfill_proposals(cfg: Config, args: argparse.Namespace) -> int:
    """Backfill historical ideation proposal records (TB-195).

    Operator-driven one-off: scans operator_log.md, briefing files, and
    events.jsonl to identify every TB-N that came in via an ideation
    `add_backlog` (briefing carries both a goal anchor and a Why-now
    paragraph), then writes
    `.cc-autopilot/ideation_proposals/<TB-N>.json` records for those
    that lack them — reconciling outcomes from the board's Complete
    section, the LAST `task_complete` event, and the operator log's
    reject / delete / approve lines.

    Idempotent: a TB-N whose record already exists is skipped, so the
    daemon-driven prospective writes (TB-188) and operator-driven
    backfill don't fight each other. Re-running after the daemon has
    accumulated more prospective records is safe — the second pass
    reports zero new records.

    `--dry-run` prints what WOULD be written without touching disk;
    operators can preview the impact before committing.

    TB-202: pre-flight refuse-if-active gate — if a task agent is
    running, refuse rather than racing the fenced-path write against
    the agent's TB-110 snapshot window and triggering a
    false-positive rollback. `.cc-autopilot/ideation_proposals/` is
    fenced (TB-188) and NOT exempt from the snapshot check; the
    refusal is cheaper than queue-routing the (rare) backfill verb.
    """
    active_id = _active_task_id(cfg)
    if active_id is not None:
        print(
            f"ap2 backfill-proposals: a task is currently active "
            f"({active_id}) — refusing.\n"
            f"  backfill-proposals writes to fenced "
            f"`.cc-autopilot/ideation_proposals/` and racing the active "
            f"task would trigger a state_violation rollback.\n"
            f"  Wait for the task to complete (see `ap2 status`) or pause "
            f"the daemon, then retry. Note: `ap2 pause` halts dispatch of "
            f"new tasks but does NOT abort the in-flight one; pause helps "
            f"only for the NEXT slot.",
            file=sys.stderr,
        )
        return 1

    from . import backfill

    report = backfill.backfill_proposals(cfg, dry_run=args.dry_run)
    for line in report.summaries:
        print(line)
    label = "would write" if args.dry_run else "wrote"
    print(
        f"backfill: {label}={len(report.written)} "
        f"skipped_existing={len(report.skipped_existing)} "
        f"skipped_non_ideation={len(report.skipped_non_ideation)} "
        f"skipped_no_briefing={len(report.skipped_no_briefing)}"
    )
    return 0


def cmd_ack(cfg: Config, args: argparse.Namespace) -> int:
    """Queue an operator-decision line for the daemon to append to
    `.cc-autopilot/operator_log.md` at the next tick (TB-106, TB-201).

    Used to communicate "I did X" / "I decided Y" back to ap2 so
    ideation stops re-proposing actions whose effects aren't visible
    on the filesystem (e.g. "considered FRAGILE plist retention,
    decided to keep them"). Optional `-t TB-N` ties the ack to a task.

    TB-201: routed through the operator queue rather than mutating
    operator_log.md synchronously. The pre-TB-201 in-place write
    raced with running task agents — operator_log.md is fenced and
    NOT exempt from the TB-110 post-hoc snapshot check, so a mid-task
    `ap2 ack` tripped a false-positive state violation and rolled
    back the task's legitimate work (cost ~$12.55 on post-train at
    2026-05-12T06:40-07:14Z; that incident is the proximate motivator
    for this retrofit). The drain-side `_apply_operator_ack` performs
    the actual operator_log.md write at tick boundary, under the
    daemon's board lock — never inside a task agent's snapshot
    window. Slight UX change from the immediate "appended to
    operator_log.md" of yesteryear; consistent with the rest of the
    queue-routed CLI verbs (`approve` / `reject` / `classify` /
    `update-goal` / `add` / etc.).
    """
    res = tools.enqueue_operator_ack(
        cfg,
        {"note": args.note, "task_id": args.task or ""},
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    print("queued ack (will land at next tick)")
    return 0
