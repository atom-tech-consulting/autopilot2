"""Outbound notification queue (TB-389).

Core call sites enqueue outbound notifications here instead of walking a
channel-adapter list. The communication component
(`ap2/components/communication/`) owns the channels; on its tick pass it
drains the undelivered notifications from this queue and delivers each to
its internal channel registry. This keeps core free of any channel
reference — a core call site only appends a JSONL record (a pure
filesystem write, no import of `ap2.components.*`); delivery is the
communication component's concern.

Shape mirrors the operator-queue pattern (`ap2.operator_queue`): an
append-only `notifications.jsonl` plus a `notifications_state.json`
recording the set of delivered uuids. Append-only + a delivered-uuid
cursor (rather than a read-modify-write rewrite of the queue) keeps a
concurrent `enqueue` (fired from the daemon's main tick / watchdog) from
racing the communication component's drain — appends never lose data,
and only the single drain pass writes the state file.

Record shape (one JSON object per line):

    {"uuid": "<hex>", "ts": "<iso8601>", "text": "<body>",
     "channel": "<dest or ''>", "thread_id": "<root or ''>",
     "kind": "<auto_diagnose|pending_review_reminder|smoke_alert|...>"}

`channel` is an optional destination hint — the communication
component's channel adapters resolve their own default destination when
it is empty (e.g. the Mattermost adapter falls back to
`AP2_MM_CHANNELS[0]`), so a core call site never needs to know the
channel identity.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid as _uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enqueue(
    cfg: "Config",
    text: str,
    *,
    channel: str = "",
    thread_id: str = "",
    kind: str = "",
) -> dict:
    """Append one outbound notification to the queue and return its record.

    Pure filesystem append — no channel walk, no `ap2.components.*`
    import. The communication component delivers it on its next tick.
    """
    rec = {
        "uuid": _uuid.uuid4().hex,
        "ts": _now_iso(),
        "text": text,
        "channel": channel or "",
        "thread_id": thread_id or "",
        "kind": kind or "",
    }
    path = cfg.notifications_file
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _read_records(cfg: "Config") -> list[dict]:
    path = cfg.notifications_file
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("uuid"):
            out.append(rec)
    return out


def _delivered_uuids(cfg: "Config") -> set[str]:
    path = cfg.notifications_state_file
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    return set(data.get("delivered", []) or [])


def pending(cfg: "Config") -> list[dict]:
    """Return queued notifications not yet marked delivered, oldest-first."""
    delivered = _delivered_uuids(cfg)
    return [r for r in _read_records(cfg) if r.get("uuid") not in delivered]


def mark_delivered(cfg: "Config", uuids) -> None:
    """Record `uuids` as delivered so `pending()` no longer surfaces them.

    Only the communication component's single drain pass calls this, so
    the read-modify-write of the state file is race-free.
    """
    uuids = [u for u in uuids if u]
    if not uuids:
        return
    delivered = _delivered_uuids(cfg)
    delivered.update(uuids)
    path = cfg.notifications_state_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"delivered": sorted(delivered)}, indent=2),
        encoding="utf-8",
    )
