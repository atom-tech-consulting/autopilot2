## Goal

Scrub sandbox-identity leaks from shipped source and make the packaging identity
metadata coherent for an outside consumer — the identity half of axis 1 under
goal.md's **Current focus: cut a public source-available distribution**. Remove
the named absolute-path leak in `ap2/json_extract.py` (a
`/Users/claude-agent/repos/post-train/...` reference in a module comment), sweep
the rest of shipped source for any other sandbox-identity string baked in as a
NON-overridable default, and confirm pyproject's `authors` + `[project.urls]`
read as coherent placeholders an outside user can adopt. Serves the Progress
signal "A clean checkout installs ... with no sandbox-specific paths or identity
baked into source."

Why now: a fresh install from a clean checkout currently leaks the sandbox's
local path (`ap2/json_extract.py:22`), which is exactly the focus delete-test's
"leaks the sandbox's local paths/identity" failure — an outside reader sees a
private filesystem path baked into the package.

## Scope
- Edit `ap2/json_extract.py` so the `/Users/claude-agent/repos/post-train/...`
  absolute path in the module comment is removed or replaced with a
  sandbox-neutral, generic example (the comment's explanatory intent may stay;
  only the private absolute path goes).
- Sweep shipped (non-test) `ap2/*.py` source for other sandbox-identity strings
  baked as NON-overridable defaults. Documented overridable defaults are
  acceptable and must NOT be churned — e.g. `DEFAULT_USER = "claude-agent"` /
  `AP2_SANDBOX_USER` in `ap2/sandbox.py` is an overridable knob, not a leak.
- Confirm `pyproject.toml [project].authors` and `[project.urls].Repository` read
  as coherent placeholders for an outside consumer (author entry present; a
  clearly-replaceable repo URL the operator can later set to the real value). Fix
  only if incoherent; a placeholder URL is acceptable.

## Design
- Distinguish a leak (a private absolute path / identity with no override path)
  from a documented overridable default (a knob with an `AP2_*` env or CLI
  override). Only the former is in scope to change.
- Do not touch LICENSE, pyproject `license`/`classifiers` (separate license
  task), or README.

## Verification
- `! grep -n "/Users/claude-agent/repos/post-train" ap2/json_extract.py` — the named absolute-path leak is gone from json_extract.py.
- `uv run --extra dev pytest -q ap2/tests/test_json_extract_util.py` — the json_extract tests stay green after the scrub.
- No sandbox-identity absolute path (e.g. a `/Users/<name>/repos/...` path) survives as a baked-in non-overridable default in shipped `ap2/*.py` source; documented overridable defaults (`DEFAULT_USER` / `AP2_SANDBOX_USER` in `ap2/sandbox.py`) remain untouched — judge confirms via Grep.
- pyproject `[project].authors` and `[project.urls].Repository` are coherent for an outside consumer (author entry plus a clearly-replaceable placeholder repo URL) — judge confirms via Read.

## Out of scope
- LICENSE text and pyproject `license`/`classifiers` (separate license task).
- README wording (separate README-accuracy task).
- Genericizing test-fixture data unless it is a shipped non-overridable default.
- Setting the real public repo URL / author identity (operator-only).