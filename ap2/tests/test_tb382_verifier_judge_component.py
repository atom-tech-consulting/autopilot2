"""TB-382 (+ TB-386): the optional LLM prose-bullet judge in the verify runner.

TB-382 had extracted the prose-bullet judge (`_judge_prose_bullet`) out of the
core verify runner into a `ap2/components/verifier_judge/` component reached
via the registry. TB-386 demoted it back into core: a judge invoked only as an
internal sub-step of `verify_task` is NOT a loop-level participant, so it is
core code gated by the plain `AP2_VERIFY_JUDGE_DISABLED` knob (not a manifest
env_flag). The judge still resolves its backend via
`select_adapter("verifier_judge", cfg)` — the adapter seam stays.

This module pins the surviving contract: with `AP2_VERIFY_JUDGE_DISABLED=1`,
`verify_task` still runs — the deterministic shell bullets execute and gate
while the LLM prose judge is skipped (the bullet is recorded `unverified`
without touching the SDK). The shell gate is NOT weakened by disabling the
prose judge.
"""
from __future__ import annotations

import asyncio

from ap2 import verify


def test_judge_prose_bullet_is_a_core_module_function():
    """`_judge_prose_bullet` is a real module-level function in
    `ap2/verify.py` (TB-386 demoted it out of the component); both attribute
    access and `from ap2.verify import _judge_prose_bullet` resolve to it."""
    from ap2.verify import _judge_prose_bullet as via_import

    assert callable(via_import)
    assert verify._judge_prose_bullet is via_import


def test_verify_resolves_prose_judge_via_select_adapter():
    """The prose judge resolves its backend through
    `select_adapter("verifier_judge", cfg)` — the adapter seam survives the
    demotion (TB-386 removed the component wrapper, not the adapter kind)."""
    import inspect

    src = inspect.getsource(verify._judge_prose_bullet)
    assert 'select_adapter("verifier_judge"' in src, (
        "TB-386: the core prose judge must resolve its backend via "
        '`select_adapter("verifier_judge", cfg)`.'
    )


# ---------------------------------------------------------------------------
# Bullet regression: disabled flag → shell gates, prose judge skipped
# ---------------------------------------------------------------------------


class _BoomSDK:
    """An SDK sentinel that records whether it was ever consulted. If
    `verify_task` tried to judge a prose bullet, the judge would dispatch
    through this handle (the Claude adapter wraps the injected sdk) — so any
    attribute access would flip `.used`. With the judge disabled, the prose
    branch short-circuits BEFORE touching the sdk, leaving `.used` False."""

    def __init__(self):
        self.used = False

    def __getattr__(self, name):  # any access (incl. ClaudeAgentOptions / query)
        self.used = True
        raise AssertionError(
            f"verify judge disabled but the prose judge still touched the "
            f"SDK (attribute {name!r}) — the LLM judge must be skipped"
        )


def test_verify_task_skips_prose_judge_but_gates_on_shell_when_disabled(
    monkeypatch, tmp_path,
):
    """With `AP2_VERIFY_JUDGE_DISABLED=1`, `verify_task` still runs — the
    deterministic shell bullet executes and gates (pass), while the prose
    bullet is recorded `unverified` WITHOUT invoking the LLM judge. Aggregates
    to `partial` (soft pass)."""
    monkeypatch.setenv("AP2_VERIFY_JUDGE_DISABLED", "1")

    briefing = (
        "## Verification\n"
        "- `true` — a deterministic shell bullet that passes\n"
        "- Prose: this acceptance bullet needs the LLM judge\n"
    )
    sdk = _BoomSDK()
    verdict = asyncio.run(verify.verify_task(
        briefing_text=briefing,
        project_root=tmp_path,
        sdk=sdk,
        task_id="TB-386",
    ))

    assert verdict is not None
    assert not sdk.used, "the prose judge must not touch the SDK when disabled"

    by_kind = {c.kind: c for c in verdict.criteria}
    assert by_kind["shell"].status == "pass"
    assert by_kind["prose"].status == "unverified"
    assert "AP2_VERIFY_JUDGE_DISABLED" in by_kind["prose"].notes
    # shell pass + prose unverified → partial (soft pass), NOT a silent full
    # pass that bypasses the shell gate.
    assert verdict.overall == "partial"


def test_verify_task_shell_gate_still_fails_when_judge_disabled(
    monkeypatch, tmp_path,
):
    """Disabling the prose judge must NOT weaken the shell gate: a failing
    shell bullet still flips the overall verdict to `fail` even though the
    prose bullet is skipped."""
    monkeypatch.setenv("AP2_VERIFY_JUDGE_DISABLED", "1")

    briefing = (
        "## Verification\n"
        "- `false` — a deterministic shell bullet that FAILS\n"
        "- Prose: this acceptance bullet would need the LLM judge\n"
    )
    verdict = asyncio.run(verify.verify_task(
        briefing_text=briefing,
        project_root=tmp_path,
        sdk=_BoomSDK(),
        task_id="TB-386",
    ))

    assert verdict is not None
    by_kind = {c.kind: c for c in verdict.criteria}
    assert by_kind["shell"].status == "fail"
    assert by_kind["prose"].status == "unverified"
    assert verdict.overall == "fail"
