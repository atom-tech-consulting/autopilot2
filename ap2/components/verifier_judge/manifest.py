"""verifier_judge component manifest (TB-382, axis 5).

Registers the optional LLM prose-bullet judge (relocated from the core
verify runner's `_judge_prose_bullet`) as a `prose_judge` hook point so
`verify.py::verify_task` reaches it via `Registry.verifier_judge(cfg)`
instead of calling a welded-in function. The deterministic shell-bullet
execution path + verdict aggregation + `## Verification` parsing stay in
**core** (verification is gating); only the SDK-call-bearing prose path
moves into this component.

env_flag polarity (`AP2_VERIFY_JUDGE_DISABLED`, suppress-style):
matches `validator_judge`'s `AP2_VALIDATOR_JUDGE_DISABLED`. The component
is `default_enabled=True`, so per the registry's polarity rule
(`Manifest.is_enabled` / `Registry._is_enabled`), a truthy
`AP2_VERIFY_JUDGE_DISABLED` DROPS the component from
`registry.verifier_judge(cfg)` — the walk returns `None`, and
`verify_task` then treats prose bullets via its existing non-judged
`unverified` path while shell bullets still gate. Existing deployments
keep prose judging without opting in (default-on kill switch).

config_schema is intentionally empty. Unlike `validator_judge` — whose
validator-judge knobs are component-owned and read via
`cfg.get_component_value("validator_judge", …)` — the prose judge's
operator knobs (`AP2_VERIFY_JUDGE_EFFORT`, `AP2_VERIFY_JUDGE_MAX_TURNS`,
plus the shared `AP2_AGENT_MODEL` / `AP2_AGENT_EFFORT`) are **core**
knobs: they live in `ap2.core_config_schema.CORE_CONFIG_SCHEMA`, map to
`core.verify_judge_effort` / `core.verify_judge_max_turns` via
`config_compat.FLAT_TO_SECTIONED`, and the relocated judge reads them
verbatim through `cfg.get_core_value(…)`. So they flow through the core
schema + the FLAT_TO_SECTIONED back-compat map already — a shell-export
operator's `AP2_VERIFY_JUDGE_EFFORT` / `AP2_VERIFY_JUDGE_MAX_TURNS`
overrides keep working bit-for-bit — and this component declares no
`[components.verifier_judge]` keys of its own. The manifest's only
operator surface is the `AP2_VERIFY_JUDGE_DISABLED` env_flag.
"""
from __future__ import annotations

from ap2.registry import Manifest

from . import _judge_prose_bullet

MANIFEST = Manifest(
    name="verifier_judge",
    # Suppress-style env flag. `default_enabled=True` means the prose judge
    # participates in `registry.verifier_judge(cfg)` by default — preserving
    # the pre-TB-382 behavior where `verify_task` always judged prose bullets
    # (given an SDK) unless the operator opted out. A truthy
    # `AP2_VERIFY_JUDGE_DISABLED` disables the component (the polarity rule
    # for `default_enabled=True`), so a deployment can verify with shell
    # bullets alone.
    env_flag="AP2_VERIFY_JUDGE_DISABLED",
    default_enabled=True,
    hook_points={
        # The prose-judge hook the core verify runner resolves via
        # `Registry.verifier_judge(cfg)`. Bound to the EXACT relocated
        # function object so `verify._judge_prose_bullet` (the TB-382
        # back-compat module `__getattr__` shim) and the registry walk
        # return the same callable.
        "prose_judge": _judge_prose_bullet,
    },
    dependencies=[],
    # See module docstring: the verify-judge knobs are core-owned (read
    # via `cfg.get_core_value`), so this component declares no
    # component-scoped config keys.
    config_schema={},
)
