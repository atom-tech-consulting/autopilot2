"""TB-270: Slim validator-judge user payload to Goal+Scope sections only.

The TB-257 investigation artifact
(`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
categorized `prompt-too-heavy` as the secondary axis-1 factor in the
dep-coherence judge's wall-clock budget: even the smallest measured
briefing (4621 B) took ~22s avg, and typical operator queue-append
briefings are ≥6 KB. The full briefing markdown — Design /
Verification / Out-of-scope — was being shoved into the SDK call's
user payload, but hard-predecessor detection only needs the briefing's
intent surface (Goal + Scope). TB-270 ships the helper that slices the
briefing to those two sections and rewires `_judge_dep_coherence_default`
to consume the slice instead of the full text. This module pins the
four-bullet contract from the briefing's `## Scope` §5:

  (a) helper returns Goal+Scope substring on a canonical-shape briefing
      (Design / Verification / Out-of-scope dropped)
  (b) helper returns the full briefing on a briefing missing either
      heading (defensive fallback so the judge is never blind)
  (c) slicing preserves Goal-then-Scope ordering when source has them
      in that order (canonical order — never a Scope-then-Goal output)
  (d) integration: `_judge_dep_coherence_default`'s SDK prompt embeds
      the SLICED briefing_markdown (helper output), NOT the full
      `briefing_text` — verified by mocking `sdk.query`, capturing the
      prompt arg, parsing the JSON Input block, and asserting equality.

Combined with TB-269's timeout bump, the expected post-deployment
shape is: nominal calls finish ~15-25s (below the new 60s default),
giving the dep-coherence judge generous headroom even on edge-case
heavy briefings.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

# TB-316: the flat module `ap2/validator_judge.py` moved to
# `ap2/components/validator_judge/`. Tests are exempt from the TB-311
# import-direction gate; the alias name (`vj`) is preserved so the rest
# of this module's bodies keep reading byte-identically.
from ap2.components import validator_judge as vj


# A canonical-shaped briefing that covers all five sections the
# briefing-validator gates accept (TB-161 / TB-164: Goal + Why-now;
# TB-235 dep-coherence runs against this shape). Used by §(a) and §(c).
CANONICAL_BRIEFING = """## Goal

Do something for TB-foo so the bar surface gains a baz field.

Why now: the upstream gates need it.

## Scope

1. Add the foo helper to ap2/foo.py.
2. Wire it into the bar dispatcher.

## Design

Internal notes about how to wire it — the foo helper returns a
NamedTuple and the bar dispatcher unpacks it positionally.

## Verification

- `uv run pytest -q ap2/tests/test_foo.py` — passes.
- `grep -nE "def foo_helper" ap2/foo.py` — exits 0.

## Out of scope

- Refactoring unrelated helpers.
- Generalizing the foo helper for other callers.
"""


# ---------------------------------------------------------------------------
# Scope §5(a) — canonical-shape slicing
# ---------------------------------------------------------------------------


def test_helper_returns_goal_plus_scope_only_on_canonical():
    """Canonical briefing → slice covers `## Goal` and `## Scope` only;
    Design / Verification / Out-of-scope dropped.

    This is the steady-state contract: every briefing authored through
    `ap2 add` flows through this path, and the bytes that survive the
    slice are the only bytes the dep-coherence judge actually needs to
    ground its hard-predecessor verdict. Bytes that DON'T survive must
    not be material the judge would have used.
    """
    sliced = vj._slice_briefing_for_dep_judge(CANONICAL_BRIEFING)

    # Kept: Goal + Scope bodies (the intent surface).
    assert "## Goal" in sliced
    assert "Do something for TB-foo" in sliced
    assert "Why now: the upstream gates need it." in sliced
    assert "## Scope" in sliced
    assert "Add the foo helper to ap2/foo.py." in sliced
    assert "Wire it into the bar dispatcher." in sliced

    # Dropped: Design / Verification / Out-of-scope (the judge wouldn't
    # have used these to change its verdict).
    assert "## Design" not in sliced
    assert "Internal notes about how to wire it" not in sliced
    assert "## Verification" not in sliced
    assert "uv run pytest" not in sliced
    assert "## Out of scope" not in sliced
    assert "Refactoring unrelated helpers" not in sliced

    # Slice is strictly smaller than the source (sanity — if the
    # implementation regressed to returning the full text, this assert
    # catches it independently of the keyword checks above).
    assert len(sliced) < len(CANONICAL_BRIEFING), (
        f"slice should be strictly smaller than source; got "
        f"slice={len(sliced)} source={len(CANONICAL_BRIEFING)}"
    )


# ---------------------------------------------------------------------------
# Scope §5(b) — defensive fallback when canonical heading is missing
# ---------------------------------------------------------------------------


def test_helper_fallback_when_goal_heading_missing():
    """Missing `## Goal` heading → return the full briefing unchanged.

    The dep-coherence judge must never be blind. A briefing that
    bypasses the queue-time validator (legacy / hand-edited) still
    needs SOME prose to ground the verdict; the fallback gives it the
    whole text rather than an empty slice.
    """
    no_goal = (
        "## Scope\n\nA single bullet.\n\n"
        "## Design\n\nInternal notes.\n"
    )
    assert vj._slice_briefing_for_dep_judge(no_goal) == no_goal


def test_helper_fallback_when_scope_heading_missing():
    """Missing `## Scope` heading → return the full briefing unchanged.

    Same defensive posture as the missing-Goal branch: better to feed
    the judge a heavier payload than to silently strip the briefing to
    nothing.
    """
    no_scope = (
        "## Goal\n\nDo a thing.\n\nWhy now: the focus needs it.\n\n"
        "## Design\n\nInternal notes.\n"
    )
    assert vj._slice_briefing_for_dep_judge(no_scope) == no_scope


def test_helper_fallback_when_both_headings_missing():
    """Neither canonical heading → return the full briefing unchanged.

    Covers the worst-case legacy briefing: a wall of prose with no
    section structure at all. The judge still gets the bytes; the
    operator's `ap2 status` shows a higher token count than the
    sliced steady-state but the gate keeps firing.
    """
    legacy = (
        "Some legacy unstructured prose.\n\n"
        "Predates the canonical heading shape; mention TB-foo "
        "as a hard dep.\n"
    )
    assert vj._slice_briefing_for_dep_judge(legacy) == legacy


def test_helper_fallback_when_both_headings_present_but_empty():
    """Both headings present but bodies empty → return the full briefing.

    A stub briefing with `## Goal\\n\\n## Scope\\n\\n## Design\\n...`
    would otherwise yield a slice of just the two heading lines and
    nothing else; the judge has nothing to verdict on. The fallback
    bumps it to the full text so at least the surrounding sections
    (Design / Verification) give the judge SOMETHING to read.
    """
    stub = "## Goal\n\n## Scope\n\n## Design\n\nNotes only down here.\n"
    assert vj._slice_briefing_for_dep_judge(stub) == stub


# ---------------------------------------------------------------------------
# Scope §5(c) — Goal-then-Scope ordering preserved
# ---------------------------------------------------------------------------


def test_helper_preserves_goal_then_scope_ordering():
    """Source order is Goal-then-Scope on canonical briefings; the
    slice MUST preserve that order so the judge reads the briefing's
    intent in the same sequence an operator would.

    Distinct from §(a) — that test asserts WHICH bytes survive the
    slice; this test asserts the ORDER those surviving bytes appear in.
    A regression that reversed the concatenation would fail here even
    if all the right keywords were present.
    """
    sliced = vj._slice_briefing_for_dep_judge(CANONICAL_BRIEFING)
    goal_idx = sliced.index("## Goal")
    scope_idx = sliced.index("## Scope")
    assert goal_idx < scope_idx, (
        f"## Goal must precede ## Scope in slice; got "
        f"goal_idx={goal_idx} scope_idx={scope_idx}"
    )


def test_helper_concatenates_when_sections_non_adjacent():
    """Unusual briefing wedges a non-canonical heading between Goal
    and Scope → slice concatenates the two sections in source order,
    dropping the intervening heading.

    This branch isn't expected on operator-curated briefings (the
    canonical shape puts Goal and Scope adjacent) but the helper's
    section-by-section design supports it, and the test pins the
    behavior so a future refactor that switches to a single-substring
    extraction has to reckon with the non-adjacent case.
    """
    odd = (
        "## Goal\n\nDo a thing.\n\n"
        "## Background\n\nHistorical context here.\n\n"
        "## Scope\n\nA bullet.\n\n"
        "## Design\n\nNotes.\n"
    )
    sliced = vj._slice_briefing_for_dep_judge(odd)
    # Goal + Scope present.
    assert "Do a thing." in sliced
    assert "A bullet." in sliced
    # Intervening Background AND trailing Design dropped.
    assert "## Background" not in sliced
    assert "Historical context" not in sliced
    assert "## Design" not in sliced
    assert "Notes." not in sliced
    # Order preserved.
    assert sliced.index("## Goal") < sliced.index("## Scope")


# ---------------------------------------------------------------------------
# Scope §5(d) — integration pin: SDK prompt embeds the SLICED briefing
# ---------------------------------------------------------------------------


def _install_fake_sdk_capture_prompt(
    monkeypatch: pytest.MonkeyPatch,
    response_text: str,
    captured: list,
):
    """Install a fake `claude_agent_sdk` module that records the
    `prompt` kwarg on every `sdk.query(...)` call into `captured` and
    returns `response_text` as a single async message.

    Mirrors the fake-SDK shape used by
    `test_tb269_validator_judge_timeout_calibration._install_fake_sdk`
    so the integration paths exercised here are byte-compatible with
    the existing TB-269 pins (no test interaction surprises). The
    `captured` list is the only new affordance — it lets the §(d)
    integration assertion inspect the prompt without an SDK call.
    """
    class _Part:
        def __init__(self, text: str):
            self.text = text

    class _Msg:
        def __init__(self, parts):
            self.content = parts

    class _Options:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def _query(*, prompt: str, options):  # noqa: ANN001
        captured.append(prompt)
        yield _Msg([_Part(response_text)])

    fake_module = types.ModuleType("claude_agent_sdk")
    fake_module.ClaudeAgentOptions = _Options
    fake_module.query = _query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_module)


def _extract_user_payload(prompt: str) -> dict:
    """Pull the JSON Input block out of `_judge_dep_coherence_default`'s
    SDK prompt string.

    The prompt's tail is:
        Input:\n```json\n{...json...}\n```
    so we anchor on the trailing ```json\\n marker, slice the JSON
    body, and parse it. Returned dict is the `user_payload` the call
    site assembled — the §(d) assertion target.
    """
    # The last (and only) ```json marker in the prompt is the JSON
    # block opener. Anchoring on the LAST occurrence avoids false hits
    # if the system prompt ever inlines a `json` literal in prose.
    marker = "```json\n"
    start = prompt.rfind(marker)
    assert start >= 0, f"no ```json marker in prompt; got: {prompt!r}"
    body_start = start + len(marker)
    end = prompt.find("\n```", body_start)
    assert end >= 0, f"no closing ``` in prompt; got: {prompt!r}"
    payload_json = prompt[body_start:end]
    return json.loads(payload_json)


def test_judge_dep_coherence_default_embeds_sliced_briefing_in_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The headline integration pin from briefing §Scope §5(d):
    `_judge_dep_coherence_default`'s SDK prompt's `briefing_markdown`
    field equals `_slice_briefing_for_dep_judge(briefing_text)`, NOT
    the raw `briefing_text`.

    Verifies the wiring end-to-end: a regression that wired the
    helper into a different field, applied it to the wrong input, or
    forgot to call it at all would fail this assertion. The full
    `briefing_text` length vs. the sliced length comparison is the
    sanity that the helper actually narrowed the input (a no-op
    helper that returned the input unchanged would also pass the
    equality assert but fail this size check).
    """
    captured: list[str] = []
    _install_fake_sdk_capture_prompt(
        monkeypatch,
        '{"hard_predecessors": [], "reasoning": "no hard deps"}',
        captured,
    )

    events_file = tmp_path / "events.jsonl"
    expected_slice = vj._slice_briefing_for_dep_judge(CANONICAL_BRIEFING)
    # Sanity: the helper produces a strictly smaller payload on this
    # canonical input. If the source-of-truth equality assert below
    # passed against a no-op helper, this guards against that regression.
    assert expected_slice != CANONICAL_BRIEFING
    assert len(expected_slice) < len(CANONICAL_BRIEFING)

    vj._judge_dep_coherence_default(
        briefing_text=CANONICAL_BRIEFING,
        description="implement TB-foo per the briefing",
        blocked_tokens=["TB-bar"],
        timeout_s=5.0,
        max_turns=2,
        events_file=events_file,
    )

    assert len(captured) == 1, f"expected one SDK call, got {len(captured)}"
    payload = _extract_user_payload(captured[0])

    # The headline pin: briefing_markdown is the SLICED text.
    assert payload["briefing_markdown"] == expected_slice, (
        "TB-270: user_payload['briefing_markdown'] must equal "
        "_slice_briefing_for_dep_judge(briefing_text), not the raw "
        "briefing. The slicing wasn't wired into the SDK call site."
    )
    # The other two fields are UNCHANGED by TB-270 (Out-of-scope §2).
    assert payload["task_description"] == "implement TB-foo per the briefing"
    assert payload["blocked_codespan_tokens"] == ["TB-bar"]


def test_judge_dep_coherence_default_falls_back_to_full_text_on_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Sibling of the §(d) pin: when the briefing lacks the canonical
    heading shape, the helper's fallback fires and the SDK call sees
    the FULL briefing_text. End-to-end pin that the fallback branch
    flows through the same call site (not just exercised in the unit
    test above).

    Without this pin, a regression that wired the helper into the call
    site but bypassed the fallback (e.g. by using a different
    function) would fall through unnoticed — the judge would receive
    an empty slice on legacy briefings and the operator would see a
    silent rise in `validator_judge_fail` rate.
    """
    captured: list[str] = []
    _install_fake_sdk_capture_prompt(
        monkeypatch,
        '{"hard_predecessors": [], "reasoning": "no hard deps"}',
        captured,
    )

    legacy_briefing = (
        "Some legacy unstructured prose with no canonical headings.\n\n"
        "TB-foo is mentioned only as historical context.\n"
    )
    # Sanity: helper returns the full text on this shape.
    assert vj._slice_briefing_for_dep_judge(legacy_briefing) == legacy_briefing

    events_file = tmp_path / "events.jsonl"
    vj._judge_dep_coherence_default(
        briefing_text=legacy_briefing,
        description="legacy task",
        blocked_tokens=[],
        timeout_s=5.0,
        max_turns=2,
        events_file=events_file,
    )

    payload = _extract_user_payload(captured[0])
    # Fallback path → SDK sees the full legacy text.
    assert payload["briefing_markdown"] == legacy_briefing
