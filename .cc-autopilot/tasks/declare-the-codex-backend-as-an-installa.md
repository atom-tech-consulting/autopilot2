# Declare the codex backend as an installable optional extra (autopilot2[codex])

## Goal

This advances the Current focus: codex support through an agent adaptor layer.
Every axis of that focus has shipped as code — the AgentAdapter ABC +
ClaudeCodeAdapter (TB-353), the CodexAdapter against the codex CLI (TB-357),
per-kind backend selection plus the three backend-aware daemon-start gates
(TB-358 / TB-368 / TB-369 / TB-370), every dispatch site adapter-routed
(TB-360/362/363/364/365/366), the mixed-config end-to-end (TB-367), and the
parity suite plus the gated codex real-SDK smoke (TB-359). But the codex backend
is code-complete and NOT runnable: `codex_sdk` (distribution `codex-sdk`) is
nowhere declared as an installable dependency. `pyproject.toml`'s
`[project.optional-dependencies]` carries only `dev`; `claude-agent-sdk` is a
hard dependency but the codex handle is not. So the lazy `import codex_sdk` in
`load_codex_sdk` (ap2/adapters), the daemon-start codex-handle gate that calls it,
and the smoke's `pytest.importorskip("codex_sdk")` (TB-359) are all dead on every
environment — there is no supported way to install the second backend. This task
adds a `codex` extra so an operator can `uv sync --extra codex` /
`pip install 'autopilot2[codex]'` and actually run a codex-backed kind.

Why now: this is the single packaging gap that keeps a fully-coded second
backend from ever driving an agent kind live — without the extra, the axis-7
real-SDK smoke and the codex-handle daemon-start gate can never be satisfied, so
the focus delete-test ("a second backend actually drives an agent kind") stays
unmet despite all seven axes being coded. The operator surfaced this exact next
step on 2026-06-02 (operator_log: "declare codex_sdk as an optional
dependency/extra ... to validate CodexAdapter end-to-end against the real
codex_sdk API").

## Scope

- Add a `codex` extra to `[project.optional-dependencies]` in `pyproject.toml`
  whose requirement provides the `codex_sdk` import — distribution name
  `codex-sdk`, matching the daemon-start gate's existing `uv pip install
  codex-sdk` remediation hint in `ap2/daemon.py`. An unpinned `codex-sdk`
  requirement is acceptable if no version floor is known.
- Leave `claude-agent-sdk` as the base (always-installed) dependency unchanged:
  the default `uv sync` / `pip install autopilot2` with no extras must remain a
  working Claude-only install. The codex backend is opt-in via the extra.
- Document the codex extra in `ap2/howto.md`: how to install it
  (`pip install 'autopilot2[codex]'` / `uv sync --extra codex`) and that a
  codex-backed kind needs both the extra AND OpenAI/codex credentials.
- Update the daemon-start codex-handle gate diagnostic in `ap2/daemon.py` so its
  remediation hint also names the extra (`pip install 'autopilot2[codex]'`)
  alongside the existing `uv pip install codex-sdk` line, keeping the
  operator-facing message coherent with the new packaging surface.
- Add a hermetic (no-network) test — e.g. `test_codex_extra_declared` in a new
  `ap2/tests/test_packaging.py` — that parses `pyproject.toml` and asserts the
  `codex` extra exists and references a `codex-sdk` distribution.

## Design

The change is packaging-only; no runtime code path changes behavior. Concretely:

- `pyproject.toml` gains `codex = ["codex-sdk"]` under
  `[project.optional-dependencies]` (sibling to the existing `dev` extra). The
  base `dependencies` list is untouched, so a bare install stays Claude-only and
  the default test suite never resolves the codex requirement.
- The existing lazy-import seam is the consumer: `load_codex_sdk()`
  (`ap2/adapters`) already does `import codex_sdk` only at first dispatch, the
  daemon-start gate calls it, and the smoke gates on
  `pytest.importorskip("codex_sdk")`. None of these change — declaring the extra
  is purely what gives them an install path. The distribution name `codex-sdk`
  is chosen to match the daemon gate's pre-existing `uv pip install codex-sdk`
  hint so the operator-facing story is consistent.
- The daemon-start gate's diagnostic string in `ap2/daemon.py` is the only
  source edit: append `pip install 'autopilot2[codex]'` to the remediation hint
  so operators discover the supported extra, not just the bare distribution.
- Docs: `ap2/howto.md` documents the install line and the
  "extra + OpenAI/codex creds" requirement for a codex-backed kind.
- Test: a hermetic `tomllib`-based parse test asserts the extra is present and
  references a `codex-sdk` requirement, so the packaging surface can't silently
  regress. No network resolution is performed.

## Verification

- `uv run pytest -q` — full suite passes under the default Claude-only install; the codex extra is not pulled by the base sync.
- `grep -q "codex-sdk" pyproject.toml` — the codex extra requirement is declared in pyproject.toml.
- `grep -q "autopilot2\[codex\]" ap2/howto.md` — howto documents installing the codex extra.
- `grep -q "autopilot2\[codex\]" ap2/daemon.py` — the daemon-start gate's remediation hint names the installable extra.
- `test_codex_extra_declared` Prose: a new hermetic test (e.g. in `ap2/tests/test_packaging.py`) parses pyproject.toml and asserts the `codex` optional-dependencies extra is present and lists a codex-sdk requirement; judge confirms via Read against the working tree.

## Out of scope

- Running the codex real-SDK smoke live against the real backend — that needs
  real OpenAI/codex credentials and `AP2_REAL_SDK` on the 6h `real-sdk-smoke`
  cron; it is operator-owned and cannot be verified unattended (no `Manual:`
  bullets).
- Pinning an exact `codex-sdk` version or resolving the extra over the network in
  CI — the base install stays Claude-only and the extra is opt-in.
- Any change to CodexAdapter behavior, dispatch routing, tool policy, or
  verification semantics (all shipped in TB-357 / TB-364 / TB-365 / TB-359).