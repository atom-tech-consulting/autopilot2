"""Real-SDK smoke-check routine (TB-350).

The real-SDK smokes (`ap2/tests/smoke/`) make live Claude calls that
round-trip the actual SDK wiring — `cron_propose`, `pipeline_task_start`,
`report_result`, the prose-judge, and the validator-judge — against the
real model. They were pulled out of the per-task verification gate
(2026-05-30, `.cc-autopilot/env`: `AP2_VERIFY_CMD ... --ignore=ap2/tests/smoke`)
because intermittent live-service blips false-failed unrelated tasks
(TB-345, TB-346). But the smokes are still the only live-API canary for
SDK-wiring regressions, so this module hoists them onto a schedule.

`run_smoke_check(cfg)` is dispatched by `daemon.run_cron` when
`job.name == "real-sdk-smoke"` (interval 6h, shipped in
`cron.default.yaml`). It mirrors the shape of
`ap2.status_report.run_status_report` and `ap2.components.janitor.run_janitor`:
the cron-job body is a Python routine selected by `job.name`, NOT an LLM
agent. Running pytest is a deterministic shell action — and control / cron
agents have no Bash anyway (`ap2/prompts.py`: "control agents have no Bash").

Behavior:
  - **Inert-by-default.** If `AP2_REAL_SDK` is unset / falsey, emit a
    `smoke_check_skipped` event and return immediately — never run paid
    calls on an install that hasn't opted into the live smokes. This
    keeps the job a one-event no-op when shipped in `cron.default.yaml`
    for downstream OSS users who don't set the flag.
  - **Timeout-bounded subprocess.** Otherwise run
    `uv run --extra dev pytest -q ap2/tests/smoke/` as a subprocess in
    the project root, inheriting the daemon env (so `AP2_REAL_SDK=1`
    propagates to the child). The run is bounded by `cfg.verify_timeout_s`
    so a hung live SDK can't stall the tick loop.
  - **Outcome events.** Emit `smoke_check_passed` (with `duration_s`) on
    exit 0, or `smoke_check_failed` (with `exit_code` + `reason` +
    captured `failure_tail`) on non-zero exit / timeout.
  - **Codex coverage guard (TB-375).** A non-zero exit carrying the smoke
    harness's `CODEX_SKIP_GUARD_MARKER` is NOT an ordinary failure — it means
    codex was EXPECTED to run (`AP2_REAL_SDK` set, `openai_codex` importable, a
    codex credential present) but a codex-parametrized smoke variant skipped, so
    coverage silently eroded. That case is treated as a smoke FAILURE — it
    never emits `smoke_check_passed`; it emits a DISTINCT
    `smoke_check_codex_coverage_missing` alarm event (naming the skipped codex
    coverage) and posts an alert. When codex is legitimately absent (SDK not
    installed or no codex credential) the guard stays quiet and a Claude-only
    box still passes.
  - **Failure-only alerting.** On failure ONLY, ENQUEUE a concise alert
    onto the `ap2.notify` outbound queue (TB-389) — the communication
    component delivers it to Mattermost on its tick pass. The smoke runner
    never walks a channel-adapter list; it only appends a notification
    (channel resolved like the status-report routine —
    `AP2_MM_REPORT_CHANNEL`, falling back to the first `AP2_MM_CHANNELS`
    entry — as a destination hint). No post on success — `events.jsonl`
    carries the pass record, and a 6h "smokes OK" post would be noise
    alongside the 8h status-report digest.
"""
from __future__ import annotations

import os
import subprocess
import time

from . import events, notify
from .config import Config


# Tail length (chars) of the captured pytest output carried on a
# `smoke_check_failed` event + the Mattermost alert. pytest's `-q` summary
# (the `FAILED <nodeid> - <reason>` lines + the `N failed, M passed` tally)
# lives at the very end of the stream, so the tail is where the failing
# test names are; 2000 chars comfortably covers a multi-failure summary
# without dumping the full run log into events.jsonl.
_FAILURE_TAIL_CHARS = 2000

# The smoke suite invocation. A list (not a shell string) so there's no
# shell-quoting surface; `cwd=cfg.project_root` + inherited `os.environ`
# means `AP2_REAL_SDK=1` propagates to the child exactly as the daemon
# sees it.
_SMOKE_CMD: list[str] = [
    "uv", "run", "--extra", "dev", "pytest", "-q", "ap2/tests/smoke/",
]


# TB-375: the line the smoke harness's session-scoped codex-coverage guard
# (`ap2/tests/smoke/conftest.py` → `ap2/tests/smoke/_codex_guard.py`) prints to
# stdout when codex was EXPECTED to run (`AP2_REAL_SDK` set, `openai_codex`
# importable, a codex credential present) but a codex-parametrized smoke variant
# nonetheless reported `skipped` — i.e. codex coverage silently eroded. The
# guard also forces a non-zero pytest exit, so this marker is how `run_smoke_check`
# distinguishes "codex coverage went missing" (a distinct alarm) from an ordinary
# test failure. One source of truth, imported by the guard so the contract can't
# drift. It is a stdout SENTINEL, not an env knob — deliberately NOT spelled in
# the `AP2_*` env-knob namespace so the docs/coverage-drift scanners don't read
# it as an operator knob. Presence-only — the guard never reads or logs any
# token contents.
CODEX_SKIP_GUARD_MARKER = "SMOKE-CODEX-COVERAGE-MISSING"


def _codex_skip_detail(output: str) -> str:
    """Return the guard's marker line from `output`, or the bare marker.

    The guard prints a single line beginning with `CODEX_SKIP_GUARD_MARKER`
    that names the skipped codex variants; surface it verbatim on the alarm
    event so the operator sees which coverage went missing.
    """
    for line in output.splitlines():
        if CODEX_SKIP_GUARD_MARKER in line:
            return line.strip()
    return CODEX_SKIP_GUARD_MARKER


def _real_sdk_enabled() -> bool:
    """True when `AP2_REAL_SDK` is set to a non-falsey value.

    Mirrors the env_flag polarity the registry uses (`registry.py`: a knob
    is on unless its value is one of the documented falsey strings). The
    smokes themselves gate on `not os.environ.get("AP2_REAL_SDK")`, so any
    non-empty value turns them on; we additionally treat the common
    falsey spellings as off so an operator who wrote `AP2_REAL_SDK=0` to
    disable the job gets the inert path, not a paid run.

    `AP2_REAL_SDK` is in `config_compat._KNOBS_STAYING_ENV_ONLY` (the
    12-factor exempt set), so reading it directly from `os.environ` is the
    sanctioned path — it never migrates to TOML.
    """
    raw = os.environ.get("AP2_REAL_SDK", "")
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _resolve_report_channel() -> str:
    """Resolve the alert destination channel, mirroring the status-report
    routine's preference order: `AP2_MM_REPORT_CHANNEL` first, then the
    first entry of `AP2_MM_CHANNELS`. Returns "" when neither is set — the
    caller passes the result through to each adapter's `.post(channel=...)`,
    and the Mattermost adapter falls back to its own `AP2_MM_CHANNELS[0]`
    lookup when handed an empty channel.

    Both knobs are Mattermost channel-identity and live in
    `config_compat._KNOBS_STAYING_ENV_ONLY`, so the direct `os.environ`
    reads are sanctioned.
    """
    channel = os.environ.get("AP2_MM_REPORT_CHANNEL", "").strip()
    if channel:
        return channel
    raw = os.environ.get("AP2_MM_CHANNELS", "").strip()
    for c in raw.split(","):
        c = c.strip()
        if c:
            return c
    return ""


def _post_failure_alert(cfg: Config, text: str) -> None:
    """Enqueue `text` onto the outbound notification queue (TB-389).

    Pre-TB-389 this walked `registry.channel_adapters(cfg)` and posted
    synchronously. TB-389 folds the channel surface behind the
    `communication` component: the smoke runner appends an undelivered
    notification to the `ap2.notify` queue (a pure filesystem write — no
    channel reference, no `ap2.components.*` import) and the communication
    component's tick pass delivers it. The resolved report channel rides
    along as a destination hint; the Mattermost channel adapter falls back
    to `AP2_MM_CHANNELS[0]` when it is empty. Best-effort by design: the
    authoritative failure record already landed as a `smoke_check_failed`
    event before this call, and the queue delivers on the next tick.
    """
    channel = _resolve_report_channel()
    notify.enqueue(cfg, text, channel=channel, kind="smoke_alert")


async def run_smoke_check(cfg: Config) -> None:
    """Run the real-SDK smoke suite (TB-350).

    Dispatched from `daemon.run_cron` for the `real-sdk-smoke` cron job.
    See the module docstring for the full behavior contract. Emits exactly
    one of `smoke_check_skipped` / `smoke_check_passed` / `smoke_check_failed`
    per invocation; posts a Mattermost alert ONLY on the failed path.
    """
    if not _real_sdk_enabled():
        # Inert-by-default: no paid calls on installs that haven't opted in.
        events.append(
            cfg.events_file,
            "smoke_check_skipped",
            reason="AP2_REAL_SDK unset",
        )
        return

    timeout_s = cfg.verify_timeout_s
    started = time.monotonic()
    try:
        proc = subprocess.run(
            _SMOKE_CMD,
            cwd=str(cfg.project_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        duration_s = round(time.monotonic() - started, 3)
        # `TimeoutExpired.output` / `.stderr` carry whatever the child
        # flushed before the kill; coalesce to "" so the tail is always a
        # string.
        partial = (exc.output or "") + (exc.stderr or "")
        if isinstance(partial, bytes):  # text=True normally yields str
            partial = partial.decode("utf-8", "replace")
        tail = partial[-_FAILURE_TAIL_CHARS:]
        events.append(
            cfg.events_file,
            "smoke_check_failed",
            reason="timeout",
            exit_code=-1,
            timeout_s=timeout_s,
            duration_s=duration_s,
            failure_tail=tail,
        )
        _post_failure_alert(
            cfg,
            f"⚠ [{cfg.project_name}] real-SDK smoke check TIMED OUT after "
            f"{timeout_s}s (`{' '.join(_SMOKE_CMD)}`).\n\n"
            f"```\n{tail}\n```",
        )
        return

    duration_s = round(time.monotonic() - started, 3)
    combined = (proc.stdout or "") + (proc.stderr or "")

    # TB-375: expected-but-skipped codex coverage is a smoke FAILURE, never a
    # pass. The session-scoped guard forces a non-zero exit AND prints
    # `CODEX_SKIP_GUARD_MARKER`; on that marker emit a DISTINCT alarm event that
    # names the skipped codex coverage (not the generic `smoke_check_failed`),
    # so "codex was supposed to run and didn't" can't masquerade as green. This
    # is checked before the `returncode == 0` pass branch on purpose: a
    # green-by-skipping run could otherwise read as a pass.
    if CODEX_SKIP_GUARD_MARKER in combined:
        detail = _codex_skip_detail(combined)
        tail = combined[-_FAILURE_TAIL_CHARS:]
        events.append(
            cfg.events_file,
            "smoke_check_codex_coverage_missing",
            reason="codex_expected_but_skipped",
            exit_code=proc.returncode,
            duration_s=duration_s,
            skipped_coverage=detail,
            failure_tail=tail,
        )
        _post_failure_alert(
            cfg,
            f"⚠ [{cfg.project_name}] real-SDK smoke check FAILED — codex "
            f"coverage SKIPPED while codex was expected to run "
            f"(AP2_REAL_SDK set, `openai_codex` importable, codex credential "
            f"present). Coverage erosion is NOT a pass.\n\n"
            f"{detail}\n\n"
            f"```\n{tail}\n```",
        )
        return

    if proc.returncode == 0:
        events.append(
            cfg.events_file,
            "smoke_check_passed",
            duration_s=duration_s,
        )
        return

    tail = combined[-_FAILURE_TAIL_CHARS:]
    events.append(
        cfg.events_file,
        "smoke_check_failed",
        reason="nonzero_exit",
        exit_code=proc.returncode,
        duration_s=duration_s,
        failure_tail=tail,
    )
    _post_failure_alert(
        cfg,
        f"⚠ [{cfg.project_name}] real-SDK smoke check FAILED "
        f"(exit {proc.returncode}, `{' '.join(_SMOKE_CMD)}`).\n\n"
        f"```\n{tail}\n```",
    )
