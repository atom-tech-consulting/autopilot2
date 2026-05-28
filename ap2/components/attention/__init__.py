"""attention component subpackage marker (TB-310 stub).

The flat module `ap2/attention.py` still owns the detector
implementations; the daemon-side wire-up
(`daemon._maybe_emit_attention_events` — wraps detection + debounce
+ event emission + opt-in Mattermost push) currently lives in
`ap2/daemon.py` and is reached through a late-binding import in
`manifest.py` here. The structural relocation (moving the wire-up
helper out of daemon.py into the component subpackage) belongs to
axis (5) of the components focus (goal.md L116-201).
"""
