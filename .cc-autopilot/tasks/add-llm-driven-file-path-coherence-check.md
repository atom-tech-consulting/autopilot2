## Goal

Add an 8th check (`_validate_briefing_file_path_coherence`) to `ap2/tools.py`'s `_validate_briefing_structure`, mirroring TB-235's check #7 LLM-judge shape (Haiku-4.5 dispatch via `sdk.query` with structured JSON output). The judge inspects every backtick-fenced shell bullet in the proposed briefing's `## Verification` section, identifies file paths referenced by commands such as `pytest -q <path>`, `test -f <path>`, `grep -q ... <path>`, `cat <path>`, `head <path>`, etc.; for each path, judges whether it (a) exists in the project HEAD at queue-append time, OR (b) is explicitly named in the briefing's `## Scope` section as a file this task creates / renames / touches. If neither is true, reject the briefing with a structured error naming the offending bullet and path, in the same shape `_validate_briefing_structure` already uses for check #7.

Current focus: end-to-end automation — axes 1 (manual-approval) and 2 (failure-recovery) rely on the briefing-validation gate to prevent dispatch of briefings whose verification bullets the per-task verifier can't honestly evaluate. TB-235 closed the dependency-coherence axis (predecessor naming). This task closes the symmetric file-path-coherence axis (verification bullets reference real or to-be-created files), so the verifier never has to fall back to renaming an existing artifact to satisfy a bullet.

Why now: TB-239's verification cycle on 2026-05-16 wasted 2 retries (~$2 token spend across `bd1dd62` + the second-retry run) AND ended with a cross-task side-effect — commit `ccfcff1` renamed `ap2/tests/test_tb234_doctor_auto_approve.py` → `test_tb234_doctor_auto_approve_audit.py` purely to make the briefing's wrong-path bullet (`uv run pytest -q ap2/tests/test_tb234_doctor_auto_approve_audit.py` — TB-234 tests stay green) pass. Without this gate, the recurring class is: ideation invents a verification bullet citing a plausible-but-nonexistent file path; operator approves on structural grounds (Goal/Scope/Why-now/Design all valid); agent commits correct work then either retries pointlessly or renames an existing artifact to satisfy the bullet. The TB-235 LLM-judge primitive already exists; the prompt-change cost is bounded.

## Scope

- New helper `_validate_briefing_file_path_coherence(briefing: str, cfg, ...)` in `ap2/tools.py`, mirroring `_validate_briefing_dependency_coherence`'s SDK-call shape (model selection via the same `AP2_BRIEFING_JUDGE_MODEL` env override, max_turns via the same `AP2_BRIEFING_JUDGE_MAX_TURNS` knob, structured-output `{"verdict": "pass"|"fail", "offending_bullets": [{"bullet": "...", "path": "...", "reason": "..."}, ...]}`).
- Wire into `_validate_briefing_structure` as check #8, immediately after check #7.
- Judge prompt: load full briefing + give the SDK call Read / Glob tools scoped to `cfg.project_root` so it can independently verify each path; instruct the judge to classify each referenced path as `exists | to_be_created_per_scope | missing` and reject when any is `missing`.
- Skip-shape: when `AP2_BRIEFING_JUDGE_DISABLED=1` (same env knob that gates check #7), file-path-coherence is also skipped — keeps offline/dev shells working.
- Graceful degradation: on SDK call failure (network / quota / timeout), the helper returns a pass-with-warning rather than blocking the queue-append (mirrors TB-235's check #7 fallback behavior).
- Tests: new `ap2/tests/test_tb240_briefing_validator_file_path_coherence.py` covers (1) bullet with path that exists in HEAD → pass; (2) bullet with path named in `## Scope` as to-be-created → pass; (3) bullet with path missing from both → reject with structured error; (4) skip-knob (`AP2_BRIEFING_JUDGE_DISABLED=1`) honored; (5) TB-239-shape regression fixture (briefing with `pytest -q ap2/tests/test_tb234_doctor_auto_approve_audit.py` against a HEAD missing that path → reject).
- Document the new check in `ap2/howto.md`'s briefing-validator section alongside TB-235's check #7.

## Design

Pure structural reuse of TB-235's `_validate_briefing_dependency_coherence` SDK-judge primitive — same `sdk.query` shape, same Haiku-4.5 default, same env-knob skip, same graceful-degradation fallback. The only meaningful difference is the judge prompt: instead of asking about predecessor naming, ask about file-path existence.

Path extraction stays in the judge prompt rather than Python regex — the LLM judge has Read / Glob tools to enumerate paths from bullet text and check each one against the working tree, avoiding a fragile shell-grammar parser.

## Verification

- `uv run pytest -q ap2/tests/test_tb240_briefing_validator_file_path_coherence.py` — new test module exists and all five behavioral cases pass.
- `uv run pytest -q ap2/tests/test_tools.py` — full `tools.py` suite stays green (no regression on check #7 or earlier checks).
- `uv run pytest -q ap2/tests/test_docs_drift.py` — docs-drift gate stays green; the new check's howto.md mention satisfies the gate.
- `grep -n "_validate_briefing_file_path_coherence" ap2/tools.py` — helper definition present.
- `grep -n "AP2_BRIEFING_JUDGE_DISABLED" ap2/tools.py` — the new helper honors the same skip-knob as check #7.
- `grep -n "file-path-coherence\|file_path_coherence" ap2/howto.md` — howto.md mentions the new check.
- Prose: `_validate_briefing_structure` in `ap2/tools.py` invokes the new helper as check #8 in sequence after check #7, and the SDK judge's reject shape names the offending bullet plus the missing path as a structured error mirroring check #7's reject shape.
- Prose: `_validate_briefing_file_path_coherence` in `ap2/tools.py` falls back to a structured pass-with-warning when the SDK call fails (network / quota / timeout), so the validator stays usable in offline test environments — mirrors TB-235 check #7's graceful-degradation behavior.

## Out of scope

- Catching file-path drift in `## Goal` / `## Scope` / `## Design` prose — this check applies only to `## Verification` shell bullets (those are the bullets the verifier gates on).
- Auto-rewriting offending bullets — operator/proposer decides whether to fix the bullet or expand `## Scope`.
- Detecting "agent renames an existing file outside `## Scope`" at verify time — that's a downstream-detection complement to this upstream-prevention; track separately if rename-side-effect recurs after this gate lands.
- Batching check #7 + check #8 into a single SDK call per queue-append for cost reduction — premature; defer to a follow-up once both checks are live and per-append cost is observed.
