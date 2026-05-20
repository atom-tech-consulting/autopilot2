"""TB-267: home-page route group tests — mirror of `ap2/web_home.py`.

Relocated from `ap2/tests/test_web.py` by the TB-267 split. Each test body
is byte-identical to its pre-TB-267 original; only the module home and the
shared `project` fixture's location (now `ap2/tests/conftest.py`) changed.

Covers:
  - `_render_home` end-to-end rendering (basic page, failure-class).
  - `_render_pending_queue` card (TB-162).
  - `_render_operator_decisions` card (TB-173 / TB-191).
  - `_render_ideation_status_block` card variants (TB-197).
  - `_render_env_stale_warning` (TB-260 / TB-265).
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from ap2 import web, events as ev_mod
from ap2.config import Config


# --------- TB-93 thaw: ideation_state (lives next to home — uses ideation_state.md) ---------


def test_ideation_state_shows_file_and_summary(project: Config, tmp_path):
    state_path = tmp_path / ".cc-autopilot" / "ideation_state.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("# Ideation State\n\n## Mission alignment\nAll good.\n")
    ev_mod.append(
        project.events_file, "ideation_complete",
        summary="Cycle 4: proposed TB-100, TB-101, TB-102.",
    )
    ev_mod.append(project.events_file, "ideation_state_updated", bytes=120)
    html = web._render_ideation_state(project)
    assert "All good." in html
    assert "Cycle 4" in html
    assert "120" in html


def test_ideation_state_no_file(project: Config):
    html = web._render_ideation_state(project)
    assert "not yet written" in html or "ideation_state.md" in html


# --------- home-page renders ---------


def test_home_renders(project: Config):
    html = web._render_home(project)
    assert "<!DOCTYPE html>" in html
    assert "TB-3" in html or "Active" in html  # board section labels present
    assert "daemon" in html.lower()
    # All four events surface in the events table
    assert "task_complete" in html
    assert "task_error" in html
    assert "ideation_empty_board" in html
    assert "daemon_start" in html


def test_home_marks_failure_class(project: Config):
    html = web._render_home(project)
    # task_error is in FAILURE_EVENT_TYPES → row gets the `failure` class
    assert 'class="failure"' in html


# --------- TB-162: pending operator-queue card on `/` ---------


def _seed_queue_entry(
    cfg: Config,
    *,
    uuid: str,
    op: str,
    args: dict,
    ts: str = "2026-05-04T17:15:30Z",
) -> None:
    """Append one operator-queue record to `.cc-autopilot/operator_queue.jsonl`.

    Mirrors the shape `tools.do_operator_queue_append` writes — uuid + op
    + args + ts is the contract `_render_pending_queue` reads.
    """
    queue_path = cfg.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"uuid": uuid, "op": op, "args": args, "ts": ts}
    with queue_path.open("a") as f:
        f.write(_json.dumps(rec) + "\n")


def _seed_queue_state_applied(cfg: Config, uuids: list[str]) -> None:
    """Mirror `tools._save_operator_queue_applied` — the state file the
    drain handler keeps in sync with the queue."""
    state_path = cfg.project_root / ".cc-autopilot" / "operator_queue_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_json.dumps({"applied": list(uuids)}, indent=2))


def test_pending_queue_card_renders_three_op_kinds(project: Config):
    """Three undrained ops (add_backlog with title, update with fields,
    approve) all appear in the rendered HTML on `/`, AND each carries
    its per-op-kind summary shape: `title="..."` for add_backlog,
    `fields=...` for update, no extra arg for approve."""
    _seed_queue_entry(
        project,
        uuid="aaaaaaaa-1111-2222-3333-444444444444",
        op="add_backlog",
        args={"task_id": "TB-200", "title": "Surface pending operator queue"},
        ts="2026-05-04T17:18:02Z",
    )
    _seed_queue_entry(
        project,
        uuid="bbbbbbbb-1111-2222-3333-444444444444",
        op="update",
        args={
            "task_id": "TB-152",
            "title": "rev",
            "fields": ["title", "description", "briefing"],
        },
        ts="2026-05-04T17:15:30Z",
    )
    _seed_queue_entry(
        project,
        uuid="cccccccc-1111-2222-3333-444444444444",
        op="approve",
        args={"task_id": "TB-152"},
        ts="2026-05-04T17:18:09Z",
    )
    page = web._render_home(project)
    # Card present.
    assert "pending-queue" in page
    # All three op kinds rendered.
    assert "[add_backlog]" in page
    assert "[update]" in page
    assert "[approve]" in page
    # All three task_ids rendered.
    assert "TB-200" in page
    assert "TB-152" in page
    # Per-op-kind summaries.
    assert 'title="Surface pending operator queue"' in page
    assert "fields=title,description,briefing" in page
    # `approve` carries no per-op extra (no fields=, no title=, no
    # force=) — the task_id pill alone is the load-bearing signal.
    li_approve = next(
        chunk for chunk in page.split("<li>") if "[approve]" in chunk
    )
    li_approve = li_approve.split("</li>", 1)[0]
    assert "title=" not in li_approve
    assert "fields=" not in li_approve
    assert "force=" not in li_approve


def test_pending_queue_card_omitted_when_queue_empty(project: Config):
    """Empty (or missing) queue file → card is omitted entirely from `/`,
    not just CSS-hidden. The `pending-queue` selector lives in the page
    `<style>` so we scope the assertion to the post-`</style>` body —
    that's where a rendered card would land if one were emitted."""
    # Case 1: file does not exist.
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="pending-queue"' not in body
    assert "operator op" not in body  # header text only fires when card renders
    # Case 2: file exists but is empty.
    queue_path = project.project_root / ".cc-autopilot" / "operator_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("")
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="pending-queue"' not in body
    assert "operator op" not in body


def test_pending_queue_uuid_is_truncated(project: Config):
    """UUIDs render as a short prefix (≤16 chars), not the full 36-char
    canonical form — avoids horizontal overflow on narrow viewports."""
    long_uuid = "deadbeef-1234-5678-9abc-def012345678"
    _seed_queue_entry(
        project,
        uuid=long_uuid,
        op="approve",
        args={"task_id": "TB-99"},
    )
    page = web._render_home(project)
    # The header label "uuid=" is followed by an 8-char prefix; the full
    # 36-char form must NOT appear inside the entry's `<li>` body
    # (the raw json `<details>` carries it, which is fine).
    li = next(chunk for chunk in page.split("<li>") if "TB-99" in chunk)
    # Cut the body at the `<details>raw json</details>` boundary so the
    # raw JSON dump (which legitimately carries the full uuid) doesn't
    # falsely satisfy the assertion.
    body, _, _ = li.partition("<details>")
    assert "uuid=deadbeef" in body
    assert long_uuid not in body, (
        f"full uuid leaked into rendered body (should be ≤8 char prefix): "
        f"{body!r}"
    )


def test_pending_queue_filters_out_drained_entries(project: Config):
    """An entry whose uuid is in `operator_queue_state.json`'s applied-set
    is treated as drained-but-not-yet-compacted and omitted from the
    rendered card. Pins the brief window between drain (state file
    updated) and `_compact_operator_queue` (queue file rewritten)."""
    drained_uuid = "11111111-1111-1111-1111-111111111111"
    pending_uuid = "22222222-2222-2222-2222-222222222222"
    _seed_queue_entry(
        project,
        uuid=drained_uuid,
        op="approve",
        args={"task_id": "TB-DRAINED"},
    )
    _seed_queue_entry(
        project,
        uuid=pending_uuid,
        op="approve",
        args={"task_id": "TB-PENDING"},
    )
    _seed_queue_state_applied(project, [drained_uuid])
    page = web._render_home(project)
    assert "pending-queue" in page  # card still rendered (one pending)
    assert "TB-PENDING" in page
    # Drained entry must not appear in the rendered card. Limit the
    # check to the card's own slice so unrelated TB-DRAINED references
    # elsewhere on the page (none today, but defensive) couldn't
    # falsely satisfy the assertion.
    card = page.split('<div class="pending-queue">', 1)[1].split("</div>", 1)[0]
    assert "TB-DRAINED" not in card
    assert "11111111" not in card


def test_pending_queue_helper_is_grep_visible():
    """The briefing's `grep -nE "def _render_pending_queue"` and
    `grep -qE "pending-queue"` verification bullets pin both the helper
    name and the CSS class name to the web module family. TB-265 split
    web.py by route group; the helper now lives in `web_home.py` and
    the CSS class in `web_chrome.py`. A refactor that drops either
    would silently break the operator-facing card."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    text = "\n".join(p.read_text() for p in sorted(root.glob("web*.py")))
    assert "def _render_pending_queue" in text
    assert "pending-queue" in text


# --------- TB-173 / TB-191: ideator decisions-needed card on `/` ---------
#
# `_render_operator_decisions(cfg)` reads the `## Decisions needed from
# operator` section from `.cc-autopilot/ideation_state.md` via
# `parse_operator_decisions`, renders one `<li>` per bullet, and is
# mounted above `_render_pending_queue` on `/`. Empty list → card
# omitted entirely (server-side, not CSS-hidden).
#
# TB-191 also added the agent-internal `## Cycle observations` section
# that MUST NOT leak to operator-facing surfaces. The test at the end
# of this block pins that the home page never surfaces observations
# content even when both sections coexist in the file.


def _seed_ideation_state(cfg: Config, body: str) -> None:
    path = cfg.project_root / ".cc-autopilot" / "ideation_state.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_operator_decisions_card_renders_when_present(project: Config):
    """Three bullets in the file → home page carries an
    `.operator-decisions` card with one `<li>` per bullet and a
    header that names the count."""
    _seed_ideation_state(
        project,
        "## Decisions needed from operator\n\n"
        "- Decision needed: should goal.md declare a new focus?\n"
        "- Approve or reject TB-171 / TB-172 / TB-173.\n"
        "- Operator input required: rotate focus item?\n",
    )
    page = web._render_home(project)
    # Card class present.
    assert 'class="operator-decisions"' in page
    # Header carries the count.
    assert "3 decisions needed from operator" in page
    # Each bullet rendered as one `<li>`.
    assert (
        "<li>Decision needed: should goal.md declare a new focus?</li>"
    ) in page
    assert (
        "<li>Approve or reject TB-171 / TB-172 / TB-173.</li>"
    ) in page
    assert "<li>Operator input required: rotate focus item?</li>" in page


def test_operator_decisions_card_omitted_when_empty(project: Config):
    """No file / no section / empty section → card omitted entirely from
    `/`, not just CSS-hidden. The `.operator-decisions` selector lives
    in the page `<style>` so we scope the assertion to the post-`</style>`
    body — that's where a rendered card would land."""
    # Case 1: file does not exist (`project` fixture doesn't seed one).
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="operator-decisions"' not in body
    assert "decisions needed" not in body.lower()

    # Case 2: file exists but no `## Decisions needed from operator` section.
    _seed_ideation_state(
        project,
        "# Ideation State\n\n## Mission alignment\n\n- nothing\n",
    )
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="operator-decisions"' not in body
    assert "decisions needed" not in body.lower()

    # Case 3: section header present but empty body.
    _seed_ideation_state(
        project,
        "## Decisions needed from operator\n\n## Proposals this cycle\n\n- TB-1\n",
    )
    page = web._render_home(project)
    body = page.split("</style>", 1)[1]
    assert 'class="operator-decisions"' not in body


def test_operator_decisions_card_renders_above_pending_queue(project: Config):
    """When BOTH cards have content, the operator-decisions card renders
    ABOVE the pending-queue card on `/` so ideator-surfaced operator-
    judgement work gets visual priority over mechanical pending ops."""
    _seed_ideation_state(
        project,
        "## Decisions needed from operator\n\n"
        "- Decision needed: should we declare verifier robustness as the next focus?\n",
    )
    _seed_queue_entry(
        project,
        uuid="aaaaaaaa-1111-2222-3333-444444444444",
        op="approve",
        args={"task_id": "TB-99"},
    )
    page = web._render_home(project)
    od_idx = page.find('class="operator-decisions"')
    pq_idx = page.find('class="pending-queue"')
    assert od_idx >= 0
    assert pq_idx >= 0
    assert od_idx < pq_idx, (
        f"operator-decisions card should render above pending-queue card; "
        f"got operator-decisions at {od_idx}, pending-queue at {pq_idx}"
    )


def test_operator_decisions_card_escapes_html(project: Config):
    """Bullet bodies are HTML-escaped before rendering — defends against
    an ideator (or some future adversarial input) writing a `<script>`
    tag into the section body."""
    _seed_ideation_state(
        project,
        "## Decisions needed from operator\n\n"
        "- Should we use `<script>` tags? & other HTML\n",
    )
    page = web._render_home(project)
    # Locate the card's `<li>` row — that's where bullet content lands.
    li_start = page.find("<li>", page.find('class="operator-decisions"'))
    li_end = page.find("</li>", li_start)
    li = page[li_start:li_end]
    # Raw `<script>` must not survive escaping; entities must be present.
    assert "<script>" not in li
    assert "&lt;script&gt;" in li
    assert "&amp;" in li


def test_operator_decisions_helper_is_grep_visible():
    """Mirrors `test_pending_queue_helper_is_grep_visible` — pins the
    helper name + CSS class to the web module family so a refactor
    that drops either silently breaks the operator-facing card.
    TB-265: the helper lives in `web_home.py` and the CSS class in
    `web_chrome.py` post-split."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    text = "\n".join(p.read_text() for p in sorted(root.glob("web*.py")))
    assert "def _render_operator_decisions" in text
    assert "operator-decisions" in text
    assert "parse_operator_decisions" in text


def test_operator_decisions_card_does_not_leak_cycle_observations(
    project: Config,
):
    """TB-191: when both `## Decisions needed from operator` AND
    `## Cycle observations` sit in `ideation_state.md`, the home page
    surfaces ONLY the decisions bullets — observations content is
    structurally excluded by `parse_operator_decisions` and must never
    appear inside the rendered card. Pinned at the home-page level
    (not just the parser level) so a refactor that re-routes the card
    can't silently regress the leak guard."""
    _seed_ideation_state(
        project,
        "# Ideation State\n\n"
        "## Cycle observations\n\n"
        "- n=3 retries on bullet kind Y this week.\n"
        "- No unadopted cron_proposed events.\n"
        "- Cadence is steady at 12 ticks/min.\n\n"
        "## Decisions needed from operator\n\n"
        "- Decision needed: approve TB-200?\n"
        "- Operator input required: rotate focus to verifier robustness?\n",
    )
    page = web._render_home(project)
    # Card present with the right count.
    assert 'class="operator-decisions"' in page
    assert "2 decisions needed from operator" in page
    # The decisions bullets land as `<li>` rows.
    assert "<li>Decision needed: approve TB-200?</li>" in page
    assert (
        "<li>Operator input required: rotate focus to verifier robustness?</li>"
    ) in page
    # None of the cycle-observations content reaches the rendered page.
    body = page.split("</style>", 1)[1]
    for forbidden in (
        "n=3 retries on bullet kind Y",
        "No unadopted cron_proposed events",
        "Cadence is steady at 12 ticks/min",
    ):
        assert forbidden not in body, (
            f"TB-191: cycle-observations bullet leaked into the rendered "
            f"home page body: {forbidden!r}"
        )


# --------- TB-181: home page links to /usage dashboard ---------


def test_home_page_links_to_usage(project: Config):
    """TB-181 scope-item gate: the home page contains an `<a
    href="/usage">` link so the dashboard is reachable from the
    existing nav bar without typing the URL."""
    h = web._render_home(project)
    assert 'href="/usage"' in h


# ---------------------------------------------------------------------------
# TB-197: ideation gate-state card on `/`.
#
# `_render_ideation_status_block(cfg)` emits a compact 1-2 line card whose
# tint and headline reflect the current ideation gate state — mirrors the
# daemon's `_maybe_ideate` decision logic so the operator can answer
# "when does ideation next fire?" without grepping `cron_state.json`. Five
# state variants: eligible / cooldown / active_running / queued_full /
# disabled. Tests below pin each variant's shape, the gate-priority
# ordering when multiple gates would block, and the helper's grep-visibility.

import json as _tb197_json
import time as _tb197_time


def _tb197_project(tmp_path: Path) -> Config:
    """Fresh project with 0 Active + small Backlog — the steady-state shape
    most TB-197 tests start from. Individual tests then layer on board
    edits / cron state / env knobs as needed."""
    (tmp_path / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-10** **first backlog item**\n"
        "- [ ] **TB-11** **second backlog item**\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    cfg = Config.load(tmp_path)
    cfg.ensure_dirs()
    return cfg


def _seed_cron_state_ideation(cfg: Config, *, seconds_ago: float) -> None:
    """Write `cron_state.json` so `IDEATION_NAME` last fired
    `seconds_ago` seconds ago. Mirrors `cron.mark_run`'s on-disk shape
    (a flat `{name: unix_ts}` dict)."""
    from ap2.ideation import IDEATION_NAME

    cfg.cron_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.cron_state_file.write_text(
        _tb197_json.dumps(
            {IDEATION_NAME: _tb197_time.time() - seconds_ago},
            indent=2, sort_keys=True,
        )
    )


def test_ideation_status_card_cooldown_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Briefing pin: cron_state last-fire 30min ago + cooldown=2h + 0
    Active + 2 queued (under threshold 5) → renders a `cooldown` card
    that carries BOTH the absolute next-eligible timestamp AND a
    relative remaining-duration string. Operator can compute "is this
    soon?" without doing math."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _tb197_project(tmp_path)
    _seed_cron_state_ideation(cfg, seconds_ago=30 * 60)  # 30 min ago

    html_block = web._render_ideation_status_block(cfg)
    assert 'class="ideation-status is-cooldown"' in html_block
    # Relative duration: 7200 - 1800 = 5400s remaining = 90 min = "1h 30m".
    # Round-up rounding and 30s-tick granularity make this a stable string
    # but allow a small slop for the (now - last) read to slip a few seconds.
    assert ("1h 30m" in html_block or "1h 29m" in html_block)
    assert "remaining" in html_block
    # Absolute next-eligible timestamp is present in ISO Z form so the
    # operator can correlate against system time.
    import re as _re
    iso_z = _re.search(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", html_block,
    )
    assert iso_z, f"no ISO-Z timestamp in cooldown card: {html_block!r}"
    # The card sits inside the rendered home page too.
    page = web._render_home(cfg)
    assert 'class="ideation-status is-cooldown"' in page


def test_ideation_status_card_eligible_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Cooldown elapsed (last fire 3h ago, cooldown 2h) + 0 Active +
    queued under threshold → eligible card. No "cooldown remaining"
    wording leaks; "next tick" semantics surface."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _tb197_project(tmp_path)
    _seed_cron_state_ideation(cfg, seconds_ago=3 * 3600)  # 3h ago

    html_block = web._render_ideation_status_block(cfg)
    assert 'class="ideation-status is-eligible"' in html_block
    assert "next tick" in html_block
    # No cooldown wording — would be misleading on an eligible card.
    assert "cooldown" not in html_block.lower()
    assert "remaining" not in html_block


def test_ideation_status_card_active_running_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """1 Active task + recent last-fire → active_running blocker
    (the hard gate runs BEFORE cooldown / threshold). The card names
    the blocker as "Active task in flight"; cooldown / queue wording
    must not leak (they're irrelevant once Active blocks)."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _tb197_project(tmp_path)
    # Add an Active task (replace TASKS.md with a board carrying one).
    (cfg.project_root / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n- [ ] **TB-50** **in-flight task**\n"
        "## Ready\n\n"
        "## Backlog\n\n- [ ] **TB-51** **queued**\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    _seed_cron_state_ideation(cfg, seconds_ago=60)  # 1 min ago — well within cooldown

    html_block = web._render_ideation_status_block(cfg)
    assert 'class="ideation-status is-blocked"' in html_block
    assert "Active task in flight" in html_block
    # Cooldown / queue wording must not leak — those gates aren't reached
    # once the active hard-gate fires.
    assert "cooldown" not in html_block.lower()
    assert "queue" not in html_block.lower()
    assert "≥ threshold" not in html_block


def test_ideation_status_card_queued_full_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """0 Active + Ready+Backlog == threshold + cooldown elapsed →
    queued_full blocker. Card surfaces both the actual count AND the
    threshold value (e.g. "5 ≥ threshold 5") so the operator can
    sanity-check `AP2_IDEATION_TRIGGER_TASK_COUNT` inline."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "3")
    cfg = _tb197_project(tmp_path)
    # 3 Backlog items — at-threshold.
    (cfg.project_root / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "- [ ] **TB-60** **a**\n"
        "- [ ] **TB-61** **b**\n"
        "- [ ] **TB-62** **c**\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    _seed_cron_state_ideation(cfg, seconds_ago=10 * 3600)  # cooldown elapsed

    html_block = web._render_ideation_status_block(cfg)
    assert 'class="ideation-status is-blocked"' in html_block
    # Both the count and the threshold appear in the same line so the
    # operator can sanity-check the env knob without leaving the card.
    assert "3" in html_block
    assert "threshold 3" in html_block
    assert "≥ threshold" in html_block
    # Names the env knob so the operator knows which knob to tune.
    assert "AP2_IDEATION_TRIGGER_TASK_COUNT" in html_block


def test_ideation_status_card_disabled_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """`AP2_IDEATION_DISABLED=1` set in env → disabled card; the env
    knob name surfaces VERBATIM so the operator can grep their env file
    without guessing the exact variable name."""
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    cfg = _tb197_project(tmp_path)

    html_block = web._render_ideation_status_block(cfg)
    assert 'class="ideation-status is-disabled"' in html_block
    assert "disabled" in html_block.lower()
    assert "AP2_IDEATION_DISABLED" in html_block


def test_ideation_status_card_gate_priority_disabled_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When MULTIPLE gates would block (disabled AND active AND
    cooldown not elapsed), the card reports ONLY the FIRST-checked gate
    per `_maybe_ideate`'s order. Disabled wins over everything because
    it short-circuits the daemon's gate chain at step 1 — reporting any
    deeper gate would mislead the operator."""
    monkeypatch.setenv("AP2_IDEATION_DISABLED", "1")
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "3")
    cfg = _tb197_project(tmp_path)
    # Layer on an Active task AND recent last-fire so disabled is one of
    # several would-be blockers. The daemon's `_maybe_ideate` returns at
    # the first gate (disabled) and never evaluates the others — the
    # card must mirror that semantics.
    (cfg.project_root / "TASKS.md").write_text(
        "# Tasks\n\n"
        "## Active\n\n- [ ] **TB-99** **active**\n"
        "## Ready\n\n"
        "## Backlog\n\n"
        "## Pipeline Pending\n\n"
        "## Complete\n\n"
        "## Frozen\n"
    )
    _seed_cron_state_ideation(cfg, seconds_ago=60)  # well within cooldown

    html_block = web._render_ideation_status_block(cfg)
    # Disabled is the only state class that should appear.
    assert "is-disabled" in html_block
    assert "is-blocked" not in html_block
    assert "is-cooldown" not in html_block
    assert "is-eligible" not in html_block
    # No "Active task" wording leaks — disabled is the only blocker reported.
    assert "Active task in flight" not in html_block
    assert "cooldown" not in html_block.lower()


def test_ideation_status_card_omits_cooldown_when_never_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """No `cron_state.json` on disk (never-fired project) and no other
    blockers → eligible card. The daemon treats `last_fire_unix=None`
    as "elapsed forever" so the card mirrors that semantics."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _tb197_project(tmp_path)
    # Do NOT seed cron_state.json — fresh project, ideation never fired.
    assert not cfg.cron_state_file.exists()

    html_block = web._render_ideation_status_block(cfg)
    assert 'class="ideation-status is-eligible"' in html_block


def test_ideation_status_card_always_renders_on_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Briefing pin: the card is small (1-2 lines) and ALWAYS rendered
    on `/`. No omit-on-empty path — even on the steady-state happy path
    the operator gets a synchronous gate-state read."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    monkeypatch.setenv("AP2_IDEATION_TRIGGER_TASK_COUNT", "5")
    cfg = _tb197_project(tmp_path)

    page = web._render_home(cfg)
    body = page.split("</style>", 1)[1]
    assert 'class="ideation-status' in body


def test_ideation_status_helper_is_grep_visible():
    """Mirrors `test_pending_queue_helper_is_grep_visible` and
    `test_operator_decisions_helper_is_grep_visible` — the briefing's
    verification bullets pin both the helper names AND the CSS class
    name to the web module family so a refactor that drops any of them
    silently breaks the operator-facing card. TB-265: post-split,
    both helpers live in `web_home.py` and the CSS class in
    `web_chrome.py`."""
    from pathlib import Path as _P

    root = _P(web.__file__).resolve().parent
    text = "\n".join(p.read_text() for p in sorted(root.glob("web*.py")))
    assert "def _render_ideation_status_block" in text
    assert "def _ideation_gate_state" in text
    assert "ideation-status" in text


def test_ideation_status_card_escapes_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Defense-in-depth: the rendered card's dynamic fields (timestamps,
    duration strings) flow through `html.escape` even though they're
    derived from int/datetime computations today — guards against a
    future refactor that introduces user-controlled content into the
    card without re-auditing the escape path."""
    monkeypatch.delenv("AP2_IDEATION_DISABLED", raising=False)
    monkeypatch.setenv("AP2_IDEATION_COOLDOWN_S", "7200")
    cfg = _tb197_project(tmp_path)
    _seed_cron_state_ideation(cfg, seconds_ago=30 * 60)

    html_block = web._render_ideation_status_block(cfg)
    # No raw `<script>` should ever land on the card; sanity check that
    # the card body uses normal HTML tags only.
    assert "<script>" not in html_block
    assert "</script>" not in html_block


# --------- TB-265 / TB-260: env-stale WARN rendering on the web home ---------


def test_env_stale_warning_omitted_when_fresh(project: Config):
    """Default-off byte-identical contract: `_render_env_stale_warning`
    returns `""` when `.cc-autopilot/env`'s mtime is not stale relative
    to the daemon-start baseline (the steady-state happy path)."""
    # The `project` fixture writes no `.cc-autopilot/env` and no
    # baseline-mtime stash, so `collect_env_staleness` reports
    # `env_stale: False`.
    assert web._render_env_stale_warning(project) == ""
    # And the full home page renders without the WARN line either.
    home = web._render_home(project)
    assert "env-stale" not in home


def test_env_stale_warning_emits_warn_line_when_stale(
    project: Config, monkeypatch: pytest.MonkeyPatch,
):
    """When `collect_env_staleness` reports `env_stale: True`, the home
    page surfaces a WARN-tinted card with both timestamps and the
    `ap2 stop && ap2 start` remediation command (mirrors the
    `cmd_status` text-mode WARN line). Pins the TB-265 prose
    verification bullet: TB-260's env-stale rendering on the web home
    is preserved end-to-end."""
    from ap2 import automation_status

    fake_state = {
        "env_stale": True,
        "env_file_mtime": "2026-05-19T20:00:00Z",
        "env_file_mtime_at_start": "2026-05-19T18:00:00Z",
    }
    monkeypatch.setattr(
        automation_status, "collect_env_staleness",
        lambda cfg: fake_state,
    )

    block = web._render_env_stale_warning(project)
    assert "env-stale" in block
    assert "WARN" in block
    assert "2026-05-19T20:00:00Z" in block  # live mtime
    assert "2026-05-19T18:00:00Z" in block  # baseline mtime
    assert "ap2 stop" in block and "ap2 start" in block

    # And the full home page includes the WARN card.
    home = web._render_home(project)
    assert "env-stale" in home
    assert "WARN" in home
    assert "ap2 stop" in home


def test_env_stale_warning_escapes_timestamps(
    project: Config, monkeypatch: pytest.MonkeyPatch,
):
    """Defense-in-depth: the rendered card's timestamp fields flow
    through `html.escape`, so a malformed mtime string can't inject
    raw markup into the page."""
    from ap2 import automation_status

    monkeypatch.setattr(
        automation_status, "collect_env_staleness",
        lambda cfg: {
            "env_stale": True,
            "env_file_mtime": "<script>alert(1)</script>",
            "env_file_mtime_at_start": "<b>baseline</b>",
        },
    )

    block = web._render_env_stale_warning(project)
    assert "<script>" not in block
    assert "&lt;script&gt;" in block
    assert "&lt;b&gt;baseline&lt;/b&gt;" in block
