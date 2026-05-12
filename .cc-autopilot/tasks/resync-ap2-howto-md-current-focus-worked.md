# TB-206 — Rewrite `ap2/howto.md` worked-example blocks as structural / fictional; decouple from `goal.md` content entirely

Tags: `#autopilot` `#docs` `#code-quality` `#operator-surface` `#regression-pin`

## Goal

Close the regression-cascade failure mode opened by TB-200's design choice to quote this repo's *live* `goal.md` content inside `ap2/howto.md`'s worked-example blocks. The coupling makes `ap2/tests/test_docs.py::test_worked_example_quotes_appear_verbatim_in_goal_md` fire on every operator focus rotation (and every other legitimate `goal.md` edit) — the 2026-05-12T17:02Z `## Current focus` pivot exposed this by cascading project-wide pytest failures into TB-203/TB-204/TB-205's verification gates despite all three doing scope-correct work. Rewrite the five worked-example blocks (`### Mission`, `### Done when`, `### Current focus`, `### Non-goals`, `### Constraints`) using fictional / illustrative content so the howto teaches the **shape** of a `goal.md` without ever quoting **content** from this repo's `goal.md`.

Why now: this is an active operational failure on the current focus: code quality theme. Two operator-approved code-quality proposals (TB-203, TB-205) only landed after retries; one (TB-204) retry-exhausted to Frozen on a sibling bug. The deeper design issue is that `goal.md` rotates by design (`## Current focus` per cycle, occasional revisions to other sections) — so coupling the howto to its content guarantees the docs gate fires on legitimate operator activity, not on real drift. Operator's framing (2026-05-12): "howto.md should structurally match the expected format but not the content." This decoupling makes operator focus rotation a `goal.md`-only change forever after.

## Scope

(1) **Choose a fictional example project** and use it consistently across all five worked-example blocks. Recommended: the Slack-bot-for-trade-alerts example already named in `ap2/howto.md` L87-88's `### Mission` "Good" guidance pair (`"a Slack bot that ingests trade alerts and posts daily P&L summaries"`) — extending it through the other four sections keeps the docs internally coherent. Any other plausible-but-clearly-illustrative project (e.g. a recipe API, a CI status reporter) is acceptable; pick one and thread it through.

(2) **Rewrite all five `Worked example` blocks in `ap2/howto.md`** so the blockquoted content describes the fictional project, not this repo:
  - `### Mission` block (currently L93-100): blockquote a one-sentence mission for the fictional project.
  - `### Done when` block (currently L121-130): blockquote 1-2 concrete, measurable completion bullets for the fictional project.
  - `### Current focus` block (currently L148-156): blockquote a `## Current focus: <fictional-theme>` heading + a short narrative paragraph naming the theme.
  - `### Non-goals` block (currently L172-181): blockquote one `- **<rejected-shape>**:` bullet for the fictional project.
  - `### Constraints` block (currently L201-210): blockquote one `- **<constraint>**:` bullet for the fictional project.

  The post-blockquote explanatory paragraphs in each block can stay structurally similar (they teach the validator interaction, lede-naming pattern, etc.) but must be updated to reference the new fictional content.

(3) **Update the section-header paragraph** at `ap2/howto.md` L73-79: the sentence `"the examples quote this repo's own `goal.md` so operators have a real filled-in model rather than a synthetic one"` is now factually wrong. Replace with text that names the actual rationale ("the examples are illustrative — they teach the section's shape and validator interaction without coupling docs tests to this repo's live `goal.md` content"). Keep the pointer to `GOAL_TEMPLATE` in `ap2/init.py` for fresh projects.

(4) **Remove `test_worked_example_quotes_appear_verbatim_in_goal_md`** from `ap2/tests/test_docs.py` entirely (along with its helpers if they become unused after this removal — `_authoring_section`, `_blockquote_lines`). That test enforced exactly the coupling this task removes.

(5) **Reshape `test_worked_example_current_focus_satisfies_anchor_validator`** to be self-contained (not dependent on this repo's `goal.md` content). Two acceptable approaches — pick one:

  - **Approach X (synthetic goal.md fixture)**: write a tmp-path `goal.md` containing the fictional `## Current focus:` heading from the howto, build a synthetic briefing citing that heading's title, and assert `_validate_briefing_structure(briefing, goal_md_path=tmp_goal)` returns None. Self-contained; no live-goal.md coupling.
  - **Approach Y (parse howto's quoted heading at runtime)**: read the `## Current focus:` heading text from the howto's `### Current focus` worked-example block, build a synthetic briefing citing it, and write a tmp goal.md containing only that heading for the validator's anchor surface. Functionally equivalent to X with slightly less hard-coding.

  Either way: the test must NOT pass `goal_md_path=GOAL_PATH` (the real goal.md) — that's the coupling we're removing. `test_authoring_section_present` stays unchanged (it gates the section headings + TB-N references, which are stable structural properties).

(6) **Don't modify** `ap2/init.py`'s `GOAL_TEMPLATE` (already placeholder-based per TB-199), `ap2/tools.py`'s `_validate_briefing_structure` (orthogonal — the validator's contract is unchanged), or `ap2/ideation.default.md` (operator-curated, separate surface).

## Design

The design principle: **`ap2/howto.md` teaches the structural shape of a `goal.md`; the live `goal.md` carries content. They share structure, never strings.** TB-200's original rationale ("operators see a real filled-in working model") traded sync tax for reader concreteness. That trade-off is being reversed because the sync tax has empirically caused project-wide verification cascades, and the "real filled-in model" benefit is recoverable in other ways — the `goal.md` at the repo root is itself one click away, and `GOAL_TEMPLATE` in `ap2/init.py` already ships a placeholder shape for fresh projects.

Why a single fictional project across all five blocks (vs. per-block fictional content): a consistent through-line is easier to read. A reader who learns "this fictional bot has a Mission, a Done-when, a Current focus..." builds one coherent mental model; five disconnected fictional projects fragment it.

Why drop `test_worked_example_quotes_appear_verbatim_in_goal_md` entirely (vs. soften it): the test's purpose was to detect coupling drift. Under structural decoupling, there's no coupling left to drift — the test would always pass (or always fail in trivial ways), so it's dead weight. Removing it is cleaner than retro-fitting it to check something else.

Why reshape `test_worked_example_current_focus_satisfies_anchor_validator` instead of dropping: this test gates a different invariant — "the docs' example shape clears the validator's queue-append gate." That invariant survives the decoupling and still pays its rent (catches the case where the howto's example shape diverges from what `_validate_briefing_structure` accepts). It just needs to stop reading the live goal.md.

Test-helper exposure: if `_authoring_section` / `_blockquote_lines` become unused after dropping the verbatim test, delete them. Don't keep dead test helpers for hypothetical future use.

## Verification

- `uv run pytest -q ap2/tests/test_docs.py` — exits 0 (test_docs.py passes end-to-end).
- `uv run pytest -q ap2/tests/` — full regression suite green (the project-wide gate that TB-203/TB-204/TB-205 tripped on is unblocked).
- `! grep -n "Current focus: ideation quality signal collection" ap2/howto.md` — exit 0 (zero matches; the stale pre-pivot focus quote is gone).
- `! grep -n "Current focus: code quality" ap2/howto.md` — exit 0 (zero matches; the post-pivot focus quote is ALSO gone — the howto must not name the live focus title at all, only the fictional one).
- `grep -rn "test_worked_example_quotes_appear_verbatim_in_goal_md" ap2/` — exit 1 (test is fully removed; no references remain in the test suite or elsewhere).
- `grep -n "goal_md_path=GOAL_PATH" ap2/tests/test_docs.py` — exit 1 (the reshaped test no longer uses the live goal.md path).
- `grep -nE "^### (Mission|Done when|Current focus|Non-goals|Constraints)$" ap2/howto.md` — exit 0 with 5 matches (all five worked-example subsection headings still present).
- `grep -cE "^> " ap2/howto.md` — output ≥ 5 (at least 5 blockquoted lines remain across the five worked-example blocks; sanity bound, the actual blockquote count will be higher).
- Prose: the chosen fictional project is used consistently across all five worked-example blocks — Mission names the project, Done-when's bullets describe its completion criteria, Current-focus names one of its themes, Non-goals names a rejected shape relevant to it, Constraints names a real constraint it operates under. Judge confirms via `Read` of the rewritten `## Authoring goal.md` section.
- Prose: the section-header paragraph at `ap2/howto.md` L73-79 no longer contains the phrase `"this repo's own"` or equivalent claims that examples are from the live goal.md. Judge confirms via `Read`.

## Out of scope

- Renaming `ap2/howto.md` itself or restructuring sections outside `## Authoring goal.md`.
- Modifying `ap2/init.py`'s `GOAL_TEMPLATE` (placeholder template for fresh projects — orthogonal).
- Modifying `ap2/tools.py`'s `_validate_briefing_structure` (the validator's contract is unchanged; only the test's coupling to live goal.md changes).
- Modifying the **bad/good** guidance bullet pairs that appear before each worked example (those are already content-independent — they teach by counter-example).
- Adding an `ap2 check` warning or any new docs-drift gate beyond what `test_docs.py` already provides post-rewrite (TB-203's `test_docs_drift.py` covers the surface-name catalog; the residual `test_docs.py` covers structure; no parallel surface needed).
- Unfreezing / re-dispatching TB-204 — that's the operator's call after this lands. TB-204's blocker is a separate briefing bug (`grep -lE` without `-r` in its Verification bullet #4) and needs its own fix.
- Renaming or restructuring the worked-example block titles (`### Mission` etc.) — the section names are stable structural anchors.
## Attempts

### 2026-05-12 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -rn "test_worked_example_quotes_appear_verbatim_in_goal_md" ap2/` — exit 1 (test is fully removed; no references r; [fail] `grep -n "goal_md_path=GOAL_PATH" ap2/tests/test_docs.py` — exit 1 (the reshaped test no longer uses the live goal.md pa
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260512T232613Z-TB-206.prompt.md`, `stream: .cc-autopilot/debug/20260512T232613Z-TB-206.stream.jsonl`, `messages: .cc-autopilot/debug/20260512T232613Z-TB-206.messages.jsonl`
### 2026-05-12 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -rn "test_worked_example_quotes_appear_verbatim_in_goal_md" ap2/` — exit 1 (test is fully removed; no references r; [fail] `grep -n "goal_md_path=GOAL_PATH" ap2/tests/test_docs.py` — exit 1 (the reshaped test no longer uses the live goal.md pa
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260512T233607Z-TB-206.prompt.md`, `stream: .cc-autopilot/debug/20260512T233607Z-TB-206.stream.jsonl`, `messages: .cc-autopilot/debug/20260512T233607Z-TB-206.messages.jsonl`
### 2026-05-12 — verification_failed
(no summary)
- **kind:** per_task
- **failed_criteria:** [fail] `grep -rn "test_worked_example_quotes_appear_verbatim_in_goal_md" ap2/` — exit 1 (test is fully removed; no references r; [fail] `grep -n "goal_md_path=GOAL_PATH" ap2/tests/test_docs.py` — exit 1 (the reshaped test no longer uses the live goal.md pa
- **Debug dumps:** `prompt: .cc-autopilot/debug/20260512T234300Z-TB-206.prompt.md`, `stream: .cc-autopilot/debug/20260512T234300Z-TB-206.stream.jsonl`, `messages: .cc-autopilot/debug/20260512T234300Z-TB-206.messages.jsonl`
