"""auto_approve component subpackage marker (TB-310 stub).

The flat module `ap2/auto_approve.py` still owns the gate logic; the
gate calls remain inline inside `daemon._tick`'s task-dispatch block
because they evaluate per-task state (whether the top-of-Backlog
task was auto-approved, plus four cumulative-failure / cost / blast-
radius / noisy-validator gates). Extracting that block into a
single tick-callable belongs to axis (5) of the components focus
(goal.md L116-201).

This stub registers a no-op `tick_hook` on `POST_DISPATCH` so the
registry-walk-everything contract is uniform — daemon._tick can
walk every phase even when a component's tick-callable is still
inline. When axis (5) extracts the gate logic, the no-op stub
becomes the real gate-application function and the daemon's inline
calls go away.
"""
