# TB-137 — Bump verifier judge max_turns default 8 → 20

## Goal

verify._judge_prose_bullet caps each per-bullet judge at AP2_VERIFY_JUDGE_MAX_TURNS turns (default 8). Eight is tight when the judge needs more than 1-2 Grep/Glob/Read round-trips — e.g. a bullet asserting a test exists in a moved file may take 3-4 tool calls (glob for the test name, grep for the function, read the file shape). Hitting the cap mid-investigation forces an unverified/fail verdict despite the bullet being satisfied in HEAD. Bump default to 20: still bounded, but gives the judge enough budget for non-trivial repo navigation. Single-line change at verify.py:303 (int(os.environ.get("AP2_VERIFY_JUDGE_MAX_TURNS", 20))). Tests in test_verify_retry_diff.py that pin the default value need updating. No new env wiring — operators can still tighten via AP2_VERIFY_JUDGE_MAX_TURNS=4 if cost matters more than thoroughness.

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
