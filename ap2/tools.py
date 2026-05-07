"""Custom SDK MCP tools for control agents.

The mattermost handler and cron agents call these to mutate the board, the cron
registry, and send Mattermost replies. Task agents do NOT get these tools — they
just code, commit, and exit.

Tools close over a Config so the daemon can wire paths at startup without the
agent having to know them.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid as _uuid
from pathlib import Path
from typing import Any

from . import events, retry
from .board import Board, board_file_lock, locked_board, parse_task_line
from .config import Config, bump_next_task_id
from .cron import update_job
from .init import (
    BRIEFING_REQUIRED_SECTIONS,
    GOAL_ANCHOR_HEADINGS,
    WHY_NOW_MIN_CHARS,
)
from .verify import parse_verification_section


# TB-123: contextvar plumb so `do_cron_propose` can stamp the calling task's
# TB-id onto the `cron_proposed` event without forcing the agent to pass its
# own id through the tool args. `daemon.run_task` sets this before awaiting
# `sdk.query(...)` and resets it on exit. The MCP tool handlers run in the
# same asyncio task as run_task, so the value is visible during dispatch.
# Tests that call `do_cron_propose` directly (no daemon) see the default ""
# and the event simply omits `proposed_by_task` — that's fine for the unit
# shape; the e2e test exercises the daemon-set path.
_task_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ap2_task_id", default="",
)


def slugify(text: str, max_len: int = 40) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def _ok(text: str, **fields: Any) -> dict:
    body = {"message": text}
    body.update(fields)
    return {
        "content": [{"type": "text", "text": json.dumps(body)}],
    }


def _err(text: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
        "isError": True,
    }


# ---------------- implementations (SDK-free, directly testable) ----------------


# TB-134: TASKS.md is a line-oriented format — TASK_LINE_RE in board.py is
# a per-line regex, so a multi-line title/description/tag silently splits the
# rendered task across physical lines, stranding the trailing `[→ brief](...)`
# link on a different line and dropping it from the parsed Task. The first
# line still parses; subsequent lines surface as `board_malformed_line`
# events but stay on disk. Hit on TB-132 / TB-133 (operator had to manually
# re-collapse). Auto-collapsing newlines to spaces would silently produce
# 400-char run-on lines that nobody actually wanted; rejecting forces the
# caller to choose between summarizing inline OR moving the rich content
# into the briefing file — the right semantic split.
SINGLE_LINE_ERR = (
    "{field} must be a single line — break long content into briefing.md "
    "instead, or summarize to one line"
)


def _validate_update_args(args: dict) -> str | None:
    """Single-line gate for TB-153 `update` op inputs.

    `update` reuses TB-134's "no embedded newlines on a board-line
    field" rule (TASK_LINE_RE is line-anchored). A multi-line title /
    tag / description / blocked CSV would silently split the rendered
    task across lines and strand the trailing `[→ brief](...)` link.
    Briefing content is exempt — that's free-form prose in its own
    file, not on the TASKS.md task line.
    """
    title = args.get("title")
    if title is not None:
        err = _validate_single_line("title", str(title))
        if err:
            return err
    desc = args.get("description")
    if desc is not None:
        err = _validate_single_line("description", str(desc))
        if err:
            return err
    blocked = args.get("blocked")
    if blocked is not None:
        err = _validate_single_line("blocked", str(blocked))
        if err:
            return err
    tags = args.get("tags")
    if tags is not None:
        for tag in tags:
            err = _validate_single_line("tag", str(tag))
            if err:
                return err
    return None


def _validate_single_line(field: str, value: str | None) -> str | None:
    """Reject a multi-line operator input (newline / carriage-return).

    Returns an error message string if `value` contains \\n or \\r;
    returns `None` if the value is single-line (or empty / None).
    Used by `cmd_add` (CLI), `do_board_edit` (MCP), and
    `do_operator_queue_append` (MCP + CLI bridge) so every entry point
    that can land a task on TASKS.md hits the same gate.
    """
    if not value:
        return None
    if "\n" in value or "\r" in value:
        return SINGLE_LINE_ERR.format(field=field)
    return None


# TB-154: top-level (`##`) section header pattern. Anchors at start-of-line,
# tolerates trailing content after the section name (e.g. `## Verification
# (launch-task — ...)`), and captures the bare section name verbatim. Same
# tolerance as `verify._is_verification_heading` so both surfaces accept
# the same author shapes.
_BRIEFING_SECTION_RE = re.compile(r"^##\s+([A-Za-z][A-Za-z ]*?)(?:\s*[(\-—:].*)?\s*$", re.M)


def _briefing_section_names(briefing_text: str) -> set[str]:
    """Return the set of `##`-level section names present in `briefing_text`.

    Parses by line-anchored regex (mirrors `check.py::_SECTION_ORDER_RE`'s
    shape) rather than mistune AST — a fenced code block whose contents
    include `## Foo` would not be a real section, but for structural
    validation false-positives only weaken the gate's reach (we'd accept
    a briefing whose author put the canonical sections in a code block);
    we don't false-reject. The empty-Verification check uses the proper
    AST-driven `parse_verification_section` so the gate's correctness
    side stays tight.
    """
    return {m.group(1).strip() for m in _BRIEFING_SECTION_RE.finditer(briefing_text)}


_BRIEFING_STRUCTURE_HINT = (
    "The briefing must contain `## Goal`, `## Scope`, `## Design`, "
    "`## Verification`, and `## Out of scope` headings (case-sensitive). "
    "See ap2/init.py BRIEFING_TEMPLATE for the canonical shape, or copy "
    "from any in-flight briefing in `.cc-autopilot/tasks/`."
)


# TB-161: extract the body text under a `##` heading (between the heading
# line and the next `##` heading or EOF). Returns "" when the heading is
# absent. Tolerates trailing content after the section name in the same
# shape as `_BRIEFING_SECTION_RE` so the validator and the body-extractor
# accept the same author shapes.
def _briefing_section_body(text: str, heading: str) -> str:
    """Return the body text of a `##`-level briefing section, or ''.

    Used by the TB-161 goal-anchor check (lifts the `## Goal` body out
    of the briefing for substring matching). The shared helper exists so
    `tools.py` and `check.py` agree on the slice boundaries — a future
    "Goal section is suspiciously short" lint can reuse it.
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}(?:\s*[(\-—:].*)?\s*$",
        re.M,
    )
    m = pattern.search(text)
    if not m:
        return ""
    body_start = m.end()
    next_m = re.search(r"^##\s+", text[body_start:], re.M)
    if next_m is None:
        return text[body_start:]
    return text[body_start: body_start + next_m.start()]


# TB-161: punctuation strip + lowercase + whitespace collapse. Used to
# compare goal.md anchor phrases against the briefing's `## Goal` body —
# both sides go through this so trivial differences (`'goal.md'` vs
# `goal md`, capitalization, em-dashes vs hyphens, multiple spaces) don't
# cause a false-reject.
_ANCHOR_NORMALIZE_RE = re.compile(r"[^a-z0-9\s]+")


def _normalize_anchor(text: str) -> str:
    lowered = text.lower()
    stripped = _ANCHOR_NORMALIZE_RE.sub(" ", lowered)
    return " ".join(stripped.split())


# TB-161: pull a candidate anchor phrase out of a `## Done when` bullet
# line. Returns the first `words` (default 6) of the bullet body
# normalized, or None if the line isn't a list bullet. Anchors shorter
# than 3 words are rejected — too generic to discriminate between a
# briefing that quotes the bullet and one that incidentally shares a
# common phrase ("the operator", "this is", etc.).
_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+(.*)$")


def _bullet_anchor_phrase(line: str, *, words: int = 6) -> str | None:
    m = _BULLET_LINE_RE.match(line)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    norm = _normalize_anchor(body)
    if not norm:
        return None
    parts = norm.split()
    phrase = " ".join(parts[:words]).strip()
    if not phrase or len(phrase.split()) < 3:
        return None
    return phrase


_GOAL_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


# TB-164: line-anchored marker for the "Why now" rationale paragraph the
# briefing's `## Goal` body must include. The marker must appear at the
# start of a line (modulo leading whitespace) and be followed by a
# whitespace or `:` separator, so the rule isn't matched mid-prose
# ("the question of why now is hard…" inside a Goal sentence is not a
# rationale paragraph). Case-insensitive to tolerate "Why Now" / "WHY
# NOW" stylings, and `(?m)` so `^` lines up with each line of the body.
_WHY_NOW_MARKER_RE = re.compile(r"(?im)^\s*why\s+now(?=[\s:])")


# TB-171: line-anchored bullet pattern for `Manual:` / `[manual]` items in
# `## Verification`. Anchors on the bullet marker (`-` / `*`) so prose that
# happens to mention the word "manual" inline doesn't false-positive.
# Mirrors `ap2/check.py::_MANUAL_BULLET_RE` (kept in sync deliberately —
# tools.py does not import check.py and the briefing recommended duplication
# over a new tools→check coupling). If you edit this regex, also update
# `ap2/check.py:144` so the queue-append gate and the operator-facing lint
# stay in agreement.
_MANUAL_BULLET_RE = re.compile(
    r"^\s*[-*]\s*(?:Manual\s*:|\[manual\])",
    re.IGNORECASE,
)


def _why_now_paragraph(goal_body: str) -> str | None:
    """Return the trailing paragraph attached to a line-anchored
    "Why now" marker inside `goal_body`, or None when no marker matches.

    The "paragraph" is everything from the marker line through to the
    next blank line (or the end of the body), with the marker token and
    its trailing punctuation stripped. The minimum-length check in the
    validator runs on this stripped text so trivial passes like
    `Why now: yes` (whose leftover is 3 chars) are rejected even though
    the marker itself is present. Returns None — distinct from "" — when
    no marker appears, so the caller can distinguish "missing entirely"
    from "marker present but rationale too short."
    """
    m = _WHY_NOW_MARKER_RE.search(goal_body)
    if m is None:
        return None
    # Slice from the end of the marker through the next blank line (or
    # EOF). A blank line is `\n\s*\n` — anything tighter would split a
    # multi-line rationale across two physical lines.
    rest = goal_body[m.end():]
    blank = re.search(r"\n\s*\n", rest)
    paragraph = rest if blank is None else rest[: blank.start()]
    # Drop the leading separator (`:`, `—`, `-`, etc.) and any
    # parenthetical (e.g. `(delete-test)`) so the length check runs on
    # the actual rationale text. Leading whitespace too.
    stripped = paragraph.lstrip(" \t")
    # Strip a parenthetical immediately after the marker (e.g.
    # "Why now (delete-test): ..." → drop "(delete-test)").
    paren_m = re.match(r"^\([^)]*\)", stripped)
    if paren_m:
        stripped = stripped[paren_m.end():].lstrip(" \t")
    # Strip a leading punctuation separator.
    stripped = stripped.lstrip(":—-–").lstrip()
    return stripped


def _goal_md_anchors_from_text(text: str) -> set[str]:
    """Pure text-input variant of `_goal_md_anchors` (TB-193).

    Factored out so `do_operator_queue_append`'s `update_goal` branch
    can sanity-check a goal.md payload BEFORE it lands on disk without
    having to write the candidate to a tempfile first. Same anchor
    rules as the file-input wrapper below: walks `##` headings, picks
    the ones starting with a `GOAL_ANCHOR_HEADINGS` prefix, and emits
    normalized titles + Done-when bullet phrases. Returns an empty set
    when the text is all-placeholder.
    """
    anchors: set[str] = set()
    bare_prefixes = {h.lower() for h in GOAL_ANCHOR_HEADINGS}
    matches = list(_GOAL_HEADING_RE.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        title_lower = title.lower()
        matched_prefix: str | None = None
        for h in GOAL_ANCHOR_HEADINGS:
            if title_lower.startswith(h.lower()):
                matched_prefix = h
                break
        if matched_prefix is None:
            continue
        norm_title = _normalize_anchor(title)
        if norm_title and norm_title not in bare_prefixes:
            anchors.add(norm_title)
        if matched_prefix.lower() == "done when":
            body_start = m.end()
            body_end = (
                matches[i + 1].start() if i + 1 < len(matches) else len(text)
            )
            body = text[body_start:body_end]
            for line in body.splitlines():
                phrase = _bullet_anchor_phrase(line, words=6)
                if phrase is not None:
                    anchors.add(phrase)
    return anchors


def _goal_md_anchors(goal_md_path: "Path | None") -> set[str]:
    """Derive substring anchors from `goal.md`.

    Walks `##` headings; for each whose title starts with one of
    `GOAL_ANCHOR_HEADINGS` (case-insensitive prefix match), the
    normalized full heading title becomes an anchor. For `Done when`
    sections only, each bullet's first 3-6 words become an additional
    anchor — the briefing's `## Goal` body can cite either the heading
    name (e.g. "current focus: ideation quality") or quote a Done-when
    bullet ("an operator can point ap2 at"). Returns an empty set when
    the file is missing, unreadable, all-placeholder, or contributes no
    anchors — the validator falls back to "skip the check" in that case
    so a fresh project without a real `goal.md` doesn't get its briefings
    rejected.

    TB-193: parsing logic moved to `_goal_md_anchors_from_text` so the
    `update_goal` queue op can validate a candidate payload before write.
    """
    if goal_md_path is None or not goal_md_path.exists():
        return set()
    try:
        text = goal_md_path.read_text()
    except OSError:
        return set()
    return _goal_md_anchors_from_text(text)


# TB-188: public helpers exposed for the per-proposal record path. Wraps
# the TB-161 / TB-164 internals (`_goal_md_anchors`, `_briefing_section_body`,
# `_normalize_anchor`, `_why_now_paragraph`) so the proposal-record writer
# doesn't have to reach into private symbols and so signal-collection
# follow-ups (TB-189 retrospective verdict, future track-record blocks)
# share one parser surface with the queue-append validator. The pair is
# deliberately read-only: extraction returns whatever the briefing carries,
# even when the validator would have rejected the briefing — the record's
# job is to capture seed context, not re-gate.
def extract_goal_anchor(
    briefing_text: str,
    goal_md_path: "Path | None" = None,
) -> str | None:
    """Return the first goal.md anchor substring matched by the briefing's
    `## Goal` body, or None when no anchor matches (or no anchors are
    available from goal.md). Reuses the TB-161 substring matcher so the
    per-proposal record's `focus_anchor` field reflects the same anchor
    the validator would have credited at queue-append time. Iteration is
    sorted for record-shape determinism — a briefing whose Goal body
    contains multiple anchors (e.g. the heading title AND a quoted
    Done-when bullet) records the lexicographically-first match.
    """
    if not briefing_text:
        return None
    anchors = _goal_md_anchors(goal_md_path)
    if not anchors:
        return None
    goal_body = _briefing_section_body(briefing_text, "Goal")
    if not goal_body:
        return None
    norm = _normalize_anchor(goal_body)
    for a in sorted(anchors):
        if a in norm:
            return a
    return None


def extract_why_now(briefing_text: str) -> str | None:
    """Return the line-anchored 'Why now' rationale paragraph from the
    briefing's `## Goal` body, or None when no marker is present.
    Reuses TB-164's `_why_now_paragraph` extractor against the Goal
    section. Returns the rationale text post-marker / post-parenthetical
    / post-separator, matching what the validator's length check sees —
    so a record whose `why_now` field is shorter than `WHY_NOW_MIN_CHARS`
    is by definition a briefing that bypassed the gate (e.g. operator-CLI
    `--skip-goal-alignment`), and the per-proposal aggregation can spot
    that without re-parsing the original briefing.
    """
    if not briefing_text:
        return None
    goal_body = _briefing_section_body(briefing_text, "Goal")
    if not goal_body:
        return None
    return _why_now_paragraph(goal_body)


# TB-188: per-proposal record path. One JSON file per ideation-authored
# `add_backlog` (those whose `blocked_on` carries the `review` token —
# the TB-121 ideation marker). Reconciled with an `outcome` block on
# the first terminal event (task_complete with status complete /
# verification_failed; operator-queue approve / reject / delete). Records
# are committed alongside other daemon-owned audit trail (`tasks/`,
# `insights/`) via the TB-126 narrowed state-commit path so signal-
# collection follow-ups (TB-189 delete-test verdict, acceptance-rate
# aggregation, retrospective classifier) can query history across cycles.
IDEATION_PROPOSALS_DIR = ".cc-autopilot/ideation_proposals"


def ideation_proposals_dir(cfg: Config) -> Path:
    return cfg.project_root / IDEATION_PROPOSALS_DIR


def proposal_record_path(cfg: Config, tb_id: str) -> Path:
    return ideation_proposals_dir(cfg) / f"{tb_id}.json"


def _atomic_write_json(target: Path, payload: dict) -> None:
    """Write JSON atomically: tmpfile in the same dir + os.replace.
    Mirrors `do_ideation_state_write` (TB-90 precedent) — same-directory
    tmpfile so `os.replace` stays atomic on POSIX; same-suffix-plus-`.tmp`
    naming so a crashed write leaves a recognizable orphan.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)


def _blocked_on_has_review(blocked_on: str) -> bool:
    """True if `blocked_on` (raw codespan body) contains the literal
    `review` token. Tolerates the TB-187 mixed-blocker shape
    (`review,TB-N`) and case stylings — operator-driven adds via
    `ap2 add` typically don't carry the marker, so this acts as the
    "is this an ideation-authored proposal?" filter for record writes.
    """
    return "review" in [
        tok.strip().lower()
        for tok in (blocked_on or "").split(",")
        if tok.strip()
    ]


def write_ideation_proposal_record(
    cfg: Config,
    *,
    tb_id: str,
    blocked_on: str,
    briefing_text: str,
    briefing_rel: str | None,
) -> Path | None:
    """Seed a per-proposal record at ideation `add_backlog` time (TB-188).

    Skips silently when `blocked_on` does not carry the `review` token
    (operator-driven adds aren't ideation proposals — see
    `_blocked_on_has_review`). Skips when a record for `tb_id` already
    exists (defensive against retries reissuing the same TB-N — should
    not happen in normal flow, but a re-write would clobber a previously
    reconciled `outcome` block).

    Returns the record path when written, None when skipped.
    """
    if not _blocked_on_has_review(blocked_on):
        return None
    target = proposal_record_path(cfg, tb_id)
    if target.exists():
        return None
    payload = {
        "tb_id": tb_id,
        "proposed_at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "focus_anchor": extract_goal_anchor(
            briefing_text, cfg.project_root / "goal.md",
        ),
        "why_now": extract_why_now(briefing_text),
        "briefing_path": briefing_rel,
        "blocked_on": blocked_on,
    }
    _atomic_write_json(target, payload)
    return target


_PROPOSAL_DECISION_KINDS = (
    "approved",
    "rejected",
    "deleted",
    "completed",
    "verification_failed",
)


def reconcile_proposal_outcome(
    cfg: Config,
    tb_id: str,
    *,
    decision_kind: str,
    decision_actor: str,
    commit: str | None = None,
    reason: str = "",
) -> Path | None:
    """Append an `outcome` block to a per-proposal record (TB-188).

    No-op when:
      - the record file does not exist (legacy proposals from before
        TB-188 landed; operator-driven adds without the `review`
        marker; etc.),
      - the record is unreadable / malformed JSON (defensive),
      - the record already carries an `outcome` block (idempotent —
        first terminal event wins, mirroring the briefing's "no
        multi-amend" contract).

    Returns the record path when reconciled, None when skipped.
    """
    if decision_kind not in _PROPOSAL_DECISION_KINDS:
        # Defensive — callers in this module pass literals from the
        # tuple above. A wrong literal silently no-ops rather than
        # poisoning the record with an unknown kind.
        return None
    target = proposal_record_path(cfg, tb_id)
    if not target.exists():
        return None
    try:
        record = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if "outcome" in record:
        return None
    record["outcome"] = {
        "decision_kind": decision_kind,
        "decision_ts": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "decision_actor": decision_actor,
        "commit": commit,
        "reason": reason,
    }
    _atomic_write_json(target, record)
    return target


def _validate_briefing_structure(
    briefing_text: str,
    *,
    goal_md_path: "Path | None" = None,
    skip_goal_alignment: bool = False,
) -> str | None:
    """TB-154 + TB-161 + TB-164: structural gate for a freshly-authored briefing.

    A briefing is structurally valid when:
      1. It contains every section in `BRIEFING_REQUIRED_SECTIONS` at
         the `##` level (case-sensitive, any order). Extra `##`-level
         sections (e.g. `## Decision log`) are allowed — extension is
         fine, omission/rename is not.
      2. The `## Verification` section is parseable by
         `verify.parse_verification_section` (i.e. the heading is
         actually parsed by the verifier — defends against rendering
         quirks that the structural-pass `_BRIEFING_SECTION_RE` accepts
         but the AST-driven verifier silently drops).
      3. `## Verification` carries at least one bullet — an empty
         section is structurally valid markdown but produces zero
         criteria for the per-task verifier to score against.
      4. (TB-161) When `goal_md_path` is supplied AND `goal.md` exposes
         derivable anchors (a `## Current focus` / `## Done when`
         heading and/or any Done-when bullet), the briefing's `## Goal`
         body must contain at least one anchor as a substring (after
         lowercase + punctuation-strip + whitespace collapse on both
         sides). Closes goal.md's "Gap-covering without drift" failure
         mode (lines 50-59) at queue-append time — proposals whose
         "Goal" is pure ap2-meta-polish unconnected to any focus item
         get rejected before TB-N is allocated. Falls back to "skip the
         check" when goal.md is missing or all-placeholder so a fresh
         project without a real `goal.md` doesn't get its briefings
         rejected.
      5. (TB-164) The `## Goal` body must include a non-empty "Why now"
         rationale paragraph — a line-anchored `Why now` marker
         followed by at least `WHY_NOW_MIN_CHARS` chars of rationale
         (post-marker, post-parenthetical, post-separator). Closes
         goal.md's "push for progress without scope creep" failure
         mode (lines 61-70) at queue-append time: every proposal must
         pass the delete-test ("if we delete this and the goal still
         ships, was it useful?"), and the author must articulate that
         test in writing. Skipped when the briefing text is empty (the
         TB-135 non-empty gate handles that case with a clearer error).
      6. (TB-171) The `## Verification` body must contain no `Manual:`
         or `[manual]` bullets. The per-task verifier runs unattended
         and cannot observe a live operator action — TB-122 hit
         `retry_exhausted` on a single manual bullet despite the
         implementation being complete. The rule already lived as
         author-side prose (TB-138 ideation prompt + briefing
         template + ap2-task skill) and as a non-fatal `ap2 check`
         lint (`_check_briefings_manual_bullets`); this gate mirrors
         it into the queue-append-time validator so a malformed
         briefing can't slip past and cost a TB-N + a task-agent run.
         Out-of-scope bullets are unaffected — only the `## Verification`
         body is scanned. Match is case-insensitive (covers `Manual:`,
         `manual:`, `[Manual]`, `[manual]`, etc.).

    `skip_goal_alignment=True` (TB-170) is the operator-CLI escape
    hatch: it skips checks (4) and (5) but runs every other validation
    unchanged. Used by `ap2 add --skip-goal-alignment` /
    `ap2 update --skip-goal-alignment` so an operator filing a
    legitimately-meta task (dependency bump, doc fix, infra
    maintenance) doesn't have to manufacture goal-alignment prose for
    a one-line typo fix. The bypass is operator-CLI-only — ideation,
    MM handler, and other control agents do NOT propagate this kwarg
    (the validators were designed for the human-out-of-the-loop case
    where ideation has no operator review).

    Returns `None` when the briefing passes; an error-message string
    naming the missing/misnamed/empty section otherwise. The message
    is plumbed verbatim into `_err(...)` by the calling validator
    boundary (`do_operator_queue_append` and `do_board_edit`'s add_*
    paths), so it has to be specific enough that the operator knows
    what to fix without re-reading the validator source.

    History: closes the gap exposed by TB-153, where the MM handler
    authored a briefing using `## Acceptance` instead of `## Verification`
    plus a top-level `## Files to touch` block. `parse_verification_section`
    silently returned `None` and the per-task verifier then skipped the
    task entirely — the briefing-required gate (TB-135) only checked
    "non-empty", not "shape matches what the verifier can read". TB-161
    extends the gate to goal-relevance: a briefing that passes (1)-(3)
    but whose Goal body cites nothing in `goal.md` is the next failure
    mode (ap2-meta-polish drift slipping past the structural check).
    """
    text = briefing_text or ""
    if not text.strip():
        # Defer to TB-135's "briefing is required" gate — that one names
        # the right error for an empty payload.
        return None
    found = _briefing_section_names(text)
    missing = [s for s in BRIEFING_REQUIRED_SECTIONS if s not in found]
    if missing:
        first = missing[0]
        return (
            f"briefing structure invalid: missing section "
            f"`## {first}`. {_BRIEFING_STRUCTURE_HINT}"
        )
    bullets = parse_verification_section(text)
    if bullets is None:
        # `## Verification` heading is present per the structural pass,
        # but `parse_verification_section` couldn't find it via mistune
        # AST. This shouldn't happen given the regex tolerance lines up
        # with `_is_verification_heading`, but if a future briefing
        # quirk slips past one and not the other, refuse rather than
        # ship a briefing the verifier will silently skip.
        return (
            "briefing structure invalid: `## Verification` heading is "
            "present but the verifier can't parse it. "
            f"{_BRIEFING_STRUCTURE_HINT}"
        )
    if not bullets:
        return (
            "briefing structure invalid: `## Verification` section is "
            "empty — the per-task verifier needs at least one bullet "
            "(backticked shell command, test name, or judge-checkable "
            "prose) to score against the agent's diff. "
            f"{_BRIEFING_STRUCTURE_HINT}"
        )
    # TB-170: operator-CLI escape hatch. When `skip_goal_alignment=True`,
    # the TB-161 goal-anchor and TB-164 Why-now checks are skipped while
    # every other gate above (TB-154 canonical sections, TB-138 verifiable
    # bullets via `parse_verification_section`, TB-135 non-empty
    # Verification) keeps firing. The bypass is opt-in at the operator
    # CLI surface only; ideation / MM handler / other control agents do
    # NOT pass this kwarg, so autonomous proposals always go through the
    # full goal-alignment gate.
    if skip_goal_alignment:
        return None
    # TB-161: goal-anchor check. Runs only when goal.md is parseable and
    # contributes derivable anchors — a fresh project whose goal.md is
    # still the all-placeholder template short-circuits to "skip" so we
    # don't reject every proposal on day-one of a new project.
    goal_anchors = _goal_md_anchors(goal_md_path)
    goal_body = _briefing_section_body(briefing_text, "Goal")
    if goal_anchors:
        goal_norm = _normalize_anchor(goal_body)
        if not any(a in goal_norm for a in goal_anchors):
            preview = sorted(goal_anchors)[:5]
            preview_str = ", ".join(f"`{a}`" for a in preview)
            return (
                "briefing structure invalid: `## Goal` body cites no "
                "anchor from goal.md — every proposal must reduce to a "
                "visible step toward the declared project goal "
                "(reject-ap2-meta-polish-drift, TB-161). Reword the "
                "Goal section to quote or reference one of `goal.md`'s "
                "`## Current focus` / `## Done when` headings or a "
                "Done-when bullet. Available anchors include: "
                f"{preview_str}."
            )
    # TB-164: "Why now" rationale check. Line-anchored marker plus a
    # minimum-length rationale so the author articulates goal.md's
    # delete-test ("if we delete this and the goal still ships, was
    # it useful?") in writing. Runs even when goal.md is missing /
    # all-placeholder — the delete-test is intrinsic to the briefing
    # contract, not a goal-relevance check, so it doesn't share the
    # TB-161 anchor-skip fallback.
    rationale = _why_now_paragraph(goal_body)
    if rationale is None:
        return (
            "## Goal section must include a non-empty 'Why now' "
            "rationale (goal.md's delete-test). Add a line beginning "
            "with `Why now` (e.g. `Why now: <one sentence answering "
            "\"if we delete this and the goal still ships, was it "
            "useful?\">`) inside the `## Goal` body. The marker must "
            "start a line — mid-sentence \"why now\" inside arbitrary "
            f"prose doesn't count. Min {WHY_NOW_MIN_CHARS} chars after "
            "the marker (TB-164)."
        )
    if len(rationale) < WHY_NOW_MIN_CHARS:
        return (
            "## Goal section must include a non-empty 'Why now' "
            "rationale (goal.md's delete-test). The `Why now` "
            f"paragraph is only {len(rationale)} chars after the "
            f"marker; minimum is {WHY_NOW_MIN_CHARS}. Articulate the "
            "delete-test answer in writing — name the failure mode "
            "this closes or the gap it fills, not just \"this would "
            "be nice to have\" (TB-164)."
        )
    # TB-171: reject `Manual:` / `[manual]` bullets in `## Verification`.
    # The per-task verifier is unattended — it has the diff, the working
    # tree, and a shell, but no live operator. TB-122 hit `retry_exhausted`
    # on a single manual bullet despite the implementation being complete.
    # The rule lives in three author-side surfaces (ideation prompt,
    # briefing template, ap2-task skill) and as a non-fatal `ap2 check`
    # lint (`_check_briefings_manual_bullets`); this is the queue-append
    # gate that mechanically blocks the malformed briefing before it costs
    # a TB-N + a task-agent run. Only `## Verification` is scanned —
    # bullets in `## Out of scope` (or anywhere else) are fine. We scan
    # raw lines (rather than the parsed `bullets` list) so the regex
    # stays aligned shape-for-shape with `ap2/check.py::_MANUAL_BULLET_RE`,
    # and we walk the text line-by-line (rather than via
    # `_briefing_section_body`) because the latter's heading regex is
    # `\s*`-greedy and can swallow the first list bullet of a section
    # whose body starts with a bullet — which is the structural shape of
    # every well-formed `## Verification` section.
    _verif_heading = re.compile(r"^##[ \t]+Verification\b", re.IGNORECASE)
    _next_heading = re.compile(r"^##[ \t]+")
    in_verification = False
    for line in briefing_text.splitlines():
        if _verif_heading.match(line):
            in_verification = True
            continue
        if in_verification and _next_heading.match(line):
            break
        if in_verification and _MANUAL_BULLET_RE.match(line):
            offending = line.strip()
            return (
                "briefing structure invalid: `## Verification` contains a "
                f"`Manual:` bullet (`{offending}`). Auto-verifiable "
                "bullets only — the unattended per-task verifier cannot "
                "observe a live operator action (TB-122 hit "
                "`retry_exhausted` on exactly this shape, TB-138 pinned "
                "the rule). Convert the bullet to a backticked shell "
                "command, a unit/e2e test name (with stubbed deps for "
                "the operator-observation case), or a judge-checkable "
                "prose claim that names a concrete file/symbol — or "
                "move the bullet to `## Out of scope` if the behavior "
                "genuinely cannot be auto-verified (TB-171)."
            )
    return None


def _allocate_id(board: Board, cfg: Config) -> str:
    """Pure: pick the next TB-N from the existing high-water marks.

    The candidate is `max(board_max + 1, CLAUDE.md next_task_id,
    queue_preallocated_max + 1)` — the third term covers TB-N's that an
    earlier `do_operator_queue_append` reserved on this same tick but
    hasn't yet drained onto the board (so back-to-back `ap2 add` calls
    issue sequential IDs without any of them touching CLAUDE.md).

    TB-141 made this side-effect-free: previously this also wrote
    `cfg.next_task_id` back to CLAUDE.md, which fired
    `task_state_violation` on whichever task was in flight when an
    operator ran `ap2 add` (CLAUDE.md is a fenced path). Persisting the
    new high-water mark is now the caller's responsibility:
      - `do_board_edit` writes synchronously (used by ideation /
        control agents — no in-flight task fence applies).
      - `do_operator_queue_append` does NOT write; the bump is deferred
        to `drain_operator_queue`, which runs as the daemon's first
        tick stage between task agent runs.
    """
    queue_max = _max_preallocated_id_in_queue(cfg)
    candidate = max(board.max_id() + 1, cfg.next_task_id, queue_max + 1)
    # In-memory bookkeeping so a second _allocate_id in the same Config
    # instance doesn't alias the just-issued ID — the disk-side bump
    # happens out of band (caller / drain).
    cfg.next_task_id = candidate + 1
    return f"TB-{candidate}"


def _max_preallocated_id_in_queue(cfg: Config) -> int:
    """Highest `preallocated_task_id` numeric suffix across queue records.

    Returns 0 if the queue is missing / empty / has no preallocated IDs.
    Reads both pending and already-applied records — the operator queue
    is compacted at drain time, so a not-yet-compacted applied record
    still holds a real reservation we mustn't reissue.
    """
    queue_path = operator_queue_path(cfg)
    if not queue_path.exists():
        return 0
    best = 0
    try:
        text = queue_path.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = rec.get("preallocated_task_id") or ""
        if not isinstance(tid, str) or not tid.startswith("TB-"):
            continue
        try:
            n = int(tid[3:])
        except ValueError:
            continue
        if n > best:
            best = n
    return best


# TB-142 (TB-121 cross-ref): the `approve` semantic strips the `review`
# blocker token from a task — both the structural `@blocked:review`
# codespan (TB-132's metadata surface) and any legacy `(blocked on:
# review)` description prose authored before TB-132 landed. Idempotent
# re-render: a task already free of the review token rewrites identically
# (modulo the legacy-description scrub). Shared by:
#   - `do_board_edit({"action":"approve",...})` — the idle-path entry,
#     used by the MM handler's FULL toolset and by direct CLI/control
#     callers.
#   - `_apply_operator_op` for queued `op="approve"` records — the
#     in-flight-task path, where the MM handler RESTRICTED toolset routes
#     through `operator_queue_append` to side-step TB-110's snapshot
#     check (drains run between agent runs, never during).
_APPROVE_LEGACY_REVIEW_RE = re.compile(
    r"\s*\(blocked on:\s*review\s*\)\s*", re.IGNORECASE,
)


def _approve_review_token(board: Board, task_id: str) -> "Task":  # type: ignore[name-defined]
    """Strip the `review` blocker from a task's `@blocked:` codespan AND
    any legacy `(blocked on: review)` description prose. Mutates `board`
    in place. Idempotent — a task without the review token rewrites to
    its current state minus the legacy description clause (cosmetic).

    Raises RuntimeError if the task is not on the board, or if the line
    fails to parse (malformed_lines case — should never happen for tasks
    Board.find returns).
    """
    loc = board.find(task_id)
    if loc is None:
        raise RuntimeError(f"{task_id} not on board")
    section, idx = loc
    line = board.sections[section][idx]
    t = parse_task_line(line, section)
    if t is None:
        raise RuntimeError(f"{task_id}: malformed task line")

    # Codespan: drop the `review` token (case-insensitive). If it was the
    # only token, drop the `@blocked:` codespan entirely so Task.render
    # emits a clean line with no leftover empty span.
    blocked = t.meta.get("blocked", "")
    if blocked:
        kept = [
            tok.strip()
            for tok in blocked.split(",")
            if tok.strip() and tok.strip().lower() != "review"
        ]
        if kept:
            t.meta["blocked"] = ",".join(kept)
        else:
            t.meta.pop("blocked", None)

    # Legacy `(blocked on: review)` description prose — TB-132 moved
    # blockers off description-regex onto codespans, but pre-TB-132 tasks
    # still in flight may carry the prose form. Stripping it keeps the
    # rendered description tidy; structurally it's already a no-op since
    # TB-132 (the legacy fallback only fires when no codespan is set).
    new_desc = _APPROVE_LEGACY_REVIEW_RE.sub(" ", t.description).strip()
    # Normalize whitespace runs that the substitution left behind.
    new_desc = re.sub(r"\s{2,}", " ", new_desc).strip()
    t.description = new_desc

    board.sections[section][idx] = t.render()
    return t


def do_board_edit(cfg: Config, args: dict) -> dict:
    action = args.get("action", "")
    task_id = args.get("task_id")
    title = (args.get("title") or "").strip()
    tags = args.get("tags") or []
    briefing = args.get("briefing")
    description = (args.get("description") or "").strip()
    blocked_on = (args.get("blocked_on") or "").strip()

    # TB-134: reject multi-line title / description / tags up-front so the
    # MCP-driven path (ideation, MM handler) sees the same gate as the CLI.
    # Briefing content is exempt — that's free-form prose and lives in its
    # own file, not on the TASKS.md task line.
    for field_name, value in (
        ("title", title),
        ("description", description),
        ("blocked_on", blocked_on),
    ):
        err = _validate_single_line(field_name, value)
        if err:
            return _err(err)
    for tag in tags:
        err = _validate_single_line("tag", tag)
        if err:
            return _err(err)

    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }
    move_map = {
        "move_to_ready": "Ready",
        "move_to_active": "Active",
        "move_to_frozen": "Frozen",
        "move_to_complete": "Complete",
        "move_to_backlog": "Backlog",
        "move_to_pipeline_pending": "Pipeline Pending",
    }

    # TB-135: briefing is now required for every add_* op. The auto-fill
    # skeleton path (TB-69) generated briefings whose `## Verification`
    # had only a placeholder bullet — the per-task verifier then
    # "passed" prose like "(additional shell or prose bullets)" through
    # the LLM judge with no real diff to score against, completing
    # tasks with zero scope-specific verification (TB-131 hit this on
    # 2026-04-30). Pushing authorship to the caller (CLI:
    # --briefing-file; ideation / MM handler: already construct the
    # payload) closes the gap. Validate BEFORE taking `locked_board`'s
    # save-on-exit lock so a rejected add doesn't side-effect TASKS.md
    # whitespace normalization.
    if action in add_map and not (briefing or "").strip():
        return _err(
            "briefing is required for add actions (TB-135). "
            "Author a briefing markdown with a real "
            "`## Verification` section and pass it as the "
            "`briefing` arg."
        )

    # TB-154: structural validation runs after TB-135's non-empty gate
    # and before `_allocate_id`. A rejected add must not leak a TB-N or
    # write a briefing file to disk. Mirrors the placement of TB-134's
    # single-line check above.
    if action in add_map:
        struct_err = _validate_briefing_structure(
            briefing or "",
            goal_md_path=cfg.project_root / "goal.md",
        )
        if struct_err:
            return _err(struct_err)

    try:
        with locked_board(cfg.tasks_file) as board:
            if action in add_map:
                if not title:
                    return _err("title is required for add actions")
                new_id = _allocate_id(board, cfg)
                briefing_rel = None
                if briefing:
                    slug = slugify(title)
                    brief_path = cfg.tasks_dir / f"{slug}.md"
                    # collision avoidance
                    n = 2
                    while brief_path.exists():
                        brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                        n += 1
                    brief_path.parent.mkdir(parents=True, exist_ok=True)
                    brief_path.write_text(briefing)
                    briefing_rel = str(brief_path.relative_to(cfg.project_root))
                # TB-132: blocked_on goes onto the task line as a
                # `@blocked:<csv>` codespan (alongside `#tags`) rather
                # than being injected into the description as
                # `(blocked on: ...)`. The codespan lives in `meta` and
                # round-trips through Task.render() / parse_task_line.
                meta: dict[str, str] = {}
                if blocked_on:
                    meta["blocked"] = blocked_on
                board.add(
                    add_map[action],
                    task_id=new_id,
                    title=title,
                    tags=tags,
                    meta=meta,
                    description=description,
                    briefing=briefing_rel,
                )
                # TB-141: persist the new high-water mark to CLAUDE.md
                # synchronously here. `_allocate_id` no longer writes —
                # this path (ideation / control agents calling the
                # `board_edit` MCP tool) is never invoked while a task
                # agent is in flight, so the synchronous CLAUDE.md
                # mutation doesn't trip the fenced-file violation
                # check. The deferred-bump pattern only applies to the
                # operator-queue path (`do_operator_queue_append` →
                # `drain_operator_queue`).
                claude_md = cfg.project_root / "CLAUDE.md"
                if claude_md.exists():
                    bump_next_task_id(claude_md, cfg.next_task_id)
                # TB-188: seed a per-proposal record for ideation-authored
                # `add_backlog` (`blocked_on` carries the `review` token).
                # No-op for operator-driven adds (no review marker) and
                # for non-backlog adds. Failures are swallowed so a bad
                # write to the records dir doesn't unwind a successful
                # board edit; the daemon's audit trail (events.jsonl)
                # still carries the canonical `task_added` event.
                if action == "add_backlog" and blocked_on:
                    try:
                        write_ideation_proposal_record(
                            cfg,
                            tb_id=new_id,
                            blocked_on=blocked_on,
                            briefing_text=briefing or "",
                            briefing_rel=briefing_rel,
                        )
                    except OSError:
                        pass
                return _ok(
                    f"{action} {new_id} {title!r}",
                    task_id=new_id,
                    briefing_path=briefing_rel,
                )

            if action in move_map:
                if not task_id:
                    return _err("task_id is required for move actions")
                to_section = move_map[action]
                checked = True if to_section == "Complete" else None
                try:
                    t = board.move(task_id, to_section, check=checked)
                except KeyError:
                    return _err(f"{task_id} not on board")
                return _ok(f"{action} {t.id}", task_id=t.id, section=t.section)

            if action == "remove":
                if not task_id:
                    return _err("task_id is required for remove")
                removed = board.remove(task_id)
                if removed is None:
                    return _err(f"{task_id} not on board")
                return _ok(f"removed {removed.id}", task_id=removed.id)

            if action == "approve":
                # TB-142 (TB-121): strip the `review` blocker so an
                # ideation-proposed Backlog task becomes dispatchable.
                # `_approve_review_token` does the work; we wrap with the
                # `ideation_approved` audit event so the operator-review
                # surface (`ap2 status`, ideation Step 0) can spot the
                # promotion. Restricted-toolset MM handler routes via
                # `operator_queue_append({"op":"approve",...})` instead
                # — same helper, drain-side, post-task-window.
                if not task_id:
                    return _err("task_id is required for approve")
                try:
                    t = _approve_review_token(board, task_id)
                except RuntimeError as e:
                    return _err(str(e))
                events.append(
                    cfg.events_file, "ideation_approved", task=t.id,
                )
                # TB-188: terminal-event reconciliation for the synchronous
                # `do_board_edit` approve surface (matches the drain-side
                # branch in `_apply_operator_op` so both approve routes
                # land identical record-shape outcomes).
                try:
                    reconcile_proposal_outcome(
                        cfg, t.id,
                        decision_kind="approved",
                        decision_actor="operator",
                    )
                except OSError:
                    pass
                return _ok(
                    f"approve {t.id}", task_id=t.id, section=t.section,
                )

            return _err(f"unknown action {action!r}")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def do_pipeline_task_start(cfg: Config, args: dict) -> dict:
    """Launch a long-running pipeline as a detached OS subprocess (TB-114).

    Spawns the command and writes a `pipeline_start` event with name + pid +
    started_at + command + log path. Returns immediately. The daemon
    correlates the spawned pid back to the launching task by walking the
    SDK message stream during `_consume` (see `daemon.run_task` — captures
    `pipeline_task_start` tool calls). After the launch agent emits
    `report_result(status="complete", ...)`, the daemon moves the task to
    the `Pipeline Pending` board section. Each tick, the Pipeline-Pending
    sweep checks every pid's liveness; once all of a task's pipelines have
    died, the daemon runs the original briefing's `## Verification` against
    the now-populated working tree, routing to Complete (pass) or Backlog
    (fail) via `_handle_failure`.

    Pre-TB-114 history: previously took `validation_title` /
    `validation_briefing` and created a separate Backlog validation task
    blocked on `pid:<N>@<TS>`. That two-task pattern was retired — the
    launch task now carries verification itself.
    """
    name = (args.get("name") or "").strip()
    command = (args.get("command") or "").strip()
    if not name or not command:
        return _err("name and command are required")

    log_dir = cfg.project_root / ".cc-autopilot" / "pipelines"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_tmp = log_dir / f"{name}.log.tmp"
    log_handle = log_tmp.open("a")
    try:
        # `start_new_session=True` puts the child in its own session/process
        # group so a parent (daemon) exit doesn't take it down.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cfg.project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    try:
        import psutil

        started_at = int(psutil.Process(proc.pid).create_time())
    except Exception:  # noqa: BLE001
        # Process may have died instantly, or psutil isn't importable. Fall
        # back to wall clock so we still record SOMETHING. PID recycling
        # detection downstream relies on the (pid, started_at) pair.
        started_at = int(time.time())

    log_path = log_dir / f"{name}-{proc.pid}.log"
    try:
        log_tmp.rename(log_path)
    except OSError:
        log_path = log_tmp

    events.append(
        cfg.events_file,
        "pipeline_start",
        name=name,
        pid=proc.pid,
        started_at=started_at,
        command=command,
        log=str(log_path),
    )
    return _ok(
        f"pipeline {name!r} started (pid {proc.pid})",
        pid=proc.pid,
        started_at=started_at,
        log=str(log_path),
    )


def do_cron_edit(cfg: Config, args: dict) -> dict:
    action = args.get("action", "")
    name = args.get("name")
    if not name:
        return _err("name is required")
    try:
        msg, jobs = update_job(
            cfg.cron_file,
            action,
            name=name,
            interval=args.get("interval"),
            prompt=args.get("prompt"),
            active_when=args.get("active_when"),
            max_turns=args.get("max_turns"),
        )
        return _ok(msg, jobs=[j.name for j in jobs])
    except (KeyError, ValueError) as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def do_task_complete(cfg: Config, args: dict) -> dict:
    """Acknowledge a `task_complete` tool call from a task agent (TB-101).

    The structured payload (status / commit / summary / files_changed /
    tests_passed) is captured by `daemon.run_task` walking the SDK
    message stream — this handler exists only to give the SDK a valid
    response so the agent doesn't loop or treat the call as failed. No
    state mutation here; the daemon owns the routing decision after the
    query returns.

    TB-123: cron-proposal moved off `report_result` and into a dedicated
    `cron_propose` MCP tool — the `cron` arg is no longer part of the
    schema. Pre-existing `cron_proposed` event semantics are preserved
    via `do_cron_propose`.

    Replaces the `RESULT:\\n status: ...` free-text contract that
    `result.py` parsed via regex.
    """
    status = args.get("status", "")
    if not isinstance(status, str) or not status.strip():
        return _err("status is required")
    return _ok(f"task_complete acknowledged (status={status})")


def do_cron_propose(cfg: Config, args: dict) -> dict:
    """Propose a recurring cron job for operator review (TB-123).

    Task agents call this to surface "while doing X I noticed Y should
    fire on a schedule" without mutating `cron.yaml` directly. Pre-TB-123
    this lived as a JSON-stringified `cron=` field on `report_result`;
    the dedicated tool gets:
      - structured args (`name` / `schedule` / `prompt` / `rationale`),
        no in-string JSON escaping,
      - per-proposal `cron_proposed` events with rationale (the operator
        review surface — `ap2 cron list` etc. — is what makes them live),
      - failure isolation: a malformed call doesn't take down the
        result-reporting path.

    Pre-TB-146, control agents (cron / ideation) had `cron_edit` for
    direct mutation; that surface was retired (no agent has `cron_edit`
    anymore — operator-CLI-only via `ap2 cron edit`). Task agents
    continue to use this proposal layer; the operator promotes via
    review.

    Args:
      name: short stable identifier, e.g. "weekly-perf-snapshot"
      schedule: interval string ("1h" / "1d" / "30m") — same vocabulary
        cron.yaml accepts; not parsed/validated here, just recorded for
        the operator's read.
      prompt: the prompt body the cron job will use when fired.
      rationale: one short sentence on why this should fire on a
        schedule. Becomes part of the audit trail.

    Emits `cron_proposed` event with all four fields plus
    `proposed_by_task` (taken from the daemon-set contextvar). Does NOT
    mutate `cron.yaml` — the operator review layer handles promotion.
    """
    name = (args.get("name") or "").strip()
    schedule = (args.get("schedule") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    rationale = (args.get("rationale") or "").strip()

    missing = [
        label
        for label, value in (
            ("name", name), ("schedule", schedule),
            ("prompt", prompt), ("rationale", rationale),
        )
        if not value
    ]
    if missing:
        return _err(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required"
        )

    # `proposed_by_task` is sourced from the daemon's contextvar plumb. If
    # not set (unit tests that bypass the daemon, or a control-agent
    # context), `task_id` is "" and the field is omitted.
    task_id = _task_id_ctx.get()
    payload: dict = {
        "name": name,
        "schedule": schedule,
        "prompt": prompt,
        "rationale": rationale,
    }
    if task_id:
        payload["proposed_by_task"] = task_id
    events.append(cfg.events_file, "cron_proposed", **payload)
    return _ok(
        f"proposed cron job {name!r} ({schedule}) for review",
        name=name,
        schedule=schedule,
    )


def do_git_log_grep(cfg: Config, args: dict) -> dict:
    """Search the project's git log for commits whose message matches `query`.

    Replaces the ad-hoc `Bash("git log --grep=...")` that ideation Step
    1.5 used to call (TB-109). Narrow MCP tool means control agents
    don't need shell access for this — `Bash` was the only legitimate
    dependency in CONTROL_AGENT_TOOLS, and dropping it closes the
    shell-redirect-into-fenced-file corruption surface (TB-108 case).

    Returns one line per match: `<short-sha> <subject>`. Capped at 100.
    Subprocess runs git with arg-list (no `shell=True`), so the query
    is shell-safe — it's a single argument to `--grep`, not interpolated.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    try:
        max_results = int(args.get("max_results") or 20)
    except (TypeError, ValueError):
        max_results = 20
    max_results = max(1, min(max_results, 100))

    if not (cfg.project_root / ".git").exists():
        return _ok("not a git repo", matches=[], count=0)

    try:
        proc = subprocess.run(
            [
                "git",
                "-c", "safe.directory=*",
                "-C", str(cfg.project_root),
                "log",
                "--grep", query,
                "--oneline",
                "-n", str(max_results),
            ],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _err("git log timed out after 10s")
    except FileNotFoundError:
        return _err("git not on PATH")
    if proc.returncode != 0:
        return _err(f"git log failed: {proc.stderr.strip()[-300:]}")

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return _ok(
        f"{len(lines)} commit(s) matched {query!r}",
        matches=lines,
        count=len(lines),
    )


def do_operator_log_append(cfg: Config, args: dict) -> dict:
    """Append a timestamped operator-decision line to
    `.cc-autopilot/operator_log.md` (TB-106).

    Operator-owned channel for decisions ideation can't observe via the
    filesystem (e.g. "decided to keep FRAGILE plists as references" or
    "considered the universe-expansion question, deferred"). Ideation
    reads the log in Step 0 and treats logged items as authoritative —
    won't re-propose them in subsequent cycles.

    Two write paths share this handler:
      - operator-side: `ap2 ack [-t TB-N] "<note>"` (CLI)
      - mattermost-handler-side: `operator_log_append` MCP tool when the
        operator sends `@claude-bot done: ...` style messages.

    Each call appends one bullet line. The file is created with a
    short header on first append. `operator_ack` event emitted for
    auditability.
    """
    note = str(args.get("note") or "").strip()
    if not note:
        return _err("note is required")
    task_id = str(args.get("task_id") or "").strip()

    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements. Append-only.\n"
            "Ideation reads this in Step 0; logged items are authoritative —\n"
            "ideation won't re-propose decisions logged here._\n\n"
        )

    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tb_tag = f" [{task_id}]" if task_id else ""
    line = f"- {ts}{tb_tag} — {note}\n"
    with log_path.open("a") as f:
        f.write(line)

    payload: dict = {"note": note[:500]}
    if task_id:
        payload["task"] = task_id
    events.append(cfg.events_file, "operator_ack", **payload)
    return _ok(f"appended to {log_path.name}", line=line.strip())


# ---------------- operator queue (TB-131) ----------------
#
# Operator board mutations (`ap2 add`, `ap2 backlog`, `ap2 unfreeze`,
# `ap2 delete`, plus the MM-handler counterpart) are appended to
# `.cc-autopilot/operator_queue.jsonl` and applied by the daemon's
# `_tick` first stage. This trades immediate write-through for
# serializability against in-flight task / ideation runs:
#   - `git reset --hard <pre_run_head>` rollback never wipes operator
#     adds, because the add isn't in HEAD until the daemon drains the
#     queue between runs.
#   - Ideation reads a stable board snapshot for an entire SDK turn —
#     a queued `ap2 add` arriving mid-thought lands BEFORE ideation's
#     next read, not during it.
#
# ID pre-allocation is done at queue-append time (under the board
# lock) so `ap2 add` can still print the new TB-N immediately. Only
# the TASKS.md insertion is deferred.
#
# TB-141: the queue file itself is intentionally NOT in
# TASK_AGENT_FENCED_PATHS — appends made by the operator while a task
# is in flight used to mis-trip the post-hoc fenced-file snapshot
# check (TB-110), rolling back legitimate task work. Agents have no
# write path to the queue: no Edit/Write permission, no MCP tool that
# emits records under their authority, and the drain-side uuid +
# applied-state bookkeeping ignores any forged record they could
# Bash-shell into the file. The matching CLAUDE.md `Next task ID`
# bump is also deferred — `_allocate_id` is now pure, and
# `drain_operator_queue` writes CLAUDE.md once at end-of-pass.

# Ops the operator-queue path knows how to drain. Shared between the
# CLI (`do_operator_queue_append`) and the drain side
# (`drain_operator_queue`).
OPERATOR_QUEUE_OPS = (
    "add_ready",
    "add_backlog",
    "add_frozen",
    "move_to_backlog",
    "unfreeze",
    "delete",
    # TB-142: approving an ideation-proposed task (strip `@blocked:review`)
    # is the second mutation surface the MM handler exposes via chat
    # commands (`@claude-bot approve TB-N`). Routing it through the queue
    # closes the second instance of the false-positive
    # `task_state_violation` class — TB-141 closed the operator-side `ap2
    # add` instance; this closes the chat-driven `board_edit({"action":
    # "approve",...})` instance. Drain-side handler shares the
    # `_approve_review_token` helper with `do_board_edit`.
    "approve",
    # TB-153: in-place edit of an existing task's `title` / `tags` /
    # `@blocked` codespan / `description` and/or its briefing file.
    # Routed through the same queue-drain path as `add_*` / `delete` /
    # `unfreeze` / `approve` so it never lands inside a task agent's
    # snapshot window. Preserves TB-N (vs. delete + re-add which would
    # orphan every prior reference) and the briefing's slug-stable
    # filename (vs. allocating a new slug, which would orphan git
    # history of `.cc-autopilot/tasks/<slug>.md`).
    "update",
    # TB-152: explicit operator rejection of an ideation-proposed task.
    # Removal semantics mirror `delete` (drop the row + briefing file +
    # emit `task_deleted`) but the audit trail is richer: the drain-side
    # writes `<ts> — rejected ideation proposal → TB-N (<title>):
    # <reason>` to operator_log.md so ideation Step 0 has a signal to
    # avoid re-proposing the same idea next cycle. The `delete` verb
    # remains the generic "remove a task" path; `reject` is specifically
    # "I considered this ideation proposal and decided against it." Pre-
    # validation in `cmd_reject` / chat-side limits the verb to
    # Backlog + `@blocked:review` tasks (ideation proposals); other
    # sections route the operator at `ap2 delete`.
    "reject",
    # TB-159: manual operator trigger for an ideation pass that bypasses
    # the natural empty-board / cooldown / `AP2_IDEATION_DISABLED`
    # gates. Routed through the queue (rather than CLI-spinning its own
    # SDK) so the daemon stays the single owner of the control-agent
    # SDK slot. The drain-side does NOT invoke ideation directly (that
    # would block the board lock for minutes); instead it records an
    # `ideation_forced` event and signals via `drain_operator_queue`'s
    # return dict that the daemon should run `force_ideate` on this
    # tick after the drain completes. TB-194: the queue-append handler
    # has NO board-state read for `ideate` — Active-emptiness is a
    # loop-topology invariant by drain time (the prior `_tick`'s
    # synchronous `run_task` cleared Active back to Complete/Backlog/
    # Frozen before the next `_tick`'s drain stage runs) and `_tick`
    # sequences the post-drain `force_ideate` SDK call before any new
    # task dispatch, so the previously-feared "concurrent task-agent +
    # control-agent SDK runs" interleaving is unreachable. The `force`
    # arg is preserved on the queue payload as audit metadata only.
    "ideate",
    # TB-193: full-file replacement of `goal.md`. Routed through the
    # queue (rather than letting the operator edit goal.md directly
    # while the daemon is running) because ideation reads goal.md
    # mid-cycle (anchors injected into the prompt; `_goal_md_anchors`
    # consulted by `_validate_briefing_structure` at queue-append time
    # for TB-161), and the per-task verifier (TB-69) reads it as part
    # of the rollback-cohesion state surface — an in-place edit racing
    # a snapshot-window write tears against any of those readers. The
    # op carries the new file content + an optional reason; the drain-
    # side performs an atomic tmpfile + `os.replace` write under
    # `board_file_lock` and lands the change in the next `state:
    # drained N operator op(s)` commit. Operator-CLI-only by design —
    # the MM-handler `operator_queue_append` MCP wrapper refuses this
    # op (same precedent as `cron_edit` / `board_edit` being CLI-only
    # post-TB-146 / TB-145). `prompts.py` already documents the design
    # intent: handlers that think goal.md needs updating raise the
    # recommendation in their RESULT summary; the operator applies via
    # `ap2 update-goal`.
    "update_goal",
)


def operator_queue_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"


def operator_queue_state_path(cfg: Config) -> Path:
    return cfg.project_root / ".cc-autopilot" / "operator_queue_state.json"


def do_operator_queue_append(cfg: Config, args: dict) -> dict:
    """Append an operator board op to the daemon-drained queue (TB-131).

    Two write paths share this handler, mirroring how
    `do_operator_log_append` shares CLI + MCP today:
      - operator-side: `ap2 add` / `ap2 backlog` / `ap2 unfreeze` /
        `ap2 delete` route here instead of mutating TASKS.md directly.
      - MM-handler-side: the `operator_queue_append` MCP tool — for
        when @claude-bot is asked to add/move/unfreeze/delete a task
        during an in-flight run, where direct `board_edit` exposes the
        change to `git reset --hard <pre_run_head>` rollback.

    For `add_*` ops, this briefly takes the board lock to (a) write
    the briefing file, (b) pre-allocate a TB-N via `_allocate_id`
    (pure read, no CLAUDE.md write — TB-141), (c) append the queued
    op carrying the pre-allocated TB-N. The operator still gets the
    new ID printed immediately — both the TASKS.md insertion AND the
    CLAUDE.md `next_task_id` bump are deferred to drain. Pre-TB-141
    the bump happened synchronously here, but that mutated a fenced
    path during in-flight task runs and was mis-attributed by TB-110
    as an agent violation (TB-139, 2026-05-01).

    For move/unfreeze/delete ops, validates the target task against
    the current board snapshot under the lock so obvious operator
    errors (typo'd TB-N, unfreeze-on-non-Frozen, delete-from-Active
    without --force) are rejected immediately. The drain path runs
    its own validation too (state may have shifted between queue and
    drain) and emits `operator_queue_error` for any op it can't apply.
    """
    op = (args.get("op") or "").strip()
    if op not in OPERATOR_QUEUE_OPS:
        return _err(
            f"unknown op {op!r}; valid: {list(OPERATOR_QUEUE_OPS)}"
        )

    rec_args: dict[str, Any] = {}
    preallocated_task_id: str | None = None

    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }

    if op in add_map:
        title = (args.get("title") or "").strip()
        if not title:
            return _err("title is required for add ops")
        tags = list(args.get("tags") or [])
        description = (args.get("description") or "").strip()
        blocked_on = (args.get("blocked_on") or "").strip()
        briefing_content = args.get("briefing")

        # TB-134: reject multi-line title / description / tags before
        # writing anything to disk — pre-allocating a TB-N or briefing
        # file for an input we're going to refuse would leak state.
        for field_name, value in (
            ("title", title),
            ("description", description),
            ("blocked_on", blocked_on),
        ):
            err = _validate_single_line(field_name, value)
            if err:
                return _err(err)
        for tag in tags:
            err = _validate_single_line("tag", tag)
            if err:
                return _err(err)

        # TB-135: briefing is required for every add_* op. The
        # auto-fill skeleton path is gone — without a real
        # `## Verification` section the per-task verifier scores prose
        # placeholders against an empty diff and "passes" with zero
        # scope-specific evidence. We refuse before allocating an ID
        # so a rejected add doesn't leak a hole in the TB-N sequence.
        if not (briefing_content or "").strip():
            return _err(
                "briefing is required for add ops (TB-135). Author a "
                "briefing markdown with a real `## Verification` "
                "section and pass it as the `briefing` arg."
            )

        # TB-154: structural gate. Runs before `_allocate_id` /
        # briefing-file write — a rejected add must not leak a TB-N
        # nor materialize an orphan briefing under `.cc-autopilot/tasks/`.
        # TB-161: also passes `goal_md_path` so the goal-anchor check
        # fires here (queue-append-time hard gate).
        # TB-170: `skip_goal_alignment=True` (operator-CLI-only) skips
        # the TB-161 + TB-164 goal-alignment gates while running every
        # other check unchanged. The flag rides on the queue payload
        # so the drain side can re-validate symmetrically.
        skip_goal_alignment = bool(args.get("skip_goal_alignment"))
        struct_err = _validate_briefing_structure(
            briefing_content or "",
            goal_md_path=cfg.project_root / "goal.md",
            skip_goal_alignment=skip_goal_alignment,
        )
        if struct_err:
            return _err(struct_err)

        # TB-132: blocked_on rides on the task line as a `@blocked:<csv>`
        # codespan, not as `(blocked on: ...)` in the description. The
        # drain side reads `meta` from the queue record and passes it to
        # `board.add(..., meta=...)`.
        meta: dict[str, str] = {}
        if blocked_on:
            meta["blocked"] = blocked_on

        # The briefing file isn't under the lock — slug collision
        # avoidance just walks `<slug>-N.md` until it finds a free
        # path; it doesn't depend on TB-N allocation order.
        briefing_rel: str | None = None
        if briefing_content:
            slug = slugify(title)
            brief_path = cfg.tasks_dir / f"{slug}.md"
            n = 2
            while brief_path.exists():
                brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                n += 1
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(briefing_content)
            briefing_rel = str(brief_path.relative_to(cfg.project_root))

        # Allocation + queue append happen under a single
        # `board_file_lock` block (TB-141) so concurrent CLI invocations
        # see each other's preallocations through the queue file:
        # process B's `_allocate_id` reads the queue and finds process
        # A's just-written `preallocated_task_id`, so it allocates
        # process A's id + 1.
        #
        # Pre-TB-141 this serialized implicitly through the synchronous
        # CLAUDE.md bump inside `_allocate_id`; that bump is now
        # deferred to drain (so an `ap2 add` issued during a task run
        # doesn't trip the fenced-file violation check), which removed
        # CLAUDE.md as the cross-process source of truth and pushed the
        # responsibility to the queue file itself.
        rec_uuid = str(_uuid.uuid4())
        rec_ts = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        queue_path = operator_queue_path(cfg)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with board_file_lock(cfg.tasks_file):
            board = Board.load(cfg.tasks_file)
            preallocated_task_id = _allocate_id(board, cfg)
            rec_args = {
                "task_id": preallocated_task_id,
                "title": title,
                "tags": tags,
                "description": description,
                "meta": meta,
                "briefing_path": briefing_rel,
            }
            # TB-170: persist the operator's bypass intent on the queue
            # record so the drain-side audit line can decorate the
            # `applied operator-queued add_backlog → TB-N` line with
            # `(goal-alignment check skipped)` when set. Default-false
            # preserves the historical record shape — only operator-CLI
            # adds with `--skip-goal-alignment` carry the flag.
            if skip_goal_alignment:
                rec_args["skip_goal_alignment"] = True
            rec: dict[str, Any] = {
                "uuid": rec_uuid,
                "op": op,
                "args": rec_args,
                "ts": rec_ts,
                "preallocated_task_id": preallocated_task_id,
            }
            with queue_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        events.append(
            cfg.events_file,
            "operator_queue_append",
            uuid=rec["uuid"],
            op=op,
            task=preallocated_task_id,
        )
        return _ok(
            f"queued {op} → {preallocated_task_id}",
            uuid=rec["uuid"],
            op=op,
            task_id=preallocated_task_id,
        )
    elif op == "ideate":
        # TB-159 / TB-194: manual ideation trigger. The op carries no
        # task_id — append-time validation is intentionally minimal
        # (no board-state read). The drain-side does NOT invoke
        # ideation (that would hold the board lock for minutes); it
        # only emits the `ideation_forced` audit event and signals the
        # daemon to run `force_ideate` after the drain completes.
        #
        # TB-194: the prior at-append-time Active hard gate (with
        # `force=true` as escape hatch) has been removed. The
        # rationale was guarding "concurrent task-agent + control-
        # agent SDK runs share the same in-process slot", but the
        # interleaving is benign by current loop topology: the drain
        # runs as `_tick`'s first stage, BEFORE task dispatch, AFTER
        # the previous tick's synchronous `run_task` already cleared
        # Active back to Complete/Backlog/Frozen. The post-drain
        # `force_ideate` SDK call also runs within the same `_tick`,
        # sequentially before task dispatch — there's no path for it
        # to overlap a task-agent SDK run on the same loop. The
        # `force` arg is captured on the queue payload as audit-only
        # metadata (kept for one release; deprecation can come later
        # if the noise accumulates).
        force = bool(args.get("force"))
        rec_args = {"force": force}
    elif op == "update_goal":
        # TB-193: full-file replacement of `goal.md`. The op carries the
        # full file content (no diff/patch — symmetric to how `add_*` ops
        # carry the full briefing payload, atomic-write semantics are
        # simpler than 3-way merge, and goal.md is small enough that the
        # size cost is negligible). `reason` is optional, single-line per
        # TB-134, and feeds the operator-log audit line.
        goal_content = args.get("goal_content")
        if not isinstance(goal_content, str) or not goal_content.strip():
            return _err(
                "goal_content is required for update_goal (non-empty "
                "string; whitespace-only is rejected)"
            )
        # Parser sanity-check: a goal.md whose anchor extraction blows up
        # would silently break TB-161 / ideation prompts later. Empty
        # anchor list is OK — placeholder goal.md is a documented valid
        # state per `check.py:226-271`; a parser exception is not.
        try:
            _goal_md_anchors_from_text(goal_content)
        except Exception as e:  # noqa: BLE001
            return _err(
                f"goal_content failed to parse "
                f"({type(e).__name__}: {e}); refusing to queue"
            )
        raw_reason = args.get("reason")
        reason = (raw_reason if raw_reason is not None else "").strip()
        if reason:
            err = _validate_single_line("reason", reason)
            if err:
                return _err(err)
        rec_args = {
            "goal_content": goal_content,
            "reason": reason,
        }
    else:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err(f"task_id is required for {op}")
        # Snapshot validation under the board lock — the drain path
        # re-validates too (state may shift) but rejecting obvious
        # operator errors immediately keeps the UX honest.
        with board_file_lock(cfg.tasks_file):
            board = Board.load(cfg.tasks_file)
            loc = board.find(task_id)
            existing = board.get(task_id) if loc else None
        if loc is None:
            return _err(f"{task_id} not on board")
        section = loc[0]
        if op == "unfreeze" and section != "Frozen":
            return _err(
                f"{task_id} is in {section}, not Frozen — "
                f"use `ap2 backlog {task_id}` for non-frozen moves"
            )
        if op == "delete" and section in ("Active", "Ready", "Pipeline Pending") \
                and not args.get("force"):
            return _err(
                f"{task_id} is in {section} — refusing without force. "
                f"Use `ap2 backlog {task_id}` first, or pass --force."
            )
        if op == "reject":
            # TB-152: `reject` is the explicit "operator considered this
            # ideation proposal and decided against it" path. The verb is
            # narrower than `delete` by design — it only fires on
            # Backlog tasks with the `@blocked:review` codespan still
            # present, i.e. unapproved ideation proposals. Anything else
            # (Active runs, Ready dispatches, already-approved tasks,
            # Frozen failures) routes the operator at `ap2 delete`,
            # which carries the generic remove semantics. This keeps
            # the audit-line distinction clean: `rejected ideation
            # proposal → TB-N: <reason>` only ever describes a real
            # ideation rejection.
            blocked_csv = (existing.meta.get("blocked", "") if existing else "")
            blocked_tokens = [
                tok.strip().lower() for tok in blocked_csv.split(",") if tok.strip()
            ]
            if section != "Backlog" or "review" not in blocked_tokens:
                return _err(
                    f"{task_id} is not a pending-review proposal "
                    f"(section={section}, "
                    f"@blocked={blocked_csv or '(none)'}) — "
                    f"use `ap2 delete {task_id}` instead. `reject` is "
                    f"reserved for Backlog tasks still gated by "
                    f"`@blocked:review` (ideation proposals)."
                )
        rec_args = {"task_id": task_id}
        if op == "delete":
            rec_args["force"] = bool(args.get("force"))
        if op == "reject":
            # TB-152: snapshot the title under the board lock so the
            # drain-side audit line ("<ts> — rejected ideation proposal
            # → TB-N (<title>): <reason>") doesn't have to re-look it
            # up after `board.remove` has dropped the row. Reason is
            # single-line per TB-134; the placeholder `(no reason
            # given)` is itself a signal — ideation can spot the
            # difference between rejected-with-reason and rejected-
            # silently and decide whether to re-propose.
            raw_reason = args.get("reason")
            reason = (raw_reason if raw_reason is not None else "").strip()
            if reason:
                err = _validate_single_line("reason", reason)
                if err:
                    return _err(err)
            else:
                reason = "(no reason given)"
            rec_args["title"] = existing.title if existing else ""
            rec_args["reason"] = reason
        if op == "update":
            # TB-153: in-place edit. Translate the public CLI / MCP shape
            # (title / tags / blocked / description / briefing flags +
            # explicit `clear_tags` / `clear_blocked`) into the queue
            # record's update_kwargs dialect (title / tags / description /
            # briefing / meta_set / meta_clear) the drain branch consumes
            # via `Board.update`.
            #
            # Field-presence convention: a key in `args` with a non-None
            # value means "set this field"; a missing key means "leave
            # unchanged." `clear_tags` and `clear_blocked` are explicit
            # bools so an operator who really means "clear" doesn't have
            # to encode that as `--tags ""` (ambiguous: typo vs intent).
            update_err = _validate_update_args(args)
            if update_err:
                return _err(update_err)

            # Per-target fence (TB-153 design): mirrors `delete`'s fence —
            # keyed on the target's section, not directory-wide. Other
            # tasks running is fine; what matters is whether THIS task is
            # in flight (Active or Pipeline Pending).
            briefing_content = args.get("briefing")
            has_briefing_edit = (
                briefing_content is not None
                and str(briefing_content).strip() != ""
            )
            if section in ("Active", "Pipeline Pending"):
                if has_briefing_edit:
                    # Hard-refused with no `--force` escape — the agent
                    # may re-read its briefing mid-run via `Read` and
                    # TB-110's snapshot may hash the file. Deferred-draft
                    # handling is carved out as a follow-up; the fence
                    # covers the 90% case where edits target Backlog /
                    # Ready / Frozen.
                    return _err(
                        f"{task_id} is in {section} — briefing-content "
                        f"edits to a running task are refused (the agent "
                        f"may re-read its briefing mid-run; TB-110 "
                        f"snapshot hash). Wait for the task to leave "
                        f"{section}, or update only board-line fields."
                    )
                if not args.get("force"):
                    return _err(
                        f"{task_id} is in {section} — refusing update "
                        f"without --force. Pass --force to edit "
                        f"board-line fields (title / tags / blocked / "
                        f"description); briefing-content edits remain "
                        f"refused."
                    )

            # TB-154: structural gate on briefing-content edits. Runs
            # before the briefing file is written below so a rejected
            # update doesn't materialize a partial / invalid briefing
            # on disk (the slug-stable write would otherwise overwrite
            # the prior good briefing with the rejected payload). Same
            # rule as the `add_*` boundary — `## Goal`, `## Scope`,
            # `## Design`, `## Verification`, `## Out of scope`, plus a
            # parseable & non-empty Verification section. Closes the
            # symmetric hole flagged by the per-task verifier on
            # TB-154's first attempt: a briefing replaced via `update`
            # could otherwise still slip past the structural check the
            # `add_*` paths now enforce.
            # TB-170: `skip_goal_alignment=True` from the CLI bypasses
            # TB-161 + TB-164 on briefing-content edits as well. Runs
            # every other validation (TB-154 canonical sections,
            # parseable + non-empty Verification) unchanged.
            update_skip_goal_alignment = bool(args.get("skip_goal_alignment"))
            if has_briefing_edit:
                struct_err = _validate_briefing_structure(
                    str(briefing_content),
                    goal_md_path=cfg.project_root / "goal.md",
                    skip_goal_alignment=update_skip_goal_alignment,
                )
                if struct_err:
                    return _err(struct_err)

            # Build the update payload + the `fields=[...]` diff list
            # the drain emits on the `task_updated` event.
            fields: list[str] = []
            if "title" in args and args["title"] is not None:
                rec_args["title"] = str(args["title"])
                fields.append("title")
            if args.get("clear_tags"):
                rec_args["tags"] = []
                fields.append("tags")
            elif "tags" in args and args["tags"] is not None:
                rec_args["tags"] = list(args["tags"])
                fields.append("tags")
            if "description" in args and args["description"] is not None:
                rec_args["description"] = str(args["description"])
                fields.append("description")
            if args.get("clear_blocked"):
                rec_args["meta_clear"] = ["blocked"]
                fields.append("blocked")
            elif "blocked" in args and args["blocked"] is not None:
                rec_args["meta_set"] = {"blocked": str(args["blocked"])}
                fields.append("blocked")

            # Briefing path resolution: write at queue-append time so the
            # update is durable across daemon restarts. Slug-stable —
            # overwrite the existing file when the task already has a
            # briefing path; allocate a fresh slug (from the CURRENT
            # title, not the new one) only for legacy / pre-TB-135 tasks
            # that have no briefing on disk yet. Title changes never
            # rename the briefing — file-name staleness is the accepted
            # trade-off for keeping git history of the briefing file
            # contiguous (TB-153 design's "Locked decisions").
            if has_briefing_edit:
                if existing and existing.briefing:
                    brief_path = cfg.project_root / existing.briefing
                    briefing_rel = existing.briefing
                else:
                    slug = slugify(existing.title if existing else task_id)
                    brief_path = cfg.tasks_dir / f"{slug}.md"
                    n = 2
                    while brief_path.exists():
                        brief_path = cfg.tasks_dir / f"{slug}-{n}.md"
                        n += 1
                    briefing_rel = str(brief_path.relative_to(cfg.project_root))
                brief_path.parent.mkdir(parents=True, exist_ok=True)
                brief_path.write_text(briefing_content)
                rec_args["briefing"] = briefing_rel
                fields.append("briefing")

            if not fields:
                return _err(
                    "update op requires at least one field to change "
                    "(title / tags / blocked / description / briefing). "
                    "Pass `clear_tags=true` / `clear_blocked=true` for "
                    "explicit clears."
                )
            rec_args["fields"] = fields
            # TB-170: persist the bypass intent on the queue record. Only
            # meaningful when the update carried a briefing edit (the
            # validator only fires on briefing-content updates), but
            # storing it unconditionally keeps the audit-line shape
            # consistent across record types.
            if update_skip_goal_alignment:
                rec_args["skip_goal_alignment"] = True

    # Non-add ops: no preallocation, no lock needed for the queue write
    # (the record is opaque to `_allocate_id`'s queue-max scan).
    rec: dict[str, Any] = {
        "uuid": str(_uuid.uuid4()),
        "op": op,
        "args": rec_args,
        "ts": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    queue_path = operator_queue_path(cfg)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    events.append(
        cfg.events_file,
        "operator_queue_append",
        uuid=rec["uuid"],
        op=op,
        task=rec_args.get("task_id", ""),
    )
    return _ok(
        f"queued {op}",
        uuid=rec["uuid"],
        op=op,
        task_id=rec_args.get("task_id", ""),
    )


def operator_queue_pending_count(cfg: Config) -> int:
    """Number of queued ops that haven't yet been drained.

    Surfaced by `ap2 status` so operators can spot a stalled daemon
    (queue depth > 0 with the daemon not running == ops stuck pending).
    """
    queue_path = operator_queue_path(cfg)
    if not queue_path.exists():
        return 0
    applied = _load_operator_queue_applied(operator_queue_state_path(cfg))
    count = 0
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("uuid") in applied:
            continue
        count += 1
    return count


def drain_operator_queue(cfg: Config) -> dict:
    """Apply queued operator ops as the first stage of each daemon tick
    (TB-131).

    Holds `board_file_lock` for the duration of the drain so concurrent
    CLI / MCP appends serialize against application. Each op:

      1. Has its uuid checked against
         `.cc-autopilot/operator_queue_state.json` — already-applied
         uuids are skipped (idempotent across crash-restart).
      2. Is dispatched through `_apply_operator_op` to the
         appropriate primitive (board.add / board.move / board.remove
         + retry-state reset for unfreeze + audit events).
      3. Records its uuid into the state file BEFORE moving on (so a
         crash mid-drain doesn't re-apply the op next tick).
      4. Writes a one-line audit summary to operator_log.md.

    Failures (op references a task that vanished, etc.) are recorded
    with `operator_queue_error` events but the uuid is still marked
    applied — silently failing forever is worse than letting the
    operator see one error and move on.

    TB-141: at end-of-drain, also bumps CLAUDE.md's `Next task ID` to
    `max(highest preallocated TB-N this pass + 1, current next_id)`.
    The synchronous bump that used to live in `_allocate_id` was
    retired so an `ap2 add` issued during a task run doesn't trip the
    fenced-file violation check; this is the corollary that keeps
    CLAUDE.md current. One write per drain pass instead of one per
    add. Drains that applied only move/unfreeze/delete ops leave
    CLAUDE.md untouched.

    Returns a dict with `applied` (count), `touched_paths` (state
    files dirtied), and `force_ideate` (TB-159 — set to True if any
    drained op was an `ideate` signal, telling the daemon to run
    `ideation.force_ideate` on this same tick after the drain releases
    the board lock).
    """
    queue_path = operator_queue_path(cfg)
    state_path = operator_queue_state_path(cfg)
    if not queue_path.exists() or queue_path.stat().st_size == 0:
        return {"applied": 0, "touched_paths": [], "force_ideate": False}

    applied = _load_operator_queue_applied(state_path)
    pending: list[dict] = []
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("uuid") in applied:
            continue
        pending.append(rec)

    if not pending:
        # No new ops; opportunistically compact in case the queue file
        # has accumulated already-applied uuids.
        _compact_operator_queue(queue_path, applied)
        return {"applied": 0, "touched_paths": [], "force_ideate": False}

    applied_count = 0
    touched: set[str] = set()
    highest_alloc = 0
    # TB-159: track whether any drained op was an `ideate` signal so the
    # daemon can run the forced ideation pass on this same tick (after
    # the drain releases the board lock). Consumed by `_tick` via the
    # return dict's `force_ideate` key.
    force_ideate_pending = False
    with board_file_lock(cfg.tasks_file):
        for rec in pending:
            try:
                board = Board.load(cfg.tasks_file)
                _apply_operator_op(cfg, board, rec)
                board.save()
                _append_operator_audit_line(cfg, rec)
                applied_count += 1
                if rec.get("op") == "ideate":
                    force_ideate_pending = True
                touched.update(
                    [
                        "TASKS.md",
                        "CLAUDE.md",
                        ".cc-autopilot/retry_state.json",
                        ".cc-autopilot/operator_log.md",
                        ".cc-autopilot/tasks",
                        # TB-188: drain-side `approve` / `reject` /
                        # `delete` may amend the per-proposal record's
                        # `outcome` block. Listed here unconditionally
                        # so `_commit_state_files` lands the rewrite in
                        # the same `state: drained N operator op(s)`
                        # commit. `_filter_state_paths` drops the dir
                        # when nothing inside it changed (the typical
                        # case for ops on non-ideation tasks).
                        ".cc-autopilot/ideation_proposals",
                    ]
                )
                # TB-193: `update_goal` writes the new goal.md content
                # under the lock; surface the path so the drain-side
                # `_commit_state_files` allowlist (TB-126) lands the
                # change in the same `state: drained N operator op(s)`
                # commit. Conditional rather than unconditional so a
                # drain pass that didn't actually touch goal.md doesn't
                # try to stage a clean working copy of it.
                if rec.get("op") == "update_goal":
                    touched.add("goal.md")
                # TB-141: track the highest preallocated TB-N across the
                # drain so we can bump CLAUDE.md once at the end (instead
                # of once per `_allocate_id` call inside
                # `do_operator_queue_append`).
                tid = rec.get("preallocated_task_id") or ""
                if isinstance(tid, str) and tid.startswith("TB-"):
                    try:
                        n = int(tid[3:])
                    except ValueError:
                        n = 0
                    if n > highest_alloc:
                        highest_alloc = n
            except Exception as e:  # noqa: BLE001
                events.append(
                    cfg.events_file,
                    "operator_queue_error",
                    uuid=rec.get("uuid", ""),
                    op=rec.get("op", ""),
                    error=f"{type(e).__name__}: {e}",
                )
            finally:
                # Mark applied (or attempted) regardless of success —
                # silently re-applying a broken op every tick is worse
                # than recording the error once and moving on. Operator
                # can inspect events.jsonl for the failure cause.
                applied.add(rec["uuid"])
                _save_operator_queue_applied(state_path, applied)
        _compact_operator_queue(queue_path, applied)

        # TB-141: end-of-drain CLAUDE.md bump. The synchronous bump
        # inside `_allocate_id` was retired so an `ap2 add` issued
        # while a task agent is in flight doesn't trip TB-110's
        # fenced-file violation check (CLAUDE.md is fenced; the
        # mid-flight mutation looks identical to an agent forging the
        # file). The drain runs as the daemon's first tick stage —
        # between agent runs — so the bump here is safe. We bump once
        # to the highest TB-N seen across this drain pass; sequential
        # drains compound naturally because each reads CLAUDE.md fresh
        # via `cfg.next_task_id`.
        if highest_alloc and applied_count:
            new_next = max(highest_alloc + 1, cfg.next_task_id)
            claude_md = cfg.project_root / "CLAUDE.md"
            if claude_md.exists():
                bump_next_task_id(claude_md, new_next)
            cfg.next_task_id = new_next

    if applied_count:
        events.append(
            cfg.events_file,
            "operator_queue_drained",
            applied=applied_count,
        )
    return {
        "applied": applied_count,
        "touched_paths": sorted(touched),
        "force_ideate": force_ideate_pending,
    }


def _apply_operator_op(cfg: Config, board: Board, rec: dict) -> None:
    op = rec.get("op", "")
    args = rec.get("args") or {}
    add_map = {
        "add_ready": "Ready",
        "add_backlog": "Backlog",
        "add_frozen": "Frozen",
    }
    if op in add_map:
        if not args.get("task_id") or not args.get("title"):
            raise RuntimeError("add op missing task_id or title")
        board.add(
            add_map[op],
            task_id=args["task_id"],
            title=args["title"],
            tags=list(args.get("tags") or []),
            # TB-132: meta dict carries the `@blocked:...` codespan (and
            # any future `@<key>:<value>` structured fields). Defaults
            # to {} for queued ops authored before TB-132 landed.
            meta=dict(args.get("meta") or {}),
            description=args.get("description") or "",
            briefing=args.get("briefing_path"),
        )
        return
    if op == "move_to_backlog":
        try:
            board.move(args["task_id"], "Backlog")
        except KeyError:
            raise RuntimeError(f"{args.get('task_id', '?')} not on board")
        return
    if op == "unfreeze":
        loc = board.find(args.get("task_id", ""))
        if loc is None:
            raise RuntimeError(f"{args.get('task_id', '?')} not on board")
        if loc[0] != "Frozen":
            raise RuntimeError(
                f"{args['task_id']} is in {loc[0]}, not Frozen"
            )
        board.move(args["task_id"], "Backlog")
        retry.reset_attempt(cfg.retry_state_file, args["task_id"])
        events.append(cfg.events_file, "task_unfrozen", task=args["task_id"])
        return
    if op == "delete":
        loc = board.find(args.get("task_id", ""))
        if loc is None:
            raise RuntimeError(f"{args.get('task_id', '?')} not on board")
        section = loc[0]
        if section in ("Active", "Ready", "Pipeline Pending") and not args.get("force"):
            raise RuntimeError(
                f"{args['task_id']} is in {section}; refusing delete without force"
            )
        existing = board.get(args["task_id"])
        title = existing.title if existing else ""
        board.remove(args["task_id"])
        events.append(
            cfg.events_file,
            "task_deleted",
            task=args["task_id"],
            section=section,
            title=title,
        )
        # TB-188: terminal-event reconciliation. No-op when no proposal
        # record exists (legacy / non-ideation tasks); otherwise stamps
        # `outcome.decision_kind=deleted` with the operator actor. Reason
        # stays empty for `delete` — the matching operator_log.md line
        # carries no free-text reason (the verb itself is the audit).
        try:
            reconcile_proposal_outcome(
                cfg, args["task_id"],
                decision_kind="deleted",
                decision_actor="operator",
            )
        except OSError:
            pass
        return
    if op == "reject":
        # TB-152: shares `delete`'s removal codepath — drop the row +
        # briefing file (briefing-file removal is implicit: `Board.remove`
        # only drops the line; the briefing under `.cc-autopilot/tasks/`
        # is unlinked here so a future re-add doesn't collide on slug).
        # Emits `task_deleted` (same event shape as `delete` — the
        # operator-log.md line is what carries the reject-vs-delete
        # distinction). The `<ts> — rejected ideation proposal → TB-N
        # (<title>): <reason>` line is written by
        # `_append_operator_audit_line`'s reject branch using the title +
        # reason snapshotted into the queue record at append time.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("reject op missing task_id")
        loc = board.find(tid)
        if loc is None:
            raise RuntimeError(f"{tid} not on board")
        section = loc[0]
        existing = board.get(tid)
        title = existing.title if existing else args.get("title", "")
        briefing_rel = existing.briefing if existing else None
        board.remove(tid)
        if briefing_rel:
            brief_path = cfg.project_root / briefing_rel
            try:
                brief_path.unlink()
            except FileNotFoundError:
                pass
        events.append(
            cfg.events_file,
            "task_deleted",
            task=tid,
            section=section,
            title=title,
        )
        # TB-188: terminal-event reconciliation. The reject path always
        # carries a `reason` arg (snapshotted into the queue record at
        # append time, defaulting to "(no reason given)"). Stamp it into
        # the record's `outcome.reason` so the same operator-authored
        # rationale lives in two places: the human-readable
        # operator_log.md line AND the structured per-proposal record
        # the signal-collection follow-ups (TB-189) query.
        try:
            reconcile_proposal_outcome(
                cfg, tid,
                decision_kind="rejected",
                decision_actor="operator",
                reason=str(args.get("reason") or ""),
            )
        except OSError:
            pass
        return
    if op == "ideate":
        # TB-159: drain-side `ideate` is a signal, not an action. The
        # actual ideation run is dispatched by the daemon's `_tick`
        # AFTER the drain releases the board lock — running the SDK
        # call here would hold `board_file_lock` for minutes and
        # serialize every other operator op + the cron / task /
        # status-report stages behind it. Emit the audit event so
        # post-hoc inspection distinguishes manual fires from natural
        # ones; the operator-queue-drain return dict ferries the
        # `force_ideate` signal up to `_tick`.
        events.append(
            cfg.events_file,
            "ideation_forced",
            force=bool(args.get("force")),
        )
        return
    if op == "update_goal":
        # TB-193: full-file replacement of `goal.md`. Atomic write —
        # tmpfile + `os.replace` — so a concurrent reader (ideation
        # mid-cycle, the per-task verifier reading the rollback-cohesion
        # state surface) can't observe a partial file. We hold
        # `board_file_lock` for the full drain (caller's responsibility),
        # so the rename plus the `state: drained N operator op(s)`
        # commit together form a single observable transition for any
        # subsequent reader.
        goal_content = args.get("goal_content")
        reason = args.get("reason") or ""
        if not isinstance(goal_content, str) or not goal_content.strip():
            raise RuntimeError(
                "update_goal op missing non-empty goal_content"
            )
        target = cfg.project_root / "goal.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(goal_content)
        os.replace(tmp, target)
        events.append(
            cfg.events_file,
            "goal_updated",
            reason=reason,
            bytes=len(goal_content),
        )
        return
    if op == "approve":
        # TB-142: drain-side approve. Shares `_approve_review_token` with
        # `do_board_edit({"action":"approve",...})` (the idle-path entry)
        # so both routes leave the task in the same state — codespan
        # `@blocked:review` stripped, legacy `(blocked on: review)` prose
        # scrubbed. Audit event mirrors the direct-call path.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("approve op missing task_id")
        _approve_review_token(board, tid)
        events.append(cfg.events_file, "ideation_approved", task=tid)
        # TB-188: terminal-event reconciliation for the operator-approval
        # path. The approve verb strips `@blocked:review` and lets the
        # task become dispatchable — from the proposal's perspective the
        # operator has weighed in and said "yes." Subsequent
        # task_complete events for this TB-N find the outcome already
        # set and silently no-op (idempotent first-write wins).
        try:
            reconcile_proposal_outcome(
                cfg, tid,
                decision_kind="approved",
                decision_actor="operator",
            )
        except OSError:
            pass
        return
    if op == "update":
        # TB-153: drain-side update. The queue-append handler already
        # wrote the briefing file (slug-stable) when `briefing` was in
        # the update payload, so this branch only mutates the task line
        # via `Board.update`. The `fields=[...]` list is what the
        # queue-append handler computed — it's the diff the operator's
        # CLI / MM-handler call asked for, and we forward it verbatim
        # onto the audit event so post-mortems can grep
        # `task_updated fields=[blocked]` etc.
        tid = args.get("task_id", "")
        if not tid:
            raise RuntimeError("update op missing task_id")
        update_kwargs: dict[str, Any] = {}
        if "title" in args:
            update_kwargs["title"] = args["title"]
        if "tags" in args:
            update_kwargs["tags"] = list(args["tags"] or [])
        if "description" in args:
            update_kwargs["description"] = args["description"]
        if "briefing" in args:
            update_kwargs["briefing"] = args["briefing"]
        if args.get("meta_set"):
            update_kwargs["meta_set"] = dict(args["meta_set"])
        if args.get("meta_clear"):
            update_kwargs["meta_clear"] = list(args["meta_clear"])
        try:
            board.update(tid, **update_kwargs)
        except KeyError:
            raise RuntimeError(f"{tid} not on board")
        fields = list(args.get("fields") or [])
        events.append(
            cfg.events_file,
            "task_updated",
            task=tid,
            fields=fields,
        )
        return
    raise RuntimeError(f"unknown op {op!r}")


def _append_operator_audit_line(cfg: Config, rec: dict) -> None:
    """One-line audit entry to operator_log.md per TB-131 scope (5)."""
    log_path = cfg.project_root / ".cc-autopilot" / "operator_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Operator log\n\n"
            "_Operator decisions and action acknowledgements. Append-only.\n"
            "Ideation reads this in Step 0; logged items are authoritative —\n"
            "ideation won't re-propose decisions logged here._\n\n"
        )
    op = rec.get("op", "?")
    args = rec.get("args") or {}
    task = args.get("task_id", "")
    ts = rec.get("ts") or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    arrow = f" → {task}" if task else ""
    # TB-159: distinguish manual ideation fires from natural cron-driven
    # ones in the operator log (`applied operator-queued ideate →
    # (forced)` vs no log line at all for the natural path). Ideation
    # Step 0 reads operator_log.md as ground truth on operator
    # decisions; the `(forced)` decoration is the human-readable signal.
    if op == "ideate":
        arrow = " → (forced)"
    # TB-170: when the operator-CLI bypass flag was set on an add_* /
    # update op, decorate the audit line with `(goal-alignment check
    # skipped)` so future ideation cycles can grep operator_log.md for
    # the `goal-alignment check skipped` substring and decide whether
    # to count the task toward "operator-validated work" vs
    # "operator-bypassed-validation work" — useful signal for the
    # rejection-reasons loop (TB-152) without a separate event type.
    suffix = ""
    if args.get("skip_goal_alignment"):
        suffix = " (goal-alignment check skipped)"
    lines: list[str] = [
        f"- {ts} — applied operator-queued {op}{arrow}{suffix}\n"
    ]
    if op == "update_goal":
        # TB-193: in addition to the standard `applied operator-queued
        # update_goal` line above (the verb-vs-other-ops distinction),
        # emit the richer `<ts> — operator updated goal.md (<reason>)`
        # line that future ideation cycles read as a "goal drift event"
        # signal. Empty reason collapses to `<ts> — operator updated
        # goal.md` (no parens).
        reason = (args.get("reason") or "").strip()
        reason_part = f" ({reason})" if reason else ""
        lines.append(
            f"- {ts} — operator updated goal.md{reason_part}\n"
        )
    if op == "reject":
        # TB-152: in addition to the standard `applied operator-queued
        # reject → TB-N` audit line above (so the reject vs. delete
        # distinction shows up in the verb), emit the richer
        # `<ts> — rejected ideation proposal → TB-N (<title>): <reason>`
        # line that ideation Step 0 reads as ground truth on operator
        # decisions. Title + reason were snapshotted into the queue
        # record at append time so this branch doesn't have to re-look
        # them up post-`board.remove`.
        title = args.get("title", "") or ""
        reason = args.get("reason", "") or "(no reason given)"
        title_part = f" ({title})" if title else ""
        lines.append(
            f"- {ts} — rejected ideation proposal{arrow}"
            f"{title_part}: {reason}\n"
        )
    with log_path.open("a") as f:
        for line in lines:
            f.write(line)


def _load_operator_queue_applied(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    items = data.get("applied")
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items}


def _save_operator_queue_applied(state_path: Path, applied: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"applied": sorted(applied)}, indent=2))
    tmp.replace(state_path)


def _compact_operator_queue(queue_path: Path, applied: set[str]) -> None:
    """Rewrite the queue file dropping fully-applied uuids, keeping any
    un-applied lines (e.g. ones that arrived between two drains) intact.

    Called after each successful drain so the file doesn't grow
    unbounded. `applied` is the set of uuids known to have been applied
    (or attempted-and-recorded); anything not in it is preserved.
    """
    if not queue_path.exists():
        return
    pending_lines: list[str] = []
    for raw in queue_path.read_text().splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Preserve unparseable lines so an operator can inspect
            # them rather than silently losing the record.
            pending_lines.append(line)
            continue
        if rec.get("uuid") in applied:
            continue
        pending_lines.append(line)
    if pending_lines:
        queue_path.write_text("\n".join(pending_lines) + "\n")
    else:
        queue_path.write_text("")


def do_ideation_state_write(cfg: Config, args: dict) -> dict:
    """Overwrite `.cc-autopilot/ideation_state.md` with a fresh assessment (TB-90).

    Called by the ideation cron in Step 0 to land the per-cycle progress
    assessment introduced by TB-87. The content is written verbatim — schema
    correctness is the prompt's responsibility, not the tool's. Atomic write
    (tmpfile + rename) so a concurrent reader can't observe a partial file.

    Reads stay through the existing `Read` tool — this tool only wraps the
    write path. Same pattern as `board_edit` / `cron_edit`: broad reads,
    narrow writes.
    """
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return _err("content is required")
    # Soft cap to surface runaway prompts. The TB-87 schema aims for ~200
    # lines (~10-20KB); 50KB leaves headroom for legitimate verbose
    # assessments without letting the file grow unbounded.
    if len(content) > 50_000:
        return _err(
            f"content too long ({len(content)} bytes); aim for <50KB. "
            "Trim to highest-signal items per the prompt's length cap."
        )
    target = (
        cfg.project_root
        / ".cc-autopilot"
        / "ideation_state.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(target)
    events.append(
        cfg.events_file,
        "ideation_state_updated",
        bytes=len(content),
    )
    return _ok(
        f"wrote {len(content)} bytes to ideation_state.md",
        bytes=len(content),
    )


def do_log_event(cfg: Config, args: dict) -> dict:
    typ = args.get("type") or "info"
    summary = args.get("summary") or ""
    evt = events.append(cfg.events_file, typ, summary=summary)
    return _ok(f"logged {typ}", event=evt)


async def do_status_report_run(cfg: Config, args: dict) -> dict:
    """Trigger an on-demand status report (TB-144).

    Routes the operator's "@claude-bot status" through the same shared
    `ap2.status_report.run_status_report` callable the cron tick uses, so
    chat-triggered reports get the same prompt body, freshness contract,
    and skip-if-idle gate as scheduled ones — and so the audit trail in
    events.jsonl shows `cron_start` / `cron_complete` (with
    `trigger="chat"`) the same way cron-triggered runs do.

    Pre-TB-144 the MM handler composed status-shaped replies inline; the
    format drifted from the canonical cron report and the audit shape
    diverged (no cron_start/complete events landed for chat triggers).
    Routing through the shared routine eliminates both gaps.

    Behavior:
      - If the daemon is paused, returns an error rather than running.
        Mirrors cron semantics — paused daemons skip due jobs; chat
        triggers should not bypass that signal.
      - If the skip-gate fires (no activity since the last report),
        returns a `_ok` summary noting the skip — the operator sees
        "no new activity since <ts>" instead of a duplicate report.
      - On a real run, the routine emits the `cron_start` /
        `cron_complete` events; this handler returns a one-line summary
        carrying the run's outcome so the handler agent can mention it
        in its mattermost_reply.
      - The chat path explicitly does NOT advance
        `cron_state[status-report].last_run` (an operator-triggered
        report at 11:00 must not silence the scheduled noon cron).

    Async because the underlying routine is async (it dispatches a
    sub-agent via `await sdk.query(...)`). Tests drive it through
    `asyncio.run(tools.do_status_report_run(cfg, args))`; the MCP tool
    adapter in `build_mcp_server` just awaits it.
    """
    # Lazy import to keep tools.py independent of the status_report ↔
    # daemon import chain at module load.
    from . import status_report as _sr

    reason = (args.get("reason") or "").strip()
    if not reason:
        return _err(
            "reason is required (one short sentence — what triggered "
            "this on-demand report; lands in events.jsonl for audit)"
        )

    if cfg.pause_flag.exists():
        return _err(
            "daemon is paused; on-demand status reports are deferred "
            "until the operator resumes (mirrors cron semantics — "
            "paused daemons skip due jobs)"
        )

    try:
        sdk, mcp_server = _sr._resolved_sdk_refs()
    except RuntimeError as e:
        return _err(str(e))

    result = await _sr.run_status_report(
        cfg, sdk, mcp_server, trigger="chat", reason=reason,
    )
    if result.skipped:
        return _ok(
            "status_report_run skipped (no activity since last report)",
            skipped=True,
            reason=result.reason or "",
            trigger="chat",
        )
    if result.timed_out:
        return _ok(
            "status_report_run timed out (event audit trail intact)",
            skipped=False,
            timed_out=True,
            trigger="chat",
        )
    if result.error:
        return _ok(
            f"status_report_run errored: {result.error}",
            skipped=False,
            error=result.error,
            trigger="chat",
        )
    return _ok(
        "status_report_run dispatched; cron_complete event emitted",
        skipped=False,
        trigger="chat",
    )


def do_daemon_control(cfg: Config, args: dict) -> dict:
    action = args.get("action")
    reason = args.get("reason") or ""
    if action == "pause":
        cfg.pause_flag.parent.mkdir(parents=True, exist_ok=True)
        cfg.pause_flag.write_text(reason + "\n")
        events.append(cfg.events_file, "daemon_pause", reason=reason)
        return _ok("daemon paused")
    if action == "resume":
        if cfg.pause_flag.exists():
            cfg.pause_flag.unlink()
        events.append(cfg.events_file, "daemon_resume", reason=reason)
        return _ok("daemon resumed")
    return _err(f"unknown action {action!r}")


def do_mattermost_thread_read(cfg: Config, args: dict) -> dict:
    """Fetch all posts in a Mattermost thread for the MM handler agent (TB-149).

    Args (string-shaped — matches every other MCP tool in this server):
      thread_id: post id of the thread root (the `thread_id` field on
        the incoming message). Required; an empty value is rejected.
      max_messages: optional integer / numeric string capping the number
        of returned posts (default 50, truncates from the OLDEST end).

    Returns an `_ok` dict whose body has:
      thread_id: echoed back so the agent can correlate calls.
      count: number of posts returned.
      posts: list of `{user, text, create_at, post_id}` dicts in
        chronological (oldest-first) order.

    Returns `_err("mattermost not configured")` when MATTERMOST_URL or
    MATTERMOST_TOKEN are unset — matches `check_new_messages`'s skip
    behavior and gives the handler a distinguishable failure so it can
    fall back to a `mattermost_reply` ("I can't read thread history
    right now") rather than acting on empty context.
    """
    from . import mattermost as _mm

    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _err("thread_id is required")

    raw_max = args.get("max_messages")
    if raw_max in (None, ""):
        max_messages = 50
    else:
        try:
            max_messages = int(raw_max)
        except (TypeError, ValueError):
            return _err(f"max_messages must be an int, got {raw_max!r}")
    if max_messages <= 0:
        max_messages = 50

    if not (os.environ.get("MATTERMOST_URL") and os.environ.get("MATTERMOST_TOKEN")):
        return _err("mattermost not configured")

    try:
        posts = _mm.fetch_thread(cfg, thread_id, max_messages=max_messages)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")

    return _ok(
        f"fetched {len(posts)} thread post(s)",
        thread_id=thread_id,
        count=len(posts),
        posts=posts,
    )


def do_mattermost_reply(cfg: Config, args: dict) -> dict:
    channel = args.get("channel") or ""
    text = args.get("text") or ""
    thread_id = args.get("thread_id") or ""
    if not channel or not text:
        return _err("channel and text are required")
    try:
        post_id = _mm_post(channel, text, thread_id)
        events.append(
            cfg.events_file,
            "mattermost_reply",
            channel=channel,
            thread_id=thread_id,
            post_id=post_id,
            summary=text[:200],
        )
        return _ok(f"posted to {channel}", post_id=post_id)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def _mm_post(channel: str, text: str, thread_id: str = "") -> str:
    url = os.environ.get("MATTERMOST_URL")
    token = os.environ.get("MATTERMOST_TOKEN")
    if not url or not token:
        raise RuntimeError("MATTERMOST_URL and MATTERMOST_TOKEN must be set")
    # Resolve channel name → id if needed (names start without alnum restriction,
    # but IDs are 26-char base32). Best-effort: treat 26-char as id.
    channel_id = channel if len(channel) == 26 and channel.isalnum() else _mm_lookup_channel(url, token, channel)
    body = {"channel_id": channel_id, "message": text}
    if thread_id:
        body["root_id"] = thread_id
    req = urllib.request.Request(
        f"{url}/api/v4/posts",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("id", "")


def _mm_lookup_channel(url: str, token: str, name: str) -> str:
    name = name.lstrip("#")
    # Need a team id; we pick the user's first team as a default.
    team_id = _mm_user_team(url, token)
    req = urllib.request.Request(
        f"{url}/api/v4/teams/{team_id}/channels/name/{name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read())["id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"channel {name!r} not found: {e}") from e


_TEAM_CACHE: str | None = None


def _mm_user_team(url: str, token: str) -> str:
    global _TEAM_CACHE
    if _TEAM_CACHE:
        return _TEAM_CACHE
    req = urllib.request.Request(
        f"{url}/api/v4/users/me/teams",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        teams = json.loads(resp.read())
    if not teams:
        raise RuntimeError("user has no mattermost teams")
    _TEAM_CACHE = teams[0]["id"]
    return _TEAM_CACHE


# ---------------- SDK wiring ----------------


def build_mcp_server(cfg: Config):
    """Build the in-process MCP server exposing the custom tools.

    Imported lazily so unit tests don't need the SDK.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "board_edit",
        "Add, move, or remove tasks on the TASKS.md board.",
        {
            "action": str,
            "task_id": str,
            "title": str,
            "tags": list,
            "briefing": str,
            "description": str,
            "blocked_on": str,
        },
    )
    async def board_edit(args):
        return do_board_edit(cfg, args)

    @tool(
        "cron_edit",
        "Add, remove, or update a scheduled cron job. Operator-CLI use "
        "via `ap2 cron edit`; not exposed to control agents (TB-146). "
        "Use `cron_propose` for agent-side proposals — task agents emit "
        "`cron_proposed` events; operator promotes via review.",
        {
            "action": str,
            "name": str,
            "interval": str,
            "prompt": str,
            "active_when": str,
            "max_turns": int,
        },
    )
    async def cron_edit(args):
        return do_cron_edit(cfg, args)

    @tool(
        "mattermost_reply",
        "Send a message to a Mattermost channel or thread.",
        {"channel": str, "text": str, "thread_id": str},
    )
    async def mattermost_reply(args):
        return do_mattermost_reply(cfg, args)

    @tool(
        "mattermost_thread_read",
        "Fetch all messages in a Mattermost thread (root + replies). Use "
        "when the user's incoming message is a thread reply and you need "
        "context from earlier in the conversation (e.g. the operator "
        "replied 'yes' in a thread where the bot earlier asked 'approve "
        "TB-N?'). `thread_id` is the post id of the thread root — pass "
        "the `thread_id` field from the incoming message verbatim. "
        "`max_messages` defaults to 50 and truncates from the OLDEST end "
        "(most-recent N posts are kept). Returns `posts` as a "
        "chronologically-ordered list of {user, text, create_at, "
        "post_id} dicts. This is a local-only HTTP call to the Mattermost "
        "server — not Anthropic-side tool budget — so it's cheap; still, "
        "one call per turn is enough (no point re-reading the same "
        "thread). Returns an error if MATTERMOST_URL / MATTERMOST_TOKEN "
        "are unset; in that case fall back to a `mattermost_reply` "
        "explaining you can't read thread history right now.",
        {"thread_id": str, "max_messages": int},
    )
    async def mattermost_thread_read(args):
        return do_mattermost_thread_read(cfg, args)

    @tool(
        "log_event",
        "Append an event to the autopilot event log.",
        {"type": str, "summary": str},
    )
    async def log_event(args):
        return do_log_event(cfg, args)

    @tool(
        "daemon_control",
        "Pause or resume the autopilot daemon.",
        {"action": str, "reason": str},
    )
    async def daemon_control(args):
        return do_daemon_control(cfg, args)

    @tool(
        "ideation_state_write",
        "Overwrite .cc-autopilot/ideation_state.md with a fresh per-cycle "
        "progress assessment (TB-87 Step 0). Body is written verbatim — the "
        "ideation prompt is responsible for schema correctness. Returns the "
        "byte count written. Path is fixed; no path arg.",
        {"content": str},
    )
    async def ideation_state_write(args):
        return do_ideation_state_write(cfg, args)

    # Tool name avoids the `task_*` prefix because Claude Code reserves that
    # namespace for its built-in TaskCreate/TaskUpdate/TaskList/TaskGet
    # subagent dispatch tools. Real-SDK smoke runs against `task_complete`
    # showed Claude Code's tool surface filtered the name out — `ToolSearch`
    # returned 0 results for `mcp__autopilot__task_complete` even though the
    # MCP server registered it. Renamed to `report_result` (no `task_`
    # prefix) so the namespace doesn't collide.
    @tool(
        "git_log_grep",
        "Search the project's git log for commits whose message matches "
        "`query` (passed verbatim to `git log --grep=...`). Returns up to "
        "`max_results` (default 20, capped at 100) one-line summaries. "
        "Replaces the ad-hoc `Bash('git log --grep=...')` pattern — "
        "control agents do not have Bash (TB-109).",
        {"query": str, "max_results": int},
    )
    async def git_log_grep(args):
        return do_git_log_grep(cfg, args)

    @tool(
        "operator_log_append",
        "Append a timestamped operator-decision line to "
        ".cc-autopilot/operator_log.md (TB-106). Use ONLY for "
        "operator-mediated messages — e.g. when an operator says "
        "`@claude-bot done: <action>` or `@claude-bot decided: <choice>`. "
        "Args: note (required, one sentence), task_id (optional TB-N). "
        "Ideation reads this log in Step 0 and treats entries as "
        "authoritative; logged decisions are not re-proposed.",
        {"note": str, "task_id": str},
    )
    async def operator_log_append(args):
        return do_operator_log_append(cfg, args)

    @tool(
        "operator_queue_append",
        "Stage an operator board op for the daemon to apply at the next "
        "tick (TB-131). Routes around the rollback / read-stale-board race "
        "that direct `board_edit` exposes during in-flight task or ideation "
        "runs: queued ops aren't in HEAD until between runs, so "
        "`git reset --hard <pre_run_head>` rollback never wipes them and "
        "long-running SDK turns can't read a board snapshot that shifts "
        "underneath them. Use this — instead of `board_edit` — when "
        "@claude-bot is asked to add/move/unfreeze/delete/approve a task "
        "and a task agent is currently active. (TB-142: `board_edit` is "
        "removed from the MM handler's RESTRICTED toolset, so this is "
        "the ONLY board-mutation surface mid-task.) For `add_*` ops, the "
        "TB-N ID is pre-allocated synchronously (so you can mention it "
        "in your reply) and the briefing file is pre-written; only the "
        "TASKS.md insertion is deferred. "
        "TB-154 BRIEFING STRUCTURE — for `add_*` ops AND for `update` "
        "ops that include a `briefing` payload, the `briefing` arg "
        "MUST use exactly these `##`-level section names (case-sensitive, "
        "any order): `## Goal`, `## Scope`, `## Design`, `## Verification`, "
        "`## Out of scope`. The validator rejects any other section names "
        "(e.g. `## Acceptance` instead of `## Verification`, or a "
        "top-level `## Files to touch` block) before allocating a TB-N "
        "(for adds) or before overwriting the slug-stable briefing file "
        "(for updates) — the per-task verifier (TB-69) parses the "
        "briefing's `## Verification` section literally, so the "
        "structural shape is load-bearing. Extra `##`-level sections "
        "(e.g. `## Decision log`, `## Why`) are fine; the "
        "`## Verification` section needs at least one bullet (backticked "
        "shell command, test name, or judge-checkable prose claim). "
        "TB-161 GOAL ANCHOR — the `## Goal` body MUST cite (as a "
        "substring) one of `goal.md`'s `## Current focus` / `## Done "
        "when` heading titles or a Done-when bullet. The validator "
        "rejects briefings whose Goal body cites no anchor, so quote "
        "the focus-item heading verbatim or paste 4-6 words of a "
        "Done-when bullet into the Goal text. Closes the gap-covering-"
        "without-drift failure mode (a structurally-canonical briefing "
        "whose value is only ap2-meta-polish, unconnected to any "
        "operator-stated focus item). Skipped when goal.md is missing "
        "or all-placeholder. "
        "TB-164 WHY-NOW RATIONALE — the `## Goal` body MUST include a "
        "line-anchored `Why now:` paragraph (≥40 chars after the "
        "marker) answering goal.md's delete-test (\"if we delete this "
        "and the goal still ships, was it useful?\"). The validator "
        "rejects briefings whose Goal body has no `Why now` marker OR "
        "a trivial one (e.g. `Why now: yes`). Name the failure mode "
        "this closes or the gap it fills, not just \"this would be "
        "nice to have\". Closes the push-for-progress-without-scope-"
        "creep failure mode (goal.md lines 61-70). "
        "Args: op (one of add_ready, "
        "add_backlog, add_frozen, move_to_backlog, unfreeze, delete, "
        "approve, update); task_id (TB-N for non-add ops); title / tags "
        "(comma-separated string) / description / briefing / blocked_on "
        "(for add ops); force (true/false, for delete from Active/Ready/"
        "Pipeline Pending, OR for update on Active/Pipeline Pending — "
        "but briefing-content edits to a running task are hard-refused "
        "regardless). For `update` ops (TB-153): the same fields apply "
        "(title / tags / description / briefing) but `blocked` (CSV) "
        "replaces `blocked_on`, and explicit `clear_tags` / "
        "`clear_blocked` (true/false) clear those fields — an omitted "
        "flag means unchanged. At least one field must be set.",
        {
            "op": str,
            "task_id": str,
            "title": str,
            "tags": str,
            "description": str,
            "briefing": str,
            "blocked_on": str,
            "blocked": str,
            "clear_tags": str,
            "clear_blocked": str,
            "force": str,
        },
    )
    async def operator_queue_append(args):
        # Normalize string-shaped args to the dict shape do_operator_queue_append
        # expects: tags is a comma-separated string here but a list inside.
        normalized = dict(args)
        # TB-193: `update_goal` is operator-CLI-only. The MM handler /
        # control agents have no path to mutate goal.md — `prompts.py`
        # already documents the design intent ("operator-curated; if
        # you think it needs updating, raise the recommendation in
        # your RESULT summary; do NOT rewrite"). Refuse here at the
        # MCP boundary so the op enum surfaced to the agent doesn't
        # include this verb regardless of what `OPERATOR_QUEUE_OPS`
        # advertises. Same precedent as `cron_edit` / `board_edit`
        # being CLI-only after TB-145 / TB-146.
        if (normalized.get("op") or "").strip() == "update_goal":
            return _err(
                "update_goal is operator-CLI-only "
                "(`ap2 update-goal --file <path>`); refusing the MCP "
                "surface. If you think goal.md needs updating, raise "
                "the recommendation in your RESULT summary."
            )
        raw_tags = normalized.get("tags")
        if isinstance(raw_tags, str):
            if raw_tags.strip():
                normalized["tags"] = [
                    t.strip() for t in raw_tags.split(",") if t.strip()
                ]
            else:
                # TB-153: for `update` ops, distinguish "tags omitted"
                # (don't touch tags) from "tags=''" (clear). Operators
                # who really mean "clear" should use `clear_tags=true`,
                # so an empty string here is treated as "omitted" by
                # dropping the key. For other ops the existing
                # behavior (treat empty as []) is preserved by the
                # add-side handler defaulting via `args.get("tags") or []`.
                normalized.pop("tags", None)
        force = normalized.get("force")
        if isinstance(force, str):
            normalized["force"] = force.strip().lower() in ("1", "true", "yes")
        # TB-153: explicit-clear flags ride as strings on the MCP wire
        # (the schema is all-string for SDK compatibility); coerce to
        # bools so the queue-append handler's truthy checks land cleanly.
        for flag in ("clear_tags", "clear_blocked"):
            v = normalized.get(flag)
            if isinstance(v, str):
                normalized[flag] = v.strip().lower() in ("1", "true", "yes")
        return do_operator_queue_append(cfg, normalized)

    @tool(
        "report_result",
        "Report task completion to the autopilot daemon. Call this ONCE at "
        "the end of your run instead of emitting a `RESULT:` text block. "
        "Args: status='complete'|'incomplete'|'blocked'|'failed' (required); "
        "commit=<7-40 char sha or empty>; summary=<one sentence>; "
        "files_changed=<comma-separated paths>; tests_passed='true'|'false'. "
        "To propose a recurring cron job, call `cron_propose` separately — "
        "it is not bundled into this result (TB-123).",
        # All-string schema — every other MCP tool in this server uses str-
        # only fields. `list` / `bool` types in the schema correlated with
        # Claude Code refusing to surface the tool in earlier smoke runs;
        # strings round-trip cleanly and the daemon-side capture parses
        # `tests_passed` / `files_changed` from their string forms.
        #
        # TB-123: `cron` field dropped — proposals are now their own MCP
        # tool (`cron_propose`) so each proposal gets a structured arg
        # surface, its own event, and failure isolation from result
        # reporting.
        {
            "status": str,
            "commit": str,
            "summary": str,
            "files_changed": str,
            "tests_passed": str,
        },
    )
    async def report_result(args):
        return do_task_complete(cfg, args)

    @tool(
        "cron_propose",
        "Propose a recurring cron job for operator review (TB-123). Use this "
        "when, while working on a task, you notice that some operation should "
        "fire on a schedule (e.g. a weekly perf snapshot, an hourly health "
        "check). The proposal is queued for operator review — it does NOT "
        "mutate cron.yaml directly. `cron_edit` (the direct-mutation tool) "
        "is operator-CLI-only post-TB-146; no agent — cron, ideation, MM "
        "handler, or task — can adopt a proposal automatically. "
        "Each call emits a `cron_proposed` event with the calling task's "
        "TB-id, so you can call it multiple times in one task — each "
        "proposal is independent. Args: name (short stable identifier, "
        "e.g. 'weekly-perf-snapshot'); schedule (interval like '1h' / '1d' "
        "/ '30m'); prompt (the prompt body the cron job will use); "
        "rationale (one short sentence on why this should fire on a "
        "schedule — part of the operator's review).",
        {
            "name": str,
            "schedule": str,
            "prompt": str,
            "rationale": str,
        },
    )
    async def cron_propose(args):
        return do_cron_propose(cfg, args)

    @tool(
        "status_report_run",
        "Trigger an on-demand autopilot status report (TB-144). Use when "
        "the operator explicitly asks for a status report (e.g. "
        "\"@claude-bot status\", \"@claude-bot what's going on\"). The call "
        "dispatches a sub-agent through the same shared routine the "
        "scheduled status-report cron uses, so chat-triggered reports get "
        "the same prompt body, freshness contract, and skip-if-idle gate "
        "as cron-triggered ones; events.jsonl gains a `cron_start` / "
        "`cron_complete` pair with `trigger=\"chat\"` so post-mortems can "
        "distinguish on-demand vs. scheduled runs. Don't call repeatedly "
        "— the routine has its own skip-if-idle gate, so calling more "
        "often than that won't get you a fresher report. Args: reason "
        "(one short sentence; what the operator asked for, lands in the "
        "audit event). The chat trigger does NOT advance "
        "`cron_state[status-report].last_run` — the next scheduled cron "
        "still fires on its normal interval.",
        {"reason": str},
    )
    async def status_report_run(args):
        return await do_status_report_run(cfg, args)

    @tool(
        "pipeline_task_start",
        "Launch a long-running pipeline as a detached OS subprocess. Use this "
        "when your task's work will exceed ~5 minutes of wall-clock time — "
        "Polygon/Polygon-class data fetches, full-history backtests, "
        "parameter sweeps, ML training. The daemon dispatches one task at a "
        "time inside a single `await sdk.query(...)` slot, so a 30-min inline "
        "run holds the only task slot for 30 min and risks tripping "
        "AP2_TASK_TIMEOUT_S (default 1h). After this call returns, finish "
        "your turn with `report_result(status='complete', ...)` summarizing "
        "what you dispatched. The daemon will move the task to "
        "`Pipeline Pending` and re-run your briefing's `## Verification` "
        "against the post-pipeline working tree once every pid you spawned "
        "has died. You can call this multiple times for parallel pipelines; "
        "the daemon waits for all of them.",
        {
            "name": str,
            "command": str,
        },
    )
    async def pipeline_task_start(args):
        return do_pipeline_task_start(cfg, args)

    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        version = _pkg_version("claude-automation")
    except PackageNotFoundError:
        version = "unknown"

    return create_sdk_mcp_server(
        name="autopilot",
        version=version,
        tools=[
            board_edit,
            cron_edit,
            mattermost_reply,
            mattermost_thread_read,
            log_event,
            daemon_control,
            ideation_state_write,
            git_log_grep,
            operator_log_append,
            operator_queue_append,
            report_result,
            cron_propose,
            status_report_run,
            pipeline_task_start,
        ],
    )


# Control agents (cron, ideation, mattermost handler) read project state
# via `Read`/`Glob`/`Grep` and mutate it via narrow MCP tools. They do
# NOT get `Bash` (TB-109) — the only legitimate use was ideation's
# `git log --grep=<TASK_ID>` in Step 1.5, replaced by the `git_log_grep`
# MCP tool. Dropping shell access closes the corruption surface that bit
# stoch's TASKS.md (TB-108): a control agent's `Bash("echo > TASKS.md")`
# bypassed every fence we'd built for task agents.
CONTROL_AGENT_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "mcp__autopilot__board_edit",
    # TB-146: `cron_edit` is NOT exposed to control agents. The only
    # in-workflow programmatic use was ideation auto-adopting
    # `cron_proposed` events from task agents — that bypassed the
    # operator-in-the-loop pattern TB-121 establishes for ideation-
    # proposed *tasks* (which require `ap2 approve` to dispatch). With
    # `cron_edit` hidden from agents, cron schedule mutation is
    # operator-CLI-only (`ap2 cron edit ...`); ideation may still
    # SURFACE unadopted `cron_proposed` events in its per-cycle
    # assessment but cannot adopt them. Task agents continue to use
    # `cron_propose` to emit proposals (no change). Re-add here only
    # alongside an explicit justification + a review gate.
    "mcp__autopilot__mattermost_reply",
    "mcp__autopilot__log_event",
    "mcp__autopilot__daemon_control",
    "mcp__autopilot__ideation_state_write",
    "mcp__autopilot__git_log_grep",
    "mcp__autopilot__operator_log_append",
    # TB-131: queue-based board mutation. The MM handler uses this in
    # place of `board_edit` (which is filtered out of MM_HANDLER_TOOLS
    # below — TB-145).
    "mcp__autopilot__operator_queue_append",
    # TB-144: on-demand status report trigger. Available to control
    # agents in general (not just the MM handler) so a future cron job
    # can also fire one without re-implementing the routine; the MM
    # handler is the immediate consumer (operator-triggered reports).
    "mcp__autopilot__status_report_run",
]

# TB-145: the Mattermost handler ALWAYS runs with this single (narrowed)
# toolset, regardless of whether a task agent is currently in flight. The
# previous TB-122 design picked between FULL and RESTRICTED variants based on
# a snapshot of `Board.iter_tasks("Active")` at handler-spawn time, but that
# check was a TOCTOU race in two ways:
#   1. Stale-at-spawn — the daemon's main tick loop could promote a Backlog
#      task and start its run while the handler was mid-turn (handler picked
#      FULL, then a new task started and the handler's `cron_edit` /
#      `board_edit` calls landed against the running task's snapshot
#      window, tripping TB-110's state-violation check).
#   2. Stale-at-tool-call — even with a race-free snapshot, the toolset
#      decision is anchored at handler-spawn time but the actual tool call
#      may fire 30s later. There's no way to re-evaluate "is a task active"
#      at every tool-call boundary without serializing the MM handler with
#      the main tick.
# Always-RESTRICTED removes both surfaces. Convenience cost: `cron_edit` and
# `ideation_state_write` are no longer reachable from chat — operator uses
# `ap2 cron list/edit` and direct `ideation_state.md` edits via the CLI
# instead. The save-busy-task safety win is constant; the convenience loss
# is rare. Post-TB-141/142/143, queue-routed board ops via
# `operator_queue_append` are the primary mutation path anyway, so the
# handler's day-to-day capability isn't materially reduced.
# What's in MM_HANDLER_TOOLS:
#   - read tools (Read/Glob/Grep/git_log_grep) so the agent can answer
#     questions and reason about state.
#   - `operator_queue_append` so the operator can still queue add / move /
#     unfreeze / delete / approve ops; the daemon drains them at the next
#     tick boundary, so the running task's window never sees the mutation.
#   - `mattermost_reply` / `log_event` so the handler can finish its turn.
#   - `daemon_control` so "@claude-bot pause" works mid-task (pause takes
#     effect on the next tick; the running task completes normally).
#   - `operator_log_append` so "@claude-bot ack: …" still lands in the
#     operator log (ideation reads it in Step 0 — the operator's veto
#     channel must stay open even mid-task).
#   - `status_report_run` (TB-144) so chat-triggered status reports use the
#     same routine as the cron job.
# What's dropped (relative to CONTROL_AGENT_TOOLS):
#   - `ideation_state_write` — would rewrite the per-cycle assessment
#     ideation was acting on. CLI alternative: edit `ideation_state.md`
#     directly while the daemon is idle.
#   - `board_edit` — direct TASKS.md mutation during an in-flight run trips
#     TB-110's state-violation check. Route via `operator_queue_append`
#     instead.
# `cron_edit` is NOT listed here because TB-146 removed it from
# CONTROL_AGENT_TOOLS entirely (no agent — cron, ideation, or MM handler —
# can mutate cron.yaml; it's operator-CLI-only via `ap2 cron edit`). The
# explicit filter is kept as a defense-in-depth no-op so a future
# re-introduction into CONTROL_AGENT_TOOLS doesn't silently leak the tool
# back into the MM handler without re-evaluating the race surface.
MM_HANDLER_TOOLS = [
    t for t in CONTROL_AGENT_TOOLS
    if t not in (
        "mcp__autopilot__cron_edit",  # defensive (already absent post-TB-146)
        "mcp__autopilot__ideation_state_write",
        "mcp__autopilot__board_edit",
    )
] + [
    # TB-149: thread-context read for the MM handler. NOT in
    # CONTROL_AGENT_TOOLS because cron jobs and ideation don't have a
    # thread to read — the handler is the only agent that receives a
    # `thread_id` in its prompt. Kept off TASK_AGENT_TOOLS for the same
    # reason (task agents have no chat surface). Added explicitly here
    # rather than via CONTROL_AGENT_TOOLS so we don't widen the cron /
    # ideation toolset for a tool they can't use.
    "mcp__autopilot__mattermost_thread_read",
]

# `pipeline_task_start` is the first MCP tool task agents can call directly
# (TB-81). The privilege increase is narrow: one tool, atomic, well-scoped to
# launching long-running work that the daemon can't host inside a single
# `await sdk.query(...)` slot. Keep this list otherwise minimal — task agents
# are not control agents and shouldn't gain blanket access to `board_edit`,
# `cron_edit`, etc. via this list.
TASK_AGENT_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "mcp__autopilot__pipeline_task_start",
    "mcp__autopilot__report_result",
    # TB-123: cron-proposal lifted out of report_result's args into a dedicated
    # tool. Task agents call `cron_propose(name, schedule, prompt, rationale)`
    # one or more times to surface "this should fire on a schedule" without
    # bundling it into the result reporting. Symmetric with control agents'
    # `cron_edit` — task agents propose, operator promotes via review.
    "mcp__autopilot__cron_propose",
]


# Files the task agent must NOT edit. Two enforcement layers wrap each
# entry: (1) `prompts._TASK_HEADER` lists each file with a one-line
# explanation so a well-behaved agent skips them, (2) `daemon.run_task`
# adds `Edit(<path>)` + `Write(<path>)` to `disallowed_tools` so the SDK
# rejects direct calls if the agent tries anyway.
#
# Defense-in-depth, not airtight: a determined agent could still write
# via `Bash` (`echo > path`, `sed -i`, `python -c "open(...).write(...)"`).
# Those rely on prompt compliance — globbing every shell shape that
# touches a fenced file is a losing arms race.
#
# Categories:
#   - Daemon-owned state: TASKS.md, progress.md, events.jsonl,
#     ideation_state.md, CLAUDE.md (the daemon bumps Next task ID).
#   - Daemon-owned config: cron.yaml (operator edits via `ap2 cron edit`
#     → `do_cron_edit`; no agent toolset has `cron_edit` post-TB-146).
#   - Operator-curated: goal.md — the project mission. Ideation reads it
#     for grounding; if a task could rewrite it, ideation would
#     effectively rewrite its own constraints. Tasks that *want* to update
#     goal.md should surface the recommendation in their RESULT summary
#     instead, leaving the operator to apply.
TASK_AGENT_FENCED_PATHS = (
    "TASKS.md",
    "CLAUDE.md",
    "goal.md",
    ".cc-autopilot/progress.md",
    ".cc-autopilot/events.jsonl",
    ".cc-autopilot/ideation_state.md",
    ".cc-autopilot/cron.yaml",
    ".cc-autopilot/operator_log.md",
    # TB-143: `operator_queue.jsonl` lives in the defense-layers list
    # (prompt-header reminder + SDK `Edit`/`Write` reject) but is
    # explicitly excluded from TB-110's post-hoc snapshot check via
    # `rollback._VIOLATION_CHECK_EXCLUDED_PATHS`. Same shape as
    # `events.jsonl`: the daemon / operator legitimately append to it
    # during in-flight task runs (every `ap2 add`, `unfreeze`,
    # `delete`, `move_to_backlog`, `approve` issued while a task is
    # active writes a record), so a hash-snapshot diff would
    # false-positive and roll back legitimate work — TB-141 narrowly
    # fixed that by dropping the path from the fence entirely, but
    # that conflated the two distinct purposes the fence list
    # serves. Re-listing here restores defense-in-depth without
    # re-introducing the false-positive.
    ".cc-autopilot/operator_queue.jsonl",
    ".cc-autopilot/operator_queue_state.json",
    # TB-188: per-proposal records (one JSON per ideation-authored
    # proposal, written at `add_backlog` time and reconciled with an
    # `outcome` block on the first terminal event). Daemon-owned audit
    # trail — task agents must NOT edit a record (a malicious or
    # confused agent could otherwise rewrite its own proposal's
    # `focus_anchor` / `why_now` mid-run to cover scope drift). The
    # directory is treated as a unit; the prompt-header rendering walks
    # it so any individual `<TB-N>.json` under it is fenced.
    ".cc-autopilot/ideation_proposals",
)
