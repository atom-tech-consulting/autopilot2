"""Real-SDK round-trip for `verify._judge_prose_bullet` (TB-103 follow-up).

The prose judge is what produces `verification_partial` events when it
can't reach a confident verdict (TB-146 round 2 was a real instance).
This test pins:

  1. Given a diff that obviously satisfies a prose bullet, the judge
     returns status="pass".
  2. Given a diff that obviously contradicts a prose bullet, the judge
     returns status="fail".

If either edge regresses, every prose-bullet briefing in stoch starts
returning verification_partial (or worse, false-passes) — this catches
that class before it hits prod.

OPT-IN: `AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s`. Default
runs skip via the module-level pytestmark.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from ._transient import call_with_transient_retry

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP2_REAL_SDK"),
    reason="real-SDK smoke; set AP2_REAL_SDK=1 to run",
)


def _run_judge(
    bullet_text: str,
    diff_text: str,
    working_tree: dict[str, str] | None = None,
) -> "verify.CriterionResult":
    """Helper: invoke `verify._judge_prose_bullet` against the real SDK.

    ``working_tree`` is an optional ``{relpath: content}`` dict written
    into the temp project_root before the judge runs. TB-136 made the
    working tree at HEAD authoritative for the prose judge (the judge
    has Read/Glob/Grep tools and is told to confirm the diff against
    real on-disk state), so the obvious-pass case has to mirror that:
    the file the bullet asserts must actually exist in the working
    tree, not just appear in the diff.
    """
    import claude_agent_sdk as sdk

    from ap2 import verify

    bullet = verify.VerifyBullet(kind="prose", text=bullet_text)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, content in (working_tree or {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return asyncio.run(
            verify._judge_prose_bullet(
                bullet,
                project_root=root,
                sdk=sdk,
                diff_text=diff_text,
            )
        )


def test_prose_judge_passes_obvious_pass_case():
    """Diff clearly contains the change the bullet describes → pass.

    Mirrors TB-136 semantics: the file the bullet asserts must exist
    in the working tree (not only in the diff), since the judge treats
    HEAD as authoritative when diff and working tree disagree.
    """
    # TB-351: a transient SDK transport/service error surfaces as a
    # CriterionResult(status="unverified", notes="judge error: ...").
    # That means the wiring couldn't be exercised this run — retry once,
    # then skip (not fail). A clean pass/fail (right or wrong) flows to
    # the assert below unchanged.
    result = call_with_transient_retry(
        lambda: _run_judge(
            bullet_text="`scripts/run_foo.py` exists with a function `build_grid` returning 6 entries.",
            diff_text=(
                "+++ b/scripts/run_foo.py\n"
                "+def build_grid():\n"
                "+    return [\n"
                "+        {'a': 1}, {'a': 2}, {'a': 3},\n"
                "+        {'b': 1}, {'b': 2}, {'b': 3},\n"
                "+    ]\n"
            ),
            working_tree={
                "scripts/run_foo.py": (
                    "def build_grid():\n"
                    "    return [\n"
                    "        {'a': 1}, {'a': 2}, {'a': 3},\n"
                    "        {'b': 1}, {'b': 2}, {'b': 3},\n"
                    "    ]\n"
                ),
            },
        ),
        describe="prose judge obvious-pass smoke",
    )
    print(f"[smoke] obvious-pass result: status={result.status!r} "
          f"notes={result.notes!r}")
    assert result.status == "pass", (
        f"expected pass for obvious-satisfaction case, got "
        f"status={result.status!r} notes={result.notes!r}"
    )


def test_prose_judge_fails_obvious_fail_case():
    """Diff is clearly empty / unrelated → fail."""
    # TB-351: skip (not fail) on a transient SDK error; a clean verdict
    # flows to the assert below. A wrong "pass" here would still fail.
    result = call_with_transient_retry(
        lambda: _run_judge(
            bullet_text="`scripts/run_foo.py` exists with a function `build_grid` returning 6 entries.",
            diff_text=(
                "+++ b/README.md\n"
                "+\n"
                "+## Update\n"
                "+\n"
                "+Documentation cleanup, no source changes.\n"
            ),
        ),
        describe="prose judge obvious-fail smoke",
    )
    print(f"[smoke] obvious-fail result: status={result.status!r} "
          f"notes={result.notes!r}")
    assert result.status == "fail", (
        f"expected fail for obvious-contradiction case, got "
        f"status={result.status!r} notes={result.notes!r}"
    )
