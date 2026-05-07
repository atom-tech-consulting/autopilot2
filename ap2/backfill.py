"""TB-195 backfill of historical ideation proposal records.

Operator-driven CLI (`ap2 backfill-proposals`) one-off: scans the project's
`operator_log.md` + briefing files + `events.jsonl`, identifies every
ideation-authored TB-N that lacks a `.cc-autopilot/ideation_proposals/<TB-N>.json`
record (TB-188's prospective-write path didn't fire for ~50 historical
proposals since TB-121's review-gate landed), and writes those records
with reconciled outcomes.

Detection: a TB-N is treated as ideation-authored iff its briefing file
on disk passes BOTH `extract_goal_anchor` (TB-161 anchor present in the
`## Goal` body) and `extract_why_now` (TB-164 Why-now paragraph
present). Operator-CLI `--skip-goal-alignment` adds typically fail
either gate and are correctly skipped — they're not ideation proposals.

Outcome reconciliation: walks the same surfaces TB-188's prospective
path uses (board section membership, the LAST `task_complete` event,
operator_log.md reject/delete/approve audit lines). The TB-188
`reconcile_proposal_outcome` helper is reused so the on-disk record
shape matches the prospective path byte-for-byte.

Idempotent: a TB-N whose record already exists is skipped (the prospective
TB-188 write path has presumably already covered it). Safe to re-run after
the operator queues new ops.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .board import Board, parse_task_line
from .config import Config
from .tools import (
    extract_goal_anchor,
    extract_why_now,
    ideation_proposals_dir,
    proposal_record_path,
    reconcile_proposal_outcome,
    write_ideation_proposal_record,
)


# ---------------------------------------------------------------------------
# Operator-log audit-line parsing (read-only — pinned against the format
# `ap2/tools.py::_append_operator_audit_line` writes).

_TS_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"

# `- <ts> — applied operator-queued <op> → <target>` — the canonical
# drain audit line. `<target>` is `TB-N` for verbs that take a task
# (add_*/move_to_backlog/approve/reject/delete/unfreeze/update/classify),
# `(forced)` for `ideate`, and may carry a parenthetical decoration
# (`(goal-alignment check skipped)`). The TB-N is extracted post-match
# via `_TB_PREFIX_RE`.
_APPLIED_RE = re.compile(
    rf"^- (?P<ts>{_TS_RE}) — applied operator-queued "
    r"(?P<op>\w+) → (?P<target>.+?)\s*$"
)

# `- <ts> — rejected ideation proposal → TB-N (<title>): <reason>` —
# TB-152 emits this in addition to the standard `applied operator-queued
# reject` line. The reason text is the operator's free-form rationale
# we surface in the per-proposal record's outcome block.
_REJECTED_PROPOSAL_RE = re.compile(
    rf"^- (?P<ts>{_TS_RE}) — rejected ideation proposal → "
    r"(?P<tb>TB-\d+) \((?P<title>.*)\): (?P<reason>.*)$"
)

_TB_PREFIX_RE = re.compile(r"^(TB-\d+)\b")


@dataclass
class OperatorLogEntry:
    """One parsed audit-line from operator_log.md.

    `kind` is `"applied"` for the canonical drain line and
    `"rejected_proposal"` for TB-152's richer reject line. `op` is the
    queue verb; `tb_id` is the parsed TB-N (None for `ideate → (forced)`
    and any line whose target doesn't start with `TB-`). `reason` is
    populated only for `rejected_proposal` entries.
    """
    ts: str
    kind: str
    op: str
    tb_id: str | None = None
    target_raw: str = ""
    title: str = ""
    reason: str = ""
    raw: str = ""


def parse_operator_log_lines(path: Path) -> list[OperatorLogEntry]:
    """Parse audit-line shapes from `.cc-autopilot/operator_log.md`.

    Lines that don't match either of the two structured shapes (free-
    form operator notes, the file header prose, blank lines, the legacy
    `Pivoted Current focus` pivot notes) are silently skipped — the
    parser returns only entries whose shape is one of:

      - `<ts> — applied operator-queued <op> → <target>` (every queue-
        drain op: add_backlog, add_ready, approve, reject, delete,
        unfreeze, update, move_to_backlog, ideate, classify, update_goal)
      - `<ts> — rejected ideation proposal → TB-N (<title>): <reason>`
        (TB-152 emits this in addition to the standard reject line)

    The regexes pin against the format `_append_operator_audit_line`
    writes (`ap2/tools.py`); a change to that emitter must update the
    regexes here too. Tests in `test_backfill_proposals.py` lock the
    parse against every audit-line shape currently in the file.
    """
    out: list[OperatorLogEntry] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        m = _APPLIED_RE.match(line)
        if m:
            target = m.group("target")
            tb_match = _TB_PREFIX_RE.match(target)
            tb_id = tb_match.group(1) if tb_match else None
            out.append(OperatorLogEntry(
                ts=m.group("ts"),
                kind="applied",
                op=m.group("op"),
                tb_id=tb_id,
                target_raw=target,
                raw=line,
            ))
            continue
        m = _REJECTED_PROPOSAL_RE.match(line)
        if m:
            out.append(OperatorLogEntry(
                ts=m.group("ts"),
                kind="rejected_proposal",
                op="reject",
                tb_id=m.group("tb"),
                title=m.group("title"),
                reason=m.group("reason"),
                raw=line,
            ))
    return out


# ---------------------------------------------------------------------------
# Backfill report + driver.


@dataclass
class BackfillReport:
    """Per-pass outcome summary returned by `backfill_proposals`.

    `written` is the list of TB-Ns where a NEW record was created (or
    would be, with `dry_run=True`). The `skipped_*` lists are diagnostic
    breakdowns — same shape across dry-run and live so a caller can
    compare the report across two passes (the second pass of an
    idempotent re-run names zero new records and N existing skips).
    """
    written: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    skipped_non_ideation: list[str] = field(default_factory=list)
    skipped_no_briefing: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)


def _events_index_by_tb(events_file: Path) -> dict[str, list[dict]]:
    """Read events.jsonl into a `{TB-N: [event, ...]}` dict (file order
    preserved). One pass over the file regardless of how many TB-Ns
    we'll iterate, so the backfill is O(events + tb_ns) instead of
    O(events * tb_ns).
    """
    idx: dict[str, list[dict]] = {}
    if not events_file.exists():
        return idx
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        tb = str(evt.get("task") or "").strip()
        if tb.startswith("TB-"):
            idx.setdefault(tb, []).append(evt)
    return idx


def _briefing_path_for(cfg: Config, board: Board, tb_id: str) -> Path | None:
    """Resolve the briefing file path for `tb_id` from the board's
    `[→ brief](path)` link. Returns None when the TB-N isn't on the
    board (operator deleted it) or the link is missing (legacy adds
    pre-briefing-file convention).
    """
    for section, lines in board.sections.items():
        for line in lines:
            t = parse_task_line(line, section)
            if t and t.id == tb_id and t.briefing:
                return cfg.project_root / t.briefing
    return None


def _is_complete(board: Board, tb_id: str) -> bool:
    """True when `tb_id` is in the board's Complete section."""
    for line in board.sections.get("Complete", []):
        t = parse_task_line(line, "Complete")
        if t and t.id == tb_id:
            return True
    return False


def _tb_num(tb: str) -> int:
    try:
        return int(tb.split("-", 1)[1])
    except (ValueError, IndexError):
        return -1


def backfill_proposals(cfg: Config, dry_run: bool = False) -> BackfillReport:
    """Backfill `.cc-autopilot/ideation_proposals/<TB-N>.json` records
    for historical ideation-authored proposals (TB-195).

    Walks every `applied operator-queued add_backlog → TB-N` line in
    `operator_log.md`, classifies the TB-N as ideation-authored vs
    operator-authored via the structural test on its briefing file
    (anchor + Why-now both present), and for ideation-authored TB-Ns
    without an existing record:

      1. Reads the briefing → derives `focus_anchor` / `why_now` /
         `briefing_path` via the TB-188 public helpers.
      2. Stamps `proposed_at` from the matching `add_backlog` line's
         timestamp (not "now" — preserves the historical signal).
      3. Writes the seed record via `write_ideation_proposal_record`
         (the TB-188 helper extended with an optional `proposed_at`
         kwarg for the backfill path).
      4. Reconciles the `outcome` block from the board's Complete
         section (last `task_complete` event picks `completed` vs
         `verification_failed`), the operator log's `reject` /
         `delete` / `approve` audit lines, or leaves the record
         outcome-less if the proposal is still in-flight.

    Idempotent: a TB-N whose record already exists is skipped. Safe to
    re-run after the daemon has accumulated new prospective writes.

    `dry_run=True` reads everything but writes nothing; the report still
    names the TB-Ns it would have written so the operator can preview
    the impact before committing.
    """
    report = BackfillReport()
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_entries = parse_operator_log_lines(log_path)

    add_lines: dict[str, OperatorLogEntry] = {}
    reject_lines: dict[str, OperatorLogEntry] = {}
    delete_lines: dict[str, OperatorLogEntry] = {}
    approve_lines: dict[str, OperatorLogEntry] = {}
    rejected_proposal_lines: dict[str, OperatorLogEntry] = {}

    for entry in log_entries:
        if not entry.tb_id:
            continue
        if entry.kind == "applied":
            # First add_backlog wins — defensive, since a TB-N's add
            # is allocated under the queue's uniqueness check; a
            # duplicate would indicate corrupt log data.
            if entry.op == "add_backlog" and entry.tb_id not in add_lines:
                add_lines[entry.tb_id] = entry
            elif entry.op == "reject":
                reject_lines[entry.tb_id] = entry
            elif entry.op == "delete":
                delete_lines[entry.tb_id] = entry
            elif entry.op == "approve":
                approve_lines[entry.tb_id] = entry
        elif entry.kind == "rejected_proposal":
            rejected_proposal_lines[entry.tb_id] = entry

    if not add_lines:
        return report

    board = Board.load(cfg.tasks_file)
    events_idx = _events_index_by_tb(cfg.events_file)

    # Ensure the records dir exists before any write attempt — a
    # dry-run leaves it untouched, but a live pass that finds zero
    # writeable candidates shouldn't fail open here either.
    if not dry_run:
        ideation_proposals_dir(cfg).mkdir(parents=True, exist_ok=True)

    for tb_id in sorted(add_lines.keys(), key=_tb_num):
        record_path = proposal_record_path(cfg, tb_id)
        if record_path.exists():
            report.skipped_existing.append(tb_id)
            continue

        brief_path = _briefing_path_for(cfg, board, tb_id)
        if brief_path is None or not brief_path.exists():
            report.skipped_no_briefing.append(tb_id)
            continue

        try:
            briefing_text = brief_path.read_text()
        except OSError:
            report.skipped_no_briefing.append(tb_id)
            continue

        anchor = extract_goal_anchor(briefing_text, cfg.project_root / "goal.md")
        why_now = extract_why_now(briefing_text)
        if anchor is None or why_now is None:
            report.skipped_non_ideation.append(tb_id)
            continue

        proposed_at = add_lines[tb_id].ts
        outcome_kind, outcome_actor, outcome_reason, outcome_commit = (
            _resolve_outcome(
                tb_id=tb_id,
                board=board,
                events_idx=events_idx,
                rejected_proposal_lines=rejected_proposal_lines,
                reject_lines=reject_lines,
                delete_lines=delete_lines,
                approve_lines=approve_lines,
            )
        )

        anchor_prefix = anchor[:60]
        outcome_label = outcome_kind or "in-flight"
        report.summaries.append(
            f"{tb_id} would write record focus={anchor_prefix} "
            f"outcome={outcome_label}"
        )
        report.written.append(tb_id)

        if dry_run:
            continue

        briefing_rel = str(brief_path.relative_to(cfg.project_root))
        write_ideation_proposal_record(
            cfg,
            tb_id=tb_id,
            blocked_on="review",
            briefing_text=briefing_text,
            briefing_rel=briefing_rel,
            proposed_at=proposed_at,
        )

        if outcome_kind:
            reconcile_proposal_outcome(
                cfg,
                tb_id,
                decision_kind=outcome_kind,
                decision_actor=outcome_actor,
                commit=outcome_commit,
                reason=outcome_reason,
            )

    return report


def _resolve_outcome(
    *,
    tb_id: str,
    board: Board,
    events_idx: dict[str, list[dict]],
    rejected_proposal_lines: dict[str, OperatorLogEntry],
    reject_lines: dict[str, OperatorLogEntry],
    delete_lines: dict[str, OperatorLogEntry],
    approve_lines: dict[str, OperatorLogEntry],
) -> tuple[str | None, str, str, str | None]:
    """Determine the per-proposal outcome block from current state +
    historical evidence. Returns `(decision_kind, decision_actor,
    reason, commit)` or `(None, ...)` when the proposal is still
    in-flight (no terminal evidence yet).

    Precedence mirrors the briefing's Scope section 4:
      Complete > rejected_proposal_line > applied reject > applied
      delete > applied approve > in-flight.
    """
    if _is_complete(board, tb_id):
        tc_events = [
            e for e in events_idx.get(tb_id, [])
            if e.get("type") == "task_complete"
        ]
        if tc_events:
            last = tc_events[-1]
            status = str(last.get("status", "")).strip()
            commit_val = str(last.get("commit") or "").strip() or None
            if status == "verification_failed":
                return "verification_failed", "verifier", "", commit_val
            return "completed", "daemon", "", commit_val
        # Defensive: section says Complete but no event found
        # (could happen on a rolled-back project with truncated
        # events.jsonl). Record the section verdict; commit unknown.
        return "completed", "daemon", "", None

    if tb_id in rejected_proposal_lines:
        return "rejected", "operator", rejected_proposal_lines[tb_id].reason, None
    if tb_id in reject_lines:
        return "rejected", "operator", "", None
    if tb_id in delete_lines:
        return "deleted", "operator", "", None
    if tb_id in approve_lines:
        return "approved", "operator", "", None

    return None, "operator", "", None
