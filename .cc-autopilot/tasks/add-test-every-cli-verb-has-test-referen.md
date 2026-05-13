## Goal

Advance goal.md's **Current focus: code quality** on two axes in a single task. On the **Testing coverage** axis (goal.md L58-63: "every shipped CLI verb, MCP tool, control-agent path, and env-knob-flagged behavior has automated tests pinning the happy path AND at least one error path"), close the deferred-by-TB-208 4th surface by adding `test_every_cli_verb_has_test_reference` to `ap2/tests/test_coverage_drift.py`, parallel to the existing three regression-pin tests for MCP tools / env knobs / event types. On the **Code reusability** axis (goal.md L74-77: "when a piece of logic appears at three or more call sites with structural similarity, extract to a shared helper"), extract `_collect_cli_verbs` (defined at `ap2/tests/test_docs_drift.py:125-168`, walking `build_parser()` and dropping `argparse.SUPPRESS` leaves) into a shared helper module that both `test_docs_drift.py` and `test_coverage_drift.py` import — that's the third reader (docs gate + coverage gate + the howto-table source-of-truth TB-207 introduced), which trips the threshold-three rule from "premature abstraction" to "structurally appropriate extraction."

The testing-axis gate stops the next CLI-verb-shipped-without-tests regression at CI rather than via ideation enumeration; the helper extraction is the threshold-triggered move TB-208's author explicitly named in their docstring (`ap2/tests/test_coverage_drift.py:41-46`: "A separate follow-up task adds that fourth test once the helper's `_collect_cli_verbs` walk is reusable across both gates").

Why now: TB-208 just landed (`e2179b9`, 2026-05-13T01:35:43Z) with an explicit deferred-task pointer in its module docstring; TB-207 just landed (`5d1d197`, 2026-05-13T02:09:58Z) introducing the `_collect_cli_verbs` walk that this task extracts. Both predecessors are settled in the current cycle, and adding the third call site is the specific event that flips goal.md L74-77's threshold-three rule. Without this task, the next CLI-verb addition can ship with zero test references (the exact TB-205-shape regression the coverage-drift gate exists to catch) and only surface via ideation enumeration weeks later.

## Scope

1. **Extract `_collect_cli_verbs` to a shared helper module.** Create `ap2/tests/_source_registry.py` (or another suitable module name; the file MUST live under `ap2/tests/`) and move `_collect_cli_verbs` (currently `ap2/tests/test_docs_drift.py:125-168`) verbatim into it, preserving the docstring, the `argparse.SUPPRESS` exclusion, and the group-vs-leaf walk semantics. Update `test_docs_drift.py` to `from ap2.tests._source_registry import _collect_cli_verbs` (or equivalent import) so the existing `test_every_cli_verb_documented` test still passes against the same set.

2. **Add `test_every_cli_verb_has_test_reference` to `ap2/tests/test_coverage_drift.py`.** The new test mirrors the three existing tests' shape:
   - Imports `_collect_cli_verbs` from the shared helper module.
   - For each verb in the returned set, asserts at least one substring reference exists somewhere under `ap2/tests/` (use the same file-walk pattern as `test_every_mcp_tool_has_test_reference`).
   - Honors the existing `_COVERAGE_DRIFT_EXEMPT_SURFACES` frozenset for opt-outs (one inline comment per exempt verb explaining why); lands with the exempt set unchanged unless a real exemption is needed.
   - Failure message names the missing verb and the test-file walk that would have caught it (mirror the precise diff-shaped error pattern of the other three tests).

3. **Update `test_coverage_drift.py`'s module docstring (lines 41-46)** to reflect that the deferred CLI-verb test now exists; remove the "deferred" language.

4. **Verify the gate actually catches a missing CLI verb.** Add a tiny test (or extend an existing one) that monkey-patches `_collect_cli_verbs` to return `{"ap2 fakeverb"}` and asserts `test_every_cli_verb_has_test_reference` fails with an error message naming `fakeverb` — pins the gate's behavior end-to-end without an ad-hoc verb edit.

## Design

- The shared helper module name is at the implementing agent's discretion (`_source_registry.py`, `_cli_verbs.py`, `_parser_walk.py` — pick the one that reads best given what other helpers, if any, also belong there). Whatever the name, both `test_docs_drift.py` AND `test_coverage_drift.py` MUST import the same `_collect_cli_verbs` from it.
- The new test in `test_coverage_drift.py` SHOULD reuse the same source-file walk helper that the existing three tests use (`_iter_source_files` is the wrong walk here; the test walks `ap2/tests/` not `ap2/`). Look at `test_every_mcp_tool_has_test_reference` for the exact pattern; mirror it.
- The exempt set (`_COVERAGE_DRIFT_EXEMPT_SURFACES`) is already shared across the three sub-types per TB-208's design ("Sub-types (env knob / MCP tool / event type) share one frozenset because the namespaces don't collide and the diff is tighter with a single audit point"). CLI verbs (`"ap2 <verb>"` / `"ap2 <group> <sub>"`) DO NOT collide with the existing namespaces (env knobs are `AP2_*`, MCP tool short names are bare identifiers, event types are `snake_case`), so the same single frozenset can hold a CLI-verb exempt entry without ambiguity. Keep the single frozenset.
- Run the full test suite to confirm no regression elsewhere — the helper extraction is a pure refactor of one function; a green suite confirms no caller drift.

## Verification

- `uv run pytest -q ap2/tests/test_coverage_drift.py` — all tests pass (exit 0), including the new `test_every_cli_verb_has_test_reference`.
- `uv run pytest -q ap2/tests/test_docs_drift.py` — full file still passes (exit 0); the existing `test_every_cli_verb_documented` test still resolves the same set after the helper is moved.
- `uv run pytest -q ap2/tests` — full ap2/tests suite passes (exit 0); confirms the helper extraction broke no other caller.
- `grep -q "def test_every_cli_verb_has_test_reference" ap2/tests/test_coverage_drift.py` — exit 0; the new test is present by the canonical name.
- `grep -q "_collect_cli_verbs" ap2/tests/test_coverage_drift.py` — exit 0; the new test imports the shared helper rather than re-defining it.
- `[ "$(grep -rc "^def _collect_cli_verbs" ap2/tests/ | grep -v ':0$' | wc -l)" -le 1 ]` — at most one file under `ap2/tests/` defines `_collect_cli_verbs` at module level (the shared-helper module); the original `test_docs_drift.py` definition is gone, replaced by an import.
- The new test `test_every_cli_verb_has_test_reference` in `ap2/tests/test_coverage_drift.py` uses the same `_COVERAGE_DRIFT_EXEMPT_SURFACES` frozenset as the existing three sibling tests (no new exempt set introduced), and asserts at least one substring reference per CLI verb under `ap2/tests/` (judged by the SDK against the diff).
- `ap2/tests/test_coverage_drift.py`'s module docstring no longer describes the CLI-verb test as "deferred" (the lines 41-46 paragraph in the current docstring is rewritten or removed).

## Out of scope

- Extracting other helpers (`_collect_env_knobs`, `_collect_event_types`, `_all_agent_mcp_tool_short_names`) to the shared module. Those are 2-call-site today (docs gate + coverage gate); threshold-three is not yet met, and pre-emptive bundling re-trips goal.md L74-77's "premature abstraction is its own failure mode" guardrail. Leave inlined; flag as "extract when a third reader appears" in the shared module's docstring if the scoping reads naturally.
- AST-walk semantics tightening for any of the four `test_coverage_drift.py` tests (substring → "imports the symbol AND asserts against it"). Deferred until the substring gate is observed missing a real pro-forma gap, per TB-208's docstring.
- Adding any new CLI verb to the parser, or modifying any existing verb's behavior. Pure test-infrastructure refactor + new gate.
- A parallel howto.md docs-side change. The howto's `## Operator CLI verbs (reference)` table already exists (TB-207); this task only closes the testing-axis mirror.
