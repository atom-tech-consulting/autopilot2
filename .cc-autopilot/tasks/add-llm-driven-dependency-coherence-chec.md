# Add LLM-driven dependency-coherence check to briefing validator: reject when prose names a hard predecessor that `@blocked:TB-N` doesn't declare

Tags: `#autopilot` `#validation` `#code-quality` `#operator-surface` `#llm` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis by adding the first LLM-driven check to `ap2/tools.py:_validate_briefing_structure`. Today's validator is six structural/deterministic checks (TB-154 sections, TB-91/TB-102 parseable Verification, ≥1 bullet, TB-161 substring goal-anchor, TB-164 Why-now, TB-171 no Manual: bullets) — all sync, all regex/string-parsing, all sub-millisecond, all deterministic, all zero-LLM. A new 7th check uses an LLM judge to detect briefings whose prose (Scope / Design / Why-now / description) names another TB-N as a hard predecessor without that TB-N appearing in the task line's `@blocked:TB-N` codespan. Mismatch is silent today and the daemon auto-promotes prematurely — TB-220's briefing said "ap2/_shared.py must already exist (created by the _locked extraction OR the _short extraction)" yet had no `@blocked:TB-217`; TB-224 said "Sequenced after TB-223" yet had no `@blocked:TB-223`. Dispatch order incidentally worked out (lowest TB-N first), but the validator should pin the contract.

Why LLM rather than regex: implicit dependencies don't use the words "depends on" or "after TB-N lands". TB-220's PRECONDITION names a module file, not a TB-N — a regex over the prose finds no `TB-\d+` to flag, even though the predecessor relationship is structural. Regex patterns can catch the "depends on TB-N" / "sequenced after TB-N" surface forms but miss the SEMANTIC predecessor claim. An LLM judge reads the briefing in context — "this prose claims it can't run until X has happened, X is what TB-217 ships, therefore @blocked must include TB-217" — at the right level of abstraction. This is also the first LLM-call surface in the validator path; getting it right sets the pattern for future LLM-augmented checks (scope-substance, anchor-specificity, cross-task duplication).

Why now: TB-223 (auto-approve) shipped. Under manual review, the operator catches dependency mismatches by reading the briefing. Under auto-approve, that judgment never fires — the validator is the only gate. Adding the dep-check now closes the highest-leverage substance hole before the operator enables `AP2_AUTO_APPROVE=1`. Goal.md's mission Done-when bullet "an operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention" requires this — without it, an operator returning after a week finds out-of-order dispatches caused by ideation's prose-vs-codespan drift.

## Scope

(1) Add a new check #7 to `ap2/tools.py:_validate_briefing_structure`. The check fires AFTER all six existing structural checks pass (so the LLM never sees a structurally-malformed briefing). It calls a Haiku-4.5 judge (lowest latency + cost; same model the existing `judge_call` SDK invocations in `ap2/verify.py:_judge_prose_bullet` use).

(2) **Judge prompt contract** (delivered as a system + user message pair to the SDK):
  - System: "You are validating a task briefing for hard-predecessor dependency coherence. A hard predecessor is another task whose work must be on disk (committed) before this task's agent can do its own work — code modules, schema, env knobs, or other artifacts the new task depends on. Soft references (historical context, sibling tasks doing parallel work, references to docstrings or prior commits for reading-comprehension only) are NOT hard predecessors. Return strict JSON: `{\"hard_predecessors\": [\"TB-N\", ...], \"reasoning\": \"<one-paragraph explanation>\"}`."
  - User: structured payload containing (a) the briefing markdown, (b) the task description (post-em-dash prose from the TASKS.md row), (c) the task's current `@blocked:` codespan declarations as a list (e.g. `["TB-217", "review"]`).

(3) **Decision logic** in the validator:
  - Parse the judge's JSON response. If parse fails OR `hard_predecessors` is empty, the check passes (no dependency claim → no @blocked requirement).
  - For each TB-N in `hard_predecessors`, check membership in the task's `@blocked:` codespan list (parsed via the existing `_META_SPAN_RE` machinery in `ap2/board.py`). If any TB-N is missing, return an error message naming the missing dependency AND the judge's reasoning verbatim.
  - Error message shape: `briefing structure invalid: judge identified TB-217 as a hard predecessor (reasoning: "the briefing references ap2/_shared.py as a precondition, which is created by TB-217"). Either add @blocked:TB-217 to the task's codespan, or rephrase the briefing to not claim TB-217 as a hard predecessor.`

(4) **Failure-mode handling** (judge SDK call failures):
  - Timeout (15s default; tunable via `AP2_VALIDATOR_JUDGE_TIMEOUT_S`): log a `validator_judge_timeout` event, SKIP the check, allow queue-append to proceed. Don't block the operator on transient infra.
  - Non-200 / parse failure / model unavailable: same — log + skip. The check is a quality-amplifier, not a hard-block-if-infra-down dependency.
  - This fail-open behavior is deliberate: validator-blocking the operator on Anthropic API hiccups is worse than letting an occasional dependency-mismatch slip through. Surface skipped-judge counts in the cron status-report (TB-228 surface) so the operator notices if skip-rate climbs.

(5) **Cost + latency knobs**:
  - `AP2_VALIDATOR_JUDGE_MAX_TOKENS` (default 500): bound the judge's reasoning output. The decision is structured-JSON; verbose explanations don't help.
  - `AP2_VALIDATOR_JUDGE_DISABLED=1`: hard off-switch. Skips the LLM check entirely; falls back to the six existing structural checks. Operator escape hatch if the check is causing false-positives during a specific operator workflow.
  - Per-invocation cost target: ≤$0.005 with Haiku (typical briefing ≈ 2-4K input tokens + 500 output tokens).

(6) **Integration points** (same surfaces the existing validator covers):
  - `do_operator_queue_append` (the queue-append-time gate that ideation, MM handler, and operator-CLI all hit) — primary integration.
  - `do_board_edit` (legacy direct-board-mutation path) — also integrated for shape symmetry.

(7) **Regression-pin tests** (`ap2/tests/test_dep_validator_judge.py` or extend existing):
  - Mock the SDK call; supply canned judge responses to test the validator's decision logic deterministically (don't make real SDK calls in tests — that's the existing `AP2_REAL_SDK=1` smokes' job).
  - Cover: (a) judge identifies hard pred matching @blocked → pass; (b) judge identifies hard pred NOT in @blocked → reject with specific message; (c) judge identifies empty list → pass; (d) judge returns malformed JSON → log + pass (fail-open); (e) judge timeout → log + pass; (f) `AP2_VALIDATOR_JUDGE_DISABLED=1` → skip entirely; (g) error message includes both the judge's reasoning and the missing TB-N.
  - Optional `AP2_REAL_SDK=1` smoke: send TB-220's actual briefing through with real Haiku → assert judge identifies TB-217 OR TB-218 as a hard predecessor (the implicit `ap2/_shared.py` dep).

(8) **Docs**:
  - Update `ap2/howto.md`'s briefing-authoring guidance to name the new check, the judge's contract, the fail-open behavior, and the `AP2_VALIDATOR_JUDGE_DISABLED` escape hatch.
  - Update the validator's docstring (`_validate_briefing_structure` at tools.py:662) to list check #7 with the same structure as the existing 1-6.

## Design

The check at #7 (not earlier) is deliberate: the LLM judge expects structurally-valid input (sections present, parseable Verification, no Manual bullets). Running it on malformed briefings wastes judge tokens on noise; let the deterministic checks reject first. The cost is one judge call per VALID briefing — typically a few hundred per week in steady-state, ≈ $5-10/month at Haiku rates.

Haiku-4.5 is the right model: low latency (2-5s typical), cheap, sufficient reasoning for "is X a hard predecessor of Y." If empirical drift shows Haiku missing real dependencies, escalate to Sonnet via the existing `AP2_VERIFY_JUDGE_*` env-knob pattern (TB-225-shape). Don't pre-emptively use Sonnet — premature optimization in the wrong direction.

The fail-open posture (timeout / parse failure → log + skip) is the LOAD-BEARING design choice. Failing closed would mean: a single API hiccup blocks every `ap2 add` until the operator notices and clears it. Auto-approve mode (TB-223) amplifies the cost — ideation's per-cycle proposal emission would block on every API hiccup, halting the loop. The check is structured as a quality-amplifier ("most of the time, catch dependency drift"), not a quality-floor ("no dependency drift can ever ship").

Why not async / drain-time check: the existing validator is sync at queue-append, returning err-string OR None. Making one check async would propagate up through `do_operator_queue_append` → `cmd_add` → CLI return. The latency cost of a sync Haiku call (≤5s typical) is acceptable for `ap2 add` (operator types and waits; doesn't block other operators). Async + drain-time would: (a) require a new "rejected after queue-append" state on the board, (b) lose the synchronous feedback loop the operator expects ("did my add succeed?"). Worth it only if the latency proves intolerable in practice — start sync, measure, escalate if needed.

Goal-anchor for this task: the bullet from `## Done when` "an operator can point ap2 at a fresh project, paste a `goal.md` (with Mission + `## Done when`), and walk away for a week without intervention" — this validator is exactly the kind of substance-gate the walk-away promise requires when auto-approve is on. Operators returning after a week shouldn't find dependency-order surprises.

Note: this is the **first** LLM-call surface in the validator path. The pattern this task establishes (Haiku judge + structured JSON response + fail-open + env-knob disable + cost budget) is the template for future LLM-augmented checks (scope-substance, anchor-specificity, cross-task duplication). Worth getting right at #7 because checks #8+ will mirror it.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q ap2/tests/test_dep_validator_judge.py` — new test module passes (exit 0); minimum 7 parameterized cases per Scope §7.
- `grep -nE "AP2_VALIDATOR_JUDGE_DISABLED|AP2_VALIDATOR_JUDGE_TIMEOUT_S|AP2_VALIDATOR_JUDGE_MAX_TOKENS" ap2/tools.py` — exit 0; all three env knobs are read in the validator path.
- `grep -nE "AP2_VALIDATOR_JUDGE_DISABLED" ap2/howto.md` — exit 0; the escape-hatch knob is documented.
- `grep -nE "validator_judge_(timeout|fail)" ap2/events.py ap2/tools.py` — exit 0; the fail-open event types are registered and emitted.
- `grep -nE "claude-haiku" ap2/tools.py` — exit 0; the validator uses Haiku (sanity check that the implementation didn't accidentally use Opus/Sonnet, which would blow the cost target).
- Prose: the validator's check #7 fail-open behavior is correct — on judge SDK call failure (timeout, parse error, network error), the validator logs an event AND returns `None` (allowing queue-append to proceed) rather than returning an error string. Judge confirms via `Read` of the new code path.
- Prose: when `AP2_VALIDATOR_JUDGE_DISABLED=1` is set, check #7 is skipped entirely; checks 1-6 still run unchanged. Judge confirms via `Read` of the conditional + a test case covering the disable path.

## Out of scope

- Migrating the existing 6 deterministic checks to use LLM — they're fast, free, and correct as-is. The LLM-judge augmentation is specifically for the substance-gap that regex can't cover.
- Cross-checking that referenced TB-N actually exists on the board — different validator surface (intra-briefing consistency vs external-reference validity).
- Auto-injecting `@blocked:TB-N` codespans based on judge output — the validator only ensures consistency between prose and codespan; doesn't author the codespan.
- AST-style dependency graph derivation across multiple briefings — even bigger LLM call, separate task if ever needed.
- Caching judge responses across queue-append calls (briefing text changes between calls; cache hit rate would be effectively zero for the legitimate-use case).
- Sonnet/Opus escalation of the judge — start with Haiku; observe; escalate via a future TB if Haiku proves insufficient on real briefings.
- Migrating other LLM-call surfaces (verify._judge_prose_bullet, janitor judges, ideation) to a shared judge-invocation helper — premature; one LLM call site in tools.py doesn't trip the threshold-three rule.
- Auto-resubmitting a rejected briefing after editing the @blocked codespan — operator manually re-runs `ap2 add` or queues an `ap2 update` after rephrasing.
