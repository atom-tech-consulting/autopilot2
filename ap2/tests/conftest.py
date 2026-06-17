"""Top-level pytest conftest for `ap2/tests/` — judge-shield default
plus cross-module CLI test helpers (TB-266).

TB-266 (the second responsibility): centralize the `_project(tmp_path)`
+ `_drain(cfg)` helpers that every cli-prefixed test module uses
(`test_cli_daemon.py`, `test_cli_board.py`, `test_cli_review.py`,
`test_cli_diagnostic.py`, `test_cli.py`). The pre-TB-266 layout
defined them once at the top of the monolithic `test_cli.py` and
relied on file-local scope; once the file split into siblings, each
new module needed access to them. Co-locating here (sibling to the
new modules) lets every module do a simple
`from ap2.tests.conftest import _project, _drain` without sibling-
to-sibling imports. The helpers are intentionally plain functions
(not pytest fixtures) so the relocated test bodies remain
literally identical to their pre-TB-266 originals — fixture
parameters would force a signature edit in ~120 tests, which the
TB-266 briefing's "identical body" rule explicitly forbids.

TB-254 (surgical mirror of `ap2/tests/e2e/conftest.py`'s shield, line 66):
set `AP2_VALIDATOR_JUDGE_DISABLED=1` by default for the entire unit-test
session. Without this shield, any unit test that exercises
`tools.do_board_edit({"action": "add_*"})` or
`tools.do_operator_queue_append({"op": "add_*"})` would dispatch real
Haiku-4.5 SDK calls per invocation via TB-235's
`_check_dependency_coherence` (check #7 of
`_validate_briefing_structure`). That is expensive in cumulative test
wall-clock (10-18s per call; n=18 of the top-20 slowest tests in
TB-253's investigation artifact were dominated by this leak) and
potentially makes live API calls from CI.

The shield is the smallest-blast-radius fix identified by TB-253's
investigation at `.cc-autopilot/insights/test-suite-slowness-2026-05-17.md`
(Option 1 in the headline finding). The e2e directory already had its
own shield in `ap2/tests/e2e/conftest.py` for the same reason — this
top-level conftest is the surgical mirror for the unit-test surface.

Why `os.environ.setdefault` rather than direct assignment: an operator
who wants to verify the validator IS firing locally can override via
the shell (`AP2_VALIDATOR_JUDGE_DISABLED=0 uv run pytest -q ap2/tests/`)
without editing this file. Direct assignment would shadow operator
intent silently.

Why module-level (import-time) rather than a session-scoped autouse
fixture: pytest imports conftest.py once per session before collecting
tests, so the env var is set before any test or fixture runs. An
autouse fixture only activates on first test invocation — same effect
in practice, but the import-time form matches the existing
`e2e/conftest.py` pattern exactly and skips one layer of indirection.

The two intentional-judge-exercising modules
(`ap2/tests/test_dep_validator_judge.py` and
`ap2/tests/test_tb243_validator_judge_surface.py`) remain free to
override the shield per-test via `monkeypatch.delenv`. The shield is
the safe default; the override is the explicit opt-in for the
modules that test the judge itself.
"""
from __future__ import annotations

import os
from pathlib import Path

# Surgical mirror of `ap2/tests/e2e/conftest.py`'s shield. Set as the
# session default so every unit test under `ap2/tests/` inherits the
# shield. Tests that need the judge to fire override via
# `monkeypatch.delenv("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", raising=False)`.
#
# TB-413: the shield now installs the SECTIONED env name
# (`AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED` → the
# `components.validator_judge.disabled` knob). The flat
# `AP2_VALIDATOR_JUDGE_DISABLED` override path was removed in TB-413
# (config.toml is the sole source for behavioral tunables; env is
# consulted only for the secrets + deployment-identity allowlist), so
# `_validator_judge_disabled(cfg)` — which routes through
# `cfg.get_component_value("validator_judge", "disabled")` — no longer
# honors the flat name. Setting the flat name here would silently leave
# the dep-coherence judge ENABLED across the whole unit-test session and
# dispatch real Haiku-4.5 SDK calls per `add_*` invocation. The
# sectioned name is the one `get_component_value` still consults (at
# highest precedence), so the shield keeps working post-TB-413.
#
# TB-333 exemption (carried forward): the shield runs at conftest import
# time — pytest hasn't constructed any project's `Config` yet, so a
# cfg-routed read has no Config to consult; the env read here is the right
# resolution surface (the one intentional cross-package env read of the
# validator_judge cluster). Post-TB-413 the only change is the knob NAME:
# the sectioned `AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED` instead of the
# retired flat `AP2_VALIDATOR_JUDGE_DISABLED`. The operator opts out via
# `AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED=0 uv run pytest` (sectioned),
# not via a TOML edit, so `setdefault` (not direct assignment) preserves
# operator intent.
#
# Edge case: shells that `export AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED=`
# (no value, empty string) make the key present in `os.environ` so
# `setdefault` would leave the empty string alone and the shield
# wouldn't take effect (the truthy test rejects the empty string and
# fires the judge anyway). Treat an unset OR empty value as "operator
# did not opt out" — strip it first so `setdefault` then installs the
# shield value. Any other operator-set value (e.g. `0` to opt out and
# verify the judge fires locally) is preserved untouched.
if not os.environ.get("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", "").strip():
    os.environ.pop("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", None)

# `setdefault` preserves the operator-shell override
# (`AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED=0 uv run pytest -q ap2/tests/`)
# so a local "did the judge actually fire?" check is one knob away.
os.environ.setdefault("AP2_COMPONENTS_VALIDATOR_JUDGE_DISABLED", "1")


# --- TB-266: cross-module CLI test helpers --------------------------------
#
# Used by every cli-prefixed test sibling (`test_cli_daemon.py`,
# `test_cli_board.py`, `test_cli_review.py`, `test_cli_diagnostic.py`)
# plus the slimmed `test_cli.py`. See module docstring above for the
# rationale on "plain functions, not pytest fixtures".


def _project(tmp_path: Path):
    """Initialize a fresh ap2 project under `tmp_path` and return its
    `Config`. Most CLI tests open with `cfg = _project(tmp_path)`."""
    from ap2.config import Config
    from ap2.init import init_project

    init_project(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _drain(cfg) -> dict:
    """Apply pending operator-queue ops as the daemon's `_tick` would.

    Tests that exercise cmd_backlog / cmd_unfreeze / cmd_delete / cmd_add
    use this to advance from "queued" to "applied" state — the CLI
    commands themselves are deferred (TB-131).
    """
    from ap2 import tools
    return tools.drain_operator_queue(cfg)


# --- TB-267: cross-module web test helpers --------------------------------
#
# Used by the web-prefixed test siblings (`test_web_home.py`,
# `test_web_events.py`, `test_web_tasks.py`, `test_web_chrome.py`,
# `test_web_insights.py`, `test_web_usage.py`, and the slimmed
# `test_web.py`). Same rationale as the TB-266 CLI helpers above:
# co-locating the fixture and shared synthesizers here lets every
# new web test module use them via pytest auto-discovery without
# sibling-to-sibling imports. The `project` fixture's body and the
# `_seed_run` / `_seed_vf_event` helpers are byte-identical to the
# pre-TB-267 originals from `test_web.py` so the relocated test
# bodies remain unchanged.

import pytest


@pytest.fixture
def project(tmp_path: Path):
    """Synthesize a fresh ap2 project with a seeded board + events.

    Mirrors the original `project` fixture from `ap2/tests/test_web.py`
    pre-TB-267; relocated here so every web-prefixed test sibling can
    pull it in via auto-discovery.
    """
    from ap2 import events as ev_mod
    from ap2.config import Config

    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n- [ ] **TB-1** **Active task**\n"
        "## Ready\n\n"
        "## Backlog\n\n- [ ] **TB-2** **Backlog task** `#tag`\n"
        "## Complete\n\n- [x] **TB-3** **Done thing** — summary text\n"
        "## Frozen\n\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    ev_mod.append(cfg.events_file, "daemon_start")
    ev_mod.append(cfg.events_file, "task_complete", task="TB-3", status="complete",
                  commit="abc12345", summary="finished it")
    ev_mod.append(cfg.events_file, "task_error", task="TB-2", error="ValueError: x")
    ev_mod.append(cfg.events_file, "ideation_empty_board", cooldown_s=7200)
    return cfg


def _seed_run(
    project,
    *,
    run_id: str,
    rows: list,
    full_rows: list | None = None,
    prompt: str = "system prompt body…\n\nUser: do the thing.",
):
    """Synthesize a debug-dump triple on disk — mirror of `_prep_debug_dumps`."""
    import json as _json

    d = project.project_root / ".cc-autopilot" / "debug"
    d.mkdir(parents=True, exist_ok=True)
    prompt_p = d / f"{run_id}.prompt.md"
    stream_p = d / f"{run_id}.stream.jsonl"
    messages_p = d / f"{run_id}.messages.jsonl"
    prompt_p.write_text(prompt)
    stream_p.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    messages_p.write_text(
        "\n".join(_json.dumps(r) for r in (full_rows or rows)) + "\n"
    )
    return prompt_p, stream_p, messages_p


def _seed_vf_event(
    project,
    *,
    task: str = "TB-VF",
    pass_n: int = 5,
    fail_bullets: list | None = None,
    unverified_n: int = 1,
) -> None:
    """Append a synthetic `verification_failed` event whose criteria list
    matches the briefing's expected shape (kind, status, bullet, notes)."""
    from ap2 import events as ev_mod

    fails = fail_bullets or []
    criteria = (
        [
            {"kind": "shell", "status": "pass", "bullet": f"shell pass #{i}",
             "notes": ""}
            for i in range(pass_n)
        ]
        + [
            {"kind": k, "status": "fail", "bullet": b, "notes": n}
            for (k, b, n) in fails
        ]
        + [
            {"kind": "prose", "status": "unverified",
             "bullet": f"prose unv #{i}", "notes": "skip"}
            for i in range(unverified_n)
        ]
    )
    ev_mod.append(
        project.events_file, "verification_failed",
        task=task, kind="per_task", overall="fail", criteria=criteria,
    )
