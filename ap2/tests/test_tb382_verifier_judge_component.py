"""TB-382: extract the prose-judge into a `verifier_judge` component (axis 5).

Pins the structural cleavage the focus scopes (goal.md axis 5): the
optional LLM prose-bullet judge (`_judge_prose_bullet`) moves out of the
core verify runner into `ap2/components/verifier_judge/` and is reached
via the registry, while the deterministic shell-bullet execution path,
`## Verification` parsing, and verdict aggregation stay in **core**
(verification is gating).

The load-bearing regression (briefing Verification bullet 6): with the
`verifier_judge` env flag disabled, `verify_task` still runs and gates on
shell bullets while SKIPPING the LLM prose judge — so a deployment can
verify with shell bullets alone, prose-judge disabled.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ap2 import verify
from ap2.registry import _reset_default_registry, default_registry


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reset the cached `default_registry()` per test so the env-flag
    polarity reads cleanly and a stale cache from another file doesn't
    leak through."""
    _reset_default_registry()
    yield
    _reset_default_registry()


# ---------------------------------------------------------------------------
# Manifest / registry surface
# ---------------------------------------------------------------------------


def test_manifest_registers_prose_judge_hook_and_env_flag():
    """The `verifier_judge` manifest declares the suppress-style
    `AP2_VERIFY_JUDGE_DISABLED` env_flag (default-on, mirroring
    `validator_judge`) and a callable `prose_judge` hook point."""
    manifest = default_registry().get("verifier_judge")
    assert manifest.env_flag == "AP2_VERIFY_JUDGE_DISABLED", manifest
    assert manifest.default_enabled is True, manifest
    hook = manifest.hook_points.get("prose_judge")
    assert callable(hook), manifest.hook_points


def test_registry_verifier_judge_returns_hook_when_enabled(monkeypatch):
    """With the env flag unset, `Registry.verifier_judge(cfg)` returns the
    component's prose-judge callable — the EXACT object bound on the
    manifest's `prose_judge` hook point."""
    monkeypatch.delenv("AP2_VERIFY_JUDGE_DISABLED", raising=False)
    _reset_default_registry()
    reg = default_registry()
    hook = reg.verifier_judge(None)
    assert hook is not None
    assert hook is reg.get("verifier_judge").hook_points["prose_judge"]


def test_registry_verifier_judge_returns_none_when_disabled(monkeypatch):
    """A truthy `AP2_VERIFY_JUDGE_DISABLED` drops the component from the
    enabled walk, so `Registry.verifier_judge(cfg)` returns None — the
    signal `verify_task` uses to fall through to the non-judged path."""
    monkeypatch.setenv("AP2_VERIFY_JUDGE_DISABLED", "1")
    _reset_default_registry()
    assert default_registry().verifier_judge(None) is None


# ---------------------------------------------------------------------------
# Back-compat: `verify._judge_prose_bullet` resolves through the registry
# ---------------------------------------------------------------------------


def test_verify_judge_prose_bullet_shim_resolves_via_registry():
    """`verify._judge_prose_bullet` is no longer defined in `ap2/verify.py`
    (it moved to the component); the module `__getattr__` shim resolves it
    through the registry to the relocated function. Both attribute access
    and `from ap2.verify import _judge_prose_bullet` keep working — the
    real-SDK smoke harness and the legacy unit tests rely on this."""
    from ap2.verify import _judge_prose_bullet as via_import

    via_attr = verify._judge_prose_bullet
    expected = default_registry().get("verifier_judge").hook_points["prose_judge"]
    assert via_attr is expected
    assert via_import is expected


# ---------------------------------------------------------------------------
# Bullet 6 regression: disabled flag → shell gates, prose judge skipped
# ---------------------------------------------------------------------------


class _BoomSDK:
    """An SDK sentinel that records whether it was ever consulted. If
    `verify_task` tried to judge a prose bullet, the judge would dispatch
    through this handle (the Claude adapter wraps the injected sdk) — so a
    `query` call would flip `.used`. With the component disabled, the
    prose branch short-circuits BEFORE touching the sdk, leaving `.used`
    False."""

    def __init__(self):
        self.used = False

    def __getattr__(self, name):  # any access (incl. ClaudeAgentOptions / query)
        self.used = True
        raise AssertionError(
            f"verifier_judge disabled but the prose judge still touched the "
            f"SDK (attribute {name!r}) — the LLM judge must be skipped"
        )


def test_verify_task_skips_prose_judge_but_gates_on_shell_when_disabled(
    monkeypatch, tmp_path,
):
    """Briefing Verification bullet 6: with `AP2_VERIFY_JUDGE_DISABLED=1`,
    `verify_task` still runs — the deterministic shell bullet executes and
    gates (pass), while the prose bullet is recorded `unverified` WITHOUT
    invoking the LLM judge. Aggregates to `partial` (soft pass)."""
    monkeypatch.setenv("AP2_VERIFY_JUDGE_DISABLED", "1")
    _reset_default_registry()

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
        task_id="TB-382",
    ))

    assert verdict is not None
    assert not sdk.used, "the prose judge must not touch the SDK when disabled"

    by_kind = {c.kind: c for c in verdict.criteria}
    assert by_kind["shell"].status == "pass"
    assert by_kind["prose"].status == "unverified"
    assert "verifier_judge component disabled" in by_kind["prose"].notes
    # shell pass + prose unverified → partial (soft pass), NOT a silent
    # full pass that bypasses the shell gate.
    assert verdict.overall == "partial"


def test_verify_task_shell_gate_still_fails_when_judge_disabled(
    monkeypatch, tmp_path,
):
    """Disabling the prose judge must NOT weaken the shell gate: a failing
    shell bullet still flips the overall verdict to `fail` even though the
    prose bullet is skipped."""
    monkeypatch.setenv("AP2_VERIFY_JUDGE_DISABLED", "1")
    _reset_default_registry()

    briefing = (
        "## Verification\n"
        "- `false` — a deterministic shell bullet that FAILS\n"
        "- Prose: this acceptance bullet would need the LLM judge\n"
    )
    verdict = asyncio.run(verify.verify_task(
        briefing_text=briefing,
        project_root=tmp_path,
        sdk=_BoomSDK(),
        task_id="TB-382",
    ))

    assert verdict is not None
    by_kind = {c.kind: c for c in verdict.criteria}
    assert by_kind["shell"].status == "fail"
    assert by_kind["prose"].status == "unverified"
    assert verdict.overall == "fail"
