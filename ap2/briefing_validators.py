"""Briefing-structure + board-input validators (TB-138 / TB-154 / TB-161 /
TB-164 / TB-171 / TB-235 / TB-188 / TB-316).

Hosts the queue-append-time gates that score a freshly-authored briefing
markdown against the structural / goal-alignment / why-now / manual-bullet
/ dep-coherence contract. Operator inputs to board edits (single-line
title / description / tags / blocked CSV; the `update`-op shape gate)
live here too — they're the same surface (refuse-bad-shape-at-write-time)
and shared by both the synchronous `do_board_edit` path and the queued
`do_operator_queue_append` path.

Moved out of `ap2/tools.py` by TB-262 — the briefing-validator surface
(`_validate_briefing_structure` + the goal-anchor matcher + Why-now
check + manual-bullet validator + section regexes + the per-proposal
record helpers + `IMPACT_VERDICTS`) is one coherent concept, and the
status-quo 224KB `tools.py` made every TB touching this surface load
~5000 unrelated LOC.

Public symbols (still re-exported from `ap2.tools` for backward compat):
- Operator-input shape gates: `SINGLE_LINE_ERR`, `TITLE_NO_ASTERISK_ERR`,
  `_validate_single_line`, `_validate_update_args`.
- Briefing-section regex + parsing: `_BRIEFING_SECTION_RE`,
  `_briefing_section_names`, `_BRIEFING_STRUCTURE_HINT`,
  `_briefing_section_body`, `_normalize_anchor`, `_bullet_anchor_phrase`.
- Goal-anchor + Why-now + manual-bullet regexes / helpers:
  `_GOAL_HEADING_RE`, `_WHY_NOW_MARKER_RE`, `_MANUAL_BULLET_RE`,
  `_why_now_paragraph`, `_goal_md_anchors_from_text`, `_goal_md_anchors`.
- Per-proposal record path + IO helpers: `IDEATION_PROPOSALS_DIR`,
  `ideation_proposals_dir`, `proposal_record_path`, `_atomic_write_json`,
  `_blocked_on_has_review`, `write_ideation_proposal_record`,
  `reconcile_proposal_outcome`, `IMPACT_VERDICTS`.
- Briefing-extraction helpers (TB-188): `extract_goal_anchor`,
  `extract_why_now`.
- The main gate: `_validate_briefing_structure` — the single entry
  point called by `do_board_edit` (add / update branches) and
  `do_operator_queue_append` (add / update branches).
- Pipeline-as-list seams (TB-316): `BriefingContext`, `BriefingValidator`,
  `_CORE_VALIDATORS` — the five deterministic structural-check
  callables in canonical order, walked by the orchestrator before the
  registry-walked validators (today: the validator_judge component's
  dep-coherence wrapper) run.

TB-235 / TB-316 / TB-386: the LLM-driven dep-coherence check
(`_check_dependency_coherence`) is a core sub-step of this briefing-
validation runner. TB-316 had briefly modeled it as a
`ap2/components/validator_judge/` component reached via
`registry.briefing_validators()`; TB-386 demoted it back into core,
because a judge invoked only as an internal sub-step of
`_validate_briefing_structure` is NOT a loop-level participant. The judge
still resolves its backend via `select_adapter("validator_judge", cfg)` —
the adapter seam stays; only the redundant component wrapper is gone.
`_validate_briefing_structure` appends the dep-coherence validator after
the five deterministic core checks and calls it directly.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NamedTuple

from . import events
from .config import Config
from .init import (
    BRIEFING_REQUIRED_SECTIONS,
    GOAL_ANCHOR_HEADINGS,
    WHY_NOW_MIN_CHARS,
)
from .json_extract import extract_rightmost_json_object
from .verify import parse_verification_section


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


# TB-216: TASK_LINE_RE's title group `\*\*(?P<title>[^*]+)\*\*` is bounded
# by `[^*]+`, so an embedded asterisk collapses the bold-fence match and
# strands the rendered task in `Board.malformed_lines` — `Board.find(id)`
# returns None and operator-queue verbs (`approve` / `update` / `delete`)
# all KeyError. Hit live on TB-214 (`Pin 4 sandbox install-* CLI verbs`),
# which silently disappeared from `ap2 status` / `pending_review_ids`
# until an operator hand-edit. Loud-reject mirrors TB-134's shape: the
# write-time gate refuses the value with an actionable hint rather than
# letting it ship and forcing a downstream recovery. Field-specific so
# existing description / tag / blocked values with `*` continue to
# round-trip (the parser only chokes on the title group).
TITLE_NO_ASTERISK_ERR = (
    "title must not contain '*' — TASKS.md's bold-fence parser "
    "(board.py TASK_LINE_RE) collapses on embedded asterisks; rename "
    "or describe the wildcard in the briefing prose instead"
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

    TB-216: when `field == "title"`, also reject any value containing
    a literal `*` — TASK_LINE_RE's `\\*\\*(?P<title>[^*]+)\\*\\*` group
    collapses on embedded asterisks and the rendered line lands in
    `Board.malformed_lines` (un-addressable by operator-queue verbs).
    Field-specific so description / tag / blocked values with `*` keep
    round-tripping; only the title group is asterisk-bounded.
    """
    if not value:
        return None
    if "\n" in value or "\r" in value:
        return SINGLE_LINE_ERR.format(field=field)
    if field == "title" and "*" in value:
        return TITLE_NO_ASTERISK_ERR
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
# briefing_validators.py does not import check.py and the briefing
# recommended duplication over a new cross-module coupling). If you edit
# this regex, also update `ap2/check.py:144` so the queue-append gate and
# the operator-facing lint stay in agreement.
_MANUAL_BULLET_RE = re.compile(
    r"^\s*[-*]\s*(?:Manual\s*:|\[manual\])",
    re.IGNORECASE,
)


# TB-308: codespan extraction pattern. Matches either a double-backtick
# fence (`` `path` ``) or a single-backtick fence (`` `path` ``); the
# alternation order matters so the double-fence form binds before the
# single-fence form on inputs like `` `foo` ``. Captures the body
# (without backticks) for substring-matching against
# TASK_AGENT_FENCED_PATHS. Path tokens may have a leading slash
# (`/.cc-autopilot/cron.yaml`); the validator strips it before
# comparing.
_CODESPAN_RE = re.compile(r"``([^`]+)``|`([^`]+)`")


# TB-308: operator-CLI alternatives for each fenced path that has one.
# A briefing whose `## Scope` lists one of these gets a tailored hint
# pointing at the right CLI surface; paths absent from this map fall
# back to "move to `## Out of scope`" (the default suggested-fix).
# Co-located with the helper rather than module-level so the map is
# trivially traceable from the rejection-site comment to the entry.
_FENCED_PATH_FIX_HINTS: dict[str, str] = {
    ".cc-autopilot/cron.yaml": (
        "use `ap2 cron edit <action> <name> [...]` (operator-CLI-only "
        "surface for cron.yaml; no agent toolset carries `cron_edit` "
        "post-TB-146)"
    ),
    "goal.md": (
        "use `ap2 update-goal --file <path>` (the operator-CLI surface "
        "for goal.md edits; ideation reads goal.md for grounding so a "
        "task can't rewrite its own constraints)"
    ),
    "TASKS.md": (
        "use the operator queue (`ap2 add` / `ap2 unfreeze` / "
        "`ap2 approve` / `ap2 delete` / etc.); TASKS.md is rendered "
        "from board state, not hand-edited"
    ),
    "CLAUDE.md": (
        "edit manually as the operator — there's no CLI surface "
        "(project-owned scratch file; the daemon only bumps "
        "`Next task ID`)"
    ),
    ".cc-autopilot/operator_log.md": (
        "the operator owns this via `ap2 ack` and the Mattermost handler "
        "appends via `operator_log_append`; agents cannot author entries"
    ),
}


def _matches_fenced_path(token: str, fenced: str) -> bool:
    """TB-308: does `token` (a backtick-stripped path codespan) reference
    the fenced entry `fenced` (a `TASK_AGENT_FENCED_PATHS` element)?

    Exact match always counts. Directory entries (last path segment has
    no `.` extension — `.cc-autopilot/tasks`,
    `.cc-autopilot/ideation_proposals`) additionally match any path
    inside them (e.g. a Scope bullet listing
    `.cc-autopilot/tasks/foo.md` flags the directory fence). Exact-match
    is the conservative default per the briefing's Design note
    ("Start with exact-codespan match; a fuzzier follow-up can land if
    false-negatives surface").
    """
    if token == fenced:
        return True
    last_seg = fenced.rsplit("/", 1)[-1]
    if "." not in last_seg:
        # `fenced` names a directory — anything under it counts too.
        if token.startswith(fenced.rstrip("/") + "/"):
            return True
    return False


def _validate_no_fenced_paths_in_scope(briefing_text: str) -> str | None:
    """TB-308: reject a briefing whose `## Scope` body backticks a
    `TASK_AGENT_FENCED_PATHS` entry.

    A task agent's SDK call wires `Edit(<path>)` + `Write(<path>)` into
    `--disallowedTools` for every entry in `TASK_AGENT_FENCED_PATHS`
    (`ap2/daemon.py::_task_disallowed_tools`). A Scope bullet that lists
    a fenced path is structurally unsatisfiable: the agent cannot edit
    the file, the unattended verifier marks the bullet as fail, and the
    daemon's retry-then-freeze loop burns dispatches until
    `retry_exhausted`. Hit live on TB-306 (which listed
    `.cc-autopilot/cron.yaml` in Scope and burned ~5 dispatches +
    ~$7 in tokens before the operator manually closed the task). This
    check pre-empts the failure mode at queue-append time, mirroring
    TB-171's manual-bullet rejection shape.

    Scan is scoped to `## Scope` only — mentions of fenced paths in
    `## Design`, `## Verification`, or `## Why now` prose are
    legitimate ("the daemon's cron.yaml ticks every N seconds" /
    "grep cron.yaml content"), the agent reads but doesn't edit those.
    `## Out of scope` is also unscanned by design — that's exactly
    where fenced-path work belongs.

    Returns the first match's error string (so the operator sees one
    concrete fix at a time), or None if Scope is clean. The error
    message names the offending path verbatim, references
    `TASK_AGENT_FENCED_PATHS` (the audit anchor), and includes an
    operator-CLI suggestion from `_FENCED_PATH_FIX_HINTS` when one
    exists; paths without a CLI alternative get the default
    "move to `## Out of scope`" hint.
    """
    # Lazy import — `tools.py` loads this module part-way through its
    # own import block (`tools.py:86`), so a module-scope
    # `from .tools import TASK_AGENT_FENCED_PATHS` would resolve against
    # a partially-loaded tools module and ImportError on first load.
    # The function-scope import binds at call time, by which point
    # tools.py has finished loading. The grep-bullet pin
    # (`grep -q "TASK_AGENT_FENCED_PATHS" ap2/briefing_validators.py`)
    # is satisfied either way; correctness drives the placement choice.
    from .tools import TASK_AGENT_FENCED_PATHS

    # Walk the text line-by-line to delimit `## Scope` rather than
    # use `_briefing_section_body` — the same rationale as the TB-171
    # manual-bullet check: the body extractor's heading regex has a
    # trailing `\s*$` that can greedy-consume the newline + first
    # body character on some inputs. Line-by-line walking is exact.
    _scope_heading = re.compile(r"^##[ \t]+Scope\b", re.IGNORECASE)
    _next_heading = re.compile(r"^##[ \t]+")
    in_scope = False
    scope_lines: list[str] = []
    for line in briefing_text.splitlines():
        if _scope_heading.match(line):
            in_scope = True
            continue
        if in_scope and _next_heading.match(line):
            break
        if in_scope:
            scope_lines.append(line)
    if not scope_lines:
        return None
    scope_body = "\n".join(scope_lines)

    for m in _CODESPAN_RE.finditer(scope_body):
        token_raw = (m.group(1) or m.group(2) or "").strip()
        if not token_raw:
            continue
        # Strip a leading slash so `/.cc-autopilot/cron.yaml` matches
        # `.cc-autopilot/cron.yaml` in the canonical list (operators
        # sometimes write paths as absolute-from-repo-root for clarity).
        normalized = token_raw.lstrip("/")
        for fenced in TASK_AGENT_FENCED_PATHS:
            if _matches_fenced_path(normalized, fenced):
                hint = _FENCED_PATH_FIX_HINTS.get(
                    fenced,
                    "move this work to `## Out of scope` and let the "
                    "operator handle the fenced path manually — no "
                    "CLI alternative exists for this entry",
                )
                return (
                    "briefing structure invalid: `## Scope` references "
                    f"`{token_raw}` which is in TASK_AGENT_FENCED_PATHS "
                    "(the task agent's SDK --disallowedTools includes "
                    f"Edit/Write on this path). {hint}. Move the "
                    "agent-uncoverable work to `## Out of scope` "
                    "(TB-308)."
                )
    return None


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
    proposed_at: str | None = None,
) -> Path | None:
    """Seed a per-proposal record at ideation `add_backlog` time (TB-188).

    Skips silently when `blocked_on` does not carry the `review` token
    (operator-driven adds aren't ideation proposals — see
    `_blocked_on_has_review`). Skips when a record for `tb_id` already
    exists (defensive against retries reissuing the same TB-N — should
    not happen in normal flow, but a re-write would clobber a previously
    reconciled `outcome` block).

    `proposed_at` overrides the default `now()` stamp — used by the
    TB-195 backfill to populate `proposed_at` from the historical
    `applied operator-queued add_backlog → TB-N` line in
    `operator_log.md` instead of "now". Forward writes (the proposal-
    time path inside `do_board_edit`) leave it None and get the
    real-time stamp.

    Returns the record path when written, None when skipped.
    """
    if not _blocked_on_has_review(blocked_on):
        return None
    target = proposal_record_path(cfg, tb_id)
    if target.exists():
        return None
    stamp = proposed_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    payload = {
        "tb_id": tb_id,
        "proposed_at": stamp,
        "focus_anchor": extract_goal_anchor(
            briefing_text, cfg.project_root / "goal.md",
        ),
        "why_now": extract_why_now(briefing_text),
        "briefing_path": briefing_rel,
        "blocked_on": blocked_on,
    }
    _atomic_write_json(target, payload)
    # TB-196: surface record creation in events.jsonl so the ideation
    # cron's events block (TB-169 allowlist) and the web /events page can
    # observe per-proposal record activity. Best-effort — if the events
    # file is fenced or unwritable, swallow the error: the record on disk
    # is the source of truth, the event is observability metadata. The
    # `focus_anchor` field is truncated to 80 chars to keep the rendered
    # events.jsonl line compact (the full anchor lives on disk in the
    # record file).
    try:
        focus_anchor = payload.get("focus_anchor") or ""
        why_now = payload.get("why_now") or ""
        events.append(
            cfg.events_file,
            "ideation_proposal_recorded",
            task_id=tb_id,
            focus_anchor=focus_anchor[:80],
            why_now_chars=len(why_now),
        )
    except OSError:
        pass
    return target


_PROPOSAL_DECISION_KINDS = (
    "approved",
    "rejected",
    "deleted",
    "completed",
    "verification_failed",
)


# TB-189 / TB-251: operator-authored retrospective verdict on a shipped
# proposal. Four fixed values, ordered as a gradient
# (substantive-positive → compliance-neutral → actively-harmful) with
# `unclear` as the explicit "can't tell yet" bucket last. goal.md L61-70
# names the base impact diagnostic ("if we delete this and the goal
# still ships, was it useful?"); TB-251 adds the stronger delete-test
# ("would the codebase be BETTER, not just neutral?") to separate
# `negative` from `pro-forma`. Single source of truth: imported by the
# CLI (cmd_classify validates `--impact <verdict>` against this), the
# operator-queue drain (the `classify` op handler), and the tests.
# Adding values is a one-line tuple edit; expanding via the operator's
# CLI is the briefing's intentional follow-up.
IMPACT_VERDICTS: tuple[str, ...] = (
    "advanced-goal",  # substantively advanced the goal (positive)
    "pro-forma",      # goal-shaped but didn't advance — compliance signal (no harm; just no impact)
    "negative",       # actively regressed something OR made the codebase worse — failed the stronger delete-test (would deletion make things BETTER, not just neutral?)
    "unclear",        # impact not yet legible (uncertain — defer)
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
    # TB-196: surface outcome reconciliation in events.jsonl so the
    # ideation cron's events block (TB-169 allowlist) and the web
    # /events page can observe per-proposal record amends. Best-effort
    # — if the events file is fenced or unwritable, swallow the error:
    # the record on disk is the source of truth (matches the failure-
    # isolation contract used by `write_ideation_proposal_record`'s
    # sibling emit).
    try:
        events.append(
            cfg.events_file,
            "ideation_proposal_reconciled",
            task_id=tb_id,
            decision_kind=decision_kind,
            decision_actor=decision_actor,
            commit=commit,
        )
    except OSError:
        pass
    return target


# ---------------------------------------------------------------------------
# TB-316 axis 4: pipeline-as-list orchestrator.
#
# Pre-TB-316 `_validate_briefing_structure` was a 200-line inline chain
# that called each TB-154 / TB-161 / TB-164 / TB-171 / TB-308 / TB-235
# check in sequence. Post-TB-316 the chain is an iterable list of
# `BriefingValidator` callables: the five deterministic structural
# checks live in core (top-level callables below) and always run; the
# TB-235 LLM dep-coherence check ships as a `validator_judge/`
# component whose manifest registers it as a `briefing_validator` hook
# via the component registry. Components can register additional
# validators via the same hook — extension is the explicit forward-
# compatibility point of the refactor.
#
# Error messages and return-on-first-failure semantics stay byte-
# identical to the pre-TB-316 chain so the existing 100+ briefing-
# validator tests (covering every reject path's exact wording) stay
# green without modification.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefingContext:
    """Shared payload threaded through every `BriefingValidator` callable
    in the TB-316 pipeline-as-list orchestrator.

    Frozen so a downstream validator can't accidentally mutate state
    another validator in the chain depends on — the chain's contract
    is purely read-the-context-and-return-an-error. Each field below
    maps to a kwarg of the pre-TB-316 `_validate_briefing_structure`
    signature; the orchestrator builds one context per validation pass
    and walks the list.

    Fields:
      - `text`               — the briefing markdown source (pre-empty-
                               text short-circuit already applied by
                               the orchestrator, so validators see
                               non-empty content).
      - `goal_md_path`       — `Path | None`; supplied by the operator-
                               queue / board-edit caller (the project
                               root's `goal.md`). `None` short-circuits
                               the TB-161 goal-anchor check.
      - `skip_goal_alignment`— TB-170 operator-CLI escape hatch; when
                               True, the goal-anchor (TB-161) and
                               Why-now (TB-164) checks short-circuit
                               but every other gate runs unchanged.
      - `description`        — task description prose (post-em-dash on
                               the board line) used by the TB-235
                               dep-coherence judge.
      - `blocked_csv`        — the task's `@blocked:` codespan tokens
                               as a comma-separated string; the TB-235
                               judge checks every hard-predecessor it
                               names against this list.
      - `events_file`        — `Path | None`; opt-in flag for TB-235.
                               Set to a real events.jsonl by queue-
                               append / board-edit callers; left None
                               by unit tests that exercise only the
                               deterministic checks.
      - `dep_judge_fn`       — test-injection seam for TB-235; the
                               production path leaves it None and the
                               judge delegates to the SDK helper.
      - `cfg`                 — TB-331 axis-5: optional `Config` plumbed
                               through to the `validator_judge`
                               component so the dep-coherence judge can
                               resolve its four cfg-routed knobs
                               (`disabled` / `timeout_s` / `max_turns`
                               / `max_tokens`) via
                               `cfg.get_component_value(...)`. Optional
                               because legacy unit tests
                               (test_dep_validator_judge.py,
                               test_tb247_*, test_tb316_*) call
                               `_validate_briefing_structure(...)`
                               without a Config; the validator_judge
                               adapter (`_briefing_validator` in the
                               manifest) synthesizes an empty Config
                               for that path so the env-first
                               precedence of `get_component_value`
                               preserves their `monkeypatch.setenv`
                               semantics. Real queue-append /
                               board-edit callers always supply a real
                               cfg.
    """

    text: str
    goal_md_path: "Path | None" = None
    skip_goal_alignment: bool = False
    description: str = ""
    blocked_csv: str = ""
    events_file: "Path | None" = None
    dep_judge_fn: "Callable[..., Any] | None" = None
    cfg: "Config | None" = None


# TB-316: canonical type for the pipeline-as-list validators. Each
# callable takes a `BriefingContext` and returns either a single error-
# message string (rejection) or None (this validator's gate passed,
# continue the chain). The chain is short-circuiting — the orchestrator
# returns on the first error so the operator's diagnostic names one
# concrete fix at a time (matches the pre-TB-316 inline-chain behavior
# byte-for-byte).
BriefingValidator = Callable[[BriefingContext], "str | None"]


def _validate_required_sections(ctx: BriefingContext) -> "str | None":
    """TB-154 (+ TB-138 + TB-135 extensions): required-sections gate.

    Asserts three structural invariants in one validator:
      1. Every section in `BRIEFING_REQUIRED_SECTIONS` is present at
         the `##` level (case-sensitive). Extra `##`-level sections
         (e.g. `## Decision log`) are allowed.
      2. The `## Verification` section is parseable by
         `verify.parse_verification_section` — defends against a
         briefing whose heading the structural-pass regex accepts but
         the AST-driven verifier silently drops (TB-153 historical
         failure mode: `## Acceptance` with a malformed Verification
         shape).
      3. `## Verification` carries at least one bullet — an empty
         section is structurally valid markdown but produces zero
         criteria for the per-task verifier to score against.

    Error messages stay byte-identical to the pre-TB-316 inline check
    so the existing reject-shape tests (TB-154 + TB-138 + TB-135 pins)
    keep passing without modification.
    """
    text = ctx.text
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
    return None


def _validate_goal_anchor(ctx: BriefingContext) -> "str | None":
    """TB-161: goal-anchor check.

    Honors the TB-170 `skip_goal_alignment` operator-CLI escape hatch
    — when True, the gate short-circuits to None and the rest of the
    pipeline runs unchanged. Otherwise runs only when `goal_md_path`
    is parseable and contributes derivable anchors — a fresh project
    whose goal.md is still the all-placeholder template short-circuits
    to "skip" so day-one of a new project doesn't reject every
    proposal.

    Error message stays byte-identical to the pre-TB-316 inline check.
    """
    if ctx.skip_goal_alignment:
        return None
    goal_anchors = _goal_md_anchors(ctx.goal_md_path)
    if not goal_anchors:
        return None
    goal_body = _briefing_section_body(ctx.text, "Goal")
    goal_norm = _normalize_anchor(goal_body)
    if any(a in goal_norm for a in goal_anchors):
        return None
    preview = sorted(goal_anchors)[:5]
    preview_str = ", ".join(f"`{a}`" for a in preview)
    return (
        "briefing structure invalid: `## Goal` body cites no "
        "anchor from goal.md — every proposal must reduce to "
        "a visible step toward the declared project goal "
        "(reject-ap2-meta-polish-drift, TB-161). Reword the "
        "Goal section to quote or reference one of "
        "`goal.md`'s `## Current focus` / `## Done when` "
        "headings or a Done-when bullet. Available anchors "
        f"include: {preview_str}."
    )


def _validate_why_now(ctx: BriefingContext) -> "str | None":
    """TB-164: "Why now" rationale check.

    Honors the TB-170 `skip_goal_alignment` operator-CLI escape hatch
    — when True, the gate short-circuits to None. Otherwise runs even
    when goal.md is missing / all-placeholder; the delete-test is
    intrinsic to the briefing contract, not a goal-relevance check.

    Error messages stay byte-identical to the pre-TB-316 inline check.
    """
    if ctx.skip_goal_alignment:
        return None
    goal_body = _briefing_section_body(ctx.text, "Goal")
    rationale = _why_now_paragraph(goal_body)
    if rationale is None:
        return (
            "## Goal section must include a non-empty 'Why now' "
            "rationale (goal.md's delete-test). Add a line beginning "
            "with `Why now` (e.g. `Why now: <one sentence answering "
            "\"if we delete this and the goal still ships, was it "
            "useful?\">`) inside the `## Goal` body. The marker must "
            "start a line — mid-sentence \"why now\" inside "
            "arbitrary prose doesn't count. Min "
            f"{WHY_NOW_MIN_CHARS} chars after the marker (TB-164)."
        )
    if len(rationale) < WHY_NOW_MIN_CHARS:
        return (
            "## Goal section must include a non-empty 'Why now' "
            "rationale (goal.md's delete-test). The `Why now` "
            f"paragraph is only {len(rationale)} chars after the "
            f"marker; minimum is {WHY_NOW_MIN_CHARS}. Articulate "
            "the delete-test answer in writing — name the failure "
            "mode this closes or the gap it fills, not just \"this "
            "would be nice to have\" (TB-164)."
        )
    return None


def _validate_no_manual_bullets(ctx: BriefingContext) -> "str | None":
    """TB-171: reject `Manual:` / `[manual]` bullets in `## Verification`.

    Runs unconditionally (the TB-170 escape hatch does NOT bypass this
    check — pre-TB-308 the hatch did, but the inconsistency was the
    load-bearing fix). Walks the briefing text line-by-line rather than
    via `_briefing_section_body` because the latter's heading regex is
    `\\s*`-greedy and can swallow the first list bullet of a section
    whose body starts with a bullet — which is the structural shape of
    every well-formed `## Verification` section.

    Error message stays byte-identical to the pre-TB-316 inline check.
    """
    _verif_heading = re.compile(r"^##[ \t]+Verification\b", re.IGNORECASE)
    _next_heading = re.compile(r"^##[ \t]+")
    in_verification = False
    for line in ctx.text.splitlines():
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


def _validate_no_fenced_paths_in_scope_check(
    ctx: BriefingContext,
) -> "str | None":
    """TB-308: reject `## Scope` bullets that codespan a
    `TASK_AGENT_FENCED_PATHS` entry.

    Thin adapter over the pre-TB-316 top-level helper
    `_validate_no_fenced_paths_in_scope(briefing_text)` so the helper's
    error-string shape (and the lazy `TASK_AGENT_FENCED_PATHS` import
    pattern it documents) stays byte-identical post-pipeline-as-list.
    """
    return _validate_no_fenced_paths_in_scope(ctx.text)


# ---------------------------------------------------------------------------
# TB-235 / TB-247 / TB-269 / TB-270 / TB-331 / TB-363 / TB-386:
# LLM-driven dependency-coherence check (validator check #8).
#
# Hosts the Haiku-4.5-driven judge that identifies hard predecessors named
# implicitly in a task briefing's prose (Scope / Design / Why-now /
# description) and the dispatcher (`_check_dependency_coherence`) that turns
# the judge's verdict into a queue-append-time gate. TB-316 had relocated
# this surface into `ap2/components/validator_judge/`; TB-386 demoted it back
# into the core briefing-validation runner — a judge invoked only as an
# internal sub-step of `_validate_briefing_structure` is NOT a loop-level
# component. The judge still resolves its backend via
# `select_adapter("validator_judge", cfg)`; the operator off-switch
# `AP2_VALIDATOR_JUDGE_DISABLED` and the timeout / max-turns knobs survive as
# plain config knobs read via `cfg.get_component_value("validator_judge", …)`.
# ---------------------------------------------------------------------------


# TB-235: knob defaults for the LLM-driven dependency-coherence check.
# TB-269: timeout bumped 15.0 → 60.0 (the TB-257 investigation artifact
# measured `_judge_dep_coherence_default` at 17.6-46.8s wall-clock; the prior
# 15s default + 5s grace sat below the median completion of even the smallest
# measured briefing). 60s sits 1.5× the artifact's worst-case ~47s.
_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT = 60.0
# TB-249: the SDK budget primitive is `max_turns`. `max_turns=2` allows ONE
# assistant message (the JSON verdict) + ONE optional tool call.
_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT = 2
# TB-249 deprecated-alias ceiling: a stale `AP2_VALIDATOR_JUDGE_MAX_TOKENS`
# value is accepted as a `max_turns` override but capped so the old default
# (`500`) doesn't translate into a 500-turn runaway.
_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL = 5
# TB-235: legacy default kept ONLY for backward-compatibility lookups.
_VALIDATOR_JUDGE_MAX_TOKENS_DEFAULT = 500
# TB-249: process-once flag so the deprecated-knob warning event fires
# exactly once per process, not once per `ap2 add` invocation.
_VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED: set[str] = set()
# Haiku-4.5 is the cost-target floor for the check (≤$0.005 per briefing at
# typical token volumes). Intentionally NOT exposed as an env knob yet.
_VALIDATOR_JUDGE_MODEL = "claude-haiku-4-5"


class _DepJudgeTimeout(Exception):
    """Sentinel raised by a `dep_judge_fn` (or the default SDK call) when the
    judge exceeded `AP2_VALIDATOR_JUDGE_TIMEOUT_S`. `_check_dependency_coherence`
    distinguishes this from generic failures so the emitted event type is
    `validator_judge_timeout` vs `validator_judge_fail`.
    """


# TB-247: parse-failure categorization labels surfaced as `parse_error` on
# `validator_judge_fail` events. Mirrors TB-236's `PARSE_ERROR_CATEGORIES`
# in `ap2/verify.py`:
#   - `empty_text`    — SDK returned no last-assistant-text at all.
#   - `no_braces`     — text exists but has no `{` / `}` to anchor extraction.
#   - `json_decode`   — `{...}` candidate parse-failed (JSONDecodeError).
#   - `non_dict`      — parsed cleanly but the value isn't a dict.
#   - `sdk_exception` — the SDK call itself raised before any text came back.
_DEP_JUDGE_PARSE_ERRORS: tuple[str, ...] = (
    "empty_text",
    "no_braces",
    "json_decode",
    "non_dict",
    "sdk_exception",
)


class _DepJudgeOutcome(NamedTuple):
    """TB-247: result + diagnostics from the dependency-coherence judge.

    Mirrors `ap2/verify.py::_ParseOutcome`. Carries the parsed judge data
    plus optional diagnostic fields so `_check_dependency_coherence` can
    enrich `validator_judge_fail` events with the dump-file path and a
    parse-error category.

    Fields:
      - `data` — parsed `{"hard_predecessors": [...], "reasoning": ...}` dict
        on a clean parse; `None` on every parse-failure branch.
      - `parse_error` — one of `_DEP_JUDGE_PARSE_ERRORS` on every parse-
        failure path, `None` on success.
      - `dump_path` — per-call debug file at
        `<events_file.parent>/debug/<UTC-ts>-validator-judge-response.txt`
        when a parse failure landed AND the diagnostic write succeeded.

    The dispatcher also accepts a legacy `dict | None` return value from
    existing test stubs that pre-date TB-247 — those are wrapped as
    `_DepJudgeOutcome(data=..., parse_error=None, dump_path=None)`.
    """

    data: dict | None
    parse_error: str | None
    dump_path: "Path | None"


def _parse_dep_judge_response(
    text: str,
    *,
    events_file: "Path | None",
) -> _DepJudgeOutcome:
    """TB-247: parse the dep-judge SDK response into a `_DepJudgeOutcome`.

    On parse failure it writes the FULL raw `text` to
    `<events_file.parent>/debug/<UTC-ts>-validator-judge-response.txt` so the
    operator can diagnose WHY the judge returned the shape it did. Successful
    parses leave NOTHING on disk. Best-effort write: any OSError on `debug/`
    mkdir or file write is swallowed and `dump_path` stays None.

    `events_file=None` suppresses the dump entirely; the outcome still
    carries `data` + `parse_error` so the caller's fail-open path works.
    """
    parse_error: str | None = None
    data: dict | None = None

    if not text:
        parse_error = "empty_text"
    else:
        # TB-247: two-pass parse. Try whole-text JSON first; on parse
        # failure, fall back to substring extraction so preamble/trailing-
        # prose responses still get extracted cleanly.
        #
        # TB-261: the substring extraction is centralized in
        # ``ap2.json_extract.extract_rightmost_json_object`` — the
        # rightmost-balanced-object semantics close the preamble-brace-
        # shadowing bug the pre-TB-261 first-`{` / last-`}` boundary-finding
        # had at all four call sites.
        stripped = text.strip()
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole_parsed = False
        else:
            whole_parsed = True
            if isinstance(whole, dict):
                data = whole
            else:
                parse_error = "non_dict"

        if not whole_parsed:
            extracted = extract_rightmost_json_object(text)
            if extracted is None:
                # No parseable JSON OBJECT anywhere. Distinguish "no braces"
                # from "braces present but every candidate `{` fails decode".
                if "{" not in text:
                    parse_error = "no_braces"
                else:
                    parse_error = "json_decode"
            else:
                parsed, _, _ = extracted
                if not isinstance(parsed, dict):
                    parse_error = "non_dict"
                else:
                    data = parsed

    dump_path: "Path | None" = None
    if parse_error is not None and events_file is not None:
        try:
            debug_dir = events_file.parent / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ",
            )
            candidate = debug_dir / f"{ts}-validator-judge-response.txt"
            candidate.write_text(text or "")
            dump_path = candidate
        except OSError:
            dump_path = None

    return _DepJudgeOutcome(
        data=data, parse_error=parse_error, dump_path=dump_path,
    )


# TB-270: canonical briefing headings that bound the dep-coherence judge's
# input. Hard-predecessor detection is answered from the briefing's
# narrative-intent sections (Goal: why; Scope: what), NOT from Verification /
# Out-of-scope / Design. Slicing to these two sections is a faithful
# narrowing of input and shrinks the SDK call's input token count by
# ~50-70% on typical operator-curated briefings.
_BRIEFING_SLICE_HEADINGS: tuple[str, ...] = ("## Goal", "## Scope")


def _slice_briefing_for_dep_judge(briefing_text: str) -> str:
    """TB-270: return the substring covering `## Goal` and `## Scope` sections
    only, terminating each section at the next `## ` heading or EOF.

    Defensive fallback: if either canonical heading is missing, or if the
    resulting slice is empty after whitespace-stripping, return the full
    `briefing_text` unchanged — slicing must not turn a parseable briefing
    into a zero-token payload.

    Both sections are concatenated in SOURCE order (Goal before Scope).
    """
    sections: list[tuple[int, str, str]] = []  # (start_offset, slice, body)
    for heading in _BRIEFING_SLICE_HEADINGS:
        # `\b` after the heading ensures `## Scope` doesn't accidentally match
        # `## ScopeAndExtras`.
        pattern = rf"^{re.escape(heading)}\b"
        m = re.search(pattern, briefing_text, flags=re.MULTILINE)
        if m is None:
            # Missing heading → defensive fallback (don't blind the judge).
            return briefing_text
        section_start = m.start()
        rest = briefing_text[m.end():]
        next_heading = re.search(r"^## ", rest, flags=re.MULTILINE)
        section_end = (
            m.end() + next_heading.start()
            if next_heading is not None
            else len(briefing_text)
        )
        section_slice = briefing_text[section_start:section_end]
        body = briefing_text[m.end():section_end]
        sections.append((section_start, section_slice, body))

    # Both headings present but each section's body is empty (e.g. a stub
    # briefing with headings and no prose between them). Fall back to the
    # full text — same defensive posture as the missing-heading branch.
    if all(not body.strip() for _, _, body in sections):
        return briefing_text

    # Preserve SOURCE order — Goal-then-Scope on canonical briefings.
    sections.sort(key=lambda triple: triple[0])
    return "".join(section_slice for _, section_slice, _ in sections)


def _resolve_validator_judge_adapter(*, sdk=None, cfg: "Config | None" = None):
    """Resolve the `AgentAdapter` backing the `validator_judge` kind (TB-363).

    The backend is chosen per agent kind by `select_adapter` reading the
    merged `[agent_backends]` config for the `validator_judge` kind
    (`AP2_AGENT_BACKEND_VALIDATOR_JUDGE` env override > `[agent_backends]`
    table > the all-`claude` default). With the default map the resolved
    adapter is a `ClaudeCodeAdapter` and the judge's verdict is identical to
    the pre-migration direct `sdk.query` path; an operator can set
    `validator_judge=codex` to route just this judge to the Codex backend.

    The resolved adapter wraps the injected `sdk` handle so the judge's
    hermetic unit tests stay deterministic. `cfg=None` is the seam
    `_judge_dep_coherence_default` hits — it carries no `Config`, so it falls
    back to a default `ClaudeCodeAdapter`, matching the all-`claude` default.
    """
    from .adapters.claude_code import ClaudeCodeAdapter

    if cfg is not None:
        from .adapters.select import select_adapter

        adapter = select_adapter("validator_judge", cfg)
    else:
        adapter = ClaudeCodeAdapter()
    if sdk is not None and isinstance(adapter, ClaudeCodeAdapter):
        adapter._sdk = sdk
    return adapter


def _judge_dep_coherence_default(
    *,
    briefing_text: str,
    description: str,
    blocked_tokens: list[str],
    timeout_s: float,
    max_turns: int,
    events_file: "Path | None" = None,
) -> _DepJudgeOutcome:
    """Real-SDK implementation of the TB-235 dependency-coherence judge.

    Returns a `_DepJudgeOutcome` carrying the parsed JSON dict (when the judge
    produced one) plus optional diagnostic fields. Returns
    `_DepJudgeOutcome(data=None, ...)` on any non-timeout failure (network,
    parse error, non-dict response). Raises `_DepJudgeTimeout` when the SDK
    call exceeds `timeout_s`.

    TB-249: budget control is `max_turns` (the SDK-native primitive). The
    judge gets a strict-JSON system prompt + a user payload naming the
    briefing (Goal+Scope slice), the post-em-dash description prose, and the
    task's current `@blocked:` codespan tokens; the response shape is
    `{"hard_predecessors": ["TB-N", ...], "reasoning": "<str>"}`.
    """
    import asyncio

    # TB-366: source the (possibly test-injected) SDK module through the
    # adapter layer (`ap2.adapters.load_claude_sdk`) so `claude_agent_sdk` is
    # imported only inside `ap2/adapters/`. The injected fake-SDK seam is
    # preserved: `load_claude_sdk` resolves the import against `sys.modules`.
    from .adapters import load_claude_sdk

    try:
        sdk = load_claude_sdk()
    except Exception:
        return _DepJudgeOutcome(data=None, parse_error=None, dump_path=None)

    # TB-363: resolve the AgentAdapter for the `validator_judge` kind and
    # dispatch through it. `_judge_dep_coherence_default` carries no `cfg`, so
    # this hits the `cfg=None` seam → a default `ClaudeCodeAdapter` wrapping
    # the `sdk` handle imported above.
    adapter = _resolve_validator_judge_adapter(sdk=sdk, cfg=None)

    # TB-247: tightened final-message contract (mirrors TB-236's prose-judge
    # prompt). Shorter `reasoning` = smaller surface area for JSON-escape bugs.
    system_text = (
        "You are validating a task briefing for hard-predecessor "
        "dependency coherence. A hard predecessor is another task "
        "whose work must be on disk (committed) before this task's "
        "agent can do its own work — code modules, schema, env knobs, "
        "or other artifacts the new task depends on. Soft references "
        "(historical context, sibling tasks doing parallel work, "
        "references to docstrings or prior commits for "
        "reading-comprehension only) are NOT hard predecessors.\n\n"
        "OUTPUT CONTRACT — your FINAL message must be a JSON object "
        "only:\n"
        '  {"hard_predecessors": ["TB-217"], '
        '"reasoning": "TB-217 created ap2/_shared.py which this '
        'briefing imports"}\n'
        "Rules for the FINAL message:\n"
        "  - It is a JSON object only. No markdown code fences (no "
        "```json or ``` wrapping). No leading prose (no 'Here is the "
        "verdict:' preamble). No trailing commentary after the closing "
        "brace.\n"
        "  - `hard_predecessors` is a (possibly empty) list of strings,"
        " each of the form 'TB-N'.\n"
        "  - `reasoning` is a single short paragraph, MAXIMUM 200 "
        "characters. Cite the briefing file:section or symbol "
        "triggering the dep claim; do NOT quote long briefing excerpts "
        "or paste prose blocks.\n"
        "  - If the reasoning would naturally exceed 200 characters, "
        "summarize: name the strongest single piece of evidence and "
        "stop.\n"
    )
    # TB-270: slice the briefing to Goal+Scope sections only.
    user_payload = {
        "briefing_markdown": _slice_briefing_for_dep_judge(briefing_text),
        "task_description": description,
        "blocked_codespan_tokens": list(blocked_tokens),
    }
    prompt = (
        f"{system_text}\n\n"
        f"Input:\n```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    async def _ask() -> str:
        # TB-363: build a backend-neutral `AgentOptions` / `AgentTools` and
        # dispatch through the resolved `adapter`. The outer
        # `asyncio.wait_for(..., timeout=timeout_s)` owns the timeout; a
        # backend fault surfaces as a non-`complete` `AgentResult.status`,
        # re-raised here so the worker maps it onto the SDK-exception
        # fail-open branch.
        from .adapters.base import AgentOptions, AgentTools

        options = AgentOptions(
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            model=_VALIDATOR_JUDGE_MODEL,
        )
        result = await adapter.run_to_result(prompt, AgentTools(), options)
        if result.status != "complete":
            raise RuntimeError(
                result.error or f"validator judge adapter {result.status}"
            )
        return (result.text or "").strip()

    # If we're already inside a running event loop (the daemon's tick is async
    # and calls sync MCP-tool handlers), `asyncio.run` raises. Run the
    # coroutine in a fresh thread with its own loop so the sync caller
    # composes correctly in both contexts.
    import threading

    result: dict[str, "str | Exception | None"] = {"text": None, "exc": None}

    def _worker() -> None:
        try:
            result["text"] = asyncio.run(
                asyncio.wait_for(_ask(), timeout=timeout_s),
            )
        except asyncio.TimeoutError as exc:
            result["exc"] = _DepJudgeTimeout(str(exc))
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    # TB-269: stopwatch around the worker join so the `validator_judge_passed`
    # event below carries an honest wall-clock duration.
    _t0 = time.monotonic()
    worker.start()
    worker.join(timeout=timeout_s + 5)
    if worker.is_alive():
        # Worker overran the inner timeout — treat as timeout. The thread
        # leaks (daemon=True so it dies at interpreter shutdown).
        raise _DepJudgeTimeout(
            f"validator judge worker exceeded {timeout_s + 5:.0f}s"
        )
    if isinstance(result["exc"], _DepJudgeTimeout):
        raise result["exc"]
    if result["exc"] is not None:
        # Non-timeout SDK exception. No raw text to dump.
        return _DepJudgeOutcome(
            data=None, parse_error=None, dump_path=None,
        )
    text = result["text"] or ""
    duration_s = time.monotonic() - _t0

    # TB-269: emit `validator_judge_passed` for every successful worker return
    # (SDK call completed without timeout / SDK exception). Fires BEFORE the
    # JSON parse so the `validator_judge_timeout_audit` doctor surface sees
    # every real-world wall-clock duration the judge paid. Best-effort write.
    if events_file is not None:
        try:
            events.append(
                events_file,
                "validator_judge_passed",
                duration_s=round(duration_s, 3),
                briefing_bytes=len(briefing_text.encode("utf-8")),
                max_turns=max_turns,
                timeout_s=timeout_s,
            )
        except OSError:
            pass

    # TB-247: delegate parse + dump to the testable helper.
    return _parse_dep_judge_response(text, events_file=events_file)


def _validator_judge_disabled(cfg: Config) -> bool:
    """TB-331 / TB-386: True iff the validator-judge kill switch is set.

    Routes through `cfg.get_component_value("validator_judge", "disabled")`,
    which evaluates sectioned env > flat env (`AP2_VALIDATOR_JUDGE_DISABLED`
    via the `FLAT_TO_SECTIONED` reverse-lookup) > `cfg.components_config`
    snapshot > default at call time. Same truthy enumeration as the registry's
    pre-TB-386 polarity rule (`1` / `true` / `yes`, case-insensitive). Default
    unset → False (judge enabled). The component is gone (TB-386) but the
    `validator_judge` config namespace survives as a plain knob namespace.
    """
    raw = cfg.get_component_value("validator_judge", "disabled")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes"}


def _validator_judge_timeout_s(cfg: Config) -> float:
    """TB-331: effective per-call timeout (seconds) for the dep-coherence SDK
    invocation. Routes through
    `cfg.get_component_value("validator_judge", "timeout_s")`. Permissive
    parse: empty / non-float / whitespace-only values fall back to
    `_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT` (60.0).
    """
    raw = cfg.get_component_value("validator_judge", "timeout_s")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT


def _validator_judge_max_turns(cfg: Config) -> int | None:
    """TB-331: effective `max_turns` budget from the canonical knob, or `None`
    when unset/invalid so the caller can fall through to the deprecated-alias
    resolution. Routes through
    `cfg.get_component_value("validator_judge", "max_turns")`.

    Returns `None` (NOT the default) when the resolved value is None / empty /
    non-int / `<= 0`, so the caller distinguishes the unset-canonical-knob
    case from a positive override.
    """
    raw = cfg.get_component_value("validator_judge", "max_turns")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _validator_judge_max_tokens_legacy(cfg: Config) -> int:
    """TB-331: deprecated-alias resolution for `AP2_VALIDATOR_JUDGE_MAX_TOKENS`
    (the pre-TB-249 knob name). Routes through
    `cfg.get_component_value("validator_judge", "max_tokens")`. Returns `0`
    when the layer is empty / non-int / non-positive — the sentinel the caller
    treats as "alias not set" before falling through to
    `_VALIDATOR_JUDGE_MAX_TURNS_DEFAULT`.
    """
    raw = cfg.get_component_value("validator_judge", "max_tokens")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 0
    try:
        legacy_val = int(raw)
    except (TypeError, ValueError):
        return 0
    return legacy_val if legacy_val > 0 else 0


def _check_dependency_coherence(
    cfg: Config,
    *,
    briefing_text: str,
    description: str,
    blocked_csv: str,
    events_file: "Path | None",
    judge_fn=None,
) -> str | None:
    """TB-235 dep-coherence check. See `_validate_briefing_structure`'s
    docstring for the contract.

    Returns an error-string when the LLM judge identifies any TB-N as a hard
    predecessor that is not present in the task's `@blocked:` codespan
    (`blocked_csv`). Returns `None` when the briefing is consistent, the judge
    returns an empty `hard_predecessors` list, the off-switch
    `AP2_VALIDATOR_JUDGE_DISABLED=1` is set (resolved via
    `_validator_judge_disabled(cfg)`), or the judge SDK call fails for any
    reason (fail-open; emits a `validator_judge_{timeout,fail}` event when
    `events_file` is supplied).

    `judge_fn`: callable matching `_judge_dep_coherence_default`'s signature.
    Test paths inject a stub; the production path uses the real SDK. TB-247
    added an optional `events_file` kwarg used by the production path; stubs
    that don't accept it fall through a `TypeError` retry.
    """
    if _validator_judge_disabled(cfg):
        return None
    timeout_s = _validator_judge_timeout_s(cfg)
    # TB-249 / TB-331: resolve `max_turns` with a layered preference:
    #   (1) AP2_VALIDATOR_JUDGE_MAX_TURNS — canonical knob, default 2.
    #   (2) AP2_VALIDATOR_JUDGE_MAX_TOKENS — deprecated alias; if set AND (1)
    #       is unset, used as `max_turns` capped at the ceiling. A
    #       `validator_judge_deprecated_knob` event fires once per process.
    #   (3) module default — _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT.
    canonical_turns = _validator_judge_max_turns(cfg)
    max_turns = _VALIDATOR_JUDGE_MAX_TURNS_DEFAULT
    if canonical_turns is not None:
        max_turns = canonical_turns
    else:
        legacy_val = _validator_judge_max_tokens_legacy(cfg)
        if legacy_val > 0:
            capped = min(legacy_val, _VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL)
            max_turns = capped
            if (
                events_file is not None
                and "AP2_VALIDATOR_JUDGE_MAX_TOKENS"
                not in _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED
            ):
                try:
                    events.append(
                        events_file,
                        "validator_judge_deprecated_knob",
                        knob="AP2_VALIDATOR_JUDGE_MAX_TOKENS",
                        replacement="AP2_VALIDATOR_JUDGE_MAX_TURNS",
                        legacy_value=legacy_val,
                        applied_max_turns=capped,
                        ceiling=_VALIDATOR_JUDGE_DEPRECATED_KNOB_CEIL,
                    )
                except OSError:
                    pass
                _VALIDATOR_JUDGE_DEPRECATED_KNOB_LOGGED.add(
                    "AP2_VALIDATOR_JUDGE_MAX_TOKENS"
                )
    blocked_tokens = [
        tok.strip()
        for tok in (blocked_csv or "").split(",")
        if tok.strip()
    ]

    fn = judge_fn or _judge_dep_coherence_default
    try:
        raw_ret = fn(
            briefing_text=briefing_text,
            description=description or "",
            blocked_tokens=blocked_tokens,
            timeout_s=timeout_s,
            max_turns=max_turns,
            events_file=events_file,
        )
    except _DepJudgeTimeout as exc:
        if events_file is not None:
            try:
                events.append(
                    events_file,
                    "validator_judge_timeout",
                    timeout_s=timeout_s,
                    error=str(exc),
                )
            except OSError:
                pass
        return None
    except TypeError:
        # TB-247: pre-TB-247 test stubs don't accept the new `events_file`
        # kwarg. Retry without it so legacy stubs stay green.
        try:
            raw_ret = fn(
                briefing_text=briefing_text,
                description=description or "",
                blocked_tokens=blocked_tokens,
                timeout_s=timeout_s,
                max_turns=max_turns,
            )
        except _DepJudgeTimeout as exc:
            if events_file is not None:
                try:
                    events.append(
                        events_file,
                        "validator_judge_timeout",
                        timeout_s=timeout_s,
                        error=str(exc),
                    )
                except OSError:
                    pass
            return None
        except Exception as exc:  # noqa: BLE001
            if events_file is not None:
                try:
                    events.append(
                        events_file,
                        "validator_judge_fail",
                        error=f"{type(exc).__name__}: {exc}",
                        parse_error="sdk_exception",
                    )
                except OSError:
                    pass
            return None
    except Exception as exc:  # noqa: BLE001
        if events_file is not None:
            try:
                events.append(
                    events_file,
                    "validator_judge_fail",
                    error=f"{type(exc).__name__}: {exc}",
                    parse_error="sdk_exception",
                )
            except OSError:
                pass
        return None

    # TB-247: normalize the judge return value into a `_DepJudgeOutcome`.
    if isinstance(raw_ret, _DepJudgeOutcome):
        outcome = raw_ret
    else:
        outcome = _DepJudgeOutcome(
            data=raw_ret if isinstance(raw_ret, dict) else None,
            parse_error=None,
            dump_path=None,
        )

    if not isinstance(outcome.data, dict):
        # Malformed JSON / non-object response. Treat as fail-open so a single
        # judge hiccup can't block every `ap2 add`. Emit `validator_judge_fail`.
        if events_file is not None:
            try:
                payload: dict[str, Any] = {
                    "error": "non-dict judge response",
                }
                if outcome.parse_error is not None:
                    payload["parse_error"] = outcome.parse_error
                if outcome.dump_path is not None:
                    payload["debug_path"] = str(outcome.dump_path)
                events.append(
                    events_file,
                    "validator_judge_fail",
                    **payload,
                )
            except OSError:
                pass
        return None

    data = outcome.data
    hard_preds = data.get("hard_predecessors")
    reasoning = str(data.get("reasoning") or "").strip()
    if not isinstance(hard_preds, list) or not hard_preds:
        # Empty list (or missing field) → no dependency claim → no @blocked
        # requirement. Common path.
        return None
    declared_lower = {t.lower() for t in blocked_tokens}
    for raw in hard_preds:
        if not isinstance(raw, str):
            continue
        tok = raw.strip()
        if not tok or not tok.upper().startswith("TB-"):
            continue
        if tok.lower() in declared_lower:
            continue
        # First missing dependency wins — same shape as the deterministic
        # checks (return on first offender so the operator's error message is
        # specific rather than a multi-line aggregate).
        return (
            f"briefing structure invalid: judge identified {tok} as a "
            f"hard predecessor (reasoning: \"{reasoning}\"). Either "
            f"add @blocked:{tok} to the task's codespan, or rephrase "
            f"the briefing to not claim {tok} as a hard predecessor "
            "(TB-235)."
        )
    return None


def _empty_cfg_for_back_compat() -> Config:
    """TB-331: synthetic empty `Config` for legacy test paths that exercise
    the dep-coherence judge without constructing a real one.

    `test_dep_validator_judge.py` and the TB-247 / TB-316 sibling modules
    call `_validate_briefing_structure(...)` with `events_file` + `dep_judge_fn`
    but no `cfg`. The component body's `cfg.get_component_value(...)` calls
    require a Config-shaped object, so this helper synthesizes one via
    `Config.__new__(Config)` and sets only the `components_config` attribute
    the resolver consults (the empty dict means the snapshot branch is a
    no-op; the env-first precedence inside `get_component_value` still walks
    sectioned-env + flat-env before falling through to the default).

    NOT a new production path — every queue-append / board-edit caller
    supplies a real `cfg` via `BriefingContext.cfg`.
    """
    cfg = Config.__new__(Config)
    cfg.components_config = {}
    return cfg


def _briefing_validator(ctx: "BriefingContext") -> str | None:
    """Adapter from `BriefingContext` to `_check_dependency_coherence`.

    Matches the canonical `BriefingValidator = Callable[[BriefingContext],
    str | None]` shape so `_validate_briefing_structure` can append it to the
    pipeline and dispatch it uniformly with the deterministic core validators.

    Preserves the opt-in contract: the dep-coherence check only fires when the
    caller supplied either an `events_file` (real queue-append / board-edit
    surface) or a `dep_judge_fn` (test injection seam). Unit tests that
    exercise only the deterministic checks omit both kwargs and this adapter
    short-circuits with `None`.

    TB-331: threads `ctx.cfg` into `_check_dependency_coherence` so the four
    cfg-routed knob reads resolve against the same `Config` the surrounding
    board-edit / operator-queue surface already has. Legacy test paths that
    don't populate `ctx.cfg` get a synthetic empty Config via
    `_empty_cfg_for_back_compat()`.
    """
    if ctx.events_file is None and ctx.dep_judge_fn is None:
        return None
    cfg = ctx.cfg if ctx.cfg is not None else _empty_cfg_for_back_compat()
    return _check_dependency_coherence(
        cfg,
        briefing_text=ctx.text,
        description=ctx.description or "",
        blocked_csv=ctx.blocked_csv or "",
        events_file=ctx.events_file,
        judge_fn=ctx.dep_judge_fn,
    )


# TB-316 / TB-386: canonical core-validator list. The five deterministic
# structural checks in the order the pre-TB-316 inline chain ran them.
# `_validate_briefing_structure` appends the dep-coherence judge
# (`_briefing_validator`) after this list — so the core checks always fire
# first (cheaper, and a malformed briefing has a clearer rejection reason
# than a downstream SDK-judge fail-open event would surface).
_CORE_VALIDATORS: tuple[BriefingValidator, ...] = (
    _validate_required_sections,
    _validate_goal_anchor,
    _validate_why_now,
    _validate_no_manual_bullets,
    _validate_no_fenced_paths_in_scope_check,
)


def _validate_briefing_structure(
    briefing_text: str,
    *,
    goal_md_path: "Path | None" = None,
    skip_goal_alignment: bool = False,
    description: str = "",
    blocked_csv: str = "",
    events_file: "Path | None" = None,
    dep_judge_fn=None,
    cfg: "Config | None" = None,
) -> str | None:
    """TB-154 + TB-161 + TB-164 + TB-171 + TB-308 + TB-235: structural
    gate for a freshly-authored briefing.

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
      7. (TB-308) The `## Scope` body must NOT codespan any path in
         `TASK_AGENT_FENCED_PATHS`. A Scope bullet that lists a fenced
         path is structurally unsatisfiable: the task agent's SDK call
         wires `Edit(<path>)` + `Write(<path>)` into `--disallowedTools`
         for every fenced entry (see `daemon._task_disallowed_tools`),
         so the agent cannot do the work and the unattended verifier
         marks the bullet as fail — the daemon's retry-then-freeze loop
         then burns dispatches until `retry_exhausted`. TB-306 hit this
         live: a Scope bullet listing `.cc-autopilot/cron.yaml` burned
         ~5 dispatches + ~$7 in tokens before the operator manually
         closed. The error message names the offending path verbatim,
         references `TASK_AGENT_FENCED_PATHS` (the audit anchor), and
         suggests the operator-CLI alternative when one exists
         (`ap2 cron edit` for `.cc-autopilot/cron.yaml`,
         `ap2 update-goal` for `goal.md`, the operator queue for
         `TASKS.md`, etc.). Only `## Scope` is scanned — mentions in
         `## Design` / `## Verification` / `## Out of scope` are
         legitimate (the agent reads but doesn't edit those). Runs
         regardless of `skip_goal_alignment` — there's no legitimate
         operator scenario where the task agent SHOULD edit a fenced
         path.
      8. (TB-235) When `blocked_csv` is supplied (the caller is a real
         queue-append / board-edit surface, not a unit test that only
         exercises the deterministic checks), a Haiku-4.5 LLM judge is
         asked to identify any hard predecessors named implicitly in
         the briefing prose (Scope / Design / Why-now / description).
         A hard predecessor is another TB-N whose work must be on disk
         (committed) before this task's agent can do its own work —
         not a soft historical / sibling reference. Any judge-named
         TB-N missing from the task's `@blocked:` codespan rejects the
         briefing with a message naming the missing dependency and the
         judge's reasoning verbatim. Closes the dependency-coherence
         hole that lets briefings like TB-220 ("ap2/_shared.py must
         already exist — created by the _locked extraction") ship
         without `@blocked:TB-217`, which under
         `AP2_AUTO_APPROVE=1` would auto-promote out of dispatch
         order. Fail-open: timeout / parse failure / SDK error logs a
         `validator_judge_{timeout,fail}` event and lets the briefing
         through — refusing to gate on a transient API hiccup. Hard
         off-switch: `AP2_VALIDATOR_JUDGE_DISABLED=1`. Timeout and
         budget tunable via `AP2_VALIDATOR_JUDGE_TIMEOUT_S` (default
         15) and `AP2_VALIDATOR_JUDGE_MAX_TURNS` (default 2; TB-249
         replaced the pre-existing `AP2_VALIDATOR_JUDGE_MAX_TOKENS`
         knob, which the Claude Agent SDK rejected as an unknown
         arg — see `_judge_dep_coherence_default` for the migration
         note and the deprecated-alias contract). Test paths inject
         a stub `dep_judge_fn` to drive the decision logic
         deterministically without a real SDK call.

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

    TB-316: refactored from an inline call chain into a pipeline-as-list
    orchestrator. The five deterministic structural checks (`_CORE_VALIDATORS`)
    plus any registry-walked `briefing_validator` hooks (today: the
    `validator_judge` component's TB-235 dep-coherence wrapper) compose
    the chain; this function builds a `BriefingContext` once and walks
    the list, returning on the first error. Error messages and chain
    order are byte-identical to the pre-TB-316 inline chain so the
    existing 100+ briefing-validator reject-shape tests stay green.
    """
    text = briefing_text or ""
    if not text.strip():
        # Defer to TB-135's "briefing is required" gate — that one names
        # the right error for an empty payload. Short-circuiting here
        # mirrors the pre-TB-316 inline-chain entry guard byte-for-byte.
        return None
    ctx = BriefingContext(
        text=briefing_text,
        goal_md_path=goal_md_path,
        skip_goal_alignment=skip_goal_alignment,
        description=description,
        blocked_csv=blocked_csv,
        events_file=events_file,
        dep_judge_fn=dep_judge_fn,
        # TB-331 axis-5: threads the caller's `Config` through to the
        # validator_judge component's `_briefing_validator` adapter so
        # the four cfg-routed knob reads (`disabled` / `timeout_s` /
        # `max_turns` / `max_tokens`) resolve against the same Config
        # the surrounding board-edit / operator-queue surface already
        # has. Defaults to None for legacy test paths that don't carry
        # a Config (the manifest adapter synthesizes an empty one for
        # back-compat).
        cfg=cfg,
    )
    # TB-316 / TB-386: the canonical pipeline is the five deterministic core
    # checks (always run) followed by the dep-coherence LLM judge
    # (`_briefing_validator`). TB-316 had resolved that final validator
    # through a registry walk of a `validator_judge` component; TB-386 demoted
    # the judge back into core — a judge invoked only as an internal sub-step
    # of this runner is not a loop-level participant — so it is appended and
    # called directly here. The adapter short-circuits when neither
    # `events_file` nor `dep_judge_fn` is supplied (unit-test paths exercising
    # only the deterministic checks); the `AP2_VALIDATOR_JUDGE_DISABLED`
    # off-switch is honored inside `_check_dependency_coherence`, and the
    # judge resolves its backend via `select_adapter("validator_judge", cfg)`.
    pipeline: list[BriefingValidator] = list(_CORE_VALIDATORS)
    pipeline.append(_briefing_validator)
    for validator in pipeline:
        err = validator(ctx)
        if err:
            return err
    return None
