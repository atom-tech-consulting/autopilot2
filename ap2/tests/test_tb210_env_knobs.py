"""TB-210: happy + error path coverage for the four env knobs that landed
TB-208's coverage-debt comment block (`test_coverage_drift.py` L302-305 prior
to this change).

The knobs `AP2_TASK_MAX_TURNS`, `AP2_JANITOR_JUDGE_EFFORT`,
`AP2_JANITOR_JUDGE_MAX_TURNS`, and `AP2_MM_TEAM_ID` each affect agent /
janitor / sandbox-channel behavior at exactly one call site, but prior to
TB-210 had ZERO real test references — only the substring drift gate's
comment-block enumeration kept them green. A future refactor of
`daemon.run_task` (uses `AP2_TASK_MAX_TURNS`), `janitor._judge_finding`
(uses both `AP2_JANITOR_JUDGE_*` knobs), or `sandbox._install_channel_for_project`
(uses `AP2_MM_TEAM_ID`) could silently drop the env read, flip the default,
or invert precedence without any test signal.

This module mirrors TB-205's `test_env_knobs.py` 17-test layout shape
(default / override / invalid contract per knob, `monkeypatch.setenv` /
`delenv` for env scoping, references to the call-site module symbol
directly so a refactor surfaces here rather than at runtime).

  1. AP2_TASK_MAX_TURNS         — `daemon.run_task` task-agent `max_turns` cap.
                                  Bare `int(os.environ.get(..., DEFAULT_TASK_MAX_TURNS))`
                                  so the invalid-value contract raises
                                  ValueError (caught + wrapped by
                                  run_task's outer try/except as a
                                  `task_error` event). Per-call-site
                                  source pin proves the literal
                                  expression and its default;
                                  behavior tests re-evaluate the same
                                  expression against the scoped env.
                                  Default raised from 50 → 200 in TB-278
                                  (battle-tested defaults pass).
  2. AP2_JANITOR_JUDGE_EFFORT   — `janitor._judge_finding` judge SDK `effort`.
                                  `os.environ.get(..., os.environ.get(
                                  "AP2_AGENT_EFFORT", "high"))` — per-site
                                  knob takes precedence over the global
                                  AP2_AGENT_EFFORT (mirrors the
                                  AP2_VERIFY_JUDGE_EFFORT precedence shape
                                  in test_verify_retry_diff.py).
  3. AP2_JANITOR_JUDGE_MAX_TURNS — `janitor._judge_finding` judge SDK
                                  `max_turns`. Bare `int(...)` with default
                                  12; invalid-value path is swallowed by the
                                  judge's outer try/except (verdict drops to
                                  ambiguous, reasoning carries the trace).
  4. AP2_MM_TEAM_ID             — `sandbox._install_channel_for_project` →
                                  `sandbox.resolve_mm_channel`. Plain
                                  `os.environ.get(...)` falls back to None;
                                  None triggers `/users/me/teams` discovery
                                  (RuntimeError when empty, naming the env
                                  var); set value flows through to the
                                  channel-name resolution call.

Each test references the call-site module symbol directly (`daemon.run_task`,
`janitor._judge_finding`, `sandbox._install_channel_for_project` /
`sandbox.resolve_mm_channel`) so a substring-drift gate green is no longer
sufficient — the new tests assert on actual call-site behavior, not just
`os.environ.get` round-trips. Removing the four matching rows from
`test_coverage_drift.py`'s discovered-at-landing comment block (TB-208 L302-305)
is paired with this file landing — the comment-block shim was a "test
mention waiting to happen" entry, redundant once a real test references the
knob name.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from ap2 import daemon, sandbox
# TB-309: janitor moved to `ap2.components.janitor`; alias under the
# old name so the rest of the file keeps referencing `janitor.<sym>`
# unchanged.
from ap2.components import janitor
from ap2.config import DEFAULT_TASK_MAX_TURNS


# ---------------------------------------------------------------------------
# Shared SDK stubs (mirror the patterns in test_env_knobs.py / test_janitor.py)
# ---------------------------------------------------------------------------


class _OptionsCapturingSDK:
    """SDK stub that captures the kwargs handed to `ClaudeAgentOptions` so
    tests can assert on `max_turns` / `extra_args` etc. Mirrors the
    same-named helper in `test_env_knobs.py` (TB-205); a per-test copy
    here keeps TB-210 self-contained without a cross-file private-helper
    import dependency."""

    def __init__(self) -> None:
        self.options_kw: dict | None = None
        self.called = False
        outer = self

        class _OptionsBound:
            def __init__(self, **kw):
                outer.options_kw = kw

        self.ClaudeAgentOptions = _OptionsBound  # noqa: N803

    def query(self, *, prompt, options):  # noqa: ARG002
        self.called = True

        async def _gen():
            if False:
                yield None

        return _gen()


class _FakeMsg:
    """Minimal message shape `_judge_finding` understands (mirrors
    `ap2/tests/test_janitor._FakeMsg`)."""

    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


class _ScriptedJudgeSDK:
    """SDK stub whose `query()` yields one canned JSON verdict string and
    captures the `ClaudeAgentOptions` kwargs. Mirrors
    `ap2/tests/test_janitor._ScriptedJudgeSDK`; a sibling here keeps
    TB-210 independent."""

    def __init__(self, response: str = '{"verdict": "ambiguous", "reasoning": "ok"}') -> None:
        self._response = response
        self.calls = 0
        self.captured_options: list[dict] = []

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt, options) -> AsyncIterator[_FakeMsg]:  # noqa: ARG002
        self.calls += 1
        opts_kw = getattr(options, "kw", {}) or {}
        self.captured_options.append(opts_kw)
        response = self._response

        async def _gen() -> AsyncIterator[_FakeMsg]:
            yield _FakeMsg(response)

        return _gen()


# ---------------------------------------------------------------------------
# Janitor harness — minimal Config + JanitorFinding for direct
# `_judge_finding` invocation. The judge call site reads the env, builds
# `ClaudeAgentOptions`, then `await sdk.query(...)`. Driving it directly
# (no `run_janitor` wrapping) keeps the env-knob assertion close to the
# read site.
# ---------------------------------------------------------------------------


def _janitor_cfg(tmp_path: Path):
    """Build a minimal Config the janitor judge can run against. The
    `_judge_finding` flow only needs `cfg.events_file` (for the
    `judge_call` event emit) and `cfg.project_root` (for `cwd`); no git
    init required because we're not running detectors."""
    from ap2.config import Config

    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n## Active\n\n## Ready\n\n## Backlog\n\n"
        "## Pipeline Pending\n\n## Complete\n\n## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drive_judge_finding(tmp_path: Path, sdk: _ScriptedJudgeSDK) -> tuple[str, str]:
    """Invoke `janitor._judge_finding` directly with a minimal finding,
    returning the (verdict, reasoning) the judge produced. The shared
    context is built fresh per call (matches `run_janitor`'s shape)."""
    cfg = _janitor_cfg(tmp_path)
    finding = janitor.JanitorFinding(
        subkind="untracked_non_ignored",
        paths=["scratch.txt"],
        age_s=0,
        hint="hint",
    )
    shared_ctx = janitor._build_judge_shared_context(cfg)
    return asyncio.run(
        janitor._judge_finding(cfg, sdk, finding, shared_ctx),
    )


# ===========================================================================
# (1) AP2_TASK_MAX_TURNS — task-agent `max_turns` cap.
#
# Read site: `ap2/daemon.py` line 208 (`run_task` → `_consume()` →
# `sdk.ClaudeAgentOptions(max_turns=int(os.environ.get(...)))`). Bare
# `int(...)` with no fallback; default `DEFAULT_TASK_MAX_TURNS` (200 —
# raised from 50 in TB-278; see `config.py`).
#
# Driving `run_task` end-to-end requires git init + Board + MCP server +
# a heavy fixture. Following the same pattern as
# `test_agent_model_env_read_in_task_agent_call_site` (TB-205,
# `test_env_knobs.py` L451-467) for the run_task call site: the
# default-literal pin comes from `inspect.getsource(daemon.run_task)`,
# the override pin re-evaluates the literal expression against the scoped
# env (which IS what the call site does — same `int(os.environ.get(...))`
# code path), and the invalid pin uses `pytest.raises(ValueError)` on
# the same expression.
#
# This is "actual call-site behavior" — the source-grep ties the test to
# the production read site, and the env-scoped re-evaluation exercises
# the EXACT expression `run_task` runs. A future refactor that swaps the
# bare `int(...)` for a permissive helper (e.g. `_int_env`-style with
# default-fallback on parse error) flips both the source-grep AND the
# `pytest.raises` test simultaneously, surfacing the deliberate change.
# ===========================================================================


# TB-278: source-grep originally anchored on the
# `DEFAULT_TASK_MAX_TURNS` named constant rather than the inline
# literal so a future bump to the constant in `config.py` propagates
# without touching this test.
#
# TB-334 (axis 5 core cluster): the call-site shape migrated from
# `int(os.environ.get("AP2_TASK_MAX_TURNS", DEFAULT_TASK_MAX_TURNS))`
# to `int(cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS))`
# so the source-grep expression below tracks the new helper-based shape.
# The behavioral asserts re-evaluate the equivalent expression via the
# helper (`Config.load(tmp_path).get_core_value(...)`) which carries
# the same env-first precedence at call time — the test still exercises
# the EXACT precedence chain `run_task` runs.
_TASK_MAX_TURNS_EXPR = (
    'int(cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS))'
)


def _eval_task_max_turns_via_helper(tmp_path):
    """Re-evaluate `run_task`'s call-site expression against the
    current env. Builds a fresh `Config` rooted at `tmp_path` so the
    project's own `.cc-autopilot/env` doesn't leak, then runs the
    same `cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS)`
    chain `run_task` runs.
    """
    from ap2.config import Config

    cfg = Config.load(tmp_path)
    return int(cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS))


def test_task_max_turns_default_is_two_hundred_when_env_unset(tmp_path, monkeypatch):
    """Happy path: `daemon.run_task` reads AP2_TASK_MAX_TURNS via
    `int(cfg.get_core_value("task_max_turns", default=DEFAULT_TASK_MAX_TURNS))`
    (TB-334 migrated shape). With env unset, the call-site expression
    evaluates to 200 — the `config.DEFAULT_TASK_MAX_TURNS` constant
    (raised from 50 in TB-278). A bump of the constant trips this test
    (source-grep flags the expression drift, behavior assert flags the
    parsed value drift)."""
    src = inspect.getsource(daemon.run_task)
    assert _TASK_MAX_TURNS_EXPR in src, (
        f"regression: `daemon.run_task` no longer reads AP2_TASK_MAX_TURNS "
        f"with the cfg-routed `{_TASK_MAX_TURNS_EXPR}` parse — either the "
        "helper was renamed, the env mapping was dropped from "
        "FLAT_TO_SECTIONED, or the named-constant default drifted"
    )

    monkeypatch.delenv("AP2_TASK_MAX_TURNS", raising=False)
    monkeypatch.delenv("AP2_CORE_TASK_MAX_TURNS", raising=False)
    # Re-evaluate the call-site expression against the scoped env. This
    # is the EXACT code path `run_task`'s `_consume()` runs every call
    # post-TB-334.
    assert _eval_task_max_turns_via_helper(tmp_path) == 200
    assert DEFAULT_TASK_MAX_TURNS == 200, (
        "TB-278: DEFAULT_TASK_MAX_TURNS must be 200; bump this assertion "
        "(and the env template) deliberately if you raise it further"
    )


def test_task_max_turns_env_override_flows_through_to_run_task(tmp_path, monkeypatch):
    """Happy path: `AP2_TASK_MAX_TURNS="30"` → `daemon.run_task`'s
    `int(cfg.get_core_value(...))` (TB-334) parses to 30 and that value
    is what `ClaudeAgentOptions.max_turns` receives. Pins the cfg-routed
    read in `run_task`'s `_consume()` so a refactor that drops it
    surfaces."""
    src = inspect.getsource(daemon.run_task)
    assert _TASK_MAX_TURNS_EXPR in src

    monkeypatch.setenv("AP2_TASK_MAX_TURNS", "30")
    assert _eval_task_max_turns_via_helper(tmp_path) == 30


def test_task_max_turns_invalid_value_raises(tmp_path, monkeypatch):
    """Error path: `daemon.run_task`'s
    `int(cfg.get_core_value(...))` (TB-334) raises ValueError on a
    non-int env value — `Config.get_core_value` returns the raw env
    string (no type coercion at the helper boundary), and the outer
    `int(...)` parse raises just like the pre-TB-334 bare
    `int(os.environ.get(...))` did. Pins CURRENT behavior — a future
    refactor to a permissive `_int_env`-style helper with
    default-fallback would flip the source-grep AND remove the raise
    here, both deliberate visible changes.

    Driving `run_task` end-to-end would catch the ValueError in the
    outer `except Exception as e:` (line 267) and route it through
    `_handle_failure` as a `task_error` event with the trace in the
    error string — the raise still happens, just gets wrapped. We pin
    the raise itself rather than the wrapping so a future refactor that
    swaps `int()` for `_int_env`-with-fallback (silently masking the
    operator's typo) surfaces here, not as a Backlog'd task."""
    src = inspect.getsource(daemon.run_task)
    assert _TASK_MAX_TURNS_EXPR in src

    monkeypatch.setenv("AP2_TASK_MAX_TURNS", "abc")
    with pytest.raises(ValueError):
        _eval_task_max_turns_via_helper(tmp_path)


# ===========================================================================
# (2) AP2_JANITOR_JUDGE_EFFORT — janitor judge SDK `effort`.
#
# Read site: `ap2/janitor.py` line 716-719 (`_judge_finding`):
#     effort = os.environ.get(
#         "AP2_JANITOR_JUDGE_EFFORT",
#         os.environ.get("AP2_AGENT_EFFORT", "high"),
#     )
# Mirrors the AP2_VERIFY_JUDGE_EFFORT precedence chain pinned in
# `test_verify_retry_diff.py` (TB-156). The per-site knob wins; falls
# through to AP2_AGENT_EFFORT global, then to per-site default "high".
# ===========================================================================


def test_janitor_judge_effort_default_falls_through_to_high(tmp_path, monkeypatch):
    """Happy path: with neither `AP2_JANITOR_JUDGE_EFFORT` nor
    `AP2_AGENT_EFFORT` set, `janitor._judge_finding` passes
    `extra_args["effort"] == "high"` to the SDK — the per-site default
    that takes precedence over the rest-of-fleet `xhigh` baseline."""
    monkeypatch.delenv("AP2_JANITOR_JUDGE_EFFORT", raising=False)
    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)
    sdk = _ScriptedJudgeSDK()
    _drive_judge_finding(tmp_path, sdk)

    assert sdk.calls == 1
    extra_args = sdk.captured_options[0].get("extra_args") or {}
    assert extra_args.get("effort") == "high", extra_args


def test_janitor_judge_effort_env_override_flows_through_to_sdk(
    tmp_path, monkeypatch,
):
    """Happy path: `AP2_JANITOR_JUDGE_EFFORT="medium"` →
    `janitor._judge_finding`'s SDK call carries
    `extra_args["effort"] == "medium"`. Pins the env read at
    `janitor.py:717` so a refactor that drops it surfaces."""
    monkeypatch.setenv("AP2_JANITOR_JUDGE_EFFORT", "medium")
    monkeypatch.delenv("AP2_AGENT_EFFORT", raising=False)
    sdk = _ScriptedJudgeSDK()
    _drive_judge_finding(tmp_path, sdk)

    extra_args = sdk.captured_options[0].get("extra_args") or {}
    assert extra_args.get("effort") == "medium"


def test_janitor_judge_effort_takes_precedence_over_global_agent_effort(
    tmp_path, monkeypatch,
):
    """Precedence pin: with BOTH `AP2_JANITOR_JUDGE_EFFORT="medium"` and
    `AP2_AGENT_EFFORT="xhigh"` set, `janitor._judge_finding` reads from
    the per-site knob (medium), NOT the global (xhigh). Mirrors the
    `AP2_VERIFY_JUDGE_EFFORT` vs `AP2_AGENT_EFFORT` precedence pin in
    `test_verify_retry_diff.py:test_judge_effort_per_site_env_takes_precedence`.
    Catches a refactor that flips the nested `os.environ.get` order."""
    monkeypatch.setenv("AP2_JANITOR_JUDGE_EFFORT", "medium")
    monkeypatch.setenv("AP2_AGENT_EFFORT", "xhigh")
    sdk = _ScriptedJudgeSDK()
    _drive_judge_finding(tmp_path, sdk)

    extra_args = sdk.captured_options[0].get("extra_args") or {}
    assert extra_args.get("effort") == "medium", (
        "janitor judge must read its own AP2_JANITOR_JUDGE_EFFORT, not "
        "the global AP2_AGENT_EFFORT — TB-210 precedence regression"
    )


def test_janitor_judge_effort_falls_through_to_global_when_per_site_unset(
    tmp_path, monkeypatch,
):
    """Precedence pin (mirror): with `AP2_JANITOR_JUDGE_EFFORT` unset and
    `AP2_AGENT_EFFORT="xhigh"` set, the global wins (xhigh). Only when
    BOTH are unset does the per-site default `high` kick in. Catches a
    refactor that hardcodes the per-site default and silently breaks the
    fall-through."""
    monkeypatch.delenv("AP2_JANITOR_JUDGE_EFFORT", raising=False)
    monkeypatch.setenv("AP2_AGENT_EFFORT", "xhigh")
    sdk = _ScriptedJudgeSDK()
    _drive_judge_finding(tmp_path, sdk)

    extra_args = sdk.captured_options[0].get("extra_args") or {}
    assert extra_args.get("effort") == "xhigh"


# ===========================================================================
# (3) AP2_JANITOR_JUDGE_MAX_TURNS — janitor judge SDK `max_turns`.
#
# Read site: `ap2/janitor.py` line 724:
#     max_turns=int(os.environ.get("AP2_JANITOR_JUDGE_MAX_TURNS", 12))
# Bare `int(...)` with no fallback; default 12. Wrapped in
# `_judge_finding`'s outer `try/except Exception` (line 748): an invalid
# env value raises ValueError on the parse, which is then captured into
# the verdict's `reasoning` ("judge error: ValueError: ...") and the
# verdict drops to `ambiguous`. We pin both branches so a future
# refactor that bypasses the try/except (or swaps to a permissive parse)
# surfaces here.
# ===========================================================================


def test_janitor_judge_max_turns_default_is_twelve_when_env_unset(
    tmp_path, monkeypatch,
):
    """Happy path: with `AP2_JANITOR_JUDGE_MAX_TURNS` unset,
    `janitor._judge_finding`'s SDK call carries `max_turns=12` — the
    in-source default. A bump trips this test (the literal lives in
    `janitor.py:724`)."""
    monkeypatch.delenv("AP2_JANITOR_JUDGE_MAX_TURNS", raising=False)
    sdk = _ScriptedJudgeSDK()
    _drive_judge_finding(tmp_path, sdk)

    assert sdk.captured_options[0].get("max_turns") == 12


def test_janitor_judge_max_turns_env_override_flows_through(
    tmp_path, monkeypatch,
):
    """Happy path: `AP2_JANITOR_JUDGE_MAX_TURNS="5"` →
    `janitor._judge_finding`'s SDK call carries `max_turns=5`. Pins the
    env read in `_judge_finding` so a refactor that drops the env read
    surfaces."""
    monkeypatch.setenv("AP2_JANITOR_JUDGE_MAX_TURNS", "5")
    sdk = _ScriptedJudgeSDK()
    _drive_judge_finding(tmp_path, sdk)

    assert sdk.captured_options[0].get("max_turns") == 5


def test_janitor_judge_max_turns_invalid_value_falls_back_to_default(
    tmp_path, monkeypatch,
):
    """TB-330 axis-5 migration shifted the parser to the permissive
    `_int_env`-style helper this test's pre-migration form anticipated:
    `_judge_max_turns(cfg)` catches `ValueError` on a non-int env value
    and returns the in-source default (12), so the SDK call proceeds
    normally with `max_turns=12` rather than the previous pre-migration
    behavior where the bare `int(...)` raised and `_judge_finding`'s
    outer `try/except` wrapped it into `verdict="ambiguous"` with a
    `judge error: ValueError: ...` trace.

    The shift is deliberate: an operator typo on a tunable should not
    crash the whole janitor cron run; the migration aligns janitor with
    every other axis-5 cluster (`_ideation_halt_empty_cycles_threshold`,
    `_task_stuck_threshold_s`, etc.) which all return the default on
    bad values. This test pins the new contract so a future revert
    surfaces here.

    Source-grep pin: the migrated helper lives at
    `janitor._judge_max_turns` and reads the value via
    `cfg.get_component_value("janitor", "judge_max_turns")`. A refactor
    that re-introduces a bare `int(...)` raising on bad input would
    drop this pin AND re-introduce the crash-on-typo regression."""
    helper_src = inspect.getsource(janitor._judge_max_turns)
    assert 'cfg.get_component_value("janitor", "judge_max_turns")' in helper_src, (
        "TB-330: `janitor._judge_max_turns` must read via "
        "`cfg.get_component_value('janitor', 'judge_max_turns')`; a "
        "refactor that reverts to a bare-env-read raises crash-on-typo "
        "and breaks the axis-5 cluster contract"
    )

    monkeypatch.setenv("AP2_JANITOR_JUDGE_MAX_TURNS", "abc")

    # Behavioral pin: driving _judge_finding through to the SDK call
    # path: the bad env value resolves to the default 12 via the helper,
    # the SDK is invoked with max_turns=12, and the scripted SDK's
    # canned real_strand reply propagates as the verdict (not the
    # pre-migration ambiguous-from-ValueError trace).
    sdk = _ScriptedJudgeSDK(
        response='{"verdict": "real_strand", "reasoning": "ok"}',
    )
    verdict, reasoning = _drive_judge_finding(tmp_path, sdk)
    assert sdk.calls == 1, (
        "TB-330: bad-value fallback must NOT short-circuit the SDK call; "
        "the helper now returns the default 12 and the judge proceeds"
    )
    assert sdk.captured_options[0].get("max_turns") == 12, (
        "TB-330: `_judge_max_turns(cfg)` must fall back to the in-source "
        "default 12 on a non-int env value"
    )
    # The verdict reflects whatever the scripted SDK returned (not
    # ambiguous-from-error).
    assert verdict == janitor.VERDICT_REAL_STRAND, (
        f"TB-330: with the SDK reached normally, the verdict mirrors "
        f"the scripted reply (not the pre-migration ambiguous-from-"
        f"ValueError); got {verdict}"
    )
    assert "ValueError" not in reasoning, (
        f"TB-330: bad-value fallback no longer routes through the outer "
        f"try/except wrapper, so the reasoning must NOT carry a "
        f"`ValueError` trace; got {reasoning!r}"
    )


# ===========================================================================
# (4) AP2_MM_TEAM_ID — Mattermost API team scope.
#
# Read sites:
#   - `ap2/sandbox.py:817` (`resolve_mm_channel`): when called with
#     `team_id=None`, the helper hits `/users/me/teams`; if the user has
#     no teams, it raises `RuntimeError` whose message names the env var
#     so the operator knows the fix.
#   - `ap2/sandbox.py:943` (`_install_channel_for_project`): reads
#     `os.environ.get("AP2_MM_TEAM_ID") or None` and passes through to
#     `resolve_mm_channel`. Empty-string env collapses to None via the
#     `or None` (`os.environ.get` returns the literal "" when set; bare
#     `or None` flips it back to None so the auto-discover branch kicks
#     in).
#
# The contract: env unset → resolve via `/users/me/teams` (RuntimeError if
# no teams); env set → use as-is. We pin BOTH branches via direct calls
# to the helpers + a `_mm_api_get` mock (no live Mattermost server).
# ===========================================================================


def test_mm_team_id_unset_raises_runtime_when_user_has_no_teams(monkeypatch):
    """Error path: `AP2_MM_TEAM_ID` unset → `sandbox.resolve_mm_channel`
    auto-discovers via `/users/me/teams`. When the user has no teams,
    raises `RuntimeError` whose message names the env var (so an operator
    reading the trace knows to set it explicitly).

    Pin BOTH the env-unset → None propagation AND the named-env-var error
    message. A refactor that drops the `AP2_MM_TEAM_ID` mention from the
    error string would silently break operator self-service: the trace
    no longer points at the fix."""
    monkeypatch.delenv("AP2_MM_TEAM_ID", raising=False)
    monkeypatch.setattr(sandbox, "_mm_api_get", lambda *a, **kw: [])

    with pytest.raises(RuntimeError) as excinfo:
        sandbox.resolve_mm_channel("http://mm", "tok", "stoch")

    assert "AP2_MM_TEAM_ID" in str(excinfo.value), (
        "error message must name the env var so operator knows the fix; "
        f"got: {excinfo.value!r}"
    )


def test_mm_team_id_unset_auto_discovers_first_team(monkeypatch):
    """Happy path (env unset, but user has teams): `resolve_mm_channel`
    hits `/users/me/teams`, takes the first one, then proceeds to channel
    resolution. Pins the auto-discover branch — a refactor that bypasses
    the discovery call (e.g. always requires AP2_MM_TEAM_ID) would break
    the existing default-no-config flow."""
    monkeypatch.delenv("AP2_MM_TEAM_ID", raising=False)
    paths_seen: list[str] = []

    def fake_api(url, token, path):  # noqa: ARG001
        paths_seen.append(path)
        if path == "/api/v4/users/me/teams":
            return [{"id": "team-auto", "name": "first"}]
        if path == "/api/v4/teams/team-auto/channels/name/stoch":
            return {"id": "chan-99", "name": "stoch"}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(sandbox, "_mm_api_get", fake_api)

    ch_id, team_id = sandbox.resolve_mm_channel("http://mm", "tok", "#stoch")
    assert (ch_id, team_id) == ("chan-99", "team-auto")
    # The auto-discover call must be the first API hit when env is unset.
    assert paths_seen[0] == "/api/v4/users/me/teams", (
        "env-unset path must auto-discover via /users/me/teams"
    )


def test_mm_team_id_set_flows_through_install_channel_for_project(
    tmp_path, monkeypatch,
):
    """Happy path (env set): `AP2_MM_TEAM_ID="team-explicit"` →
    `sandbox._install_channel_for_project` reads the env and passes it
    to `sandbox.resolve_mm_channel` as the `team_id` arg, bypassing the
    `/users/me/teams` auto-discover entirely.

    The auto-discover path would call `_mm_api_get(..., '/api/v4/users/me/teams')`
    first; with team_id provided, that call is skipped — only the
    channel-name resolution path fires. Pinning this proves the env
    read at `sandbox.py:943` flows through, AND that the `or None`
    fallback collapses empty-string env to auto-discover (separately
    pinned below).

    `install_project_channel` is stubbed to a no-op-success — the
    actual install hits `_write_sentinel_block` which sudo's as the
    sandbox user (not present in test envs). The env-knob propagation
    is fully exercised before that write, in `resolve_mm_channel`."""
    monkeypatch.setenv("AP2_MM_TEAM_ID", "team-explicit")
    monkeypatch.setenv("MATTERMOST_URL", "http://mm")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")

    paths_seen: list[str] = []

    def fake_api(url, token, path):  # noqa: ARG001
        paths_seen.append(path)
        # If the env knob propagation is broken, /users/me/teams would
        # be the first hit; the AssertionError would surface as a
        # test failure with a clear message.
        if path == "/api/v4/users/me/teams":
            raise AssertionError(
                "auto-discover path fired despite AP2_MM_TEAM_ID being set — "
                "env-knob propagation regression at sandbox.py:943"
            )
        if path == "/api/v4/teams/team-explicit/channels/name/stoch":
            return {"id": "chan-explicit", "name": "stoch"}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(sandbox, "_mm_api_get", fake_api)
    monkeypatch.setattr(
        sandbox, "install_project_channel",
        lambda *a, **kw: 0,
    )

    project_root = tmp_path / "proj"
    (project_root / ".cc-autopilot").mkdir(parents=True)
    rc = sandbox._install_channel_for_project(
        project_root, "test-user", "stoch",
    )
    assert rc == 0
    assert paths_seen == [
        "/api/v4/teams/team-explicit/channels/name/stoch",
    ], (
        "explicit team_id must skip auto-discover and hit the channel "
        f"resolve directly; got call sequence {paths_seen}"
    )


def test_mm_team_id_empty_string_falls_back_to_auto_discover(
    tmp_path, monkeypatch,
):
    """Edge contract pin: `os.environ.get("AP2_MM_TEAM_ID") or None`
    collapses empty-string env to None (since `"" or None == None`),
    which routes through the `/users/me/teams` auto-discover branch.
    A refactor that drops the `or None` fallback (e.g. switches to bare
    `os.environ.get(...)`) would propagate `""` as the team_id and the
    channel-resolve URL would become `/api/v4/teams//channels/...` —
    a 404 with no operator-actionable error. Pin the current safety net.

    `install_project_channel` is stubbed (same reason as the env-set
    sibling test): the env-knob propagation is fully exercised in
    `resolve_mm_channel` before the install write happens."""
    monkeypatch.setenv("AP2_MM_TEAM_ID", "")
    monkeypatch.setenv("MATTERMOST_URL", "http://mm")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")

    paths_seen: list[str] = []

    def fake_api(url, token, path):  # noqa: ARG001
        paths_seen.append(path)
        if path == "/api/v4/users/me/teams":
            return [{"id": "team-fallback", "name": "first"}]
        if path == "/api/v4/teams/team-fallback/channels/name/stoch":
            return {"id": "chan-fb", "name": "stoch"}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(sandbox, "_mm_api_get", fake_api)
    monkeypatch.setattr(
        sandbox, "install_project_channel",
        lambda *a, **kw: 0,
    )

    # Source-grep pin of the `or None` safety net at the install call site.
    src = inspect.getsource(sandbox._install_channel_for_project)
    assert 'os.environ.get("AP2_MM_TEAM_ID") or None' in src, (
        "regression: `_install_channel_for_project` no longer collapses "
        "empty-string AP2_MM_TEAM_ID to None — empty-env would propagate "
        "as `team_id=\"\"` and produce malformed /api/v4/teams//... URLs"
    )

    project_root = tmp_path / "proj-empty"
    (project_root / ".cc-autopilot").mkdir(parents=True)
    rc = sandbox._install_channel_for_project(
        project_root, "test-user", "stoch",
    )
    assert rc == 0
    assert paths_seen[0] == "/api/v4/users/me/teams", (
        "empty-string env must collapse to None via `or None` and hit "
        f"the auto-discover branch; got call sequence {paths_seen}"
    )
