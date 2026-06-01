"""Per-agent-kind backend resolver (TB-358 / goal.md axis 5).

Axis 4 (TB-357) landed a second real backend — the `CodexAdapter` — behind
the `AgentAdapter` contract `ClaudeCodeAdapter` already satisfied, but
nothing could *route* to it: switching a kind's backend meant a code edit
and codex hard-failed the OAuth-only daemon-start gate. This module is the
selection surface goal.md's axis 5 scopes: a small resolver that reads the
merged `[agent_backends]` config (file table + `AP2_AGENT_BACKEND_<KIND>`
env overrides + the all-`claude` default, via `Config.get_agent_backend`)
and returns the right adapter *instance* for an agent kind.

`AGENT_KINDS` is the canonical inventory of selectable agent kinds — the
nine dispatch sites the focus migrates one-by-one in axis 6. The
backend-aware daemon-start auth gate (`cli_daemon._require_oauth_token`)
walks this tuple to discover which credentials the resolved backend set
implies (OAuth for any claude-backed kind, OpenAI for any codex-backed
kind).

No production dispatch site is repointed through this resolver in axis 5 —
that is axis 6 (the ideation-scrub canary is the first consumer). This
module ships the selection machinery and its tests only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Config


#: Canonical inventory of selectable agent kinds (goal.md L169-185). Each is
#: an independently backend-selectable dispatch site the focus migrates in
#: axis 6: `task` (run_task), `ideation` / `status_report` / `cron` /
#: `mattermost` (the shared `_run_control_agent` consumers), and the four
#: judge/scrub component calls. The `AP2_AGENT_BACKEND_<KIND>` env override
#: for a kind upper-cases the entry onto the suffix (the `task` /
#: `status_report` kinds read the `_TASK` / `_STATUS_REPORT` suffixed
#: names). The auth gate walks this tuple to enumerate the resolved
#: backend set.
AGENT_KINDS: tuple[str, ...] = (
    "task",
    "ideation",
    "status_report",
    "cron",
    "mattermost",
    "verifier_judge",
    "ideation_scrub",
    "validator_judge",
    "janitor_judge",
)

#: Backend id → concrete adapter class. An id absent from this map resolves
#: to the Claude adapter (the default / behavior-reference backend), so an
#: operator typo (`AP2_AGENT_BACKEND_<KIND>=claud`) degrades to claude rather
#: than crashing dispatch.
_ADAPTER_BY_BACKEND: dict[str, type[AgentAdapter]] = {
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


def referenced_backends(cfg: "Config") -> set[str]:
    """Return the set of *effective* backends the per-kind map references.

    Walks `AGENT_KINDS`, resolves each kind via `cfg.get_agent_backend(kind)`,
    and normalizes any non-`codex` id to `"claude"` — the same default
    `select_adapter` applies (an unknown backend id degrades to the Claude
    adapter), and the same normalization the daemon-start credential gate
    (`cli_daemon._require_oauth_token`'s `_effective` helper) uses. So the
    all-claude default returns `{"claude"}`, a pure-codex map returns
    `{"codex"}`, and a mixed map returns `{"claude", "codex"}`.

    Both daemon-start gates consume this so they agree on which backends the
    map references: the credential gate to demand each backend's creds, and the
    SDK-availability gate (`daemon.main_loop`, TB-368) to import the Claude SDK
    only when at least one kind resolves to `claude`.
    """
    return {
        "codex" if cfg.get_agent_backend(k) == "codex" else "claude"
        for k in AGENT_KINDS
    }


def select_adapter(kind: str, cfg: "Config") -> AgentAdapter:
    """Return the `AgentAdapter` instance backing agent `kind` under `cfg`.

    Reads the merged per-kind backend id via `cfg.get_agent_backend(kind)`
    (env override > `[agent_backends]` table > `DEFAULT_AGENT_BACKEND`) and
    instantiates the matching adapter class:

      - `"claude"` → `ClaudeCodeAdapter`
      - `"codex"`  → `CodexAdapter`

    An unmapped kind resolves to `claude` inside `get_agent_backend`, and an
    *unknown* backend id (operator typo, a future backend this daemon
    doesn't know) defaults to `ClaudeCodeAdapter` here — selection never
    hard-fails to a missing backend; the default-claude install behaves
    exactly as it did before the adapter layer existed.

    Each call constructs a fresh adapter (the adapters are cheap, stateless
    handles that lazily import their backend SDK on first `run`), so the
    caller owns the instance for the lifetime of its dispatch.
    """
    backend = cfg.get_agent_backend(kind)
    adapter_cls = _ADAPTER_BY_BACKEND.get(backend, ClaudeCodeAdapter)
    return adapter_cls()
