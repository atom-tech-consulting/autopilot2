## Goal

Wire ap2's license for a public **source-available** distribution, the license
half of axis 1 under goal.md's **Current focus: cut a public source-available
distribution**. Replace the proprietary "All Rights Reserved" LICENSE with the
verbatim PolyForm Noncommercial License 1.0.0 text, and make `pyproject.toml`
declare it — the `[project].license` field plus trove classifiers reflecting a
noncommercial, source-available license with NO `License :: OSI Approved` claim.
Directly serves the Progress signal "The LICENSE is the verbatim PolyForm
Noncommercial 1.0.0 text and pyproject declares it (no 'All Rights Reserved', no
OSI-open-source claim)."

Why now: a clean outside checkout that still declares "All Rights Reserved"
(LICENSE L1-3, pyproject L7) cannot be published under the chosen noncommercial
license — this is the focus delete-test's first failure mode and blocks every
downstream packaging step.

## Scope
- Replace `LICENSE` contents with the verbatim, unmodified PolyForm Noncommercial
  License 1.0.0 text (canonical text from
  polyformproject.org/licenses/noncommercial/1.0.0). A licensor copyright line
  (e.g. "Copyright (c) 2026 Li Zhang") may be retained above the license body,
  but the proprietary "All rights reserved / proprietary" notice must be gone.
- In `pyproject.toml [project]`, set `license` to declare PolyForm Noncommercial
  1.0.0 (via `text = "PolyForm Noncommercial License 1.0.0"` or `file = "LICENSE"`),
  replacing `{ text = "All Rights Reserved" }`.
- Add a `[project].classifiers` list appropriate to the project (Python version,
  intended audience) including a NON-OSI license classifier (e.g.
  `License :: Other/Proprietary License`). Do NOT add any
  `License :: OSI Approved ::` classifier.

## Design
- This task owns only LICENSE + pyproject `[project].license`/`classifiers`.
  README License-section wording and pyproject author/URL coherence are separate
  tasks; do not edit README or the authors/urls tables here.
- Keep the PolyForm text byte-verbatim (license validity depends on it); only the
  surrounding repo-local copyright line is editable.

## Verification
- `grep -q "PolyForm Noncommercial License 1.0.0" LICENSE` — the canonical license title line is present in LICENSE.
- `! grep -qi "all rights reserved" LICENSE` — the proprietary "All Rights Reserved" notice is gone from LICENSE.
- `! grep -qi "all rights reserved" pyproject.toml` — the proprietary license string is gone from pyproject.
- `! grep -q "License :: OSI Approved" pyproject.toml` — no OSI-open-source classifier is claimed.
- `grep -q "classifiers" pyproject.toml` — a classifiers list is declared.
- `pyproject.toml` Prose: the `[project].license` field declares the PolyForm Noncommercial 1.0.0 license (via `text` naming it or `file = "LICENSE"`) and the classifiers include a non-OSI license classifier; judge confirms via Read.

## Out of scope
- README License section wording (separate README-accuracy task).
- pyproject `authors` / `[project.urls]` coherence and the source path scrub (separate identity-scrub task).
- The operator-only public push and the real repo URL.