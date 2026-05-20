"""TB-257 artifact-shape tests (file-name `test_tb256_*` per briefing §7).

The TB-257 deliverable is an investigation artifact at
`.cc-autopilot/insights/validator-judge-timeout-2026-05-18.md` characterizing
why the TB-235 dep-coherence judge has been timing out on ~every recent
operator queue-append (6 events in 25h, 8 in 7d). These tests pin the
artifact's shape so an accidental rewrite (operator edit, follow-up
calibration TB) that breaks the contract is caught by the project-wide
verification gate rather than silently degrading the historical record.

Mirrors `test_tb_investigate_suite_slow_artifact.py` (TB-253). Four checks,
mirroring Scope §5 + §7 of the briefing:

1. `test_artifact_file_exists` — the artifact file is present on disk.
2. `test_artifact_yaml_front_matter_parses` — leading `---\\n...\\n---`
   block parses as YAML and contains the expected keys (`tldr`,
   `updated`, `updated_by`, `cites`).
3. `test_artifact_contains_categorized_factor` — at least one of the six
   categorization buckets named in the briefing
   (`prompt-too-heavy`, `max_turns-too-tight`, `timeout-too-tight`,
   `sdk-cold-start`, `network-flake`, `investigate-further`) appears at
   least once in the file body. The aggregate section reports per-category
   rationale.
4. `test_artifact_enumerates_at_least_five_timeout_rows` — body contains
   at least 5 enumerated rows referencing
   `validator_judge_timeout` events (one per observed event in the
   investigation window). Counted as the markdown-table rows whose
   second column is a `YYYY-MM-DDTHH:MM:SSZ` ISO timestamp.

Scope (per the TB-257 briefing): pure investigation; no production code
edits, no calibration patches, no validator-judge SDK call-signature
changes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / ".cc-autopilot"
    / "insights"
    / "validator-judge-timeout-2026-05-18.md"
)


def test_artifact_file_exists() -> None:
    assert ARTIFACT.exists(), (
        f"TB-257 investigation artifact missing at {ARTIFACT}. The TB-257 "
        "deliverable is the artifact itself; without it the investigation "
        "result is lost and follow-up calibration TBs have no data to "
        "scope against."
    )


def test_artifact_yaml_front_matter_parses() -> None:
    body = ARTIFACT.read_text()
    assert body.startswith("---\n"), (
        "Artifact must open with a YAML front-matter fence (`---\\n`); "
        "the operator's `insights/` index regeneration scans for this "
        "shape (TB-198 `_index.md` rebuild reads `tldr:` / `updated:` "
        "from each insight file)."
    )
    end = body.find("\n---\n", 4)
    assert end > 0, (
        "Artifact YAML front-matter must close with `\\n---\\n`; the "
        "opening fence has no matching close, so the front-matter parse "
        "would consume the entire body."
    )
    front_matter = body[4:end]
    # Use stdlib only — yaml is not a hard dep of the project (per
    # TB-198 the insights index parses front-matter with a manual
    # line-by-line walk). Pin the required keys by literal-substring
    # match against the front-matter slice instead of going through a
    # YAML loader.
    for required in ("tldr:", "updated:", "updated_by:", "cites:"):
        assert required in front_matter, (
            f"YAML front-matter is missing the `{required}` key. The "
            "TB-257 briefing's Scope §5 names this field explicitly so "
            "the operator can grep across insights without opening "
            "each file."
        )
    # Latest-attribution pin — the artifact carries an `updated_by:`
    # tag naming the most recent TB that touched it (originally
    # `TB-256`, the TB-257 deliverable's author; bumped to `TB-269`
    # when the calibration follow-up appended its
    # `## Calibration applied (TB-269)` section per TB-269 §Scope 4).
    # Pin the LATEST attribution token so a future-TB rewrite that
    # forgets to refresh the field trips here rather than silently
    # diverging from the file's actual provenance.
    assert "updated_by: TB-269" in front_matter, (
        "Front-matter must carry `updated_by: TB-269` (the latest "
        "attribution after TB-269's calibration-applied append). A "
        "future TB touching this artifact should bump this pin to its "
        "own TB-N in the same edit that updates `updated_by:`."
    )


@pytest.mark.parametrize(
    "category",
    [
        "prompt-too-heavy",
        "max_turns-too-tight",
        "timeout-too-tight",
        "sdk-cold-start",
        "network-flake",
        "investigate-further",
    ],
)
def test_artifact_contains_categorized_factor(category: str) -> None:
    body = ARTIFACT.read_text()
    assert category in body, (
        f"Category label `{category}` is missing from the artifact. The "
        "TB-257 briefing's Scope §6 defines six buckets; the artifact "
        "must reference each at least once (in the legend, the per-row "
        "categorization, or the headline finding) so the operator can "
        "decide direction from the categorized findings."
    )


def test_artifact_enumerates_at_least_five_timeout_rows() -> None:
    body = ARTIFACT.read_text()
    assert "validator_judge_timeout" in body, (
        "Artifact must reference the `validator_judge_timeout` event type "
        "literally; otherwise the briefing's Verification "
        "`grep -q 'validator_judge_timeout' …` bullet fails."
    )
    # Per Scope §2 + §7: the body contains an enumerated table of
    # timeout events with at least 5 rows. Count markdown-table data
    # rows whose second column is an ISO-8601 UTC timestamp
    # (`YYYY-MM-DDTHH:MM:SSZ`) — that's the table's primary axis and
    # the most stable shape-pin against an operator reformatting the
    # surrounding prose.
    iso_row_pattern = re.compile(
        r"^\|\s*\d+\s*\|\s*20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*\|",
    )
    iso_rows = [
        line for line in body.splitlines() if iso_row_pattern.match(line)
    ]
    assert len(iso_rows) >= 5, (
        f"Expected at least 5 enumerated `validator_judge_timeout` rows "
        f"(one per event); found {len(iso_rows)} that match "
        f"`| <n> | <iso-ts> |`. The enumeration is the briefing's Scope "
        "§2 deliverable — the operator scans this table to scope the "
        "calibration TB."
    )
