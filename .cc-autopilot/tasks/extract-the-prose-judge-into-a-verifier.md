## Goal

Advance `Current focus: extract the remaining core subsystems into components`
by splitting the optional LLM prose-judge out of the core verify runner into a
`verifier_judge` component, mirroring the existing `validator_judge` component
at `ap2/components/validator_judge/`. goal.md axis (5): `verify.py::verify_task`
keeps parsing the `## Verification` section, running the deterministic shell
bullets, and aggregating verdicts in **core** (verification is gating); only
`_judge_prose_bullet` (verify.py L470) — the SDK-call-bearing prose path — moves
into `ap2/components/verifier_judge/` and is reached via the registry. A
deployment can then verify with shell bullets alone, prose-judge disabled.
Purely structural — `AP2_VERIFY_JUDGE_*` knob names and judge behavior preserved
bit-for-bit.

Why now: goal.md's axis-(5) delete-test says "if the prose-judge stays inside
the verify runner, the LLM verification layer can't be disabled independently of
the gating shell-bullet path" — this is the one axis with no tick-phase
dependency, so it can land in parallel with axis 1.

## Scope

- Create `ap2/components/verifier_judge/` (`impl.py` holding the relocated
  `_judge_prose_bullet` body + its SDK/adapter call, `manifest.py` exposing a
  `MANIFEST` that registers a prose-judge hook point and an `env_flag` following
  the `validator_judge` polarity, thin `__init__.py`).
- Have `verify.py::verify_task` resolve the prose-judge through the registry (the
  same shape `Registry.briefing_validators(cfg)` / the `validator_judge`
  component already use) rather than calling the welded-in function; the
  shell-bullet path + verdict aggregation stay in core.
- When the `verifier_judge` component is disabled, the runner still gates on
  shell bullets and treats prose bullets via its existing non-judged path (no
  crash, no silent pass that bypasses a shell gate).
- Preserve `AP2_VERIFY_JUDGE_EFFORT` / `AP2_VERIFY_JUDGE_MAX_TURNS` (and
  siblings) verbatim via the component's `config_schema` + the back-compat env
  override layer.

## Design

The `validator_judge` component is the structural template: a default-on
component whose `manifest.py` registers an LLM-judge hook the core caller looks
up via the registry, leaving the deterministic path in core. Apply the same
cleavage here — `verify_task` walks the registry for the prose-judge hook (empty
walk ⇒ component disabled ⇒ prose bullets fall through to the existing
unverified/non-judged handling, shell bullets still gate). The relocated
`_judge_prose_bullet` keeps its current signature, cumulative-diff resolution,
and Read/Glob/Grep allowed-tools so judge verdicts are bit-for-bit unchanged when
enabled. `env_flag` polarity matches `validator_judge` (default-on kill switch)
so existing deployments keep prose judging without opting in. The
`AP2_VERIFY_JUDGE_*` knobs flow through `config_schema` + the
`FLAT_TO_SECTIONED` back-compat map so a shell-export operator's overrides keep
working.

## Verification

- `uv run pytest -q` — full suite passes.
- `test -f ap2/components/verifier_judge/manifest.py` — the verifier_judge
  component exists with a manifest.
- `uv run pytest -q ap2/tests/test_verify_retry_diff.py ap2/tests/e2e/test_verify_per_task.py` — verify-runner behavior (shell bullets gate, prose bullets judged) is preserved.
- `! grep -nE 'def _judge_prose_bullet' ap2/verify.py` — the prose-judge body no
  longer lives in the core verify runner.
- `ap2/verify.py` Prose: `verify_task` reaches the prose-judge through the
  component registry (not a direct import of `ap2/components/verifier_judge/`),
  and the deterministic shell-bullet path + aggregation remain in core; judge
  confirms via Read.
- Prose: a new regression test pins that with the `verifier_judge` env flag
  disabled, `verify_task` still runs and gates on shell bullets while skipping
  the LLM prose judge; judge confirms the test asserts this via Read.

## Out of scope

- Extracting pipeline (axis 1), cron (axis 2), ideation (axis 4), or decoupling
  auto-approve (axis 3) — separate tasks.
- Any change to the deterministic shell-bullet execution path, verdict
  aggregation, or the `## Verification` parsing — those stay in core unchanged.