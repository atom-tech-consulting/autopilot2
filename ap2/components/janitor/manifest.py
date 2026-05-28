"""Janitor component manifest (TB-309 canary).

Declares the janitor's registry-visible shape: env flag, default-enabled
state, hook points (the `tick_hook` the daemon dispatches on the
`janitor` cron job, plus the `status_findings_counts` data accessor the
CLI status command + status-report digest both consume).

`AP2_JANITOR_DISABLED` is the kill switch — set it to a truthy value
(`1`, `true`, etc.) to disable the janitor entirely. Default-on per
goal.md L64-67 (backwards-compat: existing operators don't need to opt
in to keep TB-177/TB-178 behavior). The pre-existing per-judge
knobs (`AP2_JANITOR_MAX_FINDINGS_LLM`, `AP2_JANITOR_JUDGE_EFFORT`,
`AP2_JANITOR_JUDGE_MAX_TURNS`) stay where they are — they tune the
judge's per-run cost/quality, not the component's enable state, and
are read fresh from `os.environ` at call-time inside `__init__.py`.

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
    # TB-321 (axis 1 canary): janitor is the first component to
    # declare a `config_schema`. The single key here mirrors the
    # existing `AP2_JANITOR_DISABLED` kill switch — same default
    # (`False` → janitor on), same semantic role. The schema entry
    # proves the end-to-end parse → aggregate → validate walk works
    # against a real manifest; TB-322 (axis 3) fills in the six
    # remaining components and TB-323 (axis 2) wires the env-var
    # back-compat map so `AP2_JANITOR_DISABLED` continues to override
    # the TOML value. Per-component reads at the
    # `cfg.components_config["janitor"]["disabled"]` shape land in
    # axis-(5) per-knob migrations — this TB only lays the read
    # paths (the dict shape on Config); the runtime still consults
    # `os.environ.get("AP2_JANITOR_DISABLED")` via
    # `Manifest.is_enabled()` until the per-knob migration TB.
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
    },
)
