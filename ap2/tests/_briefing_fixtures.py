"""TB-204: canonical-valid briefing builders for the test suite.

Centralizes the structurally-complete `## Goal` + `Why now:` line +
`## Scope` + `## Design` + `## Verification` + `## Out of scope`
scaffold that `_validate_briefing_structure` (TB-154 / TB-161 /
TB-164 / TB-171) accepts. Pre-TB-204 the same scaffold was inlined
at >30 sites across `ap2/tests/`; every validator-rule extension
(e.g. TB-164's `Why now:` requirement) had to update 17+ files. The
fixture is a single edit point.

## Public API

- `canonical_briefing(task_id, *, title=..., goal_anchor=..., ...)` —
  returns a briefing string that passes the full structural gate (all
  five canonical sections + a Why-now rationale + an auto-verifiable
  Verification bullet). Defaults satisfy the gate against the project
  goal.md's `## Current focus: code quality` heading title (the
  cheapest valid anchor today).

- `minimal_briefing(task_id, **kwargs)` — same shape, shortest
  acceptable bodies (Why-now exactly `WHY_NOW_MIN_CHARS + 1` chars,
  Verification with a single backticked shell bullet). For tests that
  pin the floor of each gate.

- `briefing_missing(task_id, *, drop)` — returns a briefing with the
  named section removed (or its body zeroed). `drop` accepts
  `"Goal"`, `"Why now"`, `"Scope"`, `"Design"`, `"Verification"`,
  `"Out of scope"`, or `"goal-anchor"` (TB-161 reject case — Goal body
  cites no goal.md anchor and has no Done-when reference).

- `briefing_with_manual_bullet(task_id)` — canonical shape but the
  Verification section contains a `Manual:` bullet (TB-171 reject
  case).

## Design notes

Pure-function builders, not pytest fixtures (no `@pytest.fixture`
decorator). Call sites read as plain function calls without
depending on test-discovery magic, and the helper stays usable from
ad-hoc scripts (e.g. `adhoc/` regression sweeps) without importing
pytest.

Each builder returns `str`, not `Path` — call sites that need a
temp-dir-written briefing do their own
`(tmp_path / "brief.md").write_text(canonical_briefing("TB-N"))`.
Keeps the helper composable and free of fixture-scope decisions.

`goal_anchor` default ("current focus: code quality") matches today's
`goal.md`'s `## Current focus` heading title verbatim — the cheapest
valid anchor (per TB-161 `_goal_md_anchors`). For tests that
exercise the goal-anchor reject path explicitly, use
`briefing_missing(task_id, drop="goal-anchor")` which returns a
briefing whose Goal body cites neither a Current-focus title nor a
Done-when bullet.

The module is underscore-prefixed so pytest doesn't try to collect
it as a test file. It is test-internal — not re-exported from
`ap2/__init__.py` or `ap2/tests/__init__.py`.
"""
from __future__ import annotations

from ap2.init import WHY_NOW_MIN_CHARS

# Default text bodies — chosen to satisfy every gate and to read as
# canonical-shaped prose. The Why-now default cites the project's
# `## Current focus: code quality` heading so the briefing passes
# both TB-161 (goal-anchor) and TB-164 (Why-now ≥ min chars).
_DEFAULT_TITLE = "Test task"
_DEFAULT_GOAL_ANCHOR = "current focus: code quality"
_DEFAULT_WHY_NOW = (
    "closes the failure mode named in the briefing scope — without "
    "this, the gap stays open and operators only catch it after the "
    "fact (TB-204)."
)
_DEFAULT_SCOPE = "- foo.py\n"
_DEFAULT_DESIGN = "Straightforward edit.\n"
_DEFAULT_VERIFICATION = "- `uv run pytest -q` — gates pass\n"
_DEFAULT_OUT_OF_SCOPE = "- nothing\n"

# Shortest acceptable Why-now rationale (TB-164: post-marker char
# count must be >= WHY_NOW_MIN_CHARS). Pad by one char so the value
# is strictly above the floor — keeps the fixture stable under future
# off-by-one tweaks to the boundary check.
_MIN_WHY_NOW = "x" * (WHY_NOW_MIN_CHARS + 1)


def _format_goal_body(*, goal_anchor: str, why_now: str) -> str:
    """Assemble a `## Goal` body that satisfies TB-161 (anchor) and
    TB-164 (Why-now). The anchor lands as a sentence in the prose so
    the substring match in `_goal_body` after lowercase + punctuation-
    strip succeeds; the Why-now marker sits on its own line per
    `_WHY_NOW_MARKER_RE` (line-anchored).
    """
    return (
        f"Closes the failure mode the briefing scope names; "
        f"advances goal.md's {goal_anchor}.\n\n"
        f"Why now: {why_now}\n"
    )


def canonical_briefing(
    task_id: str,
    *,
    title: str = _DEFAULT_TITLE,
    goal_anchor: str = _DEFAULT_GOAL_ANCHOR,
    why_now: str = _DEFAULT_WHY_NOW,
    scope: str = _DEFAULT_SCOPE,
    design: str = _DEFAULT_DESIGN,
    verification: str = _DEFAULT_VERIFICATION,
    out_of_scope: str = _DEFAULT_OUT_OF_SCOPE,
) -> str:
    """Return a structurally-canonical briefing for `task_id`.

    Passes `_validate_briefing_structure` against goal.md's
    `## Current focus: code quality` heading (the default
    `goal_anchor`). Override any of the kwargs to customize the
    body — each kwarg substitutes into its respective section
    verbatim, so callers can pin specific verification bullets,
    scope files, etc. without rebuilding the scaffold.

    Pure function: no side effects, deterministic for the same
    inputs.
    """
    goal_body = _format_goal_body(goal_anchor=goal_anchor, why_now=why_now)
    return (
        f"# {task_id} — {title}\n\n"
        f"## Goal\n\n"
        f"{goal_body}\n"
        f"## Scope\n\n"
        f"{scope}\n"
        f"## Design\n\n"
        f"{design}\n"
        f"## Verification\n\n"
        f"{verification}\n"
        f"## Out of scope\n\n"
        f"{out_of_scope}"
    )


def minimal_briefing(task_id: str, **kwargs) -> str:
    """Return a briefing with the shortest acceptable bodies.

    Why-now rationale is exactly `WHY_NOW_MIN_CHARS + 1` chars (one
    above the TB-164 floor); Verification has a single backticked
    shell bullet; other sections use one-token bodies. Callers can
    override any default by passing the matching kwarg to
    `canonical_briefing`.

    For tests that pin the floor of each validator gate — the
    canonical "smallest accepted input" sample.
    """
    defaults = {
        "title": "min",
        "why_now": _MIN_WHY_NOW,
        "scope": "- x\n",
        "design": "x\n",
        "verification": "- `t`\n",
        "out_of_scope": "- x\n",
    }
    defaults.update(kwargs)
    return canonical_briefing(task_id, **defaults)


def briefing_missing(task_id: str, *, drop: str, **kwargs) -> str:
    """Return a briefing with the named section removed (or its body
    zeroed). Used by reject-path tests that need a specifically
    malformed input.

    `drop` values:
      - `"Goal"`, `"Scope"`, `"Design"`, `"Verification"`,
        `"Out of scope"` — remove the section heading + body entirely.
      - `"Why now"` — keep the Goal heading + a body, but drop the
        line-anchored `Why now:` marker (TB-164 reject case).
      - `"goal-anchor"` — keep every section + the Why-now marker, but
        the Goal body cites no anchor from goal.md (TB-161 reject case).

    Any other `drop` value raises `ValueError` — pin to keep the
    fixture surface small.
    """
    canon = canonical_briefing(task_id, **kwargs)
    if drop == "Goal":
        # Build the briefing without `## Goal`. The rest is canonical
        # shape minus a Goal body — the structural validator's first
        # reject (missing canonical section) fires.
        title = kwargs.get("title", _DEFAULT_TITLE)
        scope = kwargs.get("scope", _DEFAULT_SCOPE)
        design = kwargs.get("design", _DEFAULT_DESIGN)
        verification = kwargs.get("verification", _DEFAULT_VERIFICATION)
        out_of_scope = kwargs.get("out_of_scope", _DEFAULT_OUT_OF_SCOPE)
        return (
            f"# {task_id} — {title}\n\n"
            f"## Scope\n\n{scope}\n"
            f"## Design\n\n{design}\n"
            f"## Verification\n\n{verification}\n"
            f"## Out of scope\n\n{out_of_scope}"
        )
    if drop in ("Scope", "Design", "Verification", "Out of scope"):
        # Drop just the named section. Replace the
        # `## <name>\n\n<body>\n` block with the empty string.
        # The body delimiter is the next `## ` heading or EOF.
        return _drop_section(canon, drop)
    if drop == "Why now":
        # Keep all five canonical sections + a goal-anchor cite, but
        # remove the line-anchored Why-now marker so TB-164 fires.
        # Provide a Goal body that still cites the anchor (so TB-161
        # passes) but has no `Why now` line.
        anchor = kwargs.get("goal_anchor", _DEFAULT_GOAL_ANCHOR)
        goal_body = (
            f"Closes the failure mode the briefing scope names; "
            f"advances goal.md's {anchor}. A prose-only goal that "
            f"never says the magic words.\n"
        )
        title = kwargs.get("title", _DEFAULT_TITLE)
        scope = kwargs.get("scope", _DEFAULT_SCOPE)
        design = kwargs.get("design", _DEFAULT_DESIGN)
        verification = kwargs.get("verification", _DEFAULT_VERIFICATION)
        out_of_scope = kwargs.get("out_of_scope", _DEFAULT_OUT_OF_SCOPE)
        return (
            f"# {task_id} — {title}\n\n"
            f"## Goal\n\n{goal_body}\n"
            f"## Scope\n\n{scope}\n"
            f"## Design\n\n{design}\n"
            f"## Verification\n\n{verification}\n"
            f"## Out of scope\n\n{out_of_scope}"
        )
    if drop == "goal-anchor":
        # Keep the Why-now marker (so TB-164 passes) but the Goal body
        # cites NO goal.md anchor — TB-161 reject path. The body is
        # deliberately generic "meta-polish" prose that won't substring-
        # match against any Done-when bullet or Current-focus heading.
        why_now = kwargs.get("why_now", _DEFAULT_WHY_NOW)
        goal_body = (
            f"Polish ap2's internal logging shape — make daemon.log "
            f"prettier.\n\n"
            f"Why now: {why_now}\n"
        )
        title = kwargs.get("title", _DEFAULT_TITLE)
        scope = kwargs.get("scope", _DEFAULT_SCOPE)
        design = kwargs.get("design", _DEFAULT_DESIGN)
        verification = kwargs.get("verification", _DEFAULT_VERIFICATION)
        out_of_scope = kwargs.get("out_of_scope", _DEFAULT_OUT_OF_SCOPE)
        return (
            f"# {task_id} — {title}\n\n"
            f"## Goal\n\n{goal_body}\n"
            f"## Scope\n\n{scope}\n"
            f"## Design\n\n{design}\n"
            f"## Verification\n\n{verification}\n"
            f"## Out of scope\n\n{out_of_scope}"
        )
    raise ValueError(
        f"briefing_missing: unsupported `drop`={drop!r}. Valid: "
        '"Goal", "Why now", "Scope", "Design", "Verification", '
        '"Out of scope", "goal-anchor".'
    )


def briefing_with_manual_bullet(task_id: str, **kwargs) -> str:
    """Return a canonical-shape briefing whose `## Verification` body
    contains a `Manual:` bullet. TB-171 reject case — the gate rejects
    Manual / [manual] bullets in Verification (only).

    The first bullet is the Manual one (the validator flags the first
    offender); a clean `pytest -q` bullet follows so the empty-section
    gate also doesn't fire as a false-positive.
    """
    kwargs.setdefault(
        "verification",
        "- Manual: operator runs the daemon and observes Mattermost\n"
        "- `uv run pytest -q` — gates pass\n",
    )
    return canonical_briefing(task_id, **kwargs)


def _drop_section(briefing: str, section: str) -> str:
    """Remove the `## <section>` heading and its body from `briefing`.

    Body runs from the heading line to the next `## ` heading or EOF.
    Pure: no side effects.
    """
    lines = briefing.splitlines(keepends=True)
    out: list[str] = []
    in_target = False
    for line in lines:
        if not in_target and line.startswith(f"## {section}"):
            in_target = True
            continue
        if in_target and line.startswith("## "):
            in_target = False
            out.append(line)
            continue
        if in_target:
            continue
        out.append(line)
    return "".join(out)
