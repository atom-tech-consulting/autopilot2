"""Focus-list parser + runtime pointer state for `goal.md` (TB-226 axis 4).

Implements the multi-`## Current focus:` heading contract from goal.md
L115-138: the operator can list multiple `## Current focus:` headings in
priority order (top = active), each optionally carrying a
`Progress signals:` sub-block (an inline `Progress signals:` line whose
immediately-following bullets are advisory outcome guidance for the
ideation prompt, or a nested `### Progress signals` sub-heading whose
body bullets serve the same role). The sub-block is OPTIONAL — a focus
heading with no `Progress signals:` block parses cleanly as a focus
with `progress_signals_bullets=None`.

The daemon advances its in-memory pointer (`focus_pointer.json`) to the
next focus via the empty-cycles heuristic (N consecutive 0-proposal
ideation cycles against the active focus, configurable via
`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`, default 3); see `ap2/focus_advance.py`.
The `Progress signals:` bullets are advisory ideation-prompt context
only — they do NOT gate the pointer (TB-283 deleted the prior LLM-judge
advance path; TB-285 renamed the sub-block to reflect the new advisory
semantics).

When all foci exhaust, the daemon emits a `roadmap_complete` event +
decisions-needed bullet and halts auto-promotion until the operator
extends the roadmap and acks via `ap2 ack roadmap_complete`.

The daemon NEVER mutates goal.md itself (goal.md L187-191 "Goal.md
auto-rotation" Non-goal); the pointer is in-memory runtime state only.

Parser shape: line-based scan, not full Markdown AST. The schema is
shallow (heading + body + optional `Progress signals:` sub-block), so a
mistune dependency in the daemon hot path isn't paying rent. The
heading regex matches `## Current focus:` (with or without trailing
disambiguators after `:`); the body is everything until the next
`^## ` heading or EOF. Inside the body we look for an inline
`Progress signals:` line whose following bullets we collect (terminating
at the first blank line that isn't followed by another bullet, OR at
the next `### ` sub-heading, OR at the next `^## ` heading / EOF).
Fenced code blocks (``` ... ```) are ignored — bullets inside them
don't count as Progress-signals bullets. The legacy `Done when:` /
`### Done when` heading is NOT accepted (TB-285 hard cut — no
backcompat shim).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ap2._shared import locked_inplace, now

if TYPE_CHECKING:
    from ap2.config import Config


# Public-facing fields the pointer file carries. `schema=1` is the
# explicit version tag so future migrations can branch cleanly. The
# field ordering on disk is alphabetical for the JSON dump but the
# read path tolerates missing keys (defaults applied) so old pointers
# can flow forward across daemon upgrades.
POINTER_SCHEMA_VERSION = 1


# ===========================================================================
# Parser
# ===========================================================================


_HEADING_RE = re.compile(r"^##\s+Current focus:\s*(.*?)\s*$", re.M)
_NEXT_H2_RE = re.compile(r"^##\s+", re.M)
_PROGRESS_SIGNALS_INLINE_RE = re.compile(r"^Progress signals:\s*$", re.M)
_PROGRESS_SIGNALS_SUBHEAD_RE = re.compile(r"^###\s+Progress signals\b.*$", re.M)
_BULLET_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
_NEXT_H3_RE = re.compile(r"^###\s+", re.M)
_FENCE_RE = re.compile(r"^```")


@dataclass
class FocusItem:
    """One parsed `## Current focus:` heading + its body.

    `title` is the trimmed heading suffix (everything after `## Current
    focus:`). Empty when the heading was bare. `body` is the full body
    text between this heading and the next `## ` heading (or EOF),
    `\n`-terminated lines preserved. `progress_signals_bullets` is the
    list of Progress-signals bullet bodies, or `None` when no
    `Progress signals:` sub-block was found (the parser distinguishes
    "no block" from "empty block"; the sub-block is OPTIONAL — a focus
    heading with no `Progress signals:` block is valid and parses with
    `progress_signals_bullets=None`). `line_range` is the 1-indexed
    `(start, end)` line span of the heading + body in the source text
    (inclusive on both ends; useful for operator-facing diagnostics
    that quote the offending region).
    """

    title: str
    body: str
    progress_signals_bullets: list[str] | None
    line_range: tuple[int, int]

    def has_progress_signals(self) -> bool:
        """True iff a `Progress signals:` sub-block was structurally
        present.

        Note: an empty-but-present sub-block returns True (the operator
        wrote `Progress signals:` with no bullets — likely a draft /
        TODO). The sub-block is OPTIONAL — a focus heading with no
        `Progress signals:` block returns False here and parses with
        `progress_signals_bullets=None`. The empty-cycles advance
        heuristic (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`) runs the same way
        regardless of presence; the bullets are advisory ideation-
        prompt context only (TB-283 deleted the prior LLM-judge advance
        path).
        """
        return self.progress_signals_bullets is not None


def parse_focus_list(text: str) -> list[FocusItem]:
    """Parse all `## Current focus:` headings from `text` in source order.

    Returns an empty list when no focus headings exist (pre-pivot
    fixtures, brand-new goal.md skeleton, etc.). Malformed headings
    (e.g. `## Current focus` without the colon) are NOT picked up —
    the colon is load-bearing because goal.md's authoring contract
    states the heading is `## Current focus:` with the colon. The
    operator can intentionally use `## Current focus (archived):`
    as an archived-section pattern without confusing the parser, as
    long as the colon-after-`focus` is preserved.

    Progress-signals extraction: for each focus body, looks for the
    first structural Progress-signals marker — either an inline
    `Progress signals:` line or a nested `### Progress signals`
    sub-heading. Bullets are collected from the lines immediately
    following the marker up to the first section break (blank line not
    immediately followed by another bullet, next `### ` sub-heading, or
    `^## ` heading / EOF). Fenced code blocks inside the body are
    skipped (bullets inside `` ` `` fences don't count as
    Progress-signals bullets). The legacy `Done when:` heading is NOT
    accepted (TB-285 hard cut — no backcompat shim).
    """
    if not isinstance(text, str) or not text:
        return []

    # Map char-offsets to 1-indexed line numbers for `line_range`.
    # Cheap O(N) once-over.
    line_starts: list[int] = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def line_of(offset: int) -> int:
        # Binary search would be tidier but len(line_starts) is small
        # for any realistic goal.md (<1000 lines).
        for i in range(len(line_starts) - 1, -1, -1):
            if line_starts[i] <= offset:
                return i + 1
        return 1

    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return []

    items: list[FocusItem] = []
    for idx, m in enumerate(matches):
        title = m.group(1).strip()
        body_start = m.end() + 1 if m.end() < len(text) else m.end()
        # End at the next `## ` heading or EOF.
        next_h2 = _NEXT_H2_RE.search(text, m.end())
        body_end = next_h2.start() if next_h2 else len(text)
        body = text[body_start:body_end]
        progress_signals = _parse_progress_signals_from_body(body)
        line_range = (line_of(m.start()), line_of(max(body_end - 1, m.end())))
        items.append(FocusItem(
            title=title,
            body=body,
            progress_signals_bullets=progress_signals,
            line_range=line_range,
        ))
    return items


def _parse_progress_signals_from_body(body: str) -> list[str] | None:
    """Locate the Progress-signals sub-block in `body` (a focus's
    heading-stripped body text) and return the list of bullet bodies,
    or None when no Progress-signals marker is structurally present.

    Marker precedence: inline `Progress signals:` line FIRST, then
    nested `### Progress signals` sub-heading. Two markers are
    equivalent in effect but the parser only honors the first one
    encountered. Fenced code blocks (``` ... ```) are skipped so
    bullets inside them don't accidentally count as bullets.

    Optionality is explicit (TB-285 contract): a focus heading with no
    `Progress signals:` sub-block is valid and returns None here. The
    empty-cycles advance heuristic
    (`AP2_FOCUS_ADVANCE_EMPTY_CYCLES`) runs the same way for both
    block-present and block-absent foci; the bullets are advisory
    ideation-prompt context only (TB-283 deleted the prior LLM-judge
    advance path). The legacy `Done when:` heading is NOT accepted
    (hard cut — no backcompat shim per the project's
    git-history-is-the-rollback-substrate norm).

    Returns:
        - None when no Progress-signals marker was found (the
          common no-block case).
        - A possibly-empty list when the marker was present but no
          bullets followed (operator authored a draft Progress-signals
          block with the heading but no bullets yet — fine; the bullets
          are advisory and the daemon's advance pass doesn't read them).
    """
    if not body:
        return None

    # Strip fenced code blocks so bullets inside them don't confuse the
    # bullet-collection loop. Replace the fenced regions with newlines
    # of equivalent length (preserves line offsets) so any other
    # downstream regex stays well-behaved.
    cleaned = _strip_fenced_blocks(body)

    inline_m = _PROGRESS_SIGNALS_INLINE_RE.search(cleaned)
    subhead_m = _PROGRESS_SIGNALS_SUBHEAD_RE.search(cleaned)
    if inline_m is None and subhead_m is None:
        return None
    if inline_m is not None and (subhead_m is None or inline_m.start() < subhead_m.start()):
        bullet_start = inline_m.end() + 1 if inline_m.end() < len(cleaned) else inline_m.end()
    else:
        assert subhead_m is not None
        bullet_start = subhead_m.end() + 1 if subhead_m.end() < len(cleaned) else subhead_m.end()

    return _collect_bullets(cleaned, bullet_start)


def _strip_fenced_blocks(text: str) -> str:
    """Replace fenced ``` ... ``` regions with equivalent-length newlines.

    Preserves line offsets so subsequent regex matches against the
    cleaned string still report meaningful positions if a caller wants
    them. The newline-replacement (rather than space-replacement) keeps
    `^` / `$` line anchors well-behaved.
    """
    out_parts: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out_parts.append("\n" if line.endswith("\n") else "")
            continue
        if in_fence:
            out_parts.append("\n" if line.endswith("\n") else "")
            continue
        out_parts.append(line)
    return "".join(out_parts)


def _collect_bullets(text: str, start_offset: int) -> list[str]:
    """Collect markdown bullet bodies (each `- <body>`) starting from
    `start_offset` until the bullet block terminates.

    Termination rules:
      - First blank line that is NOT immediately followed by another
        bullet — paragraph break ends the block.
      - Next `### ` sub-heading.
      - Next `## ` heading (shouldn't happen since the caller already
        scoped to one focus body, but defensive).
      - End of `text`.

    Bullet bodies are returned with leading/trailing whitespace
    stripped. Indented continuation lines under a bullet are joined
    onto the bullet body with a single space (so a multi-line bullet
    becomes one logical criterion).
    """
    bullets: list[str] = []
    current: str | None = None
    blank_seen = False
    for line in text[start_offset:].splitlines():
        stripped = line.strip()
        if not stripped:
            if current is not None:
                bullets.append(current.strip())
                current = None
            blank_seen = True
            continue
        if _NEXT_H3_RE.match(line) or _NEXT_H2_RE.match(line):
            break
        bm = _BULLET_RE.match(line)
        if bm:
            if current is not None:
                bullets.append(current.strip())
            current = bm.group(1)
            blank_seen = False
            continue
        # Indented continuation of a bullet.
        if current is not None and (line.startswith("  ") or line.startswith("\t")):
            current = current + " " + stripped
            continue
        # Non-bullet non-empty line at the bullet-level indent (e.g.
        # trailing prose after the block). Terminate.
        if current is not None:
            bullets.append(current.strip())
            current = None
        if blank_seen:
            # Already saw a blank line + now have a non-bullet —
            # paragraph break confirmed.
            break
        # First non-bullet line without a prior blank: also a
        # block-terminator (the bullet block is over).
        break
    if current is not None:
        bullets.append(current.strip())
    return bullets


# ===========================================================================
# Pointer state file: .cc-autopilot/focus_pointer.json
# ===========================================================================


_DEFAULT_POINTER: dict[str, Any] = {
    "schema": POINTER_SCHEMA_VERSION,
    "active_index": 0,
    "active_title": "",
    "empty_cycles": 0,
    "exhausted_titles": [],
    "roadmap_complete_ack_idx": None,
    "roadmap_complete_emitted": False,
    "updated_ts": "",
}


def pointer_path(cfg) -> Path:  # cfg: ap2.config.Config (avoid import cycle)
    return cfg.project_root / ".cc-autopilot" / "focus_pointer.json"


def load_pointer(cfg) -> dict[str, Any]:
    """Read `focus_pointer.json` under `locked_inplace`. Missing or
    malformed → default-emit at index 0.

    Returns a dict matching `_DEFAULT_POINTER`'s shape with any
    missing keys filled in (forward compat: old pointers without
    `roadmap_complete_emitted` get the default False). Schema field
    is preserved so a future bump can detect the migration window.
    """
    path = pointer_path(cfg)
    if not path.exists():
        return dict(_DEFAULT_POINTER)
    try:
        with locked_inplace(path):
            raw = path.read_text()
    except OSError:
        return dict(_DEFAULT_POINTER)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, json.JSONDecodeError):
        return dict(_DEFAULT_POINTER)
    if not isinstance(data, dict):
        return dict(_DEFAULT_POINTER)
    merged = dict(_DEFAULT_POINTER)
    merged.update({k: v for k, v in data.items() if k in _DEFAULT_POINTER})
    # Defensive coercions — a hand-edited file shouldn't crash the daemon.
    try:
        merged["active_index"] = int(merged.get("active_index", 0))
    except (TypeError, ValueError):
        merged["active_index"] = 0
    try:
        merged["empty_cycles"] = int(merged.get("empty_cycles", 0))
    except (TypeError, ValueError):
        merged["empty_cycles"] = 0
    if not isinstance(merged.get("exhausted_titles"), list):
        merged["exhausted_titles"] = []
    merged["active_title"] = str(merged.get("active_title") or "")
    merged["roadmap_complete_emitted"] = bool(merged.get("roadmap_complete_emitted") or False)
    rci = merged.get("roadmap_complete_ack_idx")
    if rci is not None:
        try:
            merged["roadmap_complete_ack_idx"] = int(rci)
        except (TypeError, ValueError):
            merged["roadmap_complete_ack_idx"] = None
    return merged


def save_pointer(cfg, pointer: dict[str, Any]) -> None:
    """Atomically rewrite `focus_pointer.json` under `locked_inplace`.

    Stamps `updated_ts` at write time. The caller passes the full
    pointer dict; this helper preserves only the recognized keys so
    a caller-side typo doesn't accidentally extend the schema.
    """
    path = pointer_path(cfg)
    payload = {k: pointer.get(k, _DEFAULT_POINTER[k]) for k in _DEFAULT_POINTER}
    payload["updated_ts"] = now()
    path.parent.mkdir(parents=True, exist_ok=True)
    # In-place write under the fcntl lock (we don't replace the file,
    # so the lock fd stays bound to the same inode).
    with locked_inplace(path) as fd:
        import os
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())


# ===========================================================================
# Env knobs — parse + default + clamp. Mirrors the
# `_per_task_token_cap` / `_auto_approve_freeze_threshold` style.
# ===========================================================================

ADVANCE_EMPTY_CYCLES_DEFAULT = 3
ADVANCE_EMPTY_CYCLES_MIN = 1
ADVANCE_EMPTY_CYCLES_MAX = 20


def advance_empty_cycles_threshold(*, cfg: "Config | None" = None) -> int:
    """Effective threshold for the heuristic-fallback advance.

    `AP2_FOCUS_ADVANCE_EMPTY_CYCLES` (default 3). Clamped to
    [`ADVANCE_EMPTY_CYCLES_MIN`, `ADVANCE_EMPTY_CYCLES_MAX`] so an
    operator typo (e.g. `0` or `999999`) doesn't disable the advance
    path or wedge it permanently. Non-int / empty values fall back to
    the default.

    TB-336 axis-5: when ``cfg`` is passed, the read routes through
    ``cfg.get_component_value("focus_advance", "empty_cycles", default="")``
    which evaluates sectioned env
    (``f"AP2_COMPONENTS_{component.upper()}_{key.upper()}"`` shape built
    inside the helper) > flat env via reverse-``FLAT_TO_SECTIONED``
    lookup > ``cfg.components_config`` snapshot > default at call time.
    The cfg-less back-compat branch reads ``os.getenv`` so pre-cfg
    callers (``ap2.tests.test_tb226_focus_rotation``) keep today's
    behavior bit-for-bit; the cross-package grep gate stays green via
    the ``os.getenv`` shape the absence-check excludes by construction.
    """
    # Late-imported to avoid the `goal.py` ↔ `config.py` boundary cycle.
    from ap2.config import Config
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "advance_empty_cycles_threshold(cfg=...) expects a Config "
            f"instance; got {type(cfg).__name__}",
        )
    if cfg is not None:
        raw = str(
            cfg.get_component_value(
                "focus_advance", "empty_cycles", default="",
            )
            or "",
        ).strip()
    else:
        raw = os.getenv("AP2_FOCUS_ADVANCE_EMPTY_CYCLES", "").strip()
    if not raw:
        return ADVANCE_EMPTY_CYCLES_DEFAULT
    try:
        v = int(raw)
    except ValueError:
        return ADVANCE_EMPTY_CYCLES_DEFAULT
    if v < ADVANCE_EMPTY_CYCLES_MIN:
        return ADVANCE_EMPTY_CYCLES_MIN
    if v > ADVANCE_EMPTY_CYCLES_MAX:
        return ADVANCE_EMPTY_CYCLES_MAX
    return v


def auto_advance_disabled(*, cfg: "Config | None" = None) -> bool:
    """True iff `AP2_FOCUS_AUTO_ADVANCE_DISABLED` is set to a truthy
    value (`1` / `true` / `yes` — same convention as
    `AP2_IDEATION_DISABLED`). Default unset → False (auto-advance
    enabled).

    The kill-switch: when True, the daemon never auto-advances even
    if the empty-cycles heuristic threshold tripped. A
    `focus_advance_blocked` decisions-needed bullet surfaces so the
    operator can advance manually via `ap2 update-goal` or by
    flipping the knob.

    TB-336 axis-5: when ``cfg`` is passed, the read routes through
    ``cfg.get_component_value("focus_advance", "auto_advance_disabled",
    default="")``. The cfg-less back-compat branch reads ``os.getenv``
    so pre-cfg callers (``ap2.tests.test_tb226_focus_rotation``) keep
    today's behavior bit-for-bit; the cross-package grep gate stays
    green via the ``os.getenv`` shape the absence-check excludes by
    construction.
    """
    # Late-imported to avoid the `goal.py` ↔ `config.py` boundary cycle.
    from ap2.config import Config
    if cfg is not None and not isinstance(cfg, Config):
        raise TypeError(
            "auto_advance_disabled(cfg=...) expects a Config instance; "
            f"got {type(cfg).__name__}",
        )
    if cfg is not None:
        resolved = cfg.get_component_value(
            "focus_advance", "auto_advance_disabled", default="",
        )
        # The TOML overlay branch may surface a typed `True` / `False`
        # (when an operator opted into `[components.focus_advance]
        # auto_advance_disabled = true`); honor it directly so the
        # strict-bool path mirrors `_focus_auto_advance_disabled(cfg)`.
        if isinstance(resolved, bool):
            return resolved
        raw = "" if resolved is None else str(resolved)
    else:
        raw = os.getenv("AP2_FOCUS_AUTO_ADVANCE_DISABLED", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ===========================================================================
# High-level helpers used by daemon._maybe_advance_focus
# ===========================================================================


def goal_md_path(cfg) -> Path:
    """Resolve goal.md relative to the project root. Centralized here
    so the daemon and the operator-queue `update_goal` handler share
    one source of truth."""
    return cfg.project_root / "goal.md"


def read_focus_list(cfg) -> list[FocusItem]:
    """Read goal.md and parse its focus list. Returns [] when goal.md
    is missing (fresh project pre-`ap2 init` scaffold) or empty.

    No locking: goal.md mutations go through the operator-queue
    `update_goal` op (TB-193), which lands at tick boundary under the
    board lock — the read is concurrent-safe in steady state.
    """
    path = goal_md_path(cfg)
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    return parse_focus_list(text)


def active_focus(cfg, foci: list[FocusItem] | None = None) -> FocusItem | None:
    """Return the FocusItem at the pointer's `active_index`, or None
    when the pointer is out of bounds (all foci exhausted, fresh
    pointer against a goal.md with no focus headings, etc.).
    """
    if foci is None:
        foci = read_focus_list(cfg)
    if not foci:
        return None
    pointer = load_pointer(cfg)
    idx = pointer["active_index"]
    if idx < 0 or idx >= len(foci):
        return None
    return foci[idx]


ROADMAP_COMPLETE_ACK_TOKEN = "roadmap_complete"


def roadmap_exhausted(cfg, foci: list[FocusItem] | None = None) -> bool:
    """True iff the pointer has advanced past the last focus AND the
    operator has NOT acked the `roadmap_complete` halt since the most
    recent `roadmap_complete` event was emitted.

    Mirrors `_auto_approve_paused`'s events.jsonl-scan ack-reset shape:
    looks for the most recent `roadmap_complete` event in the tail; if
    a subsequent `operator_ack` event carries the
    `roadmap_complete` token in its `note` field, the halt is cleared.
    The ack-token check is substring-based on `note` (same shape
    `_auto_approve_paused` uses for `auto_approve_unfreeze`) so the
    operator can type `ap2 ack roadmap_complete --reason "extended the
    roadmap with axis 5"` and the daemon recognizes the token amid
    free-text rationale.

    Side-effect: when the events-driven ack is detected, the pointer's
    forensic `roadmap_complete_ack_idx` field is bumped to the current
    foci count so a separate read of the pointer (e.g. `ap2 status` /
    web UI) renders the cleared state without needing the events-scan.
    """
    from ap2 import events as _events  # local import to avoid cycle
    if foci is None:
        foci = read_focus_list(cfg)
    pointer = load_pointer(cfg)
    total = len(foci)
    if total == 0:
        # Pre-pivot goal.md with no focus headings: not exhausted —
        # there's nothing to exhaust. The daemon's other gates handle
        # this case (ideation prompt synthesizes from Mission/Done-when).
        return False
    if pointer["active_index"] < total:
        return False
    # Active index is past the last focus. Was the halt acked? Two
    # signal sources (defense-in-depth): the events.jsonl scan AND
    # the pointer's forensic ack_idx field. Either clears the halt.
    if not cfg.events_file.exists():
        # No events file → can't have acked yet.
        return True
    tail = _events.tail(cfg.events_file, 1000)
    last_roadmap_idx = -1
    last_ack_idx = -1
    for i, e in enumerate(tail):
        typ = e.get("type")
        if typ == "roadmap_complete":
            last_roadmap_idx = i
        elif typ == "operator_ack":
            note = str(e.get("note") or "")
            if ROADMAP_COMPLETE_ACK_TOKEN in note:
                last_ack_idx = i
    if last_roadmap_idx == -1:
        # Daemon hasn't emitted `roadmap_complete` yet but the pointer
        # IS out-of-bounds. This is the first-tick window before
        # `_maybe_advance_focus` runs. Treat as exhausted (the halt
        # fires; the next tick's advance pass emits the event).
        pass
    elif last_ack_idx > last_roadmap_idx:
        # Ack landed AFTER the most recent halt-emit → cleared.
        # Bump the pointer's forensic field so status surfaces stay
        # consistent. Best-effort: a write error doesn't change the
        # cleared verdict.
        if pointer.get("roadmap_complete_ack_idx") != total:
            pointer["roadmap_complete_ack_idx"] = total
            try:
                save_pointer(cfg, pointer)
            except OSError:
                pass
        return False
    # Fallback to the pointer's forensic ack_idx: an operator who
    # hand-edited the pointer (or a future migration that pre-sets it)
    # can also clear the halt without an events-scan hit.
    ack_idx = pointer.get("roadmap_complete_ack_idx")
    if ack_idx is not None and ack_idx >= total:
        return False
    return True


def reset_pointer_on_roadmap_extension(cfg, foci: list[FocusItem]) -> dict[str, Any]:
    """Side-effect-free: returns the pointer dict that SHOULD apply
    after a roadmap extension drained from the operator queue.

    Called from the operator-queue `update_goal` drain handler when
    a goal.md write landed AND the new file's focus-list length is
    longer than the pointer's prior exhaustion count. Snaps the
    active_index to the FIRST newly-added focus so work resumes on
    fresh ground rather than re-walking already-exhausted entries.
    """
    pointer = load_pointer(cfg)
    prior_count = max(
        pointer.get("active_index", 0),
        len(pointer.get("exhausted_titles", [])),
    )
    new_count = len(foci)
    if new_count <= prior_count:
        # No extension — leave the pointer alone.
        return pointer
    # First newly-added focus is at index `prior_count`. Reset
    # bookkeeping so the heuristic starts fresh.
    pointer["active_index"] = prior_count
    pointer["active_title"] = foci[prior_count].title if prior_count < new_count else ""
    pointer["empty_cycles"] = 0
    pointer["roadmap_complete_emitted"] = False
    return pointer
