"""mattermost channel adapter — thin package shim (TB-343; TB-389 demotion).

TB-389 demoted mattermost from a top-level component to a channel adapter
owned by the `communication` component — this package no longer ships a
``manifest.py``, so the registry does not discover it as a loop
participant. The implementation lives in the sibling :mod:`impl` module;
this ``__init__`` re-exports the public surface so ``import
ap2.components.mattermost`` and every ``from ap2.components.mattermost
import X`` call site (now the ``communication`` component's internal
channel registry + manifest) keep resolving unchanged.

TB-343 moved the module body out of ``__init__.py`` into ``impl.py``
(``git mv``, history-preserving) to match the conventional package
shape. The re-export list below is the component's symbol surface; the
mutable ``_TEAM_CACHE`` lookup cache stays private to ``impl`` (it is
rebound via ``global``).

Note for tests that stub the HTTP/post seams (``_api_get``,
``fetch_thread``): patch them on the implementation module
(``ap2.components.mattermost.impl``) — the intra-body callers resolve
those names in ``impl``'s namespace, so a patch on this package shim
would not be seen by ``check_new_messages`` et al.
"""
from .impl import (
    MattermostChannelAdapter,
    _api_get,
    _bot_user_id,
    _channels_to_watch,
    _err,
    _load_state,
    _mention,
    _mm_lookup_channel,
    _mm_post,
    _mm_user_team,
    _normalize,
    _ok,
    _save_state,
    _thread_has_mention,
    _trim_cache,
    check_new_messages,
    do_mattermost_reply,
    do_mattermost_thread_read,
    fetch_thread,
)

__all__ = [
    "MattermostChannelAdapter",
    "_api_get",
    "_bot_user_id",
    "_channels_to_watch",
    "_err",
    "_load_state",
    "_mention",
    "_mm_lookup_channel",
    "_mm_post",
    "_mm_user_team",
    "_normalize",
    "_ok",
    "_save_state",
    "_thread_has_mention",
    "_trim_cache",
    "check_new_messages",
    "do_mattermost_reply",
    "do_mattermost_thread_read",
    "fetch_thread",
]
