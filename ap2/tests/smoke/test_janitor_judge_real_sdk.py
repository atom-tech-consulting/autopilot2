"""Real-SDK round-trip for the janitor per-finding judge (TB-178),
parametrized over BOTH adapter backends (TB-376; backend parity for the judge
kinds).

The janitor judge classifies each deterministic git-stranded-state finding as
`real_strand` (unintended detritus), `operator_draft` (deliberate operator work),
or `ambiguous`. Before TB-376 the janitor judge had NO real-SDK smoke on either
backend — leaving codex live-validated for only a subset of the nine agent kinds.
This smoke pins, for BOTH the claude AND codex backends, that the judge returns
the correct verdict on two representative obviously-classifiable findings:

  1. A staged file matching a completed pipeline's expected output WITH a
     commit-failure log → `real_strand`.
  2. An untracked repo-root file with operator-style naming that no TB-N
     references and the operator just touched → `operator_draft`.

TB-376: the judge call is dispatched through the production `AgentAdapter` seam
resolved for the `janitor_judge` kind (`select_adapter("janitor_judge", cfg)` +
`adapter.run_to_result(...)`), parametrized over the `claude` and `codex`
backends so the SAME verdict assertion runs against whichever backend the kind
selects. Pointing `janitor_judge` at codex
(`AP2_AGENT_BACKEND_JANITOR_JUDGE=codex`, set by `force_backend`) exercises a live
codex agent producing the classification verdict. The verdict JSON is parsed by
the SAME production parser (`janitor.impl._parse_judge_response`) the janitor
uses, so the smoke pins the real verdict — a judge that dispatches but
mis-verdicts is exactly the failure mode this catches.

A backend-neutral dispatch (rather than `_judge_finding`) is used because the
production janitor judge hardcodes a Claude model (`agent_model`'s
`claude-opus-4-7`), which a live codex turn would reject; routing the finding
prompt directly through the per-kind seam with no Claude-specific model is what
lets the SAME smoke run on both backends.

OPT-IN: this test makes real API calls. It only runs when AP2_REAL_SDK is set:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/test_janitor_judge_real_sdk.py -v -s

The codex variant carries a secondary gate (the `openai_codex` `importorskip` in
`gate_backend`) so `AP2_REAL_SDK=1` on a box without the codex backend skips
rather than errors. A transport/service fault (non-`complete` adapter result)
skips after one bounded retry; a confident-but-wrong verdict still fails.
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


def _janitor_prompt(finding_block: str, context_block: str) -> str:
    """Compose the per-finding janitor-judge prompt in the SAME output-contract
    shape `janitor.impl._judge_finding` uses, so the production parser
    (`_parse_judge_response`) reads the verdict identically. The finding +
    context are inline so the verdict is robust regardless of per-backend tool
    access."""
    return (
        "You are classifying ONE janitor finding (a candidate stranded git-"
        "state observation) as either unintended detritus or deliberate "
        "operator work. Answer with ONE LINE of JSON: "
        '{"verdict": "real_strand" | "operator_draft" | "ambiguous", '
        '"reasoning": "<one sentence, <=200 chars>"}. '
        "Do not include any other text outside that JSON line.\n\n"
        "Verdict semantics:\n"
        "  real_strand    — high confidence the file is unintended detritus. "
        "Example: a staged file matches a recently-completed pipeline task's "
        "expected output paths AND the pipeline log shows a commit failure.\n"
        "  operator_draft — high confidence the file is deliberate operator "
        "work. Example: an untracked file in repo root with operator-style "
        "naming (`draft_*.md`, `notes-*.md`, `scratch.*`) AND no TB-N references "
        "it AND the operator touched it recently.\n"
        "  ambiguous      — judge cannot make a confident call.\n\n"
        f"Finding:\n{finding_block}\n\n"
        f"Static context (built once per janitor run):\n{context_block}\n"
    )


def _judge_finding_verdict(
    backend: str, monkeypatch, tmp_path, *, finding_block: str, context_block: str
) -> str:
    """Route ONE janitor-judge call through `select_adapter("janitor_judge", cfg)`
    for `backend` and return the parsed verdict string (production parser)."""
    from ap2.adapters import AgentTools, select_adapter
    from ap2.components.janitor.impl import (
        JUDGE_REPO_READ_TOOLS,
        _parse_judge_response,
    )

    cfg = bootstrap_judge_cfg(tmp_path)
    adapter = select_adapter("janitor_judge", cfg)
    assert adapter.backend == backend, adapter.backend

    prompt = _janitor_prompt(finding_block, context_block)
    tools = AgentTools(allowed=list(JUDGE_REPO_READ_TOOLS))

    def _run() -> object:
        return run_judge_to_result(
            adapter, backend, prompt, tools, cfg=cfg, cwd=tmp_path,
        )

    result = call_with_transient_retry(
        _run,
        describe=f"janitor judge smoke [{backend}]",
        transient_of=agent_result_transient,
    )
    verdict, reasoning = _parse_judge_response((result.text or "").strip())
    print(f"[smoke:{backend}] verdict={verdict!r} reasoning={reasoning!r}")
    return verdict


@pytest.mark.parametrize("backend", BACKENDS)
def test_janitor_judge_classifies_real_strand(backend, monkeypatch, tmp_path):
    """A staged file matching a completed pipeline's output with a commit-failure
    log → `real_strand`, on BOTH backends."""
    from ap2.components.janitor.impl import VERDICT_REAL_STRAND

    gate_backend(backend)
    force_backend(monkeypatch, "janitor_judge", backend)

    verdict = _judge_finding_verdict(
        backend,
        monkeypatch,
        tmp_path,
        finding_block=(
            "  subkind: staged_uncommitted\n"
            "  paths: data/spy_cache.parquet\n"
            "  age_s: 2400\n"
            "  hint: a staged file is sitting uncommitted in the index"
        ),
        context_block=(
            "  Pipeline task TB-742 'spy-cache-prep' completed 40 minutes ago; "
            "its expected output path is exactly data/spy_cache.parquet. The "
            "TB-742 pipeline log shows `git commit` exited non-zero (the commit "
            "FAILED), leaving the file staged. No operator activity has touched "
            "this path; data/ siblings are otherwise gitignored."
        ),
    )
    assert verdict == VERDICT_REAL_STRAND, (
        f"[{backend}] expected {VERDICT_REAL_STRAND!r} for a failed-pipeline "
        f"staged artifact, got {verdict!r}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_janitor_judge_classifies_operator_draft(backend, monkeypatch, tmp_path):
    """An untracked operator-named repo-root draft the operator just touched →
    `operator_draft`, on BOTH backends."""
    from ap2.components.janitor.impl import VERDICT_OPERATOR_DRAFT

    gate_backend(backend)
    force_backend(monkeypatch, "janitor_judge", backend)

    verdict = _judge_finding_verdict(
        backend,
        monkeypatch,
        tmp_path,
        finding_block=(
            "  subkind: untracked_non_ignored\n"
            "  paths: draft_roadmap_notes.md\n"
            "  age_s: 300\n"
            "  hint: an untracked file is present in the working tree"
        ),
        context_block=(
            "  draft_roadmap_notes.md is an untracked file in the repo ROOT with "
            "operator-style naming (`draft_*.md`). No TB-N task references it in "
            "the in-flight list, and the operator modified it 5 minutes ago. Its "
            "repo-root siblings are tracked, not gitignored."
        ),
    )
    assert verdict == VERDICT_OPERATOR_DRAFT, (
        f"[{backend}] expected {VERDICT_OPERATOR_DRAFT!r} for a freshly-touched "
        f"operator-named root draft, got {verdict!r}"
    )
