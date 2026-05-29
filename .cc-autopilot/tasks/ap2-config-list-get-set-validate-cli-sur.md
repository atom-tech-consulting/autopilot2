## Goal

Ship axis (4) of the **Current focus: structured config (env →
TOML)** (goal.md L266 / L342-351): the operator-facing
`ap2 config` CLI surface with `list`, `get`, `set`, and
`validate` verbs. Goal.md's Progress signal "`ap2 config list`
enumerates every tunable knob with its source (file /
env-override / default)" is currently unmet — no `ap2 config`
subcommand exists in `ap2/cli*.py`. Foundation is in place:
TB-321 shipped `Config.from_toml` + `aggregate_schemas`; TB-322
declared 25 config keys across 7 component manifests; TB-323
shipped the env-override layer + the 62-entry FLAT_TO_SECTIONED
back-compat map. But every operator trying to introspect or tune
a knob still has to read `.cc-autopilot/config.toml` +
`ap2/howto.md` directly, then hand-correlate against
`_KNOBS_STAYING_ENV_ONLY`.

Why now: the focus's delete-test at goal.md L349-351 ("if not
shipped, the new config surface is present but not
operator-discoverable — every operator has to read the toml file
directly") is literally true today with axes 1+2+3 in HEAD but no
operator-facing CLI. Axis 5 (per-cluster knob migration) gets
safer when `ap2 config get` is available to confirm a knob's
read path during each migration, so axis 4 wants to land before
the long-tail axis-5 cluster TB-Ns dispatch.

## Scope

- New module `ap2/cli_config.py` with four subcommand handlers:
  `cmd_config_list`, `cmd_config_get`, `cmd_config_set`,
  `cmd_config_validate`. Mirrors the `cli_board.py` /
  `cli_diagnostic.py` split pattern.
- Argparse wiring in `ap2/cli.py`: add `config` subparser with
  four sub-subcommands. Each verb takes `--project` like every
  other CLI verb.
- `cmd_config_set` builds an `operator_queue.jsonl` record of the
  form `{"op": "config_set", "args": {"path": "...", "value":
  "..."}, ...}` and appends via the existing
  `do_operator_queue_append` helper. New `do_config_set` handler
  in `ap2/tools.py`; the operator-queue drain in `ap2/daemon.py`
  routes `config_set` ops to it. The handler writes the resolved
  value back into `.cc-autopilot/config.toml` using `tomli_w`
  (already a dep via TB-321) under `board_file_lock`, then emits
  a `config_updated` event.
- Source-attribution helper in `ap2/config_loader.py` (or new
  `ap2/config_introspect.py` — author's choice) that returns
  `{key_path: (value, source)}` for the merged config. The `list`
  verb consumes it.
- `events.py` registers `config_updated` (single-shot per `set`
  call, not the one-shot-per-process pattern `env_deprecated`
  uses).
- `ap2/howto.md` gets a new `## `ap2 config` reference` section
  under the operator-CLI documentation.

## Design

Each verb mirrors existing operator-CLI shapes:

- `ap2 config list` — walks `aggregate_schemas(default_registry())`
  + the core schema, prints one row per key with current value +
  resolved source (`file` / `env-override` / `default`); JSON
  output via `--json` mirrors `ap2 status --json`.
- `ap2 config get <path>` — single-key lookup; non-zero exit on
  unknown path with a "did-you-mean" suggestion against the
  schema keys.
- `ap2 config set <path> <value>` — operator-queue-routed write
  (mirrors `ap2 add` / `ap2 approve`): validates against the
  schema, appends to `.cc-autopilot/operator_queue.jsonl`,
  drained by the daemon under board_file_lock, emits a
  `config_updated` event on apply. Non-zero exit on schema
  mismatch with the named-path error from
  `config_loader.validate_config`.
- `ap2 config validate` — pure dry-run: loads the current
  `.cc-autopilot/config.toml` (+ env overrides), runs the same
  `validate_config` the daemon runs at startup, exits 0/non-zero
  with the validator's error message.

Source attribution for `list` works by re-running the
`from_toml` → `apply_env_overrides` precedence pipeline and
recording where each resolved key's value came from (file value
present → `file`; flat or sectioned env var set → `env-override`;
neither → `default`). Existing `_EMITTED_ONCE` deprecation set
in `config_compat.py` is read-only here — no new event spam from
the list verb.

## Verification

- `uv run pytest -q` — full suite passes (regression gate).
- `uv run python -m ap2 config list --project .` exits 0 and
  prints at least one row per key declared in
  `aggregate_schemas(default_registry())` plus the core schema;
  each row carries a `source` column with one of `file` /
  `env-override` / `default`. Verified by a new test
  `ap2/tests/test_tb324_cli_config.py::test_list_enumerates_all_keys`.
- `uv run python -m ap2 config get components.auto_approve.dry_run
  --project .` exits 0 and prints the resolved value. Verified by
  `test_tb324_cli_config.py::test_get_known_path`.
- `uv run python -m ap2 config get components.bogus.nonexistent
  --project .` exits non-zero with an error message that names
  the bad path verbatim. Verified by
  `test_tb324_cli_config.py::test_get_unknown_path_errors`.
- `uv run python -m ap2 config validate --project .` exits 0 on
  a valid config, non-zero on a corrupted one. Verified by
  `test_tb324_cli_config.py::test_validate_passes_then_fails`
  (the test corrupts a tmp config.toml mid-run).
- `uv run python -m ap2 config set components.janitor.disabled
  true --project .` appends a `config_set` record to
  `.cc-autopilot/operator_queue.jsonl` and exits 0; the next
  daemon drain applies the record + writes `.cc-autopilot/
  config.toml` + emits a `config_updated` event. Verified by
  `test_tb324_cli_config.py::test_set_routes_through_operator_queue`
  (uses the in-memory drain helper from existing tests).
- `grep -q "config_updated" ap2/events.py` — new event type is
  registered.
- `grep -q "ap2 config" ap2/howto.md` — operator-CLI docs cover
  the new verbs.
- `ap2/cli_config.py` Prose: the new CLI module exists with
  `cmd_config_list`, `cmd_config_get`, `cmd_config_set`,
  `cmd_config_validate` handler functions; SDK judge confirms
  via Read.

## Out of scope

- `ap2 config edit` (interactive editor flow) — goal.md L342-351
  enumerates `list / get / set / validate` only.
- `ap2 config unset <path>` — single-verb scope creep; operators
  delete a line directly or pass an empty string to `set` for
  this cycle.
- MCP `config_set` tool exposure to task agents — operator-only
  surface for now; defer until operator surfaces the need.
- Migrating component-body reads (axis 5) — that's a separate
  TB-N per cluster.
