"""Docs-drift coverage gate (TB-203, TB-207).

Every operator-facing surface — MCP tool names registered in
`CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` / `MM_HANDLER_TOOLS`, every
`AP2_*` env knob referenced in `ap2/*.py`, every event-type string passed
to `events.append(...)`, every non-suppressed `ap2 <verb>` subcommand in
`build_parser()` — must be referenced (by exact name) in `ap2/howto.md`
(and/or `ap2/architecture.md` for the MCP-tools enumeration). A future
source addition (new env knob, new MCP tool, new event type, new CLI
verb) trips one of these tests until docs catch up, so the
operator-facing surface can't silently drift past the reference.

The five tests share a tiny module-local set of constants but otherwise
stay independent — a future single-surface addition fails exactly one
test with a precise diff, not a cascade.
"""
from __future__ import annotations

import re
from pathlib import Path

from ap2.tests._source_registry import _collect_cli_verbs
from ap2.tools import CONTROL_AGENT_TOOLS, MM_HANDLER_TOOLS, TASK_AGENT_TOOLS


REPO_ROOT = Path(__file__).resolve().parents[2]
AP2_DIR = REPO_ROOT / "ap2"
HOWTO_PATH = AP2_DIR / "howto.md"
ARCHITECTURE_PATH = AP2_DIR / "architecture.md"


# Claude built-ins are not autopilot MCP tools. They appear in agent
# toolsets as "broad reads" / "task agent code edits" — same set the
# existing toolset prompts already treat as built-in, so excluding them
# here matches the prompt-side framing. A future built-in addition (e.g.
# `NotebookEdit`) should be appended here; an actual new MCP tool should
# stay OUT of this set so the docs gate keeps firing.
_BUILTIN_TOOLS = frozenset({
    "Read", "Glob", "Grep", "Bash", "Edit", "Write",
})


# Private constants whose identifier happens to start with `AP2_` after the
# leading underscore. These are NOT env knobs — they're module-private
# default-value constants that piggyback on the env-var naming convention
# (e.g. `_AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT = 10` in `ap2/janitor.py`,
# the fallback for the like-named env var). Exempting them keeps the gate
# focused on real operator-tunable surfaces. A future genuine env knob
# named `*_DEFAULT` would be a misnomer and should rename rather than land
# on this list.
_DOCS_DRIFT_EXEMPT_ENV_KNOBS = frozenset({
    "AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT",
})


# Dynamic event types — `events.append(cfg.events_file, typ, ...)` from
# `do_log_event` covers any agent-emitted type. The regex below can't
# enumerate those by definition (the second arg is a runtime variable).
# Empty today; a future grep-friendly f-string-named event type can land
# here to opt out of the gate with an explicit comment.
_DOCS_DRIFT_EXEMPT_EVENT_TYPES: frozenset[str] = frozenset()


def _iter_source_files() -> list[Path]:
    """Every `*.py` under `ap2/` excluding `ap2/tests/` and `__pycache__/`.

    Mirrored across all three source-walk tests so a future addition in
    one place auto-shows up in the others.
    """
    out: list[Path] = []
    for path in sorted(AP2_DIR.rglob("*.py")):
        rel = path.relative_to(AP2_DIR)
        parts = rel.parts
        if parts and parts[0] == "tests":
            continue
        if "__pycache__" in parts:
            continue
        out.append(path)
    return out


def _short_name(tool: str) -> str:
    """Strip the `mcp__autopilot__` prefix Claude Code applies to MCP tool
    names so the docs check matches the bare tool identifier readers see
    in the reference section (e.g. `report_result`, not `mcp__autopilot__report_result`).
    """
    prefix = "mcp__autopilot__"
    return tool[len(prefix):] if tool.startswith(prefix) else tool


def _collect_env_knobs() -> set[str]:
    """Regex `AP2_[A-Z_][A-Z_0-9]*` over every source file's text.

    Default-value docstring comments that happen to mention an env knob
    still count as in-source — those are the load-bearing places a reader
    would search to learn what the knob does, and the docs gate is the
    sole authority that connects source → docs.
    """
    pat = re.compile(r"AP2_[A-Z_][A-Z_0-9]*")
    knobs: set[str] = set()
    for path in _iter_source_files():
        knobs.update(pat.findall(path.read_text()))
    return knobs - _DOCS_DRIFT_EXEMPT_ENV_KNOBS


def _collect_event_types() -> set[str]:
    """Regex the second positional arg of `events.append(events_file, "<type>", ...)`.

    `[^,]+` matches across newlines (it's a negated character class, not
    `.`), so multi-line calls with one arg per line are caught. Dynamic
    types (the `do_log_event` `typ` variable, any `f"..."` event name)
    fall outside the regex by design — those land on the exempt list with
    a comment if they ever exist.
    """
    pat = re.compile(
        r"events\.append\(\s*[^,]+,\s*[\"']([a-z_][a-z_0-9]*)[\"']"
    )
    types: set[str] = set()
    for path in _iter_source_files():
        types.update(pat.findall(path.read_text()))
    return types - _DOCS_DRIFT_EXEMPT_EVENT_TYPES


def _all_agent_mcp_tool_short_names() -> set[str]:
    """Union of `CONTROL_AGENT_TOOLS` + `TASK_AGENT_TOOLS` + `MM_HANDLER_TOOLS`,
    stripped of the `mcp__autopilot__` prefix and filtered to drop the
    Claude built-ins (Read/Glob/Grep/Bash/Edit/Write).
    """
    union = set(CONTROL_AGENT_TOOLS) | set(TASK_AGENT_TOOLS) | set(MM_HANDLER_TOOLS)
    return {_short_name(t) for t in union if _short_name(t) not in _BUILTIN_TOOLS}


def _extract_python_block(text: str, decl: str) -> str:
    """Extract the body of a `decl = [ ... ]` literal from a python fenced
    code block in markdown. `decl` is the variable name (e.g.
    `CONTROL_AGENT_TOOLS`). Returns the text between `[` and the matching
    closing `]`. Raises if no such declaration is found — that's a
    failure of the same shape as a missing-tool failure (the docs no
    longer enumerate the source-of-truth literal).
    """
    pat = re.compile(rf"{re.escape(decl)}\s*=\s*\[(.*?)\]", re.DOTALL)
    m = pat.search(text)
    if not m:
        raise AssertionError(
            f"no `{decl} = [...]` literal found in architecture.md — "
            f"docs no longer enumerate the source-of-truth toolset"
        )
    return m.group(1)


# ---------------------------------------------------------------------------
# The four tests.


def test_every_mcp_tool_documented():
    """Every MCP tool reachable by any agent toolset is mentioned (by
    exact short name) in `ap2/howto.md` OR `ap2/architecture.md`. The
    OR-across-files split is load-bearing: `architecture.md`'s
    `CONTROL_AGENT_TOOLS` literal is itself the enumeration, while
    `howto.md`'s `## Custom MCP tools (reference)` carries the
    descriptions — either surface satisfies the gate.
    """
    howto = HOWTO_PATH.read_text()
    arch = ARCHITECTURE_PATH.read_text()
    combined = howto + "\n" + arch
    missing = sorted(
        name for name in _all_agent_mcp_tool_short_names() if name not in combined
    )
    assert not missing, (
        f"MCP tool(s) reachable by an agent toolset but not mentioned in "
        f"howto.md or architecture.md: {missing}. Add a reference (with "
        f"a one-line description in `## Custom MCP tools (reference)`) "
        f"so the operator surface stays discoverable. Source of truth: "
        f"`ap2.tools.CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` / "
        f"`MM_HANDLER_TOOLS`."
    )


def test_every_env_knob_documented():
    """Every `AP2_*` env knob referenced in ap2 source is mentioned in
    `ap2/howto.md`, AND each mention is backtick-fenced (e.g.
    `` `AP2_FOO` ``) so the rendered list shape stays uniform. A
    substring-only check would silently accept prose that referenced the
    knob without the rendered-list framing; the backtick fence is what
    the operator's eye scans for in the `## Configuration knobs`
    section.
    """
    howto = HOWTO_PATH.read_text()
    knobs = _collect_env_knobs()
    assert knobs, "no env knobs found in source — regex or walk regressed"
    missing = sorted(
        knob for knob in knobs if f"`{knob}`" not in howto
    )
    assert not missing, (
        f"env knob(s) referenced in source but missing a backtick-fenced "
        f"mention in howto.md: {missing}. Add to `## Configuration "
        f"knobs` so operators can discover them. If a hit is a private "
        f"constant (e.g. `_AP2_FOO_DEFAULT`), add it to "
        f"`_DOCS_DRIFT_EXEMPT_ENV_KNOBS` with a comment explaining why."
    )


def test_every_event_type_documented():
    """Every event type emitted via `events.append(events_file, "<type>", ...)`
    in ap2 source is mentioned in `ap2/howto.md`. Substring check (not
    backtick-required) because event types appear both backtick-fenced
    in the `## Event schema` enumeration AND inside descriptive prose
    elsewhere — either flavor counts. The point is that an operator
    grepping howto.md for the event type they just saw in events.jsonl
    finds a hit.
    """
    howto = HOWTO_PATH.read_text()
    types = _collect_event_types()
    assert types, "no event types found in source — regex or walk regressed"
    missing = sorted(t for t in types if t not in howto)
    assert not missing, (
        f"event type(s) emitted in source but missing from howto.md: "
        f"{missing}. Add to `## Event schema` (Lifecycle / Failure / "
        f"State-observability) so operators reading events.jsonl can "
        f"map the type back to what code wrote it. Dynamic types "
        f"(emitted via `do_log_event`) opt out via "
        f"`_DOCS_DRIFT_EXEMPT_EVENT_TYPES` with a comment."
    )


def test_every_cli_verb_documented():
    """Every non-suppressed `ap2 <verb>` subcommand in `build_parser()`
    is mentioned (by exact `ap2 <verb>` substring) in `ap2/howto.md`'s
    `## Operator CLI verbs (reference)` section. Substring check (not
    backtick-required) so the verb can appear bare-quoted in a row's
    `verb` cell or in surrounding prose; the point is the operator's
    grep finds a hit when they read `ap2 <verb>` in a Mattermost
    mention or a `--help` string and want a WHY/when-to-use companion.

    Hidden / dev-only subparsers (`help=argparse.SUPPRESS`, e.g.
    `ap2 _run`) are excluded by `_collect_cli_verbs` so the gate
    matches the howto section's stated exclusion.
    """
    howto = HOWTO_PATH.read_text()
    verbs = _collect_cli_verbs()
    assert verbs, "no CLI verbs collected from build_parser() — walk regressed"
    missing = sorted(v for v in verbs if v not in howto)
    assert not missing, (
        f"CLI verb(s) registered in `ap2/cli.py`'s `build_parser()` but "
        f"missing from howto.md: {missing}. Add a row to `## Operator CLI "
        f"verbs (reference)` describing why an operator reaches for the "
        f"verb (purpose) and what failure mode / related verbs it sits "
        f"alongside (notes). If the new subparser is dev-only and the "
        f"operator shouldn't see it, mark it `help=argparse.SUPPRESS` "
        f"so `_collect_cli_verbs` drops it (mirroring `ap2 _run`)."
    )


def test_architecture_md_control_agent_tools_complete():
    """`ap2/architecture.md`'s ```python``` block under `## Custom MCP
    tools` enumerates `CONTROL_AGENT_TOOLS` and `TASK_AGENT_TOOLS` as
    python literals for the reader. Both literal blocks must mention
    every tool short name in the corresponding source list (substring
    match, so the `mcp__autopilot__` prefix is tolerated in either
    direction). This is the architecture-doc-specific counterpart of
    `test_every_mcp_tool_documented`: catches the case where the docs
    enumeration of the literal goes stale even if every tool also has a
    descriptive mention in `howto.md`.
    """
    text = ARCHITECTURE_PATH.read_text()

    for decl, source_list in (
        ("CONTROL_AGENT_TOOLS", CONTROL_AGENT_TOOLS),
        ("TASK_AGENT_TOOLS", TASK_AGENT_TOOLS),
    ):
        block = _extract_python_block(text, decl)
        missing = sorted(
            _short_name(t)
            for t in source_list
            if _short_name(t) not in _BUILTIN_TOOLS
            and _short_name(t) not in block
        )
        assert not missing, (
            f"architecture.md's `{decl}` literal block is missing tool(s) "
            f"that appear in `ap2.tools.{decl}`: {missing}. Extend the "
            f"python fenced block under `## Custom MCP tools` to enumerate "
            f"every entry — the literal is the operator-facing "
            f"source-of-truth proxy."
        )
