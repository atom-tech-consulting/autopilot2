## Goal

Current focus: structured config (env → TOML). Close goal.md L401-403
progress signal "The set of true 12-factor env-only knobs (secrets,
deployment identity) is documented in a single comment block in
`ap2/config_compat.py` and is clearly minimal" by gating the cut-line
with a CI test, the same shape TB-305 used for env-knob docs drift
and TB-325 used for config-key docs drift. The comment block at
`config_compat.py` L193-212 exists (Mattermost identity,
`AP2_MM_TEAM_ID`, `AP2_MM_REPORT_CHANNEL`, `AP2_MM_MENTION`, OAuth /
sandbox identity, etc.) but nothing fails CI when a new
`os.environ.get("AP2_*")` is added outside that set + outside the
bootstrap path. Without the gate, the cleanness signal silently
degrades on every PR that adds a new env read.

Why now: TB-326..TB-335 + TB-336 (in-flight) exercised the migration
walk across 6 components + 3 core clusters; the residual direct env
reads are now substantially just the documented exempts + the
bootstrap path. The cut-line is mature enough to pin — pinning it
later, once unmigrated AP2 reads have accumulated, requires either a
ratchet-up of the exempt list (defeats "clearly minimal") or a
migration-debt cleanup before the gate can land. Cheap now, expensive
later. Same delete-test as TB-305 / TB-325: without the gate, the
guarantee in the progress signal is aspirational, not enforced.

## Scope

- New test `ap2/tests/test_tb338_env_only_cut_line.py` containing two
  assertions:
  1. **Exempt-list / migrated-list disjointness**: walks
     `ap2/config_compat.py`'s `FLAT_TO_SECTIONED` keys and
     `_KNOBS_STAYING_ENV_ONLY` set; asserts their intersection is
     empty. A knob can't be both migrated (mapped) and exempt
     (staying env-only). Failure message: "knob `AP2_X` is in BOTH
     FLAT_TO_SECTIONED and _KNOBS_STAYING_ENV_ONLY — pick one".
  2. **Source-level env-read cut-line**: walks every `.py` under
     `ap2/` (excluding `ap2/tests/` and `ap2/__pycache__/`), greps
     for `os\.environ\.get\(.AP2_[A-Z_]+` matches, and asserts each
     matched knob is one of:
     - in `_KNOBS_STAYING_ENV_ONLY` (the documented exempt set), or
     - declared in a small bootstrap allowlist (initially
       `{"ap2/config.py", "ap2/env_reload.py"}` since both
       CONSTRUCT cfg). The allowlist lives in the test module
       itself with a comment block explaining the cut-line.
     Failure message names the offending file + knob and points
     at the exempt list / bootstrap allowlist: "file `ap2/foo.py`
     reads `AP2_BAR` directly; either migrate it via
     `cfg.get_core_value` / `cfg.get_component_value`, add it to
     `_KNOBS_STAYING_ENV_ONLY` with a one-line justification in
     the comment block, or add the file to the bootstrap
     allowlist".
- Extend the comment block at `ap2/config_compat.py` L193-212 to:
  - add a top-line cross-reference pointing to the new test
    (`test_tb338_env_only_cut_line.py`) as the enforcement mechanism;
  - ensure every entry in `_KNOBS_STAYING_ENV_ONLY` has its
    one-line justification preserved (audit pass; reorganize the
    comment block by category — `## Mattermost identity`,
    `## Sandbox / OAuth`, `## Deployment` — if it isn't already
    grouped; no semantic changes).
- Add the docs-drift gate to the existing CI surface: register
  the new test in `ap2/tests/test_docs_drift.py`'s imports if the
  project pattern wires drift tests there, or leave it as a
  standalone test module (mirror whichever pattern TB-325's
  `test_every_config_key_documented` uses).
- Add a one-line summary entry to howto.md `## Configuration knobs`
  (L1424) or `## Config keys (TOML)` (L2358) noting that
  `_KNOBS_STAYING_ENV_ONLY` is enforced by CI gate.

## Design

The walker uses the stdlib `ast` module (not regex) to find
`os.environ.get(...)` Call nodes whose first arg is a `Constant`
matching `r"^AP2_[A-Z_]+$"`. AST-based parsing avoids false positives
on commented-out lines and docstring mentions (e.g.
config_loader.py L33's `os.environ.get("AP2_*")` docstring shouldn't
fail the gate). For each match, the walker records `(file_path,
knob_name)` and post-validates against the exempt set + bootstrap
allowlist.

The bootstrap allowlist (`ap2/config.py`, `ap2/env_reload.py`)
captures the genuine "this code BUILDS cfg, so it can't read from
cfg" constraint. Goal.md L401-403's "clearly minimal" framing applies
to `_KNOBS_STAYING_ENV_ONLY`; the bootstrap allowlist is a separate
structural carve-out (per-file, not per-knob) and stays small by
design — adding a third file should require explicit code review.

Backstop: when the test fails, the error message is the docs (says
exactly what to do); when it passes, the cut-line guarantee in the
progress signal is enforced on every PR, not aspirational.

## Verification

- `uv run pytest -q` — full suite passes.
- `uv run pytest -q ap2/tests/test_tb338_env_only_cut_line.py` — new cut-line gate passes against current HEAD (asserts the existing _KNOBS_STAYING_ENV_ONLY + bootstrap allowlist covers every remaining direct AP2 env read).
- `test -f ap2/tests/test_tb338_env_only_cut_line.py` — new test module exists.
- `grep -cE "def test_" ap2/tests/test_tb338_env_only_cut_line.py` — test module declares ≥2 test functions (disjointness + source cut-line).
- `grep -nE "_KNOBS_STAYING_ENV_ONLY" ap2/tests/test_tb338_env_only_cut_line.py` — test imports / references the exempt set under audit.
- `grep -nE "test_tb338_env_only_cut_line" ap2/config_compat.py` — comment block cross-references the new enforcement test by name.
- `ap2/tests/test_tb338_env_only_cut_line.py` Prose: the source-walk asserts each AP2 env read is in `_KNOBS_STAYING_ENV_ONLY` or in the bootstrap allowlist (`{"ap2/config.py", "ap2/env_reload.py"}`); judge confirms via Read.
- `ap2/config_compat.py` Prose: the comment block immediately preceding `_KNOBS_STAYING_ENV_ONLY` references the new enforcement test by file name; judge confirms via Read.

## Out of scope

- Migrating any new call sites (TB-336 handles the remaining tail).
- Renaming or restructuring `_KNOBS_STAYING_ENV_ONLY` entries (audit-only; no semantic changes).
- Adding new components or schema work (orthogonal to TB-337).
- Expanding the bootstrap allowlist beyond `ap2/config.py` and `ap2/env_reload.py` (additions require operator review on a case-by-case basis).
