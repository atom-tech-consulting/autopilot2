---
title: "`ap2 doctor` warns when `AP2_AUTO_APPROVE=1` is set but token caps are unset (axis-3 misconfiguration-floor)"
tags: ["#autopilot", "#doctor", "#automation", "#operator-surface", "#safety", "#cost", "#regression-pin"]
---

## Goal

Add an `auto_approve_audit` section to `ap2/doctor.py` that emits a WARN line when `AP2_AUTO_APPROVE=1` is set in the daemon's environment but `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` and/or `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP` is unset/empty/zero. The **Current focus: end-to-end automation** (goal.md L38-151, axis 3 "Cost and blast-radius guards", L102-113) explicitly names auto-approval as the surface that "shifts the bottleneck from operator judgment to system safety" and frames cost ceilings + regression pauses + unscheduled-failure detection as the three safety-floor primitives. TB-224 shipped per-task + per-window token caps, but the implementation in `_per_task_token_cap` / `_window_token_cap` (ap2/daemon.py:2581-2614) returns 0 ("cap disabled") when the env var is unset by deliberate design — there's no fail-loud surface today telling an operator "you enabled auto-approve but left the caps unset, the safety floor is OFF." The doctor surface is the natural place: a one-shot readiness check the operator runs before walking away.

Why now: TB-227 surfaced auto-approve state in `ap2 status` (a continuous surface), but `ap2 status` is a high-noise board snapshot the operator may not read carefully after toggling environment variables on a remote sandbox. The doctor surface is purpose-built for the "did I configure this correctly?" pre-flight question — an operator who runs `ap2 doctor` after flipping `AP2_AUTO_APPROVE=1` should see immediately whether their cost-cap configuration matches the cost-bounded shape goal.md L86-88 requires for the auto-approve mode to ship value. Without this, an operator can enable auto-approve, forget the caps, and only learn the safety floor was off when an SDK bill arrives or the cumulative-regression pause fires after N freezes (TB-224's downstream catch). Pre-flight beats post-incident.

## Scope

1. Add `auto_approve_audit() -> AuditResult` function in `ap2/doctor.py` (sibling to existing `_ap2_installed_for_user` / `_project_init_state` / `_user_audit` / etc.). The function reads the calling process's env (not the sandbox user's — `AP2_AUTO_APPROVE` is read by the daemon at runtime, and the daemon inherits the user's login-shell env via the sandbox-init mechanism, so an `os.environ`-based check on the operator's shell-env is the right scope for a doctor's pre-flight signal). If a more accurate probe exists via `sudo -u <user> -i <shell> -c env` (mirroring `_ap2_installed_for_user`'s shell-probe pattern at doctor.py:46-69), use that instead so the report reflects what the daemon will see when started from the sandbox user's normal shell. Pick whichever matches doctor.py's existing convention.
2. Audit logic: (a) if `AP2_AUTO_APPROVE` is unset / empty / not in `{"1", "true", "yes"}` (case-insensitive) → `INFO` line "auto-approve disabled (AP2_AUTO_APPROVE unset) — manual approve required per task" and return; (b) if `AP2_AUTO_APPROVE=1` → check `AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP` and `AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP`: each parses as a positive int → `OK` line per cap; each is unset/empty/zero/non-integer → `WARN` line per cap with the message naming the env var + a one-line fix command (`export AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP=<budget>` etc.); (c) when BOTH caps are unset → an additional summary `WARN` line stating "auto-approve enabled with no cost ceiling — safety floor OFF; see goal.md L102-113 for rationale".
3. Wire the new section into `DoctorReport.sections` in the function that assembles the report (matching the existing `(title, AuditResult)` tuple pattern at doctor.py:136). Section title: `"auto-approve safety floor"`.
4. Add `ap2/tests/test_tb234_doctor_auto_approve.py` with at minimum: (a) `AP2_AUTO_APPROVE` unset → INFO line emitted, no WARN; (b) `AP2_AUTO_APPROVE=1` + both caps unset → 3 WARN lines (per-task cap, window cap, summary); (c) `AP2_AUTO_APPROVE=1` + per-task cap set + window cap unset → 1 WARN (window cap only) + 1 OK (per-task cap); (d) both caps set → both OK + no summary WARN; (e) `AP2_AUTO_APPROVE=true` (case-insensitive parse) is recognized; (f) cap value `"0"` is treated as unset (mirrors `_per_task_token_cap`'s `v > 0 else 0` semantics at daemon.py:2596 / 2614). Use `monkeypatch.setenv` / `delenv`; mirror the structural shape of `ap2/tests/test_doctor.py` if it exists.
5. Update `ap2/howto.md`'s `ap2 doctor` documentation section (if one exists; otherwise the env-knobs reference section) to mention the new audit + name the WARN-trigger conditions verbatim. One-paragraph addition; cross-link to goal.md L102-113.

## Design

- WARN, not FAIL: the operator may have deliberately enabled auto-approve without caps for a short experimental window. Doctor reports should never refuse to run the daemon (FAIL gates startup); WARN surfaces the misconfiguration without blocking the operator's stated intent. Matches goal.md L184-186's "operator-curated trust upgrades" framing — doctor warns, doesn't second-guess.
- Idempotent / pure: the audit function does not write events. Doctor is a one-shot read-only diagnostic; an `events.append` would create noise in `events.jsonl` every time someone runs `ap2 doctor`. The WARN visibility is via the printed report only.
- The check uses the same parse semantics as `_per_task_token_cap` (daemon.py:2581-2596) and `_window_token_cap` (2599-2614): missing / empty / non-integer / non-positive → "disabled". Mismatched semantics between doctor and daemon would mislead the operator more than no check at all.
- Doctor's existing `AuditResult` shape supports `OK` / `FAIL` / `WARN` / `INFO` (verified via sandbox.py:144-156). The new section uses WARN as the actionable signal; `report.ok` stays True (doctor doesn't fail on a WARN per existing convention).
- Out of scope: a similar doctor check for `AP2_AUTO_UNFREEZE_FIX_SHAPES` + `AP2_AUTO_UNFREEZE_MAX_PER_DAY` etc. — the auto-unfreeze caps default to 1/3 (per-task/per-day) at the helper level, so a missing env knob is already cap-bounded by definition; the auto-approve case is the unique unbounded-default surface.

## Verification

- `uv run pytest -q ap2/tests/test_tb234_doctor_auto_approve.py` — new test module exists and all six behavioral cases pass.
- `uv run pytest -q ap2/tests/` — full regression suite green.
- `grep -nE "auto_approve_audit|auto-approve safety floor" ap2/doctor.py` — function + section title present.
- `grep -nE "AP2_AUTO_APPROVE_PER_TASK_TOKEN_CAP|AP2_AUTO_APPROVE_WINDOW_TOKEN_CAP" ap2/doctor.py` — both env knob names referenced.
- `grep -nE "auto-approve|AP2_AUTO_APPROVE" ap2/howto.md` — howto.md mentions the doctor check.
- `ap2/doctor.py` Prose: the doctor.py module exports the `auto_approve_audit` function and the assembled `DoctorReport` includes the section under the title "auto-approve safety floor"; judge confirms via Read.

## Out of scope

- Refusing to start the daemon (FAIL gate) when caps are unset — operator authority preserved per goal.md L184-186; WARN is the right level.
- A sibling doctor check for `AP2_AUTO_UNFREEZE_*` caps — auto-unfreeze caps default to 1/3 at the helper level, not 0/disabled, so the unbounded-default failure mode is unique to auto-approve.
- Emitting events on doctor runs — doctor is read-only diagnostic; no events.jsonl writes.
- Auto-suggesting a recommended cap value — operator owns budgeting decisions; the WARN names the env var, not a value.
- A status-report cron echo of the doctor warning — TB-227's `automation_status` already surfaces cap state continuously in the status digest; doctor is the pre-flight surface, status is the continuous one.
