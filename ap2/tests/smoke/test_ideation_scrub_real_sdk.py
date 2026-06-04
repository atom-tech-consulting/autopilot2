"""Real-SDK smoke for the `ideation_scrub` control kind, parametrized over BOTH
adapter backends (TB-378 / goal.md axis 7).

`ideation_scrub` was the axis-6 migration canary (TB-360): its dispatch routes
through `select_adapter("ideation_scrub", cfg)` (`_resolve_scrub_adapter`). But
no live smoke ever proved the scrub kind actually produces its expected output —
deleting an exhaustion-verdict sentence while preserving the surrounding factual
breadcrumbs — on either backend.

This smoke closes that gap. It feeds the production scrub prompt
(`ideation_scrub._build_scrub_prompt`, carrying the real `_SCRUB_SYSTEM_PROMPT`)
to a real agent through the SAME per-kind resolver the scrub uses
(`select_adapter("ideation_scrub", cfg)`, under `force_backend(...,
"ideation_scrub", backend)`), and asserts (for BOTH backends) the scrubbed output
SHAPE: the exhaustion-verdict sentence is removed AND the factual breadcrumb
survives. That is the scrub kind's structured-result contract — a smoke that
asserted only "non-empty" would pass a model that returned the input verbatim
(scrubbed nothing), the failure mode that matters.

Dispatch reuses `run_judge_to_result` (the shared `adapter.run_to_result` helper
that pins `model=None`): the scrub kind returns assistant TEXT, not a tool call,
so it routes through `run_to_result` rather than `run_control_to_tool_calls`.
`model=None` is load-bearing for the codex variant — the production scrub path
resolves a Claude model (`claude-haiku-4-5`) a live codex turn would reject, so
the smoke lets each backend use its own default (same rationale as the judge
smokes). A non-`complete` adapter result (transport / service error / timeout)
is classified transient by `agent_result_transient` → skip after one retry; a
`complete` result flows to the verdict-removed / breadcrumb-kept assertions, so a
backend that scrubs WRONGLY still fails.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors.

Bounded cost: a three-line input, max_turns=4 (`run_judge_to_result`).
"""
from __future__ import annotations

import os

import pytest

from ._adapter import (
    BACKENDS,
    agent_result_transient,
    bootstrap_judge_cfg,
    force_backend,
    gate_backend,
    run_judge_to_result,
)
from ._transient import call_with_transient_retry

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


# A factual breadcrumb the scrub must KEEP, and an exhaustion-verdict sentence
# the scrub must DELETE (mirrors the `_SCRUB_SYSTEM_PROMPT` DELETE example
# "This focus is essentially done.").
_SCRUB_FACT = "TB-001 landed the agent-adapter seam."
_SCRUB_VERDICT = "This focus is essentially done."
_SCRUB_INPUT = (
    "## Progress so far\n"
    "\n"
    f"- {_SCRUB_FACT}\n"
    f"- {_SCRUB_VERDICT}\n"
)


@pytest.mark.parametrize("backend", BACKENDS)
def test_ideation_scrub_output_shape_via_adapter(backend, monkeypatch, tmp_path):
    """A real `ideation_scrub`-kind agent, dispatched through
    `select_adapter("ideation_scrub", cfg)` with the production scrub prompt,
    produces the expected scrubbed-text shape for BOTH backends: the
    exhaustion-verdict sentence removed, the factual breadcrumb kept."""
    gate_backend(backend)
    force_backend(monkeypatch, "ideation_scrub", backend)

    from ap2.adapters import AgentTools, select_adapter
    from ap2.ideation_scrub import _build_scrub_prompt

    cfg = bootstrap_judge_cfg(tmp_path)
    adapter = select_adapter("ideation_scrub", cfg)
    assert adapter.backend == backend, adapter.backend
    prompt = _build_scrub_prompt(_SCRUB_INPUT)

    result = call_with_transient_retry(
        lambda: run_judge_to_result(
            adapter, backend, prompt, AgentTools(), cwd=tmp_path,
        ),
        describe=f"ideation_scrub output-shape smoke [{backend}]",
        transient_of=agent_result_transient,
    )

    scrubbed = getattr(result, "text", None) or ""
    print(f"\n[smoke:{backend}] scrubbed output:\n{scrubbed}")

    assert scrubbed.strip(), f"[{backend}] scrub returned empty output"
    # The factual breadcrumb survives the scrub ...
    assert "TB-001" in scrubbed, (
        f"[{backend}] scrub dropped the factual breadcrumb: {scrubbed!r}"
    )
    # ... and the exhaustion-verdict sentence is removed.
    assert "essentially done" not in scrubbed.lower(), (
        f"[{backend}] scrub did NOT remove the exhaustion verdict: {scrubbed!r}"
    )
    print(f"[smoke:{backend}] PASS — verdict scrubbed, breadcrumb kept")
