"""Pins for the load-bearing structure of `ap2/ideation.default.md`.

These tests guard against silent regressions in the ideation prompt — the
sections, tool names, and heuristics agents depend on. Each test cites
the originating TB-N so future rewrites understand the *why*.
"""
from __future__ import annotations

from pathlib import Path

from ap2.config import Config
from ap2.ideation import _DEFAULT_PROMPT_PATH, _PROJECT_PROMPT_REL, load_prompt


def _default_prompt() -> str:
    return _DEFAULT_PROMPT_PATH.read_text()


def test_default_prompt_file_exists():
    assert _DEFAULT_PROMPT_PATH.exists()
    assert _DEFAULT_PROMPT_PATH.read_text().strip()


def test_load_prompt_returns_default_when_no_override(tmp_path: Path):
    cfg = _stub_config(tmp_path)
    assert load_prompt(cfg) == _default_prompt()


def test_load_prompt_uses_project_override(tmp_path: Path):
    cfg = _stub_config(tmp_path)
    override = tmp_path / _PROJECT_PROMPT_REL
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("custom project ideation prompt\n")
    assert load_prompt(cfg) == "custom project ideation prompt\n"


# ---------------------------------------------------------------------------
# Prompt-content pins (migrated from test_cron_defaults.py).

def test_ideation_prompt_mentions_goal_md():
    prompt = _default_prompt()
    assert "goal.md" in prompt
    lower = prompt.lower()
    assert "absent" in lower or "fall back" in lower or "infer" in lower


def test_ideation_prompt_mentions_followup_scan():
    prompt = _default_prompt()
    lower = prompt.lower()
    assert "complete" in lower
    assert "follow-up" in lower or "follow up" in lower


def test_ideation_prompt_includes_pipeline_task_start_for_long_work():
    """TB-81: long work routes through `pipeline_task_start`."""
    prompt = _default_prompt()
    lower = prompt.lower()
    assert "pipeline_task_start" in prompt
    assert "10 minutes" in lower or "10 min" in lower
    assert "launch task" in lower
    assert "validation" in lower
    assert "pid:" in prompt
    assert "progress bar" in lower or "fans out" in lower


def test_ideation_prompt_pins_step15_failure_review():
    """TB-88: failure review classifies into edit-briefing/split/follow-up/abandon."""
    prompt = _default_prompt()
    lower = prompt.lower()
    assert "step 1.5" in lower or "1.5" in prompt
    assert "failure review" in lower or "failed task" in lower
    assert "5 most-recent" in prompt or "up to 5" in prompt
    for label in ("edit-briefing", "split", "follow-up", "abandon"):
        assert label in prompt, f"missing classification {label!r}"
    assert "exit=127" in prompt
    assert ">7 criteria" in prompt or "7 criteria" in prompt
    # TB-109: ideation no longer has Bash; the `git_log_grep` MCP tool
    # replaces the old `git log --grep="<TASK_ID>"` Bash invocation.
    assert "git_log_grep" in prompt
    assert "Frozen" in prompt
    assert "verification_failed" in prompt
    assert "retry_exhausted" in prompt
    # TB-93+: partial verifications are also a follow-up source. Pinning the
    # event name + the load-bearing instruction (rewrite prose-bullet criteria
    # as concrete shell checks when the SDK judge can't evaluate them).
    assert "verification_partial" in prompt
    assert "concrete shell check" in prompt
    assert "#fix-briefing" in prompt
    assert "Recommend abandoning" in prompt
    assert "Do NOT auto-unfreeze" in prompt or "do not auto-unfreeze" in lower


def test_ideation_prompt_pins_step05_insights_read():
    """TB-89: insights index read + reactive #evaluation tasks."""
    prompt = _default_prompt()
    assert ".cc-autopilot/insights/_index.md" in prompt
    lower = prompt.lower()
    assert "step 0.5" in lower or "0.5" in prompt
    assert "front matter" in lower or "yaml" in lower
    for key in ("tldr", "updated_by", "cites"):
        assert key in prompt
    assert "#evaluation" in prompt
    assert "reactively" in lower or "reactive" in lower
    assert "auto-cascade" in lower or "don't auto" in lower or "do NOT" in prompt
    assert "ONE per cycle" in prompt or "ONE `#evaluation`" in prompt


def test_ideation_prompt_pins_ideation_state_write_tool():
    """TB-90: `ideation_state_write` MCP tool named in Step 0.
    TB-109: ideation has no Bash, so the warnings about `tee`/`>` were
    replaced with a clearer "you don't have Bash either" note. Pin the
    new shape — the load-bearing fact is that `ideation_state_write`
    is the ONLY way to land the file."""
    prompt = _default_prompt()
    assert "ideation_state_write" in prompt
    assert "`content`" in prompt or "content arg" in prompt or "content`" in prompt
    assert "Write/Edit access" in prompt
    flat = " ".join(prompt.split())
    assert "ONLY way to write" in flat or "only way to write" in flat.lower()
    assert "Read" in prompt


def test_ideation_prompt_pins_step0_assessment():
    """TB-87: Step 0 assessment schema + citation rule + OVERWRITE semantics."""
    prompt = _default_prompt()
    assert ".cc-autopilot/ideation_state.md" in prompt
    assert "Step 0" in prompt or "step 0" in prompt
    for section in (
        "## Mission alignment",
        "## Current focus assessment",
        "## Non-goal risk check",
        "## Considered & deferred",
        "## Open questions for operator",
        "## Proposals this cycle",
    ):
        assert section in prompt, f"missing schema section {section!r}"
    for status in ("in-progress", "exhausted-needs-operator", "deferred"):
        assert status in prompt, f"missing status value {status!r}"
    lower = prompt.lower()
    assert "cite" in lower
    assert "tb-n" in lower
    assert "forbidden" in lower or "vague claims" in lower
    assert "OVERWRITE" in prompt or "overwrite" in lower


def test_ideation_prompt_lists_ideation_state_first_in_read_order():
    prompt = _default_prompt()
    idx = prompt.find("Read these files in order:")
    assert idx >= 0
    block = prompt[idx:idx + 800]
    assert "1. .cc-autopilot/ideation_state.md" in block


def test_ideation_prompt_reads_operator_log_and_treats_as_authoritative():
    """TB-106: operator_log.md is the operator-decision channel. Ideation
    must read it and NOT re-propose actions logged there, even if its own
    prior assessment surfaced them as 'Open questions for operator'."""
    prompt = _default_prompt()
    assert ".cc-autopilot/operator_log.md" in prompt
    # Normalize whitespace — the prompt is markdown-wrapped so phrases
    # like "do NOT re-propose" can straddle a line break.
    flat = " ".join(prompt.split())
    assert "authoritative" in flat.lower()
    assert "re-propose" in flat.lower()


def test_ideation_prompt_pins_two_tier_verification_split():
    """TB-86: pipeline-launch output checks belong in validation_briefing."""
    prompt = _default_prompt()
    lower = prompt.lower()
    assert "two-tier" in lower or "where output checks belong" in lower
    assert "validation_briefing" in prompt
    assert "DO NOT put output" in prompt or "do not put output" in lower
    assert "test -f reports/" in prompt
    assert "running detached" in lower or "still running" in lower


def test_ideation_prompt_warns_off_bare_python_and_path_pitfalls():
    """TB-76: shell-bullet pitfalls warning."""
    prompt = _default_prompt()
    assert "uv run python" in prompt
    assert "python3" in prompt
    assert "test -f" in prompt


def test_ideation_prompt_instructs_verification_section_population():
    """TB-69: every proposed briefing must include `## Verification`."""
    prompt = _default_prompt()
    assert "## Verification" in prompt
    lower = prompt.lower()
    assert "shell" in lower
    assert "legacy" in lower or "skip" in lower


# ---------------------------------------------------------------------------
# Helpers


def _stub_config(tmp_path: Path) -> Config:
    """Minimal Config — only project_root is needed for load_prompt."""
    return Config.load(tmp_path)
