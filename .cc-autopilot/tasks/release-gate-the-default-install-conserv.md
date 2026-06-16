## Goal

Promote ap2's conservative-by-default posture and its install extras to release
gates under goal.md's **Current focus: cut a public source-available
distribution** (axis 2, "Default-config posture + extras"). Add a test pinning the
default merged config to "loop whole, every operator-bypassing behavior
off/inert" AND that an all-components-disabled config still loads, and confirm a
fresh `ap2 init` writes that conservative default; separately, pin that the
install extras are sound for an outside user. Serves the Progress signal "A fresh
`ap2 init` keeps the loop whole with every operator-bypassing behavior off/inert
... the default and all-disabled configs both pass the suite."

Why now: the conservative posture is already the schema default but nothing pins
it as a release gate — without a gate, a future config change could silently ship
a public install that acts unattended on the operator's behalf out of the box,
the axis-2 delete-test failure.

## Scope
- Add `ap2/tests/test_default_posture.py` asserting, against the default merged
  config (no env overrides): `auto_approve` is disabled, `attention.immediate_push`
  is off, no communication channel is configured, and `auto_unfreeze` has no
  `fix_shapes` — i.e. every operator-bypassing behavior is off/inert while the
  loop stays whole. The same test asserts an all-components-disabled config (via
  the existing enumerate-disabled-env-flags helper used by the minimal-kernel
  e2e) still loads without error.
- In the same test file, assert a fresh `ap2 init` writes a config whose resolved
  posture matches the conservative default above.
- Extend the hermetic packaging gate (`ap2/tests/test_packaging.py`) to assert the
  `dev` extra is declared (alongside the existing `codex` pin) and to record the
  `[mattermost]` decision: add a `[mattermost]` extra ONLY if the
  communication/Mattermost path pulls a dependency beyond the base set; if it does
  not (the base deps carry no Mattermost-specific package), record that as a
  comment/assertion rather than adding an empty extra.

## Design
- Reuse the existing default-config load path and the all-disabled enumeration
  helper; do not re-implement config merging.
- This pins the posture that is ALREADY the schema default — it asserts and
  documents, it does not disable or change any whole component or default value.
- Keep packaging assertions hermetic (parse pyproject; no network resolution).

## Verification
- `uv run --extra dev pytest -q ap2/tests/test_default_posture.py` — the default-posture + all-disabled-load + fresh-init gate passes.
- `uv run --extra dev pytest -q ap2/tests/test_packaging.py` — the extended extras gate (codex + dev declared, mattermost decision recorded) passes.
- `ap2/tests/test_default_posture.py` Prose: the test asserts the default merged config leaves auto_approve disabled, attention.immediate_push off, no channel configured, and no auto_unfreeze fix_shapes, AND that an all-components-disabled config loads; judge confirms via Read.

## Out of scope
- Changing any default value (the posture is already the schema default; this task asserts it).
- A live `uv sync --extra` network resolution smoke (operator/CI).
- Adding behavior or new components.