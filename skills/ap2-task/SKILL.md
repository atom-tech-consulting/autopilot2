---
name: ap2-task
description: "Add a task to an ap2 sandbox project's Backlog by authoring a briefing markdown file and passing it to `ap2 add --briefing-file` (locks TASKS.md, issues a fresh TB-N, no ID collisions). Auto-detects whether to run as the sandbox user directly or escalate via sudo from the main user. Also the operator-facing reference for the task-agent contract, authoring `## Verification` bullets (the `Prose:` prefix + the four shell-bullet pitfalls), and `ap2 classify` impact verdicts — mirroring the daemon-canonical authoring rules in `ap2/ideation.default.md`."
user_invocable: true
---

<command-name>ap2-task</command-name>

# ap2-task — add a task to a sandbox project

Wrap `ap2 add` for the common case: drop a briefing-backed task into a project's Backlog. The daemon's ideation/reflector loop picks it up on the next cycle.

## Usage

```
/ap2-task <project> <content>
```

- `<project>`: bare name like `stoch` (resolved to `~$AP2_SANDBOX_USER/repos/<name>`, where `AP2_SANDBOX_USER` defaults to `claude-agent`) or an absolute path containing `.cc-autopilot/`.
- `<content>`: free text describing the work. Can be a single line or a multi-line block. You will SHAPE this into a briefing markdown file before invoking `ap2 add`:
  - First line / first sentence → briefing **H1** (the task title).
  - Remainder → briefing's `## Goal` / `## Scope` prose.
  - Any `#hashtag` tokens go on a `Tags:` line.

If no project arg is given, list `~$AP2_SANDBOX_USER/repos/*` and ask which one.

## Why this skill exists (and not just direct `ap2 add`)

Editing `TASKS.md` directly from the human's clone causes ID collisions and merge conflicts with the daemon's in-flight edits. `ap2 add` runs *inside the sandbox*, takes the `TASKS.md.lock`, and issues IDs from the daemon's authoritative state.

## ap2 add now requires a briefing file (TB-135)

`ap2 add` no longer accepts free-form `-t`/`-d`/positional title args by themselves. You **must** supply a briefing one of three ways — title and tags are parsed from the briefing's H1 and an optional `Tags:` line:

- `--briefing-file <path>` — point the flag at a markdown file you wrote.
- `--briefing-file -` — read the briefing from stdin (this skill's default — no temp file).
- `ap2 add` with no args + `$EDITOR` set — git-commit-style: opens `$EDITOR` against the template and uses the saved buffer. Aborting the editor (empty save, unchanged template, non-zero exit) makes `ap2 add` exit non-zero. **This skill's flow uses `--briefing-file -` instead** so it works headless without an interactive terminal; the editor flow is for human operators at a tty.

The pre-TB-135 auto-fill skeleton is gone — without a real `## Verification` section the per-task verifier had no scope-specific scoring and tasks "passed" on the regression gate alone (TB-131 hit this on 2026-04-30).

**The canonical briefing template lives in `ap2/init.py:BRIEFING_TEMPLATE`** — H1, optional `Tags:` line, `## Goal`, `## Scope`, `## Design`, `## Verification`, `## Out of scope`. The daemon's per-task verifier (TB-69) reads `## Verification` to score the task's commit, so include at least one concrete shell or prose bullet there beyond the project-wide regression gate.

## Steps

### 1. Resolve `PROJECT_ROOT`

- Bare name → resolve via `eval echo "~${AP2_SANDBOX_USER:-claude-agent}/repos/<name>"`.
- Absolute path → use as-is, but verify `<path>/.cc-autopilot/` exists.
- Verify the sandbox clone exists with `test -d <PROJECT_ROOT>/.cc-autopilot`. If it doesn't, stop and report — point the user at `ap2 sandbox project-setup`.

### 2. Author the briefing markdown

Compose a briefing buffer matching the template in `ap2/init.py:BRIEFING_TEMPLATE`. Minimum required:

```markdown
# <one-line task title>

Tags: #area #kind

## Goal

<one paragraph: what success looks like, why this matters>

## Scope

- <file or module to change>

## Verification

- `uv run pytest -q` — full suite passes
- <one or more concrete shell or prose bullets the daemon's verifier
  (TB-69) can score against the task's commit diff>
```

Conventions:

- **Title (H1)**: `# Title` — the first H1 is parsed as the task title. Keep under ~70 chars, imperative verb preferred ("Add X", "Write Y"). Do NOT prefix with `TB-N`; the daemon allocates the ID. (If you copy from a daemon-prepped briefing where the H1 is already `TB-N — Title`, the parser strips `TB-N — ` for you.)
- **Tags line**: optional `Tags: #cli #helpers` (case-insensitive prefix). Tokens are `#`-prefixed words OR comma-separated bare words; both shapes round-trip onto the rendered task line. If you also pass `-t #extra` to `ap2 add`, those are appended (deduped) on top of the briefing's tags.
- **Verification**: at least one bullet beyond the project-wide pytest gate. Shell bullets (backtick-prefixed) run automatically; prose bullets are judged by an SDK call against the diff. Empty `## Verification` means the verifier scores nothing scope-specific.
  - **`Prose:` prefix for codespan-leading prose bullets (TB-219).** When a prose bullet's grammatical subject is a backtick-fenced filename or symbol (e.g. `` `ap2/tests/test_x.py` ``), the classifier would otherwise route it to shell and try to exec the bare path. Lead the post-codespan text with the literal token `Prose:` (case-sensitive, single colon) to force prose classification — it is the canonical hard-override signal and wins over every other classifier signal. Example: `` - `ap2/tests/test_x.py` Prose: the file includes the expected fixture; judge confirms via Read.`` Reach for the prefix first whenever a prose bullet starts with a codespan; see the **Authoring `## Verification` bullets** reference section at the end of this skill for the full convention (incl. the four shell-bullet pitfalls + a worked example).
  - **Auto-verifiable bullets only (TB-138).** Every `## Verification` bullet must be auto-verifiable — one of: (1) a backticked shell command the verifier can `/bin/sh -c`, (2) a unit/e2e test name the regression gate covers, or (3) a prose claim that names a concrete file/symbol an SDK judge can `Read`/`Grep` against the diff. **No `Manual:` bullets.** No "operator runs X live and observes Y" — the verifier runs unattended and cannot observe out-of-band actions. The queue-append validator rejects `Manual:` bullets outright (TB-171) before TB-N allocation, so don't bother trying.
  - Canonical conversion (TB-122): the briefing originally had `- Manual: kick a long-running task on stoch, mention @claude-bot status → handler replies in <30s`. The verifier (correctly) couldn't evaluate it; 5/6 bullets passed but the manual one kept failing → `retry_exhausted` and the task got re-frozen despite the implementation being complete. The fix: stub a slow SDK reply in an e2e test, enqueue a Mattermost mention, assert the handler's `mattermost_reply` event lands within 30s of the mention timestamp — pins the same responsiveness claim end-to-end.
  - If a behavior genuinely cannot be auto-verified (rare), put it in `## Out of scope`. Do **not** invent a separate "manual checklist" section — if the daemon can't gate on it, it's out of scope.
- **Canonical structure (TB-154).** The briefing must contain ALL FIVE `##`-level sections — `## Goal`, `## Scope`, `## Design`, `## Verification`, `## Out of scope` — by exact name. The queue-append-time validator (`_validate_briefing_structure` in `ap2/briefing_validators.py` — TB-262 split it out of `ap2/tools.py`; still importable via `ap2.tools._validate_briefing_structure` thanks to the re-export) rejects briefings missing any of these BEFORE TB-N is allocated. Section order is free; extension (extra `##` sections like `## Decision log`) is allowed; renaming (`## Acceptance` for `## Verification`) is not.
- **Goal-anchor cite (TB-161).** The `## Goal` body must reference (substring match) one of the project `goal.md`'s `## Current focus` heading titles or one of its `## Done when` bullets. The validator's error message lists the available anchors. This guards against ap2-meta-polish drift — every proposal must reduce to a visible step toward the declared project goal.
- **`Why now:` rationale (TB-164).** The `## Goal` body must include a line-anchored `Why now:` paragraph (regex `(?im)^\s*why now[\s:]`) of at least 40 chars after the marker. This articulates the project's "if we delete this and the goal still ships, was it useful?" delete-test in writing. Trivial passes (`Why now: yes`) fail the length check.
- **Operator escape hatch:** the goal-anchor + Why-now checks (TB-161 / TB-164) can be bypassed for legitimately-meta operator work via `ap2 add --skip-goal-alignment` / `ap2 update --skip-goal-alignment` (TB-170). The bypass is operator-CLI-only — ideation and the MM handler never set the flag, so autonomous-agent proposals always run all checks. The audit line in `operator_log.md` captures the bypass.

### 3. Pass the briefing to `ap2 add`

Two delivery paths — pick whichever is cleaner for the invocation context:

- **`--briefing-file -` (stdin, no temp file):** pipe the briefing buffer into the CLI from the same shell command.
- **`--briefing-file <path>`:** write the briefing to a temp file first (e.g. `mktemp`), then point the flag at it. Useful when the buffer is too big for a here-doc, or when sudo's argv quoting makes stdin awkward.

### 4. Detect who's running this skill

Run `whoami` (or read `$USER`). The result determines whether sudo is needed:

- **Same as `$AP2_SANDBOX_USER` (default `claude-agent`)** → already inside the sandbox; call `ap2` directly. No sudo.
- **anything else** (typically the human user) → escalate via `sudo -u $AP2_SANDBOX_USER`.

### 5. Compose and run the command

Resolve the `ap2` binary with `command -v ap2` (or pin to its absolute path if that's been published in your environment). The sandbox user should have `ap2` on its PATH too — typically the same `uv tool install` your human user did, run inside the sandbox shell.

**Sandbox user, stdin-fed briefing (no sudo):**

```
printf '%s' "$BRIEFING" | ap2 --project <PROJECT_ROOT> add -s Backlog --briefing-file -
```

**Main user, sudo to the sandbox user, file-fed briefing:**

```
TMP=$(mktemp) && printf '%s' "$BRIEFING" > "$TMP" && \
  sudo -u "${AP2_SANDBOX_USER:-claude-agent}" -- "$(command -v ap2)" \
    --project <PROJECT_ROOT> add -s Backlog --briefing-file "$TMP" \
  && rm -f "$TMP"
```

Quote the briefing payload carefully — it can contain backticks, dollar signs, and other shell metacharacters. Prefer single-quoted shell variables (`$'…'` if you need escapes) or write to a temp file rather than inlining the briefing into the command line.

Expected single-line output: `TB-N (queued; will land at next tick)`. If sudo prompts for a password and the call fails, fall back to printing `! sudo …` for the human to execute.

### 6. Report the result

Briefly state:
- The new **TB-N** (parse from the command's stdout).
- That the daemon will pick the queued add up on its next tick (typically <1 minute), then auto-promote Backlog → Ready → Active.
- Suggest `/ap2 <project>` if they want to verify.

Do **not** edit `TASKS.md` or `CLAUDE.md` in the human's local clone — the sandbox is the source of truth for the board now.

## Reporting failures (`task_complete blocked` summaries)

When a task agent's verification fails on a briefing-shape regression
the agent can identify in the briefing itself (misused shell flags,
literal backticks, missing `-r`, a bare path with no `test -f`, a
bare `python` invocation that should be `uv run python`, etc.), the
agent should emit a structured `BriefingFix:` line as part of its
`report_result(status="blocked", summary=...)` payload. The daemon's
auto-unfreeze sweep (TB-225) parses the line, verifies the briefing-
line literal match, patches the briefing via the operator-queue
`update` op, and re-dispatches the task — all without operator-manual
`ap2 unfreeze`, provided the named fix-shape is on the operator's
`AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist (defaults unset → feature is
opt-in; the env-knob string IS the trust contract).

Canonical line shape (`ap2._shared.parse_blocked_summary_fix_shape`
is strict — no regex-on-prose guessing, no inferring `from`/`to`
from free text):

```
BriefingFix: <shape> at <briefing_path>:<line>: <from> -> <to>
```

- `<shape>` is a snake_case fix-shape token; the operator's
  `AP2_AUTO_UNFREEZE_FIX_SHAPES` allowlist consults this name. Stick
  to a published shape — the daemon refuses anything not on the
  allowlist with `auto_unfreeze_skipped reason=shape_not_in_allowlist`.
- `<briefing_path>` is project-relative (typically
  `.cc-autopilot/tasks/<slug>.md`).
- `<line>` is the 1-indexed line number where `<from>` literally
  appears in the briefing; the daemon verifies the literal match
  before patching (closes the operator-edit-during-failure data-race
  window — a mismatch emits `auto_unfreeze_skipped
  reason=briefing_mismatch` and leaves the task Frozen, fail-safe).
- `<from>` and `<to>` are the literal substrings the daemon line-
  replaces. The first ` -> ` (space-arrow-space) is the separator;
  subsequent occurrences are part of `<to>`.

Use this prefix only when the failure root cause is genuinely a
briefing-shape regression that one of the published shapes covers.
Free-text diagnoses without the prefix fall through to today's
manual-unfreeze path identically; emitting a malformed `BriefingFix:`
line is harmless (the parser returns None and the task stays Frozen
until an operator intervenes), but it wastes the audit-trail slot.

### Four bootstrap fix-shapes (worked examples)

These four shapes are the canonical bootstrap set the auto-unfreeze
sweep ships against (each names a pitfall catalogued in
`ap2/ideation.default.md`'s `## Shell-bullet pitfalls to AVOID`
section). The originating TB-N is the in-tree task where the shape
first surfaced — if a future shape gets added, follow the same
"one fenced example per shape, labelled with origin" pattern below.

- **`grep_missing_r_on_dir`** (origin: TB-204). `grep -lE 'pattern'
  <dir>/` returns nothing without `-r`; the verifier sees zero matches
  and the bullet fails. Fix: add the `-r` recursive flag.

  ```
  BriefingFix: grep_missing_r_on_dir at .cc-autopilot/tasks/foo.md:23: grep -lE 'pattern' ap2/tests/ -> grep -rlE 'pattern' ap2/tests/
  ```

- **`literal_backtick_in_shell_bullet`** (origin: TB-207). A bullet
  with literal backticks like `` `grep ... | wc -l` `` truncates at
  the first backtick when the verifier slices the codespan out; the
  remaining argv is malformed. Fix: drop the wrapping backticks —
  the bullet body IS the command.

  ```
  BriefingFix: literal_backtick_in_shell_bullet at .cc-autopilot/tasks/foo.md:42: `grep -c 'foo' bar.py` -> grep -c 'foo' bar.py
  ```

- **`bare_python_to_uv_run`** (origin: TB-76). `python -c '...'`
  exits 127 in the daemon environment (no `python` on `$PATH` outside
  the project's `uv` venv). Fix: prefix the invocation with `uv run`.

  ```
  BriefingFix: bare_python_to_uv_run at .cc-autopilot/tasks/foo.md:55: python -c 'import ap2; print(ap2.__version__)' -> uv run python -c 'import ap2; print(ap2.__version__)'
  ```

- **`bare_path_to_test_f`** (origin: TB-76). A bullet whose body is
  a bare path (e.g. `reports/foo.md`) tries to execute the file
  (exit 126); the verifier reads this as "command failed" rather
  than "file should exist." Fix: wrap in `test -f`.

  ```
  BriefingFix: bare_path_to_test_f at .cc-autopilot/tasks/foo.md:67: reports/foo.md -> test -f reports/foo.md
  ```

For the full operator-side knob set (per-task / per-day caps, audit
event names, the trust-contract rationale for the allowlist) see
`ap2/howto.md`'s `## Operator-in-the-loop relaxations` →
auto-unfreeze (TB-225) section.

## Default section

Backlog. Don't dump tasks straight into Ready — Ready means "briefing prepared", and your briefing is a *seed* (the daemon's prep step may still flesh out `## Design`, etc.). The ideation/reflector flow handles promotion.

If the user explicitly says "ready" or "frozen" in their request, override accordingly via `-s Ready` / `-s Frozen`.

## Examples

```
/ap2-task stoch Write a CONTRIBUTING.md covering setup, tests, code style. #docs
```

→ resolves to `~claude-agent/repos/stoch` (assuming `AP2_SANDBOX_USER` unset), authors a briefing whose H1 is "Write CONTRIBUTING.md covering setup, tests, code style" with `Tags: #docs`, and pipes it through stdin:

```
printf '%s' '# Write CONTRIBUTING.md covering setup, tests, code style

Tags: #docs

## Goal

Document the project'"'"'s setup, test commands, and code-style expectations
so new contributors can ramp without DM'"'"'ing the maintainers.

## Verification

- `uv run pytest -q` — full suite passes
- `test -f CONTRIBUTING.md` — file landed at project root
- prose: CONTRIBUTING.md covers (a) install / setup, (b) running tests,
  (c) code style + lint hooks
' | sudo -u claude-agent -- "$(command -v ap2)" --project ~claude-agent/repos/stoch add -s Backlog --briefing-file -
```

## Structured task metadata: `@<key>:<value>` codespans (TB-132)

Some structured fields live on the rendered task line as backtick codespans, parallel to `#tags`. Single rule: any backtick span starting with `#` is a tag, any starting with `@` is structured metadata. Currently consumed:

- **`@blocked:<csv>`** — comma-separated blocker tokens. Each is either a `TB-N` task id or a `<scheme>:<value>` external blocker (currently `pid:<N>@<TS>`). Auto-promotion skips a task as long as any blocker token is unsatisfied. Pass via `ap2 add --blocked TB-5,TB-7` — the CLI writes the codespan; **do NOT** stuff `(blocked on: ...)` into the briefing or the description. The parser no longer regexes prose for that phrase, so writing it as descriptive text is harmless (and writing it as the only blocker carrier silently does nothing on new tasks).

The format extends naturally to `@priority:high`, `@owner:alice`, `@due_date:2026-05-15`, etc. without expanding the parser regex — `Task.meta` is a free-form dict on the parsed task. Add a CLI surface (or another writer) per-field as the need arises.

A pre-TB-132 transition fallback still parses `(blocked on: ...)` from descriptions for tasks authored before the codespan format landed, so existing tasks aren't broken. New tasks should always go through `--blocked`.

## Rules

- **Sandbox is canonical.** Never write to `TASKS.md` in the human's local clone — even if the daemon has uncommitted changes, the sandbox's state is what matters.
- **One task per invocation.** No batching. Re-run for additional tasks.
- **Briefing required (TB-135).** Every `ap2 add` call needs `--briefing-file`. The minimum useful briefing is H1 + a `## Verification` section with at least one bullet beyond the regression gate. Skipping the briefing is now a hard error, not a free pass to a skeleton.
- **Single line for tags / titles / blocked.** Tag tokens, the H1 itself, and `--blocked` values must be single-line — embedded newlines break the line-oriented parser (TB-134). For richer prose, put it inside the briefing's `## Goal` / `## Scope` sections.
- **Blockers go in `--blocked`, not in the description (TB-132).** Pass `--blocked TB-5,TB-7` to declare structured blockers; the CLI emits a `@blocked:TB-5,TB-7` codespan on the task line. Don't write `(blocked on: ...)` into the briefing prose — TB-132 ended that path because it collided with descriptive text (TB-121 self-blocked on the literal phrase).
- **Run sudo directly when invoking from the main user.** Call it through Bash like any other command. Only fall back to handing the `! sudo …` form to the user if sudo prompts for a password and the call fails.

---

# Reference: task-agent contract + verification-bullet authoring + classify verdicts

The three reference sections below were consolidated out of `ap2/howto.md`
(TB-400) so the operator-facing briefing-authoring reference lives next to
the `ap2 add` flow that produces briefings. **`ap2/ideation.default.md`
remains the canonical daemon source for the briefing-authoring rules the
ideation agent follows; this skill MIRRORS those rules for operators.**
When the two drift, reconcile against `ap2/ideation.default.md`.

## The task agent contract

If you (the Claude session) are dispatched as a **task agent**, your
prompt is built from `_TASK_HEADER` + the briefing file + a tail of
recent events + `_TASK_FOOTER`. You must:

1. **Read the briefing first** at `.cc-autopilot/tasks/<task-slug>.md`.
   It has `## Goal` / `## Scope` / `## Verification` (your gate) /
   `## Out of scope`.
2. **Check for prior work.** Before you start: `git log --grep="<TASK_ID>" --oneline`.
   If a previous attempt committed but didn't report, decide whether to
   extend or accept the existing work — don't redo from scratch.
3. **Make code changes** with regular `Edit` / `Write` / `Bash`. **Do
   NOT touch** these files (the SDK actively rejects writes via
   `disallowed_tools`):
   - `TASKS.md` — daemon owns the board
   - `CLAUDE.md` — daemon bumps `Next task ID`
   - `goal.md` — operator-curated mission; if you think it needs an
     update, raise it in your `summary`, don't rewrite
   - `.cc-autopilot/progress.md` / `events.jsonl` /
     `ideation_state.md` / `cron.yaml`
4. **Commit your work** with subject starting `<TASK_ID>: ...`. The
   prefix is load-bearing — the daemon's HEAD-recovery path (TB-65)
   uses it to salvage runs where you crashed before reporting.
5. **Call `mcp__autopilot__report_result(...)` ONCE at the end.** This
   is the only completion signal the daemon listens for.

```python
report_result(
    status="complete",          # complete | incomplete | blocked | failed
    commit="a1b2c3d4",          # 7-40 char SHA, or "" if no commit
    summary="Added X to Y, all tests pass.",
    files_changed="foo/bar.py, foo/bar_test.py",
    tests_passed="true",        # "true" / "false"
)
```

To surface "this should fire on a schedule" without bundling it into the
result reporting, call the dedicated `cron_propose(name, schedule, prompt,
rationale)` tool one or more times (TB-123 lifted the legacy `cron='...'`
argument out of `report_result`). Proposals queue for operator review;
they do NOT mutate `cron.yaml`.

If you forget to call the tool, the daemon reads `git log -1`. If HEAD's
subject starts with `<TASK_ID>:` it's salvaged as Complete; otherwise
the task shelves to Backlog and retries up to `AP2_MAX_RETRIES` (default
3), then Frozen.

### Long-running work — use `pipeline_task_start`

If your work would take >~5 minutes wall-clock (grid sweeps,
full-history backtests, Polygon-class data fetches, ML training,
anything with rate-limited APIs), don't run it inline. Call:

```python
pipeline_task_start(
    name="my-sweep",
    command="uv run python scripts/run_my_sweep.py",
)
```

The tool spawns the command detached, captures the pid +
`create_time()`, and emits a `pipeline_start` event. After your
`report_result(status="complete", ...)` the daemon moves THIS task
to a `Pipeline Pending` board section (TB-115). On every subsequent
tick, the daemon checks whether all of your spawned pids are dead.
Once they are, it re-runs your briefing's `## Verification`
against the post-pipeline working tree — pass → Complete, fail →
Backlog (with retry-counter bump) → Frozen on retry exhaustion.
You can call `pipeline_task_start` multiple times in one turn for
parallel pipelines (use distinct `name` values); the daemon waits
for ALL of them.

The briefing's `## Verification` IS the post-pipeline verification —
write it to check output artifacts (`test -f reports/foo.csv`,
JSON schema validation, etc.). Pre-TB-115's two-tier
launch-task-and-validation-task split is retired.

## Authoring `## Verification` bullets (briefing convention)

Bullets in a briefing's `## Verification` section are the per-task gate's
input — the daemon parses them into one of three kinds and dispatches
each: **shell** (run via subprocess; exit 0 = pass), **prose** (judged by
SDK against the cumulative task diff + working tree), or **malformed**
(classifier-detected unrecoverable shape; recorded as fail). The
classifier in `ap2/verify.py::parse_verification_section` (TB-219) decides
the kind from the bullet's markdown shape. Four pitfalls have caused
n=4 retry cascades in the 2026-05-12 → 2026-05-13 window alone
(TB-204/TB-206/TB-207/TB-209). The conventions below close every one.

### Prose bullets — use the `Prose:` prefix for explicit classification

Prose bullets that DON'T lead with a backtick-fenced token (e.g.
`- the new feature is documented in CLAUDE.md`) classify as prose
automatically. Prose bullets that DO lead with a backtick-fenced subject
(e.g. `- ``ap2/tests/test_x.py`` exists with the expected fixture`)
would otherwise classify as shell — and the verifier would try to exec
the bare path. To force prose classification, prefix the post-codespan
text with the literal token `Prose:` (case-sensitive, single colon):

> `` `ap2/tests/test_x.py` Prose: the file includes the expected
> `_COVERAGE_DRIFT_EXEMPT_SURFACES` fixture; judge confirms via Read.``

The `Prose:` prefix is a hard override — it wins over every other
classifier signal. Operators have been writing the convention organically
since the TB-206/207/209 fix briefings; TB-219 codified it.

A heuristic fallback also routes codespan-leading bullets to prose if the
bullet text contains any of the phrases in
`ap2/verify.py::JUDGE_INDICATOR_PHRASES` (e.g. `Judge confirms`,
`judged via`). It's a safety net for briefings that don't use the
`Prose:` prefix; the prefix is the canonical signal — reach for it first.

### Shell bullets — four authoring pitfalls

1. **No literal backticks in the command body.** Markdown's
   single-backtick codespan cannot represent a literal backtick — mistune
   truncates the codespan at the inner backtick and the rest of the
   command leaks into the bullet's prose body. Workarounds:
   - If the literal backtick is part of a regex pattern, replace it with
     the regex any-char `.` (e.g. `'^\| .pat'` instead of
     `'^\| `pat'`). This is the simplest fix and what TB-207's operator
     post-mortem ships.
   - If the literal backtick is genuinely required, wrap the codespan
     with **double backticks**: `` `` `cmd-with-`backtick`-in-it` `` ``.
     Mistune preserves the inner backtick under double-backtick wrapping.
   - The TB-219 classifier detects the broken single-backtick shape and
     emits `kind="malformed"` rather than silently exec'ing a truncated
     half-command, so a slip-up here surfaces as a verification fail
     with a rewrite suggestion in the event payload.
2. **Absence-check shell bullets must use the `!` exit-inversion prefix.**
   `grep "absent string" file` exits 1 when the string is absent, which
   the verifier reads as a FAIL. The intent is the inverse: pass iff
   absent. Use bash's exit-status negation: `! grep "absent string" file`
   passes when `grep` exits non-zero (string not found) and fails when
   `grep` exits 0 (string found — the absence claim is violated).
3. **Directory-walking grep must use `-r`.** `grep -lE 'pat' dir/` exits
   2 with "Is a directory" because plain `grep` is a file-only matcher.
   The bullet looks correct but always fails at runtime. Use `grep -rlE
   'pat' dir/` (or pre-list files via `find dir/ -type f`).
4. **`Prose:` prefix for judge bullets.** Covered above — the
   complement to the three shell pitfalls. If a bullet's grammatical
   subject is a backtick-fenced filename / symbol and the rest is a
   claim to judge against the diff, lead the suffix with `Prose:`.

A worked example combining all four:

```
## Verification

- `uv run pytest -q ap2/tests/` — full suite green (the canonical happy-path bullet).
- `! grep "deprecated_symbol" ap2/` — the symbol is gone (absence check; `!` is required).
- `grep -rlE 'pat' ap2/` — directory walk needs `-r` (file-only without it).
- `[ "$(grep -rcE '^| .pat' ap2/cli.py)" -ge 1 ]` — regex pattern; `.` substitutes for a literal backtick the codespan couldn't represent.
- `ap2/tests/test_new.py` Prose: the new test asserts on the documented fixture set; judge confirms via Read.
- `skills/ap2-task/SKILL.md` Prose: the new convention section names all four pitfalls. Judge confirms via Read.
```

## Classify verdicts

`ap2 classify TB-N --impact <verdict>` accepts one of four values from
`IMPACT_VERDICTS` (single source of truth at `ap2/briefing_validators.py`; still importable via `ap2.tools.IMPACT_VERDICTS` thanks to TB-262's re-export). The four
buckets form a gradient — substantive-positive → compliance-neutral →
actively-harmful — with `unclear` as the explicit "can't tell yet"
bucket. Pick the verdict by running two delete-tests in sequence:

- **`advanced-goal`** — substantively advanced the goal (positive).
  Passes the base delete-test: "if we deleted this task, would the
  goal still ship?" Answer: no — the goal would be visibly worse off
  without this work. Use when the task moved the active focus's
  progress signals closer (or the top-level `## Done when` criteria,
  if the work cuts across foci), unblocked a downstream task, or
  shipped a user-visible capability the goal names.

- **`pro-forma`** — goal-shaped but didn't advance — compliance signal
  (no-impact + no-harm). Fails the base delete-test: deleting this
  task would leave the goal in the same place. But also passes the
  stronger delete-test below: deleting it wouldn't make the codebase
  BETTER either — it just sat there, goal-shaped, satisfying
  validators without moving the needle. Use when the task satisfied
  its briefing on paper but the operator can't point to where the
  goal moved (goal.md L66-76's named failure mode).

- **`negative`** — actively regressed something OR made the codebase
  worse (no-impact + harm). Fails BOTH the base delete-test AND the
  stronger delete-test: "if we deleted this work, would the codebase
  be BETTER, not just neutral?" Yes → `negative`. Use when a
  regression slipped through, test coverage was inadvertently
  weakened, a refactor landed but increased complexity beyond the
  briefing's intent, or some other codebase-WORSE outcome — the kind
  of shape ideation should strongly avoid proposing again. The load-
  bearing distinction from `pro-forma` is the harm dimension:
  `pro-forma` is "neutral, didn't help"; `negative` is "neutral on
  the goal AND made the codebase worse."

- **`unclear`** — impact not yet legible (uncertain — defer). Use
  when the operator can't honestly answer either delete-test yet —
  the work is too recent, depends on downstream behavior that hasn't
  shipped, or surfaces a question rather than a verdict. Distinct
  from skipping (`ap2 audit [s]kip`): `unclear` records that you
  looked AND decided you can't decide; skip records that you didn't
  decide. Re-classify later when the impact becomes legible.

The `pro-forma` ↔ `negative` distinction (TB-251) is the load-bearing
new signal: under `AP2_AUTO_APPROVE=1` the classify stream is the
primary judgment surface for ideation prompt-tuning, and collapsing
"neutral-but-low-value" and "actively-harmful" into one bucket loses
the signal ideation needs to strongly avoid harmful shapes vs merely
de-prioritize compliance-shaped ones. When in doubt between the two,
ask: "after this shipped, was the codebase in a strictly worse state
than before? (regressed test, weakened invariant, accreted
complexity)" — if yes, `negative`; if no, `pro-forma`.

Historical classifications stand — TB-251 did not backfill prior
`pro-forma` records as `negative`. Future classifications use the
richer vocabulary.
