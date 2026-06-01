## Goal

Advance **Current focus: codex support through an agent adaptor layer** by
migrating the validator-judge and janitor-judge component dispatch calls behind
the `AgentAdapter` seam — the "validator-judge + janitor-judge" pair goal.md
explicitly groups in its axis-6 migration order (L177-183). This moves the
`validator_judge` and `janitor_judge` agent kinds (both already declared in
`AGENT_KINDS`, `ap2/adapters/select.py` L52-53) onto adapter-routed, per-kind
backend-selectable dispatch, advancing the Progress signal "Every dispatch site
(task, control, verifier-judge, ideation-scrub, validator-judge, janitor-judge)
runs through the adapter".

Why now: the adapter seam these two judges need is already in HEAD —
`select_adapter(kind, cfg)` (`ap2/adapters/select.py`), the backend-neutral
`AgentOptions`, and `AgentAdapter.run_to_result` (`ap2/adapters/base.py`). Both
are one-shot judge calls with the same dispatch shape, so grouping them in one
task (per goal.md's grouping) avoids two near-identical reviews; leaving them on
direct `sdk.query` keeps two of the six dispatch sites un-migrated while the
focus moves behind the interface.

## Scope

- Repoint `_judge_dep_coherence_default` in `ap2/components/validator_judge/impl.py`
  from `sdk.ClaudeAgentOptions(...)` (impl.py:785) + `async for msg in
  sdk.query(...)` (impl.py:796) to `select_adapter("validator_judge", cfg)` +
  `adapter.run_to_result(prompt, tools, options)`.
- Repoint the janitor judge call in `ap2/components/janitor/impl.py` from
  `sdk.ClaudeAgentOptions(...)` (impl.py:785) + `async for msg in
  sdk.query(...)` (impl.py:796) to `select_adapter("janitor_judge", cfg)` +
  `adapter.run_to_result(...)`.
- Preserve each site's existing dispatch parameters verbatim: the
  validator-judge's timeout / `max_turns` / `model` / `effort` and its
  Goal+Scope-sliced prompt (TB-270); the janitor-judge's `max_turns` /
  `model` / `effort`. Keep their parse-failure observability paths (TB-236 /
  TB-247) intact, reading the final text from the normalized `AgentResult`.

## Design

For each site build a backend-neutral `AgentOptions` instead of
`sdk.ClaudeAgentOptions`, resolve via `select_adapter(<kind>, cfg)` with a
default `ClaudeCodeAdapter()` fallback on the `cfg=None` seam so hermetic unit
tests stay deterministic, and drive `adapter.run_to_result(...)`, mapping the
returned `AgentResult` final text + `usage` onto each site's current consumers.
The in-HEAD `ap2/ideation_scrub.py` dispatch (`_resolve_scrub_adapter` /
`_run_scrub`) is a working in-tree example of this `select_adapter` +
`run_to_result` shape to follow. Late-import the adapters package at dispatch
time in both modules.

## Verification

- `uv run pytest -q ap2/tests/test_janitor.py ap2/tests/test_tb_validator_judge_sdk_args.py ap2/tests/test_tb270_validator_judge_payload_slice.py ap2/tests/test_judge_parse_observability.py` — the validator-judge and janitor-judge tests pass against the adapter-routed dispatch.
- `grep -q "select_adapter" ap2/components/validator_judge/impl.py` — the validator-judge resolves its backend through the per-kind selector.
- `grep -q "select_adapter" ap2/components/janitor/impl.py` — the janitor-judge resolves its backend through the per-kind selector.
- `! grep -rnE "sdk\.query\(" ap2/components/validator_judge/impl.py ap2/components/janitor/impl.py` — neither judge calls `sdk.query` directly anymore (both routed through the adapter).
- `ap2/components/validator_judge/impl.py` Prose: `_judge_dep_coherence_default` drives `adapter.run_to_result(...)` for the `validator_judge` kind, preserving its sliced prompt / timeout / max_turns / effort / model and its parse-failure observability; judge confirms via Read.
- `ap2/components/janitor/impl.py` Prose: the janitor judge call drives `adapter.run_to_result(...)` for the `janitor_judge` kind, preserving its max_turns / effort / model; judge confirms via Read.
- `uv run pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the full unit suite stays green.

## Out of scope

- Migrating `run_task` or `_run_control_agent` — each is its own sequenced
  axis-6 TB after this one.
- Changing either judge's prompt, timeout calibration (TB-269), payload slice
  (TB-270), or fail-open semantics — this is a pure dispatch relocation.
- Running the gated real-SDK validator-judge smoke
  (`ap2/tests/smoke/test_validator_judge_real_sdk.py`) in the unit gate — it
  runs on the 6h `real-sdk-smoke` cron.
