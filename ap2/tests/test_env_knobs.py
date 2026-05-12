"""TB-205: happy + error path coverage for four SDK-cost-shaping env knobs.

The knobs `AP2_EVENT_CONTEXT`, `AP2_CONTROL_MAX_TURNS`,
`AP2_IDEATION_MAX_TURNS`, and `AP2_AGENT_MODEL` each affect either token
cost or agent behavior on every control / task / MM-handler SDK call. Prior
to TB-205 none of them had any test-file reference under `ap2/tests/` —
the parse/default/override contract was unpinned, so a future refactor
could silently flip a default, drop the env read, or invert the precedence
chain without any test signal.

Each knob gets a focused happy + error block here:

  1. AP2_EVENT_CONTEXT        — `Config.event_context_size` parse + the
                                end-to-end `_events_block` tail-window the
                                value controls.
  2. AP2_CONTROL_MAX_TURNS    — generic per-control-agent `max_turns` cap;
                                exercised through `handle_message` (the
                                MM handler call site, daemon.py:742).
  3. AP2_IDEATION_MAX_TURNS   — per-ideation override of the generic cap;
                                pin precedence over `AP2_CONTROL_MAX_TURNS`
                                (ideation-specific knob wins on the
                                ideation path, generic knob wins on the
                                MM-handler path).
  4. AP2_AGENT_MODEL          — `ClaudeAgentOptions.model`; pin default,
                                override, and the current empty-string
                                behavior (env-set-to-"" propagates through
                                because `os.environ.get` only returns the
                                default when the key is ABSENT).

Sibling reference patterns:
  - `AP2_AGENT_EFFORT` coverage in `test_status_report_skip.py` (default /
    per-site override / precedence / source-grep) and
    `test_verify_retry_diff.py` (judge effort default / precedence).
  - `AP2_VERIFY_TIMEOUT_S` coverage in `e2e/test_verify.py`.

The shape mirrors those — `monkeypatch.setenv` / `delenv` for env
manipulation, `_OptionsCapturingSDK` to assert against the SDK boundary,
and a stub `_run_control_agent` for the ideation path so the test
doesn't depend on the full `_run_ideation` SDK plumbing.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from ap2 import events, ideation, prompts
from ap2.config import DEFAULT_EVENT_CONTEXT_SIZE, Config
from ap2.cron import save_state


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    """Minimal Config with the required board sections present so a fresh
    `Board.load` doesn't trip on missing headings."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


class _OptionsCapturingSDK:
    """SDK stub that captures the kwargs handed to `ClaudeAgentOptions` so
    tests can assert on `max_turns` / `model` / `extra_args` etc. Mirrors
    the same-named helper in `test_status_report_skip.py` (TB-156); a
    second copy here keeps the env-knob tests self-contained and avoids
    a cross-file private-helper import dependency."""

    def __init__(self) -> None:
        self.options_kw: dict | None = None
        self.called = False
        outer = self

        class _OptionsBound:
            def __init__(self, **kw):
                outer.options_kw = kw

        # Bind a per-instance options class so each SDK stub keeps its own
        # captured kwargs.
        self.ClaudeAgentOptions = _OptionsBound  # noqa: N803

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


# ===========================================================================
# (1) AP2_EVENT_CONTEXT — count of recent events inlined into agent prompts.
#
# Read site: `ap2/config.py` line 102-104:
#     event_context_size=int(
#         os.environ.get("AP2_EVENT_CONTEXT", DEFAULT_EVENT_CONTEXT_SIZE)
#     )
# DEFAULT_EVENT_CONTEXT_SIZE = 50. Bare `int(...)` with no fallback, so
# invalid (non-int) values raise ValueError at `Config.load` time. Consumer:
# `prompts._events_block` passes `cfg.event_context_size` as `n=` to
# `events.tail`, controlling how many events get inlined into the rendered
# `## Recent events` block.
# ===========================================================================


def test_event_context_default_is_fifty_when_env_unset(tmp_path, monkeypatch):
    """Happy path: env unset → `cfg.event_context_size` matches the module
    default. A regression that flips the default silently (e.g. someone
    edits `DEFAULT_EVENT_CONTEXT_SIZE`) trips this test."""
    monkeypatch.delenv("AP2_EVENT_CONTEXT", raising=False)
    cfg = _cfg(tmp_path)
    assert cfg.event_context_size == DEFAULT_EVENT_CONTEXT_SIZE == 50


def test_event_context_env_override_flows_through_to_config(tmp_path, monkeypatch):
    """Happy path: `AP2_EVENT_CONTEXT="10"` → `cfg.event_context_size == 10`.
    Pins that the env read isn't dropped silently in a future refactor of
    `Config.load`."""
    monkeypatch.setenv("AP2_EVENT_CONTEXT", "10")
    cfg = _cfg(tmp_path)
    assert cfg.event_context_size == 10


def test_event_context_invalid_value_raises(tmp_path, monkeypatch):
    """Error path: non-int env value raises ValueError at `Config.load`
    time (bare `int(...)` with no try/except). Pins CURRENT behavior —
    if a future refactor switches to a permissive `_int_env`-style helper
    with default-fallback, that's a deliberate change visible here. Same
    shape applies to negative ints (`int("-3") == -3` parses fine, so no
    raise) — only non-int strings trip the parse."""
    monkeypatch.setenv("AP2_EVENT_CONTEXT", "abc")
    with pytest.raises(ValueError):
        _cfg(tmp_path)


def test_event_context_end_to_end_controls_events_block_size(tmp_path, monkeypatch):
    """End-to-end: the env knob's downstream consumer is
    `prompts._events_block`, which tails `cfg.event_context_size` events
    from `cfg.events_file`. With the knob set to 3 and 10 events seeded,
    the rendered block contains only the 3 most recent. Catches a
    refactor that swaps the tail size for a hardcoded constant."""
    monkeypatch.setenv("AP2_EVENT_CONTEXT", "3")
    cfg = _cfg(tmp_path)
    for i in range(10):
        events.append(cfg.events_file, "marker", n=i)

    block = prompts._events_block(cfg)

    # The tail size cap is 3 — only n=7/8/9 should appear, n=0..6 must not.
    assert "n=9" in block
    assert "n=8" in block
    assert "n=7" in block
    assert "n=6" not in block
    assert "n=0" not in block


# ===========================================================================
# (2) AP2_CONTROL_MAX_TURNS — generic per-control-agent max_turns cap.
#
# Read site: `ap2/daemon.py` line 742 (`handle_message` for the MM handler).
# Also used as the precedence baseline for `AP2_IDEATION_MAX_TURNS`. Bare
# `int(...)` with no fallback; default 15.
# ===========================================================================


def _drive_handle_message(cfg, monkeypatch):
    """Drive `daemon.handle_message` once with the SDK captured and return
    the captured `ClaudeAgentOptions` kwargs. Stubs the prompt builder to
    avoid heavy markdown rendering — only the SDK-options dict matters
    for the env-knob assertion."""
    from ap2 import daemon

    monkeypatch.setattr(
        "ap2.prompts.build_mattermost_prompt",
        lambda cfg, msg: "stub mattermost prompt",
    )
    sdk = _OptionsCapturingSDK()
    msg = {
        "id": "post-1",
        "channel_id": "ch-abc",
        "channel_name": "dev",
        "user": "alice",
        "text": "@claude-bot hi",
        "thread_id": "",
    }
    asyncio.run(daemon.handle_message(cfg, sdk, mcp_server=None, msg=msg))
    assert sdk.called, "handle_message did not invoke the SDK"
    assert sdk.options_kw is not None
    return sdk.options_kw


def test_control_max_turns_default_is_fifteen_in_mm_handler(tmp_path, monkeypatch):
    """Happy path: env unset → MM-handler SDK call sees `max_turns=15`. A
    bump of the in-source default trips this test."""
    monkeypatch.delenv("AP2_CONTROL_MAX_TURNS", raising=False)
    cfg = _cfg(tmp_path)
    opts = _drive_handle_message(cfg, monkeypatch)
    assert opts["max_turns"] == 15


def test_control_max_turns_env_override_flows_through_to_sdk(tmp_path, monkeypatch):
    """Happy path: `AP2_CONTROL_MAX_TURNS="30"` → MM-handler SDK call sees
    `max_turns=30`. Pins the env read in `handle_message` so a refactor
    that drops it surfaces."""
    monkeypatch.setenv("AP2_CONTROL_MAX_TURNS", "30")
    cfg = _cfg(tmp_path)
    opts = _drive_handle_message(cfg, monkeypatch)
    assert opts["max_turns"] == 30


def test_control_max_turns_invalid_value_raises(tmp_path, monkeypatch):
    """Error path: non-int env value raises ValueError on the bare
    `int(...)` parse. Pins CURRENT behavior; a future refactor to a
    permissive helper would be a deliberate change visible here."""
    monkeypatch.setenv("AP2_CONTROL_MAX_TURNS", "abc")
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        _drive_handle_message(cfg, monkeypatch)


# ===========================================================================
# (3) AP2_IDEATION_MAX_TURNS — ideation-specific override of the generic
# AP2_CONTROL_MAX_TURNS cap.
#
# Read site: `ap2/ideation.py` line 510 (`_run_ideation`). Bare `int(...)`
# with no fallback; default `IDEATION_MAX_TURNS_DEFAULT = 30` (bumped from
# the legacy cron-default 15 because the assessment + failure-review flow
# routinely needs ~10-15 turns).
#
# Precedence (mirrors the AP2_VERIFY_JUDGE_EFFORT vs AP2_AGENT_EFFORT shape
# in test_verify_retry_diff.py): the per-site env knob wins on the
# ideation path; the generic AP2_CONTROL_MAX_TURNS wins on the
# MM-handler path. With both set, each call site reads its own.
# ===========================================================================


def _stub_run_control_agent_capturing_max_turns(monkeypatch):
    """Replace `daemon._run_control_agent` with a stub that records the
    `max_turns` kwarg per call. Returns the per-call list so a test can
    assert on `calls[0]["max_turns"]`. Mirrors the
    `_stub_run_control_agent` pattern in `test_ideation_trigger.py`."""
    calls: list[dict] = []

    async def fake(cfg, sdk, mcp_server, *,
                   label, prompt, allowed_tools, max_turns, effort=None):
        calls.append({
            "label": label,
            "max_turns": max_turns,
            "effort": effort,
        })
        return (False, None, "", Path("/tmp/fake-prompt-dump"))

    def fake_snapshot(cfg):
        return {}

    def fake_changed(pre, post):
        return []

    def fake_commit(*args, **kwargs):
        pass

    from ap2 import daemon as _daemon
    monkeypatch.setattr(_daemon, "_run_control_agent", fake)
    monkeypatch.setattr(_daemon, "_snapshot_state_paths", fake_snapshot)
    monkeypatch.setattr(_daemon, "_changed_state_paths", fake_changed)
    monkeypatch.setattr(_daemon, "_commit_state_files", fake_commit)
    return calls


def _drive_force_ideate(tmp_path, monkeypatch, calls):
    """Drive `ideation.force_ideate` once with the control-agent stub.
    `force_ideate` bypasses the disable / cooldown / queue-depth /
    focus-exhausted gates, so it's the cleanest way to land in
    `_run_ideation` with one SDK invocation."""
    cfg = _cfg(tmp_path)
    # Project ideation prompt override — keeps the prompt small and avoids
    # depending on the default prompt's content.
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("Test ideation prompt body.\n")
    # Cooldown set in the past — force_ideate ignores it anyway but
    # mark_run still runs after.
    save_state(cfg.cron_state_file, {"ideation": time.time() - 10000})
    asyncio.run(ideation.force_ideate(cfg, sdk=None, mcp_server=None))
    assert calls, "force_ideate did not reach _run_control_agent"
    return calls[0]


def test_ideation_max_turns_default_is_thirty_when_env_unset(tmp_path, monkeypatch):
    """Happy path: env unset → `_run_ideation` passes `max_turns=30` (the
    `IDEATION_MAX_TURNS_DEFAULT` constant) to `_run_control_agent`. A
    regression that drops the env read OR flips the constant trips
    this test."""
    monkeypatch.delenv("AP2_IDEATION_MAX_TURNS", raising=False)
    calls = _stub_run_control_agent_capturing_max_turns(monkeypatch)
    captured = _drive_force_ideate(tmp_path, monkeypatch, calls)
    assert captured["max_turns"] == ideation.IDEATION_MAX_TURNS_DEFAULT == 30


def test_ideation_max_turns_env_override_flows_through(tmp_path, monkeypatch):
    """Happy path: `AP2_IDEATION_MAX_TURNS="50"` → ideation SDK call sees
    `max_turns=50`. Pins the env read in `_run_ideation`."""
    monkeypatch.setenv("AP2_IDEATION_MAX_TURNS", "50")
    calls = _stub_run_control_agent_capturing_max_turns(monkeypatch)
    captured = _drive_force_ideate(tmp_path, monkeypatch, calls)
    assert captured["max_turns"] == 50


def test_ideation_max_turns_invalid_value_raises(tmp_path, monkeypatch):
    """Error path: non-int env value raises ValueError on the bare
    `int(...)` parse (current contract). The exception surfaces from the
    `_run_ideation` body before `_run_control_agent` is even called, so
    the stubbed control-agent receives no calls. Pinning CURRENT
    behavior — a refactor to the permissive `_cooldown_s`-style parse
    helper would be a deliberate change visible here."""
    monkeypatch.setenv("AP2_IDEATION_MAX_TURNS", "abc")
    calls = _stub_run_control_agent_capturing_max_turns(monkeypatch)
    cfg = _cfg(tmp_path)
    override = cfg.project_root / ".cc-autopilot" / "ideation_prompt.md"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("Test ideation prompt body.\n")
    save_state(cfg.cron_state_file, {"ideation": time.time() - 10000})
    with pytest.raises(ValueError):
        asyncio.run(ideation.force_ideate(cfg, sdk=None, mcp_server=None))
    assert calls == [], "stubbed control-agent must not have been reached"


def test_ideation_max_turns_takes_precedence_over_generic_on_ideation_path(
    tmp_path, monkeypatch,
):
    """Precedence: with BOTH `AP2_IDEATION_MAX_TURNS=42` and
    `AP2_CONTROL_MAX_TURNS=99` set, the ideation control-agent run reads
    from the per-site env (42), NOT the generic (99). Mirrors the
    `AP2_VERIFY_JUDGE_EFFORT` vs `AP2_AGENT_EFFORT` precedence pin in
    `test_verify_retry_diff.py`."""
    monkeypatch.setenv("AP2_IDEATION_MAX_TURNS", "42")
    monkeypatch.setenv("AP2_CONTROL_MAX_TURNS", "99")
    calls = _stub_run_control_agent_capturing_max_turns(monkeypatch)
    captured = _drive_force_ideate(tmp_path, monkeypatch, calls)
    assert captured["max_turns"] == 42, (
        "ideation must read its own AP2_IDEATION_MAX_TURNS, not the "
        "generic AP2_CONTROL_MAX_TURNS — TB-205 precedence regression"
    )


def test_control_max_turns_wins_on_mm_handler_path_when_both_set(
    tmp_path, monkeypatch,
):
    """Precedence (mirror image): with BOTH env knobs set, the MM-handler
    path keeps reading `AP2_CONTROL_MAX_TURNS=99` — the ideation-specific
    knob does NOT bleed into other control-agent call sites. Catches a
    refactor that swaps to a single generic knob and silently drops the
    per-site override semantics."""
    monkeypatch.setenv("AP2_IDEATION_MAX_TURNS", "42")
    monkeypatch.setenv("AP2_CONTROL_MAX_TURNS", "99")
    cfg = _cfg(tmp_path)
    opts = _drive_handle_message(cfg, monkeypatch)
    assert opts["max_turns"] == 99, (
        "MM handler must keep reading AP2_CONTROL_MAX_TURNS, not the "
        "ideation-specific AP2_IDEATION_MAX_TURNS — TB-205 precedence regression"
    )


# ===========================================================================
# (4) AP2_AGENT_MODEL — model passed to `ClaudeAgentOptions`.
#
# Read sites: `ap2/daemon.py` line 211 (run_task) + 870 (_run_control_agent),
# `ap2/verify.py` line 368 (_judge_prose_bullet), `ap2/janitor.py` line 726.
# Plain `os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7")` with no
# normalization — empty-string env DOES propagate (only an ABSENT key falls
# through to the default).
# ===========================================================================


def _run_control_agent_capturing_options(tmp_path, monkeypatch):
    """Drive `daemon._run_control_agent` once with `_OptionsCapturingSDK`
    and return the captured `ClaudeAgentOptions` kwargs. The model env
    knob is read inside `_run_control_agent` itself, so this hits the
    boundary directly."""
    from ap2 import daemon

    cfg = _cfg(tmp_path)
    sdk = _OptionsCapturingSDK()
    asyncio.run(daemon._run_control_agent(
        cfg, sdk, mcp_server=None,
        label="unit-test",
        prompt="hi",
        allowed_tools=[],
        max_turns=1,
    ))
    assert sdk.called, "_run_control_agent did not invoke the SDK"
    assert sdk.options_kw is not None
    return sdk.options_kw


def test_agent_model_default_is_claude_opus_4_7_when_env_unset(
    tmp_path, monkeypatch,
):
    """Happy path: env unset → SDK options carry the in-source default
    `claude-opus-4-7`. A regression that flips the default literal (e.g.
    a maintainer accidentally bumps it to `claude-sonnet-4-5` without
    a CHANGELOG entry) trips this test."""
    monkeypatch.delenv("AP2_AGENT_MODEL", raising=False)
    opts = _run_control_agent_capturing_options(tmp_path, monkeypatch)
    assert opts["model"] == "claude-opus-4-7"


def test_agent_model_env_override_flows_through_to_sdk(tmp_path, monkeypatch):
    """Happy path: `AP2_AGENT_MODEL="claude-haiku-4-5-20251001"` → SDK
    options carry that value. Pins that the env read isn't dropped or
    overridden in `_run_control_agent`."""
    monkeypatch.setenv("AP2_AGENT_MODEL", "claude-haiku-4-5-20251001")
    opts = _run_control_agent_capturing_options(tmp_path, monkeypatch)
    assert opts["model"] == "claude-haiku-4-5-20251001"


def test_agent_model_empty_string_propagates_through_no_silent_default(
    tmp_path, monkeypatch,
):
    """Pin CURRENT behavior on empty / whitespace-only env: `os.environ.get(
    key, default)` returns the literal value (`""` / `"   "`) when the key
    is set, NOT the default — the default kicks in only when the key is
    ABSENT. So `AP2_AGENT_MODEL=""` propagates the empty string straight
    to `ClaudeAgentOptions.model`.

    The test is pinning a contract, not endorsing it: if a future refactor
    adds normalization (e.g. `.strip() or default`), this test's
    expectation flips and that's a deliberate, visible change. The point
    is to make the surface testable so the contract is no longer
    silent."""
    monkeypatch.setenv("AP2_AGENT_MODEL", "")
    opts = _run_control_agent_capturing_options(tmp_path, monkeypatch)
    assert opts["model"] == "", (
        "current contract: empty-string env propagates through verbatim. "
        "If this test fails after a refactor that adds .strip()-or-default "
        "normalization, update the assertion (and the docstring) to "
        "match the new contract."
    )


def test_agent_model_env_read_in_task_agent_call_site(tmp_path, monkeypatch):
    """Source-level pin: the `run_task` SDK call site in
    `ap2/daemon.py` reads `AP2_AGENT_MODEL` with the same `claude-opus-4-7`
    default. Behavioral end-to-end coverage of the task-agent path would
    require the full `run_task` harness (briefing files, MCP server,
    state-fence machinery); a source-level grep on the function source
    catches a regression that drops the env read without forcing the
    heavyweight setup."""
    import inspect

    from ap2 import daemon

    src = inspect.getsource(daemon.run_task)
    assert 'os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7")' in src, (
        "regression: `run_task` no longer reads AP2_AGENT_MODEL or its "
        "default literal has drifted from `claude-opus-4-7`"
    )


def test_agent_model_env_read_in_verify_judge_call_site():
    """Source-level pin: the `_judge_prose_bullet` SDK call site in
    `ap2/verify.py` also reads `AP2_AGENT_MODEL`. Catches a refactor that
    forks the judge onto a different model knob without explicit operator
    opt-in."""
    import inspect

    from ap2 import verify

    src = inspect.getsource(verify._judge_prose_bullet)
    assert 'os.environ.get("AP2_AGENT_MODEL", "claude-opus-4-7")' in src, (
        "regression: `_judge_prose_bullet` no longer reads AP2_AGENT_MODEL "
        "or its default literal has drifted from `claude-opus-4-7`"
    )
