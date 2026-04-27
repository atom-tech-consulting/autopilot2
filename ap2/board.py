"""TASKS.md parser: read sections, move/add/remove tasks, assign TB-N IDs.

Uses fcntl.flock for file locking so multiple agents can mutate the board safely.
The board has exactly 5 sections in a fixed order (see skills/taskboard/SKILL.md):
Active, Ready, Backlog, Complete, Frozen.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

SECTIONS = ["Active", "Ready", "Backlog", "Complete", "Frozen"]
SECTION_RE = re.compile(r"^## (Active|Ready|Backlog|Complete|Frozen)\s*$", re.M)
TASK_LINE_RE = re.compile(
    r"^- \[(?P<check>[ x])\] \*\*(?P<id>TB-\d+)\*\* \*\*(?P<title>[^*]+)\*\*"
    r"(?P<tags>(?:\s+`#[^`]+`)*)"
    r"(?:\s+—\s*(?P<desc>.*?))?"
    r"(?:\s*\[→ brief\]\((?P<briefing>[^)]+)\))?\s*$"
)

# Matches a `(blocked on: ...)` clause anywhere in a task's description.
# Tokens inside are comma-separated; each is either a TB-N task id or a
# `<scheme>:<value>` external blocker (TB-81 — currently `pid:<N>@<TS>`).
_BLOCKED_CLAUSE_RE = re.compile(r"\(blocked on:\s*([^)]+)\)", re.IGNORECASE)
# Token splitter: comma OR `and` (case-insensitive) so natural-language phrases
# like "TB-5 and TB-7" still parse. Whitespace-only fragments are dropped.
_BLOCKED_TOKEN_SPLIT_RE = re.compile(r",|\band\b", re.IGNORECASE)


@dataclass
class Task:
    id: str  # e.g. "TB-42"
    title: str
    section: str
    tags: list[str] = field(default_factory=list)
    description: str = ""
    briefing: str | None = None
    checked: bool = False
    raw: str = ""  # original line for lossless preservation

    @property
    def num(self) -> int:
        return int(self.id.split("-")[1])

    @property
    def blocked_on(self) -> list[str]:
        """Tokens listed in a `(blocked on: ...)` clause in the description.

        Each token is either a `TB-N` task id or a `<scheme>:<value>` blocker
        (currently only `pid:<N>@<TS>` is consumed by the daemon — see TB-81).
        Empty list for tasks without the clause, so the dependency check is a
        no-op for existing tasks that don't explicitly declare blockers.
        """
        m = _BLOCKED_CLAUSE_RE.search(self.description)
        if not m:
            return []
        return [
            tok.strip()
            for tok in _BLOCKED_TOKEN_SPLIT_RE.split(m.group(1))
            if tok.strip()
        ]

    def render(self) -> str:
        check = "x" if self.checked else " "
        tag_str = "".join(f" `{t}`" for t in self.tags)
        desc = f" — {self.description}" if self.description else ""
        brief = f" [→ brief]({self.briefing})" if self.briefing else ""
        return f"- [{check}] **{self.id}** **{self.title}**{tag_str}{desc}{brief}"


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[int]:
    """Acquire an exclusive fcntl lock on `path`. Creates the file if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass
class Board:
    path: Path
    sections: dict[str, list[str]] = field(default_factory=dict)
    header: str = "# Tasks\n"
    # Lines that look like task lines (start with `- [`) but don't match
    # TASK_LINE_RE — typically a manual edit added junk between **TB-N** and
    # **Title**, e.g. `**TB-59** (7735de2) **Title**`. The daemon surfaces
    # these so a malformed line doesn't silently strand a Backlog task whose
    # blocker now appears uncompleted to the parser.
    malformed_lines: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Board":
        b = cls(path=path)
        if not path.exists():
            for s in SECTIONS:
                b.sections[s] = []
            return b
        text = path.read_text()
        b._parse(text)
        return b

    def _parse(self, text: str) -> None:
        matches = list(SECTION_RE.finditer(text))
        if matches:
            self.header = text[: matches[0].start()].rstrip() + "\n\n"
        for s in SECTIONS:
            self.sections[s] = []
        for i, m in enumerate(matches):
            section = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip("\n")
            for line in body.splitlines():
                if line.strip() == "" or line.strip().startswith("<!--"):
                    continue
                self.sections[section].append(line)
                if line.lstrip().startswith("- [") and not TASK_LINE_RE.match(line):
                    self.malformed_lines.append((section, line))

    def render(self) -> str:
        parts = [self.header.rstrip() + "\n\n"]
        for s in SECTIONS:
            parts.append(f"## {s}\n\n")
            for line in self.sections.get(s, []):
                parts.append(line + "\n")
            parts.append("\n")
        return "".join(parts).rstrip() + "\n"

    def save(self) -> None:
        self.path.write_text(self.render())

    # ------- mutations -------

    def find(self, task_id: str) -> tuple[str, int] | None:
        for section, lines in self.sections.items():
            for idx, line in enumerate(lines):
                m = TASK_LINE_RE.match(line)
                if m and m.group("id") == task_id:
                    return section, idx
        return None

    def get(self, task_id: str) -> Task | None:
        loc = self.find(task_id)
        if not loc:
            return None
        section, idx = loc
        return parse_task_line(self.sections[section][idx], section)

    def move(self, task_id: str, to_section: str, *, check: bool | None = None) -> Task:
        if to_section not in SECTIONS:
            raise ValueError(f"invalid section {to_section!r}")
        loc = self.find(task_id)
        if not loc:
            raise KeyError(f"{task_id} not on board")
        src, idx = loc
        line = self.sections[src].pop(idx)
        if check is not None:
            line = _set_checkbox(line, check)
        if to_section == "Complete":
            line = _set_checkbox(line, True)
        # Insert at top for Ready/Active (priority queue), bottom otherwise.
        if to_section in ("Active", "Ready"):
            self.sections[to_section].insert(0, line)
        else:
            self.sections[to_section].append(line)
        t = parse_task_line(line, to_section)
        assert t is not None
        return t

    def add(
        self,
        section: str,
        *,
        task_id: str,
        title: str,
        tags: list[str] | None = None,
        description: str = "",
        briefing: str | None = None,
    ) -> Task:
        if section not in SECTIONS:
            raise ValueError(f"invalid section {section!r}")
        t = Task(
            id=task_id,
            title=title,
            section=section,
            tags=[_norm_tag(x) for x in (tags or [])],
            description=description,
            briefing=briefing,
        )
        line = t.render()
        if section in ("Active", "Ready"):
            self.sections[section].insert(0, line)
        else:
            self.sections[section].append(line)
        return t

    def remove(self, task_id: str) -> Task | None:
        loc = self.find(task_id)
        if not loc:
            return None
        section, idx = loc
        line = self.sections[section].pop(idx)
        return parse_task_line(line, section)

    def iter_tasks(self, section: str | None = None) -> Iterator[Task]:
        sections = [section] if section else SECTIONS
        for s in sections:
            for line in self.sections.get(s, []):
                t = parse_task_line(line, s)
                if t:
                    yield t

    def completed_ids(self) -> set[str]:
        """Set of task IDs currently in the Complete section."""
        return {t.id for t in self.iter_tasks("Complete")}

    def _is_blocker_satisfied(self, blocker: str, completed: set[str]) -> bool:
        """Per-scheme dispatch.

        - `TB-N` blockers are satisfied iff the id is in Complete (legacy).
        - `pid:<N>[@<TS>]` blockers consult `pipelines.is_blocking` (TB-81).
        - Unknown schemes fail-safe to "not satisfied" — silently dispatching
          on a typo would be worse than stranding the task until an operator
          fixes it.
        """
        if blocker.startswith("TB-"):
            return blocker in completed
        if blocker.startswith("pid:"):
            from . import pipelines

            return not pipelines.is_blocking(blocker)
        return False

    def _is_dispatchable(self, t: Task, completed: set[str]) -> bool:
        """True iff every blocker declared on `t` is satisfied."""
        return all(self._is_blocker_satisfied(b, completed) for b in t.blocked_on)

    def next_ready(self) -> Task | None:
        """Top of Ready whose blockers are all satisfied (all in Complete).

        Tasks with no declared blockers (the common case, and all pre-existing
        tasks) are always dispatchable — so this is backward-compatible: any
        board authored before dependency enforcement behaves exactly as before.
        """
        return self.next_dispatchable("Ready")

    def next_dispatchable(self, section: str) -> Task | None:
        """First task in `section` whose blockers are all in Complete."""
        completed = self.completed_ids()
        for t in self.iter_tasks(section):
            if self._is_dispatchable(t, completed):
                return t
        return None

    def max_id(self) -> int:
        best = 0
        for t in self.iter_tasks():
            best = max(best, t.num)
        return best


def parse_task_line(line: str, section: str) -> Task | None:
    m = TASK_LINE_RE.match(line)
    if not m:
        return None
    tags = re.findall(r"`(#[^`]+)`", m.group("tags") or "")
    return Task(
        id=m.group("id"),
        title=m.group("title").strip(),
        section=section,
        tags=tags,
        description=(m.group("desc") or "").strip(),
        briefing=m.group("briefing"),
        checked=m.group("check") == "x",
        raw=line,
    )


def _norm_tag(t: str) -> str:
    t = t.strip().strip("`")
    if not t.startswith("#"):
        t = "#" + t
    return t


def _set_checkbox(line: str, checked: bool) -> str:
    char = "x" if checked else " "
    return re.sub(r"^- \[[ x]\]", f"- [{char}]", line, count=1)


def lock_path(tasks_file: Path) -> Path:
    return tasks_file.with_suffix(tasks_file.suffix + ".lock")


@contextlib.contextmanager
def locked_board(tasks_file: Path) -> Iterator[Board]:
    """Exclusive-lock the board file, load it, yield it, save on exit.

    Callers mutate the yielded Board; on normal exit we write it back. On
    exceptions we skip the write so the on-disk state is unchanged.
    """
    with _locked(lock_path(tasks_file)):
        board = Board.load(tasks_file)
        yield board
        board.save()
