"""Minimal TOML writer for `.cc-autopilot/config.toml` (TB-324, axis 4).

The structured-config focus's config.toml shape is intentionally
narrow — `[core.*]` for non-component tunables and `[components.<name>]`
for component-owned knobs (goal.md L307-310), each table holding
primitive values (bool / int / str / float). That narrow shape fits a
~50-line in-tree writer rather than pulling in `tomli_w` as a new
runtime dep just for the `ap2 config set` write path. Reads continue
to go through the stdlib `tomllib` parser via `config_loader.parse_toml`.

Why not a generic third-party writer:
  - Single new dep adds packaging surface for every operator install
    when we already pay for `tomllib` (Python 3.11+ stdlib).
  - The writer's input domain is restricted to the two table prefixes
    above and primitive scalars — a generic library's full TOML feature
    set (arrays-of-tables, inline tables, multi-line strings) is dead
    code here.
  - Round-trip fidelity (preserving operator-authored comments, key
    ordering, etc.) is explicitly NOT a goal — `ap2 config set` is the
    canonical mutation surface, and the daemon re-renders the full file
    on every write. The format is deterministic + alphabetically sorted
    so a `git diff` after a set call shows exactly the one-line change.

Used by:
  - `operator_queue._apply_operator_op` for the `config_set` drain
    branch (the write path).
"""
from __future__ import annotations

from typing import Any


def set_path_in_doc(doc: dict, parts: list[str], value: Any) -> None:
    """Set `doc[parts[0]][parts[1]]...[parts[-1]] = value` in place.

    Used by the `config_set` drain to overlay the operator's requested
    `<path>=<value>` onto the current TOML dict. Creates intermediate
    tables on the fly so a write to a never-before-set component
    sub-table (`components.foo.bar`) populates the path without an
    explicit pre-create step.

    `parts` must be `[core, <field>]` or `[components, <name>, <key>]`
    — the only two shapes the structured-config contract recognizes;
    other prefixes are rejected as a defensive check (an unknown
    prefix would land in the dict but never round-trip through the
    writer's section emitter below).
    """
    if not parts:
        raise ValueError("empty path")
    if parts[0] not in ("core", "components"):
        raise ValueError(
            f"unknown section prefix {parts[0]!r}; expected core or components"
        )
    if parts[0] == "core":
        if len(parts) != 2:
            raise ValueError(
                f"core path must be 2 parts (core.<field>); got {parts}"
            )
        core = doc.setdefault("core", {})
        if not isinstance(core, dict):
            raise ValueError("[core] is present but not a table")
        core[parts[1]] = value
        return
    # components.<name>.<key>
    if len(parts) != 3:
        raise ValueError(
            f"components path must be 3 parts "
            f"(components.<name>.<key>); got {parts}"
        )
    components = doc.setdefault("components", {})
    if not isinstance(components, dict):
        raise ValueError("[components] is present but not a table")
    section = components.setdefault(parts[1], {})
    if not isinstance(section, dict):
        raise ValueError(f"[components.{parts[1]}] is present but not a table")
    section[parts[2]] = value


def dump_config_toml(doc: dict) -> str:
    """Render `doc` as TOML text for `.cc-autopilot/config.toml`.

    Output layout:
      - `[core]` table first (when present), keys alphabetically sorted.
      - `[components.<name>]` tables next, name-sorted; within each,
        keys alphabetically sorted.
      - Any other top-level keys are dropped silently (the structured-
        config shape only recognizes those two prefixes — see the
        writer module's docstring).

    Determinism is load-bearing for the `git diff` of `ap2 config set`
    to surface exactly the operator's one-line change. Alphabetical
    sort within each table is the cheapest deterministic ordering.
    """
    lines: list[str] = []
    core = doc.get("core")
    if isinstance(core, dict) and core:
        lines.append("[core]")
        for key in sorted(core):
            lines.append(f"{key} = {_render_value(core[key])}")
        lines.append("")
    components = doc.get("components")
    if isinstance(components, dict) and components:
        for comp_name in sorted(components):
            section = components[comp_name]
            if not isinstance(section, dict) or not section:
                continue
            lines.append(f"[components.{comp_name}]")
            for key in sorted(section):
                lines.append(f"{key} = {_render_value(section[key])}")
            lines.append("")
    # Trailing blank-line trim so the file ends with exactly one
    # newline (matches the existing operator-authored convention).
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _render_value(value: Any) -> str:
    """TOML-shaped repr of a primitive.

    Bool uses `true` / `false` (lowercase, TOML's vocabulary, not
    Python's). String escapes the four characters TOML requires
    (`\\` `\"` `\n` `\r`) — anything else round-trips verbatim inside
    `"..."`. Int / float fall through to `repr`-via-`str` since
    Python's stringification matches TOML for plain decimal forms.

    Lists / dicts / None are NOT supported — the structured-config
    schema only ships primitives at the leaf, and the writer raises
    `TypeError` on anything else so a future axis adding a list-shape
    knob has to come back here and pick a TOML representation
    explicitly.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
                 .replace("\"", "\\\"")
                 .replace("\n", "\\n")
                 .replace("\r", "\\r")
        )
        return f"\"{escaped}\""
    raise TypeError(
        f"unsupported TOML leaf type {type(value).__name__}: "
        f"{value!r}. Structured-config keys are limited to bool / int "
        f"/ str / float (TB-321 schema declarations)."
    )
