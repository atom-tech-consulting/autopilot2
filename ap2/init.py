"""Project-init scaffolding: gitignores, dirs, marker files, board templates.

The single source of truth for what an ap2-managed project should ignore vs.
track, and what the bare minimum on-disk skeleton looks like. Replaces the
manual transcribe-from-skill-markdown flow that left stoch's `cron.yaml`
untracked for weeks and silently accumulated `.lock` / `.bak` files.

Idempotent: re-running unions gitignores, never touches existing TASKS.md /
progress.md / CLAUDE.md content (only writes if missing or appends a new
`## Autopilot` block to a CLAUDE.md that lacks one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import AUTOPILOT_DIR_NAME


TASKS_TEMPLATE = (
    "# Tasks\n\n"
    "## Active\n\n"
    "## Ready\n\n"
    "## Backlog\n\n"
    "## Pipeline Pending\n\n"
    "## Complete\n\n"
    "## Frozen\n"
)

PROGRESS_TEMPLATE = "# Progress\n"

CLAUDE_AUTOPILOT_TEMPLATE = (
    "## Autopilot\n\n"
    "- Task list: `TASKS.md`\n"
    "- Task briefings: `.cc-autopilot/tasks/`\n"
    "- Progress log: `.cc-autopilot/progress.md`\n"
    "- Next task ID: TB-1\n"
)

# Project-level goals/non-goals anchor read by the ideation cron (TB-70). The
# template is intentionally short — humans fill it in. Empty/skeleton values
# are tolerated by the ideation prompt, which falls back to inferring goals
# from CLAUDE.md + progress.md when the file is missing or all-placeholder.

# TB-154: canonical `##`-level section names every briefing must carry.
# Single source of truth — used by:
#   - `BRIEFING_TEMPLATE` below (the empty-add scaffold and editor-mode buffer).
#   - `ap2/tools.py::_validate_briefing_structure` (queue-append-time hard gate).
#   - `ap2/check.py::_check_briefing_structure` (on-disk lint, warning-level).
#   - `ap2/prompts.py` MM handler instructions + `operator_queue_append`
#     docstring (so the agent reads the requirement before authoring).
# Order is rendered order in the template; the validator only checks
# presence, not order — extension is fine, omission/rename is not.
BRIEFING_REQUIRED_SECTIONS: tuple[str, ...] = (
    "Goal",
    "Scope",
    "Design",
    "Verification",
    "Out of scope",
)

# TB-161: goal.md heading prefixes the briefing's `## Goal` body must cite
# (as a substring) for the goal-anchor check. Single source of truth — used
# by:
#   - `ap2/tools.py::_goal_md_anchors` (queue-append-time hard gate inside
#     `_validate_briefing_structure`).
#   - `ap2/check.py::_check_briefing_structure` (warning-level lint for
#     legacy on-disk briefings).
#   - `ap2/prompts.py` MM handler instructions + `operator_queue_append`
#     docstring + `ap2/ideation.default.md` so the briefing-author agent
#     reads the requirement before authoring.
# Default headings are `Current focus` and `Done when`. Prefix match is
# case-insensitive on a `##`-level heading title — `## Current focus:
# ideation quality` and `## Done when` both qualify. Bullet anchors are
# only mined from `Done when` sections; for `Current focus` only the full
# heading title is used (the body is typically prose, not enumerable
# anchors). Extension is allowed but explicitly out-of-scope this cycle.
GOAL_ANCHOR_HEADINGS: tuple[str, ...] = (
    "Current focus",
    "Done when",
)

# TB-164: minimum non-marker character count for the "Why now" rationale
# that every briefing's `## Goal` body must include. Pulled out as a
# named constant so the validator (`ap2/tools.py`), the lint
# (`ap2/check.py`), and the unit tests pin against the same number; a
# future tweak to the threshold flows from one place. 40 chars rejects
# trivial passes like `Why now: yes` while staying short enough that
# templates don't feel padded — the briefing's "if we delete this and
# the goal still ships, was it useful?" delete-test (goal.md lines
# 61-70) needs a sentence, not an essay. The marker check itself is
# line-anchored so the rule isn't matched mid-prose.
WHY_NOW_MIN_CHARS: int = 40

BRIEFING_TEMPLATE = (
    "# {task_id} — {title}\n\n"
    "Tags: \n\n"
    "## Goal\n\n"
    "{description}\n\n"
    "Why now (delete-test): (one sentence answering goal.md's "
    "delete-test — \"if we delete this and the goal still ships, was "
    "it useful?\" — name the failure mode this closes or the gap it "
    "fills, not just \"this would be nice to have\")\n\n"
    "## Scope\n\n"
    "- (file / module to change)\n\n"
    "## Design\n\n"
    "(how this will be built — surface, data flow, edge cases)\n\n"
    "## Verification\n\n"
    "Concrete acceptance criteria the daemon's per-task verifier (TB-69)\n"
    "runs after the agent's commit. Auto-verifiable bullets only (TB-138)\n"
    "— every bullet must be auto-verifiable: (1) a backticked shell command\n"
    "the verifier can `/bin/sh -c`, (2) a unit/e2e test name the regression\n"
    "gate covers, or (3) a prose claim naming a concrete file/symbol an SDK\n"
    "judge can confirm against the diff. No `Manual:` bullets — the\n"
    "verifier runs unattended and cannot observe a live operator action;\n"
    "if a behavior cannot be auto-verified, it belongs in `## Out of scope`.\n\n"
    "- `uv run pytest -q` — full suite passes\n\n"
    "## Out of scope\n\n"
    "- (filled in)\n"
)


def render_briefing(
    *,
    task_id: str,
    title: str,
    description: str = "",
) -> str:
    """Fill `BRIEFING_TEMPLATE` for a new task.

    Pure formatter — no side effects, idempotent for the same inputs.

    History: pre-TB-135 this was called from `do_board_edit` /
    `do_operator_queue_append` to auto-populate a skeleton briefing when
    `add_backlog` was invoked with no payload. That auto-fill path was
    retired (TB-135) — `## Verification` lacking real bullets meant the
    per-task verifier scored prose placeholders against an empty diff and
    "passed" with zero scope-specific evidence, completing tasks like
    TB-131 on regression-gate pass alone. Briefing authorship is now the
    caller's responsibility (CLI: `--briefing-file`; ideation / MM
    handler: build the payload directly). The function survives only as
    a convenience formatter — the CLI uses it (`ap2 add` editor mode,
    pending) and the test suite pins its shape.
    """
    desc = description.strip() or "(one-paragraph description of what success looks like)"
    return BRIEFING_TEMPLATE.format(
        task_id=task_id,
        title=title,
        description=desc,
    )


# TB-199: `## Done when` sits between Mission and Current focus so the
# strategic framing (Mission + Done-when = what success looks like) is
# grouped before the tactical state (Current focus + Constraints).
# Mirrors this project's own goal.md ordering. The section is load-
# bearing for TWO surfaces:
#   - ideation's done-signal — all-met Done-when criteria are how
#     ideation recognizes "stop proposing here" on a finished project.
#   - the TB-161 goal-anchor validator — `_goal_md_anchors` mines
#     Done-when bullets for substring anchors a briefing's `## Goal`
#     body must cite. Pre-TB-199 the shipped template omitted the
#     section, so fresh `ap2 init` projects could only anchor against
#     `## Current focus` until an operator hand-added Done-when.
#
# Placeholder body shape is deliberate: explanatory prose describing
# what belongs (mentions "criterion", names the "stop proposing here"
# done-signal, gives an example, references the model) followed by a
# single `- (TODO)` stub bullet. The stub keeps the "bulleted list"
# hint visible but its body normalizes to <3 words, so
# `_bullet_anchor_phrase` rejects it — the placeholder template
# contributes zero live anchors. That preserves the TB-161 day-one
# fresh-project skip path: a project whose `goal.md` is still the
# all-placeholder template doesn't reject every proposal on day one.
# Anchors emerge naturally the moment the operator replaces the stub
# with real shipped-when criteria (the section is "live" the instant
# real content lands; the placeholder is just inert).
GOAL_TEMPLATE = (
    "# Project Goals\n\n"
    "## Mission\n"
    "(one-sentence statement of what this project is FOR)\n\n"
    "## Done when\n"
    "(Fill in a bulleted list of concrete \"the project ships when X\"\n"
    "criteria — e.g. \"the API handles N requests/sec at p99 latency\n"
    "Xms in production\". Ideation reads these as the done-signal:\n"
    "all-met criteria mean \"stop proposing here\". See goal-draft.md's\n"
    "own Done-when examples for shape.)\n\n"
    "- (TODO)\n\n"
    "## Current focus\n"
    "- (area or theme actively in flight now)\n\n"
    "## Non-goals\n"
    "- (explicit things this project is NOT trying to do, so ideation\n"
    "  doesn't propose them)\n\n"
    "## Constraints\n"
    "- (hard constraints — tech stack, deadlines, dependencies,\n"
    "  blast-radius limits)\n"
)


# Living progress assessment maintained by ideation each cycle (TB-87).
# Ideation reads the prior assessment + goal.md + Complete tail, then
# OVERWRITES this file with a fresh assessment that grounds the cycle's
# proposals in cited TB-Ns. Schema is fixed by the cron prompt — this is
# just a placeholder so first-cycle reads don't fail.
# Placeholder for `.cc-autopilot/insights/_index.md` — overwritten by
# `ap2/insights.py::regenerate_index` at the first ideation cron tick after a
# real insight file is added. Tracked in git so cloning the repo is enough to
# get a runnable scaffold (TB-89).
INSIGHTS_INDEX_PLACEHOLDER = (
    "# Insights index\n"
    "_Auto-generated by ap2 before each ideation cron tick; do not edit._\n\n"
    "(no insights yet — drop markdown files into this directory with YAML\n"
    "front matter to surface them here.)\n"
)


IDEATION_STATE_TEMPLATE = (
    "# Ideation State\n\n"
    "_Not yet generated. Will be written on the next ideation cron tick.\n"
    "Schema (set by `ap2/cron.default.yaml`'s ideation prompt):\n"
    "Mission alignment / Current focus assessment / Non-goal risk check /\n"
    "Considered & deferred / Cycle observations / Decisions needed from\n"
    "operator / Proposals._\n"
)


# Lines that go into <project>/.cc-autopilot/.gitignore. Grouped by purpose so
# diffs against an existing file are minimal and readable.
NESTED_GITIGNORE_BLOCKS: list[tuple[str, list[str]]] = [
    ("Runtime — per-user, not committed", [
        "flag",
        "checkpoints/",
        "sessions/",
        "metrics/",
        "decisions.log",
        "context.json",
        "events.jsonl",
        "daemon.pid",
        "daemon.log",
        "paused",
        # Cron last-fired / mm cursor / auto-diagnose cooldown stay
        # gitignored: rollback (TB-111) should NOT re-fire crons,
        # replay mattermost, or re-trigger the watchdog. Ephemeral
        # runtime state lives outside git so it flows forward across
        # rollback boundaries.
        "cron_state.json",
        "mm_state.json",
        "auto_diagnose_state.json",
        # `retry_state.json` was here pre-TB-112; it's now committed
        # as part of `_STATE_FILE_NAMES` so rollback restores per-task
        # retry budgets coherently.
        # TB-131: operator-queue jsonl (CLI / MM-handler appends drained
        # at the next tick) and its applied-uuid state file. Ephemeral
        # runtime — the drained ops materialize as TASKS.md edits which
        # ARE committed; the queue itself shouldn't ride along.
        "operator_queue.jsonl",
        "operator_queue_state.json",
        # TB-226: focus-list runtime pointer (which `## Current focus:`
        # heading in goal.md is active, the heuristic empty-cycles
        # counter, exhausted titles, roadmap-complete ack idx). In-memory
        # runtime state — goal.md itself stays operator-owned and IS
        # committed; the pointer flowing forward across rollbacks would
        # also re-fire `focus_advanced` events redundantly, so the
        # pointer (like `cron_state.json`) should restart fresh.
        "focus_pointer.json",
        # TB-260: per-daemon-lifetime env-file-mtime stash powering the
        # `.cc-autopilot/env` stale-detection surface. Rewritten at
        # every daemon start by `_capture_env_mtime_at_start`; the
        # pinned mtime is only meaningful for the CURRENT daemon
        # process, so an `ap2 rollback` that restored a prior value
        # would either resurrect a stale mtime baseline (false-positive
        # WARN against the live env file) or paper over the next real
        # bump. Runtime-only, like its `cron_state.json` /
        # `operator_queue_state.json` neighbors above.
        "daemon_state.json",
    ]),
    ("Per-run prompt + stream dumps for failure diagnosis (kept only on failure)", [
        "debug/",
    ]),
    ("Pipeline log dirs (TB-81) — local debug-only, never committed", [
        "pipelines/",
    ]),
    ("Local/sandbox-specific env (secrets, channel IDs) — keep out of git", [
        "env",
    ]),
    ("Runtime fcntl locks (cron_state.json.lock, retry_state.json.lock, etc.)", [
        "*.lock",
    ]),
    ("On-disk backups created during ap2 upgrades", [
        "*.bak",
    ]),
]

# Lines that go into the project's ROOT .gitignore (above .cc-autopilot/).
# Only entries for files ap2 creates outside .cc-autopilot/.
ROOT_GITIGNORE_BLOCKS: list[tuple[str, list[str]]] = [
    ("ap2 board lock (runtime)", [
        "TASKS.md.lock",
    ]),
]


@dataclass
class InitReport:
    project_root: Path
    nested_gitignore_added: list[str] = field(default_factory=list)
    root_gitignore_added: list[str] = field(default_factory=list)
    tasks_dir_created: bool = False
    tasks_md_created: bool = False
    progress_md_created: bool = False
    claude_md_created: bool = False
    claude_md_autopilot_added: bool = False
    goal_md_created: bool = False
    ideation_state_md_created: bool = False
    insights_dir_created: bool = False

    def print(self) -> None:
        if self.nested_gitignore_added:
            print(f"  .cc-autopilot/.gitignore: +{len(self.nested_gitignore_added)} entries")
            for line in self.nested_gitignore_added:
                print(f"    + {line}")
        else:
            print("  .cc-autopilot/.gitignore: up to date")
        if self.root_gitignore_added:
            print(f"  .gitignore: +{len(self.root_gitignore_added)} entries")
            for line in self.root_gitignore_added:
                print(f"    + {line}")
        else:
            print("  .gitignore: up to date")
        print(f"  .cc-autopilot/tasks/: {'created' if self.tasks_dir_created else 'exists'}")
        print(f"  TASKS.md: {'created' if self.tasks_md_created else 'exists'}")
        print(f"  .cc-autopilot/progress.md: {'created' if self.progress_md_created else 'exists'}")
        print(f"  goal.md: {'created (template — fill in)' if self.goal_md_created else 'exists'}")
        print(f"  .cc-autopilot/ideation_state.md: {'created (placeholder)' if self.ideation_state_md_created else 'exists'}")
        print(f"  .cc-autopilot/insights/: {'created (placeholder index)' if self.insights_dir_created else 'exists'}")
        if self.claude_md_created:
            print(f"  CLAUDE.md: created (with ## Autopilot)")
        elif self.claude_md_autopilot_added:
            print(f"  CLAUDE.md: appended ## Autopilot section")
        else:
            print(f"  CLAUDE.md: ## Autopilot section already present")


def _existing_entries(text: str) -> set[str]:
    """Pattern entries already in a gitignore (skip blank lines and comments)."""
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _union_gitignore(path: Path, blocks: list[tuple[str, list[str]]]) -> list[str]:
    """Append missing entries from `blocks` to `path`. Returns the lines added.

    If `path` doesn't exist, it's created and every block is written. If it
    exists, only entries not already present (by exact-string match) are
    appended, each grouped under its header. Headers are written only when the
    block contributes at least one new entry, so re-runs don't accumulate
    empty header sections.
    """
    text = path.read_text() if path.exists() else ""
    existing = _existing_entries(text)
    added: list[str] = []
    chunks: list[str] = []
    for header, entries in blocks:
        new_entries = [e for e in entries if e not in existing]
        if not new_entries:
            continue
        chunks.append(f"\n# {header}\n" + "\n".join(new_entries) + "\n")
        added.extend(new_entries)
        existing.update(new_entries)  # protect against intra-block dups

    if not added:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "".join(chunks))
    return added


# Word-boundary + any-trailing-content match (TB-102): tolerates
# `## Autopilot (per-project)` etc. while still rejecting look-alikes
# like `## AutopilotPlus`.
_AUTOPILOT_HEADER_RE = re.compile(r"^##\s+Autopilot\b[^\n]*$", re.M)


def _ensure_file(path: Path, content: str) -> bool:
    """Write `content` to `path` only if `path` does not already exist."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


def _ensure_claude_md_autopilot(claude_md: Path) -> tuple[bool, bool]:
    """Ensure CLAUDE.md exists and has a `## Autopilot` section.

    Returns `(claude_md_created, autopilot_appended)`:
    - `(True, False)` — CLAUDE.md was missing; created with header + Autopilot.
    - `(False, True)` — CLAUDE.md existed without Autopilot; we appended.
    - `(False, False)` — Autopilot section already present; nothing changed.
    """
    if not claude_md.exists():
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text("# CLAUDE.md\n\n" + CLAUDE_AUTOPILOT_TEMPLATE)
        return True, False
    text = claude_md.read_text()
    if _AUTOPILOT_HEADER_RE.search(text):
        return False, False
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    claude_md.write_text(text + sep + CLAUDE_AUTOPILOT_TEMPLATE)
    return False, True


def init_project(project_root: Path) -> InitReport:
    """Scaffold ap2 gitignores, tasks dir, board template, autopilot config.

    Idempotent — every step skips if the target already exists. Never clobbers
    user content; CLAUDE.md only gains a fresh `## Autopilot` block when the
    file lacks one entirely.
    """
    project_root = project_root.resolve()
    autopilot_dir = project_root / AUTOPILOT_DIR_NAME

    nested_added = _union_gitignore(autopilot_dir / ".gitignore", NESTED_GITIGNORE_BLOCKS)
    root_added = _union_gitignore(project_root / ".gitignore", ROOT_GITIGNORE_BLOCKS)

    tasks_dir = autopilot_dir / "tasks"
    tasks_dir_created = not tasks_dir.exists()
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # TB-89: insights directory + placeholder index. Lazy regen kicks in
    # the first time a cron tick fires after a real insight file lands.
    insights_dir = autopilot_dir / "insights"
    insights_dir_created = not insights_dir.exists()
    insights_dir.mkdir(parents=True, exist_ok=True)
    _ensure_file(insights_dir / "_index.md", INSIGHTS_INDEX_PLACEHOLDER)

    tasks_md_created = _ensure_file(project_root / "TASKS.md", TASKS_TEMPLATE)
    progress_md_created = _ensure_file(autopilot_dir / "progress.md", PROGRESS_TEMPLATE)
    goal_md_created = _ensure_file(project_root / "goal.md", GOAL_TEMPLATE)
    ideation_state_md_created = _ensure_file(
        autopilot_dir / "ideation_state.md", IDEATION_STATE_TEMPLATE,
    )
    claude_md_created, autopilot_added = _ensure_claude_md_autopilot(project_root / "CLAUDE.md")

    return InitReport(
        project_root=project_root,
        nested_gitignore_added=nested_added,
        root_gitignore_added=root_added,
        tasks_dir_created=tasks_dir_created,
        tasks_md_created=tasks_md_created,
        progress_md_created=progress_md_created,
        claude_md_created=claude_md_created,
        claude_md_autopilot_added=autopilot_added,
        goal_md_created=goal_md_created,
        ideation_state_md_created=ideation_state_md_created,
        insights_dir_created=insights_dir_created,
    )
