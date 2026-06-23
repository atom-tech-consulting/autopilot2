"""TB-426: the fenced-file `.claude/settings.json` `permissions.deny`
project-settings layer.

A Claude Code task agent runs with `--disallowedTools` derived from
`TASK_AGENT_FENCED_PATHS` (the SDK-layer fence). This project-settings layer
mirrors that fence as a committed `.claude/settings.json` deny block so the
protection travels with every clone and also covers local, non-sandbox
`ap2 start` runs. Both writers (`ap2 init`, `ap2 sandbox project-setup`)
derive the deny entries from the SAME canonical tuple via the shared
`render_fenced_deny_entries` / `merge_fenced_deny_into_settings` renderer, so
the two enforcement layers can never drift apart. These tests pin that
contract.
"""
from __future__ import annotations

import json
from pathlib import Path

from ap2.init import init_project
from ap2.tools import (
    TASK_AGENT_FENCED_PATHS,
    merge_fenced_deny_into_settings,
    render_fenced_deny_entries,
)


def test_renderer_emits_edit_and_write_for_every_fenced_path():
    """The renderer emits BOTH an `Edit(<p>)` and a `Write(<p>)` entry for
    every `TASK_AGENT_FENCED_PATHS` element — and nothing outside that set."""
    entries = render_fenced_deny_entries()

    for path in TASK_AGENT_FENCED_PATHS:
        assert f"Edit({path})" in entries, f"Edit({path}) missing from deny list"
        assert f"Write({path})" in entries, f"Write({path}) missing from deny list"

    # Exactly two entries per fenced path, nothing extra.
    assert len(entries) == 2 * len(TASK_AGENT_FENCED_PATHS)
    expected = {f"Edit({p})" for p in TASK_AGENT_FENCED_PATHS} | {
        f"Write({p})" for p in TASK_AGENT_FENCED_PATHS
    }
    assert set(entries) == expected


def test_init_scaffolds_claude_settings_deny(tmp_path: Path):
    """`init_project(tmp)` writes `tmp/.claude/settings.json` whose
    `permissions.deny` covers the full fenced set as `Edit`/`Write` pairs,
    and the InitReport flags the fresh scaffold."""
    report = init_project(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists()
    assert report.claude_settings_created is True
    assert report.claude_settings_deny_merged is False

    settings = json.loads(settings_path.read_text())
    deny = settings["permissions"]["deny"]
    for path in TASK_AGENT_FENCED_PATHS:
        assert f"Edit({path})" in deny
        assert f"Write({path})" in deny


def test_init_is_idempotent_on_claude_settings(tmp_path: Path):
    """A second `init_project` on a tree that already carries the fenced
    deny list is a no-op (neither created nor merged the second time)."""
    init_project(tmp_path)
    report2 = init_project(tmp_path)
    assert report2.claude_settings_created is False
    assert report2.claude_settings_deny_merged is False


def test_init_merges_into_existing_settings_preserving_unrelated_entries(tmp_path: Path):
    """`init_project` on a tmp that already has a `.claude/settings.json`
    with an unrelated deny entry MERGES: the unrelated entry survives, an
    unrelated top-level key survives, and the fenced entries are added."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir(parents=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": ["Edit(secrets.txt)"],
                    "allow": ["Bash(ls)"],
                },
                "hooks": {"PreToolUse": []},
            },
            indent=2,
        )
        + "\n"
    )

    report = init_project(tmp_path)
    assert report.claude_settings_created is False
    assert report.claude_settings_deny_merged is True

    settings = json.loads(settings_path.read_text())
    deny = settings["permissions"]["deny"]
    # Pre-existing unrelated entries survive (deny + allow + the hooks key).
    assert "Edit(secrets.txt)" in deny
    assert settings["permissions"]["allow"] == ["Bash(ls)"]
    assert settings["hooks"] == {"PreToolUse": []}
    # Fenced entries are added.
    for path in TASK_AGENT_FENCED_PATHS:
        assert f"Edit({path})" in deny
        assert f"Write({path})" in deny


def test_merge_helper_does_not_mutate_input_and_is_idempotent():
    """`merge_fenced_deny_into_settings` returns a new dict (no input
    mutation) and converges: merging its own output changes nothing."""
    original = {"permissions": {"deny": ["Edit(secrets.txt)"]}}
    merged = merge_fenced_deny_into_settings(original)

    # Input untouched.
    assert original == {"permissions": {"deny": ["Edit(secrets.txt)"]}}
    # Unrelated entry preserved + fenced entries present.
    assert "Edit(secrets.txt)" in merged["permissions"]["deny"]
    for path in TASK_AGENT_FENCED_PATHS:
        assert f"Edit({path})" in merged["permissions"]["deny"]
        assert f"Write({path})" in merged["permissions"]["deny"]

    # Idempotent: a second merge is byte-equal.
    assert merge_fenced_deny_into_settings(merged) == merged


def test_merge_helper_seeds_empty_settings():
    """Given no settings at all, the merge helper produces a minimal
    `{permissions: {deny: [...]}}` shape covering the fenced set."""
    merged = merge_fenced_deny_into_settings(None)
    assert set(merged) == {"permissions"}
    assert merged["permissions"]["deny"] == render_fenced_deny_entries()
