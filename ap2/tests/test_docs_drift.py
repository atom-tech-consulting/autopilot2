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
#
# TB-323 adds four entries: `AP2_COMPONENTS_` and `AP2_CORE_` are the
# regex-anchored f-string prefixes the sectioned-env override layer
# builds in `ap2/config_compat.py::_apply_sectioned_env_overrides`
# (`f"AP2_COMPONENTS_{comp.upper()}_{key.upper()}"` and the parallel
# `AP2_CORE_<FIELD>` form). They're synthetic shapes the regex picks up,
# not operator-tunable knobs in their own right — the operator's
# surface is `AP2_COMPONENTS_AUTO_APPROVE_ENABLED` (a fully-qualified
# name), never the bare prefix. `AP2_DIR` and `AP2_REAL_SDK` are
# forward-compatibility placeholders in
# `config_compat._KNOBS_STAYING_ENV_ONLY` listed per goal.md L358's
# 12-factor cut-line documentation; neither is currently read in
# source, but documenting them on the env-only side now keeps a
# future addition on the right side of the partition without an
# architectural debate.
_DOCS_DRIFT_EXEMPT_ENV_KNOBS = frozenset({
    "AP2_JANITOR_MAX_FINDINGS_LLM_DEFAULT",
    "AP2_JANITOR_JUDGE_MAX_TURNS_DEFAULT",  # TB-330 — private in-source default sentinel, not an operator knob
    "AP2_COMPONENTS_",  # TB-323 — sectioned-env f-string prefix, not an operator knob
    "AP2_CORE_",        # TB-323 — sectioned-env f-string prefix, not an operator knob
    "AP2_DIR",          # TB-323 — forward-compat placeholder per goal.md L358, not currently read
    "AP2_REAL_SDK",     # TB-323 — forward-compat placeholder per goal.md L358, not currently read
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


def test_every_env_knob_in_template_or_exempt():
    """Every `AP2_*` env knob referenced in ap2 source is EITHER mentioned
    (substring) in `ap2.init.ENV_TEMPLATE` — the `.cc-autopilot/env`
    scaffold `ap2 init` writes into fresh projects — OR listed in
    `ap2.init._TEMPLATE_EXEMPT_KNOBS` with an inline `# reason:` comment
    explaining why operators don't need it in the template (TB-305).

    Parallel to `test_every_env_knob_documented` (which gates howto.md),
    but pinned against the scaffold operators actually paste into their
    project. Forces the operator-facing-vs-internal decision at the same
    PR that adds a knob, instead of letting drift compound silently the
    way it did between TB-278 (which authored the template at 10 knobs)
    and TB-305 (which added this gate after ~40 knobs had accumulated
    outside the template).

    Substring-matching against `ENV_TEMPLATE` is safe: the f-string
    default-value interpolations resolve to constant values (numbers,
    strings like `"claude-opus-4-7"`), not knob names, so a literal
    `AP2_FOO` substring scan cannot false-positive on a default.
    """
    from ap2.init import ENV_TEMPLATE, _TEMPLATE_EXEMPT_KNOBS
    knobs = _collect_env_knobs()
    assert knobs, "no env knobs found in source — regex or walk regressed"
    missing = sorted(
        knob for knob in knobs
        if knob not in ENV_TEMPLATE and knob not in _TEMPLATE_EXEMPT_KNOBS
    )
    assert not missing, (
        f"env knob(s) referenced in source but NEITHER documented in "
        f"`ap2.init.ENV_TEMPLATE` (the `.cc-autopilot/env` scaffold) NOR "
        f"listed in `ap2.init._TEMPLATE_EXEMPT_KNOBS`: {missing}.\n\n"
        f"To make the gate pass, EITHER:\n\n"
        f"(1) Add a commented `# AP2_FOO=<default>` block to "
        f"`ENV_TEMPLATE` following the existing format — a block "
        f"comment explaining what the knob does + a commented-out "
        f"KEY=VALUE line. Reference a `config.DEFAULT_*` constant via "
        f"f-string interpolation where one exists; inline a literal "
        f"otherwise. Use this path when the knob is operator-facing — "
        f"something a fresh-project operator should discover from the "
        f"scaffold alone.\n\n"
        f"(2) Add the knob to `_TEMPLATE_EXEMPT_KNOBS` (next to "
        f"`ENV_TEMPLATE` in `ap2/init.py`) with a `# reason: ...` "
        f"comment categorizing why it's not template-worthy — "
        f"debug/test only, internal default rarely tuned, integration "
        f"secret set via shell export, covered by a sibling global, "
        f"etc. The `# reason:` comment IS the audit trail for a future "
        f"reader asking \"should this graduate to the template?\""
    )


def test_every_config_key_documented():
    """Every config key declared in `aggregate_schemas(default_registry())`
    is EITHER referenced verbatim in `ap2/howto.md`'s `## Config keys
    (TOML)` block (the structurally-parallel sibling to `## Configuration
    knobs`) OR listed in `ap2.init._CONFIG_TEMPLATE_EXEMPT_KEYS` with an
    inline `# reason:` comment explaining why operators don't need it
    surfaced (TB-325, axis 6 of the structured-config focus).

    Parallel to `test_every_env_knob_documented` / `_in_template_or_exempt`
    (TB-305 — flat `AP2_*` env knob gate) but pinned against the new
    per-component schema surface TB-321/322 landed. Forces the
    operator-facing-vs-internal decision at the same PR that adds a
    `ConfigKey`, instead of letting drift compound the way it did between
    TB-278 (which authored the env template at 10 knobs) and TB-305
    (which added that gate after ~40 knobs had accumulated outside it).

    Matching is full-path substring: each key is rendered as the dotted
    `components.<name>.<key>` form an operator would type into config.toml.
    A `` `components.foo.bar` `` backtick-fence OR a bare row entry both
    satisfy the gate — the howto section uses tree-rendered prose, not a
    strict-fence enumeration. The test ALSO pins existence of the
    `## Config keys (TOML)` heading so a future refactor that moves the
    block (or removes it entirely) trips here.

    TB-337 (axis-1 completion) extends the walk to `[core.*]` keys via
    `ap2.core_config_schema.CORE_CONFIG_SCHEMA`. Pre-TB-337 the test
    only covered per-component manifests because the core surface was
    declared "schema deferred to a future axis" in `howto.md` L2376-2379;
    that gap is now closed and the gate enforces docs-drift parity
    across both surfaces in one walk.
    """
    from ap2.init import _CONFIG_TEMPLATE_EXEMPT_KEYS
    from ap2.config_loader import aggregate_schemas
    from ap2.core_config_schema import CORE_CONFIG_SCHEMA
    from ap2.registry import default_registry

    howto = HOWTO_PATH.read_text()
    assert "## Config keys (TOML)" in howto, (
        "howto.md is missing the `## Config keys (TOML)` heading — the "
        "structured-config TOML surface (TB-321/322/325) needs a "
        "dedicated reference block parallel to `## Configuration knobs`. "
        "Add the section with a tree-rendered list of `components.<name>.<key>` "
        "paths sourced from the per-component manifest `config_schema` "
        "declarations."
    )

    # TB-337: include CORE_CONFIG_SCHEMA in the aggregate walk so the
    # `[core.*]` keys land on the docs-drift gate too. The returned
    # dict carries `"core"` as a reserved namespace alongside the
    # per-component entries; we re-bind each side to its own emit
    # prefix (`core.<key>` vs `components.<name>.<key>`).
    schemas = aggregate_schemas(
        default_registry(), core_schema=CORE_CONFIG_SCHEMA,
    )
    assert schemas, (
        "no component config schemas found — registry walk or "
        "aggregate_schemas regressed; the docs-drift gate would pass "
        "vacuously"
    )

    paths: list[str] = []
    for namespace in sorted(schemas):
        prefix = "core" if namespace == "core" else f"components.{namespace}"
        for key_name in sorted(schemas[namespace]):
            paths.append(f"{prefix}.{key_name}")

    missing = sorted(
        p for p in paths
        if p not in howto and p not in _CONFIG_TEMPLATE_EXEMPT_KEYS
    )
    assert not missing, (
        f"config key(s) declared on a component manifest "
        f"`config_schema` but NEITHER documented in `ap2/howto.md`'s "
        f"`## Config keys (TOML)` block NOR listed in "
        f"`ap2.init._CONFIG_TEMPLATE_EXEMPT_KEYS`: {missing}.\n\n"
        f"To make the gate pass, EITHER:\n\n"
        f"(1) Add a row to `## Config keys (TOML)` in howto.md naming the "
        f"full `components.<name>.<key>` path, its default, and a one-line "
        f"description (mirror the ConfigKey's `description` field). Use "
        f"this path when the knob is operator-facing — something a "
        f"fresh-project operator should discover from the reference "
        f"section alone.\n\n"
        f"(2) Add the path to `_CONFIG_TEMPLATE_EXEMPT_KEYS` in "
        f"`ap2/init.py` with a `# reason: ...` comment categorizing why "
        f"the key is not documentation-worthy — deprecated alias for "
        f"another key, test-only, undocumented-on-purpose, etc. The "
        f"`# reason:` comment IS the audit trail for a future reader "
        f"asking \"should this graduate to documentation?\""
    )


def test_every_config_key_in_template():
    """Every config key declared in `aggregate_schemas(default_registry())`
    appears in `ap2.init.CONFIG_TEMPLATE` — the `.cc-autopilot/config.toml`
    scaffold `ap2 init` writes into fresh projects (TB-325, axis 6).

    Parallel to `test_every_env_knob_in_template_or_exempt` for the env
    template surface, this test pins the template's "renders the full
    schema union" contract. The template body is generated at module-
    import time from `aggregate_schemas(default_registry(),
    core_schema=CORE_CONFIG_SCHEMA)`, so this assertion would only fire
    if the renderer's walk regressed (e.g. a future refactor that
    hardcoded a partial section list). No exempt set is consulted here
    — by construction, every schema key MUST appear in the rendered
    template; the operator-facing exemption is
    `_CONFIG_TEMPLATE_EXEMPT_KEYS` (covered by
    `test_every_config_key_documented` above), which gates documentation
    presence, not template inclusion.

    TB-337 (axis-1 completion) extends the walk to `[core.*]` keys —
    the `"core"` reserved namespace in the aggregated dict renders as
    a top-level `[core]` block (NOT `[components.core]`); per-component
    blocks still render as `[components.<name>]` as before.
    """
    from ap2.init import CONFIG_TEMPLATE
    from ap2.config_loader import aggregate_schemas
    from ap2.core_config_schema import CORE_CONFIG_SCHEMA
    from ap2.registry import default_registry

    schemas = aggregate_schemas(
        default_registry(), core_schema=CORE_CONFIG_SCHEMA,
    )
    assert schemas, (
        "no component config schemas found — registry walk regressed; "
        "the template-coverage gate would pass vacuously"
    )

    missing: list[str] = []
    for namespace in sorted(schemas):
        # TB-337: `"core"` renders as `[core]` at the top; other
        # entries render as `[components.<name>]`.
        section_marker = (
            "[core]" if namespace == "core" else f"[components.{namespace}]"
        )
        if section_marker not in CONFIG_TEMPLATE:
            missing.append(section_marker)
            continue
        prefix = "core" if namespace == "core" else f"components.{namespace}"
        for key_name in sorted(schemas[namespace]):
            # The renderer emits the key as `# <key> = <default>` so we
            # match the commented-out form to avoid false positives on
            # description-prose mentions.
            needle = f"# {key_name} ="
            # Each section's body is bounded — but a substring scan on the
            # full template is enough: the key names are unique within a
            # well-formed schema and tightly fenced by the comment prefix.
            if needle not in CONFIG_TEMPLATE:
                missing.append(f"{prefix}.{key_name}")

    assert not missing, (
        f"config key(s) declared on a component manifest "
        f"`config_schema` or in `CORE_CONFIG_SCHEMA` but missing from "
        f"`ap2.init.CONFIG_TEMPLATE`: {missing}. The template body is "
        f"generated at module-import time from `aggregate_schemas(...)` "
        f"— if this test trips, the renderer (`_render_config_template`) "
        f"has regressed and is no longer walking the full schema union."
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
