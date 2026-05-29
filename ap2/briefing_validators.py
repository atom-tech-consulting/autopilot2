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

TB-316: the LLM-driven dep-coherence check (`_check_dependency_coherence`)
no longer lives at the flat `ap2/validator_judge.py` path; it moved to
`ap2/components/validator_judge/` and registers itself as a
`briefing_validator` hook via the component registry. Core resolves it
through `registry.briefing_validators()` rather than a static import —
the TB-311 import-direction gate forbids the latter. The dependency
is still one-way: the validator_judge component has no awareness of
briefing-section parsing.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import events
from .config import Config
from .init import (
    BRIEFING_REQUIRED_SECTIONS,
    GOAL_ANCHOR_HEADINGS,
    WHY_NOW_MIN_CHARS,
)
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


# TB-316: canonical core-validator list. The five deterministic
# structural checks in the order the pre-TB-316 inline chain ran them.
# Components register additional validators via
# `manifest.hook_points["briefing_validator"]` and the orchestrator
# appends `registry.briefing_validators()` after this list — so the
# core checks always fire first (cheaper, and a malformed briefing has
# a clearer rejection reason than a downstream SDK-judge fail-open
# event would surface).
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
    # TB-316: the canonical pipeline is the five core checks (always
    # run, deterministic structural gates) followed by the registry-
    # walked `briefing_validator` hooks (today: the validator_judge
    # component's dep-coherence wrapper). Sorting is by component-name
    # inside the registry's `briefing_validators()` accessor, so the
    # chain stays deterministic across daemon restarts. Local import
    # of `default_registry` keeps `ap2.briefing_validators` cheap to
    # import for paths that don't trigger validation (e.g. the per-
    # proposal record helpers below).
    from .registry import default_registry

    pipeline: list[BriefingValidator] = list(_CORE_VALIDATORS)
    pipeline.extend(default_registry().briefing_validators())
    for validator in pipeline:
        err = validator(ctx)
        if err:
            return err
    return None
