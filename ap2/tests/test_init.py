"""Tests for `ap2 init` scaffolding (ap2/init.py).

The scaffolding is the only deterministic source of truth for what an
ap2-managed project ignores vs. tracks. Drift here is what stranded stoch's
`cron.yaml` for weeks and let `*.lock` files leak into the working tree.
"""
from __future__ import annotations

from pathlib import Path

from ap2.config import Config
from ap2.init import (
    ENV_TEMPLATE,
    GOAL_TEMPLATE,
    NESTED_GITIGNORE_BLOCKS,
    ROOT_GITIGNORE_BLOCKS,
    init_project,
)


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def test_creates_files_in_empty_project(tmp_path: Path):
    report = init_project(tmp_path)

    nested = tmp_path / ".cc-autopilot" / ".gitignore"
    root = tmp_path / ".gitignore"
    tasks = tmp_path / ".cc-autopilot" / "tasks"

    assert nested.exists()
    assert root.exists()
    assert tasks.is_dir()
    assert report.tasks_dir_created is True

    # Every entry from each block landed in the right file.
    nested_text = nested.read_text()
    for _, entries in NESTED_GITIGNORE_BLOCKS:
        for e in entries:
            assert e in nested_text, f"missing {e!r} in nested gitignore"

    root_text = root.read_text()
    for _, entries in ROOT_GITIGNORE_BLOCKS:
        for e in entries:
            assert e in root_text, f"missing {e!r} in root gitignore"


def test_load_bearing_entries_present(tmp_path: Path):
    """Pin the entries whose absence caused real bugs in stoch."""
    init_project(tmp_path)
    nested = (tmp_path / ".cc-autopilot" / ".gitignore").read_text()
    root = (tmp_path / ".gitignore").read_text()

    # Secrets must never end up tracked.
    assert "env" in nested
    # Lock files (cron_state.json.lock, retry_state.json.lock) leak otherwise.
    assert "*.lock" in nested
    # On-disk backups created during ap2 upgrades.
    assert "*.bak" in nested
    # Pipeline log dirs (TB-81) — debug-only, never committed.
    assert "pipelines/" in nested
    # Board lock at project root, NOT under .cc-autopilot/.
    assert "TASKS.md.lock" in root


def test_idempotent_no_duplicates_on_rerun(tmp_path: Path):
    init_project(tmp_path)
    nested = tmp_path / ".cc-autopilot" / ".gitignore"
    root = tmp_path / ".gitignore"
    nested_first = nested.read_text()
    root_first = root.read_text()

    report2 = init_project(tmp_path)

    # Second run reports nothing added and writes nothing new.
    assert report2.nested_gitignore_added == []
    assert report2.root_gitignore_added == []
    assert report2.tasks_dir_created is False
    assert nested.read_text() == nested_first
    assert root.read_text() == root_first


def test_unions_with_existing_gitignore(tmp_path: Path):
    """Pre-existing entries are preserved; only missing ones are appended."""
    autopilot = tmp_path / ".cc-autopilot"
    autopilot.mkdir()
    nested = autopilot / ".gitignore"
    # Existing user content + one of our entries already.
    nested.write_text("# user-managed\nmy_local_thing/\nevents.jsonl\n")

    report = init_project(tmp_path)

    text = nested.read_text()
    # User content untouched.
    assert "# user-managed" in text
    assert "my_local_thing/" in text
    # The one of our entries that was already there isn't duplicated.
    assert text.count("events.jsonl") == 1
    # Entries we added are new arrivals, not the pre-existing one.
    assert "events.jsonl" not in report.nested_gitignore_added
    assert "*.lock" in report.nested_gitignore_added


def test_does_not_clobber_root_gitignore_entries(tmp_path: Path):
    """Project's own root .gitignore (e.g. .env, build/) must survive."""
    root = tmp_path / ".gitignore"
    root.write_text(".env\n.venv/\nbuild/\n")

    init_project(tmp_path)

    text = root.read_text()
    for keep in (".env", ".venv/", "build/", "TASKS.md.lock"):
        assert keep in text


def test_existing_tasks_dir_not_clobbered(tmp_path: Path):
    """Briefings already on disk must not be touched."""
    tasks = tmp_path / ".cc-autopilot" / "tasks"
    tasks.mkdir(parents=True)
    brief = tasks / "old-briefing.md"
    brief.write_text("# old briefing")

    report = init_project(tmp_path)

    assert report.tasks_dir_created is False
    assert brief.read_text() == "# old briefing"


def test_creates_tasks_md_with_5_section_template(tmp_path: Path):
    report = init_project(tmp_path)

    tasks = tmp_path / "TASKS.md"
    assert tasks.exists()
    assert report.tasks_md_created is True
    text = tasks.read_text()
    for section in ("## Active", "## Ready", "## Backlog", "## Complete", "## Frozen"):
        assert section in text


def test_creates_progress_md(tmp_path: Path):
    report = init_project(tmp_path)
    progress = tmp_path / ".cc-autopilot" / "progress.md"
    assert progress.exists()
    assert progress.read_text().startswith("# Progress")
    assert report.progress_md_created is True


def test_creates_claude_md_when_missing(tmp_path: Path):
    report = init_project(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.exists()
    assert report.claude_md_created is True
    text = claude_md.read_text()
    assert "## Autopilot" in text
    assert "Next task ID: TB-1" in text


def test_appends_autopilot_to_existing_claude_md(tmp_path: Path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Pre-existing\n\nSome content.\n")

    report = init_project(tmp_path)

    assert report.claude_md_created is False
    assert report.claude_md_autopilot_added is True
    text = claude_md.read_text()
    assert "# Pre-existing" in text
    assert "Some content." in text
    assert "## Autopilot" in text


def test_does_not_re_append_autopilot_section(tmp_path: Path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Project\n\n## Autopilot\n\n- Task list: `TASKS.md`\n")

    report = init_project(tmp_path)

    assert report.claude_md_autopilot_added is False
    assert claude_md.read_text().count("## Autopilot") == 1


def test_does_not_overwrite_existing_tasks_md(tmp_path: Path):
    tasks = tmp_path / "TASKS.md"
    tasks.write_text("# Tasks\n\n## Active\n\n- [ ] **TB-7** **existing** — keep me\n")

    report = init_project(tmp_path)

    assert report.tasks_md_created is False
    assert "TB-7" in tasks.read_text()


def test_init_output_is_loadable_by_config(tmp_path: Path):
    """End-to-end: a freshly-init'd project must `Config.load()` cleanly."""
    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    assert cfg.tasks_file == (tmp_path / "TASKS.md").resolve()
    assert cfg.tasks_file.exists()
    assert (tmp_path / ".cc-autopilot" / "progress.md").exists()


def test_creates_insights_dir_with_placeholder_index(tmp_path: Path):
    """TB-89: bootstrap creates `.cc-autopilot/insights/` + placeholder index."""
    report = init_project(tmp_path)
    insights_dir = tmp_path / ".cc-autopilot" / "insights"
    assert insights_dir.is_dir()
    assert report.insights_dir_created is True
    index = insights_dir / "_index.md"
    assert index.exists()
    text = index.read_text()
    assert "Insights index" in text
    assert "no insights yet" in text


def test_does_not_overwrite_existing_insights_index(tmp_path: Path):
    """A pre-existing `_index.md` written by ap2 in a prior cycle survives
    re-running `init_project` unchanged. The lazy regen path will rebuild
    it on the next ideation cron tick if files have changed."""
    autopilot = tmp_path / ".cc-autopilot"
    insights_dir = autopilot / "insights"
    insights_dir.mkdir(parents=True)
    index = insights_dir / "_index.md"
    index.write_text("# Insights index\n\n- `kept.md` — already-here entry\n")

    init_project(tmp_path)

    assert index.read_text() == "# Insights index\n\n- `kept.md` — already-here entry\n"


def test_insights_dir_idempotent(tmp_path: Path):
    init_project(tmp_path)
    report2 = init_project(tmp_path)
    assert report2.insights_dir_created is False


def test_creates_ideation_state_md_when_missing(tmp_path: Path):
    """TB-87: bootstrap places a placeholder `ideation_state.md` so first-cycle
    reads succeed before ideation has run.
    """
    report = init_project(tmp_path)
    state = tmp_path / ".cc-autopilot" / "ideation_state.md"
    assert state.exists()
    assert report.ideation_state_md_created is True
    text = state.read_text()
    assert "# Ideation State" in text
    assert "Not yet generated" in text


def test_does_not_overwrite_existing_ideation_state_md(tmp_path: Path):
    """A pre-existing `ideation_state.md` written by a prior ideation cycle
    must survive `init_project` re-run unchanged (idempotency)."""
    autopilot = tmp_path / ".cc-autopilot"
    autopilot.mkdir()
    state = autopilot / "ideation_state.md"
    state.write_text("# Custom\n\n## Mission alignment\nReal assessment here.\n")

    report = init_project(tmp_path)

    assert report.ideation_state_md_created is False
    assert state.read_text() == "# Custom\n\n## Mission alignment\nReal assessment here.\n"


def test_ideation_state_md_idempotent(tmp_path: Path):
    """Second init_project call is a no-op for ideation_state.md."""
    init_project(tmp_path)
    state = tmp_path / ".cc-autopilot" / "ideation_state.md"
    first = state.read_text()

    report2 = init_project(tmp_path)

    assert report2.ideation_state_md_created is False
    assert state.read_text() == first


def test_creates_goal_md_when_missing(tmp_path: Path):
    """Fresh project gets a templated goal.md so the ideation cron has an
    explicit project-level anchor to read (TB-70). TB-199 adds the
    `## Done when` section so the TB-161 anchor validator's Done-when
    surface is populated out of the box."""
    report = init_project(tmp_path)
    goal = tmp_path / "goal.md"
    assert goal.exists()
    assert report.goal_md_created is True
    text = goal.read_text()
    # Pin the five sections the ideation prompt and the TB-161 anchor
    # validator expect to find.
    for section in ("# Project Goals", "## Mission", "## Done when",
                    "## Current focus", "## Non-goals", "## Constraints"):
        assert section in text
    # Placeholder body mentions "criterion" / "criteria" (TB-199's
    # verification: "criterion (or equivalent)") so an operator reading
    # the template learns what belongs in Done-when without consulting
    # docs.
    done_when_idx = text.index("## Done when")
    current_focus_idx = text.index("## Current focus")
    done_when_body = text[done_when_idx:current_focus_idx].lower()
    assert "criterion" in done_when_body or "criteria" in done_when_body, (
        f"Done-when placeholder must explain what belongs there "
        f"(mention criterion / criteria); got:\n{done_when_body!r}"
    )


def test_goal_template_section_order_is_canonical():
    """TB-199: section order pins Mission → Done when → Current focus →
    Non-goals → Constraints. Strategic framing (Mission + Done-when =
    what success looks like) is grouped before the tactical state
    (Current focus + Constraints). Order is load-bearing — a future
    refactor that reshuffles the template should explicitly update this
    test rather than silently drift."""
    sections = (
        "## Mission",
        "## Done when",
        "## Current focus",
        "## Non-goals",
        "## Constraints",
    )
    positions = [GOAL_TEMPLATE.index(s) for s in sections]
    assert positions == sorted(positions), (
        f"GOAL_TEMPLATE sections out of canonical order: {sections} "
        f"resolved to positions {positions}"
    )


def test_goal_template_round_trips_through_briefing_validator(tmp_path: Path):
    """TB-199 round-trip: `init_project` writes goal.md from
    GOAL_TEMPLATE; a minimal briefing whose `## Goal` body quotes the
    Done-when placeholder text verbatim passes
    `_validate_briefing_structure`. Pins the day-one fresh-project
    contract: the all-placeholder goal.md (with the new Done-when
    section) is anchor-empty so the validator's TB-161 anchor check
    short-circuits to skip rather than reject every minimal briefing.
    Anchors emerge the moment an operator replaces the `(TODO)` stub
    with real shipped-when criteria — at which point briefings must
    cite them, exactly as for any operator-filled `## Current focus`.
    """
    # Lazy import — `tools` pulls in MCP machinery that's heavier than
    # the rest of test_init needs.
    from ap2 import tools

    init_project(tmp_path)
    goal_md = tmp_path / "goal.md"
    assert goal_md.exists()

    # Sanity-check the day-one fresh-project property: the template
    # itself contributes zero anchors so the TB-161 validator falls
    # through to skip on a freshly-initialized project. If a future
    # refactor adds anchoring text to the placeholder, this assertion
    # fires first (clear error) before the downstream validator-accept
    # check fails in a more confusing way.
    from ap2.tools import _goal_md_anchors
    assert _goal_md_anchors(goal_md) == set(), (
        "fresh init_project goal.md must contribute zero anchors so the "
        "TB-161 validator skips on day one; placeholder body has drifted"
    )

    # Quote the placeholder text verbatim in the briefing's `## Goal`
    # body — the round-trip case the TB-199 verification description
    # names. With the all-placeholder skip path active, the validator
    # accepts regardless of whether the briefing cites the Done-when
    # text or not; this test still pins the explicit shape an operator
    # would author on day one (no anchor obligation, but the briefing
    # nonetheless references the Done-when surface).
    briefing = (
        "# tb-199-roundtrip\n\n"
        "## Goal\n\n"
        "Quoting the fresh template's `## Done when` placeholder: "
        "fill in a bulleted list of concrete \"the project ships when "
        "X\" criteria — e.g. \"the API handles N requests/sec at p99 "
        "latency Xms in production\".\n\n"
        "Why now: closes the template/validator drift the briefing "
        "names — fresh projects had no Done-when surface at all.\n\n"
        "## Scope\n\n- ap2/init.py\n\n"
        "## Design\n\nGrow GOAL_TEMPLATE with a Done-when section.\n\n"
        "## Verification\n\n- `uv run pytest -q` — gates pass\n\n"
        "## Out of scope\n\n- nothing\n"
    )
    err = tools._validate_briefing_structure(briefing, goal_md_path=goal_md)
    assert err is None, (
        f"briefing quoting the GOAL_TEMPLATE Done-when placeholder "
        f"should pass the validator on a freshly-initialized project; "
        f"got: {err!r}"
    )


def test_does_not_overwrite_existing_goal_md(tmp_path: Path):
    """Pre-existing goal.md with custom content survives init unchanged."""
    goal = tmp_path / "goal.md"
    goal.write_text("# Custom\n\n## Mission\nMake widgets.\n")

    report = init_project(tmp_path)

    assert report.goal_md_created is False
    assert goal.read_text() == "# Custom\n\n## Mission\nMake widgets.\n"


def test_goal_md_idempotent(tmp_path: Path):
    """Second init_project call is a no-op for goal.md."""
    init_project(tmp_path)
    goal = tmp_path / "goal.md"
    first = goal.read_text()

    report2 = init_project(tmp_path)

    assert report2.goal_md_created is False
    assert goal.read_text() == first


def test_partial_state_only_appends_missing(tmp_path: Path):
    """If init had been run before but the template was extended later
    (e.g. we added *.lock and *.bak in TB-68), re-running picks up just the
    new entries — no header churn for blocks that are fully present.
    """
    autopilot = tmp_path / ".cc-autopilot"
    autopilot.mkdir()
    nested = autopilot / ".gitignore"
    # Simulate the pre-TB-68 template: full Runtime block, full debug block,
    # full env block, but missing *.lock and *.bak.
    pre = "\n".join(
        ["# Runtime — per-user, not committed"]
        + NESTED_GITIGNORE_BLOCKS[0][1]
        + ["", "# Per-run prompt + stream dumps for failure diagnosis (kept only on failure)"]
        + NESTED_GITIGNORE_BLOCKS[1][1]
        + ["", "# Local/sandbox-specific env (secrets, channel IDs) — keep out of git"]
        + NESTED_GITIGNORE_BLOCKS[2][1]
    )
    nested.write_text(pre + "\n")

    report = init_project(tmp_path)

    assert "*.lock" in report.nested_gitignore_added
    assert "*.bak" in report.nested_gitignore_added
    # Already-present entries don't show up as added.
    assert "events.jsonl" not in report.nested_gitignore_added


# ---------------------------------------------------------------------------
# TB-278: .cc-autopilot/env documented template scaffolding
# ---------------------------------------------------------------------------


def test_creates_env_template_when_missing(tmp_path: Path):
    """Fresh project gets a documented `.cc-autopilot/env` template. Post-
    TB-413/TB-414 the env surface is SECRETS + DEPLOYMENT-IDENTITY only —
    behavioral tunables live in `.cc-autopilot/config.toml` — so the
    template documents the `config.ENV_PERMITTED_KEYS` allowlist, not the
    flat `AP2_<tunable>` examples it carried pre-TB-414.

    Pin:
      - File exists at `.cc-autopilot/env`.
      - Report flag flipped to True (first-init signal).
      - Content matches `ENV_TEMPLATE` verbatim.
      - The secrets + deployment-identity knobs are each mentioned,
        commented-out (no uncommented `KEY=VALUE` override).
    """
    report = init_project(tmp_path)
    env = tmp_path / ".cc-autopilot" / "env"

    assert env.exists()
    assert report.env_template_created is True
    text = env.read_text()
    assert text == ENV_TEMPLATE, (
        "init_project must write the canonical ENV_TEMPLATE verbatim "
        "when `.cc-autopilot/env` is absent"
    )

    # The secrets + deployment-identity allowlist knobs must each appear
    # commented-out. Pin existence + the leading `# ` fence so a future
    # refactor that flips one to live (silently overriding) trips here.
    for knob in (
        "AP2_MM_CHANNELS",
        "AP2_WEB_PORT",
        "AP2_WEB_DISABLED",
        "AP2_SANDBOX_USER",
        "AP2_PROJECT_NAME",
        "AP2_CHANNEL_FILE_PATH",
        "AP2_TICK_S",
        "AP2_MM_TICK_S",
        "AP2_MM_BOT_USER_ID",
        "AP2_WEBHOOK_URL",
    ):
        assert knob in text, (
            f"env template must document the deployment-identity / secret "
            f"knob {knob!r} (TB-414 allowlist)"
        )
        # Every knob mention must sit behind a `# ` so it's prose, not a
        # live override. The check is per-line: any uncommented
        # `KNOB=` line would be a regression.
        for line in text.splitlines():
            if knob in line and "=" in line:
                stripped = line.lstrip()
                assert stripped.startswith("# "), (
                    f"env template must keep {knob!r} commented-out so the "
                    f"file documents without overriding defaults; "
                    f"found live line: {line!r}"
                )


def test_env_template_is_secrets_and_deployment_identity_only(tmp_path: Path):
    """TB-414 contract: the env scaffold documents ONLY secrets +
    deployment-identity, NOT behavioral tunables. The flat `AP2_<tunable>`
    override was removed in TB-413, so a scaffolded env still listing
    `AP2_TASK_MAX_TURNS` / `AP2_AGENT_MODEL` / … would re-teach a retired
    pattern. Pin: those behavioral-tunable flat names are absent, and the
    header points operators at config.toml + `ap2 config set`.
    """
    init_project(tmp_path)
    text = (tmp_path / ".cc-autopilot" / "env").read_text()

    # Representative behavioral tunables must NOT be scaffolded into env.
    for tunable in (
        "AP2_AGENT_MODEL",
        "AP2_AGENT_BACKEND",
        "AP2_AGENT_EFFORT",
        "AP2_TASK_MAX_TURNS",
        "AP2_TASK_TIMEOUT_S",
        "AP2_CONTROL_TIMEOUT_S",
        "AP2_IDEATION_MAX_TURNS",
        "AP2_IDEATION_TRIGGER_TASK_COUNT",
        "AP2_VERIFY_CMD",
        "AP2_VERIFY_TIMEOUT_S",
        "AP2_ATTENTION_IMMEDIATE_PUSH",
        "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP",
        "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP",
    ):
        assert tunable not in text, (
            f"env template must NOT scaffold the behavioral tunable "
            f"{tunable!r} (config.toml is its sole source post-TB-413)"
        )

    # The header must steer operators to config.toml + `ap2 config set`.
    assert "config.toml" in text
    assert "ap2 config set" in text


def test_does_not_clobber_existing_env_file(tmp_path: Path):
    """A pre-existing `.cc-autopilot/env` with operator secrets / channel
    IDs / tuned overrides MUST survive `init_project` re-run unchanged.
    Idempotency contract: init never stomps an operator's env (the file
    is gitignored, project-scoped, and typically carries
    AP2_MM_CHANNELS / AP2_VERIFY_CMD / agent-effort overrides).
    """
    autopilot = tmp_path / ".cc-autopilot"
    autopilot.mkdir()
    env = autopilot / "env"
    # Realistic operator content — secrets, channel IDs, real overrides.
    pre = (
        "# Operator-curated; init must NOT touch this.\n"
        "AP2_VERIFY_CMD=uv run pytest -q\n"
        "AP2_VERIFY_TIMEOUT_S=1800\n"
        "AP2_MM_CHANNELS=u4e41y7gr78zupikzus8huw6kr\n"
        "AP2_TASK_MAX_TURNS=500\n"
    )
    env.write_text(pre)

    report = init_project(tmp_path)

    # File content is byte-identical post-init.
    assert env.read_text() == pre, (
        "init_project clobbered an existing .cc-autopilot/env — "
        "operator overrides / secrets / channel IDs would be lost"
    )
    # Report flag flipped to False so the operator-visible print and any
    # caller-side branching see "kept existing", not "wrote template".
    assert report.env_template_created is False


def test_env_template_scaffold_is_idempotent(tmp_path: Path):
    """Second `init_project` call is a no-op for the env file (matches the
    goal.md / ideation_state.md / insights_dir idempotency contract).
    The first init wrote the template; the second sees the file already
    present and reports `env_template_created=False`."""
    init_project(tmp_path)
    env = tmp_path / ".cc-autopilot" / "env"
    first = env.read_text()

    report2 = init_project(tmp_path)

    assert report2.env_template_created is False
    assert env.read_text() == first
