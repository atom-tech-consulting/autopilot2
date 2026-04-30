# TB-137 — Bump verifier judge max_turns default 8 → 20

## Goal

verify._judge_prose_bullet caps each per-bullet judge at AP2_VERIFY_JUDGE_MAX_TURNS turns (default 8). Eight is tight when the judge needs more than 1-2 Grep/Glob/Read round-trips — e.g. a bullet asserting a test exists in a moved file may take 3-4 tool calls (glob for the test name, grep for the function, read the file shape). Hitting the cap mid-investigation forces an unverified/fail verdict despite the bullet being satisfied in HEAD. Bump default to 20: still bounded, but gives the judge enough budget for non-trivial repo navigation. Single-line change at verify.py:303 (int(os.environ.get("AP2_VERIFY_JUDGE_MAX_TURNS", 20))). Tests in test_verify_retry_diff.py that pin the default value need updating. No new env wiring — operators can still tighten via AP2_VERIFY_JUDGE_MAX_TURNS=4 if cost matters more than thoroughness.

## Scope

- (file / module to change)

## Design

(filled in by /tb prep or by the ideation agent)

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `grep -qE 'AP2_VERIFY_JUDGE_MAX_TURNS.{0,8}20' ap2/verify.py` — new default value of 20 present at the call site
- `! grep -qE "AP2_VERIFY_JUDGE_MAX_TURNS.{0,8}\\b8\\b" ap2/verify.py` — old hard-coded 8 fallback is gone (so a missing env var picks up 20, not 8)
- New / updated unit test in `test_verify_retry_diff.py` (or wherever the judge max_turns is currently pinned): with `AP2_VERIFY_JUDGE_MAX_TURNS` unset, the SDK options handed to `_judge_prose_bullet` carry `max_turns=20`.
- Updated unit test: with `AP2_VERIFY_JUDGE_MAX_TURNS=4` set, the SDK options carry `max_turns=4` — the env-override path keeps working so operators can still tighten the budget.
- The diff does NOT introduce new code paths or env vars beyond bumping the default. Single-line change in verify.py plus the test pin update.

## Out of scope

- Adding a separate hard timeout on the judge SDK call (separate concern; the user explicitly opted out earlier).
- Changing the per-bullet vs. batched judge structure.
- Tuning AP2_VERIFY_JUDGE_MAX_TURNS for daemon-wide cost — operators can override via env.
