## Goal

Make the README accurate for an outside consumer under goal.md's **Current focus:
cut a public source-available distribution**. Update the README License section
from "All rights reserved" to PolyForm Noncommercial 1.0.0 (source-available,
noncommercial — explicitly NOT OSI open source), and add a note that the
committed `.cc-autopilot/` tree is ap2's own self-management state (resettable via
`ap2 init`) so an outside reader isn't confused by the shipped task history.
Serves the focus's "accurately documented under the noncommercial source-available
license" delete-test.

Why now: the README still says "All rights reserved — see LICENSE" (README
L108-110), which will contradict the new PolyForm LICENSE and mislead the first
outside reader about whether and how they may use the code.

## Scope
- Rewrite the README `## License` section to state the project is licensed under
  the PolyForm Noncommercial License 1.0.0 (source-available, noncommercial; not
  OSI-approved open source), pointing at the LICENSE file.
- Add a short README note (in the License section or a nearby notes/layout area)
  explaining that the committed `.cc-autopilot/` directory is ap2's own
  self-management state (board, tasks, progress, events) and is resettable via
  `ap2 init` — it is not part of a consumer's own project.
- Keep the rest of the README quickstart accurate for an outside checkout (no
  edits that introduce sandbox-only assumptions).

## Design
- This is the README counterpart to the LICENSE/pyproject license change; it must
  name the SAME license, so it is sequenced after that task (blocked on TB-408).
- Edit only README.md; LICENSE, pyproject, and source paths are owned by other
  tasks.

## Verification
- `grep -qi "PolyForm Noncommercial" README.md` — the README names the PolyForm Noncommercial license.
- `! grep -qi "all rights reserved" README.md` — the old proprietary statement is gone from the README.
- README's License section states PolyForm Noncommercial 1.0.0 (source-available, noncommercial, not OSI open source), and a note explains the committed `.cc-autopilot/` tree is ap2's self-management state resettable via `ap2 init`; judge confirms via Read.

## Out of scope
- The LICENSE file and pyproject metadata (separate tasks).
- Source path/identity scrub (separate task).
- Any operator-only publish / real repo URL.