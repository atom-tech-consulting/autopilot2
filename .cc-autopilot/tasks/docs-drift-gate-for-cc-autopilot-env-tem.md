# Docs-drift gate for `.cc-autopilot/env` template + exemption set

Tags: #autopilot #docs #ci #regression-pin

## Goal

The `ENV_TEMPLATE` constant in `ap2/init.py` — written verbatim into
fresh projects' `.cc-autopilot/env` — currently lists only 10 of the
~51 `AP2_*` knobs the source consults. The 38 absent knobs cluster
in recent arcs (attention/push, auto-approve, auto-unfreeze, focus
rotation, validator-judge, watchdog, MM tuning) that shipped after
TB-278 authored the template; nothing has held the template current
since. This silently weakens the goal.md "Current focus:
operator-legible reporting and monitoring" pull-surface contract —
an operator can point ap2 at a fresh project, paste a `goal.md`,
and walk away, but cannot discover those knobs from the template
alone. `test_every_env_knob_documented` keeps `ap2/howto.md` in sync,
but no equivalent gate exists for the template.

Add a CI gate parallel to `test_every_env_knob_documented` that
asserts every `AP2_*` knob in source either appears (substring) in
`ENV_TEMPLATE` OR is listed in a new `_TEMPLATE_EXEMPT_KNOBS`
frozenset (declared next to `ENV_TEMPLATE` in `ap2/init.py`) with an
inline `# reason:` comment. Mirrors the `_DOCS_DRIFT_EXEMPT_ENV_KNOBS`
pattern already in `ap2/tests/test_docs_drift.py`. The gate forces
the operator-facing/internal decision at the same PR that adds a
knob, instead of letting drift compound silently.

Why now: TB-303's doc sweep audited `ap2/README.md` / `architecture.md`
/ `howto.md` against the post-2026-05-27 focus-advance + attention +
ideation-toolset arcs, but the env template was outside its scope.
Without this gate the next focus arc will compound the gap (38 knobs
today, likely 45+ in a quarter).

## Scope

- `ap2/init.py` — extend `ENV_TEMPLATE` with three commented entries
  for the operator-tuned knobs the recent arcs surfaced as
  practically-tuned: `AP2_ATTENTION_IMMEDIATE_PUSH`,
  `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`,
  `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`. Add a new
  `_TEMPLATE_EXEMPT_KNOBS: frozenset[str]` constant declaring the
  remaining knobs whose absence from the template is intentional;
  each entry carries a one-line `# reason: ...` comment explaining
  why operators don't need it in the template (debug/test-only,
  default rarely tuned, lifecycle-resource, covered by a sibling
  global, etc.).

- `ap2/tests/test_docs_drift.py` — add
  `test_every_env_knob_in_template_or_exempt` following
  `test_every_env_knob_documented`'s shape: re-use the existing
  `_collect_env_knobs()` helper, then assert each knob is either a
  substring of `ENV_TEMPLATE` (the literal source string, not the
  rendered output) or a member of `_TEMPLATE_EXEMPT_KNOBS` (imported
  from `ap2.init`). The assertion error message tells the failing-PR
  author the two ways to make the gate pass.

## Design

- `_TEMPLATE_EXEMPT_KNOBS` lives in `ap2/init.py` next to
  `ENV_TEMPLATE` (not in the test module) so a future knob-adder
  touches one source file when making the template/exempt decision.
  Mirrors how `HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` live in
  `ap2/env_reload.py` alongside the live reload code, with the
  assertion-on-import that piggybacks on them.

- Substring-matching against the `ENV_TEMPLATE` source string is
  safe — the f-string default-value interpolations are constant
  values (numbers, strings like `"claude-opus-4-7"`), not knob
  names, so a literal `AP2_FOO` substring scan cannot false-positive
  on a default.

- New template entries follow the existing format: a block comment
  explaining what the knob does + a commented-out `# AP2_FOO=<default>`
  line. Reference `DEFAULT_*` constants by f-string interpolation
  where one exists (matching the existing `DEFAULT_TASK_MAX_TURNS`
  style), inline a literal otherwise.

- `# reason:` comments on exempt-set entries should categorize each
  knob by why it's not template-worthy. Suggested taxonomy:
  `# reason: internal default, rarely tuned`, `# reason: debug/test
  only`, `# reason: lifecycle resource (FIXED_KNOBS)`, `# reason:
  covered by global AP2_AGENT_EFFORT`. The comment IS the audit
  trail for future readers asking "should this graduate to the
  template?"

- The new test's error message should name the two escape hatches
  explicitly (one paragraph each): how to add a knob to the
  template, and how to add it to the exempt set with a `# reason:`
  comment. Mirrors the existing `test_every_env_knob_documented`
  error-message verbosity.

## Verification

- `uv run pytest -q ap2/tests/test_docs_drift.py` — full docs-drift
  suite passes, including the new gate.
- `grep -q "^def test_every_env_knob_in_template_or_exempt" ap2/tests/test_docs_drift.py`
  — the new test function exists by name.
- `grep -q "^_TEMPLATE_EXEMPT_KNOBS" ap2/init.py` — the new
  exemption-set constant exists by name.
- `grep -q "AP2_ATTENTION_IMMEDIATE_PUSH" ap2/init.py` — the knob
  is referenced somewhere in `ap2/init.py` (template or exempt set).
- `grep -q "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP" ap2/init.py` —
  similarly referenced.
- `grep -q "AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP" ap2/init.py` —
  similarly referenced.
- `uv run pytest -q ap2/tests/` — full suite still passes (regression
  pin against the rest of the docs-drift gates and init-test surfaces).
- `ap2/init.py` Prose: the three knobs `AP2_ATTENTION_IMMEDIATE_PUSH`,
  `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP`, and
  `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` appear as commented `# AP2_FOO=...`
  entries inside the `ENV_TEMPLATE` string body (not only in
  `_TEMPLATE_EXEMPT_KNOBS`). Judge confirms via Read of `ap2/init.py`.
- `ap2/init.py` Prose: every `AP2_*` knob present in source per
  `_collect_env_knobs()` is either a substring of `ENV_TEMPLATE` or
  a member of `_TEMPLATE_EXEMPT_KNOBS`, and every `_TEMPLATE_EXEMPT_KNOBS`
  entry has a `# reason: ...` comment on the same line or the line
  immediately above. Judge confirms via Read.

## Out of scope

- Reorganizing or grouping the existing 10 template knob entries.
- Promoting any of the exempt knobs to the template body beyond the
  three named in Scope. Future graduations from exempt → template are
  one-line follow-ups; the gate now forces them to be conscious
  decisions.
- Changing `HOT_RELOADABLE_KNOBS` / `FIXED_KNOBS` membership or
  semantics in `ap2/env_reload.py`.
- Surfacing exempt-knob counts on `ap2 status` or `ap2 doctor` (the
  gate's audit surface is the CI test's error message).
- Backporting the gate's history (no need to retroactively justify
  why each existing exempt knob was originally omitted — `# reason:`
  comments are best-effort forward-looking).
