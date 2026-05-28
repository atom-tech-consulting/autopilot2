"""Channel-adapter abstraction (TB-312, axis (3)).

This module is the structural cleavage that lets digests / watchdog
posts / attention pushes / status-report deliveries reach "anything
other than Mattermost" without editing each `_mm_post` call site by
hand (goal.md L160-161 delete-test).

Core ships three concrete `ChannelAdapter`s as siblings of the ABC so
the daemon's default destination is non-null when Mattermost is
disabled (goal.md L156-157 — the digest needs *somewhere* to go even
on a fresh project that hasn't wired up Mattermost yet):

  - `StdoutChannelAdapter`     — prints to stdout. Useful for `ap2 start
                                  --foreground` smoke runs.
  - `FileAppendChannelAdapter` — appends to a file configured via
                                  `AP2_CHANNEL_FILE_PATH` (default
                                  `<cfg.project_root>/.cc-autopilot/channel.log`).
  - `WebhookChannelAdapter`    — POSTs JSON to `AP2_WEBHOOK_URL` (any
                                  generic HTTP webhook — Slack incoming
                                  webhook, Discord, internal collector).

The Mattermost adapter (`MattermostChannelAdapter`) lives under
`ap2/components/mattermost/__init__.py` because it carries the
project-specific HTTP client, channel/team/bot env knobs, and the
`mattermost_reply` MCP tool (goal.md L184-186 explicitly bundles those
together). The core sibling adapters here are intentionally minimal
(~10 lines each) — they exist for the default-destination
backstop, not as feature-rich delivery channels.

Why ABC (not Protocol): subclasses register via
`Manifest.hook_points["channel_adapter"]` and the registry walks them
at delivery time; making `ChannelAdapter` an `abc.ABC` lets the
adapter contract carry default behavior (the `name` property's
default to the class name, the `__repr__`, etc.) without runtime
isinstance checks at every call site. Protocol would force structural
typing for a contract that's already nominal — a component author
declares "I am a ChannelAdapter" by inheriting, not by accidentally
matching a shape.

Contract:

    class MyChannelAdapter(ChannelAdapter):
        name = "my-channel"

        def post(self, text: str, **meta) -> dict | None:
            # Deliver `text`. `meta` carries optional fields the
            # caller may pass (today: `thread_id` for Mattermost;
            # other adapters ignore unknown keys). Return a small
            # dict describing the delivery (e.g.
            # `{"post_id": "abc"}`) or None on best-effort failure.
            # Raise on hard failure so the caller's per-adapter
            # try/except can emit a `*_error` audit event.

The registry's `channel_adapters(cfg)` accessor returns enabled
adapters in deterministic component-name-sorted order so digest
delivery is reproducible across daemon restarts (load-bearing for
e2e tests that assert on adapter dispatch order).
"""
from __future__ import annotations

import abc
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path


class ChannelAdapter(abc.ABC):
    """Abstract base for outbound-message delivery channels.

    Every adapter declares a `name` (short identifier for ordering /
    diagnostic events) and implements `post(text, **meta) -> dict | None`.

    The `**meta` shape is intentionally open — the Mattermost adapter
    consumes `channel` and `thread_id` keys, the file adapter consumes
    none, etc. Adapters MUST NOT raise on unknown meta keys (forward-
    compat: a future caller may pass extra keys for a different
    adapter; ignored keys keep adapters loosely coupled).
    """

    name: str = ""

    @abc.abstractmethod
    def post(self, text: str, **meta) -> dict | None:
        """Deliver `text` and return a small dict describing the
        delivery (e.g. `{"post_id": "..."}`) or None on a best-effort
        no-op (no destination configured). Raise on hard failure so
        the caller's per-adapter try/except emits a `*_error` audit
        event.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # noqa: D401
        return f"<{type(self).__name__} name={self.name!r}>"


class StdoutChannelAdapter(ChannelAdapter):
    """Prints to stdout, prefixed with `[<name>]` so multi-adapter
    delivery is greppable in `ap2 start --foreground` logs.

    No env knobs — always on when enabled by the registry. The
    `meta` is ignored (stdout doesn't carry threads / channels).
    """

    name = "stdout"

    def post(self, text: str, **meta) -> dict | None:
        sys.stdout.write(f"[{self.name}] {text}\n")
        sys.stdout.flush()
        return {"adapter": self.name}


class FileAppendChannelAdapter(ChannelAdapter):
    """Appends `text` (plus a trailing newline) to the file at
    `AP2_CHANNEL_FILE_PATH`, defaulting to
    `<cwd>/.cc-autopilot/channel.log`.

    Lookup is lazy at every `.post()` call so a hot-reloaded env file
    (TB-271) takes effect on the next delivery without daemon
    restart. Parent directory is created if missing — operators
    don't need to pre-create the log file.
    """

    name = "file-append"
    _ENV_KEY = "AP2_CHANNEL_FILE_PATH"
    _DEFAULT_REL = ".cc-autopilot/channel.log"

    def _resolve_target(self) -> Path:
        raw = os.environ.get(self._ENV_KEY, "").strip()
        if raw:
            return Path(raw).expanduser()
        return Path.cwd() / self._DEFAULT_REL

    def post(self, text: str, **meta) -> dict | None:
        target = self._resolve_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        return {"adapter": self.name, "path": str(target)}


class WebhookChannelAdapter(ChannelAdapter):
    """POSTs `{"text": <text>, **meta}` as JSON to `AP2_WEBHOOK_URL`.

    Compatible with generic HTTP collectors (Slack incoming webhook,
    Discord, internal sinks). When `AP2_WEBHOOK_URL` is unset, the
    adapter returns None without raising — best-effort delivery, the
    caller's audit event can still note the no-destination state.

    Timeout is fixed at 10s (short — webhooks are expected to ack
    fast; a slow webhook blocking the watchdog tick would hold up the
    whole loop).
    """

    name = "webhook"
    _ENV_KEY = "AP2_WEBHOOK_URL"
    _TIMEOUT_S = 10

    def post(self, text: str, **meta) -> dict | None:
        url = os.environ.get(self._ENV_KEY, "").strip()
        if not url:
            return None
        body = {"text": text}
        # Forward known meta keys verbatim so a collector can route on
        # them (e.g. `channel`, `thread_id`); unknown keys ride along
        # too — webhooks don't care about extras.
        for k, v in meta.items():
            if v is not None:
                body[k] = v
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=self._TIMEOUT_S) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
        return {"adapter": self.name, "status": status, "url": url}
