"""Ideation: a first-class autopilot mechanism, not a cron job.

Ideation fires when the working queue (Ready+Backlog) has fewer than
`AP2_IDEATION_TRIGGER_TASK_COUNT` items AND Active is empty, throttled
by a per-project cooldown. Its prompt instructs the agent to propose new
tasks based on goal.md, TASKS.md, progress.md, the insights index, and
recent failures (see `ideation.default.md`).

Why a dedicated module rather than a cron job: ideation is the only
mechanism that creates new work, so it needs to evolve faster than the
generic cron infrastructure — its prompt structure (assessment, failure
review, insights grounding, two-tier verification) is load-bearing and
changes often. Splitting it out also lets projects override just the
prompt without touching cron.yaml.

Configuration:
- Default prompt: `ap2/ideation.default.md` shipped with the package.
- Project override (optional): `.cc-autopilot/ideation_prompt.md` in the
  project root — when present, it replaces the default verbatim.
- Cooldown: `AP2_IDEATION_COOLDOWN_S` (default 7200 — 2h).
- Trigger threshold: `AP2_IDEATION_TRIGGER_TASK_COUNT` (default 3).
  Ideation fires when the count of Ready+Backlog tasks is BELOW this
  threshold (and Active is empty as a hard SDK-contention gate). The
  threshold doubles as the per-cycle proposal-slot budget — TB-183
  pre-computes `slots = max(0, threshold - workable)` and passes it
  via the prompt's `## Current state` snapshot block (single source
  of truth, no hardcoded magic number drifting from the env knob).
  Set to 1 for the legacy "fire only when the working queue is fully
  empty" behavior.
- Max turns: `AP2_IDEATION_MAX_TURNS` (default 100 — sourced from
  `config.DEFAULT_IDEATION_MAX_TURNS`; TB-278 bumped from the prior 30
  after a goal.md rewrite mid-cycle hit `error_max_turns` at 31 turns.
  Already-validated against this project's own override of 100; the
  raised default just spares fresh projects from rediscovering that wall.
  `AP2_CONTROL_TIMEOUT_S` still bounds runaway wall-clock).
- Disable: `AP2_IDEATION_DISABLED=1` opts out of empty-board ideation
  entirely (used by the test suite by default; useful for projects that
  want to drive ideation manually rather than on the natural gate).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import events
from .board import Board
from .config import Config, DEFAULT_IDEATION_MAX_TURNS
from .cron import load_state, mark_run


IDEATION_NAME = "ideation"
# TB-278: re-pointed at the new `config.DEFAULT_IDEATION_MAX_TURNS` named
# constant so the timeouts / max-turns defaults live in one place. The
# alias survives for backwards compat (tests + code that import
# `IDEATION_MAX_TURNS_DEFAULT` directly keep working). Value bumped from
# the prior 30 to 100 as part of the TB-278 battle-tested-defaults pass.
IDEATION_MAX_TURNS_DEFAULT = DEFAULT_IDEATION_MAX_TURNS
IDEATION_COOLDOWN_DEFAULT_S = 7200  # 2h between fires when board stays empty
# Trigger threshold: ideation fires when Ready+Backlog count is BELOW this
# value (and Active is empty). TB-183: also serves as the per-cycle
# proposal-slot budget — `slots = max(0, threshold - workable)` flows into
# the prompt's `## Current state` snapshot block so the agent reads it
# from a single source of truth instead of a hardcoded magic-3 in the
# prompt body. Tunable via AP2_IDEATION_TRIGGER_TASK_COUNT.
IDEATION_TRIGGER_TASK_COUNT_DEFAULT = 3

# TB-169: allowlist of event `type` values ideation actually keys off.
# `_run_ideation` passes this to `build_control_prompt` so the rendered
# `## Recent events` tail filters out observability/plumbing noise (full
# `judge_call` payloads with token-usage dumps, `status_report`,
# `cron_*`, `mattermost_*`, `task_run_usage` / `control_run_usage`,
# daemon lifecycle, etc.). The 6KB `format_for_prompt` byte budget then
# carries lifecycle + operator-decision + cron-proposal signal instead
# of being half-eaten by 2KB-each `judge_call` lines.
#
# Allowlist (not denylist) is intentional: new event types added in
# future TBs default to *exclusion* unless someone consciously opts them
# in. New event types are typically observability/plumbing
# (`task_run_usage` / `control_run_usage` are exactly that pattern),
# which is what we want excluded by default.
#
# See `ideation.default.md` for how each retained kind feeds the
# agent's reasoning:
# - `task_complete` / `verification_failed` / `verification_partial` /
#   `retry_exhausted` / `task_state_violation` — Step 1 follow-up
#   discovery + Step 1.5 failure review.
# - `ideation_approved` / `task_deleted` / `task_updated` — operator
#   decisions (cross-cycle "what was approved/rejected/edited" context).
# - `cron_proposed` — explicit ideation.default.md surfacing rule.
IDEATION_RELEVANT_EVENT_TYPES: tuple[str, ...] = (
    # Task lifecycle — Step 1 follow-up discovery + Step 1.5 failure review.
    "task_complete",
    "verification_failed",
    "verification_partial",
    "retry_exhausted",
    "task_state_violation",
    # Operator decisions — cross-cycle context.
    "ideation_approved",
    "task_deleted",
    "task_updated",
    # Cron proposals — explicit ideation.default.md rule.
    "cron_proposed",
    # Self-skip telemetry (TB-174) — when the natural ideation cron
    # short-circuited because every focus item is
    # `exhausted-needs-operator`. Visible to the next cycle so the
    # ideator sees the prior gate trip in its events block (avoids
    # re-discovering a stale "we already self-reported exhausted"
    # signal across cycles).
    "ideation_skipped",
    # Per-proposal record activity (TB-196) — emitted by
    # `write_ideation_proposal_record` on seed-write and
    # `reconcile_proposal_outcome` on outcome-block append. Surfaces
    # TB-188's record substrate in the ideation prompt's events block
    # so the next cycle can observe its own proposals' record
    # creation + terminal-event reconciliation. Listed alphabetically.
    "ideation_proposal_recorded",
    "ideation_proposal_reconciled",
    # TB-223: opt-in auto-approval audit trail. `auto_approved` fires
    # at proposal-emission time (in `tools.do_board_edit`'s
    # `add_backlog` branch) when `AP2_AUTO_APPROVE=1` strips the
    # `@blocked:review` codespan from a proposed row. Including the
    # event in the ideation events block lets the next cycle observe
    # what auto-approval shipped without operator review (otherwise
    # the auto-approved tasks would be silently invisible to ideation
    # — anti-pattern for the audit-trail expectation set by goal.md's
    # end-to-end-automation focus). `auto_approve_paused` fires when
    # the daemon's cumulative-regression circuit-breaker trips and
    # halts auto-promote until the operator emits `ap2 ack
    # auto_approve_unfreeze`.
    "auto_approved",
    "auto_approve_paused",
    # TB-232: dry-run sibling of `auto_approved`. Surfacing it in the
    # ideation events block lets the next cycle observe which proposals
    # WOULD have shipped without operator review while the dry-run
    # knob is on — same audit-trail need as `auto_approved` itself.
    "would_auto_approve",
    # TB-233: dry-run sibling of `auto_unfreeze_applied` (axis-2
    # on-ramp). Surfacing it in the ideation events block lets the
    # next cycle observe which Frozen tasks WOULD have been
    # auto-unfrozen while `AP2_AUTO_UNFREEZE_DRY_RUN=1` is on —
    # parallel to the `auto_unfreeze_applied` surfacing on the
    # real-application path. Without it, dry-run decisions would be
    # invisible to ideation and the next cycle would miss the
    # signal that the allowlist is actually getting exercised.
    "would_auto_unfreeze",
    # TB-282: proactive attention-raised push surface. Surfacing
    # fresh `attention_raised` events in the ideation events block
    # lets the next cycle observe the conditions that warrant
    # immediate operator attention (today: `task_stuck`; future:
    # validator-judge noisy / cost-cap approach / decisions-needed-
    # new / frozen-task recency) — the ideator can then reason about
    # whether the proposal queue should pivot to address the
    # surfaced condition (e.g. a recurring `task_stuck` for the same
    # task identity might justify a follow-up task to investigate
    # the dispatch hang). Without surfacing here, the only ideation-
    # cycle visibility into attention conditions would be a fresh
    # re-run of the detector — which has no access to the prior
    # tick's debounce state.
    "attention_raised",
)

_DEFAULT_PROMPT_PATH = Path(__file__).parent / "ideation.default.md"
_PROJECT_PROMPT_REL = ".cc-autopilot/ideation_prompt.md"


# TB-173 / TB-191: parser for the `## Decisions needed from operator`
# section of `.cc-autopilot/ideation_state.md` (renamed from the
# pre-TB-191 `## Open questions for operator`). The ideation prompt's
# Step 0 schema mandates this section whenever the agent has an
# actionable decision the operator must engage with — naming the
# specific operator action and the unblock-condition for the next
# cycle. Today the surface is silent — the section sits unread in
# `ideation_state.md` until the operator manually opens the file.
#
# `parse_operator_decisions` is the single source of truth that
# `ap2 status` (CLI), the web home page, and the cron status-report
# all call so the three operator-facing surfaces stay in sync.
#
# TB-191 added the sibling `## Cycle observations` section as
# agent-internal working notes that MUST NOT leak to operator-facing
# surfaces. The header-match regex below is line-anchored on
# `## Decisions needed from operator` precisely, so the parser
# structurally cannot scrape `## Cycle observations` content even
# when the two sections sit adjacent. The defensive
# `test_parse_operator_decisions_ignores_cycle_observations` test
# pins this against future schema reorderings.
#
# Section-slicing reuses the same shape as
# `ap2/check.py::_check_briefings_manual_bullets` — header regex
# matches the section heading line-anchored, slice runs from
# heading-end to the next `## ` (or EOF). Per-bullet extraction:
# `- ` / `* ` lines start a new entry; subsequent indented lines (two
# spaces or more, or a tab) join the previous bullet with a single
# space; blank lines finalize the current bullet. Cap at
# `_OPERATOR_DECISIONS_MAX_ENTRIES` (default 7) entries to bound
# rendering cost; on truncation, append a synthetic "(+M more)"
# trailer entry so the UI surfaces both the visible cap and the
# residual count.
#
# Failure mode: ideator may write the section as prose paragraphs
# instead of bullets. Defense: when the bullet pass yields nothing, fall
# back to splitting the section body on blank lines and treating each
# paragraph as one entry. The same 7-cap applies to the fallback.
_OPERATOR_DECISIONS_HEADER_RE = re.compile(
    r"^##\s+Decisions needed from operator\s*$", re.M,
)
_OPERATOR_DECISIONS_NEXT_SECTION_RE = re.compile(r"^##\s+", re.M)
_OPERATOR_DECISIONS_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OPERATOR_DECISIONS_MAX_ENTRIES = 7


def parse_operator_decisions(path: Path) -> list[str]:
    """Return the bullets under `## Decisions needed from operator` in `path`.

    `path` is the absolute path to a project's
    `.cc-autopilot/ideation_state.md`. Returns ``[]`` when the file is
    missing, when the section header is absent, or when the section is
    empty.

    TB-191: this parser MUST ignore the sibling `## Cycle observations`
    section (agent-internal working notes that must not leak to
    operator-facing surfaces). The line-anchored header regex matches
    only `## Decisions needed from operator` precisely, and the
    next-section regex (`^##\\s+`) terminates the slice at the very
    next `## ` heading, so a `## Cycle observations` section sitting
    adjacent — before OR after the decisions section — is structurally
    excluded from the returned list.

    Bullet handling: each `- ` / `* ` line opens a new entry; indented
    continuation lines under a bullet are joined into the previous entry
    with a single space (newlines collapsed). Blank lines finalize the
    current entry.

    Fallback: if the section has no bullet lines at all (ideator wrote
    prose paragraphs), split the body on blank lines and treat each
    paragraph as one entry — same single-space whitespace collapse.

    Cap: at most `_OPERATOR_DECISIONS_MAX_ENTRIES` (7) real entries; on
    overflow, the returned list is truncated to 7 and a synthetic
    `"(+M more)"` trailer entry is appended (so the returned list can
    be at most 8 elements long). Callers that render the list in a
    space-constrained surface (CLI status text) may apply their own
    further truncation; the JSON / web surfaces consume the whole list
    untouched.

    Pure / no I/O beyond the single read of `path`. Defensive against
    OSErrors (returns ``[]`` rather than raising) so a transient
    permission glitch on the ideation_state file never breaks
    ``ap2 status``.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    m = _OPERATOR_DECISIONS_HEADER_RE.search(text)
    if m is None:
        return []
    body_start = m.end()
    next_m = _OPERATOR_DECISIONS_NEXT_SECTION_RE.search(text, body_start)
    body = text[body_start: next_m.start() if next_m else len(text)]

    entries: list[str] = []
    current: list[str] | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if not stripped:
            # Blank line — finalize the current bullet if any.
            if current is not None:
                joined = " ".join(current).strip()
                if joined:
                    entries.append(joined)
                current = None
            continue
        bullet_m = _OPERATOR_DECISIONS_BULLET_RE.match(raw)
        if bullet_m is not None:
            if current is not None:
                joined = " ".join(current).strip()
                if joined:
                    entries.append(joined)
            current = [bullet_m.group(1).strip()]
        elif current is not None and (raw.startswith(" ") or raw.startswith("\t")):
            # Continuation line under the current bullet — join with a
            # single space so multi-line bullets render as one entry.
            current.append(stripped)
        # Else: a non-bullet, non-indented line outside any bullet
        # context. Ignored for the bullet pass; the prose-fallback below
        # handles the all-paragraphs shape.
    if current is not None:
        joined = " ".join(current).strip()
        if joined:
            entries.append(joined)

    if not entries:
        # Prose fallback: treat each blank-line-separated paragraph as
        # one entry. Whitespace inside a paragraph collapses to single
        # spaces (matches the bullet-continuation behavior above).
        paragraphs = re.split(r"\n\s*\n", body.strip())
        entries = [
            " ".join(p.split()).strip()
            for p in paragraphs if p.strip()
        ]

    if len(entries) > _OPERATOR_DECISIONS_MAX_ENTRIES:
        truncated = entries[:_OPERATOR_DECISIONS_MAX_ENTRIES]
        truncated.append(
            f"(+{len(entries) - _OPERATOR_DECISIONS_MAX_ENTRIES} more)"
        )
        return truncated
    return entries


# TB-174: parser for the `## Current focus assessment` section's per-item
# `Status:` field. Each top-level focus-item bullet has the shape
# documented in `ap2/ideation.default.md` (lines 60-66):
#
#     - **<focus item verbatim from goal.md>**
#       - Progress so far: ...
#       - Gaps: ...
#       - Status: `in-progress` | `exhausted-needs-operator` | `deferred`
#       - Reasoning: <one sentence>
#
# `parse_focus_statuses` returns `{focus title: status}` so the daemon
# can self-skip the natural ideation cron when every focus item is
# `exhausted-needs-operator` (gate in `_maybe_ideate`). The forced
# operator path (`force_ideate`, TB-159) bypasses the gate.
#
# Section-slicing reuses the same `^##\s+` next-section regex as
# `parse_operator_decisions` (the next H2 heading or EOF terminates the
# section). Top-level focus-item bullets are detected by
# `_FOCUS_TOP_BULLET_RE` (line starts with `- **` at column 0); the
# title may wrap onto an indented continuation line until the closing
# `**`. The `Status:` sub-bullet is found by scanning forward inside
# the focus item's body.
_FOCUS_HEADER_RE = re.compile(r"^##\s+Current focus assessment\s*$", re.M)
_FOCUS_TOP_BULLET_RE = re.compile(r"^-\s+\*\*")
_FOCUS_TITLE_SPAN_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_FOCUS_STATUS_RE = re.compile(r"^\s*-\s+Status:\s*(.+?)\s*$")
_FOCUS_VALID_STATUSES: frozenset[str] = frozenset(
    {"in-progress", "exhausted-needs-operator", "deferred"}
)
_FOCUS_STATUS_UNKNOWN = "unknown"


def parse_focus_statuses(path: Path) -> dict[str, str]:
    """Return `{focus title: status}` from `## Current focus assessment` in `path`.

    `path` is the absolute path to a project's
    `.cc-autopilot/ideation_state.md`. Returns ``{}`` when the file is
    missing, when the section header is absent, or when no focus-item
    bullets parse out of the section body.

    Each top-level `- **<title>**` bullet inside the section becomes one
    entry in the returned dict, mapping the title (whitespace-collapsed)
    to its `Status:` sub-bullet's value (lowercased, surrounding
    backticks stripped). Statuses outside the canonical set
    {`in-progress`, `exhausted-needs-operator`, `deferred`} — including
    a focus item with no `Status:` sub-bullet at all — are reported as
    `unknown`. The gate in `_maybe_ideate` only short-circuits on
    `exhausted-needs-operator`, so `unknown` keeps the natural ideation
    path running (the safer default — never skip on a parse glitch).

    Title handling: the title may wrap across one indented continuation
    line before its closing `**` (the load-bearing example from
    production: `**Ideation quality (gap-covering without drift; push\n
    for progress without scope creep)**` spans two lines). Whitespace
    inside the title collapses to single spaces.

    Pure / no I/O beyond the single read of `path`. Defensive against
    OSErrors (returns ``{}`` rather than raising) so a transient
    permission glitch on the ideation_state file never crashes
    `_maybe_ideate`.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text()
    except OSError:
        return {}
    m = _FOCUS_HEADER_RE.search(text)
    if m is None:
        return {}
    body_start = m.end()
    next_m = _OPERATOR_DECISIONS_NEXT_SECTION_RE.search(text, body_start)
    body = text[body_start: next_m.start() if next_m else len(text)]

    lines = body.splitlines()
    starts = [
        i for i, ln in enumerate(lines)
        if _FOCUS_TOP_BULLET_RE.match(ln)
    ]
    if not starts:
        return {}
    bounds = starts + [len(lines)]

    result: dict[str, str] = {}
    for k in range(len(starts)):
        start, end = bounds[k], bounds[k + 1]
        title, status = _parse_one_focus_item(lines[start:end])
        if title:
            result[title] = status
    return result


def _parse_one_focus_item(item_lines: list[str]) -> tuple[str, str]:
    """Extract `(title, status)` from a single focus-item slice.

    `item_lines[0]` is the `- **<title-start>` line; later lines are
    the title's continuation (if `**` didn't close on line 0) plus the
    nested sub-bullets (`- Progress so far:`, `- Gaps:`, `- Status:`,
    `- Reasoning:`). Title is the text inside the FIRST `**...**`
    span (whitespace collapsed). Status is the value of the FIRST
    `- Status:` sub-bullet, lowercased and with surrounding backticks
    stripped; falls back to `_FOCUS_STATUS_UNKNOWN` when the value
    isn't in the canonical set or the sub-bullet is missing entirely.
    """
    if not item_lines:
        return "", _FOCUS_STATUS_UNKNOWN
    head = item_lines[0].lstrip()
    if head.startswith("- "):
        head = head[2:]
    # Join the head with continuation lines (whitespace-stripped) so a
    # `**...**` span that wraps across one or more lines re-assembles
    # cleanly. The non-greedy match in `_FOCUS_TITLE_SPAN_RE` then
    # picks the first `**...**` span and discards the sub-bullets that
    # follow.
    joined = " ".join([head] + [ln.strip() for ln in item_lines[1:]])
    m = _FOCUS_TITLE_SPAN_RE.search(joined)
    title = " ".join(m.group(1).split()) if m else ""

    status = _FOCUS_STATUS_UNKNOWN
    for line in item_lines:
        ms = _FOCUS_STATUS_RE.match(line)
        if ms is None:
            continue
        raw = ms.group(1).strip().strip("`").strip().lower()
        status = raw if raw in _FOCUS_VALID_STATUSES else _FOCUS_STATUS_UNKNOWN
        break
    return title, status


def load_prompt(cfg: Config) -> str:
    """Return the ideation prompt — project override if present, else default."""
    override = cfg.project_root / _PROJECT_PROMPT_REL
    if override.is_file():
        return override.read_text()
    return _DEFAULT_PROMPT_PATH.read_text()


def _cooldown_s() -> int:
    """Effective cooldown (seconds), env-overridable."""
    v = os.environ.get("AP2_IDEATION_COOLDOWN_S")
    if v:
        try:
            return int(v)
        except ValueError:
            pass
    return IDEATION_COOLDOWN_DEFAULT_S


def _trigger_task_count() -> int:
    """Effective Ready+Backlog trigger threshold, env-overridable.

    Reads `AP2_IDEATION_TRIGGER_TASK_COUNT`. Same permissive parsing style
    as `_cooldown_s`: invalid (non-int, non-positive, empty) values fall
    back to the module default silently. A value <= 0 would make the gate
    impossible to clear (every count >= 0 satisfies `count >= 0`), so we
    treat that as invalid too.
    """
    v = os.environ.get("AP2_IDEATION_TRIGGER_TASK_COUNT")
    if v:
        try:
            parsed = int(v)
        except ValueError:
            return IDEATION_TRIGGER_TASK_COUNT_DEFAULT
        if parsed > 0:
            return parsed
    return IDEATION_TRIGGER_TASK_COUNT_DEFAULT


# TB-223: opt-in `AP2_AUTO_APPROVE` mode lets the daemon dispatch
# ideation-proposed tasks without the operator running `ap2 approve`
# first — closes the most-frequently-triggered operator-in-the-loop
# bottleneck under the **Current focus: end-to-end automation** goal.
# A representative session approves 10-20 tasks per cycle; that's not
# the walk-away promise the Mission says, that's approving constantly.
#
# Three layered safety knobs (defaults preserve current behavior):
#   - `AP2_AUTO_APPROVE` (master switch) — unset by default. When set
#     to a truthy value, ideation-authored `add_backlog` rows omit the
#     `@blocked:review` codespan so the daemon's next-tick auto-promote
#     dispatches the task immediately.
#   - `AP2_AUTO_APPROVE_GATE_TAGS` (per-shape opt-out) — comma-separated
#     list of tag strings (default: `#breaking-change,#high-risk`). A
#     proposed task carrying ANY of these tags retains `@blocked:review`
#     even in auto-approve mode — operator's escape hatch for elevated-risk
#     categories ideation itself self-tags.
#   - `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` (systemic-regression circuit
#     breaker) — integer count, default 3. Consumed by the daemon
#     (`daemon.py`), not here; included in this module's helper layer
#     so the three-knob safety model lives in one source file.
#
# Truthy-value parse matches the canonical `AP2_IDEATION_DISABLED`
# pattern in `_maybe_ideate` (L641): `.strip() in ("1", "true", "yes")`.
# Tag-list parse: comma-separated, whitespace-stripped, normalized to
# leading `#` (operator may type `#high-risk` or bare `high-risk`).
AUTO_APPROVE_DEFAULT_GATE_TAGS: tuple[str, ...] = (
    "#breaking-change",
    "#high-risk",
)


def _is_auto_approve_enabled() -> bool:
    """True iff `AP2_AUTO_APPROVE` is set to a truthy value.

    Truthy values: `"1"`, `"true"`, `"yes"` (case-sensitive, leading/trailing
    whitespace stripped). Default unset → False (current behavior; every
    ideation-proposed task carries `@blocked:review` and waits for
    `ap2 approve`).

    Matches the parsing shape of `AP2_IDEATION_DISABLED` in
    `_maybe_ideate` (L641) so operators tuning the autopilot env file see
    one consistent boolean convention across knobs.
    """
    return os.environ.get("AP2_AUTO_APPROVE", "").strip() in ("1", "true", "yes")


def _auto_approve_gate_tags() -> frozenset[str]:
    """Parsed `AP2_AUTO_APPROVE_GATE_TAGS` as a frozenset of normalized
    tag strings.

    Comma-separated, whitespace-stripped, normalized to a leading `#`
    (so operators may type `"#high-risk,#breaking-change"` or bare
    `"high-risk,breaking-change"` and both parse identically). Default
    (env unset or empty) is `AUTO_APPROVE_DEFAULT_GATE_TAGS` —
    `#breaking-change` + `#high-risk`, which are the categories
    ideation itself uses for proposals it judges as elevated risk
    (so the defaults align with ideation's existing self-tagging).
    Empty entries are dropped; deliberate `AP2_AUTO_APPROVE_GATE_TAGS=""`
    falls back to the default set (the explicit way to opt out of every
    gate-tag is `AP2_AUTO_APPROVE_GATE_TAGS="#__never__"` or similar
    sentinel that no real task carries).
    """
    raw = os.environ.get("AP2_AUTO_APPROVE_GATE_TAGS", "").strip()
    if not raw:
        return frozenset(AUTO_APPROVE_DEFAULT_GATE_TAGS)
    out: set[str] = set()
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        out.add(t)
    return frozenset(out) if out else frozenset(AUTO_APPROVE_DEFAULT_GATE_TAGS)


def should_auto_approve(tags: list[str] | tuple[str, ...] | None) -> bool:
    """Should this ideation-proposed task have its `@blocked:review`
    codespan dropped at `add_backlog` time?

    True iff `AP2_AUTO_APPROVE` is enabled AND `tags` does NOT intersect
    `_auto_approve_gate_tags()`. Tag comparison normalizes leading `#`
    (so a task tagged `"breaking-change"` matches a gate-tag
    `"#breaking-change"`).

    Called by `tools.do_board_edit` on the `add_backlog` branch with the
    proposed task's tag list to decide whether to strip
    `blocked_on="review"` before the row hits TASKS.md. Pure / no I/O —
    relies only on the env layer + the in-memory tag list.
    """
    if not _is_auto_approve_enabled():
        return False
    if not tags:
        return True
    gate_tags = _auto_approve_gate_tags()
    if not gate_tags:
        return True
    for t in tags:
        norm = t.strip()
        if not norm:
            continue
        if not norm.startswith("#"):
            norm = "#" + norm
        if norm in gate_tags:
            return False
    return True


# TB-223: `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` default — also referenced
# by `daemon._auto_approve_paused` (the consumer site). Listed here so
# the three-knob safety model documentation lives next to the master
# switch and gate-tag parsers; `daemon.py` reads the env directly via
# its own `_auto_approve_freeze_threshold()` helper (no circular import
# back into ideation).
AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT = 3


async def _run_ideation(cfg: Config, sdk, mcp_server, *, slots: int) -> None:
    """Run the ideation control-agent unconditionally.

    All gating (disable knob, cooldown, queue-depth, Active hard gate)
    is the caller's responsibility — this helper is the actual SDK
    invocation, prompt-dump, event emission, cooldown bookkeeping, and
    state-file commit. Both `_maybe_ideate` (natural cron-driven path)
    and `force_ideate` (TB-159 manual operator trigger) reuse this
    helper so they emit the same `ideation_empty_board` /
    `ideation_timeout` / `ideation_error` event vocabulary, advance the
    same cooldown clock, and produce the same state-file commit.

    `slots` is the per-cycle proposal-slot budget computed by the caller
    (TB-183) — `max(0, AP2_IDEATION_TRIGGER_TASK_COUNT - workable_count)`.
    It's appended into the `## Current state` snapshot block via the
    `state_extras` mechanism (TB-151) so the agent can read it as a
    single line: `- proposal slots this cycle: N`. The prompt body's
    "propose at most N" instruction reads N from the same line, replacing
    the hardcoded magic-3 that drifted out of sync with the env knob
    (TB-160 introduced the env knob; the prompt body kept "fewer than 3"
    until TB-183 closed the gap).

    Note: `ideation_empty_board` is the historical entry-marker name —
    kept for backward compatibility even though forced runs may fire
    on a non-empty board. Callers distinguish forced from natural via
    the separate `ideation_forced` event the operator-queue drain
    emits at queue-application time (TB-159).
    """
    state = load_state(cfg.cron_state_file)
    last = state.get(IDEATION_NAME, 0.0)
    cooldown = _cooldown_s()
    now = time.time()
    events.append(
        cfg.events_file,
        "ideation_empty_board",
        cooldown_s=cooldown,
        seconds_since_last=int(now - last) if last else None,
    )
    # Lazy imports to avoid daemon ↔ ideation circular dependency.
    from . import daemon as _daemon
    from . import prompts
    from .tools import CONTROL_AGENT_TOOLS

    # TB-168: ideation opts out of the board-counts and recent-commits
    # sub-blocks of `_current_state_block`. The board snapshot is
    # redundant — ideation reads `TASKS.md` directly per its read-order
    # and gets per-section detail with full task titles. The 10 recent
    # commits are ~60% `state:` daemon meta-commits with no signal, and
    # the remaining shipped-feature lines are subsumed by `progress.md`
    # (Step 5 of `ap2/ideation.default.md`). `now:` survives — it's
    # ideation's only deterministic clock for the `_Last updated:` line
    # in the `ideation_state.md` schema.
    #
    # TB-169: ideation also opts in to event-type filtering — the
    # rendered `## Recent events` tail keeps only the kinds ideation
    # actually keys off (lifecycle, operator decisions, cron
    # proposals). `judge_call` / `task_run_usage` / `control_run_usage`
    # / cron-lifecycle / mattermost / daemon-plumbing events are
    # dropped before the 6KB `format_for_prompt` byte cap, so the
    # signal density of the prompt doesn't degrade as observability
    # event volume grows. See `IDEATION_RELEVANT_EVENT_TYPES` for the
    # full list and rationale.
    # TB-183: pre-computed proposal-slot count flows into the snapshot
    # block as a single bulleted line the agent reads near the top of
    # the prompt. Joined to any other state_extras consumers in the
    # future via the same `## Current state` mechanism (TB-151 /
    # TB-163). The prompt body's "propose at most N" instruction reads
    # N from this line — single source of truth, no hardcoded magic
    # number drifting out of sync with `AP2_IDEATION_TRIGGER_TASK_COUNT`.
    state_extras = [f"- proposal slots this cycle: {slots}"]
    full_prompt = prompts.build_control_prompt(
        cfg, IDEATION_NAME, load_prompt(cfg),
        state_extras=state_extras,
        include_board=False, include_commits=False,
        include_types=IDEATION_RELEVANT_EVENT_TYPES,
    )
    max_turns = int(os.environ.get("AP2_IDEATION_MAX_TURNS", IDEATION_MAX_TURNS_DEFAULT))
    # TB-126: snapshot the state surface before ideation runs so the post-
    # run state commit only stages paths ideation actually touched (new
    # briefings, ideation_state.md, TASKS.md / CLAUDE.md from add_backlog,
    # any insights). Briefings already in the working tree from a prior op
    # do NOT ride along.
    pre_snapshot = _daemon._snapshot_state_paths(cfg)
    # TB-89 / TB-192: refresh the insights index. Ideation's Step 0.5 reads
    # `.cc-autopilot/insights/_index.md` for grounding, so the regen must
    # happen BEFORE the control agent starts. It must also happen AFTER
    # `pre_snapshot` so any rewrite to `_index.md` shows up in the post-run
    # diff (`_changed_state_paths`) and rides along in the `state: ideation`
    # commit — TB-192 caught the pre-fix ordering: regen ran before the
    # snapshot, so a rewritten `_index.md` was part of the snapshot baseline,
    # silently sat dirty in the working tree, and broke linear-rollback
    # cohesion (TB-111/TB-112). Lazy: no-op when nothing changed. A failure
    # here must NOT block the run.
    try:
        from . import insights

        insights.maybe_regenerate_index(cfg)
    except Exception:  # noqa: BLE001
        pass
    timed_out, error, stderr_tail, prompt_dump = await _daemon._run_control_agent(
        cfg,
        sdk,
        mcp_server,
        label="ideation",
        prompt=full_prompt,
        allowed_tools=CONTROL_AGENT_TOOLS,
        max_turns=max_turns,
    )
    if timed_out:
        events.append(
            cfg.events_file,
            "ideation_timeout",
            timeout_s=cfg.control_timeout_s,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    elif error is not None:
        events.append(
            cfg.events_file,
            "ideation_error",
            error=error,
            stderr_tail=stderr_tail,
            prompt_dump=str(prompt_dump),
        )
    # Always advance the cooldown — even on failure — so a broken
    # ideation agent doesn't get hammered every tick. For forced runs
    # this is what makes back-to-back `ap2 ideate` calls still subject
    # to the natural cooldown for the NEXT cron-driven fire (TB-159).
    mark_run(cfg.cron_state_file, IDEATION_NAME)
    touched = _daemon._changed_state_paths(
        pre_snapshot, _daemon._snapshot_state_paths(cfg)
    )
    if touched:
        _daemon._commit_state_files(cfg, "state: ideation", paths=touched)


def _compute_slots(cfg: Config) -> tuple[int, int, int]:
    """Return `(slots, queued, threshold)` for the current board.

    TB-183: shared helper so `_maybe_ideate` (natural path) and
    `force_ideate` (operator-forced path) compute the same per-cycle
    proposal-slot budget. `slots = max(0, threshold - queued)` —
    `queued` counts Ready+Backlog only (Pipeline Pending and Frozen do
    not count, matching the existing trigger-gate semantics from
    TB-160). The `max(0, ...)` clamp prevents negative slot counts
    when `queued > threshold`.
    """
    board = Board.load(cfg.tasks_file)
    queued = sum(
        sum(1 for _ in board.iter_tasks(section=s))
        for s in ("Ready", "Backlog")
    )
    threshold = _trigger_task_count()
    slots = max(0, threshold - queued)
    return slots, queued, threshold


async def _maybe_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Fire ideation when the working queue is shallow and the cooldown elapsed.

    Gates (in order):
    1. `AP2_IDEATION_DISABLED` opt-out (tests + manual-only projects).
    2. Active hard gate — non-empty Active means a task is in flight and
       sharing the SDK slot with a control agent is unsafe.
    3. Cooldown — `AP2_IDEATION_COOLDOWN_S` since the last fire. This
       gate is positioned ABOVE every emit-and-`mark_run` branch below
       (TB-186) so that those branches' `mark_run` writes actually
       suppress re-emission on the next tick — pre-TB-186 the slot-skip
       branch was positioned BEFORE the cooldown check, so the early
       return short-circuited before the cooldown clock could gate the
       skip event, and `ideation_skipped_no_slots` fired once per ~30s
       tick instead of once per cooldown window.
    4. Per-cycle proposal-slot budget (TB-183) —
       `slots = max(0, AP2_IDEATION_TRIGGER_TASK_COUNT - (Ready+Backlog))`.
       When `slots <= 0` the queue is already at the operator's
       configured threshold, so there's nothing for the agent to fill;
       we emit `ideation_skipped_no_slots` (so the no-op is visible in
       events.jsonl) and advance the cooldown via `mark_run` (so a
       broken board state can't hammer the gate every tick). This
       subsumes the pre-TB-183 `queued >= threshold` silent-return
       check — same trigger condition, but with explicit event +
       cooldown advancement.
    5. Focus-exhausted gate (TB-174) — when the prior cycle's
       `ideation_state.md` self-reports `Status:
       exhausted-needs-operator` for EVERY focus item under
       `## Current focus assessment`, ideation skips the SDK call
       (emits `ideation_skipped reason=focus_exhausted` and advances
       the cooldown). Closes goal.md's "stops proposing when target
       project's `## Done when` criteria are all met" Done-when
       bullet at the ideator-self-report level: today, even a
       unanimous `exhausted-needs-operator` self-report keeps burning
       SDK cost on increasingly thin proposals every cooldown window.
       The forced path (`force_ideate`, TB-159) bypasses this gate so
       the operator can override after refreshing goal.md.

    Delegates the actual SDK invocation + bookkeeping to `_run_ideation`
    so the forced-run path (`force_ideate`, TB-159) shares the same
    event vocabulary, cooldown writeback, and state-file commit. The
    computed `slots` value flows into `_run_ideation` so the prompt's
    `## Current state` block carries `- proposal slots this cycle: N`
    (TB-183) — the agent reads N from there instead of the
    pre-TB-183 hardcoded magic-3 in the prompt body.

    Set `AP2_IDEATION_DISABLED=1` to opt out entirely (the tests use this
    by default; it's also useful for projects that want to drive ideation
    manually rather than on the natural gate).
    """
    if os.environ.get("AP2_IDEATION_DISABLED", "").strip() in ("1", "true", "yes"):
        return
    board = Board.load(cfg.tasks_file)
    # Active is a HARD gate independent of the threshold: a task agent and
    # a control agent cannot share the SDK slot safely (TB-159 background).
    # Skip whenever Active is non-empty regardless of how many Ready/Backlog
    # items there are.
    if next(board.iter_tasks(section="Active"), None) is not None:
        return
    state = load_state(cfg.cron_state_file)
    last = state.get(IDEATION_NAME, 0.0)
    cooldown = _cooldown_s()
    now = time.time()
    if now - last < cooldown:
        return
    slots, queued, threshold = _compute_slots(cfg)
    if slots <= 0:
        # TB-183: queue at-or-above threshold → no slots to fill. Emit
        # the explicit skip event (so the no-op shows up in events.jsonl
        # rather than vanishing into a silent return) and advance the
        # cooldown so a wedged-at-threshold board doesn't hammer the
        # gate on every tick.
        #
        # TB-186: this branch must run AFTER the cooldown gate above —
        # `mark_run` here only suppresses re-emission on subsequent ticks
        # if the cooldown check actually reads `last_run` before reaching
        # this branch. (The pre-TB-186 ordering placed this branch first,
        # so the early-return short-circuited before the cooldown check
        # ever ran, and the gate fired once per ~30s tick instead of once
        # per cooldown window.)
        events.append(
            cfg.events_file,
            "ideation_skipped_no_slots",
            queued=queued,
            threshold=threshold,
        )
        mark_run(cfg.cron_state_file, IDEATION_NAME)
        return
    # TB-246: roadmap-complete gate — when the focus-list pointer has
    # advanced past the LAST `## Current focus:` heading in goal.md AND
    # the operator has not yet acked the `roadmap_complete` halt for
    # the current foci-list length, the daemon's axis-4 dispatch
    # (`daemon.py`) and auto-approve (`tools.py`) paths already block
    # forward motion via `goal.roadmap_exhausted(cfg)`. Without a
    # matching ideation gate, ideation keeps firing every cooldown
    # window during a walk-away weekend and proposals pile up as
    # `@blocked:review` against an already-exhausted roadmap (up to
    # ~48 wasted SDK calls per 48h × 60-min cooldown). Mirrors the
    # TB-174 focus-exhausted gate's shape (same `ideation_skipped`
    # event type with a structured `reason` field, same `mark_run`
    # cooldown bump) and uses the SAME canonical predicate the
    # dispatch + auto-approve gates use — single source of truth, no
    # new state file. Cheaper than the focus-exhausted gate below
    # (dict load + bounded event-tail scan vs. ideation_state.md
    # parse) so it runs first. `force_ideate` bypasses this gate
    # alongside the focus-exhausted gate so the operator's recovery
    # path (`ap2 ack roadmap_complete && ap2 update-goal && ap2
    # ideate --force`) still works if the ack hasn't propagated to
    # the pointer state yet.
    from . import goal as _goal  # local import to avoid module-load cycle
    if _goal.roadmap_exhausted(cfg):
        events.append(
            cfg.events_file,
            "ideation_skipped",
            reason="roadmap_complete",
        )
        mark_run(cfg.cron_state_file, IDEATION_NAME)
        return
    # TB-174: focus-exhausted gate — if the prior cycle's
    # ideation_state.md self-reports `Status: exhausted-needs-operator`
    # for every focus item under `## Current focus assessment`, skip
    # the SDK call. The natural path is the only one that gates here;
    # `force_ideate` (TB-159) bypasses this check so the operator can
    # override after refreshing goal.md. We still call `mark_run` so a
    # 30s daemon tick doesn't keep re-evaluating the gate every loop.
    focus_statuses = parse_focus_statuses(
        cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    )
    if focus_statuses and all(
        s == "exhausted-needs-operator" for s in focus_statuses.values()
    ):
        events.append(
            cfg.events_file,
            "ideation_skipped",
            reason="focus_exhausted",
            focus_count=len(focus_statuses),
        )
        mark_run(cfg.cron_state_file, IDEATION_NAME)
        return
    await _run_ideation(cfg, sdk, mcp_server, slots=slots)


async def force_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Run ideation unconditionally — manual operator trigger (TB-159).

    Bypasses the `AP2_IDEATION_DISABLED` opt-out, the cooldown, the
    Ready+Backlog queue-depth gate, the TB-174 focus-exhausted gate
    (i.e. fires even when every focus item in `ideation_state.md`
    self-reports `Status: exhausted-needs-operator` — that's the
    precise scenario where the operator triggers a forced run after
    refreshing goal.md so the fresh focus has somewhere to land its
    first proposals), AND the TB-246 roadmap-complete gate (i.e.
    fires even when `goal.roadmap_exhausted(cfg)` is True — the
    operator's standard recovery path is `ap2 ack roadmap_complete
    && ap2 update-goal && ap2 ideate --force`, and the forced run
    must work even when the ack hasn't yet propagated to the pointer
    state). Does NOT bypass the Active hard gate — that check lives
    at queue-append time in `do_operator_queue_append({"op":
    "ideate", ...})` and at drain time is implicit (the daemon won't
    dispatch the forced run while a task agent is sharing the SDK
    slot).

    Still calls `mark_run` (via `_run_ideation`) after the run so the
    NEXT natural cooldown clock resets — i.e. running `ap2 ideate` ten
    times in a row would still hit a real `AP2_IDEATION_COOLDOWN_S` gap
    before the next cron-driven fire. The `ideation_forced`
    audit event is emitted by the queue-drain side, not here, so this
    helper stays the single SDK-invocation path shared with `_maybe_ideate`.

    TB-183: the per-cycle slot count flows through unchanged — forced
    runs compute the same `max(0, threshold - workable)` against the
    current board so the agent's `## Current state` snapshot still
    carries `- proposal slots this cycle: N`. A forced run with
    `slots=0` is intentional (the operator triggered the run knowing
    the board was full); the prompt body's "if N is 0, do not propose"
    rule still applies, so the agent does the assessment without
    adding tasks.
    """
    slots, _, _ = _compute_slots(cfg)
    await _run_ideation(cfg, sdk, mcp_server, slots=slots)
