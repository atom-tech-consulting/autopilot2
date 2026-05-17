# Fix TB-235 validator-judge: `extra_args={"max-tokens": ...}` is rejected by SDK; replace with valid budget control or drop

Tags: `#autopilot` `#bug` `#validator` `#llm` `#regression-pin`

## Goal

Advance goal.md's **Current focus: end-to-end automation** focus's (1) **Manual-approval bottleneck** axis by repairing TB-235's LLM dep-coherence validator (`_validator_judge_llm` in `ap2/tools.py`). The validator's SDK invocation passes `extra_args={"max-tokens": str(max_tokens)}` at `ap2/tools.py:747`, but the Claude Agent SDK rejects `--max-tokens` as an unknown option — every `ap2 add` since the daemon restart has emitted `error: unknown option '--max-tokens'` to stderr AND incremented `validator_judge_fail_count_24h`. The fail-open posture I designed into TB-235 (timeout / parse error / unknown error → log + skip) means operators don't see the failure as a hard block, but the validator NEVER actually runs — the LLM dep-coherence check shipped 100% non-functional. TB-243 (`647b771`) surfaced the failure count in `ap2 status` text + JSON + web; without that surface this would still be silent.

Why now: I just hit it filing TB-248 (`/tmp/tb-audit-cmd.md`), confirmed via `grep` that every other `extra_args` in the codebase uses `{"effort": ...}` (verify.py:550, janitor.py, daemon.py × 3), and the failure count rose to 4 within the 24h window. Under `AP2_AUTO_APPROVE=1` (not yet enabled but TB-232's dry-run on-ramp is the next planned step), the validator is the only substance gate between ideation proposals and dispatch — having it silently broken means proposals with hard-dependency drift would slip through unjudged. Fix the bug so the validator actually fires; downstream the `validator_judge_fail_count_24h` counter on next `ap2 add` invocations should return to zero.

## Scope

(1) **Fix `_validator_judge_llm` SDK invocation** at `ap2/tools.py:747`:
  - Remove `extra_args={"max-tokens": str(max_tokens)}` — the option doesn't exist in the Claude Agent SDK's recognized arg set.
  - Replace the budget-bound mechanism with `max_turns` on `ClaudeAgentOptions` (the existing SDK-supported budget knob; every other ap2 SDK call uses it). For this validator, `max_turns=2` is right: one assistant message (JSON verdict) + one optional tool call (which shouldn't be needed since the validator passes the full briefing + @blocked list inline; the judge has no reason to invoke Read/Grep). Cap at 2 keeps the call short and bounded.
  - Wire `AP2_VALIDATOR_JUDGE_MAX_TURNS` (new env knob, default 2) as the operator-tunable knob in place of `AP2_VALIDATOR_JUDGE_MAX_TOKENS`. The old knob name (`AP2_VALIDATOR_JUDGE_MAX_TOKENS`) should be DEPRECATED — kept in code as an alias for backward compatibility (if set, log a `validator_judge_deprecated_knob` event once and use the value as `max_turns` instead, ceiling-cap at 5). Pure migration courtesy; the alias can be removed in a future TB if no operator hits it.

(2) **Verify the fix end-to-end**: add a smoke that fires the validator against a known-good briefing (no dep-mismatch claims) and asserts `validator_judge_fail_count_24h` does NOT increment. Use `AP2_REAL_SDK=1` smoke pattern from `test_env_knobs.py` so the test only runs in the real-SDK suite, not on every PR.

(3) **Reset the existing failure counter**: not a code change — operator will see `validator_judge_fail_count_24h` continue to count failures from the OLD code for up to 24h after this fix lands (events.jsonl retains them in the window). Document this in the commit message + briefing's Out-of-scope so the operator knows to expect a 24h tail before the counter cleanly resets to 0.

(4) **Tests** (`ap2/tests/test_tb_validator_judge_sdk_args.py`):
  - `test_validator_judge_extra_args_does_not_contain_max_tokens`: read `_validator_judge_llm` source via inspect; assert the `extra_args=` literal does not contain the string `max-tokens` (regression-pin against re-introducing the bug).
  - `test_validator_judge_uses_max_turns_for_budget`: mock the SDK; assert `ClaudeAgentOptions(max_turns=...)` is called with a positive int (2 by default, env-overridable).
  - `test_validator_judge_deprecated_knob_alias`: set `AP2_VALIDATOR_JUDGE_MAX_TOKENS=10`; assert the validator uses `max_turns=5` (ceiling-capped from 10) AND emits a `validator_judge_deprecated_knob` event once per process.
  - `test_validator_judge_fail_count_unchanged_on_happy_path`: integration test with mocked SDK returning valid JSON; assert no `validator_judge_fail` event fires.

(5) **Update howto.md**: the validator's section currently documents `AP2_VALIDATOR_JUDGE_MAX_TOKENS` per TB-235's briefing. Update to document `AP2_VALIDATOR_JUDGE_MAX_TURNS` as the canonical knob + `AP2_VALIDATOR_JUDGE_MAX_TOKENS` as a deprecated alias with the ceiling-cap behavior.

## Design

**Why `max_turns` not raw token count**: every other SDK call in the codebase uses `max_turns` (verify.py:547 reads `AP2_VERIFY_JUDGE_MAX_TURNS`, janitor.py:724 reads `AP2_JANITOR_JUDGE_MAX_TURNS`, daemon.py:208 reads `AP2_TASK_MAX_TURNS`). The validator should follow the same pattern — turns are the SDK's native budget abstraction, tokens are an LLM-API abstraction that the agent SDK wraps. Using `max_turns` makes the validator consistent with the rest of the codebase AND uses a primitive the SDK actually accepts.

**Why max_turns=2 default**: the validator is a single-shot JSON-emitting judge. It needs exactly ONE assistant message (the verdict). The `max_turns=2` cap allows ONE tool call (Read/Grep) in case the judge legitimately needs to read goal.md or a file the briefing references — but the validator's prompt is structured to pass all needed context inline, so the second turn shouldn't be needed. Cap at 2 (not 1) gives the judge a small escape hatch without unbounded multi-turn cost.

**Why deprecate `AP2_VALIDATOR_JUDGE_MAX_TOKENS` rather than silently remove**: TB-235 documented it in howto.md and (presumably) in the briefing template. Operators who copied it into their env file would silently lose the constraint if removed. The alias-with-warning preserves backward compat for one cycle; a future TB can remove the alias after operator engagement confirms no one set it.

**Goal-anchor**: the Done-when bullet "an operator can point ap2 at a fresh project, paste a `goal.md`, and walk away for a week without intervention" depends on the validator gate working under auto-approve. Today's broken-but-fail-open state passes the walk-away promise's letter (operator can `ap2 add` without errors) but fails its spirit (validator is non-functional, no dep-coherence check actually fires). Fix restores spirit.

## Verification

- `uv run pytest -q ap2/tests/` — full suite green (exit 0).
- `uv run pytest -q ap2/tests/test_tb_validator_judge_sdk_args.py` — new test module passes (4 cases per Scope §4).
- `! grep -n 'max-tokens' ap2/tools.py` — exit 0 (zero matches; the broken arg name is gone). `!` inverts the no-match exit per the TB-187 idiom.
- `grep -nE "max_turns\s*=" ap2/tools.py` — exit 0; the corrected budget mechanism is wired (positive match; this grep finds the new `ClaudeAgentOptions(max_turns=...)` call site).
- `grep -nE "AP2_VALIDATOR_JUDGE_MAX_TURNS" ap2/tools.py ap2/howto.md` — exit 0; the new env knob is read in code AND documented in howto.
- `grep -nE "validator_judge_deprecated_knob" ap2/tools.py` — exit 0; the deprecated-alias event type is emitted.
- Prose: post-fix, an `ap2 add` invocation against a structurally-valid briefing emits an `auto_approved`-shape event chain (if AP2_AUTO_APPROVE is on) or a normal queue-append event (if off) — and does NOT emit a `validator_judge_fail` event. Judge confirms by running `ap2 add --briefing-file <test-briefing> --blocked review` against a freshly-restarted daemon and reading the resulting events.jsonl tail.
- Prose: the `AP2_VALIDATOR_JUDGE_MAX_TOKENS` deprecated alias correctly emits the `validator_judge_deprecated_knob` event AND uses the value as `max_turns` capped at 5. Judge confirms via `Read` of the alias-handling code + a test case from Scope §4.

## Out of scope

- Removing the `AP2_VALIDATOR_JUDGE_MAX_TOKENS` deprecated alias outright — preserve for backward compat one cycle; remove in a follow-up TB if no operator uses it.
- Resetting historical `validator_judge_fail_count_24h` events in events.jsonl — append-only file; counter will naturally tail down as the 24h window slides past the old failures.
- Migrating other SDK call sites to a shared `_sdk_extra_args(effort, ...)` helper — that's a separate threshold-three case (currently 5 call sites use `{"effort": ...}`); a follow-up TB can extract if the pattern doesn't shift further.
- Adding a "validator health check" command (e.g. `ap2 doctor` audit for validator-judge fail rate) — separate observability surface; TB-243 already covers the surfacing, this fix restores the underlying functionality.
- Changing the prompt the validator sends to the SDK — the prompt was correct, only the invocation args were wrong. Prompt iteration is a future cycle question if operator engagement shows the validator over- or under-flags hard predecessors.
## Attempts

### 2026-05-17 — verification_failed
(no summary)
- **kind:** project_wide
- **verify_command:** uv run pytest -q ap2/tests/
- **exit_code:** None
- **stderr_tail:** 
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260517T060342Z-TB-249.prompt.md`, `stream: .cc-autopilot/debug/20260517T060342Z-TB-249.stream.jsonl`, `messages: .cc-autopilot/debug/20260517T060342Z-TB-249.messages.jsonl`
