# Expand README with sandbox setup, Codex backend, and components sections

Tags: #docs #onboarding #readme #distribution

## Goal

The top-level `README.md` currently has Install, How-it-works, Quickstart, "What's in this repo", Documentation, Tests, and License sections. Three things a first-time public reader needs are missing or under-documented:

1. **Agent user + sandbox project setup** — the canonical production deployment pattern. Today it's a single linked sentence pointing at `sandboxed-user-setup.md`; a reader has to click through to a runbook just to see what commands they'd run.
2. **Codex backend** — mentioned only in passing in Install ("`[codex]` extra"). No explanation of how to enable it per-agent-kind, what auth model it uses, or when to pick it over Claude.
3. **Key features / components** — the "How it works" diagram shows the ideation→dispatch→verify loop, but the README never enumerates the daemon's component participants (ideation, dispatch, verifier, auto-approve, operator queue, status report, janitor, web UI, Mattermost handler) — exactly the names the reader will see in `ap2 status` and the web UI.

Why now: the project is mid-execution on its current focus — **cut a public source-available distribution** — and the README is the public-distribution front door. The last README work (TB-431) just refreshed the Quickstart for auto-approve-on-default; extending it now with production-deployment, backend-selection, and a component map closes the remaining onboarding gaps while that surface is hot, instead of leaving a half-finished door for the first external reader. Each missing section maps directly to a question a fresh operator hits within the first 10 minutes ("how do I run it safely?", "can I use Codex?", "what's actually running?").

## Scope

- `README.md` (top-level only) — add three new `##`-level sections between the existing "Quickstart" and "What's in this repo":
  - `## Sandbox setup` — inline the `ap2 sandbox user-setup` and `ap2 sandbox project-setup` commands with one-paragraph explanation of what each does. Keep the link to `sandboxed-user-setup.md` at the end for depth.
  - `## Codex backend` — document the `[codex]` install extra, the per-agent-kind backend selection mechanism (env knob or config.toml key — whichever ships today), what auth each backend brings (Claude OAuth via `CLAUDE_CODE_OAUTH_TOKEN` vs. Codex API key via `OPENAI_API_KEY` or equivalent), and a brief note on when to pick one over the other.
  - `## Components` — one-line-per-component overview of the daemon's loop-level components. Map each to where the operator sees it surface (e.g. "operator queue → `ap2 status` drain lines", "janitor → cron-scheduled, surfaces findings in events.jsonl").

## Design

- Verify the component list against `ap2/architecture.md` and the actual component manifests in `ap2/components/*/manifest.py` (or wherever components are registered today — TB-429 just landed the unified manifest enablement source). Don't invent components — only document what's registered.
- Verify the Codex backend selection mechanism by reading the current `ap2/config.py` and the codex adapter (TB-371 shipped the optional extra). Don't guess at env-knob names; grep for them.
- Verify the sandbox subcommand surface by running `ap2 sandbox --help` (or reading `ap2/cli.py`'s sandbox parser) — commands shown in the README must match what the CLI actually exposes today, not stale runbook prose.
- Keep each new section's prose under ~30 lines — the top-level README is a first-look doc, not a manual. Depth lives in linked runbooks (`sandboxed-user-setup.md`) and the skill bundles (`ap2/skills/*/SKILL.md`).
- Section ordering after the additions: Install → How it works → Quickstart → Sandbox setup → Codex backend → Components → What's in this repo → Documentation → Tests → License. Reader's eye flow: install → understand the loop → run it locally → upgrade to production isolation → choose backend → see what's running → navigate the repo.

## Verification

- `uv run pytest -q` — full suite passes
- `grep -qE '^## Sandbox setup' README.md` — sandbox-setup section header exists
- `grep -qE '^## Codex backend' README.md` — codex-backend section header exists
- `grep -qE '^## Components' README.md` — components-overview section header exists
- `grep -qE 'ap2 sandbox user-setup' README.md` — user-setup command appears in the new sandbox section
- `grep -qE 'ap2 sandbox project-setup' README.md` — project-setup command appears in the new sandbox section
- `grep -qE '\[codex\]' README.md` — the codex install extra is documented in the codex section
- `README.md` Prose: the new Components section lists each currently-registered loop-level daemon component (ideation, dispatch, verifier, auto-approve, operator queue, status report, janitor, web UI, Mattermost handler) by name with a one-line description. The judge confirms by Read against `ap2/architecture.md` and the component manifests.
- `README.md` Prose: the new Codex backend section accurately describes the actual per-agent-kind selection mechanism in the current codebase (env knob or config.toml key). The judge confirms by Read of `ap2/config.py` and the codex adapter module.
- `README.md` Prose: the new Sandbox setup section's `ap2 sandbox user-setup` / `project-setup` invocation shapes match the current CLI surface. The judge confirms by Read of `ap2/cli.py`'s sandbox subparser.

## Out of scope

- Reorganizing existing sections (Install, How it works, Quickstart, What's in this repo) — only adding three new ones.
- Documenting every CLI verb, config knob, or component internal — those live in `ap2/README.md`, `ap2/howto.md`, and the skill bundles.
- Touching `ap2/README.md` (the package-internal operator reference) — this task is the top-level README only.
- Adding diagrams or screenshots — text-only additions.
- Changing the existing "## How it works" loop diagram or the auto-approve safety callout — leave them as-is.
