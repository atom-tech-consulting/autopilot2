"""TB-312: `ap2/channel.py` ChannelAdapter ABC + core sibling adapters.

Pins the shape of the channel-adapter abstraction landed alongside
the `mattermost/` component migration (axes 3 + 5 bundled). Each
adapter exercises the `.post(text, **meta) -> dict | None` contract
against a unit-test surface — no network calls; the
`WebhookChannelAdapter` test stubs `urllib.request.urlopen` so the
HTTP-vs-env-fallback shape is exercised without leaving the test
process.

Why these tests live OUTSIDE the mattermost-component test file:
the abstraction is core (not bound to any one component); the
sibling adapters here ship in `ap2/channel.py` and are intended to
be reusable by any downstream component that wires them onto its
own manifest. A future `slack/` migration adds a
`SlackChannelAdapter` next to the Mattermost one — the ABC + the
three core stubs stay untouched.
"""
from __future__ import annotations

import json
import pathlib
import urllib.error

import pytest

from ap2.channel import (
    ChannelAdapter,
    FileAppendChannelAdapter,
    StdoutChannelAdapter,
    WebhookChannelAdapter,
)


def test_channel_adapter_is_abstract():
    """`ChannelAdapter` cannot be instantiated directly — `.post()` is
    abstract. Forces a subclass + concrete implementation."""
    with pytest.raises(TypeError):
        ChannelAdapter()  # type: ignore[abstract]


def test_stdout_adapter_writes_to_stdout(capsys):
    """`StdoutChannelAdapter` prints `[<name>] <text>\\n` and returns a
    small dict noting the adapter name. Unknown meta keys are
    silently ignored (forward-compat)."""
    adapter = StdoutChannelAdapter()
    out = adapter.post("hello world", channel="ignored", extra="also-ignored")
    captured = capsys.readouterr()
    assert captured.out == "[stdout] hello world\n"
    assert out == {"adapter": "stdout"}


def test_file_append_adapter_default_path(tmp_path, monkeypatch):
    """No `AP2_CHANNEL_FILE_PATH` set → appends to
    `<cwd>/.cc-autopilot/channel.log`. Parent dir auto-created."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AP2_CHANNEL_FILE_PATH", raising=False)
    adapter = FileAppendChannelAdapter()
    out = adapter.post("first line")
    assert out is not None
    target = pathlib.Path(out["path"])
    assert target == tmp_path / ".cc-autopilot" / "channel.log"
    assert target.read_text() == "first line\n"
    # Second post appends, doesn't truncate.
    adapter.post("second line")
    assert target.read_text() == "first line\nsecond line\n"


def test_file_append_adapter_env_override(tmp_path, monkeypatch):
    """`AP2_CHANNEL_FILE_PATH` overrides the default location. The
    env var is read fresh per `.post()` call so a hot-reloaded env
    (TB-271) takes effect on the next dispatch."""
    target = tmp_path / "sub" / "outbound.log"
    monkeypatch.setenv("AP2_CHANNEL_FILE_PATH", str(target))
    adapter = FileAppendChannelAdapter()
    adapter.post("via-env-override")
    assert target.exists()
    assert target.read_text() == "via-env-override\n"


def test_webhook_adapter_no_url_returns_none(monkeypatch):
    """`AP2_WEBHOOK_URL` unset → adapter returns `None` without
    raising. The caller's audit event notes the no-destination
    state per the per-adapter try/except contract."""
    monkeypatch.delenv("AP2_WEBHOOK_URL", raising=False)
    adapter = WebhookChannelAdapter()
    assert adapter.post("any text") is None


def test_webhook_adapter_posts_json_payload(monkeypatch):
    """With `AP2_WEBHOOK_URL` set, the adapter POSTs
    `{"text": <text>, **meta}` as JSON. Forwards known meta keys
    (`channel`, `thread_id`) and unknown keys verbatim — collectors
    can route on any field."""
    monkeypatch.setenv("AP2_WEBHOOK_URL", "https://hooks.example.test/abc")
    captured: dict = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

    def _fake_urlopen(req, context=None, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResp()

    import ap2.channel as _channel_mod
    monkeypatch.setattr(
        _channel_mod.urllib.request, "urlopen", _fake_urlopen,
    )

    adapter = WebhookChannelAdapter()
    outcome = adapter.post("hello", channel="ch1", thread_id="t1", extra="x")
    assert outcome is not None
    assert outcome["adapter"] == "webhook"
    assert outcome["status"] == 200
    assert outcome["url"] == "https://hooks.example.test/abc"
    assert captured["body"] == {
        "text": "hello",
        "channel": "ch1",
        "thread_id": "t1",
        "extra": "x",
    }
    # Short 10s timeout so a slow webhook doesn't hold up the watchdog tick.
    assert captured["timeout"] == 10
    assert captured["headers"]["Content-type"] == "application/json"
