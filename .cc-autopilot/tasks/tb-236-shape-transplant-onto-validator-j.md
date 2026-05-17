# TB-247 — TB-236-shape transplant onto validator-judge: tighten strict-JSON prompt + dump full raw response on parse-failure + enrich event payload

## Goal

Close the diagnostic-dump half of TB-243's validator-judge fail-open
surface so the operator's now-live count signal is actionable rather
than ornamental. This work directly serves goal.md's **Current focus:
end-to-end automation**, axis 1 ("Manual-approval bottleneck") —
specifically the L82-85 framing that "upstream gates already make
this safe in practice" depends on the gates being trustworthy. A
gate that fails 100% of the wall-clock time with zero diagnostic
output is not a trustworthy gate; it's a silent bypass with a
counter.

TB-236 already implemented the operator-blessed prevention +
observability pattern for the prose judge (`f32374f`, 2026-05-15).
The validator judge has the same failure shape, the same fail-open
semantics, and just demonstrated the same diagnostic gap — but never
got TB-236's treatment because TB-235's dep-coherence judge shipped
in parallel without that pattern. This task transplants the
TB-236 pattern verbatim onto the validator judge.

Why now: TB-243 (`647b771`, 2026-05-16T23:59Z) shipped the
`validator_judge_fail` count surface in `ap2 status` + web home; the
very next two ideation cycles (00:29:15Z TB-245 proposal + 02:33:01Z
TB-246 proposal) both hit `validator_judge_fail
error="non-dict judge response"` — 2-for-2 wild failure rate within
4 hours of the surface going live. Under live auto-approve mode (per
status: "auto-approve: enabled"), the dep-coherence gate is the
upstream-trustworthiness anchor for axis-1 walk-away; a 100%
silent-fail rate with no raw-response dump means the operator sees
a number climb but has zero diagnostic data to act on. The operator
explicitly named this fix shape in the TB-231 rejection
(2026-05-16T01:16:59Z): "Right shape is prevention (tighten judge
prompt for shorter strict-JSON output) + observability (full raw
response dumped on parse failure)." TB-236 applied it to the prose
judge; TB-247 applies it to the validator judge. Without this work,
TB-243's count surface stays half-feature and the next investigator
of "why does the dep-coherence judge keep failing" has nothing but
the same 200-char truncated `notes` field that motivated TB-231 →
TB-236 in the first place.

## Scope

Three changes, all within `ap2/tools.py` (callers + tests):

1. **Tighten `_judge_dep_coherence_default` system prompt** (L719-731)
   to match TB-236's prose-judge tightening (`ap2/verify.py`
   `_judge_prose_bullet` system prompt as updated in commit
   `f32374f`). Concretely, add (a) "Your FINAL message must be a JSON
   object only — no markdown fences, no preamble, no trailing
   commentary" directive, (b) a "reasoning field MUST be ≤ 200
   characters" cap, (c) an inline example of the exact response
   shape `{"hard_predecessors": ["TB-N"], "reasoning": "<≤200 chars>"}`,
   (d) keep the existing strict-JSON schema sentence. Intermediate
   tool calls (none currently — judge runs without Read/Glob/Grep)
   stay unconstrained; only the last message is contracted.

2. **Dump full raw SDK response on parse failure** inside
   `_judge_dep_coherence_default`. The function currently captures
   the raw text at L802 (`text = result["text"] or ""`) and returns
   `None` on three parse-failure branches: empty text (L804-805), no
   `{}` braces (L806-809), JSONDecodeError (L810-813), non-dict
   (L814-815). The full text is in scope at all four branches but
   never persisted. Add the TB-236 dump shape:
   - Path: `<events_file.parent>/debug/<ts>-validator-judge-response.txt`
     where `<ts>` is `datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")` —
     identical UTC-timestamp convention to TB-236
     (`ap2/verify.py:630`).
   - Write the FULL raw `text` (no truncation) via
     `dump_path.write_text(text or "")`; create `debug/` with
     `mkdir(parents=True, exist_ok=True)`.
   - Dump fires ONLY on parse-failure branches (successful parses
     leave no trace on disk) — mirrors TB-236's
     `outcome.parse_error is not None` gate.
   - Best-effort: any OSError on dir-create or write swallows to a
     None dump_path so the judge call never fails on diagnostic
     write failure (mirrors TB-236's `try / except` swallow).
   - The dump path needs to surface to `_check_dependency_coherence`
     for inclusion in the event payload — pick whichever signature
     change is least invasive (e.g. change the judge return type from
     `dict | None` to a small `JudgeOutcome` dataclass / NamedTuple
     carrying `data: dict | None`, `dump_path: Path | None`,
     `parse_error: str | None`; or add an `out_dump_path` mutable
     ref). Either choice is fine; document the choice in a code
     comment citing TB-247.

3. **Enrich `validator_judge_fail` event payload** at the existing
   emission sites (L897-905 SDK-exception branch + L915-921 non-dict
   branch). Add two new payload fields when available:
   - `debug_path`: string path to the dump file (relative to
     `events_file.parent` is fine; mirror whatever TB-236 chose for
     `judge_response_dump` in `ap2/verify.py:656-664`).
   - `parse_error`: short categorization string —
     `empty_text` / `no_braces` / `json_decode` / `non_dict` /
     `sdk_exception`. The category is computable at the emission
     site from which branch fired.
   The SDK-timeout branch (L884-895) keeps emitting
   `validator_judge_timeout` unchanged — timeouts don't have a raw
   response to dump.

Test module: `ap2/tests/test_tb247_validator_judge_observability.py`,
parallel to `ap2/tests/test_tb236_prose_judge_observability.py` (if
that exists from TB-236) or `ap2/tests/test_tb236*.py` whichever
landed. Cover at minimum: (a) malformed-text path → dump file lands
in `.cc-autopilot/debug/`, content is the full raw text byte-for-byte,
event payload carries `debug_path` + `parse_error="json_decode"`;
(b) non-dict-JSON path (e.g. judge returns `[1, 2, 3]` as valid JSON)
→ dump fires with `parse_error="non_dict"`; (c) no-braces path
(judge returns prose-only) → dump fires with `parse_error="no_braces"`;
(d) successful-parse path → NO dump file written; (e) OSError on dump
write swallows cleanly — judge still returns None, no crash; (f)
prompt-text regression pin: `grep` finds the new "JSON object only"
+ "≤ 200 characters" directives + inline example in the prompt
string.

## Design

The TB-236 commit `f32374f` is the canonical reference — read it
end-to-end before starting (`git show f32374f -- ap2/verify.py`),
and mirror its choices (debug-dir creation, UTC-ts format, write
swallow, event-payload enrichment with both `parse_error` and dump
path) onto the validator judge. The two judges are structurally
parallel; the goal is byte-for-byte pattern parity, not invention.

Specifically NOT in scope (defer to a future task if evidence
warrants):
- A retry loop on parse failure (operator rejected this shape in
  TB-231; do not reintroduce).
- A separate `judge_call` event for the validator judge (TB-236's
  `judge_call` is verify-side; the existing `validator_judge_fail`
  event covers the failure path here; adding success-path telemetry
  is out of scope unless TB-243's data shows a gap).
- Any change to the fail-open semantics — the gate stays fail-open
  on parse error; this task only adds diagnostic capture.
- Investigating why the SDK returns non-dict in the wild — that's
  the NEXT task, once 2-3 dumps have accumulated from this work.

## Verification

- `uv run pytest -q ap2/tests/test_tb247_validator_judge_observability.py` — new test module exists and all behavioral cases pass.
- `uv run pytest -q ap2/tests/` — full regression gate green.
- `test -f ap2/tests/test_tb247_validator_judge_observability.py` — test module present on disk.
- `grep -nE "JSON object only" ap2/tools.py` — tightened prompt directive present in `_judge_dep_coherence_default`.
- `grep -nE "200 characters" ap2/tools.py` — rationale-length cap present in the prompt.
- `grep -nE "validator-judge-response" ap2/tools.py` — debug-dump filename token wired.
- `grep -nE "debug_path" ap2/tools.py` — `validator_judge_fail` event payload carries the dump-path field.
- `grep -nE "parse_error" ap2/tools.py` — event payload carries the parse-error category.
- `grep -nE "TB-247" ap2/tools.py` — code comment cites the TB-N for the design choice (signature change vs out-ref).
- Prose: `ap2/tools.py` Prose: `_judge_dep_coherence_default` writes a `.cc-autopilot/debug/<UTC-ts>-validator-judge-response.txt` file containing the FULL raw SDK response (no truncation) on parse-failure branches (empty text / no braces / JSONDecodeError / non-dict); successful parses leave no file on disk; the dump-path is surfaced to `_check_dependency_coherence` and included in the `validator_judge_fail` event payload. Pattern mirrors TB-236's `ap2/verify.py::_judge_prose_bullet` (commit `f32374f`).
- Prose: `ap2/tools.py` Prose: parse-failure dump writes are wrapped in a `try/except OSError` swallow so a diagnostic-write failure (full disk, permission denied) does NOT propagate out of `_judge_dep_coherence_default` — the judge still returns None and the fail-open path still emits `validator_judge_fail` even when the dump cannot land. Mirrors TB-236's best-effort write pattern.

## Out of scope

- Retry logic on parse failure (operator-rejected via TB-231).
- Root-cause investigation of WHY the SDK returns non-dict responses
  in the wild — deliberately deferred until this task accrues 2-3
  on-disk dumps to look at.
- Adding success-path telemetry (a `validator_judge_call` event
  analogous to `judge_call`) — out of scope without evidence of need.
- Changing the fail-open semantics — the dep-coherence gate stays
  fail-open on judge failure; this task only adds diagnostic capture.
- Status-report / `ap2 status` / web surface changes — TB-243
  already covers the count surface; this task is purely about
  per-failure diagnostic capture.
## Attempts

### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T080648Z-TB-247.prompt.md`, `stream: .cc-autopilot/debug/20260517T080648Z-TB-247.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T080648Z-TB-247.messages.jsonl`
