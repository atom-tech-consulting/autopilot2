"""TB-269: Calibrate `AP2_VALIDATOR_JUDGE_TIMEOUT_S` default (15 → 60),
emit `validator_judge_passed` event, and add the axis-1 mirror doctor
audit `validator_judge_timeout_audit`.

The TB-257 investigation artifact
(`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md`)
measured the real `_judge_dep_coherence_default` SDK call at
17.6-46.8s wall-clock against a 15s default + 5s outer-thread grace
(`worker.join(timeout=timeout_s + 5)`) — a 20s ceiling that sat below
the median completion of even the smallest measured briefing. 15/15
recent operator queue-appends timed out; the load-bearing
"upstream gates already make this safe in practice" floor named at
goal.md L82-85 was silently fail-open for 7+ days.

TB-269 ships the calibration follow-up the artifact called out as
deferred, paired with a TB-252-shape preventive doctor surface so the
same calibration-drift class can't silently re-degrade after a future
workload shift. This module pins all three scope items:

  (a) `_VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT == 60.0` (constant pin —
      a future re-bump trips this gate and forces the doctor band
      docs to update in lockstep).

  (b) `validator_judge_passed` event-emission shape — the SDK call's
      success path emits a `validator_judge_passed` event carrying
      the full payload schema (`duration_s`, `briefing_bytes`,
      `max_turns`, `timeout_s`) before the JSON parse step.

  (c) `validator_judge_timeout_audit` verdict bands (insufficient
      samples → INFO, timeout below typical → WARN with one-line
      fix, comfortable → INFO) — synthesizes
      `validator_judge_passed` rows in a temp `events.jsonl` and
      asserts the audit's band-switching matches the TB-252 mirror.

The doctor verdict bands here are byte-for-byte mirrors of the TB-252
`verify_timeout_audit` bands the briefing's `## Design` calls out as
the canonical template.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import types
from pathlib import Path

import pytest

from ap2 import doctor as doctor_mod
from ap2 import events as events_mod
# TB-386: the dep-coherence judge surface was demoted out of
# `ap2/components/validator_judge/` back into the core briefing-validation
# runner (`ap2/briefing_validators.py`). The alias name (`vj`) stays the
# same so the rest of this module's bodies keep reading byte-identically.
from ap2 import briefing_validators as vj
from ap2.config import Config, DEFAULT_VERIFY_TIMEOUT_S, EVENTS_FILE
from ap2.doctor import (
    _VALIDATOR_JUDGE_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES,
    validator_judge_timeout_audit,
)


# ---------------------------------------------------------------------------
# Scope §6(a) — default constant pin
# ---------------------------------------------------------------------------


def test_validator_judge_timeout_s_default_is_60():
    """TB-269 bumped the default from 15.0 → 60.0. The bump is the
    headline of the briefing; pin the constant so a future tweak that
    moves it back without going through the doctor surface trips
    immediately.

    60s sits 1.5× the TB-257 artifact's measured worst case (~47s,
    rounded up) — same `_VERIFY_TIMEOUT_AUDIT_FIX_MULT=1.5` ratio the
    TB-252 doctor audit recommends. Operators can still tighten via
    the env knob; the default now sits above the real-world ceiling
    instead of below the median.
    """
    assert vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT == 60.0, (
        "TB-269: AP2_VALIDATOR_JUDGE_TIMEOUT_S default must be 60.0 "
        "(bumped from 15.0 per the TB-257 measurement artifact). Got: "
        f"{vj._VALIDATOR_JUDGE_TIMEOUT_S_DEFAULT}"
    )


# ---------------------------------------------------------------------------
# Scope §6(b) — `validator_judge_passed` event-emission shape
# ---------------------------------------------------------------------------


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, response_text: str):
    """Install a fake `claude_agent_sdk` module that returns
    `response_text` from a single async iteration of `sdk.query(...)`.

    `_judge_dep_coherence_default` lazy-imports `claude_agent_sdk`
    inside the function body, so monkeypatching `sys.modules` BEFORE
    the call is sufficient to intercept the SDK round-trip without
    real network. The fake produces one message whose `.content` is a
    list of one part with a `.text` attribute — matches the shape the
    real function iterates through to extract the last assistant text.
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
        # Single-message stream; mirrors how the real SDK yields one
        # final assistant turn for this short prompt.
        yield _Msg([_Part(response_text)])

    fake_module = types.ModuleType("claude_agent_sdk")
    fake_module.ClaudeAgentOptions = _Options
    fake_module.query = _query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_module)


def test_validator_judge_passed_event_emission_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The SDK success path emits a `validator_judge_passed` event
    carrying the briefing-named payload schema:
    `{type, ts, duration_s, briefing_bytes, max_turns, timeout_s}`.

    Triggers the real `_judge_dep_coherence_default` with a fake
    `claude_agent_sdk` module so the worker-thread + asyncio.wait_for
    + timing-measurement code paths fire end-to-end. Asserts the
    payload's keys + value shapes; absolute `duration_s` is not pinned
    (it's wall-clock and varies) but the field must be present and
    numeric.
    """
    response_text = (
        '{"hard_predecessors": [], "reasoning": "no hard deps"}'
    )
    _install_fake_sdk(monkeypatch, response_text)

    events_file = tmp_path / "events.jsonl"
    briefing_text = "## Goal\n\nDo a thing.\n\n## Scope\n\nA single bullet.\n"
    outcome = vj._judge_dep_coherence_default(
        briefing_text=briefing_text,
        description="add a foo helper",
        blocked_tokens=[],
        timeout_s=5.0,
        max_turns=2,
        events_file=events_file,
    )
    # Sanity: the SDK call resolved cleanly and the response parsed.
    assert outcome.data == {"hard_predecessors": [], "reasoning": "no hard deps"}
    assert outcome.parse_error is None

    # Exactly one `validator_judge_passed` event landed.
    evts = events_mod.tail(events_file, 50)
    passed = [e for e in evts if e.get("type") == "validator_judge_passed"]
    assert len(passed) == 1, (
        f"expected exactly one validator_judge_passed event; got {passed!r}"
    )
    evt = passed[0]

    # Required payload keys per the briefing §Scope (2):
    # `{type, ts, duration_s, briefing_bytes, max_turns, timeout_s}`.
    for required in ("type", "ts", "duration_s", "briefing_bytes",
                     "max_turns", "timeout_s"):
        assert required in evt, (
            f"validator_judge_passed event missing required key "
            f"{required!r}; got {sorted(evt)}"
        )

    # Type-shape pins.
    assert evt["type"] == "validator_judge_passed"
    assert isinstance(evt["duration_s"], (int, float))
    assert evt["duration_s"] >= 0.0
    assert evt["briefing_bytes"] == len(briefing_text.encode("utf-8"))
    assert evt["max_turns"] == 2
    assert evt["timeout_s"] == 5.0


def test_validator_judge_passed_emitted_even_when_parse_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A parse-failure call still spent the same wall-clock against
    the SDK — that cost matters for sizing the timeout knob. So the
    `validator_judge_passed` event must fire BEFORE the JSON parse,
    regardless of whether the response parses cleanly.

    Without this contract, a project whose model regression returned
    `[1, 2, 3]` on every call would see zero `validator_judge_passed`
    samples and `validator_judge_timeout_audit` would degrade to its
    "insufficient data" INFO branch — masking exactly the workload
    shape that's most likely to drift the audit needs to catch.
    """
    # Non-dict JSON → parse failure path (`parse_error=non_dict`).
    _install_fake_sdk(monkeypatch, "[1, 2, 3]")

    events_file = tmp_path / "events.jsonl"
    outcome = vj._judge_dep_coherence_default(
        briefing_text="brief",
        description="d",
        blocked_tokens=[],
        timeout_s=5.0,
        max_turns=2,
        events_file=events_file,
    )
    # Parse failure path.
    assert outcome.data is None
    assert outcome.parse_error == "non_dict"

    # `validator_judge_passed` still fired — the SDK round-trip
    # succeeded, only the parse failed.
    evts = events_mod.tail(events_file, 50)
    passed = [e for e in evts if e.get("type") == "validator_judge_passed"]
    assert len(passed) == 1, (
        "validator_judge_passed must fire BEFORE the JSON parse so "
        "parse-failure calls still contribute their wall-clock to the "
        "doctor's timeout-audit sample window."
    )


# ---------------------------------------------------------------------------
# Scope §6(c) — `validator_judge_timeout_audit` verdict bands
# ---------------------------------------------------------------------------


def _make_cfg(project_root: Path) -> Config:
    """Construct a minimal Config without sourcing project env.

    Tests synthesize their own events.jsonl under `project_root /
    .cc-autopilot/`. `verify_timeout_s` is irrelevant to this audit
    (which reads `AP2_VALIDATOR_JUDGE_TIMEOUT_S` directly from
    `os.environ`) but Config requires the field — pin to the default.
    Mirrors `test_doctor_verify_timeout.py::_make_cfg`.
    """
    events_file = project_root / EVENTS_FILE
    events_file.parent.mkdir(parents=True, exist_ok=True)
    return Config(
        project_root=project_root,
        tasks_file=project_root / "TASKS.md",
        progress_file=project_root / ".cc-autopilot" / "progress.md",
        tasks_dir=project_root / ".cc-autopilot" / "tasks",
        events_file=events_file,
        cron_file=project_root / ".cc-autopilot" / "cron.yaml",
        pid_file=project_root / ".cc-autopilot" / "daemon.pid",
        pause_flag=project_root / ".cc-autopilot" / "paused",
        cron_state_file=project_root / ".cc-autopilot" / "cron_state.json",
        mm_state_file=project_root / ".cc-autopilot" / "mm_state.json",
        retry_state_file=project_root / ".cc-autopilot" / "retry_state.json",
        auto_diagnose_state_file=(
            project_root / ".cc-autopilot" / "auto_diagnose_state.json"
        ),
        daemon_state_file=(
            project_root / ".cc-autopilot" / "daemon_state.json"
        ),
        # TB-379: daemon effective-config snapshot path.
        effective_config_file=(
            project_root / ".cc-autopilot" / "effective_config.json"
        ),
        env_file=project_root / ".cc-autopilot" / "env",
        next_task_id=1,
        # TB-280: project-identity headline prefix for status-report.
        project_name=project_root.name,
        tick_interval_s=30,
        mm_tick_interval_s=10,
        event_context_size=50,
        task_timeout_s=1200,
        control_timeout_s=300,
        max_retries=3,
        verify_cmd="uv run pytest -q",
        verify_timeout_s=DEFAULT_VERIFY_TIMEOUT_S,
        auto_diagnose_idle_threshold_s=10800,
        auto_diagnose_cooldown_s=21600,
    )


def _seed_validator_judge_passed_events(
    events_file: Path,
    *,
    durations: list[float],
    base_ts: _dt.datetime | None = None,
    step: _dt.timedelta = _dt.timedelta(hours=1),
) -> None:
    """Synthesize `validator_judge_passed` rows in events.jsonl with the
    given `durations`. Timestamps walk forward from `base_ts` (default:
    1 day before now, so seeded events fit inside the audit's 7-day
    window) at `step` intervals.
    """
    base_ts = base_ts or (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
    )
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a") as f:
        for i, dur in enumerate(durations):
            ts = (base_ts + step * i).strftime("%Y-%m-%dT%H:%M:%SZ")
            evt = {
                "ts": ts,
                "type": "validator_judge_passed",
                "duration_s": float(dur),
                "briefing_bytes": 5000,
                "max_turns": 2,
                "timeout_s": 60.0,
            }
            f.write(json.dumps(evt) + "\n")


def _levels(res) -> list[str]:
    return [lvl for lvl, _ in res.messages]


def test_validator_judge_timeout_audit_info_when_insufficient_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Fresh installs with <3 samples → INFO ("insufficient data"),
    NOT a WARN. A single anomalous slow call shouldn't push an
    operator to retune the env. Mirrors TB-252's matching band on
    `verify_timeout_audit`.
    """
    monkeypatch.delenv("AP2_VALIDATOR_JUDGE_TIMEOUT_S", raising=False)
    cfg = _make_cfg(tmp_path)
    _seed_validator_judge_passed_events(
        cfg.events_file, durations=[80.0, 90.0],  # 2 samples — below floor
    )

    res = validator_judge_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert "WARN" not in levels, levels
    assert levels == ["INFO"], levels
    info_msg = next(t for lvl, t in res.messages if lvl == "INFO")
    assert "insufficient data" in info_msg
    assert "AP2_VALIDATOR_JUDGE_TIMEOUT_S" in info_msg
    assert (
        f">={_VALIDATOR_JUDGE_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES}"
        in info_msg
    )
    assert res.ok


def test_validator_judge_timeout_audit_warns_when_timeout_below_typical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The headline TB-269 case mirrored from TB-257's measurement.
    Synthesized 5 successful-judge samples of 90s wall-clock vs. a
    60s configured timeout means the next call has zero headroom over
    the worst-case observed run. Audit must surface a WARN with the
    one-line fix-shape recommendation (recommend bumping to
    `ceil(typical * 1.5)`).
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_TIMEOUT_S", "60")
    cfg = _make_cfg(tmp_path)
    _seed_validator_judge_passed_events(cfg.events_file, durations=[90.0] * 5)

    res = validator_judge_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert levels.count("WARN") == 1, levels
    warn_msg = next(t for lvl, t in res.messages if lvl == "WARN")
    # WARN body names the misconfiguration shape + the fix env knob.
    assert "AP2_VALIDATOR_JUDGE_TIMEOUT_S=60s" in warn_msg
    assert "recommend `export AP2_VALIDATOR_JUDGE_TIMEOUT_S=" in warn_msg
    assert "below observed-typical" in warn_msg
    # 90s * 1.5 = 135s → recommendation should be at least 135.
    assert "135" in warn_msg, warn_msg
    # n / sample-days attribution is in the body.
    assert "n=5" in warn_msg
    # WARN doesn't FAIL the report (operator authority preserved per
    # goal.md L184-186).
    assert res.ok


def test_validator_judge_timeout_audit_info_when_comfortable_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Production-healthy case: 60s timeout, 20s typical worst-case
    successful run → 3× headroom. Audit emits INFO confirming the
    configuration is comfortable and no nudge is needed.
    """
    monkeypatch.setenv("AP2_VALIDATOR_JUDGE_TIMEOUT_S", "60")
    cfg = _make_cfg(tmp_path)
    _seed_validator_judge_passed_events(
        cfg.events_file, durations=[18.0, 20.0, 19.5, 17.0, 19.0],
    )

    res = validator_judge_timeout_audit(tmp_path, cfg)

    levels = _levels(res)
    assert "WARN" not in levels, levels
    assert levels == ["INFO"], levels
    info_msg = next(t for lvl, t in res.messages if lvl == "INFO")
    assert "comfortable headroom" in info_msg
    assert "AP2_VALIDATOR_JUDGE_TIMEOUT_S=60s" in info_msg
    # observed-typical = max(durations) = 20.
    assert "20" in info_msg
    assert res.ok


# ---------------------------------------------------------------------------
# Bonus integration pin: `diagnose()` wires the new audit immediately
# after `verify_timeout_audit` (briefing §Scope 3, "directly after the
# existing verify_timeout_audit call").
# ---------------------------------------------------------------------------


def test_diagnose_wires_validator_judge_timeout_audit_after_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """The new "validator-judge timeout headroom" section sits
    immediately after the existing "verify timeout headroom" section
    so the operator sees the axis-1 + axis-2 timeout-fit surfaces as
    a paired block in `ap2 doctor` output.
    """
    # Stub out sandbox-user probes so we don't depend on the real
    # machine.
    monkeypatch.setattr(doctor_mod, "_user_exists", lambda u: False)
    monkeypatch.setattr(
        doctor_mod, "_sandbox_clone_path", lambda root, user: None
    )
    (tmp_path / "CLAUDE.md").write_text("## Autopilot\n- Next task ID: TB-1\n")
    (tmp_path / "TASKS.md").write_text("# Tasks\n")
    (tmp_path / ".cc-autopilot").mkdir()
    (tmp_path / ".cc-autopilot" / "progress.md").write_text("# Progress\n")
    (tmp_path / ".cc-autopilot" / "tasks").mkdir()

    cfg = _make_cfg(tmp_path)
    report = doctor_mod.diagnose(tmp_path, user="ghost", cfg=cfg)
    titles = [t for t, _ in report.sections]
    assert "validator-judge timeout headroom" in titles, titles
    vt_idx = titles.index("verify timeout headroom")
    vj_idx = titles.index("validator-judge timeout headroom")
    assert vj_idx == vt_idx + 1, (
        f"validator-judge timeout-headroom section must sit directly "
        f"after verify-timeout section; got titles={titles}"
    )
