"""Project-init scaffolding: gitignores, dirs, marker files for ap2.

The single source of truth for what an ap2-managed project should ignore vs.
track. Replaces the manual transcribe-from-skill-markdown flow that left
stoch's `cron.yaml` untracked for weeks and silently accumulated `.lock` /
`.bak` files in the working tree.

Idempotent: re-running unions with whatever already exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import AUTOPILOT_DIR_NAME


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
        "cron_state.json",
        "mm_state.json",
        "retry_state.json",
    ]),
    ("Per-run prompt + stream dumps for failure diagnosis (kept only on failure)", [
        "debug/",
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
        if self.tasks_dir_created:
            print(f"  .cc-autopilot/tasks/: created")
        else:
            print("  .cc-autopilot/tasks/: exists")


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


def init_project(project_root: Path) -> InitReport:
    """Scaffold ap2 ignore lists + tasks dir for `project_root`. Idempotent."""
    project_root = project_root.resolve()
    autopilot_dir = project_root / AUTOPILOT_DIR_NAME

    nested_added = _union_gitignore(autopilot_dir / ".gitignore", NESTED_GITIGNORE_BLOCKS)
    root_added = _union_gitignore(project_root / ".gitignore", ROOT_GITIGNORE_BLOCKS)

    tasks_dir = autopilot_dir / "tasks"
    created = not tasks_dir.exists()
    tasks_dir.mkdir(parents=True, exist_ok=True)

    return InitReport(
        project_root=project_root,
        nested_gitignore_added=nested_added,
        root_gitignore_added=root_added,
        tasks_dir_created=created,
    )
