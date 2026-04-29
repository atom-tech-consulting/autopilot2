"""Real-SDK smoke tests for ap2 — opt-in only.

These tests make real Claude API calls (cost real money). They only run
when `AP2_REAL_SDK` is set in the environment:

    AP2_REAL_SDK=1 uv run pytest ap2/tests/smoke/ -v -s

The default `pytest` invocation skips them.
"""
