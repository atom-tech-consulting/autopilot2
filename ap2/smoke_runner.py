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
  - **Failure-only alerting.** On failure ONLY, post a concise alert to
    Mattermost via the shared channel-adapter delivery path
    (`registry.channel_adapters(cfg)` — the same path the watchdog /
    attention pushes use), with the channel resolved like the
    status-report routine (`AP2_MM_REPORT_CHANNEL`, falling back to the
    first `AP2_MM_CHANNELS` entry). No post on success — `events.jsonl`
    carries the pass record, and a 6h "smokes OK" post would be noise
    alongside the 8h status-report digest.
"""
from __future__ import annotations

import os
import subprocess
import time

from . import events
from .config import Config
from .registry import default_registry


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
    """Best-effort post `text` to every enabled channel adapter.

    Walks `registry.channel_adapters(cfg)` (the same delivery path the
    watchdog's auto-diagnose + attention immediate-push use) and posts to
    each with the resolved report channel. Best-effort by design: a post
    failure is swallowed because the authoritative failure record already
    landed as a `smoke_check_failed` event before this call — losing the
    Mattermost ping must not also lose the audit trail or wedge the tick.
    When no adapter is registered (Mattermost disabled because
    `AP2_MM_CHANNELS` is unset) the walk is a silent no-op.
    """
    channel = _resolve_report_channel()
    adapters = default_registry().channel_adapters(cfg)
    for adapter in adapters:
        try:
            adapter.post(text, channel=channel)
        except Exception:  # noqa: BLE001 — best-effort; record already saved
            continue


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
