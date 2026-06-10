# Provider-neutral default model: agent_model default → None (both backends self-default), + make the codex smoke exercise the real resolution

Tags: #autopilot #codex #backend #model #config #smoke #bug

## Goal

The default agent model is Claude-specific and applied globally to every
backend, so a codex-routed kind is handed a Claude model string and errors.
`core_config_schema.py:253` sets `agent_model` `default="claude-opus-4-7"`; the
dispatch sites (`daemon.py:319` task, `daemon.py:1352` control, `verify.py:643`
prose judge, plus janitor) pass `model=cfg.get_core_value("agent_model")`
unconditionally; and `codex.py:496-497` forwards that string straight to
`openai_codex`, which rejects a `claude-*` id. The real-SDK codex smokes do NOT
catch this because the harness deliberately passes `model=None`
(`ap2/tests/smoke/_adapter.py:167-170,252` — the comments explicitly note "a
live codex turn would [reject]" the Claude `agent_model` and side-step it),
testing "codex with its own default" instead of the production resolution.

Make the default **provider-neutral** so both Claude and Codex work out of the
box, and make the codex smoke exercise the **real** model-resolution path so
this class of leak is caught. Meta-infra bug fix, no focus anchor.

Scope is deliberately the minimal fix (default → None), NOT per-backend model
resolution. A project that explicitly PINS a Claude model
(`AP2_AGENT_MODEL=claude-…`) and then routes a kind to codex will still need to
set a codex-appropriate model (or unset it) for that kind — an accepted
operator responsibility, to be documented (below), not auto-resolved here.

## Scope

- **`core_config_schema.py` — provider-neutral default.** Change `agent_model`'s
  schema `default` from `"claude-opus-4-7"` to a value that resolves to **`None`**
  at the dispatch sites (so the adapters' `if options.model is not None` guards
  omit the `model` kwarg and each backend self-defaults). Note the subtlety:
  `""` is NOT acceptable — `"" is not None` is `True`, so an empty string would
  forward `model=""`. Ensure the resolved value reaching `options.model` is
  genuinely `None` (set the schema default to `None`, and/or coerce empty→`None`
  at the four dispatch sites). Update the `ConfigKey` description to say the
  default is provider-neutral (each backend uses its own default; set
  `AP2_AGENT_MODEL` / the `[core] agent_model` key to pin one).
- **Dispatch sites.** Confirm `daemon.py:319`, `daemon.py:1352`, `verify.py:643`,
  and the janitor model resolution all pass `None` (omit the kwarg) when
  `agent_model` is unset — i.e. nothing reintroduces an inline Claude default.
- **Smoke harness — exercise the real resolution.** In
  `ap2/tests/smoke/_adapter.py`, stop hardcoding `model=None` as a workaround;
  resolve the model the SAME way production does (through the config /
  `agent_model` path under a default config) so that if the default ever
  regresses to a Claude string, the codex variant receives it and the live
  codex turn fails loudly. Update the L167-170 / L252 comments that documented
  the workaround.
- **Gate-runnable regression pin (no real SDK).** Add a FakeSDK/unit test that
  pins the contract without a live call: under a default config (no
  `AP2_AGENT_MODEL`), the resolved `agent_model` is `None` and a codex-routed
  dispatch omits the `model` kwarg (so codex self-defaults); a Claude default
  must NOT reach a codex kind. This is the test that actually guards the gate
  (the real codex smokes are out-of-band).
- **Docs caveat (`ap2/howto.md` backend section).** Add a one-line note in the
  "Agent backend selection" block: `AP2_AGENT_MODEL` / `[core] agent_model` is a
  single global model applied to whichever backend a kind resolves to — leave it
  unset (each backend self-defaults) OR, if you pin it, ensure it's valid for
  every backend in your `[agent_backends]` map (a `claude-*` id will fail a
  codex-routed kind).
- Update any existing test that asserts the old `"claude-opus-4-7"` schema
  default (e.g. config-default / `ap2 config get core.agent_model` tests).

## Design

- Minimal, provider-neutral: `None` means "let the adapter pick its backend
  default," which both `ClaudeCodeAdapter` and `CodexAdapter` already honor
  (`if options.model is not None`). This trades the fresh-project Claude pin
  (`claude-opus-4-7`) for provider-neutrality — accepted; operators who want a
  specific Claude model set `AP2_AGENT_MODEL` (this project already does).
- The smoke change is the real fix to the test gap: the prior `model=None`
  literal made the codex smokes test a configuration production never uses.
  Routing through the actual resolution closes that.

## Verification

- `! grep -qE 'default="claude-opus-4-7"' ap2/core_config_schema.py` — the Claude-specific schema default is gone.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — full suite passes, including the new resolution test and any updated default-assertion tests.
- New test (no real SDK): with no `AP2_AGENT_MODEL` set, the resolved `agent_model` is `None` and a codex-routed dispatch omits the `model` kwarg (asserting a `claude-*` default cannot reach a codex kind).
- `ap2/core_config_schema.py` Prose: `agent_model`'s default is provider-neutral (resolves to `None`, not a `claude-*` string or `""`), and its description states each backend self-defaults unless `AP2_AGENT_MODEL` is set. Judge confirms via Read.
- `ap2/tests/smoke/_adapter.py` Prose: the codex tool-round-trip smokes resolve the model through the production config path (not a hardcoded `model=None`), so a default regression to a Claude model would surface in a live codex run; the old workaround comments are updated. Judge confirms via Read.
- `ap2/howto.md` Prose: the backend section notes `AP2_AGENT_MODEL` is applied to whichever backend a kind resolves to and must be valid for every mapped backend (or left unset). Judge confirms via Read.

## Out of scope

- Per-backend model resolution (`AP2_AGENT_MODEL_<BACKEND>` / an `[agent_models]` table) — the fuller "provider-dependent defaults" design is explicitly deferred; this task only makes the default provider-neutral.
- Auto-resolving a pinned Claude model for codex kinds — an operator who pins `AP2_AGENT_MODEL` to a `claude-*` id and routes a kind to codex must update their config (documented).
- Changing this project's own pinned `AP2_AGENT_MODEL=claude-opus-4-8[1m]` in `.cc-autopilot/env` (operator-owned; all kinds here are Claude-backed).
