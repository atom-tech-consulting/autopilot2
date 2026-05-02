# Add `mattermost_thread_read` MCP tool for chat conversation context

## Goal

Give the Mattermost handler agent access to the rest of the messages in a thread, not just the single message that triggered it. Adds a new MCP tool `mcp__autopilot__mattermost_thread_read(thread_id: str, max_messages: int = 50)` that fetches all posts in the thread (root + replies) and returns a chronologically-ordered list of `{user, text, create_at, post_id}` dicts.

## Why

The handler today receives a per-message dict with `text` + `thread_id` + sender, no thread history. So a thread-reply like `"yes"` to a question the bot asked 10 minutes ago lands with no context — the handler can't tell what "yes" approves. Concrete UX hits:

- **Thread-reply approval**: bot asks "approve TB-N? (yes/no)", operator replies "yes" in-thread → handler doesn't know what it's approving, so today the operator has to repeat the TB-N in their reply.
- **Multi-turn debugging from chat**: operator pastes a stack trace, then in a follow-up "what about the `verify_task` line?" — handler has no memory of the trace.
- **Conversational refinement**: bot reports a task summary, operator nudges "no, just the failing tests" — handler has no anchor.

The compensating tool today is `Read` against events.jsonl, but events don't capture the chat thread bodies (they capture *replies* the bot sent via `mattermost_reply`, which is enough to reconstruct half the conversation, but operator messages on a third-party server aren't there). The clean answer is letting the handler ask Mattermost directly.

## Scope

(1) New handler function in `ap2/mattermost.py` alongside `check_new_messages`:

```python
def fetch_thread(thread_id: str, *, max_messages: int = 50) -> list[dict]:
    """Fetch all posts in a thread via /api/v4/posts/{thread_id}/thread.
    Returns chronologically-ordered list of dicts with user / text /
    create_at / post_id. Resolves user_ids to display names via the
    same cache used by check_new_messages."""
```

The Mattermost endpoint `/api/v4/posts/{post_id}/thread` returns `{order: [...], posts: {pid: post, ...}}`; we re-use the same `_api_get`, user-name cache, and channel-name cache that already exist in `mattermost.py`.

(2) New MCP tool in `ap2/tools.py`:

```python
@tool(
    "mattermost_thread_read",
    "Fetch all messages in a Mattermost thread (root + replies). Use when "
    "the user's incoming message is a thread reply and you need context "
    "from earlier in the conversation. `thread_id` is the post id of "
    "the thread root (the `thread_id` field on the incoming message). "
    "`max_messages` defaults to 50; thread reads are local-only HTTP "
    "to the Mattermost server, not Anthropic-side tool budget.",
    {"thread_id": str, "max_messages": int},
)
async def mattermost_thread_read(args):
    return do_mattermost_thread_read(cfg, args)
```

(3) Add `mcp__autopilot__mattermost_thread_read` to `MM_HANDLER_TOOLS` (and only there — task agents and crons don't need it). Stays out of `CONTROL_AGENT_TOOLS` since cron jobs / ideation don't have thread context to read.

(4) Update `prompts.build_mattermost_prompt`:
   - When the incoming message has a non-empty `thread_id`, instruct the handler: "This message is a thread reply. Call `mattermost_thread_read(thread_id='<id>')` first to read prior messages, then act on the user's intent in context."
   - When `thread_id` is empty (top-level mention), no instruction — the message is self-contained.

(5) Skip behavior when Mattermost env is missing (matches `check_new_messages`): `do_mattermost_thread_read` returns `_err("mattermost not configured")` rather than raising. Tool stays callable but reports the misconfiguration so the handler can fall back to `mattermost_reply` with "I can't read thread history right now."

## Verification

- `uv run pytest -q ap2/tests/` — full regression gate passes (gating)
- `python3 -c "from ap2.tools import MM_HANDLER_TOOLS; assert 'mcp__autopilot__mattermost_thread_read' in MM_HANDLER_TOOLS"` — tool wired into the handler toolset.
- `python3 -c "from ap2.tools import CONTROL_AGENT_TOOLS, TASK_AGENT_TOOLS; assert 'mcp__autopilot__mattermost_thread_read' not in CONTROL_AGENT_TOOLS + TASK_AGENT_TOOLS"` — not in control / task toolsets (scope discipline).
- New unit test in `test_mattermost.py`: `fetch_thread` mocks `_api_get` to return a fake thread payload (3 posts, mixed users), asserts the returned list is chronologically ordered and resolves user_ids to display names via the cache.
- New unit test in `test_mattermost.py`: `fetch_thread` with `max_messages=2` truncates from the OLDEST end (drops the deepest history first), keeping the most recent N.
- New unit test in `test_tools.py`: `do_mattermost_thread_read` returns `_err` when MATTERMOST_URL/MATTERMOST_TOKEN are unset, doesn't raise.
- New unit test in `test_prompts.py`: the MM handler prompt for a message with non-empty `thread_id` includes a sentence instructing the agent to call `mattermost_thread_read`. For top-level messages (empty `thread_id`), the instruction is absent.
- New e2e test (`tests/e2e/`): inject a thread-reply message into the handler with a stubbed `mattermost_thread_read` returning two prior messages; assert the handler invokes the tool exactly once and the resulting `mattermost_reply` references content from the prior messages (e.g. by including the TB-N from the bot's earlier question).

## Out of scope

- Caching thread reads across handler invocations — each invocation is a fresh SDK call; cheap enough to refetch.
- Tool support for posting to a specific thread (replying-in-thread is already implicit via `mattermost_reply` if it accepts thread_id; if not, that's a separate scope).
- Streaming or pagination beyond `max_messages` — typical operator threads are <50 posts; bigger threads can be summarized server-side later.
- Channel history (non-thread). If operator wants the bot to see general channel context, that's a different tool with different cost — file separately if needed.
