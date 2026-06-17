"""Docs-drift coverage gate (TB-203, TB-207).

Every operator-facing surface — MCP tool names registered in
`CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` / `MM_HANDLER_TOOLS`, every
`AP2_*` env knob referenced in `ap2/*.py`, every event-type string passed
to `events.append(...)`, every non-suppressed `ap2 <verb>` subcommand in
`build_parser()` — must be referenced (by exact name) in the owning
operator skill under `skills/ap2-*/SKILL.md` (and/or `ap2/architecture.md`
for the MCP-tools enumeration). A future source addition (new env knob, new
MCP tool, new event type, new CLI verb) trips one of these tests until docs
catch up, so the operator-facing surface can't silently drift past the
reference. (TB-397–406 carved every operator domain out of the old
operator manual into per-domain skills and retired the file; each gate below
now reads the skill that owns its surface.)

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
ARCHITECTURE_PATH = AP2_DIR / "architecture.md"

# TB-397 — the observability domain (event schema + prose-judge diagnostics
# + `ap2 logs` / stats) was carved out of the old operator manual into the
# first domain skill as the canary that established the carve-plus-gate-
# retarget pattern. The event-type drift gate reads its coverage surface from
# here. (TB-406 retired the file entirely; every gate below now reads its
# owning skill — there is no `HOWTO_PATH` fallback left.)
OBSERVABILITY_SKILL = REPO_ROOT / "skills/ap2-observability/SKILL.md"

# TB-398 — the configuration domain (the `## Configuration knobs` flat
# `AP2_*` catalogue + the `## Config keys (TOML)` typed-schema reference +
# the Codex backend setup) was carved out of the old operator manual into
# `skills/ap2-config/SKILL.md`, the second domain carve following the
# TB-397 canary pattern. The env-knob and config-key coverage gates read
# their documentation surface from here instead of `HOWTO_PATH`.
CONFIG_SKILL = REPO_ROOT / "skills/ap2-config/SKILL.md"

# TB-399 — the board-ops domain (the `## Custom MCP tools (reference)` tool
# catalogue + the `## Operator CLI verbs (reference)` table) was carved out
# of the old operator manual into `skills/ap2-board-ops/SKILL.md`, the third domain carve
# following the TB-397 canary pattern. The CLI-verb gate reads its coverage
# surface from here instead of `HOWTO_PATH`; the MCP-tool gate adds the skill
# to its accepted set alongside `architecture.md` (keeping the
# howto-OR-architecture fallback semantics, just with the skill replacing
# howto as the descriptive surface) so no MCP tool documented here becomes
# uncovered mid-migration.
BOARD_OPS_SKILL = REPO_ROOT / "skills/ap2-board-ops/SKILL.md"

# TB-402 — the failure-recovery domain (the `## Failure modes the daemon
# recovers from` auto-recovery catalogue + the `## Operator-question
# playbook` lookup table) was carved out of the old operator manual into
# `skills/ap2-failure-recovery/SKILL.md`, the fourth domain carve following
# the TB-397 canary pattern. Unlike the env-knob / config-key / event-type /
# CLI-verb axes, neither carved section had a mechanical docs-drift gate
# pinning its coverage (both are operator prose + a lookup table, not a
# source-enumerated set), so there is no `HOWTO_PATH`-keyed gate to retarget
# here — the constant registers the skill path for parity with its three
# siblings and for any future failure-recovery coverage gate to read from.
FAILURE_RECOVERY_SKILL = REPO_ROOT / "skills/ap2-failure-recovery/SKILL.md"

# TB-403 — the ideation + goal/focus-management domain (the `## Authoring
# goal.md` operator-curated five-section reference + the `## Retrospective
# audit workflow` `ap2 audit` review surface) was carved out of the old
# operator manual into `skills/ap2-ideation-goals/SKILL.md`, the fifth domain carve following
# the TB-397 canary pattern. The `## Authoring goal.md` section's existing
# docs-location gate lives in `ap2/tests/test_docs.py` and was retargeted onto
# this skill in the same commit (the structural-anchor + worked-example-
# validator pins); like the failure-recovery carve, the `## Retrospective
# audit workflow` prose had no mechanical `HOWTO_PATH`-keyed coverage gate to
# retarget (the `ap2 audit` CLI verb is gated against `BOARD_OPS_SKILL`).
# `test_ideation_goals_domain_carved_to_skill` below uses this constant to pin
# that the content moved to the skill and is not duplicated back in howto.
# `ideation.default.md` stays canonical for the daemon ideation agent's own
# briefing-authoring conventions — the skill references but does not move them.
IDEATION_GOALS_SKILL = REPO_ROOT / "skills/ap2-ideation-goals/SKILL.md"


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
    # TB-358 — per-agent-kind backend selector f-string prefix
    # (`f"AP2_AGENT_BACKEND_{kind.upper()}"` in `config.get_agent_backend`).
    # The bare prefix is a synthetic shape the regex picks up, not an
    # operator knob; the operator surface is the fully-qualified
    # `AP2_AGENT_BACKEND_<KIND>` family, documented (with the valid kinds
    # enumerated) in `init.ENV_TEMPLATE`'s agent-backend block.
    "AP2_AGENT_BACKEND_",
    # A spelled-out family member: `ap2/ideation_scrub.py` (TB-360,
    # fc5db75) names the concrete `ideation_scrub` kind's override in a
    # docstring, so the regex picks up the fully-qualified literal rather
    # than the f-string-derived bare prefix above. Same family, same
    # rationale — its operator surface is the generic
    # `AP2_AGENT_BACKEND_<KIND>` block, not an individually-documented knob.
    "AP2_AGENT_BACKEND_IDEATION_SCRUB",
    # TB-363 — same shape: the validator-judge / janitor-judge axis-6
    # migration spells the two concrete `validator_judge` / `janitor_judge`
    # kinds' overrides literally in `_resolve_validator_judge_adapter` /
    # `_resolve_janitor_judge_adapter` docstrings, so the regex captures the
    # fully-qualified literals. Same family, same rationale — the operator
    # surface is the generic `AP2_AGENT_BACKEND_<KIND>` block.
    "AP2_AGENT_BACKEND_VALIDATOR_JUDGE",
    "AP2_AGENT_BACKEND_JANITOR_JUDGE",
    # TB-364 — same shape: the run_task axis-6 migration spells the concrete
    # `task` kind's override literally in `run_task`'s adapter-resolution
    # comment, so the regex captures the fully-qualified literal. Same family,
    # same rationale — the operator surface is the generic
    # `AP2_AGENT_BACKEND_<KIND>` block, not an individually-documented knob.
    "AP2_AGENT_BACKEND_TASK",
    "AP2_DIR",          # TB-323 — forward-compat placeholder per goal.md L358, not currently read
    "AP2_REAL_SDK",     # TB-323 — forward-compat placeholder per goal.md L358, not currently read
    # TB-413/TB-414 — deployment-identity members of
    # `config.ENV_PERMITTED_KEYS` (the 12-factor allowlist) that the regex
    # picks up from the allowlist literal in `ap2/config.py`. TB-414 lands
    # their operator-facing documentation in `init.ENV_TEMPLATE`'s
    # `Deployment identity` block + the `ap2-config` skill's `##
    # Configuration knobs`. They stay exempt from the mechanical docs gate
    # because neither has a DEDICATED reader in source yet (both appear only
    # in the `ENV_PERMITTED_KEYS` allowlist literal) — forward-compat
    # allowlist members, same shape as `AP2_DIR` / `AP2_REAL_SDK` above. A
    # future TB that wires a real reader graduates them off this list.
    "AP2_WEB_HOST",
    "AP2_SANDBOX_USER",
    # TB-414 — the global agent-model tunable's flat name. It HAS a
    # config.toml home (`core.agent_model`) and is documented in the
    # `ap2-config` skill (`## Configuration knobs` + `## Config keys
    # (TOML)`), so it is NOT undocumented; it is exempted here only because
    # TB-414's env-template canary grep (`! grep -nE "AP2_AGENT_MODEL|…"
    # ap2/init.py`) forbids the flat-name literal anywhere in `ap2/init.py`,
    # which rules out the usual `init._TEMPLATE_EXEMPT_KNOBS` home its
    # behavioral-tunable siblings (AP2_TASK_MAX_TURNS, …) use. Operators set
    # it via `ap2 config set core.agent_model <id>`, never the env scaffold.
    "AP2_AGENT_MODEL",
    # TB-345 — DEPRECATED back-compat aliases. The `focus_advance`
    # component was merged into the core `ap2/ideation_halt.py` module
    # and these two flat names were renamed to the `AP2_IDEATION_HALT_*`
    # namespace (the new canonical names ARE template+docs gated). The
    # old names survive ONLY in `ap2/config_compat.py`'s FLAT_TO_SECTIONED
    # deprecated-alias map (and tests) for one release; they are
    # intentionally absent from `init.py`'s ENV_TEMPLATE / exempt set so
    # a fresh-project scaffold never advertises a deprecated knob. Exempt
    # them from the template-vs-exempt + config-skill-mention gates here so
    # the alias map doesn't force the deprecated name back into the scaffold.
    # (Keeping them out of the scaffold is belt-and-suspenders, not
    # gate-required.) A later task drops the aliases entirely.
    "AP2_FOCUS_ADVANCE_EMPTY_CYCLES",
    "AP2_FOCUS_AUTO_ADVANCE_DISABLED",
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
    exact short name) in `skills/ap2-board-ops/SKILL.md` OR
    `ap2/architecture.md`. The OR-across-files split is load-bearing:
    `architecture.md`'s `CONTROL_AGENT_TOOLS` literal is itself the
    enumeration, while the board-ops skill's `## Custom MCP tools
    (reference)` carries the descriptions — either surface satisfies the
    gate.

    TB-399 retargeted the descriptive surface from `HOWTO_PATH` to
    `BOARD_OPS_SKILL` when the board-ops domain (the MCP-tool catalogue +
    the operator CLI-verb table) was carved into the `ap2-board-ops` skill.
    `architecture.md` stays in the accepted set (its literal enumeration is
    independently gated by `test_architecture_md_control_agent_tools_complete`),
    so the skill replaces howto as the prose half of the howto-OR-architecture
    fallback — no MCP tool documented in the moved section becomes uncovered
    by the carve.
    """
    skill = BOARD_OPS_SKILL.read_text()
    arch = ARCHITECTURE_PATH.read_text()
    combined = skill + "\n" + arch
    missing = sorted(
        name for name in _all_agent_mcp_tool_short_names() if name not in combined
    )
    assert not missing, (
        f"MCP tool(s) reachable by an agent toolset but not mentioned in "
        f"`skills/ap2-board-ops/SKILL.md` or architecture.md: {missing}. Add "
        f"a reference (with a one-line description in the skill's `## Custom "
        f"MCP tools (reference)`) so the operator surface stays discoverable. "
        f"Source of truth: `ap2.tools.CONTROL_AGENT_TOOLS` / `TASK_AGENT_TOOLS` "
        f"/ `MM_HANDLER_TOOLS`."
    )


def test_every_env_knob_documented():
    """Every `AP2_*` env knob referenced in ap2 source is mentioned in
    `skills/ap2-config/SKILL.md`, AND each mention is backtick-fenced (e.g.
    `` `AP2_FOO` ``) so the rendered list shape stays uniform. A
    substring-only check would silently accept prose that referenced the
    knob without the rendered-list framing; the backtick fence is what
    the operator's eye scans for in the skill's `## Configuration knobs`
    section.

    TB-398 retargeted this gate from `HOWTO_PATH` to `CONFIG_SKILL` when
    the configuration domain was carved into the `ap2-config` skill — the
    skill is now the canonical home of the env-knob catalogue, so it is the
    surface a source-side knob addition must keep in sync.
    """
    skill = CONFIG_SKILL.read_text()
    knobs = _collect_env_knobs()
    assert knobs, "no env knobs found in source — regex or walk regressed"
    missing = sorted(
        knob for knob in knobs if f"`{knob}`" not in skill
    )
    assert not missing, (
        f"env knob(s) referenced in source but missing a backtick-fenced "
        f"mention in `skills/ap2-config/SKILL.md`: {missing}. Add to the "
        f"skill's `## Configuration knobs` so operators can discover them. "
        f"If a hit is a private constant (e.g. `_AP2_FOO_DEFAULT`), add it "
        f"to `_DOCS_DRIFT_EXEMPT_ENV_KNOBS` with a comment explaining why."
    )


def test_every_env_knob_in_template_or_exempt():
    """Every `AP2_*` env knob referenced in ap2 source is EITHER mentioned
    (substring) in `ap2.init.ENV_TEMPLATE` — the `.cc-autopilot/env`
    scaffold `ap2 init` writes into fresh projects — OR listed in
    `ap2.init._TEMPLATE_EXEMPT_KNOBS` with an inline `# reason:` comment
    explaining why operators don't need it in the template (TB-305).

    Parallel to `test_every_env_knob_documented` (which gates the
    `ap2-config` skill), but pinned against the scaffold operators actually
    paste into their
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
    is EITHER referenced verbatim in `skills/ap2-config/SKILL.md`'s
    `## Config keys (TOML)` block (the structurally-parallel sibling to
    `## Configuration knobs`) OR listed in
    `ap2.init._CONFIG_TEMPLATE_EXEMPT_KEYS` with an inline `# reason:`
    comment explaining why operators don't need it surfaced (TB-325, axis 6
    of the structured-config focus).

    TB-398 retargeted this gate from `HOWTO_PATH` to `CONFIG_SKILL` when
    the configuration domain (both `## Configuration knobs` and `## Config
    keys (TOML)`) was carved into the `ap2-config` skill — the skill is now
    the canonical home of the TOML key reference, so it is the surface a
    new `ConfigKey` declaration must keep in sync.

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
    satisfy the gate — the skill section uses tree-rendered prose, not a
    strict-fence enumeration. The test ALSO pins existence of the
    `## Config keys (TOML)` heading so a future refactor that moves the
    block (or removes it entirely) trips here.

    TB-337 (axis-1 completion) extends the walk to `[core.*]` keys via
    `ap2.core_config_schema.CORE_CONFIG_SCHEMA`. Pre-TB-337 the test
    only covered per-component manifests because the core surface was
    declared "schema deferred to a future axis" (pre-TB-337);
    that gap is now closed and the gate enforces docs-drift parity
    across both surfaces in one walk.
    """
    from ap2.init import _CONFIG_TEMPLATE_EXEMPT_KEYS
    from ap2.config_loader import aggregate_schemas
    from ap2.core_config_schema import CORE_CONFIG_SCHEMA
    from ap2.registry import default_registry

    skill = CONFIG_SKILL.read_text()
    assert "## Config keys (TOML)" in skill, (
        "skills/ap2-config/SKILL.md is missing the `## Config keys (TOML)` "
        "heading — the structured-config TOML surface (TB-321/322/325) needs "
        "a dedicated reference block parallel to `## Configuration knobs`. "
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
        if p not in skill and p not in _CONFIG_TEMPLATE_EXEMPT_KEYS
    )
    assert not missing, (
        f"config key(s) declared on a component manifest "
        f"`config_schema` but NEITHER documented in "
        f"`skills/ap2-config/SKILL.md`'s "
        f"`## Config keys (TOML)` block NOR listed in "
        f"`ap2.init._CONFIG_TEMPLATE_EXEMPT_KEYS`: {missing}.\n\n"
        f"To make the gate pass, EITHER:\n\n"
        f"(1) Add a row to `## Config keys (TOML)` in the ap2-config skill "
        f"naming the full `components.<name>.<key>` path, its default, and a "
        f"one-line description (mirror the ConfigKey's `description` field). "
        f"Use "
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
    in ap2 source is mentioned in `skills/ap2-observability/SKILL.md`.
    Substring check (not backtick-required) because event types appear
    both backtick-fenced in the skill's `## Event schema` enumeration AND
    inside descriptive prose elsewhere — either flavor counts. The point
    is that an operator grepping the observability skill for the event
    type they just saw in events.jsonl finds a hit.

    TB-397 retargeted this gate from the old `HOWTO_PATH` to
    `OBSERVABILITY_SKILL` when the event-schema domain was carved into the
    canary skill — the skill is now the canonical home of the event timeline,
    so it is the surface a source-side event-type addition must keep in sync.
    (TB-406 finished the carve sweep and retired the file; every sibling
    gate now reads its owning skill, so no gate has a `HOWTO_PATH` fallback.)
    """
    skill = OBSERVABILITY_SKILL.read_text()
    types = _collect_event_types()
    assert types, "no event types found in source — regex or walk regressed"
    missing = sorted(t for t in types if t not in skill)
    assert not missing, (
        f"event type(s) emitted in source but missing from "
        f"`skills/ap2-observability/SKILL.md`: {missing}. Add to the "
        f"skill's `## Event schema` (Lifecycle / Failure / "
        f"State-observability) so operators reading events.jsonl can "
        f"map the type back to what code wrote it. Dynamic types "
        f"(emitted via `do_log_event`) opt out via "
        f"`_DOCS_DRIFT_EXEMPT_EVENT_TYPES` with a comment."
    )


def test_every_cli_verb_documented():
    """Every non-suppressed `ap2 <verb>` subcommand in `build_parser()`
    is mentioned (by exact `ap2 <verb>` substring) in
    `skills/ap2-board-ops/SKILL.md`'s `## Operator CLI verbs (reference)`
    section. Substring check (not backtick-required) so the verb can appear
    bare-quoted in a row's `verb` cell or in surrounding prose; the point is
    the operator's grep finds a hit when they read `ap2 <verb>` in a
    Mattermost mention or a `--help` string and want a WHY/when-to-use
    companion.

    TB-399 retargeted this gate from `HOWTO_PATH` to `BOARD_OPS_SKILL` when
    the board-ops domain was carved into the `ap2-board-ops` skill — the
    skill is now the canonical home of the operator CLI-verb table, so it is
    the surface a new subparser must keep in sync.

    Hidden / dev-only subparsers (`help=argparse.SUPPRESS`, e.g.
    `ap2 _run`) are excluded by `_collect_cli_verbs` so the gate
    matches the skill section's stated exclusion.
    """
    skill = BOARD_OPS_SKILL.read_text()
    verbs = _collect_cli_verbs()
    assert verbs, "no CLI verbs collected from build_parser() — walk regressed"
    missing = sorted(v for v in verbs if v not in skill)
    assert not missing, (
        f"CLI verb(s) registered in `ap2/cli.py`'s `build_parser()` but "
        f"missing from `skills/ap2-board-ops/SKILL.md`: {missing}. Add a row "
        f"to `## Operator CLI verbs (reference)` describing why an operator "
        f"reaches for the verb (purpose) and what failure mode / related "
        f"verbs it sits alongside (notes). If the new subparser is dev-only "
        f"and the operator shouldn't see it, mark it `help=argparse.SUPPRESS` "
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
    descriptive mention in `skills/ap2-board-ops/SKILL.md`.
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


def test_failure_recovery_domain_carved_to_skill():
    """TB-402 docs-location pin: the failure-recovery domain — the
    `## Failure modes the daemon recovers from` auto-recovery catalogue and
    the `## Operator-question playbook` lookup table — lives in
    `skills/ap2-failure-recovery/SKILL.md`. TB-406 retired the old operator
    manual entirely, so the old no-duplication-in-howto half of this pin is moot
    (there is no manual to duplicate into); what remains is the pin that the
    domain's content is present in its owning skill on exactly one surface.
    """
    skill = FAILURE_RECOVERY_SKILL.read_text()

    # (1) Skill carries agentskills.io frontmatter.
    assert skill.startswith("---"), "SKILL.md must open with a YAML frontmatter fence"
    assert "\nname:" in skill, "SKILL.md must carry a `name:` frontmatter field"
    assert "\ndescription:" in skill, (
        "SKILL.md must carry a `description:` auto-trigger frontmatter field"
    )

    # (2) Both carved sections + a content anchor from each landed in the skill.
    for anchor in (
        "## Failure modes the daemon recovers from",  # auto-recovery catalogue header
        "task_implicit_commit",                       # ...and its content
        "## Operator-question playbook",              # lookup-table header
        "Daemon running?",                            # ...and its content
    ):
        assert anchor in skill, (
            f"failure-recovery skill is missing carved content anchor: {anchor!r}"
        )


def test_ideation_goals_domain_carved_to_skill():
    """TB-403 docs-location pin: the ideation + goal/focus-management domain —
    the `## Authoring goal.md` operator-curated five-section reference and the
    `## Retrospective audit workflow` `ap2 audit` review surface — lives in
    `skills/ap2-ideation-goals/SKILL.md`. TB-406 retired the old operator
    manual entirely, so the old no-duplication-in-howto half of this pin is moot;
    what remains is the pin that the domain's content is present in its owning
    skill on exactly one surface.

    The `## Authoring goal.md` section's structural-anchor + worked-example-
    validator gates were retargeted onto this skill in `ap2/tests/test_docs.py`.
    `ideation.default.md` is deliberately NOT asserted to move — it stays
    canonical for the daemon ideation agent's own briefing-authoring
    conventions, which the skill references but does not carry.
    """
    skill = IDEATION_GOALS_SKILL.read_text()

    # (1) Skill carries agentskills.io frontmatter.
    assert skill.startswith("---"), "SKILL.md must open with a YAML frontmatter fence"
    assert "\nname:" in skill, "SKILL.md must carry a `name:` frontmatter field"
    assert "\ndescription:" in skill, (
        "SKILL.md must carry a `description:` auto-trigger frontmatter field"
    )

    # (2) Both carved sections + a content anchor from each landed in the skill.
    for anchor in (
        "## Authoring goal.md",            # goal-authoring section header
        "### Done when",                   # ...one of its five subsections
        "delete-test",                     # ...the Done-when honesty heuristic
        "## Retrospective audit workflow",  # audit-workflow section header
        "ap2 audit",                       # ...the verb it documents
        "audit_skip",                      # ...its interactive-skip op-shape
    ):
        assert anchor in skill, (
            f"ap2-ideation-goals skill is missing carved content anchor: {anchor!r}"
        )


def test_components_enumeration_carved_to_observability_skill():
    """TB-405 docs-location pin (final domain carve): the
    `## Components enumeration (ap2 status)` domain — the registry-walk prose
    describing the `## Components` block that `ap2 status` renders (text-mode
    layout, the three env-flag polarity conventions, the `<env_flag_desc>`
    rendering rules, and `--json` parity) — lives in
    `skills/ap2-observability/SKILL.md`, the skill that owns the components surface.

    Mirrors the TB-402 / TB-403 `test_*_domain_carved_to_skill` shape: the
    content lives wholesale in its owning skill. This section is prose with no
    `HOWTO_PATH`-keyed coverage gate to retarget (the `ap2 status` CLI verb is
    gated against `BOARD_OPS_SKILL`, the `AP2_*` env flags against
    `CONFIG_SKILL`), so a location pin is the correct shape, not a gate flip.
    TB-406 retired the old operator manual entirely, so the old
    no-duplication-in-howto half is moot. The `## Components` render BEHAVIOR stays pinned by
    `ap2/tests/test_tb379_effective_config_snapshot.py` /
    `ap2/tests/test_tb319_status_components.py` — this is a docs-only move.
    """
    skill = OBSERVABILITY_SKILL.read_text()

    # (1) Skill carries agentskills.io frontmatter.
    assert skill.startswith("---"), "SKILL.md must open with a YAML frontmatter fence"
    assert "\nname:" in skill, "SKILL.md must carry a `name:` frontmatter field"
    assert "\ndescription:" in skill, (
        "SKILL.md must carry a `description:` auto-trigger frontmatter field"
    )

    # (1b) The frontmatter description advertises the `ap2 status` components
    # surface so the skill auto-triggers on a components-monitoring question.
    desc_line = next(
        (ln for ln in skill.splitlines() if ln.startswith("description:")), ""
    )
    assert "ap2 status" in desc_line, (
        "ap2-observability SKILL.md frontmatter `description:` must mention the "
        "`ap2 status` components surface so it auto-triggers on a "
        "components-monitoring question"
    )

    # (2) The carved section + its content anchors landed in the skill.
    for anchor in (
        "default_registry().components",  # the registry-walk reference
        "## Components",                  # the rendered block header
        "env_flag=None",                  # the always-on polarity convention
    ):
        assert anchor in skill, (
            f"ap2-observability skill is missing carved content anchor: {anchor!r}"
        )

    # (2b) The carved section cross-references the sibling skills for the
    # env-flag knob catalogue and the status verb instead of re-listing them.
    assert "ap2-config" in skill, (
        "the carved components section must cross-reference the ap2-config skill "
        "for the `AP2_*` env-flag knob catalogue instead of re-listing it"
    )
    assert "ap2-board-ops" in skill, (
        "the carved components section must cross-reference the ap2-board-ops "
        "skill for the `ap2 status` verb instead of re-listing it"
    )
