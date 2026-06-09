"""TB-319: behavioral pinning for the `ap2 status` `## Components`
section (text branch) + the `components` block (JSON branch).

Closes the goal.md L235-237 Progress signal that named `ap2 status` as
the natural surface for component enumeration. Before TB-319, the only
way to discover which components were wired into the daemon was to
`ls ap2/components/` and read each manifest by hand; after TB-319 a
single `ap2 status` invocation lists every discovered component with
its on/off state and the env-flag string that controls it.

Pinned shape:

  (a) Text branch ALWAYS prints a `## Components` header followed by
      one indented line per discovered manifest (always-emitted —
      unlike the operator-attention cluster's omit-on-empty rule, the
      registry walk is deterministic and the same set of components
      ships on every project, so suppressing the section would be a
      regression worth surfacing).
  (b) Text branch entries render in alphabetic order by manifest name
      (matches `default_registry().components` / `tick_hooks(phase)`
      iteration so a reader's mental model of "in what order do
      hooks fire?" lines up with what `ap2 status` shows).
  (c) Text branch per-entry shape is `  <name>: <on|off>
      (<env_flag_desc>)` — two-space indent matches existing status
      sub-block style; the env_flag_desc renders the polarity
      convention in operator-legible form.
  (d) JSON branch ALWAYS carries a top-level `components` list with
      one entry per discovered manifest, each carrying the four
      documented keys (`name`, `enabled`, `env_flag`, `default_enabled`).
      Mirrors the TB-227 `auto_approve` / TB-258 `audit` / TB-298
      `attention` parser-stability promise.
  (e) Polarity: a `*_DISABLED`-style env_flag (suppress polarity —
      janitor, validator_judge) flips `enabled` to `False` when set
      truthy; an opt-in env_flag with `default_enabled=False`
      (require polarity — mattermost / `AP2_MM_CHANNELS`) flips
      `enabled` to `True` only when the env var is set non-empty.
  (f) Text + JSON branches share the same source-of-truth walk
      (`default_registry().components` + `Manifest.is_enabled` /
      `Manifest.env_flag_description`) so they can never disagree
      about a component's state.

Fixtures mirror TB-298 / TB-242 — `init_project` + a `cfg` pytest
fixture. Polarity tests use `monkeypatch.setenv` so the live process
env stays clean across cases.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ap2.config import Config
from ap2.init import init_project
from ap2.registry import Manifest, default_registry


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Fresh ap2 project scaffold — same shape as TB-298 / TB-242."""
    init_project(tmp_path)
    c = Config.load(tmp_path)
    c.ensure_dirs()
    return c


# ===========================================================================
# (a) + (b) Text branch ALWAYS prints the `## Components` header
#           and an indented per-manifest line, alphabetic by name.
# ===========================================================================


def test_text_emits_components_header(cfg: Config, capsys):
    """`ap2 status` text output contains a `## Components` heading —
    pins the briefing's `grep -q "^## Components"` verification."""
    from ap2 import cli_daemon

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Components" in out, out
    # The header is at the start of its own line (not mid-line) so a
    # `grep -q "^## Components"` pin matches.
    assert any(line == "## Components" for line in out.splitlines()), out


def test_text_emits_janitor_line_with_on_off_state(cfg: Config, capsys):
    """The janitor entry renders as `  janitor: on (...)` or
    `  janitor: off (...)` — pins the briefing's
    `grep -qE "^  janitor: (on|off)"` verification."""
    from ap2 import cli_daemon

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    janitor_lines = [
        line for line in out.splitlines() if line.startswith("  janitor:")
    ]
    assert len(janitor_lines) == 1, (
        f"expected exactly one janitor line, got: {janitor_lines!r}\n"
        f"full output: {out}"
    )
    assert janitor_lines[0].startswith("  janitor: on ") or janitor_lines[0].startswith(
        "  janitor: off "
    ), janitor_lines[0]


def test_text_lists_every_discovered_component(cfg: Config, capsys):
    """The text branch enumerates every component the registry knows
    about. Pins the "walk what's there, not a hardcoded list" promise:
    a future migration that drops a new subpackage under
    `ap2/components/<name>/` flows through here without a CLI edit."""
    from ap2 import cli_daemon

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    for manifest in default_registry().components:
        assert f"  {manifest.name}:" in out, (
            f"expected `  {manifest.name}:` line in components block, "
            f"got: {out}"
        )


def test_text_components_lines_alphabetic(cfg: Config, capsys):
    """Component lines render in alphabetic order by manifest name,
    matching `default_registry().components` iteration. Determinism
    matters so an operator's mental model of "in what order do hooks
    fire?" lines up with what they see in status."""
    from ap2 import cli_daemon

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    names = []
    for line in out.splitlines():
        # Component lines start with two spaces + a name + `:`.
        if line.startswith("  ") and ": " in line:
            head, _ = line.split(":", 1)
            candidate = head.strip()
            # Filter to known component names so unrelated indented
            # lines (`pending:`, `audit:`, etc.) don't leak in.
            if candidate in {m.name for m in default_registry().components}:
                names.append(candidate)
    assert names == sorted(names), names
    # And every discovered component shows up exactly once.
    assert sorted(names) == sorted(
        m.name for m in default_registry().components
    ), names


# ===========================================================================
# (d) JSON branch ALWAYS carries a `components` list with the documented
#     entry shape.
# ===========================================================================


def test_json_carries_components_list(cfg: Config, capsys):
    """`ap2 status --json` always carries a top-level `components` list
    with one entry per discovered manifest. Each entry has the four
    documented keys. Pins the briefing's `--json` verification."""
    from ap2 import cli_daemon

    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "components" in payload, list(payload.keys())
    assert isinstance(payload["components"], list)
    assert len(payload["components"]) == len(default_registry().components)
    assert len(payload["components"]) >= 6, (
        "registry should have ≥ 6 real components after TB-309..TB-318; "
        f"got {len(payload['components'])}"
    )
    for entry in payload["components"]:
        assert set(entry.keys()) == {
            "name",
            "enabled",
            "env_flag",
            "default_enabled",
        }, entry
        assert isinstance(entry["name"], str)
        assert isinstance(entry["enabled"], bool)
        assert entry["env_flag"] is None or isinstance(entry["env_flag"], str)
        assert isinstance(entry["default_enabled"], bool)


def test_json_components_alphabetic(cfg: Config, capsys):
    """JSON `components` entries are in alphabetic order by name —
    same source-of-truth walk as the text branch (no separate sort)."""
    from ap2 import cli_daemon

    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    names = [c["name"] for c in payload["components"]]
    assert names == sorted(names), names


# ===========================================================================
# (e) Polarity: suppress vs require flips `enabled` correctly.
# ===========================================================================


def test_janitor_disabled_flips_enabled_to_false(
    cfg: Config, capsys, monkeypatch,
):
    """Setting `AP2_JANITOR_DISABLED=1` flips the janitor entry's
    `enabled` to False in both branches — suppress-polarity check
    (`default_enabled=True` + `*_DISABLED` env_flag => env var is a
    kill switch). Mirrors TB-309's polarity canary at the
    `ap2 status` surface."""
    from ap2 import cli_daemon

    monkeypatch.setenv("AP2_JANITOR_DISABLED", "1")

    # JSON branch
    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    janitor_entry = next(
        c for c in payload["components"] if c["name"] == "janitor"
    )
    assert janitor_entry["enabled"] is False, janitor_entry

    # Text branch
    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    janitor_lines = [
        line for line in out.splitlines() if line.startswith("  janitor:")
    ]
    assert len(janitor_lines) == 1, janitor_lines
    assert janitor_lines[0].startswith("  janitor: off "), janitor_lines[0]


def test_mattermost_demoted_communication_always_on(cfg: Config, capsys, monkeypatch):
    """TB-389: mattermost is no longer a top-level component — it was
    demoted to a channel adapter under the always-on `communication`
    component, and `AP2_MM_CHANNELS` is channel-level config rather than
    a component env_flag. So `ap2 status` lists NO `mattermost` component
    and a `communication` component that is always-on (`env_flag=None`),
    independent of `AP2_MM_CHANNELS`.

    The fresh project's `.cc-autopilot/env` may pre-set `AP2_MM_CHANNELS`
    (sandbox `install-channel`); we toggle it both ways to prove it no
    longer flips a component's `enabled` bit."""
    from ap2 import cli_daemon

    for mm_value in (None, "", "channel-id"):
        if mm_value is None:
            monkeypatch.delenv("AP2_MM_CHANNELS", raising=False)
        else:
            monkeypatch.setenv("AP2_MM_CHANNELS", mm_value)
        rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        names = {c["name"] for c in payload["components"]}
        # mattermost is gone as a top-level component.
        assert "mattermost" not in names, names
        # communication is present and always-on regardless of the knob.
        comm = next(c for c in payload["components"] if c["name"] == "communication")
        assert comm["enabled"] is True, comm
        assert comm["env_flag"] is None, comm
        assert comm["default_enabled"] is True, comm


# ===========================================================================
# (f) Cross-branch consistency: text + JSON walk the same source-of-truth.
# ===========================================================================


def test_text_and_json_agree_on_enabled_state(
    cfg: Config, capsys, monkeypatch,
):
    """For every component, the text branch's `on` / `off` token
    matches the JSON branch's `enabled` boolean. The two surfaces
    walk the same `default_registry().components` snapshot inside
    one `cmd_status` call (cf. cli_daemon.py — `_component_manifests`
    is computed once and reused) so they can never disagree.

    Exercise a non-default polarity to make the agreement
    interesting: flip janitor off + mattermost on, then compare."""
    from ap2 import cli_daemon

    monkeypatch.setenv("AP2_JANITOR_DISABLED", "1")
    monkeypatch.setenv("AP2_MM_CHANNELS", "abc")

    rc = cli_daemon.cmd_status(cfg, Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    json_enabled = {c["name"]: c["enabled"] for c in payload["components"]}

    rc = cli_daemon.cmd_status(cfg, Namespace(json=False))
    assert rc == 0
    out = capsys.readouterr().out
    text_enabled = {}
    for line in out.splitlines():
        if line.startswith("  ") and ": " in line:
            head, rest = line.split(":", 1)
            name = head.strip()
            if name in json_enabled:
                token = rest.strip().split(" ", 1)[0]
                text_enabled[name] = token == "on"
    assert text_enabled == json_enabled, (
        f"text vs JSON disagreement:\n  text: {text_enabled}\n"
        f"  json: {json_enabled}"
    )


# ===========================================================================
# Manifest.is_enabled + env_flag_description unit tests (TB-319's
# polarity-helper extraction).
# ===========================================================================


def test_manifest_is_enabled_env_flag_none():
    """`env_flag=None` → enabled iff `default_enabled` is True. The
    polarity helper short-circuits without reading any env."""
    on = Manifest(
        name="x",
        env_flag=None,
        default_enabled=True,
        hook_points={},
    )
    off = Manifest(
        name="y",
        env_flag=None,
        default_enabled=False,
        hook_points={},
    )
    # Pass an empty env so the test is independent of the live process env.
    assert on.is_enabled(env={}) is True
    assert off.is_enabled(env={}) is False


def test_manifest_is_enabled_suppress_polarity():
    """`default_enabled=True` + `*_DISABLED` env_flag → env var
    DISABLES on truthy. Mirrors the janitor / validator_judge contract."""
    m = Manifest(
        name="x",
        env_flag="AP2_X_DISABLED",
        default_enabled=True,
        hook_points={},
    )
    assert m.is_enabled(env={}) is True
    assert m.is_enabled(env={"AP2_X_DISABLED": ""}) is True
    assert m.is_enabled(env={"AP2_X_DISABLED": "0"}) is True
    assert m.is_enabled(env={"AP2_X_DISABLED": "false"}) is True
    assert m.is_enabled(env={"AP2_X_DISABLED": "1"}) is False
    assert m.is_enabled(env={"AP2_X_DISABLED": "yes"}) is False


def test_manifest_is_enabled_require_polarity():
    """`default_enabled=False` + opt-in env_flag → env var ENABLES on
    truthy. Mirrors the mattermost / `AP2_MM_CHANNELS` contract."""
    m = Manifest(
        name="x",
        env_flag="AP2_X",
        default_enabled=False,
        hook_points={},
    )
    assert m.is_enabled(env={}) is False
    assert m.is_enabled(env={"AP2_X": ""}) is False
    assert m.is_enabled(env={"AP2_X": "0"}) is False
    assert m.is_enabled(env={"AP2_X": "1"}) is True
    assert m.is_enabled(env={"AP2_X": "channel-id"}) is True


def test_manifest_env_flag_description():
    """`env_flag_description` produces an operator-legible string in
    each of the three render branches: no-flag, unset, and set-with-
    value. Long values get truncated at 32 chars so a multi-channel
    `AP2_MM_CHANNELS` list doesn't blow up the status block width."""
    none_m = Manifest(
        name="x", env_flag=None, default_enabled=True, hook_points={},
    )
    suppress_m = Manifest(
        name="y",
        env_flag="AP2_Y_DISABLED",
        default_enabled=True,
        hook_points={},
    )
    require_m = Manifest(
        name="z", env_flag="AP2_Z", default_enabled=False, hook_points={},
    )
    assert none_m.env_flag_description(env={}) == "env_flag=None"
    assert suppress_m.env_flag_description(env={}) == "AP2_Y_DISABLED unset"
    assert (
        suppress_m.env_flag_description(env={"AP2_Y_DISABLED": "1"})
        == "AP2_Y_DISABLED=1"
    )
    assert require_m.env_flag_description(env={"AP2_Z": "abc"}) == "AP2_Z=abc"
    # 40-char value triggers truncation at 32 chars (29 + "...").
    long_val = "x" * 40
    desc = require_m.env_flag_description(env={"AP2_Z": long_val})
    assert desc.startswith("AP2_Z=") and desc.endswith("..."), desc
    assert len(desc) <= len("AP2_Z=") + 32, desc
