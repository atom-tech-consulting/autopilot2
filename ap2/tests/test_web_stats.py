"""TB-267: /stats route group placeholder — mirror of `ap2/web_stats.py`.

The pre-TB-267 monolithic `ap2/tests/test_web.py` carried no tests
specifically exercising the `/stats` + `/stats.json` route group; the
TB-255 stats-dashboard coverage lives in `ap2/tests/test_stats_dashboard.py`
(rendering / window-chip behavior) and `ap2/tests/test_tb259_status_report_stats_window.py`
(status-report integration). This module exists to (a) satisfy the
flat-structure mirror convention from the TB-267 briefing and (b) hold
any future `_render_stats` / `_render_stats_json` tests that grow out
of the web UI side specifically (vs. the cross-surface coverage already
landed in the sibling files above).

No tests collected by design — relocating any unrelated coverage here
would violate the briefing's "pure mechanical move — NO new/renamed
tests" rule.
"""
from __future__ import annotations
