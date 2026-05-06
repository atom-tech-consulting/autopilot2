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
    """The ideation prompt mentions `pipeline_task_start` so a long-work
    proposer knows the tool exists, but otherwise stays out of the
    inline-vs-pipeline decision (the task agent owns it at run time).
    Pinned: tool name + 5-min threshold + "task agent" framing.
    """
    prompt = _default_prompt()
    lower = prompt.lower()
    assert "pipeline_task_start" in prompt
    assert "5 minutes" in lower or "5 min" in lower
    assert "task agent" in lower


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


def test_ideation_prompt_says_briefings_for_long_work_use_same_shape():
    """The ideation prompt instructs that long-running work uses the same
    briefing shape as synchronous work — concrete scope + `## Verification`
    bullets that check output artifacts. No two-tier split, no separate
    validation_briefing sub-document.
    """
    prompt = _default_prompt()
    lower = prompt.lower()
    assert "## Verification" in prompt
    assert "test -f reports/" in prompt
    # Retired patterns must not creep back in.
    assert "two-tier" not in lower
    assert "validation_briefing" not in prompt


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


def test_ideation_prompt_requires_auto_verifiable_bullets_only():
    """TB-138: ideation must NEVER propose `Manual:` bullets — the per-task
    verifier runs unattended and cannot observe live operator actions
    (TB-122 hit `retry_exhausted` on a single manual bullet despite
    implementation being complete). Pin the auto-verifiable rule, the
    explicit no-Manual-bullet ban, the canonical TB-122 conversion
    example, and the "out of scope" escape hatch so future prompt
    rewrites don't silently drop the rule.
    """
    prompt = _default_prompt()
    lower = prompt.lower()
    # Rule keyword. Allow either "auto-verifiable" or "auto verifiable".
    assert "auto-verifiable" in lower or "auto verifiable" in lower
    # Explicit ban on manual bullets.
    assert "Manual:" in prompt or "manual:" in lower
    # The three valid bullet shapes are enumerated.
    assert "shell command" in lower
    assert "test" in lower
    assert "diff" in lower or "working tree" in lower
    # TB-122 cited as the canonical conversion example.
    assert "TB-122" in prompt
    # The "if you can't auto-verify, it's out of scope" escape hatch.
    assert "Out of scope" in prompt or "out of scope" in lower
    # No `## Manual checklist` end-run is suggested.
    assert "Manual checklist" not in prompt or "Do not invent" in prompt or "do not invent" in lower


def test_ideation_prompt_surfaces_cron_proposals_does_not_adopt():
    """TB-146: ideation MUST NOT adopt `cron_proposed` events via
    `cron_edit` — that tool is no longer in any agent toolset (operator
    promotes via `ap2 cron edit`). The prompt should instead instruct
    ideation to SURFACE unadopted proposals (e.g. in its per-cycle
    assessment) so the operator sees what's pending. Pin both:
      (a) the prompt does NOT instruct calling `cron_edit`,
      (b) the prompt mentions `cron_proposed` events in a SURFACE-not-
          adopt framing that names `cron_propose` and `ap2 cron edit`.
    """
    prompt = _default_prompt()
    flat = " ".join(prompt.split())
    lower = flat.lower()
    # (a) No instruction to invoke `cron_edit` (the retired direct-mutation
    # path). The token may appear in cross-references, but never as an
    # instruction like "call cron_edit" / "via cron_edit" / "use
    # cron_edit". Tolerate the `ap2 cron edit` CLI form (operator-side).
    for forbidden in (
        "call `cron_edit`",
        "call cron_edit",
        "via `cron_edit`",
        "via cron_edit",
        "use `cron_edit`",
        "use cron_edit",
        "adopt via cron_edit",
        "adopt via `cron_edit`",
    ):
        assert forbidden.lower() not in lower, (
            f"ideation prompt instructs `cron_edit` use ({forbidden!r}); "
            f"TB-146 removed that path — surface proposals instead"
        )
    # (b) The prompt acknowledges `cron_proposed` events and names the
    # surface-not-adopt framing.
    assert "cron_proposed" in prompt
    assert "cron_propose" in prompt
    assert "ap2 cron edit" in prompt
    assert "surface" in lower or "SURFACE" in prompt
    # Pin the explicit "do not adopt" instruction so a paraphrase that
    # only mentions surfacing without forbidding adoption regresses here.
    assert "do not adopt" in lower or "cannot adopt" in lower or "Do NOT" in prompt


def test_ideation_prompt_pins_tb146_section_header():
    """TB-146: the cron-proposal handling note must live as a discoverable
    section in the ideation prompt so a contributor scrolling the file
    can find it without grepping for arbitrary phrases. Pin the section
    title + the TB-146 cross-ref."""
    prompt = _default_prompt()
    assert "TB-146" in prompt
    # Some heading or block-level mention; the exact wording is flexible
    # but `cron proposals` should appear as a topical anchor.
    lower = prompt.lower()
    assert "cron proposals from task agents" in lower or "cron proposals" in lower


def test_ideation_prompt_pins_review_gate_clause():
    """TB-121: every task ideation proposes MUST be gated behind operator
    review before dispatch. Pin both the directive (pass `blocked_on:
    "review"` to every `add_backlog` call) and the briefing's grep
    anchor (`blocked on: review` literal phrase). Without this, a
    hallucinated proposal pipelines straight into the daemon's
    autonomous dispatch loop with no human in the loop — the failure
    mode the gate prevents.
    """
    prompt = _default_prompt()
    flat = " ".join(prompt.split())
    lower = flat.lower()
    # Verification grep anchor — the briefing's `## Verification` runs
    # `grep -q 'blocked on: review' ap2/ideation.default.md`.
    assert "blocked on: review" in lower
    # The directive instructs ideation to attach the gate to every
    # `board_edit({"action": "add_backlog", ...})` it emits.
    assert "blocked_on" in prompt
    assert "review" in lower
    assert "add_backlog" in prompt
    # CLI surface mentioned so ideation's audit (and the operator
    # reading the prompt) sees the canonical promotion path.
    assert "ap2 approve" in prompt
    # TB-121 cross-ref so future trims preserve the lineage.
    assert "TB-121" in prompt


def test_ideation_prompt_explains_why_gate_is_uniform():
    """The gate applies to every proposal, not just "non-trivial" ones —
    the operator decides what's trivial. Pin the explicit "do not skip"
    instruction so a paraphrase that drops the universality reverses
    the policy."""
    prompt = _default_prompt()
    flat = " ".join(prompt.split())
    lower = flat.lower()
    # "Do NOT skip" / "uniform" — tolerate either phrasing.
    assert (
        "do not skip" in lower
        or "uniform" in lower
        or "every proposal" in lower
    )


def test_ideation_prompt_forbids_tasks_awaiting_review_in_open_questions():
    """TB-182: the `## Open questions for operator` schema fragment must
    NOT instruct ideation to write "Tasks awaiting review" / "TB-N
    awaiting approval" bullets — those duplicate the mechanically-
    derived `Pending operator review (N): TB-...` line that
    `ap2 status` (CLI) and the cron status-report inject from current
    board state per run (TB-151 / TB-173). When the gap between
    ideation cycles diverges from current board state (e.g. an
    `ap2 approve` lands in the gap) the two lines actively
    contradict each other in the same Mattermost post.

    Pin both halves:
      (a) the prompt does NOT instruct ideation to LIST tasks-awaiting-
          review TB-Ns inside `ideation_state.md` (the pre-TB-182
          phrasing — `List the tasks awaiting review in your
          ideation_state.md ...`);
      (b) the prompt explicitly PROHIBITS that content with a
          greppable phrase (`tasks-awaiting-review`) so an editor
          regression that removes the prohibition trips this test.
    """
    prompt = _default_prompt()
    flat = " ".join(prompt.split())
    lower = flat.lower()
    # (a) The pre-TB-182 instruction is gone. The exact sentence was
    # "List the tasks awaiting review in your ideation_state.md
    # 'Open questions for operator' section ..."; assert the directive
    # form ("list the tasks awaiting review in your ideation_state")
    # no longer appears.
    assert "list the tasks awaiting review in your ideation_state" not in lower, (
        "TB-182: ideation prompt still instructs listing tasks-awaiting-"
        "review TB-Ns in ideation_state.md — that duplicates the "
        "mechanical Pending-review line and is forbidden."
    )
    # (b) The new prohibition is explicit and greppable.
    assert "tasks-awaiting-review" in lower, (
        "TB-182: ideation prompt is missing the explicit prohibition "
        "phrase 'tasks-awaiting-review' — the schema fragment should "
        "say `Do NOT include tasks-awaiting-review bullets`."
    )
    assert "TB-182" in prompt, (
        "TB-182 cross-ref expected in the prompt for future trims to "
        "preserve the lineage."
    )

    # Schema-fragment-local check: between `## Open questions for
    # operator` (the schema fragment near the top of the file, NOT
    # `### Open questions for operator` or other appearances) and the
    # next `## ` heading inside the schema, the literal substring
    # "Tasks awaiting review" must NOT appear (case-insensitive).
    schema_idx = prompt.find("## Open questions for operator")
    assert schema_idx >= 0
    # The schema fragment is indented (it's inside a code block in the
    # markdown). Find the next `## ` heading that immediately follows.
    after = prompt[schema_idx + len("## Open questions for operator"):]
    next_section_idx = after.find("## Proposals this cycle")
    assert next_section_idx >= 0, (
        "schema fragment is missing the trailing `## Proposals this "
        "cycle` anchor; this test relies on that anchor"
    )
    section_body = after[:next_section_idx]
    assert "tasks awaiting review" not in section_body.lower(), (
        f"TB-182: the `## Open questions for operator` schema fragment "
        f"contains a 'Tasks awaiting review' bullet — that is the "
        f"redundancy this task removes. Section body:\n{section_body!r}"
    )


def test_ideation_prompt_does_not_contain_a_manual_bullet():
    """Self-consistency: the ideation prompt itself MUST NOT use a
    `- Manual: ...` bullet anywhere. Catches accidental regression where
    an editor pastes an example that violates the very rule the prompt
    is teaching.
    """
    prompt = _default_prompt()
    import re as _re
    assert not _re.search(r"(?m)^\s*[-*]\s*Manual\s*:", prompt), (
        "ideation prompt contains a `- Manual: ...` bullet — TB-138 forbids "
        "these in any briefing, and the prompt itself must lead by example"
    )


# ---------------------------------------------------------------------------
# Helpers


def _stub_config(tmp_path: Path) -> Config:
    """Minimal Config — only project_root is needed for load_prompt."""
    return Config.load(tmp_path)
