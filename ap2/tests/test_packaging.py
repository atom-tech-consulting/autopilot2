"""Hermetic packaging-surface gates for ap2's declared dependencies (TB-371).

The codex backend is code-complete (AgentAdapter ABC + ClaudeCodeAdapter,
CodexAdapter, per-kind backend selection, adapter-routed dispatch, the parity
suite, and the gated real-SDK smoke) but only becomes *installable* once
`openai-codex` (OpenAI's official Codex SDK, import name `openai_codex`) is
declared as an optional dependency. `CodexAdapter` lazily `import openai_codex`
(ap2.adapters.load_codex_sdk) at first dispatch, the daemon-start codex-handle
gate calls it, and the smoke gates on `pytest.importorskip("openai_codex")` —
all dead without an install path.

These tests parse `pyproject.toml` (no network resolution) and assert:

1. The `codex` optional-dependencies extra exists and references the
   `openai-codex` distribution, so the packaging surface can't silently regress.
2. The base `dependencies` list stays Claude-only — `claude-agent-sdk` is the
   always-installed backend and `openai-codex` is NOT pulled by a bare install —
   so `pip install autopilot2` with no extras remains a working Claude install.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _requirement_names(reqs: list[str]) -> list[str]:
    """Return the bare distribution name for each PEP 508 requirement string.

    Strips version specifiers / extras / markers so an unpinned `openai-codex`
    and a future pinned `openai-codex>=1.2` both compare equal.
    """
    names: list[str] = []
    for raw in reqs:
        token = raw.strip()
        # cut markers / extras / version specifiers
        for sep in (";", "[", "<", ">", "=", "!", "~", " "):
            idx = token.find(sep)
            if idx != -1:
                token = token[:idx]
        names.append(token.strip().lower())
    return names


def test_codex_extra_declared():
    """The `codex` extra exists and references the `openai-codex` distribution."""
    data = _load_pyproject()
    extras = data["project"]["optional-dependencies"]
    assert "codex" in extras, (
        "pyproject.toml [project.optional-dependencies] must declare a `codex` "
        "extra so `pip install 'autopilot2[codex]'` / `uv sync --extra codex` "
        "can install the second backend's `openai_codex` handle."
    )
    codex_reqs = _requirement_names(extras["codex"])
    assert "openai-codex" in codex_reqs, (
        "the `codex` extra must list an `openai-codex` requirement (OpenAI's "
        "official Codex SDK, the distribution providing `import openai_codex`); "
        f"got {extras['codex']!r}."
    )


def test_base_install_stays_claude_only():
    """A bare install resolves claude-agent-sdk but never pulls openai-codex."""
    data = _load_pyproject()
    base = _requirement_names(data["project"]["dependencies"])
    assert "claude-agent-sdk" in base, (
        "claude-agent-sdk must remain a base (always-installed) dependency."
    )
    assert "openai-codex" not in base, (
        "openai-codex must stay opt-in via the `codex` extra — a bare "
        "`pip install autopilot2` must remain a working Claude-only install."
    )
