"""Real-SDK round-trip for the LLM dep-coherence validator judge (TB-249),
parametrized over BOTH adapter backends (TB-376; backend parity for the judge
kinds).

The validator judge identifies hard predecessors named implicitly in a task
briefing's prose, gating queue-append-time dependency coherence (validator check
#7). This smoke pins, now for BOTH the claude AND codex backends, that the judge
returns the correct verdict:

  1. Given a briefing that claims NO hard predecessor, the judge returns an
     EMPTY `hard_predecessors` list (the briefing is coherent → validator passes).
  2. Given a briefing that explicitly names an uncommitted TB-N as a hard
     predecessor, the judge IDENTIFIES that TB-N (the briefing is incoherent →
     validator would gate).

TB-376: the judge call is dispatched through the production `AgentAdapter` seam
resolved for the `validator_judge` kind (`select_adapter("validator_judge", cfg)`
+ `adapter.run_to_result(...)`), parametrized over the `claude` and `codex`
backends so the SAME verdict assertion runs against whichever backend the kind
selects. Pointing `validator_judge` at codex
(`AP2_AGENT_BACKEND_VALIDATOR_JUDGE=codex`, set by `force_backend`) exercises a
live codex agent producing the dep-coherence verdict. The verdict JSON is parsed
by the SAME production parser (`validator_judge.impl._parse_dep_judge_response`)
the validator uses, so the smoke pins the real verdict.

Why a fresh dispatch and not the full `_validate_briefing_structure` path: the
production validator's `_judge_dep_coherence_default` resolves its adapter with
`cfg=None` (always claude) AND hardcodes `model="claude-haiku-4-5"`, so it can
neither honor a forced backend nor run on codex (a codex turn rejects a Claude
model). Routing the dep-coherence prompt directly through the per-kind seam with
no Claude-specific model is what lets the SAME smoke run on both backends.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/test_validator_judge_real_sdk.py -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors. A transport/service fault (non-`complete` adapter result)
skips after one bounded retry; a confident-but-wrong verdict still fails.
"""
from __future__ import annotations

import json
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

# A coherent briefing: claims NO external dependency. The judge should return an
# empty hard_predecessors list (the validator would return None → pass).
_GOOD_BRIEFING = (
    "## Goal\n\n"
    "Add a one-line no-op helper to ap2/tools.py so the validator has a real "
    "briefing to chew on. This task has no external dependencies — it touches a "
    "single self-contained file.\n\n"
    "## Scope\n\n"
    "(1) Add `def _toy_noop(): return None` to ap2/tools.py.\n"
)

# An incoherent briefing: explicitly names an UNCOMMITTED predecessor (TB-901)
# whose work this task imports, with no `@blocked:` codespan declaring it. The
# judge should identify TB-901 as a hard predecessor (the validator would gate).
_DEP_PREDECESSOR = "TB-901"
_BAD_BRIEFING = (
    "## Goal\n\n"
    "Wire this task's new module to the shared tool registry.\n\n"
    "## Scope\n\n"
    f"(1) Import `build_tool_set` from `ap2/_shared.py` — the module that "
    f"{_DEP_PREDECESSOR} created and committed. This task cannot begin until "
    f"{_DEP_PREDECESSOR}'s `ap2/_shared.py` is on disk; every Scope item below "
    f"calls a symbol {_DEP_PREDECESSOR} defines.\n"
)


def _validator_prompt(
    briefing_markdown: str, description: str, blocked_tokens: list[str]
) -> str:
    """Compose the dep-coherence prompt in the SAME output-contract shape
    `validator_judge.impl._judge_dep_coherence_default` uses, so the production
    parser (`_parse_dep_judge_response`) reads the verdict identically."""
    system_text = (
        "You are validating a task briefing for hard-predecessor dependency "
        "coherence. A hard predecessor is another task whose work must be on "
        "disk (committed) before this task's agent can do its own work — code "
        "modules, schema, env knobs, or other artifacts the new task depends "
        "on. Soft references (historical context, sibling tasks doing parallel "
        "work, references to docstrings or prior commits for "
        "reading-comprehension only) are NOT hard predecessors.\n\n"
        "OUTPUT CONTRACT — your FINAL message must be a JSON object only:\n"
        '  {"hard_predecessors": ["TB-217"], '
        '"reasoning": "TB-217 created ap2/_shared.py which this briefing '
        'imports"}\n'
        "Rules for the FINAL message:\n"
        "  - It is a JSON object only. No markdown code fences, no leading "
        "preamble, no trailing commentary after the closing brace.\n"
        "  - `hard_predecessors` is a (possibly empty) list of strings, each of "
        "the form 'TB-N'.\n"
        "  - `reasoning` is a single short sentence.\n"
    )
    payload = {
        "briefing_markdown": briefing_markdown,
        "task_description": description,
        "blocked_codespan_tokens": list(blocked_tokens),
    }
    return (
        f"{system_text}\n\n"
        f"Input:\n```json\n{json.dumps(payload, indent=2)}\n```"
    )


def _hard_predecessors(
    backend: str, monkeypatch, tmp_path, *, briefing: str, description: str
) -> list[str]:
    """Route ONE dep-coherence judge call through
    `select_adapter("validator_judge", cfg)` for `backend` and return the parsed
    `hard_predecessors` list (production parser)."""
    from ap2.adapters import AgentTools, select_adapter
    from ap2.briefing_validators import _parse_dep_judge_response

    cfg = bootstrap_judge_cfg(tmp_path)
    adapter = select_adapter("validator_judge", cfg)
    assert adapter.backend == backend, adapter.backend

    prompt = _validator_prompt(briefing, description, blocked_tokens=[])

    def _run() -> object:
        return run_judge_to_result(
            adapter, backend, prompt, AgentTools(), cwd=tmp_path
        )

    result = call_with_transient_retry(
        _run,
        describe=f"validator judge smoke [{backend}]",
        transient_of=agent_result_transient,
    )
    outcome = _parse_dep_judge_response((result.text or "").strip(), events_file=None)
    assert outcome.data is not None, (
        f"[{backend}] validator judge response did not parse to a JSON object "
        f"(parse_error={outcome.parse_error!r}); raw text={result.text!r}"
    )
    preds = outcome.data.get("hard_predecessors")
    return [p for p in (preds or []) if isinstance(p, str)]


@pytest.mark.parametrize("backend", BACKENDS)
def test_validator_judge_coherent_briefing(backend, monkeypatch, tmp_path):
    """A briefing claiming no dependency → empty hard_predecessors, on BOTH
    backends (the pre-TB-376 happy-path assertion, now adapter-routed)."""
    gate_backend(backend)
    force_backend(monkeypatch, "validator_judge", backend)

    preds = _hard_predecessors(
        backend, monkeypatch, tmp_path,
        briefing=_GOOD_BRIEFING, description="add a no-op helper",
    )
    print(f"[smoke:{backend}] coherent-briefing hard_predecessors={preds!r}")
    assert preds == [], (
        f"[{backend}] expected NO hard predecessors for a self-contained "
        f"briefing, got {preds!r}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_validator_judge_incoherent_briefing(backend, monkeypatch, tmp_path):
    """A briefing naming an uncommitted predecessor → that TB-N is identified,
    on BOTH backends."""
    gate_backend(backend)
    force_backend(monkeypatch, "validator_judge", backend)

    preds = _hard_predecessors(
        backend, monkeypatch, tmp_path,
        briefing=_BAD_BRIEFING,
        description="wire the new module to the shared registry",
    )
    print(f"[smoke:{backend}] incoherent-briefing hard_predecessors={preds!r}")
    upper = {p.strip().upper() for p in preds}
    assert _DEP_PREDECESSOR in upper, (
        f"[{backend}] expected the judge to identify {_DEP_PREDECESSOR} as a "
        f"hard predecessor, got {preds!r}"
    )
