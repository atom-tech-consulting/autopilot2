"""Shared source-of-truth registry walks for the test-suite gates (TB-209).

Three readers consume this module:

1. `ap2/tests/test_docs_drift.py` — docs-axis gate (TB-203 / TB-207):
   "every CLI verb is mentioned in `ap2/howto.md`."
2. `ap2/tests/test_coverage_drift.py` — testing-axis gate (TB-208 / TB-209):
   "every CLI verb has at least one substring reference under `ap2/tests/`."
3. `ap2/howto.md`'s `## Operator CLI verbs (reference)` section (TB-207) —
   the table that the docs-axis gate matches against. Not an importer,
   but the third structural reader of the same walk semantics: the table
   was authored against this enumeration, and `## Operator CLI verbs
   (reference)` carries the same `argparse.SUPPRESS` exclusion language
   so the table and the walk agree on what counts as a verb.

The 3rd-call-site threshold flipped goal.md L74-77's threshold-three rule
("when a piece of logic appears at three or more call sites with
structural similarity, extract to a shared helper") from "premature
abstraction" to "structurally appropriate extraction."

## Future extractions

Other walks currently inlined in BOTH `test_docs_drift.py` AND
`test_coverage_drift.py` — `_collect_env_knobs`, `_collect_event_types`,
`_all_agent_mcp_tool_short_names` — are 2-call-site today (docs gate +
coverage gate). Threshold-three is not yet met for those; extracting
them now would re-trip goal.md L74-77's "premature abstraction is its
own failure mode" guardrail. Extract them here when a third reader
appears (e.g. an architecture.md-side test-presence branch, an
operator-CLI surface audit, or any other structural use of the same
walk).
"""
from __future__ import annotations

import argparse

from ap2.cli import build_parser


def _collect_cli_verbs() -> set[str]:
    """Walk `build_parser()`'s subparser tree and return every
    non-suppressed leaf verb as `"ap2 <verb>"` (top-level) or
    `"ap2 <group> <sub>"` (nested under `cron` / `sandbox`).

    Argparse marks a subparser as hidden by setting its `help` to
    `argparse.SUPPRESS` (rendered as the literal `"==SUPPRESS=="`); those
    entries are NOT operator-facing (e.g. `ap2 _run`, the backgrounded
    daemon entrypoint forked by `cmd_start`), so they're dropped here.
    The howto-side wording in `## Operator CLI verbs (reference)` mirrors
    the same exclusion explicitly so the table and the gate agree on
    what counts as a verb.

    Group nodes (`ap2 cron`, `ap2 sandbox`) themselves are skipped — the
    operator-facing leaves are the nested sub-verbs, and the howto table
    documents one row per leaf rather than a redundant row for the
    group root.
    """
    verbs: set[str] = set()

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            # `_choices_actions` carries the help string per visible subparser;
            # entries with `help=argparse.SUPPRESS` show `'==SUPPRESS=='` here.
            help_by_name = {ca.dest: ca.help for ca in action._choices_actions}
            for name, sub in action.choices.items():
                help_str = help_by_name.get(name)
                if help_str == argparse.SUPPRESS:
                    continue
                full = f"{prefix} {name}".strip()
                has_nested = any(
                    isinstance(a, argparse._SubParsersAction)
                    for a in sub._actions
                )
                if has_nested:
                    # Group node — recurse to leaves; don't emit the group itself.
                    walk(sub, full)
                else:
                    verbs.add(full)

    walk(build_parser(), "ap2")
    return verbs
