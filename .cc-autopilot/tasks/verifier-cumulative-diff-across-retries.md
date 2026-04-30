# TB-136 — Verifier: cumulative diff across retries + direct repo file reads

## Goal

Follow-up to TB-127. Two gaps surfaced when TB-135 retried 3x and exhausted on verifier false-negatives despite all flagged tests being on disk in HEAD. (1) _find_task_commit (verify.py:352) returns the MOST RECENT '<task_id>: ...' commit; on retries the most recent commit is an incremental fix, while the bulk of the implementation lives in the FIRST '<task_id>:' commit. The judge then sees only the small follow-up diff, doesn't find tests/changes that landed in the original commit, and falsely fails. Concrete: TB-135 has commits f839194 (95% of work, all flagged tests) and 248957f (incremental editor mode + skill doc). Verifier picked 248957f, said 'no test for --briefing-file path' — the test exists at test_cli.py:435 but in f839194's diff. Fix (option 1 from the design discussion): replace _find_task_commit + _git_show_for_task with a cumulative range diff. Walk back to find the FIRST '<task_id>:' commit (oldest), then 'git diff <first>^..HEAD -- :!.cc-autopilot/' so the judge sees every code change across all retries minus daemon state-file noise. Falls back to HEAD diff when no task-id commit exists yet. (2) The judge today reasons purely from the diff text. That assumes the diff is faithful (per (1)) and that the file content of HEAD matches the diff's last hunk — both of which break under retries with state-commits in between. Augment the prose-bullet judge with direct repo-read access: the SDK call gets allowed_tools including Read + Glob + Grep scoped to the project_root, so the judge can  to confirm a test actually exists in HEAD before declaring it missing. The judge prompt instructs it to verify presence in the working tree as authoritative when the diff is ambiguous. Belt and suspenders: cumulative diff catches the common case, repo reads catch the edge cases (file moved, symbol renamed, test split across files). Scope: verify.py refactor of _find_task_commit and _git_show_for_task into a single cumulative-range resolver; verify._judge_prose_bullet upgrades the SDK call to include Read/Glob/Grep tools and updated prompt; tests pin the cumulative-walk behavior across 1, 2, 3 task-id commits + interleaved state commits; tests pin that the judge's allowed_tools includes Read/Glob/Grep. Re-run TB-135 verification after this lands; expected pass.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

Concrete acceptance criteria the daemon's per-task verifier (TB-69)
runs after the agent's commit. Shell-command bullets (backtick-fenced
at the start of the bullet) are run automatically; prose bullets are
judged by an SDK call against the diff.

- `uv run pytest -q` — full suite passes
- (additional shell or prose bullets)

## Out of scope

- (filled in)
