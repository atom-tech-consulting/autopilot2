"""auto_unfreeze component subpackage marker (TB-310 stub).

The flat module `ap2/auto_unfreeze.py` still owns the implementation —
this subpackage exists only to register the component via
`manifest.py` so `Registry.discover()` picks it up alongside the
canary `janitor/` subpackage. The structural relocation (moving the
flat module's contents into `__init__.py` here) belongs to axis (5)
of the components focus (goal.md L116-201); axis (2)'s job is to
land the registry-driven dispatch contract first, then individual
migrations follow.
"""
