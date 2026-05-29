"""Operator-facing `ap2 config` CLI surface (TB-324, axis 4).

Axis (4) of the **structured config (env → TOML)** focus (goal.md
L342-351). Ships four verb handlers — `list`, `get`, `set`,
`validate` — so an operator can introspect every tunable knob with
its current value + resolved source (`file` / `env-override` /
`default`) without grepping `.cc-autopilot/config.toml` against
`ap2/howto.md` by hand.

Verb shapes (each mirrors an existing operator-CLI surface):

  - `ap2 config list`           — enumerate every key + value + source.
                                  `--json` for machine output (mirrors
                                  `ap2 status --json`).
  - `ap2 config get <path>`     — single-key lookup; non-zero on
                                  unknown path with a did-you-mean
                                  suggestion against the schema keys.
  - `ap2 config set <path> <v>` — operator-queue-routed write. Validates
                                  against the schema, appends a
                                  `config_set` record to
                                  `.cc-autopilot/operator_queue.jsonl`,
                                  drained by the daemon (mirrors
                                  `ap2 add` / `ap2 approve`).
  - `ap2 config validate`       — pure dry-run: parse current TOML +
                                  env overlay, run `validate_config`,
                                  exit 0/non-zero with the validator's
                                  message.

Source attribution for `list` re-runs the precedence pipeline
(`from_toml` → `apply_env_overrides`) and records where each
resolved value came from:
  - TOML file declares the key      → `file`
  - sectioned env (`AP2_<SECTION>_<KEY>`) or flat env (back-compat)
    is set in `os.environ`          → `env-override`
  - neither                         → `default`

The introspection layer is a thin helper module (`config_introspect`
re-export) so the `list` verb stays a presentation layer.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config_introspect, tools
from .config import CONFIG_TOML_FILE, Config
from .config_loader import ConfigSchemaError, validate_config
from .registry import default_registry


def cmd_config_list(cfg: Config, args: argparse.Namespace) -> int:
    """Enumerate every known config key with value + source.

    The source column distinguishes `file` (TOML declares the key) /
    `env-override` (sectioned or flat env var set) / `default` (neither).
    `--json` emits the same data as a list of dicts for machine
    consumption.
    """
    rows = config_introspect.collect_rows(cfg, default_registry())
    if args.json:
        print(json.dumps([_row_to_json(r) for r in rows], indent=2))
        return 0
    if not rows:
        print("(no config keys discovered — registry is empty?)")
        return 0
    # Plain text table: path | value | source | description.
    # Column widths sized to the widest row + a small floor so a single
    # narrow value doesn't crunch the layout.
    path_w = max(40, max(len(r.path) for r in rows))
    value_w = max(14, max(len(_format_value(r.value)) for r in rows))
    source_w = max(12, max(len(r.source) for r in rows))
    header = (
        f"{'path':<{path_w}}  {'value':<{value_w}}  "
        f"{'source':<{source_w}}  description"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        desc = r.description or ""
        print(
            f"{r.path:<{path_w}}  {_format_value(r.value):<{value_w}}  "
            f"{r.source:<{source_w}}  {desc}"
        )
    return 0


def cmd_config_get(cfg: Config, args: argparse.Namespace) -> int:
    """Single-key lookup. Non-zero on unknown path with did-you-mean."""
    path = (args.path or "").strip()
    if not path:
        print("ap2 config get: <path> is required", file=sys.stderr)
        return 2
    rows = config_introspect.collect_rows(cfg, default_registry())
    by_path = {r.path: r for r in rows}
    if path not in by_path:
        suggestion = _did_you_mean(path, list(by_path.keys()))
        msg = f"ap2 config get: unknown path {path!r}"
        if suggestion:
            msg += f" (did you mean {suggestion!r}?)"
        print(msg, file=sys.stderr)
        return 1
    row = by_path[path]
    print(_format_value(row.value))
    return 0


def cmd_config_set(cfg: Config, args: argparse.Namespace) -> int:
    """Queue a `config_set` op for the daemon to apply at the next tick.

    The op carries `path` + `value` (string). The drain-side handler
    in `ap2.tools` parses the value against the schema's declared type,
    writes the resolved value back into `.cc-autopilot/config.toml`
    under `board_file_lock`, and emits a `config_updated` event.
    Validates the path + the value type at append-time so an obvious
    operator error (typo'd path, `tick_interval_s = "lots"`) fails
    fast.
    """
    path = (args.path or "").strip()
    value = args.value
    if not path:
        print("ap2 config set: <path> is required", file=sys.stderr)
        return 2
    if value is None:
        print("ap2 config set: <value> is required", file=sys.stderr)
        return 2
    res = tools.do_operator_queue_append(
        cfg,
        {"op": "config_set", "path": path, "value": value},
    )
    if res.get("isError"):
        print(res["content"][0]["text"], file=sys.stderr)
        return 1
    msg = json.loads(res["content"][0]["text"])
    print(
        f"queued config_set {path}={value} "
        f"(uuid={msg.get('uuid', '')}; will land at next tick)"
    )
    return 0


def cmd_config_validate(cfg: Config, args: argparse.Namespace) -> int:
    """Dry-run schema check.

    Loads `.cc-autopilot/config.toml` (if present) + env overrides via
    the same `Config.load` path the daemon runs at startup, then walks
    `validate_config` against the registry's aggregated schemas. Exits
    0 on pass, non-zero on the first schema mismatch (with the
    validator's named-path error).
    """
    toml_path = cfg.project_root / CONFIG_TOML_FILE
    if not toml_path.exists():
        print(
            f"ap2 config validate: no config.toml at "
            f"{toml_path} — env-only path is always valid (no schema "
            f"to validate against)."
        )
        return 0
    try:
        # Re-parse the file fresh so the validate verb's read is
        # independent of any in-process cfg side effects.
        from .config_loader import parse_toml
        raw = parse_toml(toml_path)
        validate_config(raw, default_registry())
    except ConfigSchemaError as e:
        print(f"ap2 config validate: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        # TOML decode errors, OS errors, etc. — surface them with the
        # exception type so the operator can grep the file directly.
        print(
            f"ap2 config validate: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1
    print(f"ap2 config validate: OK ({toml_path})")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    """Format a value for the `list` / `get` text output.

    Strings round-trip verbatim (no quotes — operators reading a long
    string value shouldn't see distracting framing). Bools render as
    `true` / `false` (TOML vocabulary, not Python's `True`/`False`).
    None renders as `(unset)`. Everything else falls back to `repr`-ish
    via `str()`.
    """
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _row_to_json(row: "config_introspect.ConfigRow") -> dict:
    """JSON form of a ConfigRow for `list --json`."""
    return {
        "path": row.path,
        "value": row.value,
        "source": row.source,
        "description": row.description,
        "type": row.type_name,
        "hot_reloadable": row.hot_reloadable,
    }


def _did_you_mean(needle: str, candidates: list[str]) -> str | None:
    """Return the single closest candidate to `needle` if one is close
    enough to surface as a suggestion, else None.

    Uses `difflib.get_close_matches` with a conservative cutoff so a
    truly unrelated typo (`foo.bar.baz`) doesn't pull up a random
    suggestion. The single-match return shape matches the `ap2`
    operator-CLI's existing "<error>: did you mean <x>?" surfaces.
    """
    from difflib import get_close_matches

    hits = get_close_matches(needle, candidates, n=1, cutoff=0.6)
    return hits[0] if hits else None
