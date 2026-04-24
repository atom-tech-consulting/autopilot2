"""Shared fake-SDK harness for ap2 e2e tests.

The real `claude_agent_sdk` is replaced by a scripted stub. Tests register
responders keyed by a substring that matches the generated prompt (task,
mattermost, cron) and the FakeSDK routes each `query()` call to the matching
responder.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator, Callable


class _FakeMsg:
    """Minimal message shape `daemon._extract_text` understands."""

    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(text=text)]


class FakeSDK:
    """Scripted SDK stub.

    Usage:
        sdk = FakeSDK()
        sdk.on("## Task\\nTB-5", text_respond("RESULT:\\nstatus: complete\\n..."))
        sdk.on("## Scheduled job: status-report", my_cron_responder)
        async for msg in sdk.query(prompt=p, options=sdk.ClaudeAgentOptions(...)):
            ...

    Each responder is an *async generator factory* — a callable taking
    (prompt, options) and returning an async iterator of `_FakeMsg`. The
    scripts are checked in registration order; first substring match wins.
    """

    def __init__(self) -> None:
        self._scripts: list[tuple[str, Callable]] = []
        self._default_text: str = ""

    def on(self, substring: str, factory: Callable) -> None:
        self._scripts.append((substring, factory))

    def default(self, text: str) -> None:
        self._default_text = text

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def query(self, *, prompt: str, options) -> AsyncIterator[_FakeMsg]:
        for substring, factory in self._scripts:
            if substring in prompt:
                return factory(prompt, options)
        return _single_text(self._default_text)


async def _single_text(text: str) -> AsyncIterator[_FakeMsg]:
    if text:
        yield _FakeMsg(text)


def text_respond(text: str) -> Callable:
    """Return an async-gen factory that yields a single message with `text`."""

    def factory(prompt, options):  # noqa: ARG001
        return _single_text(text)

    return factory


def crash_respond(exc: Exception) -> Callable:
    """Return an async-gen factory that raises `exc` mid-iteration — simulates
    the "SDK subprocess died with exit 1" pattern where the agent had already
    been streaming messages before the crash.
    """

    async def _gen() -> AsyncIterator[_FakeMsg]:
        # Yield one in-progress message to populate the stream log, then crash.
        yield _FakeMsg("(working...)")
        raise exc

    def factory(prompt, options):  # noqa: ARG001
        return _gen()

    return factory
