"""TASKS.md parser: read sections, move/add/remove tasks, assign TB-N IDs.

Uses fcntl.flock for file locking so multiple agents can mutate the board safely.
The board has 6 sections in a fixed order (see skills/taskboard/SKILL.md):
Active, Ready, Backlog, Pipeline Pending, Complete, Frozen.

`Pipeline Pending` (TB-114-era refactor) is the holding area for a launch
task that has dispatched one or more pipelines via `pipeline_task_start`.
The launch agent's own SDK turn finished, but the work isn't truly done
until each spawned subprocess dies and the original briefing's
`## Verification` passes against the post-pipeline working tree. The
daemon sweeps Pipeline Pending each tick: when all of a task's
pipeline pids are dead, it runs verification and routes to Complete
(pass) or Backlog/Frozen (fail).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

SECTIONS = ["Active", "Ready", "Backlog", "Pipeline Pending", "Complete", "Frozen"]
SECTION_RE = re.compile(
    r"^## (Active|Ready|Backlog|Pipeline Pending|Complete|Frozen)\s*$", re.M,
)
# TB-132: backtick spans on a task line are a uniform metadata surface —
# any span starting with `#` is a tag, any span starting with `@<key>:` is
# a structured field. Single capture group `spans` collects them all; the
# `parse_task_line` helper below splits the two shapes apart. This stops
# the parser from regexing free-text descriptions for clause syntax — the
# old `(blocked on: ...)` regex collided with prose (TB-121's description
# literally contained the phrase as descriptive text and auto-blocked the
# task on the non-existent token `review`).
TASK_LINE_RE = re.compile(
    r"^- \[(?P<check>[ x])\] \*\*(?P<id>TB-\d+)\*\* \*\*(?P<title>[^*]+)\*\*"
    r"(?P<spans>(?:\s+`[^`]+`)*)"
    r"(?:\s+—\s*(?P<desc>.*?))?"
    r"(?:\s*\[→ brief\]\((?P<briefing>[^)]+)\))?\s*$"
)
# Codespan splitters: `#tag` vs `@key:value`. The key shape mirrors a
# Python identifier (so `@blocked` / `@owner` / `@due_date` all work) and
# the value is everything up to the closing backtick, comma-delimited
# inside for multi-token fields like `@blocked:TB-5,TB-7`.
_TAG_SPAN_RE = re.compile(r"`(#[^`]+)`")
_META_SPAN_RE = re.compile(r"`@([A-Za-z][A-Za-z0-9_]*):([^`]*)`")

@dataclass
class Task:
    id: str  # e.g. "TB-42"
    title: str
    section: str
    tags: list[str] = field(default_factory=list)
    # TB-132: structured metadata captured from `@<key>:<value>` codespans
    # on the task line. Sits alongside `tags` rather than mining the
    # description prose. Currently consumed: `meta['blocked']` (comma-
    # separated TB-N or scheme:value tokens). Format extends naturally to
    # `@priority`, `@owner`, `@due_date`, etc. without expanding the regex.
    meta: dict[str, str] = field(default_factory=dict)
    description: str = ""
    briefing: str | None = None
    checked: bool = False
    raw: str = ""  # original line for lossless preservation

    @property
    def num(self) -> int:
        return int(self.id.split("-")[1])

    @property
    def blocked_on(self) -> list[str]:
        """Tokens declared as blockers via the `@blocked:<csv>` codespan.

        Codespan format: `` `@blocked:TB-5,TB-7` `` — comma-separated.
        Each token is either a `TB-N` task id or a `<scheme>:<value>`
        external blocker (currently only `pid:<N>@<TS>` is consumed by
        the daemon — see TB-81). Empty list when no blockers are
        declared, so the dependency check is a no-op for tasks that
        don't explicitly declare any.

        Closes TB-132's transition: the legacy `(blocked on: ...)`
        description-regex fallback was kept around for migration; it's
        gone now. Prose like TB-121's "(blocked on: review)" no longer
        registers as a structural blocker.
        """
        raw = self.meta.get("blocked", "")
        return [tok.strip() for tok in raw.split(",") if tok.strip()]

    def render(self) -> str:
        check = "x" if self.checked else " "
        tag_str = "".join(f" `{t}`" for t in self.tags)
        # TB-132: meta codespans render after tags, before the em-dash —
        # mirrors how a reader scans the line (id → title → tags → meta
        # → prose). dict iteration is insertion-ordered (3.7+) so a
        # round-trip parse → render preserves the codespan order the
        # author wrote.
        meta_str = "".join(f" `@{k}:{v}`" for k, v in self.meta.items())
        desc = f" — {self.description}" if self.description else ""
        brief = f" [→ brief]({self.briefing})" if self.briefing else ""
        return (
            f"- [{check}] **{self.id}** **{self.title}**"
            f"{tag_str}{meta_str}{desc}{brief}"
        )


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
    # Lines in a section that don't parse as a canonical task line. Two
    # distinct shapes hit this:
    #   1. Lines that LOOK like task lines (`- [`-prefixed) but don't match
    #      TASK_LINE_RE — typically a manual edit added junk between
    #      **TB-N** and **Title**, e.g. `**TB-59** (7735de2) **Title**`.
    #      Risk: the malformed task disappears from `iter_tasks` so a
    #      depending task's blocker check silently treats it as
    #      uncompleted.
    #   2. Non-task lines that wedged into a section — e.g. unfinalized
    #      `/tb prep` text whose author never wrapped it in a `- [ ]`
    #      bullet. These don't appear in `iter_tasks` (correct) but
    #      previously inflated `len(sections[s])` and got reported as
    #      "Backlog tasks" by `ap2 status` (TB-92 — diagnosed in stoch
    #      where 3 lines of orphan README prose showed up as `3B`).
    # The daemon's TB-68 hook emits dedup'd `board_malformed_line`
    # events for both shapes, surfacing them on the operator-visible
    # event log instead of silently passing.
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
                # Flag ANY non-task-shaped line — not just `- [`-prefixed
                # ones — so orphan prose (TB-92) gets surfaced rather than
                # silently inflating section counts.
                if not TASK_LINE_RE.match(line):
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
        meta: dict[str, str] | None = None,
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
            meta=dict(meta or {}),
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

        - `TB-N` blockers are satisfied iff the id is in Complete.
        - The `review` scheme (TB-121) is the human-review gate ideation
          stamps onto every proposed task. It is NEVER satisfied while
          present — the operator removes it via `ap2 approve TB-N`
          (which routes through `_approve_review_token`), at which
          point the task has no more `review` token in its `@blocked`
          codespan and `Task.blocked_on` simply doesn't return it. So
          this branch only fires while the gate is still active, and
          always returns False. `diagnose._board_health` distinguishes
          this case from `unsatisfiable_blocks` so the watchdog doesn't
          conflate "operator AFK" with "daemon broken."
        - Unknown schemes fail-safe to "not satisfied" — silently dispatching
          on a typo would be worse than stranding the task until an operator
          fixes it. Includes the retired `pid:<N>@<TS>` scheme (TB-81 →
          retired in TB-117 once stoch's last pre-TB-115 validation tasks
          drained from the live board) — any straggler stays Backlog until
          the operator removes the clause manually.
        """
        if blocker.startswith("TB-"):
            return blocker in completed
        if blocker.lower() == "review":
            return False
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
    spans = m.group("spans") or ""
    # Single backtick-span list captures both shapes; split here so the
    # TASK_LINE_RE itself stays a single regex rule (TB-132). Spans not
    # matching either shape (e.g. a stray `` `code` `` inside the
    # tags-and-meta block) are ignored by both extractors and won't
    # silently end up in tags or meta.
    tags = _TAG_SPAN_RE.findall(spans)
    meta: dict[str, str] = {}
    for kv in _META_SPAN_RE.finditer(spans):
        meta[kv.group(1)] = kv.group(2)
    return Task(
        id=m.group("id"),
        title=m.group("title").strip(),
        section=section,
        tags=tags,
        meta=meta,
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


@contextlib.contextmanager
def board_file_lock(tasks_file: Path) -> Iterator[None]:
    """Hold the board's fcntl lock without loading or saving any Board.

    Used by callers (TB-110 violation path, TB-111 rollback) that mutate
    TASKS.md *out-of-band* via `git reset --hard` and don't want
    `locked_board`'s save-on-exit to clobber the post-reset on-disk content.
    """
    with _locked(lock_path(tasks_file)):
        yield
