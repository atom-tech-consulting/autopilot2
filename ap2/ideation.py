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
  default of 3 matches the prompt's "Propose new tasks ONLY if Backlog
  has fewer than 3 workable items" cap. Set to 1 for the legacy "fire
  only when the working queue is fully empty" behavior.
- Max turns: `AP2_IDEATION_MAX_TURNS` (default 30 — bumped from the legacy
  cron-default 15 because the assessment + failure-review + proposal flow
  routinely needs ~10-15 turns and 15 was running close to the wire).
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
from .config import Config
from .cron import load_state, mark_run


IDEATION_NAME = "ideation"
IDEATION_MAX_TURNS_DEFAULT = 30
IDEATION_COOLDOWN_DEFAULT_S = 7200  # 2h between fires when board stays empty
# Trigger threshold: ideation fires when Ready+Backlog count is BELOW this
# value (and Active is empty). Matches the prompt's "fewer than 3 workable
# items" cap. Tunable via AP2_IDEATION_TRIGGER_TASK_COUNT.
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
)

_DEFAULT_PROMPT_PATH = Path(__file__).parent / "ideation.default.md"
_PROJECT_PROMPT_REL = ".cc-autopilot/ideation_prompt.md"


# TB-173: parser for the `## Open questions for operator` section of
# `.cc-autopilot/ideation_state.md`. The ideation prompt's Step 0 mandates
# this section whenever a focus item is `exhausted-needs-operator`, when
# goal.md appears to need updating, OR when the ideator notices a gap
# outside any current focus item. Today the surface is silent — the
# section sits unread in `ideation_state.md` until the operator manually
# opens the file.
#
# `parse_open_questions` is the single source of truth that `ap2 status`
# (CLI), the web home page, and the cron status-report all call so the
# three operator-facing surfaces stay in sync.
#
# Section-slicing reuses the same shape as
# `ap2/check.py::_check_briefings_manual_bullets` — header regex matches
# the `## Open questions for operator` heading line-anchored, slice runs
# from heading-end to the next `## ` (or EOF). Per-bullet extraction:
# `- ` / `* ` lines start a new entry; subsequent indented lines (two
# spaces or more, or a tab) join the previous bullet with a single
# space; blank lines finalize the current bullet. Cap at
# `_OPEN_QUESTIONS_MAX_ENTRIES` (default 7) entries to bound rendering
# cost; on truncation, append a synthetic "(+M more)" trailer entry so
# the UI surfaces both the visible cap and the residual count.
#
# Failure mode: ideator may write the section as prose paragraphs
# instead of bullets. Defense: when the bullet pass yields nothing, fall
# back to splitting the section body on blank lines and treating each
# paragraph as one entry. The same 7-cap applies to the fallback.
_OPEN_QUESTIONS_HEADER_RE = re.compile(
    r"^##\s+Open questions for operator\s*$", re.M,
)
_OPEN_QUESTIONS_NEXT_SECTION_RE = re.compile(r"^##\s+", re.M)
_OPEN_QUESTIONS_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OPEN_QUESTIONS_MAX_ENTRIES = 7


def parse_open_questions(path: Path) -> list[str]:
    """Return the bullets under `## Open questions for operator` in `path`.

    `path` is the absolute path to a project's
    `.cc-autopilot/ideation_state.md`. Returns ``[]`` when the file is
    missing, when the section header is absent, or when the section is
    empty.

    Bullet handling: each `- ` / `* ` line opens a new entry; indented
    continuation lines under a bullet are joined into the previous entry
    with a single space (newlines collapsed). Blank lines finalize the
    current entry.

    Fallback: if the section has no bullet lines at all (ideator wrote
    prose paragraphs), split the body on blank lines and treat each
    paragraph as one entry — same single-space whitespace collapse.

    Cap: at most `_OPEN_QUESTIONS_MAX_ENTRIES` (7) real entries; on
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
    m = _OPEN_QUESTIONS_HEADER_RE.search(text)
    if m is None:
        return []
    body_start = m.end()
    next_m = _OPEN_QUESTIONS_NEXT_SECTION_RE.search(text, body_start)
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
        bullet_m = _OPEN_QUESTIONS_BULLET_RE.match(raw)
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

    if len(entries) > _OPEN_QUESTIONS_MAX_ENTRIES:
        truncated = entries[:_OPEN_QUESTIONS_MAX_ENTRIES]
        truncated.append(
            f"(+{len(entries) - _OPEN_QUESTIONS_MAX_ENTRIES} more)"
        )
        return truncated
    return entries


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


async def _run_ideation(cfg: Config, sdk, mcp_server) -> None:
    """Run the ideation control-agent unconditionally.

    All gating (disable knob, cooldown, queue-depth, Active hard gate)
    is the caller's responsibility — this helper is the actual SDK
    invocation, prompt-dump, event emission, cooldown bookkeeping, and
    state-file commit. Both `_maybe_ideate` (natural cron-driven path)
    and `force_ideate` (TB-159 manual operator trigger) reuse this
    helper so they emit the same `ideation_empty_board` /
    `ideation_timeout` / `ideation_error` event vocabulary, advance the
    same cooldown clock, and produce the same state-file commit.

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
    # Refresh the insights index — ideation Step 0.5 reads
    # `.cc-autopilot/insights/_index.md` for grounding (TB-89). Lazy:
    # no-op when nothing changed. A failure here must NOT block the run.
    try:
        from . import insights

        insights.maybe_regenerate_index(cfg)
    except Exception:  # noqa: BLE001
        pass
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
    full_prompt = prompts.build_control_prompt(
        cfg, IDEATION_NAME, load_prompt(cfg),
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


async def _maybe_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Fire ideation when the working queue is shallow and the cooldown elapsed.

    Gates (in order):
    1. `AP2_IDEATION_DISABLED` opt-out (tests + manual-only projects).
    2. Active hard gate — non-empty Active means a task is in flight and
       sharing the SDK slot with a control agent is unsafe.
    3. Ready+Backlog count below `AP2_IDEATION_TRIGGER_TASK_COUNT`
       (default 3). Pipeline Pending and Frozen do not count.
    4. Cooldown — `AP2_IDEATION_COOLDOWN_S` since the last fire.

    Delegates the actual SDK invocation + bookkeeping to `_run_ideation`
    so the forced-run path (`force_ideate`, TB-159) shares the same
    event vocabulary, cooldown writeback, and state-file commit.

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
    queued = sum(
        sum(1 for _ in board.iter_tasks(section=s))
        for s in ("Ready", "Backlog")
    )
    if queued >= _trigger_task_count():
        return
    state = load_state(cfg.cron_state_file)
    last = state.get(IDEATION_NAME, 0.0)
    cooldown = _cooldown_s()
    now = time.time()
    if now - last < cooldown:
        return
    await _run_ideation(cfg, sdk, mcp_server)


async def force_ideate(cfg: Config, sdk, mcp_server) -> None:
    """Run ideation unconditionally — manual operator trigger (TB-159).

    Bypasses the `AP2_IDEATION_DISABLED` opt-out, the cooldown, and the
    Ready+Backlog queue-depth gate. Does NOT bypass the Active hard
    gate — that check lives at queue-append time in
    `do_operator_queue_append({"op": "ideate", ...})` and at drain time
    is implicit (the daemon won't dispatch the forced run while a task
    agent is sharing the SDK slot).

    Still calls `mark_run` (via `_run_ideation`) after the run so the
    NEXT natural cooldown clock resets — i.e. running `ap2 ideate` ten
    times in a row would still hit a real `AP2_IDEATION_COOLDOWN_S` gap
    before the next cron-driven fire. The `ideation_forced`
    audit event is emitted by the queue-drain side, not here, so this
    helper stays the single SDK-invocation path shared with `_maybe_ideate`.
    """
    await _run_ideation(cfg, sdk, mcp_server)
