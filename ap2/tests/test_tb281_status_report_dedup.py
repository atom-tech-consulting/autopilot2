"""TB-281: content-fingerprint dedup gate for the status-report cron.

Pre-TB-281 the skip-gate (`_status_report_should_skip`) only suppressed
fully-idle windows — windows where zero "interesting" events landed
since the previous `cron_complete name=status-report`. A window with
even one `ideation_skipped reason=focus_exhausted` event (NOT in the
boring-types denylist) bypassed the gate, the agent ran, and the post
landed — but its STRUCTURAL CONTENT (board counts, pending-review
TB-Ns, decisions-needed bullets, digest sub-sections, halt reason)
was byte-for-byte identical to the previous post. Three consecutive
low-delta posts trained the operator to ignore the channel, defeating
the monitoring half of the walk-away promise the goal.md focus
`operator-legible reporting and monitoring` is built around.

This module pins the following arcs:

  (1) Fingerprint stability: two equivalent snapshots produce the same
      hash; the headline timestamp delta does NOT bust the hash.
  (2) Axis-by-axis sensitivity: changing board counts → different
      hash; changing decisions-needed → different hash; changing
      halt reason → different hash; an appearing/disappearing digest
      sub-section → different hash.
  (3) Skip-gate fires `duplicate_content` when the prospective
      fingerprint matches the stashed one + emits the new
      `cron_skipped reason=duplicate_content` event.
  (4) Skip-gate returns False on the first-ever run (no stored
      fingerprint).
  (5) Post-success path stashes the fingerprint via
      `mark_run_with_payload` under the sibling key
      `status-report.last_post_fingerprint`.
"""
from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path

import pytest

from ap2 import events
from ap2.config import Config
from ap2.cron import load_state, mark_run_with_payload
from ap2.init import init_project
from ap2.status_report import (
    _LAST_POST_FINGERPRINT_FIELD,
    _compose_status_report_snapshot,
    _load_last_post_fingerprint,
    _status_report_skip_decision,
    _status_report_should_skip,
    compute_status_report_fingerprint,
    run_status_report,
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Fresh project with the boilerplate snapshot composer needs.

    Each test gets a fresh `tmp_path` so cron_state.json / events.jsonl
    start clean. We also drop MM env vars so the target-channel branch
    doesn't surface an extra state_extras line that would couple the
    fingerprint to the test process's environment.
    """
    monkeypatch.delenv("AP2_MM_REPORT_CHANNEL", raising=False)
    monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
    monkeypatch.delenv("AP2_AUTO_APPROVE", raising=False)
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


class _NoopSDK:
    """SDK stub: records `query` was called, returns an empty async gen.

    Mirrors the pattern used in `test_tb228_status_report_automation_digest`
    so the routine threads through `_run_control_agent` without spinning
    up real Claude wiring.
    """

    def __init__(self) -> None:
        self.called = False

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


# ===========================================================================
# Arc 1: fingerprint stability across equivalent snapshots.
# ===========================================================================


def test_fingerprint_stable_across_equivalent_snapshots(cfg: Config):
    """Two back-to-back snapshot computations against an unchanged
    events.jsonl + TASKS.md produce identical fingerprints. Pins the
    determinism contract — the dedup gate relies on
    `compute_status_report_fingerprint(snapshot_A) ==
    compute_status_report_fingerprint(snapshot_B)` when A and B are
    structurally identical.

    Also pins the headline-timestamp exclusion: the fingerprint must
    NOT bake the wall-clock into the hash, otherwise back-to-back
    runs in the same second pass dedup but runs across a second
    boundary fail it (defeating the gate's purpose).
    """
    # Seed a prior report so the snapshot has a stable since-idx anchor.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    fp_a = compute_status_report_fingerprint(cfg)
    fp_b = compute_status_report_fingerprint(cfg)
    assert fp_a == fp_b, (
        f"identical-state fingerprints must match; got {fp_a!r} vs {fp_b!r}"
    )
    # Sanity-check the truncated-hex shape: 12 hex chars per the
    # docstring contract.
    assert isinstance(fp_a, str) and len(fp_a) == 12
    assert all(c in "0123456789abcdef" for c in fp_a)


def test_fingerprint_stable_with_fabricated_snapshot(cfg: Config):
    """`compute_status_report_fingerprint(cfg, snapshot=<fabricated>)`
    is deterministic when callers supply the snapshot directly. Pins
    the test-injection path the rest of this module uses to drive
    axis-by-axis sensitivity tests without spinning up real events
    files."""
    snapshot = {
        "pending_review_ids": ["TB-1", "TB-2"],
        "decisions_needed": ["foo"],
        "digest_sections": {"## A": "alpha"},
        "halt_reason": "",
    }
    fp_a = compute_status_report_fingerprint(cfg, snapshot=snapshot)
    fp_b = compute_status_report_fingerprint(cfg, snapshot=snapshot)
    assert fp_a == fp_b


# ===========================================================================
# Arc 2: fingerprint sensitivity to each input axis.
# ===========================================================================


def _baseline_snapshot() -> dict:
    """Return a structurally-complete baseline snapshot.

    Tests mutate one axis at a time and assert that the fingerprint
    changes — pinning sensitivity to that axis.
    """
    return {
        "pending_review_ids": ["TB-100"],
        "decisions_needed": ["focus rotation call"],
        "digest_sections": {
            "## Recent task activity": (
                "- **TB-1** — Add foo helper: complete (abc1234)"
            ),
            "## Automation loop activity": (
                "auto-approve: healthy; auto-unfreeze: healthy"
            ),
        },
        "halt_reason": "",
    }


def _fp(cfg: Config, snapshot: dict) -> str:
    """Compute fingerprint with a passed-in snapshot so the axis under
    test is the snapshot fields, not the on-disk board.

    The cfg fixture lends its tmp-path board (init_project's empty
    TASKS.md → zero counts for all six sections) so board counts
    contribute the same baseline to every comparison in a test —
    only the mutated snapshot axis differs.
    """
    return compute_status_report_fingerprint(cfg, snapshot=snapshot)


def test_fingerprint_sensitive_to_board_counts(cfg: Config):
    """Changing the board's per-section counts (e.g. a Backlog task
    becomes Complete) produces a different fingerprint. The cron's
    primary signal is board-state movement — a hash that ignored
    section counts would be useless."""
    # Snapshot is empty digest_sections so only board counts contribute.
    snapshot = {
        "pending_review_ids": [],
        "decisions_needed": [],
        "digest_sections": {},
        "halt_reason": "",
    }
    # Baseline TASKS.md (init_project's empty board).
    fp_baseline = compute_status_report_fingerprint(cfg, snapshot=snapshot)
    # Move a task in by editing TASKS.md directly so the board has a
    # different per-section count on the next computation.
    tasks_text = cfg.tasks_file.read_text()
    cfg.tasks_file.write_text(
        tasks_text.replace(
            "## Backlog\n\n",
            "## Backlog\n\n- [ ] TB-1 some task @blocked:review\n\n",
        )
    )
    fp_changed = compute_status_report_fingerprint(cfg, snapshot=snapshot)
    assert fp_baseline != fp_changed, (
        f"board-count delta must bust the fingerprint; "
        f"baseline={fp_baseline!r} changed={fp_changed!r}"
    )


def test_fingerprint_sensitive_to_pending_review_ids(cfg: Config):
    """A new pending-review TB-N landing in the snapshot produces a
    different fingerprint. The operator's "Pending operator review
    (N): TB-..." bullet is THE call-to-action surface the dedup gate
    must NOT mask."""
    baseline = _baseline_snapshot()
    fp_baseline = _fp(cfg, baseline)
    extended = dict(baseline)
    extended["pending_review_ids"] = list(baseline["pending_review_ids"]) + [
        "TB-200"
    ]
    fp_extended = _fp(cfg, extended)
    assert fp_baseline != fp_extended


def test_fingerprint_sensitive_to_decisions_needed(cfg: Config):
    """A new decisions-needed bullet text produces a different
    fingerprint. The ideator's `## Decisions needed from operator`
    section is the explicit operator-judgement surface the dedup gate
    must NOT mask."""
    baseline = _baseline_snapshot()
    fp_baseline = _fp(cfg, baseline)
    extended = dict(baseline)
    extended["decisions_needed"] = list(baseline["decisions_needed"]) + [
        "new residual-risk decision"
    ]
    fp_extended = _fp(cfg, extended)
    assert fp_baseline != fp_extended


def test_fingerprint_sensitive_to_halt_reason(cfg: Config):
    """A new auto-approve halt reason produces a different fingerprint.
    A halt landing in the window is by definition a content delta the
    operator needs to see — even if every other axis is stable."""
    baseline = _baseline_snapshot()
    fp_baseline = _fp(cfg, baseline)
    halted = dict(baseline)
    halted["halt_reason"] = "window_token_cap_exceeded"
    fp_halted = _fp(cfg, halted)
    assert fp_baseline != fp_halted


def test_fingerprint_sensitive_to_appearing_digest_subsection(cfg: Config):
    """A new digest sub-section appearing in the snapshot (e.g.
    `## Focus rotation activity` after a `focus_advanced` event)
    produces a different fingerprint — even if every other axis is
    unchanged. Pins the axis-by-axis closure goal.md focus-1 calls
    for: every axis whose content the operator would see in the post
    must contribute to the dedup hash."""
    baseline = _baseline_snapshot()
    fp_baseline = _fp(cfg, baseline)
    with_focus = dict(baseline)
    with_focus["digest_sections"] = {
        **baseline["digest_sections"],
        "## Focus rotation activity": (
            "- focus_advanced: alpha → beta (2 of 3)"
        ),
    }
    fp_with_focus = _fp(cfg, with_focus)
    assert fp_baseline != fp_with_focus


def test_fingerprint_sensitive_to_digest_subsection_content_change(cfg: Config):
    """Changing the CONTENT of an existing digest sub-section (e.g.
    a new `task_complete` bullet appearing in `## Recent task
    activity`) produces a different fingerprint. Pins that the
    fingerprint hashes the rendered content, not just the heading
    presence."""
    baseline = _baseline_snapshot()
    fp_baseline = _fp(cfg, baseline)
    edited = dict(baseline)
    edited["digest_sections"] = {
        **baseline["digest_sections"],
        "## Recent task activity": (
            "- **TB-1** — Add foo helper: complete (abc1234)\n"
            "- **TB-2** — Fix bar bug: complete (def5678)"
        ),
    }
    fp_edited = _fp(cfg, edited)
    assert fp_baseline != fp_edited


# ===========================================================================
# Arc 3: skip-gate fires `duplicate_content` when fingerprints match.
# ===========================================================================


def test_skip_decision_returns_duplicate_content_when_fingerprint_matches(
    cfg: Config,
):
    """The fingerprint gate fires when the prospective post would
    match the previously-stashed fingerprint. Stage a previous post
    by computing the snapshot's fingerprint and writing it to
    cron_state.json directly, then assert
    `_status_report_skip_decision` returns
    `(True, "duplicate_content")` — even though an interesting event
    landed (so the idle gate would let the post through)."""
    # Seed: prior report landed, then a single ideation_skipped event
    # (not boring, but doesn't move any structural axis — exactly the
    # pattern the briefing's "Recent events" tail shows for TB-280's
    # back-to-back near-identical posts).
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "ideation_skipped",
        reason="focus_exhausted", focus_count=1,
    )

    # Compute the prospective fingerprint and stash it as if the
    # previous post produced exactly this content.
    prospective_fp = compute_status_report_fingerprint(cfg)
    mark_run_with_payload(
        cfg.cron_state_file, "status-report",
        payload={_LAST_POST_FINGERPRINT_FIELD: prospective_fp},
    )

    should_skip, reason = _status_report_skip_decision(cfg)
    assert should_skip is True
    assert reason == "duplicate_content", (
        f"expected duplicate_content reason; got {reason!r}"
    )
    # Backward-compat: `_status_report_should_skip` returns True too.
    assert _status_report_should_skip(cfg) is True


def test_run_status_report_emits_cron_skipped_duplicate_content(
    cfg: Config, monkeypatch,
):
    """End-to-end: when the prospective fingerprint matches the
    stashed one, `run_status_report` emits `cron_skipped
    reason=duplicate_content` (no SDK call) and the operator can
    audit suppressions via `ap2 logs` / `/events`.

    Pin against a refactor that silently widened the existing
    `no_activity_since_last_report` reason instead of registering
    `duplicate_content` as a distinct event-vocabulary member.
    """
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "ideation_skipped",
        reason="focus_exhausted", focus_count=1,
    )

    prospective_fp = compute_status_report_fingerprint(cfg)
    mark_run_with_payload(
        cfg.cron_state_file, "status-report",
        payload={_LAST_POST_FINGERPRINT_FIELD: prospective_fp},
    )

    sdk = _NoopSDK()
    result = asyncio.run(
        run_status_report(cfg, sdk, mcp_server=None, trigger="cron"),
    )
    assert result.skipped is True
    assert result.reason == "duplicate_content"
    assert sdk.called is False, "dedup gate must NOT burn an SDK turn"

    # The `cron_skipped reason=duplicate_content` event landed for
    # operator audit.
    tail = events.tail(cfg.events_file, 50)
    skipped = [e for e in tail if e.get("type") == "cron_skipped"]
    assert any(
        e.get("reason") == "duplicate_content" for e in skipped
    ), (
        f"expected at least one cron_skipped with reason=duplicate_content; "
        f"got {[e.get('reason') for e in skipped]!r}"
    )


# ===========================================================================
# Arc 4: skip-gate returns False on first-ever run (no stored fingerprint).
# ===========================================================================


def test_skip_decision_first_run_no_stored_fingerprint(cfg: Config):
    """When `cron_state.json` carries NO
    `status-report.last_post_fingerprint` (first-ever run, or the
    state file was reset by `ap2 rollback`), the fingerprint gate
    must NOT fire. The routine then runs to completion and the
    post-success path stashes the first fingerprint, arming dedup
    for subsequent ticks."""
    # Seed an interesting event so the idle gate doesn't pre-empt.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )
    # No fingerprint stashed yet.
    assert _load_last_post_fingerprint(cfg) == ""

    should_skip, reason = _status_report_skip_decision(cfg)
    assert should_skip is False
    assert reason is None


def test_skip_decision_idle_gate_wins_when_window_empty(cfg: Config):
    """When the inter-report window has ZERO interesting events, the
    cheap idle gate fires first with `no_activity_since_last_report`
    — even if a stale fingerprint sits in cron_state.json. Pins the
    two-tier ordering: idle is the cheap-first fast path."""
    events.append(cfg.events_file, "cron_complete", job="status-report")
    # Stash a stale fingerprint that would never match anything.
    mark_run_with_payload(
        cfg.cron_state_file, "status-report",
        payload={_LAST_POST_FINGERPRINT_FIELD: "ffffffffffff"},
    )

    should_skip, reason = _status_report_skip_decision(cfg)
    assert should_skip is True
    assert reason == "no_activity_since_last_report"


# ===========================================================================
# Arc 5: post-success path stashes the fingerprint under cron_state.json.
# ===========================================================================


def test_run_status_report_stashes_fingerprint_on_cron_success(
    cfg: Config, monkeypatch,
):
    """The cron-trigger post-success path stashes the rendered post's
    fingerprint under `status-report.last_post_fingerprint` via
    `mark_run_with_payload`. Pin the wiring so a refactor that drops
    the call site (or routes through plain `mark_run` instead) trips
    here — without the stash, the next tick's dedup gate has nothing
    to compare against.

    The expected fingerprint is computed BEFORE calling
    `run_status_report` so the snapshot's `since_idx` window matches
    what the routine saw at prompt-build time. After the routine
    runs, it emits its own `cron_start` / `cron_complete` events
    which shift the window — a post-run recompute would mismatch
    even on a correct stash.
    """
    # Seed an interesting event so the routine reaches the
    # SDK / mark_run_with_payload path. Use ideation_skipped (NOT a
    # task_complete) so the recent-task-activity digest stays empty
    # — task_complete events trigger Board.find lookups that can
    # surface transient state we don't want to couple this test to.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "ideation_skipped",
        reason="focus_exhausted", focus_count=1,
    )

    # Pre-compute the expected fingerprint against the SAME snapshot
    # the routine will see at prompt-build time. The routine's
    # `_compose_status_report_snapshot` call is deterministic given
    # the events tail at that moment.
    expected = compute_status_report_fingerprint(cfg)

    # Stub the prompt builder so the test doesn't pay the cost of
    # `build_control_prompt` (which threads through Bash for git
    # log). The routine's mark_run_with_payload call site runs
    # regardless.
    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="cron"))

    state = load_state(cfg.cron_state_file)
    key = f"status-report.{_LAST_POST_FINGERPRINT_FIELD}"
    assert key in state, (
        f"expected `{key}` in cron_state.json; got keys "
        f"{sorted(state.keys())!r}"
    )
    stashed = state[key]
    assert stashed == expected, (
        f"stashed fingerprint {stashed!r} must match pre-computed "
        f"{expected!r}"
    )
    # And the helper that the next tick's skip-gate uses to read the
    # value back returns the same string.
    assert _load_last_post_fingerprint(cfg) == expected


def test_run_status_report_chat_trigger_does_not_stash_fingerprint(
    cfg: Config, monkeypatch,
):
    """Chat-trigger posts do NOT advance `cron_state[status-report]`
    (TB-144 — an operator-triggered report at 11:00 must not silence
    the noon cron). TB-281 keeps the same contract for the
    fingerprint sidecar: chat-trigger paths don't update the stashed
    fingerprint either, so the cron's dedup gate compares against
    the last CRON-triggered post.

    This is the deliberate trade-off documented in
    `_status_report_skip_decision`'s docstring — a chat-triggered
    near-duplicate landing between cron ticks is acceptable; missing
    a legitimate scheduled post because a chat post moved the
    fingerprint forward is not.
    """
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "task_complete",
        task="TB-1", status="complete", commit="abc1234",
    )

    monkeypatch.setattr(
        "ap2.prompts.build_control_prompt",
        lambda cfg, name, body, **_kw: "stub prompt",
    )

    sdk = _NoopSDK()
    asyncio.run(run_status_report(cfg, sdk, mcp_server=None, trigger="chat"))

    state = load_state(cfg.cron_state_file)
    key = f"status-report.{_LAST_POST_FINGERPRINT_FIELD}"
    assert key not in state, (
        f"chat-trigger paths must not stash the fingerprint; "
        f"unexpectedly found {key!r} in state"
    )


# ===========================================================================
# Snapshot composition: state_extras + digest_sections are coherent.
# ===========================================================================


def test_compose_snapshot_returns_state_extras_and_digest_sections(
    cfg: Config,
):
    """The snapshot composer returns both the `state_extras` markdown
    list (used to build the prompt) AND the `digest_sections` dict
    (used to hash). Pins the coherence contract: every digest section
    that lands in `digest_sections` also appears in `state_extras`,
    so the agent sees what the fingerprint hashed.
    """
    # Seed a focus_advanced event so the TB-244 focus-rotation
    # sub-section lands in both surfaces.
    events.append(cfg.events_file, "cron_complete", job="status-report")
    events.append(
        cfg.events_file, "focus_advanced",
        **{
            "from": "alpha", "to": "beta",
            "trigger": "done_when_judge",
            "new_index": 1, "total_foci": 3,
        },
    )

    snapshot = _compose_status_report_snapshot(cfg)
    assert "state_extras" in snapshot
    assert "digest_sections" in snapshot

    joined_extras = "\n".join(snapshot["state_extras"])
    for heading, content in snapshot["digest_sections"].items():
        assert heading in joined_extras, (
            f"digest heading {heading!r} hashed by fingerprint must "
            f"also appear in state_extras (agent must see it); "
            f"extras={snapshot['state_extras']!r}"
        )
        # And the content body must appear too — partial match against
        # the first non-empty line is enough to pin the wiring.
        first_content_line = next(
            (ln for ln in content.splitlines() if ln.strip()),
            "",
        )
        assert first_content_line in joined_extras


# ===========================================================================
# Source-level pin: the new event-reason member is documented.
# ===========================================================================


def test_status_report_module_carries_duplicate_content_reason():
    """Briefing verifier: `grep -q "duplicate_content" ap2/status_report.py`
    must match. The reason literal lives at the call site so a
    refactor that renames it (or routes the dedup branch through a
    different reason) trips here AND fails the briefing's grep.
    """
    src = Path(__file__).resolve().parent.parent / "status_report.py"
    text = src.read_text()
    assert "duplicate_content" in text


def test_events_module_documents_duplicate_content_reason():
    """Briefing verifier: `grep -q "duplicate_content" ap2/events.py`
    must match. The event-reason vocabulary is documented in the
    module docstring so `test_every_event_type_documented` (and human
    readers) can audit the canonical reason set without grepping for
    call sites."""
    src = Path(__file__).resolve().parent.parent / "events.py"
    text = src.read_text()
    assert "duplicate_content" in text
