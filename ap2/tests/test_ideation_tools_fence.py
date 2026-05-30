"""TB-291: regression-pin the ideation toolset fence.

On 2026-05-26 the empty-cycles counter falsely advanced the focus to
``ROADMAP_COMPLETE`` the same tick TB-290 (a real ideation proposal) was
being drained into Backlog. Root cause: the ideation control agent
preferred ``mcp__autopilot__operator_queue_append`` over the prompt-named
``board_edit`` path, because the queue tool's own docstring recommends it
as a TOCTOU defense ("use this instead of `board_edit` when a task agent
is currently active"). That defense doesn't apply to ideation — ideation
is sequential with task execution by construction (``_maybe_ideate`` gates
on ``Active == 0`` and TB-110's snapshot-window fence prevents concurrent
state mutation). But the queue path emits ``operator_queue_append
op=add_backlog``, NOT ``ideation_proposal_recorded`` — and only the latter
is recognized by ``_consecutive_empty_ideation_cycles`` as a proposal-reset
signal. One productive cycle silently ticked the counter as if empty.

The fix fences ideation's toolset to ``IDEATION_TOOLS`` (a strict subset
of ``CONTROL_AGENT_TOOLS`` omitting ``operator_queue_append``), so the
agent must use the direct ``board_edit`` path the counter expects. Other
control agents (cron jobs, MM handler) are unchanged.

These pins are a regression seam: removing the fence, re-adding
``operator_queue_append`` to ``IDEATION_TOOLS``, or dropping the
``allowed_tools=IDEATION_TOOLS`` wire-up in ``_run_ideation`` re-opens the
desync the bug closed.
"""
from __future__ import annotations

import inspect

import ap2.ideation as ideation_mod
from ap2.tools import CONTROL_AGENT_TOOLS, IDEATION_TOOLS


def test_ideation_tools_is_importable():
    """``IDEATION_TOOLS`` is exposed as a top-level attribute of
    ``ap2.tools`` — sibling to ``CONTROL_AGENT_TOOLS`` / ``MM_HANDLER_TOOLS``
    / ``TASK_AGENT_TOOLS``."""
    from ap2 import tools

    assert hasattr(tools, "IDEATION_TOOLS")
    assert tools.IDEATION_TOOLS is IDEATION_TOOLS


def test_ideation_tools_excludes_operator_queue_append():
    """The whole point of the fence: ideation must NOT have
    ``operator_queue_append`` in its allowed tools, so the agent can't
    silently route a proposal through the queue path (which doesn't emit
    ``ideation_proposal_recorded`` and so doesn't reset the empty-cycles
    counter)."""
    assert "mcp__autopilot__operator_queue_append" not in IDEATION_TOOLS


def test_ideation_tools_includes_board_edit():
    """The direct ``board_edit`` path is the ONLY board-mutation surface
    ideation should have. Without it, ideation cannot add backlog tasks
    at all — the fence becomes a regression."""
    assert "mcp__autopilot__board_edit" in IDEATION_TOOLS


def test_ideation_tools_is_strict_subset_of_control_agent_tools():
    """The fence is a *narrowing* — IDEATION_TOOLS must not introduce
    any new tools. Adding a tool here would silently widen ideation's
    surface in a way ``CONTROL_AGENT_TOOLS``-based pins (e.g. the
    Bash-exclusion pin in ``test_tools.py``) would not catch."""
    assert set(IDEATION_TOOLS).issubset(set(CONTROL_AGENT_TOOLS))
    # And the narrowing is strict — at minimum, operator_queue_append is
    # excluded.
    assert set(IDEATION_TOOLS) != set(CONTROL_AGENT_TOOLS)


def test_run_ideation_wires_ideation_tools():
    """The fence is enforced at the call site: ``_run_ideation`` must
    pass ``IDEATION_TOOLS`` to ``_daemon._run_control_agent``. A future
    refactor that silently swaps the symbol back to ``CONTROL_AGENT_TOOLS``
    would unfence the agent without tripping the subset / membership pins
    above."""
    src = inspect.getsource(ideation_mod._run_ideation)
    assert "IDEATION_TOOLS" in src, (
        "TB-291: `_run_ideation` must reference `IDEATION_TOOLS` — either "
        "in its import line or as the `allowed_tools=` kwarg passed to "
        "`_daemon._run_control_agent`. Found neither."
    )
    # Belt-and-braces: the kwarg name + symbol must coincide.
    assert "allowed_tools=IDEATION_TOOLS" in src, (
        "TB-291: `_run_ideation` must pass `allowed_tools=IDEATION_TOOLS` "
        "to `_daemon._run_control_agent`. The symbol may be imported under "
        "an alias but the kwarg wiring is the load-bearing seam."
    )
