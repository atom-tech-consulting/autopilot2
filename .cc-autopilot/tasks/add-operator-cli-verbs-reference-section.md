# TB-207 — Add `## Operator CLI verbs (reference)` section to `ap2/howto.md`; gate with docs-drift test against the live CLI parser

Tags: `#autopilot` `#docs` `#code-quality` `#operator-surface` `#regression-pin`

## Goal

Close the remaining operator-surface documentation gap on the
**current focus: code quality** focus's (2) **Operator-facing
documentation** axis (goal.md L65-72): TB-203 landed howto.md
reference tables for MCP tools, env knobs, and event types — each
gated by `ap2/tests/test_docs_drift.py` so a new source-surface
addition CAN'T ship without a docs mention. The symmetric CLI-verb
surface is the last operator-touchable surface without that treatment.
Today an operator asking "what does `ap2 classify TB-N --delete-test
advanced-goal` actually record?" or "what's the difference between
`ap2 reject` and `ap2 delete`?" has no single landing place in
howto.md — the 24+ `ap2 <verb>` subcommands are scattered as
inline mentions across the prose (only 5 verbs appear verbatim in
howto.md per `grep -nE 'ap2 (approve|reject|classify|ack|ideate|
update-goal|backfill-proposals|delete|unfreeze|update|add)'`).
Operators fall back to `ap2 <verb> --help` or to reading `ap2/cli.py`
— exactly the goal.md L65-72 failure mode the focus names.

Why now: the docs-drift gate primitive already exists post-TB-203
(`ap2/tests/test_docs_drift.py::test_every_env_knob_documented`,
`test_every_mcp_tool_documented`, `test_every_event_type_documented`
each enumerate the source-of-truth registry and assert each entry
appears in `ap2/howto.md`); adding a `test_every_cli_verb_documented`
gate is a same-shape extension that costs ~20 LOC and closes the
last operator-surface anti-drift hole symmetric to the three TB-203
already pinned. Without this, the next CLI-verb addition (the operator
log shows 6 new verbs landed since 2026-05-04: `approve`, `reject`,
`classify`, `ack`, `ideate`, `update-goal`, `backfill-proposals`,
`update`) silently ships without a documentation entry, and the
inconsistency between "MCP tools / env knobs / events have a
reference table + drift gate" and "CLI verbs don't" itself becomes
a code-quality smell. The four-axis focus's "delete-test" check
(goal.md L92-97 "would the codebase get noticeably less confidently
modifiable if this work didn't ship?") passes: an operator
encountering a Mattermost mention of `ap2 classify` today has to
open `cli.py` to learn the verdict values — that's the exact
"can't understand a surface from its documented description and
has to read source" failure mode L70 names.

## Scope

(1) Add `## Operator CLI verbs (reference)` section to
`ap2/howto.md`, placed AFTER the existing `## Custom MCP tools
(reference)` and BEFORE `## Event schema (the canonical timeline)`
so the three operator-surface reference sections sit together. The
section opens with a one-paragraph framing that names the surface
("Subcommands of `ap2` invoked by the operator from the host
shell — distinct from MCP tools (agent-internal) and chat verbs
(`@claude-bot <verb>`, which route through the operator queue)").

(2) Table-of-verbs structure: one row per subcommand, three columns
— `verb` (e.g. `ap2 classify TB-N --delete-test <verdict> [--reason
TEXT]`), `purpose` (one sentence on WHY the operator reaches for
it — not what it does internally), `notes` (the failure mode it
addresses, the related verbs, or the WHY behind a non-obvious
constraint — e.g. for `classify`: "captures the operator's
retrospective delete-test verdict for ideation signal; reasons feed
TB-189 per-proposal records"). Subcommand groups (`ap2 cron`,
`ap2 sandbox`) get one row per sub-verb. Hidden / dev-only verbs
(argparse.SUPPRESS in `cli.py`, e.g. `ap2 _run`) are deliberately
excluded; explicitly note this in the section's opening paragraph
so the gate test can mirror the exclusion.

(3) Add `ap2/tests/test_docs_drift.py::test_every_cli_verb_documented`
mirroring the shape of `test_every_mcp_tool_documented`:
  - Iterate the argparse parser tree from `ap2/cli.py`'s
    `build_parser()` (or equivalent factory). Collect every
    non-suppressed subcommand name (including `<group> <sub>`
    pairs like `cron list`, `sandbox project-setup`).
  - For each verb, assert it appears as a substring in
    `ap2/howto.md` (case-sensitive). Fail with a diff-shaped
    error message listing missing verbs.
  - Mark the source-of-truth list as an authoritative gate: when
    a new subcommand lands without a docs entry, CI fails.

(4) Don't touch `ap2 <verb> --help` strings; help text is the
short-form reference, the howto section is the WHY/when-to-use
companion. Don't auto-generate the table from argparse — the WHY
columns require human judgment (paraphrased docs are the L70-72
failure mode in the opposite direction).

(5) Don't add the section to `ap2/architecture.md` (it's an
operator surface, not an internals topic). Don't introduce a new
howto.md heading depth (the existing `## H2 + ### H3` pattern
covers it).

## Design

The table-of-verbs format mirrors howto.md's existing
"Operator-question playbook" table (L468-487) — same pipe-table
shape, same column-count discipline — so the new section reads as
a natural continuation of the operator-facing reference pattern.
Each row's `purpose` column is one sentence; `notes` is one or
two sentences max so the table stays scannable and doesn't drift
into multi-paragraph docstrings (the L83 anti-pattern).

Verbs to cover (full enumeration from `ap2/cli.py`'s parser tree,
excluding `_run`): `start`, `stop`, `status`, `init`, `doctor`,
`logs`, `backlog`, `add`, `update`, `delete`, `reject`, `classify`,
`ack`, `approve`, `unfreeze`, `ideate`, `update-goal`,
`backfill-proposals`, `pause`, `resume`, `cron list`,
`sandbox user-audit`, `sandbox user-setup`, `sandbox project-setup`,
`sandbox project-audit`, `check`, `web`, `version`. Cross-check
the enumeration against the live parser at test-write time —
hand-counting from cli.py grep results misses argparse subparsers
nested in a sub-group.

The drift-gate test reuses TB-203's parser-walk shape (the existing
mcp/env/event tests share a "introspect source registry → assert
each entry in howto.md" pattern). Adding a fourth instance of the
same shape doesn't trip the goal.md L74-78 reusability threshold
(three+ call sites with structural similarity → extract) ONLY
because the prior three were already inlined as separate tests.
If at this fourth instance the structural similarity becomes
painful, a separate follow-up can factor a `_assert_registry_in_howto`
helper — but that's out of scope here (don't pre-emptively refactor
existing TB-203 tests).

Subcommand-group flattening: `ap2 cron list` and `ap2 sandbox
user-audit` (etc.) are nested under group subparsers in argparse.
The walk must recurse into `_SubParsersAction` to enumerate both
top-level and nested verbs. Test that both `ap2 init` (top-level)
and `ap2 sandbox project-setup` (nested) get caught by the gate.

Sequencing: the docs section and the test land in the same commit
so there's no intermediate state where the gate fails on
freshly-introduced rows. The docs body is the human-authored
content; the test is the anti-drift guard for future additions.

## Verification

- `uv run pytest -q ap2/tests/test_docs_drift.py` — all tests pass
  (exit 0), including the new `test_every_cli_verb_documented`.
- `uv run pytest -q ap2/tests/` — full regression suite green
  (exit 0); no test should change behavior in unrelated modules.
- `grep -nE "^## Operator CLI verbs \(reference\)" ap2/howto.md`
  — exit 0 (the new section heading exists).
- `[ "$(grep -cE '^\| \`ap2 [a-z][a-z-]*' ap2/howto.md)" -ge 20 ]`
  — at least 20 verb rows in the new table (the live parser has
  24+ non-suppressed subcommands; ≥20 is a safe lower bound).
- `grep -nE "def test_every_cli_verb_documented" ap2/tests/test_docs_drift.py`
  — exit 0 (the new test function exists by that exact name, for
  greppability symmetric to the other three TB-203 gate tests).
- `grep -nE "ap2 (approve|reject|classify|ack|ideate|update-goal|backfill-proposals|update)" ap2/howto.md | wc -l | awk '{ exit ($1 < 8) }'`
  — at least 8 occurrences of these eight recently-added verbs
  (one per row at minimum), up from today's grep-counted ~5 scattered
  prose mentions.
- Prose: the section's opening paragraph names the distinction
  between CLI verbs (operator-shell), MCP tools (agent-internal),
  and chat verbs (`@claude-bot <verb>` → operator queue) — judge
  confirms via `Read` of the new section.
- Prose: each row's `purpose` column is one sentence (not a
  multi-paragraph docstring), and `notes` is at most two sentences
  — judge confirms by reading 5 randomly-selected rows.

## Out of scope

- Auto-generating the table body from argparse `--help` strings
  (paraphrased-source-without-WHY is goal.md L70-72's failure
  mode in the opposite direction).
- Refactoring the three existing TB-203 drift tests to share an
  `_assert_registry_in_howto` helper (premature — wait for a fifth
  registry-type addition or for the existing three to grow
  per-test complexity that pays for extraction).
- Documenting hidden / dev-only subcommands (`ap2 _run`); the gate
  test deliberately excludes them via the SUPPRESS marker.
- Adding the same table to `ap2/architecture.md` (architecture.md
  is for internals; howto.md is the operator surface).
- Splitting howto.md into multiple files / migrating to mkdocs
  / adding a docs site (orthogonal; howto.md as a single-file
  reference is the current intentional shape).
- Re-documenting MCP tool / env knob / event type entries (TB-203
  already landed those tables).
## Attempts

### 2026-05-13 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `[ "$(grep -cE '^\| \`ap2 [a-z][a-z-]*' ap2/howto.md)" -ge 20 ]`— at least 20 verb rows in the new table (the live parse
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260513T011559Z-TB-207.prompt.md`, `stream: .cc-autopilot/debug/20260513T011559Z-TB-207.stream.jsonl`, `messages: .cc-autopilot/debug/20260513T011559Z-TB-207.messages.jsonl`
