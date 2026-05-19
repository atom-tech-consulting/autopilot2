# Centralize LLM-JSON extraction in stdlib `raw_decode` util; replace 4 brittle `find("{")/rfind("}")` sites

Tags: #autopilot #verify #judge #parser #refactor #reuse #regression-pin

## Goal

The same hand-rolled JSON-from-LLM-response extraction pattern lives in FOUR places — `ap2/verify.py:721`, `ap2/verify.py:810`, `ap2/janitor.py:793`, `ap2/tools.py:845`. All four use `text.find("{") + text.rfind("}")` which is **unbalanced** (captures the first `{` to the last `}`, not the first balanced object). When the LLM's preamble contains literal braces (set notation, code samples, parameter sweeps), the slice contains free-form prose and `json.loads` fails. Concrete downstream cost: post-train TB-89 burned 3 retry cycles + manual unfreeze when the judge's prose contained `{50/150, 150/50}` and the parser misread that as the start of the verdict JSON. Full repro at `/Users/claude-agent/repos/post-train/.cc-autopilot/bug-reports/ap2-judge-response-parser-greedy-braces.md`; captured failing response at `/Users/claude-agent/repos/post-train/.cc-autopilot/debug/20260519T095236Z-TB-89-judge-bullet15-response.txt`.

Goal anchor: this directly serves `goal.md` `## Done when` bullet "Failure recovery (verification fails, retries exhaust, daemon restart, cron drift, agent timeouts) is fully automatic; only genuine design forks escalate." TB-89's failure sequence (verification fails → retries exhaust → operator manually unfreezes) is exactly that bullet, and the same bug pattern lives at three more call sites — each one a latent retry-exhaustion-and-Frozen incident waiting for an LLM response with set notation in the preamble.

Why now: the bug already cost a downstream ap2-driven project a manual-unfreeze cycle (post-train TB-89, 3 retries). The validator-judge dep-coherence parser (TB-247's site) shares the bug shape but was hardened only on the prompt + observability axis, not the extraction axis. Janitor (`ap2/janitor.py:793`) and the second `verify.py` site haven't blown up yet but are one set-notation-preamble away. Centralizing the four sites into one util closes all four at once and makes the next `find/rfind` regression impossible (one place to test, one place to break).

## Scope

- **New module `ap2/json_extract.py`** exporting:
  - `extract_rightmost_json_object(text: str) -> tuple[dict, int, int] | None` — returns `(parsed_object, start_offset, end_offset)` for the rightmost top-level JSON object in `text`, or `None` if no parseable JSON object is found. Implementation uses `json.JSONDecoder().raw_decode` from stdlib (NOT a hand-rolled brace-depth scanner): walks all candidate `{` positions from rightmost to leftmost, tries `raw_decode` from each, returns the first success.
- **Replace four call sites with the new util:**
  - `ap2/verify.py:721-735` — JSON parsing in the sibling judge / extraction block.
  - `ap2/verify.py:810-829` — the prose-bullet judge `_parse_judge_response` (the TB-89 trigger).
  - `ap2/janitor.py:793-798` — janitor agent JSON parsing.
  - `ap2/tools.py:845-...` — validator-judge dep-coherence JSON parsing (the site TB-247 hardened on prompt + observability axes).
- **Preserve each call site's existing `parse_error` taxonomy and TB-236 / TB-247 observability hooks** — only the extraction boundary-finding moves to the shared util. Categorization, debug-file-dump-on-parse-failure, and event-payload enrichment all stay intact at each call site.
- **Update docstrings**: `ap2/verify.py:_parse_judge_response` currently claims to extract "the first balanced `{...}` substring" but the code is unbalanced — fix the docstring along with the implementation. Same for any other sites whose comments lie.
- **Regression-pin test module `ap2/tests/test_json_extract_util.py`** covering: preamble brace shadowing (the TB-89 captured shape), multiple shadowing snippets, JSON strings containing internal `{` / `}` chars, JSON escape sequences (`\"`, `\\`), no JSON object found → returns `None`, multiple top-level JSON objects → returns rightmost.
- **Integration check** against the captured post-train TB-89 response file (same shape as the bug report's integration verification bullet).

## Design

- **Use `json.JSONDecoder.raw_decode`, not a hand-rolled brace scanner.** `raw_decode(text, offset)` parses JSON starting at `offset` and returns `(obj, end_offset)`. It's the actual stdlib JSON parser — all string-escape semantics (`"` inside strings, `\"`, `\\`, `{`/`}` inside strings, Unicode escapes) are correct by construction because they're handled by the same code that parses regular JSON. The bug report's proposed reverse-scanning brace-depth scanner is correct in shape but reinvents what stdlib already provides.
- **Algorithm:** find all `{` positions in `text`; iterate from rightmost to leftmost; for each position try `decoder.raw_decode(text, pos)`; first success is the rightmost top-level JSON object. O(n × k) where k = number of `{` chars; k is typically 1-5 in LLM responses so this is effectively O(n).
- **Why rightmost wins by contract:** the judge / janitor / validator-judge prompts all require the final JSON verdict at the end of the response. Prose preamble is tolerated (per TB-236's `trailing_prose_after_json` distinction). So the verdict is always the LAST top-level `{...}` block. Scanning rightmost-first is correct by contract.
- **Why stdlib over third-party libraries:**
  - `json-repair` (popular for LLM JSON extraction) — adds a dep to handle a problem stdlib already solves; its value is repairing malformed JSON, not finding JSON in mixed text.
  - `dirtyjson` / `demjson3` — handle relaxed JSON (trailing commas, comments). Not the bug shape here; judge prompts pin strict JSON.
  - `partial-json-parser` — streaming partial JSON. Not relevant.
  - `jsonfinder` — finds JSON in text, but is ~50 LOC of stdlib `raw_decode` wrapping. Not worth a dep.
  - `raw_decode` is zero-dep, idiomatic Python, battle-tested string-handling — strictly better fit.
- **Fallback semantics:** if `raw_decode` fails on all candidate `{` positions, return `None`. Each call site translates `None` back to its existing "no JSON object in response" error path so observable behavior on truly-malformed responses is unchanged.
- **Preserve `parse_error` taxonomy at call sites:** the util returns `tuple | None`; each call site keeps its TB-236 / TB-247-shape categorization, debug-dump-on-failure, and event-payload-enrichment logic. The fix is purely about *which substring gets fed to `json.loads`*, not about diagnostics.
- **Backward compatibility:** every response that the current parsers handle correctly continues to be handled correctly post-fix; the fix only widens the set of inputs that parse correctly. Existing test cases in `ap2/tests/test_judge_parse_observability.py` and any janitor/validator-judge tests should all still pass — the change is strictly additive on the correctness frontier.

## Verification

- `uv run pytest -q` — full project suite passes.
- prose: a new module `ap2/json_extract.py` exports `extract_rightmost_json_object` and uses `json.JSONDecoder().raw_decode` from stdlib (NOT a hand-rolled brace-depth scanner). The implementation walks `{` positions from rightmost to leftmost.
- prose: `ap2/verify.py:_parse_judge_response` calls the new util; the `start = response.find("{")` / `end = response.rfind("}")` pair at lines 810-811 in HEAD is gone.
- prose: same replacement landed at `ap2/verify.py:721` (shell-bullet judge sibling block), `ap2/janitor.py:793` (janitor JSON parsing), and `ap2/tools.py:845` (validator-judge dep-coherence) — all four call sites use the new util.
- prose: regression test module `ap2/tests/test_json_extract_util.py` covers (a) preamble brace shadowing — the TB-89 captured shape, (b) multiple shadowing snippets in preamble, (c) JSON strings containing internal `{` / `}` chars, (d) JSON escape sequences (`\"`, `\\`), (e) no JSON object found returns `None`, (f) multiple top-level JSON objects returns rightmost.
- `cd /Users/claude-agent/repos/autopilot2 && uv run python -c "from ap2.verify import _parse_judge_response; resp = open('/Users/claude-agent/repos/post-train/.cc-autopilot/debug/20260519T095236Z-TB-89-judge-bullet15-response.txt').read(); outcome = _parse_judge_response('prose: verdict-conditional Reading paragraph', resp); assert outcome.verdict.status == 'pass', f'expected pass got {outcome.verdict.status!r} parse_error={outcome.parse_error!r}'; assert outcome.parse_error is None, f'expected parse_error=None got {outcome.parse_error!r}'; print('TB-89 captured response now parses correctly')"` — the captured post-train TB-89 response now parses as `pass` with `parse_error=None`.
- `grep -nE 'response\.find\("\{"\)|response\.rfind\("\}"\)|text\.find\("\{"\)|text\.rfind\("\}"\)' ap2/*.py | grep -v test_ | wc -l | awk '$1 == 0 { exit 0 } { exit 1 }'` — none of the four hand-rolled `find("{") + rfind("}")` patterns remain in non-test code.

## Out of scope

- Adding `json-repair` or any third-party JSON library as a dependency — stdlib `raw_decode` solves this fully.
- Retroactive re-evaluation of previously-frozen tasks (e.g. re-judging post-train TB-89 with the fixed parser). Downstream operator workflow is to unfreeze + retry; this TB fixes the underlying defect so future retries pass.
- Relaxed JSON (trailing commas, comments, single quotes). Strict JSON only — the judge / janitor / validator-judge prompts all pin strict JSON.
- Prompt-contract tightening (forbidding LLM agents from using literal braces in preamble). The parser fix is the correct layer; further prompt tightening can be a follow-up if rightmost-balanced-object extraction misses other edge cases.
- `parse_error` taxonomy expansion. Existing TB-236 + TB-247 categories cover the call sites' behaviors; no new enum value needed.
- Event-schema changes to `judge_call` / `validator_judge_fail` / janitor events.
- Refactoring `parse_error` categorization into a shared util as well. That's a follow-up if needed; this TB is scoped to the extraction-boundary fix.
- Operator-side CLI changes (e.g. `ap2 judge --replay-response`). Separate UX scope.
