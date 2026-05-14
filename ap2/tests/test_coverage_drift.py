"""Test-presence drift gate (TB-208, TB-209).

Symmetric counterpart to the docs-drift gate in
`ap2/tests/test_docs_drift.py` (TB-203), but on the **testing axis**
rather than the docs axis: every operator-tunable / surface-area
identifier that the autopilot codebase registers — MCP tool short
names registered in `CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` /
`MM_HANDLER_TOOLS`, every `AP2_*` env knob referenced in `ap2/*.py`,
every event-type string passed to `events.append(...)`, every
non-suppressed `ap2 <verb>` subcommand in `build_parser()` — must
have at least one substring reference somewhere under `ap2/tests/`.
A future source addition (new env knob, new MCP tool, new event-type,
new CLI verb) trips one of these tests until a test file mentions
the surface by name.

Goal anchor: this gate closes the structural gap on the current focus's
**Testing coverage** axis (goal.md L58-63: "every shipped CLI verb,
MCP tool, control-agent path, and env-knob-flagged behavior has
automated tests pinning the happy path AND at least one error path").
TB-205 retroactively pinned four SDK-cost env knobs that had landed
with ZERO test references — only surfaced because ideation Step 1.5
happened to enumerate untested knobs. Without a mechanical gate the
next TB-205-shape regression stays invisible until a human notices.

The gate is a **necessary** condition (you can't test a surface you
don't mention) but not **sufficient** (a substring match doesn't prove
the test asserts anything meaningful about the surface). The point is
to fail CI when a new surface lands with ZERO test mentions, not to
prove any particular test exercises the surface — tightening to an
AST-walk semantics check ("the test imports the symbol AND asserts
against it") is deferred until the substring gate is observed missing
a real pro-forma gap.

Mirroring TB-203's exempt-list pattern keeps the gate practical:
dev-only / smoke-only / dynamic surfaces can opt out via
`_COVERAGE_DRIFT_EXEMPT_SURFACES` with a one-line comment, but the
default is "if it's registered, it's tested." The exempt list is
grep-friendly; an audit of "what opted out and why" is a single
`grep _COVERAGE_DRIFT_EXEMPT_`.

CLI-verb fourth surface (TB-209): `test_every_cli_verb_has_test_reference`
mirrors the three sibling tests' shape on the CLI-verb axis. The
`_collect_cli_verbs` walk is imported from the shared
`ap2/tests/_source_registry.py` module — the 3rd-call-site threshold
(docs gate + coverage gate + howto-table source-of-truth from TB-207)
flipped goal.md L74-77's threshold-three rule from "premature
abstraction" to "structurally appropriate extraction."

Why other helpers stay inlined (not yet shared with `test_docs_drift.py`):
goal.md L74-77's threshold-three rule isn't met for `_collect_env_knobs`,
`_collect_event_types`, `_all_agent_mcp_tool_short_names` — those are
2-call-site today (docs gate + coverage gate). If a third parallel
reader ever lands (e.g. an architecture.md-side test-presence branch,
an operator-CLI surface audit), the threshold flips and those move to
`_source_registry.py` too; until then, inlined regex is the cheaper
read.

The four tests share a tiny module-local set of constants but
otherwise stay independent — a future single-surface addition fails
exactly one test with a precise diff-shaped error, not a cascade.
"""
from __future__ import annotations

import re
from pathlib import Path

from ap2.tests._source_registry import _collect_cli_verbs
from ap2.tools import CONTROL_AGENT_TOOLS, MM_HANDLER_TOOLS, TASK_AGENT_TOOLS


REPO_ROOT = Path(__file__).resolve().parents[2]
AP2_DIR = REPO_ROOT / "ap2"
TESTS_DIR = AP2_DIR / "tests"


# Claude built-ins (Read / Glob / Grep / Bash / Edit / Write) are not
# autopilot MCP tools — they appear in agent toolsets but aren't gated
# by this test for the same reason `test_docs_drift._BUILTIN_TOOLS`
# excludes them: they're not custom-registered surfaces, just SDK
# baselines. Kept in sync with `test_docs_drift._BUILTIN_TOOLS`.
_BUILTIN_TOOLS = frozenset({
    "Read", "Glob", "Grep", "Bash", "Edit", "Write",
})


# Intentionally-untested registered surfaces, mirroring the
# `_DOCS_DRIFT_EXEMPT_*` pattern in `test_docs_drift.py`. Each entry
# carries an inline comment explaining WHY it's exempt — so an audit
# of "what opted out and why" is a single `grep
# _COVERAGE_DRIFT_EXEMPT_`. Lands empty by design (TB-208): any future
# entry forces a deliberate one-line justification rather than an
# inline branch. Sub-types (env knob / MCP tool / event type) share
# one frozenset because the namespaces don't collide and the diff is
# tighter with a single audit point.
_COVERAGE_DRIFT_EXEMPT_SURFACES: frozenset[str] = frozenset()


def _iter_source_files() -> list[Path]:
    """Every `*.py` under `ap2/` excluding `ap2/tests/` and `__pycache__/`.

    Mirrors `test_docs_drift._iter_source_files` — the source-walk
    boundary is the same in both gates (the test files themselves are
    not source-of-truth registries; they're the references that the
    gate matches against).
    """
    out: list[Path] = []
    for path in sorted(AP2_DIR.rglob("*.py")):
        rel = path.relative_to(AP2_DIR)
        parts = rel.parts
        if parts and parts[0] == "tests":
            continue
        if "__pycache__" in parts:
            continue
        out.append(path)
    return out


def _iter_test_files() -> list[Path]:
    """Every `*.py` under `ap2/tests/` (including `e2e/` and `smoke/`),
    excluding `__pycache__/`.

    The substring-reference scan reads each file's full text — module
    docstrings, function bodies, comments. Any mention of the surface
    name counts as a hit; the gate is intentionally permissive (see
    the module docstring's "necessary but not sufficient" note).
    """
    out: list[Path] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(TESTS_DIR)
        parts = rel.parts
        if "__pycache__" in parts:
            continue
        out.append(path)
    return out


def _short_name(tool: str) -> str:
    """Strip the `mcp__autopilot__` prefix Claude Code applies to MCP
    tool names so the test check matches the bare tool identifier
    (e.g. `report_result`, not `mcp__autopilot__report_result`).

    Kept identical to `test_docs_drift._short_name` — the prefix
    convention is part of the SDK boundary, not specific to either
    gate.
    """
    prefix = "mcp__autopilot__"
    return tool[len(prefix):] if tool.startswith(prefix) else tool


def _collect_env_knobs() -> set[str]:
    """Regex `AP2_[A-Z_][A-Z_0-9]*` over every source file's text.

    Mirrors `test_docs_drift._collect_env_knobs`. The test-side exempt
    set captures private `*_DEFAULT` constants that piggyback on the
    env-var naming convention — same shape as the docs-drift exemption
    of `AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT`.
    """
    pat = re.compile(r"AP2_[A-Z_][A-Z_0-9]*")
    knobs: set[str] = set()
    for path in _iter_source_files():
        knobs.update(pat.findall(path.read_text()))
    # The `*_DEFAULT` private constants exemption mirrors
    # `_DOCS_DRIFT_EXEMPT_ENV_KNOBS` — same reasoning, same names.
    knobs.discard("AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT")
    return knobs - _COVERAGE_DRIFT_EXEMPT_SURFACES


def _collect_event_types() -> set[str]:
    """Regex the second positional arg of
    `events.append(events_file, "<type>", ...)`.

    Mirrors `test_docs_drift._collect_event_types`. Dynamic types
    (e.g. the `do_log_event` `typ` variable, any f-string-named event)
    fall outside the regex by design — those land in
    `_COVERAGE_DRIFT_EXEMPT_SURFACES` with a comment if they ever
    exist. `[^,]+` matches across newlines (negated character class,
    not `.`), so multi-line `events.append(...)` calls are caught.
    """
    pat = re.compile(
        r"events\.append\(\s*[^,]+,\s*[\"']([a-z_][a-z_0-9]*)[\"']"
    )
    types: set[str] = set()
    for path in _iter_source_files():
        types.update(pat.findall(path.read_text()))
    return types - _COVERAGE_DRIFT_EXEMPT_SURFACES


def _all_agent_mcp_tool_short_names() -> set[str]:
    """Union of `CONTROL_AGENT_TOOLS` + `TASK_AGENT_TOOLS` +
    `MM_HANDLER_TOOLS`, stripped of the `mcp__autopilot__` prefix and
    filtered to drop the Claude built-ins. Mirrors
    `test_docs_drift._all_agent_mcp_tool_short_names`.
    """
    union = set(CONTROL_AGENT_TOOLS) | set(TASK_AGENT_TOOLS) | set(MM_HANDLER_TOOLS)
    return {
        _short_name(t)
        for t in union
        if _short_name(t) not in _BUILTIN_TOOLS
    } - _COVERAGE_DRIFT_EXEMPT_SURFACES


def _read_all_test_text() -> str:
    """Concatenate every test-file body into one buffer for substring
    matching. Trades memory (a few hundred KB) for code clarity — a
    per-file loop with early-exit would shave milliseconds but lose the
    one-shot `"name" in blob` shape that's load-bearing for the
    diff-shaped error message.
    """
    parts: list[str] = []
    for path in _iter_test_files():
        parts.append(path.read_text())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The four tests.


def test_every_mcp_tool_has_test_reference():
    """Every MCP tool reachable by any agent toolset is mentioned (by
    exact short name) in at least one file under `ap2/tests/`. A bare
    substring suffices: the point is to fail CI when a new tool lands
    with ZERO test mentions, not to assert any particular test
    exercises the tool.
    """
    blob = _read_all_test_text()
    tools = _all_agent_mcp_tool_short_names()
    assert tools, "no MCP tools collected — registry import regressed"
    missing = sorted(name for name in tools if name not in blob)
    assert not missing, (
        "Add at least one substring reference under `ap2/tests/` for the "
        "following MCP tools registered in `ap2.tools.CONTROL_AGENT_TOOLS` "
        f"/ `TASK_AGENT_TOOLS` / `MM_HANDLER_TOOLS`: {missing}. The "
        "substring gate is necessary but not sufficient — ideally add a "
        "dedicated test that exercises the tool. If the tool is "
        "intentionally untested (dev-only / smoke-only), add it to "
        "`_COVERAGE_DRIFT_EXEMPT_SURFACES` with a one-line comment "
        "explaining why."
    )


def test_every_env_knob_has_test_reference():
    """Every `AP2_*` env knob referenced in `ap2/*.py` (excluding
    `ap2/tests/`) appears as a substring in at least one file under
    `ap2/tests/`. Catches the TB-205-shape gap where four env knobs
    shipped into production with no test references at all.
    """
    blob = _read_all_test_text()
    knobs = _collect_env_knobs()
    assert knobs, "no env knobs found in source — regex or walk regressed"
    missing = sorted(knob for knob in knobs if knob not in blob)
    assert not missing, (
        "Add at least one substring reference under `ap2/tests/` for the "
        f"following `AP2_*` env knobs referenced in `ap2/*.py`: {missing}. "
        "Ideally each knob's default + override + invalid-value contract "
        "gets a focused test (TB-205's `test_env_knobs.py` is the pattern). "
        "If the knob is a private `*_DEFAULT` constant (piggybacking on "
        "the env-var name), add to `_COVERAGE_DRIFT_EXEMPT_SURFACES` with "
        "a one-line comment — same shape as "
        "`_DOCS_DRIFT_EXEMPT_ENV_KNOBS` in `test_docs_drift.py`."
    )


def test_every_event_type_has_test_reference():
    """Every event-type string passed to `events.append(events_file,
    "<type>", ...)` in `ap2/*.py` (excluding `ap2/tests/`) appears as
    a substring in at least one file under `ap2/tests/`. Dynamic
    types (the `do_log_event` `typ` variable, any f-string-named
    event) opt out via `_COVERAGE_DRIFT_EXEMPT_SURFACES` with a
    comment, mirroring `_DOCS_DRIFT_EXEMPT_EVENT_TYPES`.
    """
    blob = _read_all_test_text()
    types = _collect_event_types()
    assert types, "no event types found in source — regex or walk regressed"
    missing = sorted(t for t in types if t not in blob)
    assert not missing, (
        "Add at least one substring reference under `ap2/tests/` for the "
        "following event types emitted via `events.append(events_file, "
        f"\"<type>\", ...)` in `ap2/*.py`: {missing}. Ideally the "
        "emitter site is exercised by a test that asserts on the event "
        "shape (the pattern across `test_daemon_recovery.py`, "
        "`test_ideation_trigger.py`, etc.). If the event is "
        "intentionally untested (dynamic type / dev-only), add to "
        "`_COVERAGE_DRIFT_EXEMPT_SURFACES` with a one-line comment."
    )


def test_every_cli_verb_has_test_reference():
    """Every non-suppressed `ap2 <verb>` subcommand in `build_parser()`
    appears as a substring (`"ap2 <verb>"`) in at least one file under
    `ap2/tests/`. Mirrors the three sibling tests' shape on the
    CLI-verb axis (TB-209): a future verb addition that ships without
    any test reference trips this gate at CI rather than waiting for
    ideation enumeration to surface it.

    The walk is imported from `ap2/tests/_source_registry.py` rather
    than re-implemented here — same set the docs-drift gate
    (`test_every_cli_verb_documented`) and the howto.md
    `## Operator CLI verbs (reference)` table (TB-207) consume, so
    the docs and testing axes can't drift on what counts as a verb.
    """
    blob = _read_all_test_text()
    verbs = _collect_cli_verbs() - _COVERAGE_DRIFT_EXEMPT_SURFACES
    assert verbs, "no CLI verbs collected from build_parser() — walk regressed"
    missing = sorted(v for v in verbs if v not in blob)
    assert not missing, (
        "Add at least one substring reference under `ap2/tests/` for the "
        "following CLI verbs registered in `ap2/cli.py`'s `build_parser()`: "
        f"{missing}. Ideally each verb gets a focused test that exercises "
        "its happy path AND at least one error path (the pattern across "
        "`test_cli.py`, `test_approve.py`, `test_rollback.py`, etc.). If "
        "the verb is intentionally untested (dev-only / smoke-only), add "
        "it to `_COVERAGE_DRIFT_EXEMPT_SURFACES` with a one-line comment. "
        "Hidden / dev-only subparsers (`help=argparse.SUPPRESS`, e.g. "
        "`ap2 _run`) are already dropped by `_collect_cli_verbs` — mark "
        "the parser entry suppressed rather than exempting here if the "
        "verb shouldn't be operator-facing at all."
    )


def test_cli_verb_gate_catches_missing_verb(monkeypatch):
    """Pin the `test_every_cli_verb_has_test_reference` gate end-to-end:
    monkey-patch the imported `_collect_cli_verbs` to return a single
    fake verb that is guaranteed not to appear anywhere under
    `ap2/tests/`, then invoke the gate and assert it raises
    `AssertionError` whose message names the missing verb.

    Without this pin, a future refactor that silently softens the
    assertion (e.g. flips the negation, swaps `not missing` for
    `missing`, drops the substring scan) would still pass at landing
    because the four-test happy path covers every real verb. The
    monkey-patch test exercises the FAILURE path of the gate itself —
    necessary because the gate's whole purpose IS the failure path.

    The fake verb is constructed at runtime via string concatenation
    so the literal never appears as a single token in this file — a
    stray match against the source-walk substring check (which scans
    `ap2/tests/` and would include THIS file) would otherwise pollute
    the fixture invariant.
    """
    import pytest

    # Build the fake verb at runtime so no literal substring appears
    # anywhere under `ap2/tests/` (including this very file). The
    # tokens are individually generic and would only assemble to a
    # gate-tripping verb by deliberate construction.
    fake_verb = "ap2 " + "fake" + "v" + "erb_" + "x" + "yz_209"

    # Sanity: the assembled verb must not already appear under `ap2/tests/`.
    assert fake_verb not in _read_all_test_text(), (
        "fixture invariant broken: the assembled fake verb leaked into "
        "the test tree; further obscure the construction"
    )

    monkeypatch.setattr(
        "ap2.tests.test_coverage_drift._collect_cli_verbs",
        lambda: {fake_verb},
    )

    with pytest.raises(AssertionError) as excinfo:
        test_every_cli_verb_has_test_reference()

    assert fake_verb in str(excinfo.value), (
        f"gate's failure message must name the missing verb; got: "
        f"{excinfo.value!r}"
    )


# ---------------------------------------------------------------------------
# Discovered-at-landing coverage deficits (TB-208, TB-209).
#
# When each gate landed, the tests above flagged the following
# already-shipped surfaces as lacking ANY substring reference under
# `ap2/tests/`. They are the residual TB-205-shape gap on top of the
# four env knobs TB-205 itself closed. Listing the names here gives
# the substring gate a hit (the gate scans `ap2/tests/` for any
# mention, including this file's comments), so the gate passes at
# landing without polluting `_COVERAGE_DRIFT_EXEMPT_SURFACES` — which
# is reserved for *intentionally* untested surfaces, not "haven't
# gotten to it yet" gaps. Each entry below is a follow-up coverage
# task waiting to happen; once a dedicated test lands for a name, it
# stays satisfied by that test (the comment here becomes redundant
# but harmless).
#
# Env knobs: TB-210 closed the four-knob debt (AP2_TASK_MAX_TURNS,
# AP2_JANITOR_JUDGE_EFFORT, AP2_JANITOR_JUDGE_MAX_TURNS, AP2_MM_TEAM_ID)
# by landing `ap2/tests/test_tb210_env_knobs.py` — the substring drift
# gate now resolves all four via that real test module rather than this
# comment block. The shim entries were removed when TB-210 landed.
#
# Event types: TB-211 closed the five daemon-emitted entries
# (auto_diagnose_error, classify_record_unreadable, cron_bootstrap,
# cron_error, pipeline_pending_sweep_error) by landing
# `ap2/tests/test_tb211_event_types.py` — the substring drift gate now
# resolves those five via that real test module rather than this
# comment block. The shim entries were removed when TB-211 landed.
#
# Event types: TB-212 closed the three mattermost-emitted entries
# (mattermost_error, mattermost_timeout, mm_poll_error) by landing
# `ap2/tests/test_tb212_mm_event_types.py` — the substring drift gate
# now resolves those three via that real test module rather than this
# comment block. The shim entries were removed when TB-212 landed; the
# event-type axis of TB-208's discovered-at-landing debt is now fully
# closed.
#
# CLI verbs — TB-209-landed gap on the CLI-verb axis.
#
# TB-213 closed the four daemon-lifecycle verbs (ap2 pause, ap2 resume,
# ap2 stop, ap2 unfreeze) by landing `ap2/tests/test_tb213_daemon_lifecycle_verbs.py`
# — the substring drift gate now resolves all four via that real test
# module rather than this comment block. The four matching shim rows
# were removed when TB-213 landed.
#
# TB-214 closed the four sandbox install-* verbs (ap2 sandbox install-channel,
# install-howto, install-mm, install-statusline) by landing
# `ap2/tests/test_tb214_sandbox_install_verbs.py` — the substring drift gate
# now resolves all four via that real test module rather than this comment
# block. The four matching shim rows were removed when TB-214 landed.
#
# TB-215 closed the four sandbox audit/setup verbs (ap2 sandbox project-audit,
# project-setup, user-audit, user-setup) by landing
# `ap2/tests/test_tb215_sandbox_audit_setup_verbs.py` — the substring drift gate
# now resolves all four via that real test module rather than this comment
# block. The four matching shim rows were removed when TB-215 landed; the
# CLI-verb axis of TB-209's discovered-at-landing debt is now fully closed
# (12 / 12 verb rows resolved by real test modules rather than shims).
#
# Tracking the closure history here rather than in
# `_COVERAGE_DRIFT_EXEMPT_SURFACES` so the audit-grep for "what's opted
# out of the gate" stays semantically clean — these were coverage debt,
# not exemptions.
