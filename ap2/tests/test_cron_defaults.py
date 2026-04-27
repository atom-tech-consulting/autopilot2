"""Smoke tests for `ap2/cron.default.yaml` and first-start bootstrap."""
from __future__ import annotations

from pathlib import Path

from ap2.cron import CronJob, bootstrap, load_jobs


DEFAULT = Path(__file__).resolve().parent.parent / "cron.default.yaml"


def test_default_cron_file_exists():
    assert DEFAULT.exists()


def test_default_cron_parses_cleanly():
    jobs = load_jobs(DEFAULT)
    names = {j.name for j in jobs}
    assert "status-report" in names
    assert "ideation" in names
    for j in jobs:
        assert isinstance(j, CronJob)
        assert j.interval_s > 0
        assert j.prompt.strip()


def test_default_cron_intervals_are_sane():
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    # 10 min → 4 h envelope for status-report; 1 h → 12 h for ideation.
    assert 600 <= jobs["status-report"].interval_s <= 4 * 3600
    assert 3600 <= jobs["ideation"].interval_s <= 12 * 3600


def test_ideation_has_backlog_guard():
    """The ideation job should only fire when the Backlog is under-full."""
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    aw = jobs["ideation"].active_when or ""
    assert aw.startswith("sh:"), aw
    assert "Backlog" in aw


def test_bootstrap_copies_default(tmp_path: Path):
    target = tmp_path / "cron.yaml"
    assert not target.exists()

    copied = bootstrap(target)
    assert copied is True
    assert target.exists()

    # Re-run: should be a no-op now that the file exists.
    copied2 = bootstrap(target)
    assert copied2 is False

    # And the file should parse as valid jobs.
    jobs = load_jobs(target)
    assert {j.name for j in jobs} == {"status-report", "ideation"}


def test_bootstrap_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "sub" / "deeper" / "cron.yaml"
    assert bootstrap(target) is True
    assert target.exists()


# ---------------------------------------------------------------------------
# TB-70: ideation prompt now reads goal.md and scans Complete for follow-ups.
# These tests pin the load-bearing phrases so a future prompt rewrite can't
# silently drop them.

def test_ideation_prompt_mentions_goal_md():
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    prompt = jobs["ideation"].prompt
    assert "goal.md" in prompt
    # Fallback path documented for projects that don't have goal.md yet.
    lower = prompt.lower()
    assert "absent" in lower or "fall back" in lower or "infer" in lower


def test_ideation_prompt_mentions_followup_scan():
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    prompt = jobs["ideation"].prompt
    lower = prompt.lower()
    # The agent must be told to look at Complete (not just propose greenfield).
    assert "complete" in lower
    # And must understand the intent: discover follow-ups.
    assert "follow-up" in lower or "follow up" in lower


def test_ideation_prompt_keeps_active_when():
    """TB-49 set the Backlog<3 gate; TB-70 must NOT change when ideation runs.
    The prompt content evolves but the firing condition is load-bearing.
    """
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    aw = jobs["ideation"].active_when or ""
    assert aw.startswith("sh:")
    assert "Backlog" in aw
    assert "$1>=3" in aw  # the under-full threshold


def test_ideation_prompt_includes_pipeline_task_start_for_long_work():
    """TB-81: ideation must steer long work (>10 min) into a single
    `pipeline_task_start` MCP tool call. Pin the load-bearing terms so a
    future prompt rewrite can't drop them. (Replaces the TB-80 8-step
    recipe pin — the recipe was collapsed into one atomic tool.)
    """
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    prompt = jobs["ideation"].prompt
    lower = prompt.lower()
    # The new tool must be named verbatim — that's what the agent calls.
    assert "pipeline_task_start" in prompt
    # Threshold guidance — a number of minutes the agent uses to decide.
    assert "10 minutes" in lower or "10 min" in lower
    # The launch-task framing survives (fast launch, not mega-task).
    assert "launch task" in lower
    # Validation task must still be mentioned — the `(blocked on: pid:...)`
    # mechanism is what auto-promotes it once the pipeline dies.
    assert "validation" in lower
    # The pid: blocker scheme is the new TB-81 mechanic; mention it so the
    # ideation agent knows the auto-promotion is automatic, not cron-driven.
    assert "pid:" in prompt
    # Heuristic still pinned — short circuit for "do I want a pipeline?".
    assert "progress bar" in lower or "fans out" in lower


def test_ideation_prompt_pins_two_tier_verification_split():
    """TB-86: pipeline-launch ideation must steer output-artifact checks into
    `validation_briefing` (the validation task's verification, runs AFTER the
    pipeline dies) and keep the launch task's top-level `## Verification`
    section limited to checks that pass at launch-completion time. Stoch's
    TB-83/TB-92 retry-exhausted because their launch-task verifications had
    `test -f reports/<name>/grid.csv` bullets that ran while the pipeline
    was still computing — every bullet exited 1.
    """
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    prompt = jobs["ideation"].prompt
    lower = prompt.lower()
    # The two-tier framing must be named so future rewrites don't drop it.
    assert "two-tier" in lower or "where output checks belong" in lower
    # The destination for output checks must be called out by parameter name.
    assert "validation_briefing" in prompt
    # Negative instruction — the load-bearing "DO NOT" that prevents the
    # TB-83/TB-92 failure mode.
    assert "DO NOT put output" in prompt or "do not put output" in lower
    # Concrete grounding: agents need to know what counts as an output check
    # vs a launch-time check. The template-style examples must survive.
    assert "test -f reports/" in prompt
    # The "running detached" rationale connects the rule to its reason.
    assert "running detached" in lower or "still running" in lower


def test_ideation_prompt_warns_off_bare_python_and_path_pitfalls():
    """TB-76: live stoch tasks (TB-71, TB-73) verification_failed solely
    because their shell bullets used bare `python` (claude-agent's env has
    `uv run python` / `python3` only) or treated paths as executable. Pin
    the prompt's guidance so future ideation runs steer agents away.
    """
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    prompt = jobs["ideation"].prompt
    # Bare-python warning — must explicitly recommend uv run python / python3.
    assert "uv run python" in prompt
    assert "python3" in prompt
    # Path-existence-check guidance — must mention `test -f`.
    assert "test -f" in prompt


def test_ideation_prompt_instructs_verification_section_population():
    """TB-69 contract: every ideation-proposed briefing must include a
    `## Verification` section with concrete bullets the verifier can run.
    Pin the prompt language so a future rewrite can't drop this."""
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    prompt = jobs["ideation"].prompt
    assert "## Verification" in prompt
    # The prompt should mention shell bullets (the preferred form).
    lower = prompt.lower()
    assert "shell" in lower
    # And acknowledge the legacy skip path so the agent knows what NOT to do.
    assert "legacy" in lower or "skip" in lower
