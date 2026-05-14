# Teach `BriefingFix:` prefix convention in briefing-author prompts so axis-2 auto-unfreeze can fire

## Goal

The current focus is `Current focus: end-to-end automation`. TB-225
shipped axis 2 (failure-recovery operator dependency) with the parser
side fully wired: `parse_blocked_summary_fix_shape` in
`ap2/_shared.py` extracts `BriefingFix: <bullet snippet> =>
<corrected snippet>` lines from `task_complete blocked` summaries,
the `_maybe_auto_unfreeze` daemon sweep applies the patch and
re-dispatches when the shape is on `AP2_AUTO_UNFREEZE_FIX_SHAPES`,
and four bootstrap shapes ship as defaults. But the upstream EMITTER
— the per-task agent that writes the `task_complete blocked` summary
— doesn't currently know that `BriefingFix:` is a recognized
prefix. `skills/ap2-task/SKILL.md` doesn't teach it; the per-task
agent prompt body doesn't mention it. So even when an agent
self-diagnoses a briefing-shape regression in its blocked-summary
prose (as the recent task arc shows the agent often does), the
parser sees free-text prose and the auto-unfreeze sweep finds
nothing to apply.

Why now: this is the same shape as TB-219 → TB-221 (verifier
learned to classify `Prose:` correctly, but until TB-221 taught
the prompt the convention the override stayed cold). Without
upstream teaching, TB-225's parser + sweep + allowlist machinery
cannot fire — axis-2's delete-test ("if this work didn't ship,
every briefing-shape regression cascades into operator-manual
unfreeze; with it, the loop self-heals on the recurring class")
fails because no `BriefingFix:` line ever lands in a blocked
summary for the sweep to pick up.

## Scope

(1) Update `skills/ap2-task/SKILL.md`: add a new section
`## Reporting failures (`task_complete blocked` summaries)` that
teaches the agent the `BriefingFix: <verbatim bullet snippet> =>
<corrected snippet>` line convention. Show the four bootstrap
shapes that TB-225 ships in `AP2_AUTO_UNFREEZE_FIX_SHAPES` as
concrete worked examples (TB-204 `grep -lE` → `grep -rlE`,
TB-207 literal-backtick fix, the two others) so the agent knows
the format the parser expects.

(2) Update the per-task agent prompt body (the matching constant
in `ap2/prompts.py` — locate via `grep -n "task_complete" ap2/prompts.py`):
when the prompt enumerates "if your task fails verification" guidance,
add a bullet teaching the agent: "If verification failed because of
a briefing-shape regression you can identify in the briefing
(misused shell flags, literal backticks, missing `-r`, etc.), emit
a `BriefingFix: <broken bullet snippet> => <corrected snippet>`
line as part of your `task_complete blocked` summary. The daemon's
auto-unfreeze sweep will apply the patch and re-dispatch when the
fix shape is on the operator's allowlist."

(3) Cross-reference: add a forward link in
`ap2/howto.md`'s existing `## Failure recovery` / TB-225 section
(landed in commit `b8af9b5`) to `skills/ap2-task/SKILL.md`'s new
section so the operator surface and the agent-author surface point
at each other.

(4) Tests in new `ap2/tests/test_tb229_briefing_fix_teaching.py`:
  - `grep`-style structural test asserting
    `skills/ap2-task/SKILL.md` contains a `BriefingFix:` token plus
    a `=>` separator example.
  - Structural test asserting `ap2/prompts.py`'s task-agent prompt
    body references `BriefingFix:` at least once.
  - Structural test asserting all four bootstrap fix-shapes from
    `AP2_AUTO_UNFREEZE_FIX_SHAPES` appear as worked examples in
    the SKILL.md section (one bullet per shape).
  - Anti-drift test: when TB-225's bootstrap shape list grows by
    one (mocked), the SKILL.md teaching covers the same count.

(5) No code change. Pure prompt + skill + docs delta. This
mirrors TB-221's shape exactly (verifier learned `Prose:`, then
prompts caught up).

## Design

- Worked-example format follows TB-221's pattern:
  ````markdown
  ```
  BriefingFix: `grep -lE "foo" reports/` => `grep -rlE "foo" reports/`
  ```
  ````
  Fenced code block inside the SKILL.md section so the literal
  prefix is unambiguous to the future agent reader.
- The structural anti-drift test imports
  `AP2_AUTO_UNFREEZE_FIX_SHAPES_DEFAULT` (or whatever TB-225 named
  the bootstrap list constant — confirm via Read of
  `ap2/daemon.py`) and asserts the count of `=>` examples in the
  SKILL.md section is at least `len(bootstrap)`. Catches the
  failure mode where a fifth shape lands on the allowlist but the
  teaching example doesn't.
- The howto.md cross-reference is a one-line "see also
  `skills/ap2-task/SKILL.md` §..." pointer; no content
  duplication.

## Verification

- `uv run pytest -q ap2/tests/test_tb229_briefing_fix_teaching.py` — new test module exists and all structural cases pass.
- `uv run pytest -q ap2/tests/` — full suite green vs current 1421 baseline.
- `test -f ap2/tests/test_tb229_briefing_fix_teaching.py` — test module present.
- `grep -nE "BriefingFix:" skills/ap2-task/SKILL.md` — at least 5 matches (one heading mention + four worked examples for the bootstrap shapes).
- `grep -nE "BriefingFix:" ap2/prompts.py` — at least 1 match in the task-agent prompt body.
- `grep -nE "skills/ap2-task/SKILL.md" ap2/howto.md` — at least one cross-reference link from the failure-recovery / TB-225 section.
- Prose: the SKILL.md section walks the agent through the convention with one fenced code-block example per bootstrap fix-shape (4 examples), each labelled with the originating TB-N where the shape originally surfaced; judge confirms via Read of `skills/ap2-task/SKILL.md`.
- Prose: the per-task agent prompt body in `ap2/prompts.py` teaches the `BriefingFix:` emission rule in the same paragraph that already enumerates failure-reporting guidance, not as a standalone bullet appended to the prompt tail; judge confirms via Read of `ap2/prompts.py`.

## Out of scope

- Adding new fix-shapes to `AP2_AUTO_UNFREEZE_FIX_SHAPES`'s
  bootstrap list. Allowlist growth is operator-curated trust
  upgrade per goal.md L92-100. This task only teaches the EMITTER
  about the convention; what shapes the daemon TRUSTS to apply
  unattended stays operator-decided.
- Wiring auto-unfreeze for non-shell prose bullets. TB-225
  bootstrapped on shell-pitfall shapes; broader coverage is a
  separate cycle's call once usage data accumulates.
- Mattermost surfacing of auto-unfreeze loop activity. TB-228's
  status-report digest covers that.
- Changing `_maybe_auto_unfreeze`'s parse / apply behavior. This
  task is purely about teaching the upstream emitter; the
  downstream sweep stays unchanged.
