# Update the top-level README quickstart for auto-approve-on-by-default + the no-sandbox safety tradeoff

Tags: #autopilot #docs #readme #quickstart #auto_approve #distribution

## Goal

Bring the repo-root `README.md` (the PyPI/front-page quickstart) in line with
auto-approve now being ON by default. Today the quickstart still presents manual
approval as the default flow — the "How it works" loop diagram shows an `ap2 approve`
step, path (a) says proposals "land in Backlog, marked pending-review" and tells the
user to run `ap2 approve TB-1`, and the closing prose frames approving proposals as a
routine in-the-loop step. With the default-on flip, ideation proposals are
auto-approved and dispatched without operator action, so that framing is now wrong and
under-warns a first-time user. Rewrite the quickstart to reflect autonomous-by-default
dispatch, show how to keep a manual review gate (opt out), and flag the safety tradeoff
of the no-sandbox quickstart path. Operator-filed docs follow-up; no goal.md focus
anchor (filed `--skip-goal-alignment`).

Why now: the auto-approve default flip changes what a fresh `ap2 start` does — agents
get dispatched and edit the user's repo + run commands unsupervised from the first
tick. The front-page quickstart is the first thing a new user reads; it must describe
the real default behavior and the opt-out, or a user following it will be surprised by
autonomous dispatch (especially on the no-sandbox path, where the daemon runs as their
own user with no OS isolation).

## Scope

- `README.md` (repo root) "How it works" loop diagram + surrounding prose: make
  auto-approve the default (ideation → auto-approved → dispatch, no manual gate), and
  present the manual review/approve step as the OPT-OUT, not the default. Update the
  closing "you stay in the loop only for…" paragraph accordingly.
- Quickstart path (a): stop instructing `ap2 approve TB-1` as the normal flow. Show that
  proposals auto-approve and dispatch on their own, and document how to KEEP a review
  gate — the opt-out knob this ships with (`[components.auto_approve] disabled = true`,
  or the `AP2_AUTO_APPROVE_DISABLED` env flag) and/or starting in dry-run
  (`[components.auto_approve] dry_run = true`) to watch decisions first. Use the EXACT
  knob names/spellings the auto-approve default-on change actually shipped — read the
  manifest / `ap2/README.md` to confirm them rather than guessing.
- Add a concise, prominent caution near the quickstart: auto-approve is ON by default,
  so a bare `ap2 start` dispatches agents that edit your repo + run commands
  unattended. The no-sandbox quickstart works, but for unattended / long-running use the
  `sandboxed-user-setup.md` runbook (separate OS user, tool isolation) is recommended;
  first-timers can opt into the review gate or dry-run above.
- Keep the no-sandbox quickstart functional and the existing path (b) author-a-task
  example intact; this is a framing/accuracy update, not a structural rewrite.

## Design

- Mirror the existing README voice/structure (the ASCII loop diagram, the (a)/(b)
  split); only the approval-default framing + the new caution change.
- Single source of truth for knob names: cross-check the shipped auto-approve manifest /
  `ap2/README.md` so the README's opt-out instructions match reality exactly.
- **Execution discipline.** FOREGROUND verification only; no `run_in_background` + poll.
  Keep tool calls bounded.

## Verification

- `! grep -nE "approve TB-1" README.md` — the quickstart no longer presents manual `ap2 approve TB-1` as the default flow (the `-E` is a plain extended-regex grep; the `!` asserts no match).
- `grep -qiE "default|by default" README.md && grep -qE "disabled|dry_run|AP2_AUTO_APPROVE_DISABLED" README.md` — the README states auto-approve is on by default AND documents an opt-out / review-gate knob.
- `uv run --extra dev pytest -q ap2/tests/ --ignore=ap2/tests/smoke` — the suite stays green (no docs-gate regressions).
- `README.md` Prose: the quickstart presents auto-approve as ON by default (autonomous dispatch), documents how to keep a manual review gate / dry-run, and carries a clear caution that the no-sandbox path runs agents unattended as the user with the sandbox runbook recommended for unattended use; judge confirms via Read.

## Out of scope

- The auto-approve behavior/posture itself (shipped by the default-on flip; this only
  documents it).
- `ap2/README.md` / `ap2/architecture.md` / the operator skills (updated by the
  default-on flip task) — this task is the repo-root front-page `README.md` only.
- Restructuring the quickstart or the install section beyond the approval-default
  framing + the safety caution.
