"""`ap2 doctor` — one-shot readiness check for the ap2 setup.

Combines `user_audit` (does the sandbox user exist + is it cred-clean?) with
`project_audit` (is there a sandbox clone of THIS project at the expected
path?), plus a check that the `ap2` CLI is installed for the sandbox user.

The output is a flat list of OK / FAIL / WARN / INFO lines designed to
replace the manual environment-check ladder previously done in markdown by
the setup-project skill.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, DEFAULT_VERIFY_TIMEOUT_S, EVENTS_FILE
from .sandbox import (
    AuditResult,
    DEFAULT_USER,
    _user_exists,
    _user_home,
    _user_login_shell,
    project_audit,
    user_audit,
)


@dataclass
class DoctorReport:
    sections: list[tuple[str, AuditResult]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for _, s in self.sections)

    def print(self) -> None:
        for title, res in self.sections:
            print(f"[{title}]")
            res.print()
            print()
        print("doctor: " + ("OK" if self.ok else "FAIL — see above"))


def _ap2_installed_for_user(user: str) -> AuditResult:
    res = AuditResult()
    if not _user_exists(user):
        res.add("FAIL", f"user {user!r} does not exist (run: ap2 sandbox user-setup)")
        return res
    # Probe via the user's actual login shell — `uv tool install` puts
    # `~/.local/bin` on PATH via `~/.zshenv` for zsh users, and a bash
    # probe wouldn't source it. See sandbox._user_login_shell for the
    # full rationale.
    shell = _user_login_shell(user)
    r = subprocess.run(
        ["sudo", "-u", user, "-i", shell, "-c", "command -v ap2 || true"],
        capture_output=True, text=True,
    )
    path = r.stdout.strip()
    if path:
        res.add("OK", f"ap2 CLI on $PATH for {user}: {path}")
    else:
        res.add(
            "FAIL",
            f"ap2 not on $PATH for {user}. As that user run: "
            f"uv tool install --from <path-to-claude-tools> 'claude-automation[ap2]'",
        )
    return res


def _project_init_state(project_root: Path) -> AuditResult:
    """Verify the local clone has the bare-minimum on-disk skeleton.

    Doesn't try to run `ap2 init` for the user — just reports what's missing
    so the next-step is obvious.
    """
    res = AuditResult()
    expected = [
        (project_root / "TASKS.md", "TASKS.md"),
        (project_root / ".cc-autopilot", ".cc-autopilot/"),
        (project_root / ".cc-autopilot" / "progress.md", ".cc-autopilot/progress.md"),
        (project_root / ".cc-autopilot" / "tasks", ".cc-autopilot/tasks/"),
        (project_root / "CLAUDE.md", "CLAUDE.md"),
    ]
    missing = [name for path, name in expected if not path.exists()]
    if missing:
        res.add("FAIL", f"missing: {', '.join(missing)} — run: ap2 init")
        return res

    # CLAUDE.md exists but make sure it has the Autopilot section the daemon reads.
    text = (project_root / "CLAUDE.md").read_text()
    if "## Autopilot" not in text:
        res.add("FAIL", "CLAUDE.md has no `## Autopilot` section — run: ap2 init")
    else:
        res.add("OK", "project skeleton in place (TASKS.md, progress.md, autopilot config)")
    return res


def _sandbox_clone_path(project_root: Path, user: str) -> Path | None:
    """Where the sandbox user's clone of this project SHOULD live."""
    home = _user_home(user)
    if home is None:
        return None
    return home / "repos" / project_root.resolve().name


def _parse_positive_int(raw: str) -> int:
    """Mirror `_per_task_token_cap` / `_window_token_cap` parse semantics
    (ap2/daemon.py:2581-2614): unset / empty / non-integer / non-positive
    → 0 (disabled). Doctor reusing the same shape avoids the failure mode
    where doctor reports OK on a value the daemon will treat as disabled.
    """
    s = (raw or "").strip()
    if not s:
        return 0
    try:
        v = int(s)
    except ValueError:
        return 0
    return v if v > 0 else 0


def _truthy(raw: str) -> bool:
    """Same shape as the daemon's `_truthy` env parse for
    `AP2_AUTO_APPROVE` (`1` / `true` / `yes`, case-insensitive)."""
    return (raw or "").strip().lower() in ("1", "true", "yes")


def auto_approve_audit() -> AuditResult:
    """Pre-flight check on `AP2_AUTO_APPROVE` + token-cap configuration.

    Goal.md L102-113 frames axis-3 cost guards (per-task cap, window cap,
    regression pauses) as the safety floor that lets auto-approve ship
    bounded blast-radius. `_per_task_token_cap` / `_window_token_cap`
    (daemon.py:2581-2614) deliberately return 0 ("disabled") on unset, so
    an operator can enable auto-approve without realizing the floor is
    OFF. This audit fail-loud surfaces that misconfiguration at pre-flight
    time. WARN, not FAIL: operator authority preserved per goal.md
    L184-186 — doctor warns, doesn't refuse to run.
    """
    res = AuditResult()
    enabled_raw = os.environ.get("AP2_AUTO_APPROVE", "")
    if not _truthy(enabled_raw):
        res.add(
            "INFO",
            "auto-approve disabled (AP2_AUTO_APPROVE unset) — "
            "manual approve required per task",
        )
        return res

    per_task = _parse_positive_int(os.environ.get("AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP", ""))
    window = _parse_positive_int(os.environ.get("AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP", ""))

    if per_task > 0:
        res.add("OK", f"AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP={per_task}")
    else:
        res.add(
            "WARN",
            "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP unset/zero — per-task cost "
            "ceiling DISABLED. Fix: export "
            "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP=<budget>",
        )

    if window > 0:
        res.add("OK", f"AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP={window}")
    else:
        res.add(
            "WARN",
            "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP unset/zero — 24h rolling-"
            "window cost ceiling DISABLED. Fix: export "
            "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP=<budget>",
        )

    if per_task == 0 and window == 0:
        res.add(
            "WARN",
            "auto-approve enabled with no cost ceiling — safety floor OFF; "
            "see goal.md L102-113 for rationale",
        )
    return res


def _parse_nonneg_int_with_default(raw: str, default: int) -> int:
    """Mirror `_auto_unfreeze_max_per_task` / `_auto_unfreeze_max_per_day`
    parse semantics (ap2/daemon.py:3084-3091 / 3109-3116): empty /
    non-integer / negative falls back to `default`; non-negative integers
    are honored (including 0, which the daemon treats as "cap disabled").
    Doctor reusing the same shape avoids the failure mode where doctor
    reports a cap value the daemon will treat differently.
    """
    s = (raw or "").strip()
    if not s:
        return default
    try:
        v = int(s)
    except ValueError:
        return default
    return v if v >= 0 else default


def auto_unfreeze_audit() -> AuditResult:
    """Pre-flight check on `AP2_AUTO_UNFREEZE_FIX_SHAPES` +
    `AP2_AUTO_UNFREEZE_DRY_RUN` configuration (TB-239, axis-2 mirror
    of `auto_approve_audit()`).

    `_maybe_auto_unfreeze` (daemon.py:3301-3303) silently early-returns
    when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty — EVEN when
    `AP2_AUTO_UNFREEZE_DRY_RUN=1` is set. An operator who flips dry-run
    expecting observation gets a silent no-op (zero
    `would_auto_unfreeze` events, zero `auto_unfreeze_skipped` events,
    no doctor warning). This audit fail-loud surfaces that
    misconfiguration at pre-flight time. WARN, not FAIL: operator
    authority preserved per goal.md L184-186 — doctor warns, doesn't
    refuse to run.

    Note on default asymmetry vs `auto_approve_audit()`: axis-1
    defaults are permissive (caps default to 0 = disabled = unbounded),
    so enabling auto-approve without caps is the loud-warn shape.
    Axis-2 defaults are conservative (allowlist defaults to empty =
    no-op; per-task cap defaults to 1; per-day cap defaults to 3), so
    the loud-warn shape here is flipping the dry-run knob without
    populating the allowlist (silent no-op).
    """
    res = AuditResult()
    allowlist_raw = os.environ.get("AP2_AUTO_UNFREEZE_FIX_SHAPES", "").strip()
    shapes = [s.strip() for s in allowlist_raw.split(",") if s.strip()]
    dry_run = _truthy(os.environ.get("AP2_AUTO_UNFREEZE_DRY_RUN", ""))
    per_task_cap = _parse_nonneg_int_with_default(
        os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_TASK", ""), 1,
    )
    per_day_cap = _parse_nonneg_int_with_default(
        os.environ.get("AP2_AUTO_UNFREEZE_MAX_PER_DAY", ""), 3,
    )

    if not shapes and not dry_run:
        # Default-off case: feature unconfigured, no operator engagement.
        res.add(
            "INFO",
            "auto-unfreeze disabled (allowlist unset) — "
            "set AP2_AUTO_UNFREEZE_FIX_SHAPES=<comma-list> to opt in",
        )
        return res

    if not shapes and dry_run:
        # The misconfiguration shape: dry-run set without allowlist.
        # `_maybe_auto_unfreeze` (daemon.py:3301-3303) early-returns
        # silently on empty allowlist BEFORE the dry-run check at
        # daemon.py:3416 — zero observable events, silent no-op.
        res.add(
            "WARN",
            "auto-unfreeze dry-run set without allowlist — silent no-op. "
            "`_maybe_auto_unfreeze` (ap2/daemon.py:3301-3303) early-"
            "returns on empty allowlist BEFORE the dry-run check, so "
            "zero `would_auto_unfreeze` events fire. Fix: set "
            "AP2_AUTO_UNFREEZE_FIX_SHAPES=<comma-list> before dry-run "
            "will emit observable decisions.",
        )
        return res

    # From here shapes is non-empty.
    n = len(shapes)
    if dry_run:
        res.add(
            "INFO",
            f"auto-unfreeze dry-run armed: {n} shapes, "
            f"per-task cap {per_task_cap}, per-day cap {per_day_cap}",
        )
    else:
        res.add(
            "INFO",
            f"auto-unfreeze live: {n} shapes, "
            f"per-task cap {per_task_cap}, per-day cap {per_day_cap}",
        )
    return res


def _verify_gate_state() -> AuditResult:
    """Report whether AP2_VERIFY_CMD is configured (project-wide regression gate).

    The gate is opt-in — unset is the documented default and not a problem;
    an INFO line just tells the operator how to enable it. When set, OK with
    the resolved command + timeout so the human can verify what the daemon
    will actually run.
    """
    res = AuditResult()
    cmd = os.environ.get("AP2_VERIFY_CMD", "").strip()
    timeout = int(os.environ.get("AP2_VERIFY_TIMEOUT_S", DEFAULT_VERIFY_TIMEOUT_S))
    if not cmd:
        res.add(
            "INFO",
            "AP2_VERIFY_CMD unset — project-wide verify gate disabled. "
            "To enable, add e.g. `AP2_VERIFY_CMD=uv run pytest -q` to "
            ".cc-autopilot/env.",
        )
    else:
        res.add("OK", f"AP2_VERIFY_CMD: {cmd!r} (timeout {timeout}s)")
    return res


# TB-252: window + band constants for `verify_timeout_audit`. Internal
# constants (no env knobs) — the audit's job is to surface
# `AP2_VERIFY_TIMEOUT_S` misconfiguration, not to introduce another knob
# that itself can be misconfigured. Lives at module scope alongside the
# audit function so the values are easy to find when tuning.
#
# `_VERIFY_TIMEOUT_AUDIT_WINDOW_DAYS` / `_VERIFY_TIMEOUT_AUDIT_MIN_SAMPLES`
# define the sample window per the briefing: "last 7 days OR last 20
# successful samples, whichever covers more". Implementation reads the
# tail and applies both filters, picking the LARGER resulting set so a
# burst week + a slow week both surface adequate signal.
#
# `_VERIFY_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES` is the floor below which
# the audit emits INFO ("insufficient data") rather than WARN — avoids
# false-positives on fresh installs where one slow run would otherwise
# trip the alarm. Three samples is the briefing-spec'd floor.
#
# `_VERIFY_TIMEOUT_AUDIT_WARN_RATIO` / `_VERIFY_TIMEOUT_AUDIT_INFO_RATIO`
# are the headroom bands: ratio < 1.0 → WARN (timeout below worst-case
# successful run); ratio < 1.5 → INFO "tight" (some headroom but
# operator should consider bumping); else INFO "comfortable".
_VERIFY_TIMEOUT_AUDIT_WINDOW_DAYS = 7
_VERIFY_TIMEOUT_AUDIT_MAX_SAMPLES = 20
_VERIFY_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES = 3
_VERIFY_TIMEOUT_AUDIT_WARN_RATIO = 1.0
_VERIFY_TIMEOUT_AUDIT_INFO_RATIO = 1.5
# Recommendation multiplier when emitting the WARN fix line: bump the
# operator's timeout to ceil(typical * 1.5) so the new floor has a
# 50% safety margin over the observed-typical worst-case.
_VERIFY_TIMEOUT_AUDIT_FIX_MULT = 1.5


def _iter_verify_passed_durations(
    events_file: Path,
    *,
    window_days: int,
    max_samples: int,
    now: _dt.datetime | None = None,
) -> tuple[list[float], int]:
    """Return (durations, sample_days) for recent successful project-wide
    verify runs (`verify_passed` events emitted at daemon.py's
    post-`_run_verify` branch, TB-252).

    Window selection: take the tail of events.jsonl, filter to
    `verify_passed` rows with a numeric `duration_s` field, then choose
    whichever of (last `max_samples` samples) and (samples within the
    last `window_days` days) yields the LARGER set — the briefing's
    "whichever covers more" rule. `sample_days` returned is the actual
    span (days, ceiling) of the chosen sample set, used for the
    audit's "n=N over D days" attribution.

    Returns ([], 0) when the events file doesn't exist or no qualifying
    events are present — the audit treats that as "insufficient data"
    (INFO branch).
    """
    if not events_file.exists():
        return [], 0

    # Tail-scan up to a generous bound so the 7d window can capture a
    # weeks-quiet project. 5000 lines is well under 1MB on real
    # events.jsonl shapes (each event is a few hundred bytes); pulling
    # the whole tail in one shot is simpler than a streaming filter.
    # If a project's events.jsonl is so large that even 5000 lines'
    # tail-read is concerning, the audit's INFO-on-insufficient branch
    # still degrades safely.
    cutoff_dt: _dt.datetime | None = None
    if window_days > 0:
        now = now or _dt.datetime.now(_dt.timezone.utc)
        cutoff_dt = now - _dt.timedelta(days=window_days)

    recent_samples: list[tuple[_dt.datetime | None, float]] = []
    try:
        with events_file.open("r") as f:
            # Read all lines; events.jsonl is append-only and bounded
            # by project age. For very large files the test gate
            # `test_verify_timeout_audit_handles_missing_events_file`
            # exercises the missing-file branch; the present branch
            # always has a path that exists at this point.
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict):
                    continue
                if evt.get("type") != "verify_passed":
                    continue
                dur = evt.get("duration_s")
                if not isinstance(dur, (int, float)):
                    continue
                ts_raw = evt.get("ts")
                ts_dt: _dt.datetime | None = None
                if isinstance(ts_raw, str):
                    try:
                        ts_dt = _dt.datetime.strptime(
                            ts_raw, "%Y-%m-%dT%H:%M:%SZ",
                        ).replace(tzinfo=_dt.timezone.utc)
                    except ValueError:
                        ts_dt = None
                recent_samples.append((ts_dt, float(dur)))
    except OSError:
        return [], 0

    # Compute the two candidate sample sets:
    #   A) last `max_samples` (regardless of age)
    #   B) all samples within `window_days`
    # Return whichever has more elements (ties → A).
    by_recent = recent_samples[-max_samples:] if max_samples > 0 else []
    by_window = (
        [s for s in recent_samples if s[0] is not None and s[0] >= cutoff_dt]
        if cutoff_dt is not None
        else []
    )
    chosen = by_window if len(by_window) > len(by_recent) else by_recent

    durations = [d for _, d in chosen]
    # Sample-span days: from the earliest dated sample in the chosen set
    # to "now", rounded up. Falls back to window_days when no sample
    # carries a parseable timestamp (rare — the canonical writer always
    # stamps).
    dated = [t for t, _ in chosen if t is not None]
    if dated:
        oldest = min(dated)
        now = now or _dt.datetime.now(_dt.timezone.utc)
        span = max(1, math.ceil((now - oldest).total_seconds() / 86400.0))
    else:
        span = window_days
    return durations, span


def verify_timeout_audit(state_dir: Path, cfg: Config) -> AuditResult:
    """Pre-flight check on `AP2_VERIFY_TIMEOUT_S` vs observed-typical
    successful full-suite verify duration (TB-252, axis-2 mirror of
    TB-234's auto-approve cap audit + TB-239's auto-unfreeze allowlist
    audit).

    Anchored to the 2026-05-17 retry_exhausted cascade (TB-245 / 246 /
    247 / 249 / 250) where the project-wide verifier killed five
    consecutive task runs at the 600s `AP2_VERIFY_TIMEOUT_S` default
    while the actual `uv run pytest -q ap2/tests/` suite took
    1320-1349s on a healthy commit. Goal.md axis 2 (failure-recovery
    operator dependency, L88-100) commits the harness to surfacing
    misconfiguration before it cascades; this audit closes the
    env-knob-vs-current-workload gap that TB-225 BriefingFix /
    TB-233 dry-run / TB-239 misconfiguration-floor surfaces don't
    catch (they audit static env config, not workload-relative fit).

    Reads `.cc-autopilot/events.jsonl` for `verify_passed` events
    (emitted by daemon.py's post-`_run_verify` success branch) within
    the last `_VERIFY_TIMEOUT_AUDIT_WINDOW_DAYS` OR up to
    `_VERIFY_TIMEOUT_AUDIT_MAX_SAMPLES` recent samples, whichever
    yields more. Uses `max()` over durations (NOT `mean()` — the
    worst-case successful run is the realistic ceiling for sizing
    the timeout; a 1349s P100 matters more than an 850s mean when
    the timeout is 600s).

    Verdict bands:
      - <3 samples → INFO "insufficient data" (avoids false-positives
        on fresh installs).
      - timeout < typical * 1.0 → WARN with one-line fix
        recommending `ceil(typical * 1.5)`.
      - typical * 1.0 ≤ timeout < typical * 1.5 → INFO "tight
        headroom".
      - timeout ≥ typical * 1.5 → INFO "comfortable headroom".

    WARN (not FAIL) per goal.md L184-186: operator authority
    preserved; doctor warns, doesn't refuse to run.
    """
    res = AuditResult()
    events_file = state_dir / EVENTS_FILE
    timeout = int(cfg.verify_timeout_s)
    durations, sample_days = _iter_verify_passed_durations(
        events_file,
        window_days=_VERIFY_TIMEOUT_AUDIT_WINDOW_DAYS,
        max_samples=_VERIFY_TIMEOUT_AUDIT_MAX_SAMPLES,
    )
    n = len(durations)
    if n < _VERIFY_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES:
        res.add(
            "INFO",
            f"insufficient data to assess `AP2_VERIFY_TIMEOUT_S` "
            f"headroom (n={n} successful verify samples; need "
            f">={_VERIFY_TIMEOUT_AUDIT_INSUFFICIENT_SAMPLES})",
        )
        return res

    # Worst-case successful run is what blows up the timeout. Don't use
    # mean/median — the P100 matters here, not central tendency.
    typical = max(durations)
    if timeout < typical * _VERIFY_TIMEOUT_AUDIT_WARN_RATIO:
        recommended = int(math.ceil(typical * _VERIFY_TIMEOUT_AUDIT_FIX_MULT))
        res.add(
            "WARN",
            f"AP2_VERIFY_TIMEOUT_S={timeout}s is below observed-"
            f"typical successful verify duration ({typical:.0f}s, "
            f"n={n} samples over {sample_days} days); recommend "
            f"`export AP2_VERIFY_TIMEOUT_S={recommended}` and "
            f"`ap2 unfreeze TB-N` for any 600s-timeout-shape Frozen "
            f"tasks.",
        )
        return res

    if timeout < typical * _VERIFY_TIMEOUT_AUDIT_INFO_RATIO:
        # "Tight" band — operator survived the worst case but margin is
        # below the recommended 1.5× safety buffer. INFO, not WARN: no
        # active failure to surface, just a nudge.
        headroom_pct = (timeout / typical - 1.0) * 100.0
        res.add(
            "INFO",
            f"AP2_VERIFY_TIMEOUT_S={timeout}s has {headroom_pct:.0f}% "
            f"headroom over recent verifies (observed-typical "
            f"{typical:.0f}s, n={n} samples over {sample_days} days) "
            f"— consider bumping for safety margin.",
        )
        return res

    res.add(
        "INFO",
        f"AP2_VERIFY_TIMEOUT_S={timeout}s has comfortable headroom "
        f"over observed-typical {typical:.0f}s (n={n} samples over "
        f"{sample_days} days).",
    )
    return res


def diagnose(
    project_root: Path,
    user: str = DEFAULT_USER,
    cfg: Config | None = None,
) -> DoctorReport:
    report = DoctorReport()

    report.sections.append(("project skeleton", _project_init_state(project_root)))
    report.sections.append(("verify gate", _verify_gate_state()))
    # TB-252: workload-relative timeout fit. Section sits next to the
    # static "verify gate" config check so the operator sees both the
    # gate's command (what runs) and the gate's timeout headroom (how
    # long it has to run) as a paired block. `cfg` is optional for
    # backward-compat with legacy test fixtures; when absent we
    # synthesize a minimal cfg from the project root + env so the
    # audit still runs against today's `AP2_VERIFY_TIMEOUT_S`.
    if cfg is None:
        cfg_for_audit = Config.load(project_root)
    else:
        cfg_for_audit = cfg
    report.sections.append((
        "verify timeout headroom",
        verify_timeout_audit(project_root, cfg_for_audit),
    ))
    report.sections.append(("auto-approve safety floor", auto_approve_audit()))
    report.sections.append(("auto-unfreeze safety floor", auto_unfreeze_audit()))
    report.sections.append((f"sandbox user ({user})", user_audit(user)))
    report.sections.append((f"ap2 CLI for {user}", _ap2_installed_for_user(user)))

    sb_path = _sandbox_clone_path(project_root, user)
    if sb_path is None:
        miss = AuditResult()
        miss.add("FAIL", f"cannot resolve home for {user!r}")
        report.sections.append(("sandbox clone", miss))
    else:
        if sb_path.exists():
            report.sections.append((f"sandbox clone ({sb_path})", project_audit(sb_path, user)))
        else:
            miss = AuditResult()
            miss.add(
                "INFO",
                f"sandbox clone not found at {sb_path} — run: "
                f"ap2 sandbox project-setup {project_root}",
            )
            report.sections.append(("sandbox clone", miss))

    return report
