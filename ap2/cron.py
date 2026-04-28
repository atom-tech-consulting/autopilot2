"""Cron registry backed by `.cc-autopilot/cron.yaml`.

The daemon reads this each tick to discover scheduled jobs. Agents mutate it via
the `cron_edit` custom tool. Last-run timestamps live in a separate state file
so editing `cron.yaml` doesn't clobber them.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml


_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])?\s*$")


def parse_interval(s: str | int) -> int:
    """Parse '30m', '2h', '45s', '1d', or a bare int (seconds)."""
    if isinstance(s, int):
        return s
    m = _INTERVAL_RE.match(str(s))
    if not m:
        raise ValueError(f"bad interval: {s!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


VALID_TRIGGERS = ("interval", "empty_board")


@dataclass
class CronJob:
    name: str
    interval_s: int
    prompt: str
    trigger: str = "interval"
    active_when: str | None = None
    max_turns: int = 15
    created_by: str | None = None
    created_at: str | None = None
    allowed_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "CronJob":
        trigger = d.get("trigger", "interval")
        if trigger not in VALID_TRIGGERS:
            raise ValueError(f"unknown trigger {trigger!r}; expected one of {VALID_TRIGGERS}")
        # interval is irrelevant for non-interval triggers but we keep a value
        # on the dataclass for serialization symmetry; default it to the
        # cooldown ceiling so any accidental due_jobs path remains harmless.
        return cls(
            name=d["name"],
            interval_s=parse_interval(d.get("interval", "1h")),
            prompt=d.get("prompt", "").strip(),
            trigger=trigger,
            active_when=d.get("active_when"),
            max_turns=int(d.get("max_turns", 15)),
            created_by=d.get("created_by"),
            created_at=d.get("created_at"),
            allowed_tools=list(d.get("allowed_tools", [])),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "prompt": self.prompt,
            "max_turns": self.max_turns,
        }
        if self.trigger == "interval":
            d["interval"] = _interval_str(self.interval_s)
            if self.active_when:
                d["active_when"] = self.active_when
        else:
            d["trigger"] = self.trigger
        if self.created_by:
            d["created_by"] = self.created_by
        if self.created_at:
            d["created_at"] = self.created_at
        if self.allowed_tools:
            d["allowed_tools"] = self.allowed_tools
        return d


def _interval_str(seconds: int) -> str:
    for unit, factor in [("d", 86400), ("h", 3600), ("m", 60)]:
        if seconds % factor == 0 and seconds >= factor:
            return f"{seconds // factor}{unit}"
    return f"{seconds}s"


def load_jobs(cron_file: Path) -> list[CronJob]:
    if not cron_file.exists():
        return []
    data = yaml.safe_load(cron_file.read_text()) or {}
    jobs = data.get("jobs", []) or []
    return [CronJob.from_dict(j) for j in jobs]


_DEFAULT_CRON_FILE = Path(__file__).parent / "cron.default.yaml"


def bootstrap(cron_file: Path) -> bool:
    """Copy the packaged default cron.yaml into place if `cron_file` is missing.

    Returns True if a copy was made, False if the file already existed or the
    default is unavailable.
    """
    if cron_file.exists():
        return False
    if not _DEFAULT_CRON_FILE.exists():
        return False
    cron_file.parent.mkdir(parents=True, exist_ok=True)
    cron_file.write_text(_DEFAULT_CRON_FILE.read_text())
    return True


def save_jobs(cron_file: Path, jobs: list[CronJob]) -> None:
    cron_file.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        {"jobs": [j.to_dict() for j in jobs]},
        sort_keys=False,
        default_flow_style=False,
    )
    cron_file.write_text(text)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def update_job(cron_file: Path, action: str, **kw: Any) -> tuple[str, list[CronJob]]:
    """Mutate the cron registry.

    Supported actions: add, remove, update. Returns `(message, current_jobs)`.
    """
    with _locked(cron_file):
        jobs = load_jobs(cron_file)
        if action == "add":
            name = kw["name"]
            if any(j.name == name for j in jobs):
                raise ValueError(f"job {name!r} already exists")
            mt = kw.get("max_turns")
            trigger = kw.get("trigger") or "interval"
            if trigger not in VALID_TRIGGERS:
                raise ValueError(f"unknown trigger {trigger!r}; expected one of {VALID_TRIGGERS}")
            interval_raw = kw.get("interval")
            if trigger == "interval" and interval_raw is None:
                raise ValueError("interval is required for trigger='interval'")
            interval_s = parse_interval(interval_raw) if interval_raw is not None else 3600
            jobs.append(CronJob(
                name=name,
                interval_s=interval_s,
                prompt=kw["prompt"].strip(),
                trigger=trigger,
                active_when=kw.get("active_when"),
                max_turns=int(mt) if mt else 15,
                created_by=kw.get("created_by"),
                created_at=kw.get("created_at") or _now(),
                allowed_tools=list(kw.get("allowed_tools") or []),
            ))
            msg = f"added cron job {name!r}"
        elif action == "remove":
            name = kw["name"]
            before = len(jobs)
            jobs = [j for j in jobs if j.name != name]
            if len(jobs) == before:
                raise KeyError(f"job {name!r} not found")
            msg = f"removed cron job {name!r}"
        elif action == "update":
            name = kw["name"]
            target = next((j for j in jobs if j.name == name), None)
            if target is None:
                raise KeyError(f"job {name!r} not found")
            if "trigger" in kw and kw["trigger"] is not None:
                if kw["trigger"] not in VALID_TRIGGERS:
                    raise ValueError(f"unknown trigger {kw['trigger']!r}; expected one of {VALID_TRIGGERS}")
                target.trigger = kw["trigger"]
            if "interval" in kw and kw["interval"] is not None:
                target.interval_s = parse_interval(kw["interval"])
            if "prompt" in kw and kw["prompt"] is not None:
                target.prompt = kw["prompt"].strip()
            if "active_when" in kw:
                target.active_when = kw["active_when"]
            if "max_turns" in kw and kw["max_turns"] is not None:
                target.max_turns = int(kw["max_turns"])
            msg = f"updated cron job {name!r}"
        else:
            raise ValueError(f"unknown action {action!r}")
        save_jobs(cron_file, jobs)
        return msg, jobs


# ---- scheduling state ----


def load_state(state_file: Path) -> dict[str, float]:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_file: Path, state: dict[str, float]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True))


def due_jobs(
    jobs: list[CronJob],
    state: dict[str, float],
    project_root: Path,
    *,
    now: float | None = None,
) -> list[CronJob]:
    """Return interval-triggered jobs whose interval has elapsed.

    `active_when` conditions are evaluated here — if false, the job is skipped
    (not marked as run), so it'll be rechecked next tick. Jobs whose `trigger`
    is not "interval" (e.g. `empty_board`) are skipped entirely; the daemon
    fires those via a separate path.
    """
    t = now if now is not None else time.time()
    out = []
    for j in jobs:
        if j.trigger != "interval":
            continue
        last = state.get(j.name, 0.0)
        if t - last < j.interval_s:
            continue
        if j.active_when and not evaluate_condition(j.active_when, project_root):
            continue
        out.append(j)
    return out


def mark_run(state_file: Path, name: str, *, now: float | None = None) -> None:
    """Record that `name` just ran (mutates state_file under a lock)."""
    with _locked(state_file):
        state = load_state(state_file)
        state[name] = now if now is not None else time.time()
        save_state(state_file, state)


# ---- active_when conditions ----
# Intentionally tiny grammar. Agents can rely on two forms:
#   "<path> exists"       → True if path exists (relative to project root)
#   "<shell command>"     → True if command exits 0 (prefixed with `sh:`)


def evaluate_condition(expr: str, project_root: Path) -> bool:
    expr = expr.strip()
    if not expr:
        return True
    if expr.startswith("sh:"):
        cmd = expr[3:].strip()
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=project_root, capture_output=True, timeout=10
            )
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False
    m = re.match(r"^(.+?)\s+exists$", expr)
    if m:
        p = m.group(1).strip().strip("\"'")
        full = Path(p) if Path(p).is_absolute() else project_root / p
        return full.exists()
    m = re.match(r"^(.+?)\s+missing$", expr)
    if m:
        p = m.group(1).strip().strip("\"'")
        full = Path(p) if Path(p).is_absolute() else project_root / p
        return not full.exists()
    # Default: unknown condition → skip the job, don't run.
    return False


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
