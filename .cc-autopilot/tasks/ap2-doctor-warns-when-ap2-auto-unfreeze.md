# TB-239 — `ap2 doctor` warns when `AP2_AUTO_UNFREEZE_DRY_RUN=1` is set but `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty (axis-2 misconfiguration floor)

## Goal

Current focus: end-to-end automation — TB-234 (`f350824`,
2026-05-16T01:39:02Z) shipped `auto_approve_audit()` in
`ap2/doctor.py` to catch the axis-1 misconfiguration shape
(`AP2_AUTO_APPROVE=1` with token caps unset → unbounded
blast radius). Axis 2 has a symmetric misconfiguration
shape: `_maybe_auto_unfreeze` (daemon.py:3301-3303) early-
returns silently when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is
unset/empty — `if not allowlist: return` — and that branch
fires BEFORE the dry-run check (`_auto_unfreeze_dry_run()` at
line 3416 is downstream of the allowlist gate). So an
operator who sets `AP2_AUTO_UNFREEZE_DRY_RUN=1` expecting to
observe the auto-unfreeze sweep's decisions on their live
Frozen set gets a silent no-op: zero `would_auto_unfreeze`,
zero `auto_unfreeze_skipped`, no doctor warning. The dry-run
knob is observation-useless without the allowlist.

This task adds `auto_unfreeze_audit()` to `ap2/doctor.py`
mirroring the TB-234 `auto_approve_audit()` structure: WARN
when the dry-run knob is set without the allowlist (the
silent-no-op misconfiguration); INFO summary lines for the
default-off case and the correctly-configured-on case. Wire
the new audit into `diagnose()` as a new section
("auto-unfreeze safety floor", matching TB-234's
"auto-approve safety floor" naming).

Why now: TB-233 (`74bd793`, 2026-05-16T01:32:05Z) just shipped
`AP2_AUTO_UNFREEZE_DRY_RUN`; TB-234 just shipped the axis-1
doctor audit. Without this symmetric mirror on axis 2, the
first operator who flips `AP2_AUTO_UNFREEZE_DRY_RUN=1`
without realizing the allowlist is also required hits a
silent-no-op and assumes the feature is broken — eroding
trust in the on-ramp model TB-232/233 just established. The
TB-234 approval at 01:03:11Z establishes the operator's
precedent that pre-flight misconfiguration warnings on
opt-in automation knobs are in-scope; mirroring that
pattern to axis 2 closes the symmetry gap.

## Scope

(1) Add `auto_unfreeze_audit() -> AuditResult` to
`ap2/doctor.py`, structurally parallel to `auto_approve_
audit()` (line 130). Cases to emit:

  - INFO when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty
    AND `AP2_AUTO_UNFREEZE_DRY_RUN` is unset: "auto-unfreeze
    disabled (allowlist unset)" — the default-off case, no
    further action needed.
  - WARN when `AP2_AUTO_UNFREEZE_DRY_RUN` is set (truthy)
    AND `AP2_AUTO_UNFREEZE_FIX_SHAPES` is unset/empty:
    "auto-unfreeze dry-run set without allowlist —
    silent no-op". Body text names the daemon.py early-
    return location and the fix: set
    `AP2_AUTO_UNFREEZE_FIX_SHAPES=<comma-list>` before
    dry-run will emit observable decisions.
  - INFO when both knobs are set correctly
    (`AP2_AUTO_UNFREEZE_FIX_SHAPES` non-empty +
    `AP2_AUTO_UNFREEZE_DRY_RUN=1`): "auto-unfreeze dry-run
    armed: <N shapes>, per-task cap <N>, per-day cap <N>"
    — confirms the operator-flipped configuration is the
    intended observability state.
  - INFO when `AP2_AUTO_UNFREEZE_FIX_SHAPES` is non-empty
    AND `AP2_AUTO_UNFREEZE_DRY_RUN` is unset: "auto-
    unfreeze live: <N shapes>, per-task cap <N>, per-day
    cap <N>" — the production-mode state.

(2) Wire the new audit into `diagnose()` as a new
`report.sections.append(("auto-unfreeze safety floor",
auto_unfreeze_audit()))` line directly after the existing
TB-234 line (`report.sections.append(("auto-approve safety
floor", auto_approve_audit()))` at line 212). Keep the
section names parallel so an operator scanning the report
sees axis-1 + axis-2 misconfiguration floors as a paired
unit.

(3) Tests: add `ap2/tests/test_tb239_doctor_auto_unfreeze_
audit.py` with four cases covering the four branches above
(default-off, dry-run-without-allowlist WARN,
dry-run-with-allowlist INFO, live-mode INFO). Match the
fixture / monkeypatch / assertion style of the existing
TB-234 tests in
`ap2/tests/test_tb234_doctor_auto_approve_audit.py` (if
that file exists; otherwise mirror the closest TB-234
test pattern).

(4) Howto.md: add a short paragraph near the existing
`AP2_AUTO_UNFREEZE_DRY_RUN` docs (L967-997) mentioning the
new doctor warning as the operator-facing pre-flight
diagnostic for the silent-no-op misconfiguration.

## Design

Structural mirror of TB-234. The audit function reads env
vars directly (no daemon-runtime calls), uses the existing
`AuditResult` / WARN / INFO emission shape, and wires into
`diagnose()` as a new section. No production-runtime
changes — purely a pre-flight diagnostic surface; the
silent-no-op early-return in `_maybe_auto_unfreeze` stays as
designed (it's the right behavior for the feature's
master-switch contract per the docstring at line 3286-3291).

Naming: "auto-unfreeze safety floor" matches TB-234's
"auto-approve safety floor" — operator scanning `ap2 doctor`
output sees axis-1 + axis-2 misconfiguration floors as
adjacent sections with parallel naming. The asymmetry vs
TB-234 (which warns on unbounded cost via missing token
caps, not silent no-op) is intentional: axis-1 defaults are
permissive (caps default to 0 = disabled = unbounded),
axis-2 defaults are conservative (allowlist defaults to
empty = no-op; caps default to 1/3). The misconfiguration
shapes differ; the doctor warnings reflect that.

No new env knobs. No production-runtime gating. The audit
is observation-only.

## Verification

- `uv run pytest -q ap2/tests/test_tb239_doctor_auto_unfreeze_audit.py` — new test module exists and all four behavioral cases pass.
- `uv run pytest -q ap2/tests/test_tb234_doctor_auto_approve_audit.py` — TB-234 tests stay green (no regression on the parallel auto-approve audit).
- `uv run pytest -q ap2/tests/` — full suite green vs current baseline.
- `test -f ap2/tests/test_tb239_doctor_auto_unfreeze_audit.py` — test module present on disk.
- `grep -nE "def auto_unfreeze_audit" ap2/doctor.py` — new audit function declared.
- `grep -nE "auto-unfreeze safety floor" ap2/doctor.py` — new section name wired into `diagnose()`.
- `grep -nE "AP2_AUTO_UNFREEZE_DRY_RUN" ap2/doctor.py` — env var read by the new audit.
- `grep -nE "AP2_AUTO_UNFREEZE_FIX_SHAPES" ap2/doctor.py` — env var read by the new audit.
- `grep -nE "silent no-op|silent-no-op" ap2/doctor.py` — WARN body text names the misconfiguration shape.
- `grep -nE "AP2_AUTO_UNFREEZE_DRY_RUN" ap2/howto.md` — howto.md still references the knob (regression-pin).
- Prose: `ap2/doctor.py` Prose: `auto_unfreeze_audit()` is structurally parallel to `auto_approve_audit()` — reads env vars directly (no runtime imports), returns an `AuditResult` with one INFO line per branch + one WARN line for the dry-run-without-allowlist case; judge confirms by reading the function body and comparing structure to `auto_approve_audit()`.
- Prose: `ap2/doctor.py` Prose: `diagnose()` appends the new `auto-unfreeze safety floor` section directly after the existing `auto-approve safety floor` section (axis-pairing preserved in operator output ordering); judge confirms by reading the diagnose body.

## Out of scope

- Production-runtime change to the silent-no-op early-return
  in `_maybe_auto_unfreeze` — the silent return is by-design
  per the existing docstring; this task is observability-
  only.
- New env knobs — TB-225 / TB-233 shipped the existing
  knobs; this task is doctor-only.
- Mattermost / status-report surface for the same warning —
  `ap2 doctor` is the operator's pre-flight diagnostic
  surface; status-report covers ongoing runtime state.
  Defer cross-surface bundling to a separate cycle if the
  warning isn't visible enough.
- Validation of allowlist contents (typo'd shape names that
  don't match any `parse_blocked_summary_fix_shape` shape)
  — a separate misconfiguration shape; defer to a follow-up
  cycle if operator engagement surfaces typo-shaped
  failures.
