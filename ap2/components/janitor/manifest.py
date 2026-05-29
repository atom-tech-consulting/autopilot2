"""Janitor component manifest (TB-309 canary + TB-330 read-site migration).

Declares the janitor's registry-visible shape: env flag, default-enabled
state, hook points (the `tick_hook` the daemon dispatches on the
`janitor` cron job, plus the `status_findings_counts` data accessor the
CLI status command + status-report digest both consume).

`AP2_JANITOR_DISABLED` is the kill switch — set it to a truthy value
(`1`, `true`, etc.) to disable the janitor entirely. Default-on per
goal.md L64-67 (backwards-compat: existing operators don't need to opt
in to keep TB-177/TB-178 behavior). The per-judge knobs
(`AP2_JANITOR_MAX_FINDINGS_LLM`, `AP2_JANITOR_JUDGE_EFFORT`,
`AP2_JANITOR_JUDGE_MAX_TURNS`) tune the judge's per-run cost/quality,
not the component's enable state.

TB-330 axis-5 read-site migration — chosen resolved-config access shape
=========================================================================
The three operator-tunable per-judge knobs the component logically owns
(`max_findings_llm`, `judge_effort`, `judge_max_turns`) are now read via
the **`cfg.get_component_value(component, key)`** helper on `Config`
(option 2 of the TB-326 pilot's three candidate shapes — see
`ap2/components/auto_approve/manifest.py` and `ap2/config.py`'s
docstring for the helper). The three legacy flat env names
(`AP2_JANITOR_MAX_FINDINGS_LLM`, `AP2_JANITOR_JUDGE_EFFORT`,
`AP2_JANITOR_JUDGE_MAX_TURNS`) are no longer read directly via the
`os.environ` mapping inside the component body; the back-compat path
flows through `Config.get_component_value`'s reverse-`FLAT_TO_SECTIONED`
lookup so a shell-export operator who never migrated their
`.cc-autopilot/env` keeps today's behavior bit-for-bit, while a
TOML-opted operator's `[components.janitor]` values win transparently
once env-side overrides are unset.

The kill switch `AP2_JANITOR_DISABLED` continues to flow through
`Manifest.is_enabled()`'s `env_flag` mechanism (consulted in
`ap2/registry.py`, not inside the component body), so it never appears
as a direct `os.environ.get(...)` call inside `ap2/components/janitor/`
— the briefing's grep-absence Verification bullet already passes for
the kill switch by construction. The TB-323 `FLAT_TO_SECTIONED` map
preserves the back-compat path for a TOML-opted operator who wants to
flip `[components.janitor] disabled = true` from the structured config
side instead; the env-only consumer in `registry.py` reads
`AP2_JANITOR_DISABLED` directly as today.

Why option 2 (helper) and not 1 (raw dict) or 3 (per-component
dataclass): TB-326's pilot (b3eba54), TB-327's auto_unfreeze sibling
(48ab4a8), TB-328's attention sibling (980da5e), and TB-329's
focus_advance sibling (17deb25) ratified the helper as the lightest-
touch incremental shape every remaining cluster reuses verbatim —
option 1 loses env-only-mode back-compat without an extra wrapper (the
env-only resolution branch doesn't invoke `apply_env_overrides`), and
option 3 requires a code-gen pass on every `Manifest.config_schema`.
The TB-330 regression-pin at
`ap2/tests/test_tb330_janitor_cfg_reads.py` mirrors the
TB-326/TB-327/TB-328/TB-329 cleavages (grep-absence, TOML-first read
precedence, flat-env back-compat parity, parser default-on-bad-value
semantics preservation, and the manifest's documented access shape).

Hook-points contract under TB-330: the three helpers acquired (or kept)
a `cfg: Config` argument as part of the migration — `_max_findings_llm`
gained `cfg` (it was env-only pre-TB-330), and `_judge_effort` /
`_judge_max_turns` are new helpers that `_judge_finding` now consults
in place of the inline env reads. None of these helpers are exposed via
`hook_points` (they're internal to `run_janitor`'s judge step); the
public surface (`run_janitor`, `recent_finding_counts_by_verdict`) is
unchanged.

The registry walks `ap2/components/*/manifest.py` via
`pkgutil.iter_modules` and reads each module's `MANIFEST` attribute. No
hardcoded list in `ap2.registry` mentions "janitor" — discovery is
filesystem-driven so future migrations need zero registry-side edits
(goal.md L188-201).
"""
from __future__ import annotations

from ap2.config_loader import ConfigKey
from ap2.registry import Manifest, Phase

from . import recent_finding_counts_by_verdict, run_janitor


MANIFEST = Manifest(
    name="janitor",
    env_flag="AP2_JANITOR_DISABLED",
    default_enabled=True,
    hook_points={
        # axis (2) — the daemon's `run_cron` dispatches the `janitor`
        # cron job by looking this hook up and `await`-ing it. Signature:
        # `async def tick_hook(cfg, sdk) -> JanitorReport`.
        "tick_hook": run_janitor,
        # janitor-specific data accessor for the CLI `ap2 status` and
        # status-report digest composition. Signature:
        # `def status_findings_counts(cfg, *, window_s=None) -> dict[str, int]`.
        # The verdict-keyed counts feed both the urgency-tone split in
        # cli_daemon's `janitor:` line and the status-report digest's
        # `## Current state` section.
        "status_findings_counts": recent_finding_counts_by_verdict,
    },
    # TB-310 (axis 2): janitor's tick_hook is registered on
    # `POST_CRON` for the new phase-walked contract. The daemon does
    # NOT walk POST_CRON from `_tick`; the cron scheduler owns
    # janitor's invocation cadence (the existing `run_cron` lookup
    # via `default_registry().hook("tick_hook", component="janitor")`
    # stays unchanged). The entry here documents the phase the
    # janitor's tick-callable conceptually belongs to so the
    # registry's phase-keyed view is complete — load-bearing for the
    # `Registry.tick_hooks(POST_CRON)` regression-pin in
    # `ap2/tests/test_tb310_tick_hook_protocol.py`.
    tick_hooks=[(Phase.POST_CRON, run_janitor)],
    dependencies=[],
    # TB-321 (axis 1 canary): janitor was the first component to
    # declare a `config_schema`. The original single-key form mirrored
    # the existing kill-switch env. TB-330 (axis 5) extends the schema
    # to the three per-judge knobs the component also logically owns
    # so a TOML-opted operator can write
    # `[components.janitor]` entries for any of the four knobs without
    # tripping `validate_config`'s reject-unknown-key path. Per-
    # component reads at the
    # `cfg.get_component_value("janitor", <key>)` shape flow through
    # the resolved-config helper on `Config`; the kill switch
    # `disabled` is still consulted by `Manifest.is_enabled()`'s
    # `env_flag` mechanism inside `ap2/registry.py` rather than via
    # the component body (so the briefing's grep-absence pin passes
    # for the kill switch by construction).
    config_schema={
        "disabled": ConfigKey(
            name="disabled",
            type=bool,
            default=False,
            description=(
                "Kill switch for the janitor cron job. True suppresses "
                "every `run_janitor` invocation (CLI status block keeps "
                "showing the component as off). Mirrors the existing "
                "`AP2_JANITOR_DISABLED` env var; TB-323 (axis 2) wires "
                "the env-override back-compat map so the env var keeps "
                "overriding the TOML value during the migration window."
            ),
            hot_reloadable=False,
        ),
        "max_findings_llm": ConfigKey(
            name="max_findings_llm",
            type=int,
            default=10,
            description=(
                "Per-run cap on LLM judge calls (TB-178). A scan with N "
                "candidate findings issues at most `min(N, cap)` SDK "
                "calls; findings beyond the cap emit with "
                "`verdict=\"ambiguous\"`. Set to 0 to disable the judge "
                "entirely (deterministic-only fallback). Mirrors "
                "`AP2_JANITOR_MAX_FINDINGS_LLM`; read fresh at each "
                "janitor cron run via `cfg.get_component_value` (TB-330)."
            ),
            hot_reloadable=True,
        ),
        "judge_effort": ConfigKey(
            name="judge_effort",
            type=str,
            default="high",
            description=(
                "Per-judge effort label passed as `extra_args={'effort': "
                "<value>}` to the SDK options for each finding's judge "
                "call (TB-178). Falls back to `AP2_AGENT_EFFORT` then "
                "to `\"high\"` when unset. Mirrors "
                "`AP2_JANITOR_JUDGE_EFFORT`; read fresh at each judge "
                "call via `cfg.get_component_value` (TB-330)."
            ),
            hot_reloadable=True,
        ),
        "judge_max_turns": ConfigKey(
            name="judge_max_turns",
            type=int,
            default=12,
            description=(
                "Per-judge `ClaudeAgentOptions.max_turns` cap for the "
                "per-finding judge call (TB-178). Default 12 caps a "
                "single judge invocation; operators who want a tighter "
                "or looser budget can override. Mirrors "
                "`AP2_JANITOR_JUDGE_MAX_TURNS`; read fresh at each "
                "judge call via `cfg.get_component_value` (TB-330)."
            ),
            hot_reloadable=True,
        ),
    },
)
