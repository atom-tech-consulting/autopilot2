"""TB-253 artifact-shape tests.

The TB-253 deliverable is an investigation artifact at
`.cc-autopilot/insights/test-suite-slowness-2026-05-17.md` produced from a
`uv run pytest -q ap2/tests/ --durations=20` run. These tests pin the
artifact's shape so an accidental rewrite (operator edit, follow-up TB) that
breaks the contract is caught by the project-wide verification gate rather
than silently degrading the historical record.

Three checks, mirroring Scope §5 of the briefing:

1. `test_artifact_file_exists` — the artifact file is present on disk.
2. `test_artifact_contains_durations_table` — the file contains the literal
   `## Top-20 slowest tests` heading and at least 20 duration-prefixed lines
   (`^\\s*\\d+\\.\\d+s\\s`). This is the data the operator scans to identify
   candidate fix targets.
3. `test_artifact_contains_category_aggregate` — each of the four
   categorization buckets named in the briefing
   (`essential-slow`, `fixable-slow`, `candidate-for-removal`,
   `investigate-further`) appears at least once in the file. The aggregate
   section reports per-category totals + percentages.

Scope (per the TB-253 briefing): pure investigation; no fixes applied, no
pytest configuration changed, no existing test code modified.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / ".cc-autopilot"
    / "insights"
    / "test-suite-slowness-2026-05-17.md"
)


def test_artifact_file_exists() -> None:
    assert ARTIFACT.exists(), (
        f"TB-253 investigation artifact missing at {ARTIFACT}. The TB-253 "
        "deliverable is the artifact itself; without it the investigation "
        "result is lost and follow-up fix TBs have no data to scope against."
    )


def test_artifact_contains_durations_table() -> None:
    body = ARTIFACT.read_text()
    assert "## Top-20 slowest tests" in body, (
        "Artifact must contain the literal heading `## Top-20 slowest tests` "
        "so the operator can find the table without grepping for synonyms."
    )
    duration_lines = [
        line
        for line in body.splitlines()
        if re.match(r"^\s*\d+\.\d+s\s", line)
    ]
    assert len(duration_lines) >= 20, (
        f"Expected at least 20 lines starting with a duration prefix "
        f"(e.g. `5.23s ...`); found {len(duration_lines)}. The top-20 "
        "table is the central data of the artifact."
    )


@pytest.mark.parametrize(
    "category",
    [
        "essential-slow",
        "fixable-slow",
        "candidate-for-removal",
        "investigate-further",
    ],
)
def test_artifact_contains_category_aggregate(category: str) -> None:
    body = ARTIFACT.read_text()
    assert category in body, (
        f"Category label `{category}` is missing from the artifact. The "
        "briefing defines four buckets; the artifact must reference each at "
        "least once (in the legend, the table, or the aggregate counts) so "
        "the operator can decide direction from per-category totals."
    )
