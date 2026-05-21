# Operator-doc reconciliation: de-dup the two READMEs, fix stale quickstart/test-count, refresh + relink the sandbox runbook

Tags: #autopilot #docs #readme #runbook #onboarding #regression-pin

## Goal

The operator-facing first-contact docs have drifted and partly broken. An audit found:
- Root `README.md`'s quickstart `ap2 add "..." -s Backlog -d "..."` uses the PRE-TB-135 syntax — `--briefing-file` is now REQUIRED, so the literal getting-started command errors out.
- Both `README.md` AND `ap2/README.md` carry a `## Tests` section claiming "~349 tests" — the actual suite is ~1900. The identical stale number in both files proves the duplicated sections drift unmaintained.
- The two READMEs duplicate `## Quickstart` and `## Tests`; the rest is a legitimate split (root = GitHub landing: install + overview + pointers; `ap2/README.md` = full reference: CLI, config, event schema, MCP tools).
- Root README links `plan/sandboxed-user-setup.md` three times, but the file lives at the REPO ROOT (`sandboxed-user-setup.md`) — three broken links from a wrong path, not a missing file.
- `sandboxed-user-setup.md` is the live sandbox-provisioning runbook (this daemon runs by it) but has decayed: marked "Status: draft runbook", missing 5 of the 9 current sandbox verbs (`install-token`, `install-statusline`, `install-mm`, `install-channel`, `sync-assets`), and carries an "Open questions / decisions still owed" section (mostly answered now) + an ancient "Related tasks: TB-47/48/54/55/56" footer.

Goal anchor: serves `goal.md` `## Done when` bullet "an operator can point ap2 at a fresh project, paste a goal.md, and walk away for a week without intervention." A broken quickstart command, a 5x-stale test count, dead runbook links, and a half-stale provisioning runbook all force the onboarding operator to debug the docs before they can even start — the opposite of hands-off.

Why now: these are the literal first-contact docs (the GitHub README's getting-started command is broken) and the canonical deployment runbook (missing the verbs needed to actually provision a sandbox). The `@blocked` codespan records that the runbook refresh depends on TB-276 (which created `sync-assets`) and the quickstart fix depends on TB-135 (which made `--briefing-file` required) — both already Complete, so the blocker is satisfied.

## Scope

- De-duplicate the two READMEs WITHOUT merging them (the overview-vs-reference split is correct):
  - Keep root `README.md` as the thin GitHub landing page: Install, a canonical Quickstart, "what's in this repo", and pointers to the detailed docs.
  - Keep `ap2/README.md` as the full operator reference (CLI, config, event schema, MCP tools).
  - The overlapping `## Quickstart` and `## Tests` sections must be single-sourced: one README owns the canonical copy, the other links to it rather than maintaining a parallel (drift-prone) copy. Implementer picks which owns Quickstart (lean: root, since it's the landing page); Tests likewise single-sourced.
- Fix the staleness in whichever copies survive:
  - Quickstart `ap2 add` must use the `--briefing-file` form (TB-135), not the removed `-d`/positional-title syntax.
  - Replace "~349 tests" with a current approximate (~1900) OR drop the hardcoded count entirely to avoid re-drift (implementer's call; count-free phrasing is more maintainable).
- Refresh + promote `sandboxed-user-setup.md` from draft to maintained runbook:
  - Add the 5 missing sandbox verbs to the Helper-CLI section: `install-token`, `install-statusline`, `install-mm`, `install-channel`, `sync-assets`.
  - Resolve or remove the "Open questions / decisions still owed" section (the questions are largely answered — e.g. the daemon DOES hold a Mattermost token via `install-mm`).
  - Update or drop the ancient "Related tasks" footer (TB-47/48/54/55/56).
  - Flip the "Status: draft runbook" line to reflect it's the maintained deployment runbook.
- Fix the broken link path in BOTH READMEs: `plan/sandboxed-user-setup.md` → `sandboxed-user-setup.md` (repo root).

## Design

- Single-source-of-truth for overlapping doc sections: the drift here (identical "~349" in both files) is the symptom of maintaining two copies. After this task, Quickstart + Tests exist in exactly ONE README; the other links. That's the structural fix, not just patching today's numbers.
- The runbook is the canonical OS-sandbox provisioning guide and describes the LIVE deployment (this daemon runs as `claude-agent` per its Phase 0-5). Refresh it in place — do NOT delete it (it'd orphan the README links and lose the only deployment guide) and do NOT move it back to `plan/` (it's at root now; fix the links to match reality).
- Prefer count-free or approximate test phrasing so the Tests section doesn't re-stale on the next test addition (the "~349" rot is exactly this failure mode).
- Reference symbols/commands, not line numbers, in any doc citations (line-num citations rot on refactors).

## Verification

- `uv run pytest -q ap2/tests/` — full suite passes.
- `! grep -qE "349 tests|~349" README.md` AND `! grep -qE "349 tests|~349" ap2/README.md` — the stale ~349 test count is gone from both READMEs (`!` inverts so absence passes).
- `! grep -qE "plan/sandboxed-user-setup\.md" README.md` AND `! grep -qE "plan/sandboxed-user-setup\.md" ap2/README.md` — the wrong `plan/` link path is gone from both.
- `grep -qE "sandboxed-user-setup\.md" README.md` — the corrected root-path link to the runbook is present.
- `grep -qE "sync-assets" sandboxed-user-setup.md && grep -qE "install-token" sandboxed-user-setup.md && grep -qE "install-mm" sandboxed-user-setup.md && grep -qE "install-channel" sandboxed-user-setup.md && grep -qE "install-statusline" sandboxed-user-setup.md` — all 5 previously-missing sandbox verbs are now documented in the runbook.
- `! grep -qE "Status:\s*\*?\*?draft runbook" sandboxed-user-setup.md` — the "draft runbook" status line is updated/removed (`!` inverts so absence passes).
- Prose: the Quickstart `ap2 add` example (in whichever README owns it) uses `--briefing-file`, not the removed `-d`/positional-title form. The judge confirms via Read.
- Prose: the two READMEs no longer duplicate the Quickstart and Tests sections — overlapping content is single-sourced with the other README linking to it. The judge confirms by reading both and seeing one canonical copy + a link, not two parallel copies.
- Prose: `sandboxed-user-setup.md`'s "Open questions / decisions still owed" and "Related tasks" sections are resolved/removed/updated rather than left as stale planning residue. The judge confirms via Read.

## Out of scope

- `ap2/architecture.md`, `ap2/howto.md`, `skills/ap2-task/SKILL.md` — already reconciled by TB-274; don't re-touch.
- `CHANGELOG.md` — not in this pass (can be a follow-up if it's stale).
- Merging the two READMEs into one — the overview-vs-reference split is intentional; this task de-duplicates overlap, it does not consolidate the files.
- Rewriting the runbook's actual setup procedure (Phase 0-5 steps) — they're current; only add the missing verbs + strip the planning residue + fix status.
- Moving `sandboxed-user-setup.md` into a `plan/` or `docs/` directory — it's at root; fix the links to match, don't relocate the file.
- Deploying the refreshed howto/skills — `ap2 sandbox sync-assets` is a separate operator/deploy action, not part of this doc edit.
