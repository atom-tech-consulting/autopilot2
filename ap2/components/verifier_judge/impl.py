"""verifier_judge component implementation (TB-382, axis 5).

Holds the optional LLM prose-bullet judge relocated from the core verify
runner (`ap2/verify.py`'s pre-TB-382 `_judge_prose_bullet` at L470). The
core `verify.py::verify_task` keeps parsing the `## Verification` section,
running the deterministic shell bullets, and aggregating verdicts —
verification is *gating*, so that path stays in core. Only the
SDK-call-bearing prose path moves here and is reached through the
registry (`Registry.verifier_judge(cfg)`), so a deployment can verify
with shell bullets alone by disabling this component via
`AP2_VERIFY_JUDGE_DISABLED` (the manifest's suppress-polarity env_flag,
mirroring `validator_judge`).

Structural-only extraction: the judge's signature, cumulative-diff
resolution, Read/Glob/Grep allowed-tools, the verify-judge knob reads
(`AP2_VERIFY_JUDGE_EFFORT` / `AP2_VERIFY_JUDGE_MAX_TURNS` via
`cfg.get_core_value(...)`), and the `judge_call` event emission are
preserved bit-for-bit from the pre-TB-382 core body. The
deterministic helpers the judge composes with — `_parse_judge_response`,
the `_ParseOutcome` / parse-error categorization surface, and the
`JUDGE_REPO_READ_TOOLS` / `CriterionResult` / `VerifyBullet` types —
remain in core (`ap2/verify.py`) and are imported here; this is the
normal component→core direction (the TB-311 gate only forbids the
reverse).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ap2.verify import (
    JUDGE_REPO_READ_TOOLS,
    CriterionResult,
    VerifyBullet,
    _parse_judge_response,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ap2.config import Config


async def _judge_prose_bullet(
    bullet: VerifyBullet,
    *,
    project_root: Path,
    sdk,
    diff_text: str,
    events_file: Path | None = None,
    task_id: str | None = None,
    bullet_idx: int | None = None,
    cfg: "Config | None" = None,
) -> CriterionResult:
    """Ask the SDK whether `bullet.text` is satisfied by `diff_text` plus the
    working tree at HEAD.

    The judge gets two evidence sources:

      1. ``diff_text`` — the cumulative diff across all task-id commits
         (TB-136). Reasoning over a diff is fast and catches most cases.
      2. ``Read``/``Glob``/``Grep`` tools scoped to ``project_root`` — the
         judge can confirm a test/symbol actually exists in HEAD before
         declaring it missing. This is the authoritative check when the
         diff is ambiguous (file moved, symbol renamed, or the diff was
         truncated). TB-136.

    Asks for a structured one-line JSON response; falls back to ``unverified``
    on parse failure rather than failing the whole verification (the prose
    judge is best-effort).

    TB-157: when ``events_file`` is provided, emits a ``judge_call`` event
    on each judge SDK call carrying usage / model / cost / verdict so
    cost-tradeoff experiments can aggregate per-judge token spend without
    routing through the daemon's `_log_message` (the judge has its own
    SDK loop that bypasses that capture path).
    """
    prompt = (
        "You are evaluating ONE acceptance bullet from a task's verification "
        "section against the agent's CUMULATIVE diff (every code commit "
        "across any retries of this task, with daemon state-file noise "
        "filtered out) AND the project's working tree at HEAD.\n\n"
        # TB-236: tightened final-message contract. The pre-TB-236 prompt
        # asked for "ONE LINE of JSON" but did not cap rationale length,
        # did not forbid markdown code fences, and did not show an
        # explicit example. Observed failure (TB-228 bullet 7) was a
        # 1100-token response with a long rationale containing unescaped
        # JSON-breaking characters; bullet 6 from the same task succeeded
        # at ~510 tokens with a short rationale. The shorter the
        # rationale, the smaller the surface area for JSON-escape bugs.
        # The constraint applies ONLY to the FINAL message — intermediate
        # Read/Grep tool calls (legal via JUDGE_REPO_READ_TOOLS) are
        # unconstrained.
        "OUTPUT CONTRACT — your FINAL message must be a JSON object only:\n"
        '  {"status": "pass", "rationale": "X exists per L42"}\n'
        "Rules for the FINAL message:\n"
        "  - It is a JSON object only. No markdown code fences (no ```json"
        " or ``` wrapping). No leading prose (no 'Here is the verdict:'"
        " preamble). No trailing commentary after the closing brace.\n"
        "  - `status` is exactly `\"pass\"` or `\"fail\"` (lowercase).\n"
        "  - `rationale` is a single short sentence, MAXIMUM 200 characters."
        " Cite a file:line or symbol name when possible; do NOT quote long"
        " code blocks or paste diff hunks into the rationale.\n"
        "  - If the rationale would naturally exceed 200 characters,"
        " summarize: cite the strongest single piece of evidence and"
        " stop.\n"
        "  - Intermediate tool calls (Read, Glob, Grep) during reasoning"
        " are unconstrained — only the FINAL message must satisfy this"
        " contract.\n\n"
        "Evidence priority — when the diff and the working tree disagree, "
        "the working tree at HEAD is AUTHORITATIVE. The diff can be "
        "truncated, span renames, or simply not show what's actually on "
        "disk after a multi-retry sequence. You have Read, Glob, and Grep "
        "tools scoped to the project root; before declaring a test or "
        "symbol or file missing, USE Grep/Glob to confirm it isn't present "
        "in HEAD under a different name or path. If you can find the "
        "asserted test/symbol/file in the working tree (Read it to verify "
        "shape if needed), the bullet PASSES regardless of whether the "
        "diff makes that obvious.\n\n"
        f"Bullet:\n  {bullet.text}\n\n"
        # TB-156: diff cap lowered from 100KB → 30KB. Most cumulative
        # diffs land in the 5-30KB range; the prior 100KB worst-case-
        # defensive cap was paying ~70KB of judge tokens per bullet for
        # padding. The judge has Read/Glob/Grep (TB-136) and the prompt
        # tells it the working tree at HEAD is authoritative — when the
        # truncated tail matters, it can pull what it needs directly. So
        # the cap is now a soft hint rather than a hard wall, traded
        # against ~50% judge-token savings on average. Operators wanting
        # a different cap can edit the source.
        f"Cumulative diff:\n```\n{diff_text[:30_000]}\n```\n"
    )
    # TB-334 (axis 5 core cluster): `cfg` lets us resolve the
    # agent-runtime core knobs (`agent_model`, `agent_effort`,
    # `verify_judge_max_turns`) through `Config.get_core_value`'s
    # sectioned-env > flat-env > TOML > default precedence chain
    # rather than direct `os.environ.get`. Callers that don't thread
    # `cfg` (pre-TB-334 tests, harness paths without a project root)
    # synthesize one via `Config.load(project_root)` — the same
    # back-compat shape pre-migration env-reads gave them. The
    # synthesized cfg's env-first precedence preserves the
    # `monkeypatch.setenv(...)` idiom every existing test uses.
    if cfg is None:
        from ap2.config import Config as _Config
        cfg = _Config.load(project_root)
    try:
        # TB-156: per-call-site effort knob. The judge's job — read a diff,
        # optionally Grep/Read for confirmation, emit a one-line JSON
        # verdict — doesn't need the multi-step reasoning budget that
        # `xhigh` is sized for. Default to `high` here so the judge runs
        # cheaper than task agents (which stay on the global default,
        # `xhigh`); operators can still pin a specific value via
        # `AP2_VERIFY_JUDGE_EFFORT`, or globally via `AP2_AGENT_EFFORT`.
        # Precedence: per-site env > global env > per-site default.
        # TB-339 (axis-5 cleanup): the per-site `verify_judge_effort`
        # layer is now resolved through `cfg.get_core_value(...)` too —
        # the `or`-chain collapses the empty-string default to the
        # global `agent_effort` fallback, preserving the original
        # `per-site env > global env > per-site default` precedence
        # exactly (sectioned env > flat env > TOML > "" > sectioned
        # env > flat env > TOML > "high"). FLAT_TO_SECTIONED already
        # maps `AP2_VERIFY_JUDGE_EFFORT` → `core.verify_judge_effort`.
        effort = cfg.get_core_value("verify_judge_effort", default="") \
            or cfg.get_core_value("agent_effort", default="high")
        # The judge can take a few tool roundtrips (Grep → Read) before
        # emitting its final verdict, so allow a handful of turns. The
        # tools are read-only and scoped to project_root via cwd.
        #
        # TB-362 (axis-6 migration): the judge no longer constructs
        # `sdk.ClaudeAgentOptions` / consumes the SDK stream directly — it
        # builds a backend-neutral `AgentOptions` / `AgentTools` and dispatches
        # through the `AgentAdapter` resolved for the `verifier_judge` kind
        # (`select_adapter("verifier_judge", cfg)`). Under the default
        # all-`claude` `[agent_backends]` map the resolved adapter is a
        # `ClaudeCodeAdapter` wrapping the injected `sdk` handle, so this stays
        # hermetic on the unit-test seam and bit-for-bit on Claude; an operator
        # can set `verifier_judge=codex` to route just this judge to the Codex
        # backend while every other kind stays on Claude. Late-import the
        # adapters package so `verify.py`'s import path stays light. The shape
        # mirrors `ideation_scrub._resolve_scrub_adapter` / `_run_scrub` (the
        # axis-6 canary).
        from ap2.adapters.base import AgentOptions, AgentTools
        from ap2.adapters.claude_code import ClaudeCodeAdapter

        if cfg is not None:
            from ap2.adapters.select import select_adapter

            adapter = select_adapter("verifier_judge", cfg)
        else:
            # cfg=None seam (kept for parity with the canary): default to the
            # Claude adapter the all-`claude` map would resolve anyway, so the
            # existing hermetic unit tests stay deterministic.
            adapter = ClaudeCodeAdapter()
        # The resolved Claude adapter wraps the injected `sdk` handle so the
        # hermetic prose-judge unit tests stay deterministic (they pass a stub
        # `sdk` exposing `ClaudeAgentOptions` + `query`); the daemon passes its
        # already-imported `claude_agent_sdk` module. Only the Claude backend
        # carries an injectable handle — any other backend ignores it.
        if sdk is not None and isinstance(adapter, ClaudeCodeAdapter):
            adapter._sdk = sdk

        options = AgentOptions(
            cwd=str(project_root),
            permission_mode="bypassPermissions",
            max_turns=int(cfg.get_core_value("verify_judge_max_turns", default=20)),
            setting_sources=["project"],
            # TB-344: schema is the single source of truth for the
            # agent_model default (see CORE_CONFIG_SCHEMA).
            model=cfg.get_core_value("agent_model"),
            effort=effort,
        )
        tools = AgentTools(allowed=list(JUDGE_REPO_READ_TOOLS))

        # TB-157: capture usage / cost / model / num_turns for the per-judge
        # cost accounting (the judge bypasses the daemon's `_log_message`
        # path). TB-362: these now come off the adapter's normalized
        # `AgentResult.usage` rather than walking raw `ResultMessage`
        # envelopes; `stop_reason` (absent from the normalized usage record)
        # is read off the terminal envelope the adapter retains on
        # `raw_result`.
        result = await adapter.run_to_result(prompt, tools, options)

        # A backend error / timeout surfaces as a non-`complete` status with
        # the `"<Type>: <msg>"` string on `.error` (`run_to_result` folds in
        # the `asyncio.wait_for` error handling the direct consume loop used to
        # get from the surrounding `try`/`except`). Preserve the pre-migration
        # `unverified` fallback so a judge fault never fails the whole
        # verification.
        if result.status in ("error", "timeout"):
            return CriterionResult(
                bullet=bullet.text, kind="prose", status="unverified",
                notes=f"judge error: {result.error or result.status}",
            )

        # TB-385: the prose judge no longer emits a per-bullet `judge_call`
        # event — the per-bullet verdict is folded into the daemon's single
        # terminal `task_verify` event (`bullets[]`) instead. So the per-call
        # usage / model / cost capture that fed `judge_call` is gone; only the
        # final-message `text` is needed to parse the verdict.
        text = (result.text or "").strip()
    except Exception as e:  # noqa: BLE001
        return CriterionResult(
            bullet=bullet.text, kind="prose", status="unverified",
            notes=f"judge error: {type(e).__name__}: {e}",
        )

    outcome = _parse_judge_response(bullet.text, text)
    verdict = outcome.verdict

    # TB-236: when the response can't be parsed into a verdict, dump the
    # FULL raw last-assistant-text to a per-bullet debug file so the
    # operator can diagnose WHY without being limited to the 200-char
    # truncated preview the verifier carries in `notes`. Dumps are written
    # ONLY on parse failure — a successful judge call leaves no trace on
    # disk.
    #
    # TB-385: the per-bullet `judge_call` event that used to carry the
    # `parse_error` / `response_length` / `rationale_length` categorization
    # is gone (folded — for the verdict only — into the daemon's terminal
    # `task_verify` event). The on-disk dump is retained as the operator's
    # parse-failure diagnostic surface, keyed by the same
    # `<run_ts>-<task>-judge-bullet<idx>-response.txt` convention so the
    # existing `ap2 logs` / debug-dir grep workflow is unaffected.
    if outcome.parse_error is not None and events_file is not None:
        try:
            import datetime as _dt
            debug_dir = events_file.parent / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bullet_label = (
                bullet_idx if bullet_idx is not None else -1
            )
            task_label = task_id or "unknown"
            dump_path = (
                debug_dir
                / f"{ts}-{task_label}-judge-bullet{bullet_label}-response.txt"
            )
            dump_path.write_text(text or "")
        except Exception:  # noqa: BLE001
            # Diagnostic write must never break verification.
            pass

    return verdict
