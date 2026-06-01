## Goal

Advance **Current focus: codex support through an agent adaptor layer** by
migrating the verifier prose-bullet judge (`verify._judge_prose_bullet`) — the
verifier prose-judge named in goal.md's axis-6 migration order (L177-183). This
moves the `verifier_judge` agent kind (already declared in `AGENT_KINDS`,
`ap2/adapters/select.py` L50) behind the `AgentAdapter` seam so it is
adapter-routed and per-kind backend-selectable, paying down the Progress signal
"Every dispatch site (task, control, verifier-judge, ideation-scrub,
validator-judge, janitor-judge) runs through the adapter".

Why now: every piece of adapter seam this migration needs already exists in
HEAD — `select_adapter(kind, cfg)` (`ap2/adapters/select.py`), the
backend-neutral `AgentOptions`, and `AgentAdapter.run_to_result`
(`ap2/adapters/base.py`) — so repointing the prose-judge is a small,
well-templated read-only-tool dispatch relocation; leaving the verifier on
direct `sdk.query` keeps it the lone un-migrated judge while the rest of the
focus moves behind the interface, and every future change that assumes the
Claude stream shape makes this retrofit more expensive.

## Scope

- Repoint `verify._judge_prose_bullet` (`ap2/verify.py`) from building
  `sdk.ClaudeAgentOptions(...)` (verify.py:593) and consuming
  `async for msg in sdk.query(...)` (verify.py:611) to resolving the adapter
  via `select_adapter("verifier_judge", cfg)` and dispatching through
  `adapter.run_to_result(prompt, tools, options)`.
- Preserve every dispatch parameter the site passes today: `cwd=project_root`,
  `allowed_tools=JUDGE_REPO_READ_TOOLS`, `permission_mode`, `max_turns`
  (`verify_judge_max_turns`), `model` (`agent_model`), and the resolved
  `effort` (`verify_judge_effort` -> `agent_effort` fallback at verify.py:588).
- Keep the TB-157 usage/cost capture working by reading the normalized
  `AgentResult.usage` the adapter returns, so the per-judge cost accounting is
  unchanged.

## Design

Build a backend-neutral `AgentOptions` instead of `sdk.ClaudeAgentOptions`;
resolve the adapter with `cfg` in hand via `select_adapter("verifier_judge",
cfg)`, falling back to a default `ClaudeCodeAdapter()` on the `cfg=None` seam so
the existing hermetic unit tests stay deterministic. Drive the call through
`adapter.run_to_result(...)` and map the returned `AgentResult`'s final
assistant text and `usage` onto the current `text` / `result_meta` consumers.
The existing in-HEAD `ap2/ideation_scrub.py` dispatch (`_resolve_scrub_adapter`
/ `_run_scrub`) is a working in-tree example of this exact `select_adapter` +
`run_to_result` shape to follow. Late-import the adapters package at dispatch
time to keep `verify.py`'s import path light.

## Verification

- `uv run pytest -q ap2/tests/test_verify_retry_diff.py ap2/tests/e2e/test_verify_per_task.py` — the prose-judge tests pass against the adapter-routed dispatch.
- `grep -q "select_adapter" ap2/verify.py` — the prose-judge resolves its backend through the per-kind selector.
- `grep -q "verifier_judge" ap2/verify.py` — the migrated site names the `verifier_judge` agent kind passed to `select_adapter`.
- `! grep -nE "sdk\.query\(" ap2/verify.py` — the direct `sdk.query` dispatch is gone from the verifier (routed through the adapter instead).
- `ap2/verify.py` Prose: `_judge_prose_bullet` drives `adapter.run_to_result(...)` rather than calling `sdk.query` directly, passing the same allowed_tools / max_turns / effort / model it passed before the migration; judge confirms via Read.
- `uv run pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full unit suite stays green.

## Out of scope

- Migrating any other dispatch site (validator-judge / janitor-judge / run_task
  / control-agent) — each is its own sequenced axis-6 TB.
- Changing the judge prompt, tool allowlist, or verification semantics — this is
  a pure dispatch relocation behind the interface.
- Running the gated real-SDK prose-judge smoke
  (`ap2/tests/smoke/test_prose_judge_real_sdk.py`) in the unit gate — it
  requires real-SDK creds and runs on the 6h `real-sdk-smoke` cron.
