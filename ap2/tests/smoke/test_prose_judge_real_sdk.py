"""Real-SDK round-trip for the verifier prose judge (TB-103 follow-up),
parametrized over BOTH adapter backends (TB-376; backend parity for the judge
kinds).

The prose judge is what produces `verification_partial` events when it can't
reach a confident verdict (TB-146 round 2 was a real instance). This smoke pins,
now for BOTH the claude AND codex backends:

  1. Given a diff that obviously satisfies a prose bullet, the judge returns
     status="pass".
  2. Given a diff that obviously contradicts a prose bullet, the judge returns
     status="fail".

TB-376: the judge call is dispatched through the production `AgentAdapter` seam
resolved for the `verifier_judge` kind (`select_adapter("verifier_judge", cfg)` +
`adapter.run_to_result(...)`), and the smoke is parametrized over the `claude`
and `codex` backends so the SAME verdict assertion runs against whichever backend
the kind selects. Pointing `verifier_judge` at codex
(`AP2_AGENT_BACKEND_VERIFIER_JUDGE=codex`, set by `force_backend`) now actually
exercises a live codex agent returning a prose-judge verdict — coverage the
claude-only pre-TB-376 smoke omitted. The judge's verdict text is parsed by the
SAME production parser (`verify._parse_judge_response`) the verifier uses, so the
smoke pins the real verdict, not a re-derived one. If either backend mis-verdicts
(a confident-but-wrong pass/fail), the assert below still fires — only a
transport/service fault (non-`complete` adapter result) skips.

OPT-IN: `AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s`. Default runs skip
via the module-level pytestmark. The codex variant carries a secondary gate (the
`openai_codex` `importorskip` in `gate_backend`) so `AP2_REAL_SDK=1` on a box
without the codex backend skips rather than errors.
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

# The prose bullet both cases evaluate against. The pass case supplies a diff
# (and working-tree file) that obviously satisfies it; the fail case supplies an
# unrelated diff that obviously does not.
_BULLET = (
    "`scripts/run_foo.py` exists with a function `build_grid` returning 6 entries."
)

_PASS_DIFF = (
    "+++ b/scripts/run_foo.py\n"
    "+def build_grid():\n"
    "+    return [\n"
    "+        {'a': 1}, {'a': 2}, {'a': 3},\n"
    "+        {'b': 1}, {'b': 2}, {'b': 3},\n"
    "+    ]\n"
)

_FAIL_DIFF = (
    "+++ b/README.md\n"
    "+\n"
    "+## Update\n"
    "+\n"
    "+Documentation cleanup, no source changes.\n"
)


def _prose_prompt(bullet_text: str, diff_text: str) -> str:
    """Compose the prose-judge prompt in the SAME output-contract shape
    `verify._judge_prose_bullet` uses, so `verify._parse_judge_response` parses
    the verdict the same way production does. All evidence is inline (the diff)
    so the verdict is robust regardless of per-backend tool access."""
    return (
        "You are evaluating ONE acceptance bullet from a task's verification "
        "section against the agent's cumulative diff.\n\n"
        "OUTPUT CONTRACT — your FINAL message must be a JSON object only:\n"
        '  {"status": "pass", "rationale": "X exists per the diff"}\n'
        "Rules for the FINAL message:\n"
        "  - It is a JSON object only. No markdown code fences, no leading "
        "preamble, no trailing commentary after the closing brace.\n"
        '  - `status` is exactly `"pass"` or `"fail"` (lowercase).\n'
        "  - `rationale` is a single short sentence.\n\n"
        f"Bullet:\n  {bullet_text}\n\n"
        f"Cumulative diff:\n```\n{diff_text}\n```\n"
    )


def _judge_via_adapter(
    backend: str,
    monkeypatch,
    tmp_path,
    *,
    bullet_text: str,
    diff_text: str,
    working_tree: dict[str, str] | None = None,
) -> "object":
    """Route ONE prose-judge call through `select_adapter("verifier_judge", cfg)`
    for `backend` and return the parsed `CriterionResult`.

    The adapter is resolved through the production per-kind selector (so the
    backend the parametrization pins is the one that actually answers). The raw
    `AgentResult` flows through `call_with_transient_retry` so `agent_result_transient`
    classifies a non-`complete` adapter fault (error/timeout) as transient → skip
    (mirroring the pre-TB-376 `judge error` posture); a `complete` result is
    parsed by the SAME production parser (`verify._parse_judge_response`) the
    verifier uses, so a confident-but-wrong verdict still fails the assert.
    """
    from pathlib import Path

    from ap2 import verify
    from ap2.adapters import AgentTools, select_adapter

    cfg = bootstrap_judge_cfg(tmp_path)
    adapter = select_adapter("verifier_judge", cfg)
    assert adapter.backend == backend, adapter.backend

    # TB-136: the judge treats the working tree at HEAD as authoritative; write
    # the asserted file so a Read-capable judge confirms it on the pass case.
    for rel, content in (working_tree or {}).items():
        target = Path(tmp_path) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    prompt = _prose_prompt(bullet_text, diff_text)
    tools = AgentTools(allowed=list(verify.JUDGE_REPO_READ_TOOLS))

    def _run() -> "object":
        return run_judge_to_result(adapter, backend, prompt, tools, cwd=tmp_path)

    result = call_with_transient_retry(
        _run,
        describe=f"prose judge smoke [{backend}]",
        transient_of=agent_result_transient,
    )
    # `agent_result_transient` guarantees a `complete` result here (a non-complete
    # adapter fault would have skipped). Parse the verdict the production way.
    return verify._parse_judge_response(
        bullet_text, (result.text or "").strip()
    ).verdict


@pytest.mark.parametrize("backend", BACKENDS)
def test_prose_judge_passes_obvious_pass_case(backend, monkeypatch, tmp_path):
    """Diff clearly contains the change the bullet describes → pass, on BOTH
    backends."""
    gate_backend(backend)
    force_backend(monkeypatch, "verifier_judge", backend)

    result = _judge_via_adapter(
        backend,
        monkeypatch,
        tmp_path,
        bullet_text=_BULLET,
        diff_text=_PASS_DIFF,
        working_tree={
            "scripts/run_foo.py": (
                "def build_grid():\n"
                "    return [\n"
                "        {'a': 1}, {'a': 2}, {'a': 3},\n"
                "        {'b': 1}, {'b': 2}, {'b': 3},\n"
                "    ]\n"
            ),
        },
    )
    print(f"[smoke:{backend}] obvious-pass result: status={result.status!r} "
          f"notes={result.notes!r}")
    assert result.status == "pass", (
        f"[{backend}] expected pass for obvious-satisfaction case, got "
        f"status={result.status!r} notes={result.notes!r}"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_prose_judge_fails_obvious_fail_case(backend, monkeypatch, tmp_path):
    """Diff is clearly empty / unrelated → fail, on BOTH backends."""
    gate_backend(backend)
    force_backend(monkeypatch, "verifier_judge", backend)

    result = _judge_via_adapter(
        backend,
        monkeypatch,
        tmp_path,
        bullet_text=_BULLET,
        diff_text=_FAIL_DIFF,
    )
    print(f"[smoke:{backend}] obvious-fail result: status={result.status!r} "
          f"notes={result.notes!r}")
    assert result.status == "fail", (
        f"[{backend}] expected fail for obvious-contradiction case, got "
        f"status={result.status!r} notes={result.notes!r}"
    )
