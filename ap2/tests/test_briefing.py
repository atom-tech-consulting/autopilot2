"""Tests for the briefing template + Verification-section parser (TB-69)."""
from __future__ import annotations

from ap2.init import BRIEFING_TEMPLATE, render_briefing
from ap2.verify import VerifyBullet, parse_verification_section


def test_render_briefing_idempotent():
    """Same inputs → identical output (formatter is pure)."""
    a = render_briefing(task_id="TB-99", title="My task", description="Do X")
    b = render_briefing(task_id="TB-99", title="My task", description="Do X")
    assert a == b


def test_render_briefing_includes_verification_section():
    """Every templated briefing must have the load-bearing Verification header
    so the per-task verifier (TB-69) has something to evaluate."""
    text = render_briefing(task_id="TB-99", title="t", description="d")
    assert "## Verification" in text
    # The template ships with a default shell-bullet so the verifier
    # actually does something on a fresh task. Operators replace/extend it.
    assert "uv run pytest" in text


def test_render_briefing_fills_id_and_title():
    text = render_briefing(task_id="TB-101", title="Implement foo")
    assert "# TB-101 — Implement foo" in text


def test_render_briefing_uses_placeholder_when_description_empty():
    """Empty description gets a useful placeholder rather than a blank line —
    so the rendered briefing is immediately readable."""
    text = render_briefing(task_id="TB-99", title="t", description="")
    assert "(one-paragraph description" in text


def test_parse_verification_section_returns_none_when_missing():
    """Legacy briefings without `## Verification` get a None — the daemon
    treats this as the skip path so existing tasks keep working."""
    text = "# Task\n\n## Goal\nDo X\n\n## Scope\n- foo.py\n"
    assert parse_verification_section(text) is None


def test_parse_verification_section_empty_bullets():
    """Header present but no bullets → `[]`. Caller treats as skip."""
    text = "# T\n\n## Verification\n\n## Out of scope\n- (nothing)\n"
    assert parse_verification_section(text) == []


def test_parse_verification_extracts_shell_bullets():
    text = (
        "# T\n\n## Verification\n"
        "- `uv run pytest -q` — full suite passes\n"
        "- `ruff check .` — lint clean\n"
        "\n## Out of scope\n"
    )
    bullets = parse_verification_section(text)
    assert len(bullets) == 2
    assert all(b.kind == "shell" for b in bullets)
    assert bullets[0].command == "uv run pytest -q"
    assert bullets[1].command == "ruff check ."


def test_parse_verification_extracts_prose_bullets():
    text = (
        "# T\n\n## Verification\n"
        "- new feature is documented in CLAUDE.md under the right section\n"
        "- `uv run pytest -q` — tests pass\n"
        "\n## Out of scope\n"
    )
    bullets = parse_verification_section(text)
    assert len(bullets) == 2
    assert bullets[0].kind == "prose"
    assert "documented in CLAUDE.md" in bullets[0].text
    assert bullets[1].kind == "shell"


def test_parse_verification_handles_multiline_prose():
    text = (
        "# T\n\n## Verification\n"
        "- a multi-line prose bullet that wraps onto\n"
        "  a continuation line — both should be captured\n"
        "- `echo done` — second bullet\n"
    )
    bullets = parse_verification_section(text)
    assert len(bullets) == 2
    assert bullets[0].kind == "prose"
    assert "continuation line" in bullets[0].text


def test_briefing_template_is_renderable():
    """A defensive smoke: BRIEFING_TEMPLATE itself must accept the .format
    keys that render_briefing uses. Catches accidental {} drift."""
    out = BRIEFING_TEMPLATE.format(task_id="TB-1", title="x", description="y")
    assert "TB-1" in out and "x" in out and "y" in out
