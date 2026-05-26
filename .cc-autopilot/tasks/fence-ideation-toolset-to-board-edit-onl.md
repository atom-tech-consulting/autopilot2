# Fence ideation toolset to board_edit only; remove operator_queue_append

Tags: #autopilot #ideation #tools #empty-cycles #regression-pin #bug

## Goal

Close the empty-cycles counter false-trip path that fired on 2026-05-26 by
fencing the ideation control agent's toolset to `mcp__autopilot__board_edit`
only — i.e. drop `mcp__autopilot__operator_queue_append` from the tool list
ideation runs with. Closes the goal.md `## Done when` failure mode "Ideation
reliably proposes goal-aligned next steps that substantively advance the
goal (not just goal-shaped pro-forma compliance)" — the false trip caused
ideation to advance the focus to ROADMAP_COMPLETE the same tick a valid
proposal (TB-290) was being queued, parking the loop and silently dropping
ideation's substantive contribution; operator-mediated pointer rewind +
manual approval was required to recover, defeating the walk-away promise
for this failure mode.

Why now: at 2026-05-26T08:36:05Z, `focus_advanced trigger=empty_cycles_heuristic`
fired at the same tick TB-290 (a real ideation proposal closing the last
named-in-goal.md attention-detector axis) was being drained into Backlog.
Root cause: the agent chose `operator_queue_append op=add_backlog` (despite
the ideation prompt at `ideation.default.md:318-319` instructing
`board_edit (action: add_backlog)`) because that tool's own description in
`ap2/tools.py:845-847` recommends it as a TOCTOU defense — "use this instead
of board_edit when a task agent is currently active." The agent followed the
tool docstring's safety advice. But ideation and task execution are
sequential by design — ideation's `_maybe_ideate` only fires when Active is
empty, and TB-110's snapshot-window check fences in-flight runs from
concurrent state mutation. The TOCTOU race the queue path defends against
cannot occur during ideation. Meanwhile, the queue-routed proposal emitted
`operator_queue_append op=add_backlog`, NOT `ideation_proposal_recorded` —
and `ideation_proposal_recorded` is the only event the empty-cycles counter
in `ap2/focus_advance.py:_ideation_empty_against_focus` recognizes as a
reset signal. Removing the tool from ideation's toolset forces the agent
down the direct path the counter expects, aligning the prompt + tool surface
+ event vocabulary on one consistent shape. Compounded by a separate
entry/exit-double-count bug in the same counter (filed separately), the
result was that ONE productive cycle ticked the counter from 1 to 3 and
falsely advanced the focus to ROADMAP_COMPLETE.

## Scope

(1) `ap2/tools.py`: introduce a new `IDEATION_TOOLS` list as a subset of
`CONTROL_AGENT_TOOLS`, omitting `mcp__autopilot__operator_queue_append`.
Define near the existing `CONTROL_AGENT_TOOLS` / `MM_HANDLER_TOOLS`
definitions (around line 1096-1194). The other control-agent toolsets
(`CONTROL_AGENT_TOOLS` itself for cron jobs, `MM_HANDLER_TOOLS` for the
Mattermost handler, `TASK_AGENT_TOOLS` for task agents) stay unchanged.

(2) `ap2/ideation.py`: replace the `from .tools import CONTROL_AGENT_TOOLS`
import at line 715 with `from .tools import IDEATION_TOOLS`, and replace
the corresponding `allowed_tools=CONTROL_AGENT_TOOLS` kwarg passed to
`_daemon._run_control_agent` at line 779 with `allowed_tools=IDEATION_TOOLS`.
The control-agent plumbing in `daemon._run_control_agent` accepts
`allowed_tools` as a parameter — no daemon-side changes.

(3) Add a header comment at `IDEATION_TOOLS` explaining design intent:
ideation and task execution are sequential by construction
(`_maybe_ideate` gates on `Active == 0` and TB-110's snapshot-window
fence prevents concurrent state mutation). The TOCTOU race that
`operator_queue_append` defends against cannot occur during ideation,
so the tool is unnecessary surface that the agent will defensively
prefer over the direct `board_edit` path — and that preference desyncs
the empty-cycles counter's reset signal (`ideation_proposal_recorded`
only fires on the `board_edit` path).

(4) No changes to `ideation.default.md` needed — the L318 instruction
"Propose the top 3 via board_edit (action: add_backlog) with a
structured briefing" already names `board_edit` as the correct tool.

(5) New regression-pin module `ap2/tests/test_ideation_tools_fence.py`
asserting: `IDEATION_TOOLS` exists as an importable symbol from
`ap2.tools`; `mcp__autopilot__operator_queue_append` is NOT in
`IDEATION_TOOLS`; `mcp__autopilot__board_edit` IS in `IDEATION_TOOLS`;
`IDEATION_TOOLS` is a strict subset of `CONTROL_AGENT_TOOLS` (no new
tools introduced); `ap2/ideation.py` passes `IDEATION_TOOLS` to its
control-agent invocation (verifiable via `inspect.getsource` of
`_run_ideation` containing the symbol).

## Design

Fence-the-agent over fix-the-counter for two reasons:

(a) **Single source of truth on the proposal-reset signal.** The
counter stays simple — one event type means proposal landed, reset.
Adding `operator_queue_append op=add_backlog` to the counter's reset
set conflates event vocabularies: `operator_queue_append` is the MM
handler's primary mutation event and the operator-CLI surface's audit
event, and the counter shouldn't have to know which of them came from
ideation. Fencing keeps the event-to-reset mapping 1:1.

(b) **Match capability to need.** Ideation has zero need for the
TOCTOU defense the queue path provides — there's no concurrent task
agent during ideation by construction. Removing surface the agent
will defensively choose closes the desync at the source rather than
patching the downstream consequence.

The fence is a small subset definition. No change to the queue path's
own logic (other agents continue to use it). No change to the
counter's reset logic in `_ideation_empty_against_focus` (still keyed
off `ideation_proposal_recorded` only). The separate entry/exit
double-count bug in the same counter is filed under its own TB and
is independent of this fix.

## Verification

- `grep -q '^IDEATION_TOOLS = ' ap2/tools.py` — new constant defined.
- `grep -q 'IDEATION_TOOLS' ap2/ideation.py` — ideation.py imports the fenced toolset.
- `uv run python -c "from ap2.tools import IDEATION_TOOLS, CONTROL_AGENT_TOOLS; assert 'mcp__autopilot__operator_queue_append' not in IDEATION_TOOLS; assert 'mcp__autopilot__board_edit' in IDEATION_TOOLS; assert set(IDEATION_TOOLS).issubset(set(CONTROL_AGENT_TOOLS))"` — fence invariants hold.
- `test -f ap2/tests/test_ideation_tools_fence.py` — regression-pin module exists.
- `uv run pytest -q ap2/tests/test_ideation_tools_fence.py` — fence tests pass.
- `uv run pytest -q` — full suite passes.

## Out of scope

- Fixing the entry/exit double-count bug in
  `_ideation_empty_against_focus` (where `ideation_empty_board` AND
  `ideation_complete` both increment the counter, so one cycle = +2).
  Separate TB; complementary fix needed for full counter correctness.
- Operator-pointer-rewind not emitting a synthetic `focus_advanced`
  event for the counter's cutoff logic. Separate TB; affects manual
  recovery flows.
- TB-284 scrub mechanism silent-timeout bug (the 60s timeout fires
  and `scrub_exhaustion_language` swallows the error with no audit
  event). Separate TB; independent surface.
- Auto-approve gate behavior under `roadmap_complete` (TB-290 didn't
  promote despite docstring claim "task dispatch is NOT affected").
  Separate investigation; behavior may be correct-but-undocumented or
  may be a fourth bug.
- Removing `operator_queue_append` from `CONTROL_AGENT_TOOLS` entirely
  (other control agents — cron jobs — may have legitimate use cases
  not yet exercised; fence ideation only to avoid widening scope).
- Refactoring the broader control-agent toolset taxonomy (could become
  `TASK_AGENT_TOOLS` / `IDEATION_TOOLS` / `MM_HANDLER_TOOLS` /
  `CRON_TOOLS` per-agent fences; that's a bigger restructuring this
  TB doesn't take on).
