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

import re
from pathlib import Path

from .config import Config, DEFAULT_IDEATION_MAX_TURNS


IDEATION_NAME = "ideation"
# TB-278: re-pointed at the new `config.DEFAULT_IDEATION_MAX_TURNS` named
# constant so the timeouts / max-turns defaults live in one place. The
# alias survives for backwards compat (tests + code that import
# `IDEATION_MAX_TURNS_DEFAULT` directly keep working). Value bumped from
# the prior 30 to 100 as part of the TB-278 battle-tested-defaults pass.
IDEATION_MAX_TURNS_DEFAULT = DEFAULT_IDEATION_MAX_TURNS
IDEATION_COOLDOWN_DEFAULT_S = 3600  # 1h between fires when board stays empty (TB-418, was 7200)
# Trigger threshold: ideation fires when Ready+Backlog count is BELOW this
# value (and Active is empty). TB-183: also serves as the per-cycle
# proposal-slot budget — `slots = max(0, threshold - workable)` flows into
# the prompt's `## Current state` snapshot block so the agent reads it
# from a single source of truth instead of a hardcoded magic-3 in the
# prompt body. Tunable via AP2_IDEATION_TRIGGER_TASK_COUNT.
# TB-418: baseline bumped 3 → 10 (operator-directed cadence default).
IDEATION_TRIGGER_TASK_COUNT_DEFAULT = 10

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
    # Self-skip telemetry — emitted by `_maybe_ideate` when a gate
    # short-circuits the natural ideation cron (today: TB-246
    # `reason=roadmap_complete` when the focus pointer has crossed
    # the last goal.md `## Current focus:` heading). Visible to the
    # next cycle so the ideator sees the prior gate trip in its
    # events block (avoids re-discovering stale skip signals across
    # cycles). TB-284 retired the focus-exhaustion-self-report
    # variant along with its predicate — the empty-cycles
    # focus-advance heuristic (TB-283) is now the authority on
    # exhaustion.
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
# `parse_focus_statuses` returns `{focus title: status}` for surfaces
# that want to render the per-cycle self-assessment (web home page,
# `ap2 status`, future operator-facing summaries). TB-284 removed the
# `_maybe_ideate` self-skip predicate that used to short-circuit the
# natural ideation cron when every focus item was
# `exhausted-needs-operator` — the empty-cycles focus-advance heuristic
# (TB-283) is now the authority on exhaustion, and the post-write
# scrub (`ideation_scrub.scrub_exhaustion_language`) strips the verdict
# language that was the only thing producing the cached
# `exhausted-needs-operator` statuses anyway.
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
    `unknown`. TB-284 removed the `_maybe_ideate` self-skip predicate
    that used these values; the parser is now a pure read used by
    rendering surfaces (web home, `ap2 status`).

    Title handling: the title may wrap across one indented continuation
    line before its closing `**` (the load-bearing example from
    production: `**Ideation quality (gap-covering without drift; push\n
    for progress without scope creep)**` spans two lines). Whitespace
    inside the title collapses to single spaces.

    Pure / no I/O beyond the single read of `path`. Defensive against
    OSErrors (returns ``{}`` rather than raising) so a transient
    permission glitch on the ideation_state file never crashes a
    caller mid-tick.
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


# TB-223: opt-in `AP2_AUTO_APPROVE` mode lets the daemon dispatch
# ideation-proposed tasks without the operator running `ap2 approve`
# first — closes the most-frequently-triggered operator-in-the-loop
# bottleneck under the **Current focus: end-to-end automation** goal.
#
# TB-383 (axis 3): the tags-policy half of this safety model
# (`AUTO_APPROVE_DEFAULT_GATE_TAGS`, `_is_auto_approve_enabled`,
# `_auto_approve_gate_tags`, `should_auto_approve`) RELOCATED to the
# `auto_approve` component (`ap2/components/auto_approve/impl.py`) — the
# component that OWNS the `AP2_AUTO_APPROVE` / `AP2_AUTO_APPROVE_GATE_TAGS`
# knobs. It squatted here for historical reasons (TB-223 put the
# "three-knob safety model" next to the ideation prompt); moving it lets
# the ideation extraction (axis 4) proceed without an
# `evaluate_auto_approve_decision` → `ideation` reach-back. Only the
# `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` default below remains here (consumed
# by the daemon's circuit-breaker via component→core access, not the tags
# policy). The auto-approve decision no longer happens at `add_backlog`
# mutation time at all — `board_edit` is policy-free and the
# `auto_approve` component's PRE_DISPATCH loop pass strips
# `@blocked:review` from gate-clearing Backlog tasks between agent runs.

# TB-223: `AP2_AUTO_APPROVE_FREEZE_THRESHOLD` default — also referenced
# by `daemon._auto_approve_paused` (the consumer site). Listed here so
# the three-knob safety model documentation lives next to the master
# switch and gate-tag parsers; `daemon.py` reads the env directly via
# its own `_auto_approve_freeze_threshold()` helper (no circular import
# back into ideation).
AUTO_APPROVE_FREEZE_THRESHOLD_DEFAULT = 3


# ---------------------------------------------------------------------------
# TB-391: proposal-engine relocation — back-compat `__getattr__` shim.
# ---------------------------------------------------------------------------
#
# The ideation proposal engine (the natural empty-board trigger gate
# `_maybe_ideate`, the shared SDK-invocation helper `_run_ideation`, the
# per-cycle slot budget `_compute_slots`, the operator-forced run
# `force_ideate`, the post-write exhaustion-language scrub
# `_maybe_scrub_ideation_state`, and the ideation knob readers
# `_cooldown_s` / `_trigger_task_count` / `_ideation_disabled`) moved into
# the `ideation` component at `ap2/components/ideation/impl.py` (TB-391
# axis 4). `daemon._tick` now drives them purely via the registry's
# `Phase.IDEATION` tick hook (no inline `ideation` import). This module
# keeps the read-layer parsers (`parse_operator_decisions` /
# `parse_focus_statuses`), the prompt loader (`load_prompt`, whose
# `__file__`-relative path resolves `ideation.default.md` in `ap2/`), and
# the shared constants — they are read-layer / shared data consumed by
# core surfaces (web home, `ap2 status`, status-report, the auto_approve
# component), not loop participants.
#
# The PEP-562 module-level `__getattr__` re-exports the moved symbols so
# every non-core caller (the ideation tests, the web-home gate mirror's
# `ideation._cooldown_s(cfg)`, etc.) keeps resolving via `ap2.ideation`.
# The dynamic `importlib.import_module` (NOT a static
# `from ap2.components... import ...`) keeps the TB-311 import-direction
# gate green — the gate AST-walks `Import`/`ImportFrom` nodes and exempts
# dynamic `importlib` calls (the same escape hatch the registry uses for
# component discovery). `monkeypatch.setattr(ideation, "_maybe_ideate",
# ...)` still works: `__getattr__` makes `hasattr` true, so pytest records
# an original and shadows it with a real attribute, and the component's
# tick-hook wrapper reads through this module's namespace so the patch
# controls what the daemon runs.
_MOVED_TO_COMPONENT: frozenset[str] = frozenset(
    {
        "_maybe_ideate",
        "force_ideate",
        "_run_ideation",
        "_compute_slots",
        "_maybe_scrub_ideation_state",
        "_cooldown_s",
        "_trigger_task_count",
        "_ideation_disabled",
    }
)


def __getattr__(name: str):
    """PEP-562 lazy re-export of the moved proposal-engine symbols (TB-391)."""
    if name in _MOVED_TO_COMPONENT:
        import importlib

        impl = importlib.import_module("ap2.components.ideation.impl")
        return getattr(impl, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
