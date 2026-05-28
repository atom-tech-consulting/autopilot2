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

from ap2.registry import Manifest

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
    dependencies=[],
)
