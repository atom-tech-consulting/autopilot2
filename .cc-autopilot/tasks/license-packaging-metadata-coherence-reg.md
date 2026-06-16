# License + packaging-metadata coherence regression gate (PolyForm verbatim, no OSI claim, README accuracy)

Tags: #autopilot #distribution #packaging #license #regression-pin #tests

## Goal

Pin the license posture of the **Current focus: cut a public source-available
distribution** so it cannot silently regress. The repo already carries the correct
license metadata on disk today: `LICENSE` is the verbatim PolyForm Noncommercial
1.0.0 text, pyproject `[project].license` declares it with no `License :: OSI
Approved` classifier, and the README License section names PolyForm. What is missing
is any test locking that state. goal.md's second Progress signal requires "the
LICENSE is the verbatim PolyForm Noncommercial 1.0.0 text and pyproject declares it
(no 'All Rights Reserved', no OSI-open-source claim)", and the focus is explicitly
"source-available and noncommercial ... NOT OSI open source ... no `License :: OSI
Approved` classifier is claimed". This task adds one regression module over the
already-committed artifacts so a later edit cannot quietly contradict that decision.

Why now: the license metadata is correct on disk but has zero test coverage — the
delete-test condition "if the repo still declares 'All Rights Reserved'" holds only
by manual inspection, so a single careless metadata edit could ship a public cut
that reintroduces "All Rights Reserved", drops the PolyForm text, or adds an OSI
classifier with nothing failing.

## Scope

- Add a regression gate `ap2/tests/test_license_metadata.py` (text / `tomllib`
  parse, no network, no build) asserting all four invariants against the current
  on-disk artifacts:
  1. `LICENSE` contains the verbatim PolyForm Noncommercial 1.0.0 title text (and
     does NOT contain "All Rights Reserved").
  2. pyproject `[project].license` references PolyForm Noncommercial (not "All
     Rights Reserved").
  3. pyproject `[project].classifiers` contains NO `License :: OSI Approved`
     classifier (source-available, not OSI open source).
  4. `README.md`'s License section names PolyForm Noncommercial (not "All rights
     reserved").

## Design

- The gate pins ONE coherent invariant set — "shipped license metadata matches the
  operator's PolyForm-Noncommercial-not-OSI decision" — read directly from goal.md's
  Progress signal 2 and the focus's no-OSI-claim sentence. It is NOT a speculative
  linter; it checks fixed, already-committed values.
- Parse pyproject with `tomllib` (stdlib) and read `LICENSE` / `README.md` as text;
  keep the LICENSE title-match tolerant of whitespace but specific enough that "All
  Rights Reserved" or an OSI classifier trips it.
- Anchor each assertion message to the goal.md Progress signal so a future failure
  explains why the invariant exists.

## Verification

- `grep -q "PolyForm Noncommercial License 1.0.0" LICENSE` — the LICENSE file declares the PolyForm Noncommercial 1.0.0 title.
- `! grep -rin "all rights reserved" LICENSE pyproject.toml README.md` — no shipped license/metadata/doc carries the proprietary "All Rights Reserved" string (passes iff absent, case-insensitive).
- `! grep -n "License :: OSI Approved" pyproject.toml` — pyproject claims no OSI-approved license classifier (passes iff absent).
- `grep -q "PolyForm" README.md` — the README references PolyForm Noncommercial in its License section.
- `uv run --extra dev pytest -q ap2/tests/test_license_metadata.py` — the new license/metadata regression gate passes.
- `ap2/tests/test_license_metadata.py` Prose: the gate asserts LICENSE carries the verbatim PolyForm Noncommercial 1.0.0 title (and no "All Rights Reserved"), pyproject `[project].license` references PolyForm with no `License :: OSI Approved` classifier, and the README License section names PolyForm Noncommercial; judge confirms via Read.

## Out of scope

- Changing any license text or metadata value — the LICENSE, pyproject, and README
  already hold the correct PolyForm values; this task only pins them against
  regression.
- The sandbox-path-leak regression gate (sibling proposal this cycle).
- Relicensing to an OSI-permissive license (goal.md Non-goal — a separate operator
  call).
- Any `python -m build --sdist` build smoke (deferred — non-hermetic).
