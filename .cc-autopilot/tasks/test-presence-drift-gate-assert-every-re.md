# TB-208 — Test-presence drift gate for MCP tools / env knobs / event types

Tags: `#autopilot` `#tests` `#code-quality` `#regression-pin`

## Goal

Close the lingering gap on the **current focus: code quality** focus's
(1) **Testing coverage** axis (goal.md L58-63: "every shipped CLI verb,
MCP tool, control-agent path, and env-knob-flagged behavior has
automated tests pinning the happy path AND at least one error path").
TB-203 landed a docs-drift gate that catches surface-vs-docs regressions;
TB-205 retroactively covered four already-shipped env knobs that had
ZERO `ap2/tests/` references — the canonical missed-coverage case. The
structural fix on the test axis is the symmetric primitive: a
regression-pin test that enumerates each registered surface from the
source-of-truth registry and asserts each entry has at least one
substring reference somewhere under `ap2/tests/`, failing CI with a
diff-shaped error message when a new surface lacks a test-file mention.

Why now: TB-205 (commit `c13a07c`, 2026-05-12) shipped 17 unit tests for
four env knobs that had been in production with no test references —
only surfaced when ideation Step 1.5 happened to enumerate untested
SDK-cost knobs. Without a mechanical gate, the next TB-205-shape
regression (new env knob, MCP tool, or event type landing without test
refs) stays invisible until a human notices, typically weeks later. The
docs-drift gate (`test_docs_drift.py::test_every_env_knob_documented`,
`_mcp_tool_documented`, `_event_type_documented`) WOULD catch the same
gap on the docs axis if the same surfaces were also docs-stale — that
symmetric value is what TB-208 captures on the test axis. The gate is
a one-shot mechanical primitive (~30 LOC parallel to the docs-drift
tests), not enumerative pitfall hunting.

## Scope

(1) Add `ap2/tests/test_coverage_drift.py` (sibling to
`ap2/tests/test_docs_drift.py`). Three regression-pin tests, one per
registered surface. Inline the source-walk regex from
`test_docs_drift.py` rather than extracting a shared helper (per
goal.md L74-77, the threshold-three for extraction isn't met — only
two call sites today: docs-drift and coverage-drift).

(2) Test shapes:

  - `test_every_mcp_tool_has_test_reference` — union of
    `CONTROL_AGENT_TOOLS` + `TASK_AGENT_TOOLS` + `MM_HANDLER_TOOLS`
    short names (Claude built-ins Read/Glob/Grep/Bash/Edit/Write
    excluded, mirroring `test_docs_drift._BUILTIN_TOOLS`). Each name
    must appear as a substring in at least one file under
    `ap2/tests/` (including `ap2/tests/e2e/`). Fails with a
    diff-shaped error naming the missing tools and pointing at the
    source-of-truth registry.

  - `test_every_env_knob_has_test_reference` — regex
    `AP2_[A-Z_][A-Z_0-9]*` over the same source-file walk used by
    `test_docs_drift._collect_env_knobs` (every `*.py` under `ap2/`
    excluding `ap2/tests/` and `__pycache__/`). Test-side exempt set
    mirrors `_DOCS_DRIFT_EXEMPT_ENV_KNOBS` (the private `*_DEFAULT`
    constants exemption). Each knob appears in at least one test file.

  - `test_every_event_type_has_test_reference` — regex over
    `events.append(events_file, "<type>", ...)` calls (mirrors
    `test_docs_drift._collect_event_types`); each event type appears
    in at least one test file. Dynamic-type exemption mirrors
    `_DOCS_DRIFT_EXEMPT_EVENT_TYPES`.

(3) An empty `_COVERAGE_DRIFT_EXEMPT_SURFACES` constant in the new
file, shaped like `_DOCS_DRIFT_EXEMPT_ENV_KNOBS` (frozenset of
intentionally-untested surface names with an inline comment per entry
explaining why). Land empty; future exempt additions surface as
explicit one-line entries with a reason, never inline branches.

(4) Each test's failure message includes the source-of-truth registry
name (e.g. "Add at least one substring reference to `ap2/tests/` for
the following MCP tools registered in `ap2.tools.CONTROL_AGENT_TOOLS`
/ `TASK_AGENT_TOOLS` / `MM_HANDLER_TOOLS`: ...") so a CI failure tells
the operator exactly which registry to scan against.

(5) CLI-verb fourth surface DEFERRED: an argparse-walk helper that
enumerates `ap2 <verb>` subcommands isn't a shared primitive yet
(TB-207 in Backlog awaiting review would land it). The new file's
module docstring notes the deferral and pins TB-207 as the natural
follow-up — once TB-207's helper lands, a follow-up task adds
`test_every_cli_verb_has_test_reference` using the same shape.

## Design

The substring-presence primitive is intentional. It's the same shape
as the docs-drift gate: a *necessary* condition (you can't test a
surface you don't mention) but not *sufficient* (mentioning a name in
a test doesn't prove the test asserts anything about it). The point
is to fail CI when a new surface lands with ZERO test mentions, not
to assert any particular test exercises the surface meaningfully. A
follow-up could tighten the gate to AST-walk semantics ("the test
imports the symbol AND calls an assertion against it"), but that
needs meaningfully larger LOC and an observed pro-forma-test failure
case to design against. Defer that escalation until the substring
gate is observed missing a real gap.

Mirroring TB-203's exempt-list pattern keeps the gate practical:
dev-only or smoke-only surfaces can opt out with a one-line comment,
but the default is "if it's registered, it's tested." The exempt
list is grep-friendly; an audit of "what opted out and why" is a
single `grep _COVERAGE_DRIFT_EXEMPT_`.

Inlining the source-walk regex (rather than extracting from
`test_docs_drift.py`) is the deliberate goal.md L74-77 call: with
only two call sites today, extraction is premature. If a third
parallel gate ever lands (e.g. an `ap2/architecture.md`-side
test-presence gate, or a TB-207-CLI-verb test-presence branch), the
threshold flips and a shared `_source_registry.py` extraction
becomes the right move; until then, inlined regex is the cheaper
read.

## Verification

- `uv run pytest -q ap2/tests/test_coverage_drift.py` — exits 0
  (all three tests pass against current HEAD with no exempt entries).
- `test -f ap2/tests/test_coverage_drift.py` — gate file exists.
- `[ "$(grep -cE '^def test_every_(mcp_tool|env_knob|event_type)_has_test_reference\(' ap2/tests/test_coverage_drift.py)" -ge 3 ]` — all three test functions present.
- `uv run pytest -q ap2/tests/` — full regression suite green (no existing test should change behavior; only the new file lands).
- `grep -qE '_COVERAGE_DRIFT_EXEMPT_SURFACES' ap2/tests/test_coverage_drift.py` — exempt-set constant present.
- Prose: the new file's module docstring names TB-203 /
  `test_docs_drift.py` as the structural parent AND cites goal.md's
  Testing coverage axis (L58-63) as the goal anchor — judge confirms
  by reading the first ~40 lines of
  `ap2/tests/test_coverage_drift.py`.

## Out of scope

- Behavioral-coverage gate (AST walk asserting an assertion against
  the surface) — substring presence is the chosen primitive; tighten
  only on observed pro-forma test gap.
- CLI-verb fourth branch — waits on TB-207's argparse-walk helper.
- Refactoring `test_docs_drift.py` to share helpers — done only if a
  third parallel gate lands (goal.md L74-77 threshold-three).
- Pre-emptively populating `_COVERAGE_DRIFT_EXEMPT_SURFACES` — the
  set lands empty; exempt entries surface reactively.
