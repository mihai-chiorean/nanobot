"""Hand-off of long private websocket chat turns to Work (MIT-1855).

Design doc ``docs/design/onboarding-and-approval-tuning.md`` §4 (product
repo): when a private websocket chat turn has run for
``agents.defaults.backgroundHandoffSeconds`` (default 45 s) and is about to
execute another tool batch, the rest of the work moves to a Work task and the
chat is freed.

Three constraints shape the mechanism:

* A hook cannot cancel its own turn task: ``CompositeHook._for_each_hook_safe``
  swallows hook exceptions unless the hook sets ``_reraise = True``, and
  ``AgentRunner.run`` turns any other exception into ``stop_reason="error"``
  plus ``on_error``. So the hand-off is a ``HandoffRequested`` exception the
  runner recognises as a stop, raised by a ``_reraise`` hook.
* Writes must not be repeated. A Gmail re-proposal is keyed by the model's own
  ``request_key`` and its argument hash includes the model's reason, so a
  re-run would create a second approval. A turn whose executed tool names
  contain any write tool -- or whose tool results carry an
  ``approval_required`` status -- never hands off.
* The transcript must stay valid. The raise fires in ``before_execute_tools``,
  so the pending batch never runs; the loop keeps history only up to the last
  completed tool result and drops the assistant message whose tool calls never
  executed.
"""

from __future__ import annotations

import json
import re
from time import monotonic
from typing import Any, Literal

from nanobot.agent.hook import AgentHook, AgentHookContext

HandoffReason = Literal["time", "signin"]

# The fixed chat replies. ``handoff_task_id`` rides the outbound metadata when
# the task was created, so the apps can link the chat to the Work task.
HANDOFF_REPLY = (
    "This is taking a while. I'll keep going in Work and let you know when it's done."
)
HANDOFF_UNAVAILABLE_REPLY = "I couldn't move this to Work; please ask again."

# Every Gmail mutation tool (exact MCP names from the connectors'
# ``mcp_gmail_write.go``) plus the browser tools that act on the user's
# signed-in session: ``browser_act`` clicks/sends/saves/posts/deletes/buys
# (and follows download links) and ``browser_fill_form`` can submit a form.
# Gmail reads (``gmail_search``, ``gmail_get_message``, ``gmail_list_labels``,
# ``gmail_connection_status``) and browser reads (``browser_open``,
# ``browser_find``, ``browser_read_page``) deliberately stay out of this set:
# re-running them in Work is harmless.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "gmail_modify_labels",
        "gmail_archive_message",
        "gmail_trash_message",
        "gmail_trash_messages",
        "gmail_create_draft",
        "gmail_send_message",
        "browser_act",
        "browser_fill_form",
    }
)

# MCP tools reach the agent as ``mcp_<server>_<tool>`` (``mcp.py``), and the
# connector server key is tenant-settable (``ziggy``, legacy ``ziggy_gmail``),
# so names match on their trailing tool segment rather than a fixed prefix.
def is_write_tool_name(name: str | None) -> bool:
    """Whether a tool name (as seen by the agent) is a write that must not re-run."""
    if not name:
        return False
    return any(name == tool or name.endswith(f"_{tool}") for tool in WRITE_TOOL_NAMES)


# Gmail/browser mutations that need a tap answer with a structured
# ``{"status": "approval_required", ...}`` result; the approval is already
# outstanding, so a Work re-run would propose it a second time.
_APPROVAL_REQUIRED_RE = re.compile(r'"status"\s*:\s*"approval_required"')


def result_requests_approval(result: Any) -> bool:
    """Whether a tool result reports an outstanding ``approval_required``."""
    text = result if isinstance(result, str) else str(result or "")
    if "approval_required" not in text:
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        return _APPROVAL_REQUIRED_RE.search(text) is not None
    return isinstance(parsed, dict) and parsed.get("status") == "approval_required"


class HandoffRequested(Exception):  # noqa: N818
    """Recognised stop: the runner passes it up without error bookkeeping.

    Not an ``Error`` by design (the issue names it ``HandoffRequested``): it
    reports work moving to the background, not a failure.
    """

    def __init__(self, reason: HandoffReason) -> None:
        super().__init__(reason)
        self.reason: HandoffReason = reason


class HandoffHook(AgentHook):
    """Raise ``HandoffRequested`` at a safe tool boundary once the turn is long.

    ``_reraise`` keeps ``CompositeHook`` from swallowing the exception; the
    runner treats it as a stop, not a failure. The hook also records whether
    the turn has done any write (or has an approval outstanding), which
    disqualifies the turn from hand-off permanently.
    """

    def __init__(self, *, threshold: float, started_at: float | None = None) -> None:
        super().__init__(reraise=True)
        self.threshold = threshold
        self.started_at = monotonic() if started_at is None else started_at
        self.saw_write = False
        # The live runner transcript, captured at the last tool boundary so
        # the loop can build the kept history after the raise.
        self.transcript: list[dict[str, Any]] | None = None

    async def after_iteration(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            if is_write_tool_name(call.name):
                self.saw_write = True
                return
        for result in context.tool_results:
            if result_requests_approval(result):
                self.saw_write = True
                return

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self.transcript = context.messages
        if self.saw_write or self.threshold <= 0:
            return
        if monotonic() - self.started_at >= self.threshold:
            raise HandoffRequested("time")
